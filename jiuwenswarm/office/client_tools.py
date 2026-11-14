# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The tools of an Office app's contract, which the task pane of the attached document runs.

Each tool of the contract is a ``ClientTool``: once every ``before_tool_call``
rail (permissions among them) lets a call through, the turn pauses and the
task pane gets the call's arguments, plus what ``build_request`` returns
(``downloads``: a link for each asset the arguments name). The task pane's
result resumes the turn and ``complete`` makes it the tool output.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.tool import (
    ClientTool,
    ClientToolContext,
    ClientToolResult,
    ClientToolUnavailable,
    ToolCard,
    ToolOutput,
    default_client_output,
)

from jiuwenswarm.office.assets import OfficeAssetStore, referenced_handles
from jiuwenswarm.office.contract import load_contract
from jiuwenswarm.office.review import (
    ReviewFailed,
    geometry_summary,
    review_checklist,
    review_renders,
    verifier_error,
    verifier_result,
    vision_client_config,
)
from jiuwenswarm.office.runtime import image_attachment

_VISION_TOOL_HINT = (
    "If a vision tool is configured, call visual_question_answering with visual_attachment.path to inspect the "
    "pixels. This is a file in this conversation's uploads. Do not use shell to search the user's documents or "
    "another conversation's assets."
)

# One document review judges at most this many pages, from the first; the industry add-ins stop at six too.
_REVIEW_PAGE_LIMIT = 6


class TaskPaneTool(ClientTool):
    """A contract tool, sent to the task pane of the document attached to the call's conversation."""

    def __init__(self, card: ToolCard, *, native_image_model: Callable[[], Model | None],
                 store: OfficeAssetStore) -> None:
        super().__init__(card)
        self._native_image_model = native_image_model
        self._store = store

    def build_request(self, arguments: dict[str, Any], context: ClientToolContext) -> dict[str, Any]:
        """Refuse a call no task pane can run now; give the task pane the links to the assets it needs.

        A call while the document is closed fails at once instead of
        holding the turn until the attachment sweep answers it ``client_gone``.
        """
        from jiuwenswarm.server.client_attachments import get_attachment_directory, load_client_capabilities

        session_id = context.session_id
        capabilities = load_client_capabilities(session_id) if session_id else None
        if capabilities is None:
            raise ClientToolUnavailable("no document is attached to this conversation")
        if self.card.name not in (capabilities.get("supported_tools") or ()):
            raise ClientToolUnavailable(
                f"this add-in does not offer {self.card.name} yet, in any version of this Office app; do the work "
                "with the tools you have, or tell the user this add-in cannot do it yet")
        if get_attachment_directory().connection_for_session(session_id) is None:
            raise ClientToolUnavailable(
                "this conversation's document is not open right now; ask the user to open it and continue in "
                "its task pane", retryable=True)
        try:
            downloads = {handle: self._store.download_url(session_id, handle)
                         for handle in sorted(referenced_handles(arguments))}
        except (ValueError, OSError) as exc:
            raise ClientToolUnavailable(f"{exc}; publish the file with publish_office_asset first") from exc
        return {"downloads": downloads}

    def complete(self, arguments: dict[str, Any], result: ClientToolResult, context: ClientToolContext) -> ToolOutput:
        """The task pane's result; an image in it reaches the model once, or is stored in the conversation's uploads.

        A model with native image input sees the image in the next model call
        only (``keep_in_context: False``). Any other model gets the path of the
        image in the conversation's uploads, for ``visual_question_answering``.
        """
        output = default_client_output(result, tool_name=self.card.name)
        images = (output.data or {}).get("multimodal")
        if not output.success or not images:
            return output
        if self._native_image_model() is not None:
            for image in images:
                image["keep_in_context"] = False
            return output
        data = dict(output.data)
        del data["multimodal"]
        try:
            attachments = [_stored_image(context.session_id, image["data_url"]) for image in images]
        except (ValueError, OSError) as exc:
            data["content"] = f"{data['content']}\nThe returned image could not be stored: {exc}".strip()
            return output.model_copy(update={"data": data})
        lines = [f"visual_attachment.path: {attachment['path']}" for attachment in attachments]
        data["content"] = "\n".join([data["content"], *lines, _VISION_TOOL_HINT]).strip()
        return output.model_copy(update={"data": data})


@dataclass(frozen=True)
class Capture:
    """What the task pane captured for a visual review: the renders, the evidence read with them, what they show."""

    renders: list[str]
    evidence: str
    # Names what was reviewed, as attributes of the verifier result (``slide_id``).
    target: dict[str, str]


class VisualReviewTool(TaskPaneTool):
    """A visual check: the task pane captures renders, a model that saw nothing else reviews them.

    One pause: the task pane answers with the capture, which the tool's entry
    in ``_VISUAL_REVIEWS`` reads, and ``complete`` runs the review against the
    checklist of the app's package (``review.md``), by this conversation's
    model when it takes images, else by ``models.vision``. The author gets the
    verdict with the same renders, which reach it as any other contract tool's
    images do.
    """

    def __init__(self, card: ToolCard, *, checklist: str,
                 capture: Callable[[dict[str, Any], ClientToolResult], Capture],
                 native_image_model: Callable[[], Model | None], store: OfficeAssetStore) -> None:
        super().__init__(card, native_image_model=native_image_model, store=store)
        self._checklist = checklist
        self._capture = capture

    def build_request(self, arguments: dict[str, Any], context: ClientToolContext) -> dict[str, Any]:
        request = super().build_request(arguments, context)
        if self._native_image_model() is None and vision_client_config() is None:
            raise ClientToolUnavailable(
                f"{self.card.name} needs a model that can see images: this conversation's model cannot, "
                "and the vision model (models.vision) is not configured and enabled")
        return request

    def complete(self, arguments: dict[str, Any], result: ClientToolResult,
                 context: ClientToolContext) -> ToolOutput | Awaitable[ToolOutput]:
        if result.failed:
            return super().complete(arguments, result, context)
        return self._reviewed(arguments, result, context)

    async def _reviewed(self, arguments: dict[str, Any], result: ClientToolResult,
                        context: ClientToolContext) -> ToolOutput:
        try:
            capture = self._capture(arguments, result)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"{self.card.name}: {exc}")
        try:
            review = await review_renders(self._checklist, self._native_image_model(), capture.renders,
                                          capture.evidence, arguments.get("expected_changes"))
        except ReviewFailed as exc:
            text = verifier_error(str(exc), **capture.target)
        else:
            text = verifier_result(review, **capture.target)
        images = ({"type": "image", "data_url": render} for render in capture.renders)
        reviewed = ClientToolResult(content=[{"type": "text", "text": text}, *images])
        return super().complete(arguments, reviewed, context)


def _slide_capture(arguments: dict[str, Any], result: ClientToolResult) -> Capture:
    """One slide's render and the geometry the task pane measured on it, for ``verify_slide_visual``."""
    slide_id = arguments["slide_id"]
    output = default_client_output(result)
    images = output.data.get("multimodal") or []
    if len(images) != 1:
        raise ValueError(f"the task pane returned {len(images)} images instead of one slide render")
    try:
        capture = json.loads(output.data["content"])
    except json.JSONDecodeError as exc:
        raise ValueError("the task pane did not describe the captured slide") from exc
    captured = capture.get("slide_id") if isinstance(capture, dict) else None
    if captured != slide_id:
        raise ValueError(f"the task pane captured slide {captured!r} instead of {slide_id!r}")
    geometry = capture.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError("the task pane sent no geometry for the captured slide")
    return Capture([images[0]["data_url"]], geometry_summary(geometry), {"slide_id": slide_id})


def _page_capture(arguments: dict[str, Any], result: ClientToolResult) -> Capture:
    """The document's first rendered pages, at most ``_REVIEW_PAGE_LIMIT``, for ``verify_doc_visual``.

    The reviewer reads with them how many pages the document has and the author's focus area, wrapped as data.
    """
    images = default_client_output(result).data.get("multimodal") or []
    if not images:
        raise ValueError("the task pane returned no rendered page")
    sent = images[:_REVIEW_PAGE_LIMIT]
    page_hint = arguments.get("page_hint")
    focus = f"<focus_area>{page_hint}</focus_area>" if page_hint else "No focus area was named."
    evidence = f"The document has {len(images)} pages; the first {len(sent)} are attached.\n{focus}"
    return Capture([image["data_url"] for image in sent], evidence,
                   {"pages_reviewed": f"{len(sent)} of {len(images)}"})


# The contract tools whose result an independent reviewer judges, by name: how each reads the task pane's capture.
_VISUAL_REVIEWS: dict[str, Callable[[dict[str, Any], ClientToolResult], Capture]] = {
    "verify_slide_visual": _slide_capture,
    "verify_doc_visual": _page_capture,
}


def build_task_pane_tools(
    package_dir: Path, native_image_model: Callable[[], Model | None], store: OfficeAssetStore,
) -> list[TaskPaneTool]:
    """One tool per contract tool of the app's package, for one agent."""
    tools: list[TaskPaneTool] = []
    for tool in load_contract(package_dir):
        capture = _VISUAL_REVIEWS.get(tool.name)
        if capture is None:
            tools.append(TaskPaneTool(tool.card(), native_image_model=native_image_model, store=store))
        else:
            tools.append(VisualReviewTool(tool.card(), checklist=review_checklist(package_dir), capture=capture,
                                          native_image_model=native_image_model, store=store))
    return tools


def _stored_image(session_id: str, data_url: str) -> dict[str, Any]:
    header, _, base64_data = data_url.partition(",")
    mime_type = header.removeprefix("data:").removesuffix(";base64")
    return image_attachment(session_id, mime_type, base64_data)
