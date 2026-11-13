# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Startup package state reset, independent of the optional evolving SDK."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def reset_harness_packages_state(path: Path | None = None) -> None:
    from jiuwenswarm.common.utils import get_user_workspace_dir
    path = path or get_user_workspace_dir() / "auto-harness" / "harness-packages.json"
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("active_package_ids"):
            return
        data["active_package_ids"] = []
        data.pop("active_package_id", None)
        data.pop("active_extension_name", None)
        if data.get("native_version"):
            data["native_version"]["is_active"] = True
        for package in data.get("packages", []):
            if package.get("is_active", False):
                package["is_active"] = False
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("Auto-Harness package state reset failed", exc_info=True)
