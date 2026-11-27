# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persist gate upload OBS URLs in the task workspace for post-round cleanup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Must match scripts.upload_skill.GATE_OBS_STATE_FILE
_GATE_OBS_STATE_FILE = ".gate_obs_upload.json"
_GATE_OBS_MIME_TYPE = "application/zip"


def pop_gate_obs_upload(task_workspace: str | Path) -> dict[str, str] | None:
    """Read and remove the gate upload record; return None if missing or invalid."""
    path = Path(task_workspace) / _GATE_OBS_STATE_FILE
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[gate_obs_upload] failed to read %s: %s", path, exc)
        data = None
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("[gate_obs_upload] failed to delete %s: %s", path, exc)

    if not isinstance(data, dict):
        return None

    url = str(data.get("url") or "").strip()
    if not url:
        return None

    return {
        "url": url,
        "filename": str(data.get("filename") or "").strip(),
        "mimeType": str(data.get("mimeType") or _GATE_OBS_MIME_TYPE).strip() or _GATE_OBS_MIME_TYPE,
    }
