# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``POST /file-api/external-cli/custom-agents``: upload a custom agent package.

The multipart form carries ``wheel`` (the openjiuwen harness provider wheel,
required) and ``cli`` (the vendor CLI executable, optional). Both are written
to a temporary directory and handed to
:func:`jiuwenswarm.common.custom_agent_runtime.install_custom_agent`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

from jiuwenswarm.common.custom_agent_runtime import CustomAgentError, install_custom_agent
from jiuwenswarm.server.runtime.skill.skill_manager import SkillRpcError
from jiuwenswarm.server.runtime.skill.skills_multipart_http import parse_multipart_form

logger = logging.getLogger(__name__)

# A provider wheel bundling its CLI and a separately uploaded CLI are each
# tens to a couple of hundred megabytes.
MAX_CUSTOM_AGENT_UPLOAD_BYTES = 512 * 1024 * 1024


def handle_custom_agent_upload_http(*, content_type: str, body: bytes) -> tuple[int, dict[str, Any]]:
    """Install the uploaded custom agent and describe it.

    Returns:
        ``(status, payload)``: 200 with ``{"agent": {...}}``, or 400 with
        ``{"error": message}`` when the upload or the package is invalid.
    """
    try:
        fields = parse_multipart_form(content_type, body)
    except SkillRpcError as exc:
        return 400, {"error": exc.message}
    try:
        wheel = _uploaded_file(fields, "wheel", required=True)
        cli = _uploaded_file(fields, "cli", required=False)
    except CustomAgentError as exc:
        return 400, {"error": str(exc)}
    if not wheel["filename"].endswith(".whl"):
        return 400, {"error": "wheel must be a .whl file"}

    upload_dir = Path(tempfile.mkdtemp(prefix="jiuwenswarm_custom_agent_"))
    try:
        wheel_path = upload_dir / wheel["filename"]
        wheel_path.write_bytes(wheel["content"])
        cli_path = None
        if cli is not None:
            cli_path = upload_dir / "cli" / cli["filename"]
            cli_path.parent.mkdir()
            cli_path.write_bytes(cli["content"])
        agent = install_custom_agent(wheel_path, cli_executable=cli_path)
    except CustomAgentError as exc:
        return 400, {"error": str(exc)}
    except OSError as exc:
        logger.exception("[custom-agent] upload failed: %s", exc)
        return 500, {"error": "failed to install the custom agent"}
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
    return 200, {
        "agent": {
            "name": agent.name,
            "version": agent.version,
            "summary": agent.summary,
            "has_cli": bool(agent.cli_path),
        },
    }


def _uploaded_file(fields: dict[str, Any], name: str, *, required: bool) -> dict[str, Any] | None:
    field = fields.get(name)
    if field is None and not required:
        return None
    if not isinstance(field, dict) or not isinstance(field.get("content"), (bytes, bytearray)):
        raise CustomAgentError(f"missing {name} file")
    filename = field.get("filename")
    unsafe = not isinstance(filename, str) or filename in {"", ".", ".."}
    if unsafe or any(char in filename for char in '/\\:\x00') or PureWindowsPath(filename).is_reserved():
        raise CustomAgentError(f"invalid {name} file name")
    if not field["content"]:
        raise CustomAgentError(f"{name} file is empty")
    return {"filename": filename, "content": bytes(field["content"])}


__all__ = ["MAX_CUSTOM_AGENT_UPLOAD_BYTES", "handle_custom_agent_upload_http"]
