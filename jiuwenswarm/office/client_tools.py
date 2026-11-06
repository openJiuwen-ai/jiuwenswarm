# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The PowerPoint tools the task pane of the attached presentation runs.

Each tool of the contract is a ``ClientTool``: once every ``before_tool_call``
rail (permissions among them) lets a call through, the turn pauses and the
task pane gets the call's arguments, plus what ``build_request`` returns
(``downloads``: a link for each asset the arguments name). The task pane's
result resumes the turn and ``complete`` makes it the tool output.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from jiuwenswarm.office.contract import POWERPOINT_PACKAGE, load_contract
from jiuwenswarm.office.runtime import image_attachment

_VISION_TOOL_HINT = (
    "If a vision tool is configured, call visual_question_answering with visual_attachment.path to inspect the "
    "pixels. This is a file in this conversation's uploads. Do not use shell to search presentation files or "
    "another conversation's assets."
)


class PowerPointTool(ClientTool):
    """A contract tool, sent to the task pane of the presentation attached to the call's conversation."""

    def __init__(self, card: ToolCard, *, native_images: Callable[[], bool], store: OfficeAssetStore) -> None:
        super().__init__(card)
        self._native_images = native_images
        self._store = store

    def build_request(self, arguments: dict[str, Any], context: ClientToolContext) -> dict[str, Any]:
        """Refuse a call no task pane can run now; give the task pane the links to the assets it needs.

        A call while the presentation is closed fails at once instead of
        holding the turn until the attachment sweep answers it ``client_gone``.
        """
        from jiuwenswarm.server.client_attachments import get_attachment_directory, load_client_capabilities

        session_id = context.session_id
        capabilities = load_client_capabilities(session_id) if session_id else None
        if capabilities is None:
            raise ClientToolUnavailable("no PowerPoint presentation is attached to this conversation")
        if self.card.name not in (capabilities.get("supported_tools") or ()):
            raise ClientToolUnavailable(f"this presentation's PowerPoint cannot run {self.card.name}")
        if get_attachment_directory().connection_for_session(session_id) is None:
            raise ClientToolUnavailable(
                "this conversation's presentation is not open in PowerPoint right now; ask the user to open it "
                "and continue in the PowerPoint task pane", retryable=True)
        try:
            downloads = {handle: self._store.download_url(session_id, handle)
                         for handle in sorted(referenced_handles(arguments))}
        except (ValueError, OSError) as exc:
            raise ClientToolUnavailable(f"{exc}; publish the file with publish_powerpoint_asset first") from exc
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
        if self._native_images():
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


def build_powerpoint_tools(
    native_images: Callable[[], bool], store: OfficeAssetStore, package_dir: Path = POWERPOINT_PACKAGE,
) -> list[PowerPointTool]:
    """One tool per contract tool, for one agent."""
    return [PowerPointTool(tool.card(), native_images=native_images, store=store)
            for tool in load_contract(package_dir)]


def _stored_image(session_id: str, data_url: str) -> dict[str, Any]:
    header, _, base64_data = data_url.partition(",")
    mime_type = header.removeprefix("data:").removesuffix(";base64")
    return image_attachment(session_id, mime_type, base64_data)
