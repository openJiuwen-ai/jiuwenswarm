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
    check_claims,
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
    """What the task pane captured for a review: renders, the evidence read with them, what they show."""

    renders: list[str]
    evidence: str
    # Names what was reviewed, as attributes of the verifier result (``slide_id``).
    target: dict[str, str]


@dataclass(frozen=True)
class ReviewRequest:
    """One review: the checklist, the reviewer, the capture and the author's call."""

    checklist: str
    model: Model | None
    capture: Capture
    arguments: dict[str, Any]
    package_dir: Path


@dataclass(frozen=True)
class Review:
    """How a contract tool's result is judged by a reviewer that saw nothing of the conversation.

    ``capture`` reads the task pane's result, ``judge`` turns it into what the
    author reads, against the ``checklist`` file of the app's package. A review
    of ``images`` needs a model that takes them: the conversation's own when it
    does, else ``models.vision``; any other is made by the conversation's model.
    """

    capture: Callable[[dict[str, Any], ClientToolResult], Capture]
    judge: Callable[[ReviewRequest], Awaitable[str]]
    checklist: str = "review.md"
    images: bool = True


class ReviewTool(TaskPaneTool):
    """A check whose result an independent reviewer judges.

    One pause: the task pane answers with the capture, which the tool's entry
    in ``_REVIEWS`` reads, and ``complete`` has the reviewer judge it. The
    author gets the judgment with the capture's renders, which reach it as any
    other contract tool's images do.
    """

    def __init__(self, card: ToolCard, *, review: Review, package_dir: Path, session_model: Callable[[], Model],
                 native_image_model: Callable[[], Model | None], store: OfficeAssetStore) -> None:
        super().__init__(card, native_image_model=native_image_model, store=store)
        self._review = review
        self._package_dir = package_dir
        self._session_model = session_model

    def build_request(self, arguments: dict[str, Any], context: ClientToolContext) -> dict[str, Any]:
        request = super().build_request(arguments, context)
        if self._review.images and self._native_image_model() is None and vision_client_config() is None:
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
            capture = self._review.capture(arguments, result)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"{self.card.name}: {exc}")
        model = self._native_image_model() if self._review.images else self._session_model()
        text = await self._review.judge(ReviewRequest(review_checklist(self._package_dir, self._review.checklist),
                                                      model, capture, arguments, self._package_dir))
        images = ({"type": "image", "data_url": render} for render in capture.renders)
        reviewed = ClientToolResult(content=[{"type": "text", "text": text}, *images])
        return super().complete(arguments, reviewed, context)


async def _visual_verdict(request: ReviewRequest) -> str:
    """The verdict on renders, sorted into what to repair and what not; or why there is none."""
    capture = request.capture
    try:
        review = await review_renders(request.checklist, request.model, capture.renders, capture.evidence,
                                      request.arguments.get("expected_changes"))
    except ReviewFailed as exc:
        return verifier_error(str(exc), **capture.target)
    return verifier_result(review, **capture.target)


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

    The task pane renders no more than that, and says how many pages the document has. The reviewer reads that count
    and the author's focus area, wrapped as data, with the pages.
    """
    output = default_client_output(result)
    images = output.data.get("multimodal") or []
    if not images:
        raise ValueError("the task pane returned no rendered page")
    try:
        page_count = json.loads(output.data["content"])["result"]["page_count"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("the task pane did not say how many pages the document has") from exc
    sent = images[:_REVIEW_PAGE_LIMIT]
    page_hint = arguments.get("page_hint")
    focus = f"<focus_area>{page_hint}</focus_area>" if page_hint else "No focus area was named."
    evidence = f"The document has {page_count} pages; the first {len(sent)} are attached.\n{focus}"
    return Capture([image["data_url"] for image in sent], evidence,
                   {"pages_reviewed": f"{len(sent)} of {page_count}"})


def _document_capture(arguments: dict[str, Any], result: ClientToolResult) -> Capture:
    """The document's final text and pending tracked changes, for ``explain_edits``, as the reviewer reads them."""
    try:
        captured = json.loads(default_client_output(result).data["content"])["result"]
        paragraphs, changes = captured["paragraphs"], captured["tracked_changes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("the task pane did not send the document's text") from exc
    # The document's own text cannot close a block: each "<" in it is written "&lt;".
    evidence = "<document_text>\n" + "\n".join(paragraphs).replace("<", "&lt;") + "\n</document_text>"
    if changes:
        revisions = "\n".join(f"[revision {index}] {change['author']!r} {change['type']}: {change['text'][:200]!r}"
                               .replace("<", "&lt;") for index, change in enumerate(changes))
        evidence += f"\n\n<tracked_changes_at_turn_start>\n{revisions}\n</tracked_changes_at_turn_start>"
    return Capture([], evidence, {})


async def _claim_verdicts(request: ReviewRequest) -> str:
    """Each planned edit with a verdict per claim; a claim on a skill reads that skill of the app's package."""
    edits = request.arguments["edits"]
    claims = [claim for edit in edits for claim in edit["grounding"]]
    named = {claim.get("skill_name") for claim in claims if claim["basis"] == "skill_instruction"}
    # Only a skill the package lists is read: a name is the model's, and could be a path out of the package.
    listed = {path.parent.name: path for path in (request.package_dir / "skills").glob("*/SKILL.md")}
    skills = {name: listed[name].read_text("utf-8") for name in sorted(named & listed.keys())}
    return await check_claims(request.checklist, request.model, request.capture.evidence, edits, skills)


# The contract tools whose result an independent reviewer judges, by name.
_REVIEWS: dict[str, Review] = {
    "verify_slide_visual": Review(_slide_capture, _visual_verdict),
    "verify_doc_visual": Review(_page_capture, _visual_verdict),
    "explain_edits": Review(_document_capture, _claim_verdicts, checklist="claim_check.md", images=False),
}


def build_task_pane_tools(
    package_dir: Path, session_model: Callable[[], Model], native_image_model: Callable[[], Model | None],
    store: OfficeAssetStore,
) -> list[TaskPaneTool]:
    """One tool per contract tool of the app's package, for one agent."""
    tools: list[TaskPaneTool] = []
    for tool in load_contract(package_dir):
        review = _REVIEWS.get(tool.name)
        if review is None:
            tools.append(TaskPaneTool(tool.card(), native_image_model=native_image_model, store=store))
        else:
            tools.append(ReviewTool(tool.card(), review=review, package_dir=package_dir, session_model=session_model,
                                    native_image_model=native_image_model, store=store))
    return tools


def _stored_image(session_id: str, data_url: str) -> dict[str, Any]:
    header, _, base64_data = data_url.partition(",")
    mime_type = header.removeprefix("data:").removesuffix(";base64")
    return image_attachment(session_id, mime_type, base64_data)
