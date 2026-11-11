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
    review_slide,
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
                f"this add-in does not offer {self.card.name} yet, in any PowerPoint version; do the work with the "
                "tools you have, or tell the user this add-in cannot do it yet")
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


class VerifySlideVisualTool(TaskPaneTool):
    """``verify_slide_visual``: the task pane captures one slide, a model that saw nothing else reviews it.

    One pause: the task pane answers with the slide's render and geometry, and
    ``complete`` runs the review against the checklist of the app's package
    (``review.md``), by this conversation's model when it takes images, else by
    ``models.vision``. The author gets the verdict with the same render, which
    reaches it as any other contract tool's image does.
    """

    def __init__(self, card: ToolCard, *, checklist: str, native_image_model: Callable[[], Model | None],
                 store: OfficeAssetStore) -> None:
        super().__init__(card, native_image_model=native_image_model, store=store)
        self._checklist = checklist

    def build_request(self, arguments: dict[str, Any], context: ClientToolContext) -> dict[str, Any]:
        request = super().build_request(arguments, context)
        if self._native_image_model() is None and vision_client_config() is None:
            raise ClientToolUnavailable(
                "verify_slide_visual needs a model that can see images: this conversation's model cannot, "
                "and the vision model (models.vision) is not configured and enabled")
        return request

    def complete(self, arguments: dict[str, Any], result: ClientToolResult,
                 context: ClientToolContext) -> ToolOutput | Awaitable[ToolOutput]:
        if result.failed:
            return super().complete(arguments, result, context)
        return self._reviewed(arguments, result, context)

    async def _reviewed(self, arguments: dict[str, Any], result: ClientToolResult,
                        context: ClientToolContext) -> ToolOutput:
        slide_id = arguments["slide_id"]
        try:
            render, geometry = _slide_capture(result, slide_id)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"verify_slide_visual: {exc}")
        try:
            review = await review_slide(self._checklist, self._native_image_model(), render,
                                        geometry_summary(geometry), arguments.get("expected_changes"))
        except ReviewFailed as exc:
            text = verifier_error(slide_id, str(exc))
        else:
            text = verifier_result(slide_id, review)
        reviewed = ClientToolResult(content=[{"type": "text", "text": text}, {"type": "image", "data_url": render}])
        return super().complete(arguments, reviewed, context)


def build_task_pane_tools(
    package_dir: Path, native_image_model: Callable[[], Model | None], store: OfficeAssetStore,
) -> list[TaskPaneTool]:
    """One tool per contract tool of the app's package, for one agent."""
    tools: list[TaskPaneTool] = []
    for tool in load_contract(package_dir):
        if tool.name == "verify_slide_visual":
            tools.append(VerifySlideVisualTool(tool.card(), checklist=review_checklist(package_dir),
                                               native_image_model=native_image_model, store=store))
        else:
            tools.append(TaskPaneTool(tool.card(), native_image_model=native_image_model, store=store))
    return tools


def _slide_capture(result: ClientToolResult, slide_id: str) -> tuple[str, dict[str, Any]]:
    """The render (an image data URL) and the geometry of the slide the task pane captured for review."""
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
    return images[0]["data_url"], geometry


def _stored_image(session_id: str, data_url: str) -> dict[str, Any]:
    header, _, base64_data = data_url.partition(",")
    mime_type = header.removeprefix("data:").removesuffix(";base64")
    return image_attachment(session_id, mime_type, base64_data)
