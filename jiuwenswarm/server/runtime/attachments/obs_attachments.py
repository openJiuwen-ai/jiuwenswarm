# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Import OBS bytes into the workspace selected by the existing tenant scope."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from jiuwenswarm.common.obs_files import AttachmentStore, object_prefix
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.attachments.document_attachments import (
    is_forbidden_document,
)
from jiuwenswarm.server.runtime.attachments.media_attachments import (
    supported_image_suffixes,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    atomic_write_unique,
    safe_session_dirname,
    safe_upload_filename,
)
from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


def _open_uploads(root: Path, uploads: Path) -> int:
    """Walk directory descriptors so a concurrent symlink swap cannot escape."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags, 0o700)
    try:
        for name in uploads.relative_to(root).parts:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            parent_fd = fd
            fd = os.open(name, flags, 0o700, dir_fd=parent_fd)
            os.close(parent_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def import_obs_files(request) -> list[dict]:
    files = request.params.get("files")
    if not isinstance(files, list) or not 0 < len(files) <= 20:
        raise ValueError("files must contain 1..20 items")
    session_id = request.session_id
    if not session_id or safe_session_dirname(session_id) != session_id:
        raise ValueError("invalid session_id")
    names: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("attachment URL required")
        name = safe_upload_filename(str(item.get("name") or ""), fallback="upload")
        if is_forbidden_document(filename=name):
            raise ValueError("forbidden attachment type")
        if (
            item.get("type") == "image"
            and Path(name).suffix.lower() not in supported_image_suffixes()
        ):
            raise ValueError("unsupported image type")
        names.append(name)
    store = AttachmentStore.from_env()
    prefix = object_prefix(*TenantAgentPool.extract_triple(request))
    uploads = get_agent_sessions_dir() / session_id / "uploads"
    tenant_root = TenantContext.current_tenant_root()
    if tenant_root is None or not uploads.resolve().is_relative_to(
        tenant_root.resolve()
    ):
        raise ValueError("uploads must remain inside the current tenant")
    directory_fd = _open_uploads(tenant_root, uploads)
    created: list[Path] = []
    results: list[dict] = []
    try:
        for item, name in zip(files, names):
            data = store.read(item["url"], prefix)
            if item.get("type") == "image" and len(data) > 10 * 1024 * 1024:
                raise ValueError("image exceeds 10 MiB")
            path = atomic_write_unique(uploads / name, data, directory_fd=directory_fd)
            created.append(path)
            results.append(
                {
                    "path": str(path),
                    "filename": path.name,
                    "name": name,
                    "size_bytes": len(data),
                    "size": len(data),
                    "mime_type": mimetypes.guess_type(name)[0]
                    or "application/octet-stream",
                }
            )
        return results
    except BaseException:
        for path in created:
            os.unlink(path.name, dir_fd=directory_fd)
        raise
    finally:
        os.close(directory_fd)
