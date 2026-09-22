"""Service-level hosting storage paths (not agent workspace)."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.utils import get_service_root_dir, get_user_workspace_dir
from jiuwenswarm.edition import is_enterprise


def hosting_root_dir(*, workspace_key: str | None = None) -> Path:
    """Personal: ``~/.jiuwenswarm/service_{sid}/im_hosting``.

    Enterprise: ``~/.jiuwenswarm/workspace_{key}/im_hosting``.
    """
    if is_enterprise():
        from jiuwenswarm.common.utils import _effective_workspace_key

        wk = _effective_workspace_key(workspace_key)
        return get_user_workspace_dir() / f"workspace_{wk}" / "im_hosting"
    return get_service_root_dir() / "im_hosting"


def hosting_db_path(*, workspace_key: str | None = None) -> Path:
    return hosting_root_dir(workspace_key=workspace_key) / "hosting.db"
