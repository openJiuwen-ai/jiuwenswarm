# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""File-download token API.

HMAC tokens live in ``jiuwenswarm.common.file_download_token`` so Gateway can
mint them without importing this package. Path boundary checks stay here.
"""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.file_download_token import (
    PURPOSE_SKILL_CONTENT_IMAGE,
    WebFileDownloadManager,
    build_file_download_info,
    build_verified_asset_download_info,
    generate_file_download_token,
    generate_file_upload_token,
    generate_skill_content_image_token,
    validate_file_download_token,
)

__all__ = [
    "PURPOSE_SKILL_CONTENT_IMAGE",
    "WebFileDownloadManager",
    "build_file_download_info",
    "build_verified_asset_download_info",
    "generate_file_download_token",
    "generate_file_upload_token",
    "generate_skill_content_image_token",
    "is_path_within_user_dirs",
    "validate_file_download_token",
]


def _resolve_user_dirs() -> list[Path]:
    """返回用户态业务目录根（注入目录 workspace / sessions / agent workspace + 项目目录）。"""
    from jiuwenswarm.common.utils import (
        get_agent_sessions_dir,
        get_agent_workspace_dir,
        get_user_workspace_dir,
    )

    roots: list[Path] = []
    for factory in (get_user_workspace_dir, get_agent_sessions_dir, get_agent_workspace_dir):
        try:
            root = Path(factory()).resolve(strict=False)
        except Exception:  # noqa: BLE001
            continue
        roots.append(root)

    try:
        from jiuwenswarm.server.runtime.session import project_store

        for project in project_store.list_projects(include_hidden=True):
            project_dir = str(project.project_dir or "").strip()
            if not project_dir:
                continue
            try:
                roots.append(Path(project_dir).resolve(strict=False))
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return roots


def is_path_within_user_dirs(path_str: str) -> bool:
    """判断路径是否位于用户态业务目录内（Phase 2 下载/上传边界校验）。"""
    if not path_str or not str(path_str).strip():
        return False
    try:
        candidate = Path(str(path_str)).resolve(strict=False)
    except OSError:
        return False
    for root in _resolve_user_dirs():
        if candidate == root or root in candidate.parents:
            return True
    return False
