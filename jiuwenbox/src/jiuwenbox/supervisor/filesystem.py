# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared backing creation and permissions; storage lifecycle belongs to callers."""

import base64
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from jiuwenbox.models.policy import DirectoryMount, FileMount, SecurityPolicy

logger = logging.getLogger(__name__)


def materialize_directories(
    specs: Sequence[str | DirectoryMount],
    root: Path,
    policy: SecurityPolicy,
    uid: int,
    gid: int,
) -> list[dict[str, str]]:
    """Create directory backing and apply the shared ownership and permission rules."""
    return _materialize(specs, root, policy, uid, gid, is_file=False)


def materialize_files(
    specs: Sequence[str | FileMount],
    root: Path,
    policy: SecurityPolicy,
    uid: int,
    gid: int,
) -> list[dict[str, str]]:
    """Create file backing without truncating content, then apply permission rules."""
    return _materialize(specs, root, policy, uid, gid, is_file=True)


def _directory_spec(directory: object) -> tuple[str, str | None]:
    if isinstance(directory, str):
        return directory, None
    return getattr(directory, "path"), getattr(directory, "permissions", None)


def _file_spec(file: object) -> tuple[str, str | None]:
    if isinstance(file, str):
        return file, None
    return getattr(file, "path"), getattr(file, "permissions", None)


def _host_entry_name(sandbox_path: str) -> str:
    encoded = base64.urlsafe_b64encode(sandbox_path.encode()).decode()
    return encoded.rstrip("=")


def _apply_path_ownership(path: Path, uid: int, gid: int) -> bool:
    try:
        os.chown(path, uid, gid)
        return True
    except PermissionError:
        logger.warning(
            "Failed to chown policy path %s to %d:%d; keeping current owner",
            path,
            uid,
            gid,
        )
        return False


def _apply_path_permissions(path: Path, permissions: str | None) -> None:
    if permissions is None:
        return
    os.chmod(path, int(permissions, 8))


def _needs_userns_write_fallback(
    policy: SecurityPolicy,
    uid: int,
    permissions: str | None,
) -> bool:
    if os.geteuid() != 0 or not policy.namespace.user or uid == 0:
        return False
    if permissions is None:
        return True
    mode = int(permissions, 8)
    return (
        bool(mode & 0o200)
        and (mode & 0o005) == 0o005
        and not bool(mode & 0o002)
    )


def _needs_userns_owner_access_fallback(
    policy: SecurityPolicy,
    uid: int,
) -> bool:
    return os.geteuid() == 0 and policy.namespace.user and uid != 0


def _apply_userns_write_fallback(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    fallback_mode = mode | 0o003
    if fallback_mode == mode:
        return
    logger.warning(
        "Relaxing policy path %s permissions from %s to %s because "
        "root-run user namespaces cannot map the sandbox uid onto the "
        "bind-mounted backing path owner",
        path,
        oct(mode),
        oct(fallback_mode),
    )
    os.chmod(path, fallback_mode)


def _apply_userns_file_access_fallback(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    owner_bits = (mode & 0o700) >> 6
    fallback_mode = mode | owner_bits
    if fallback_mode == mode:
        return
    logger.warning(
        "Relaxing policy file %s permissions from %s to %s because "
        "root-run user namespaces cannot preserve owner-only file access",
        path,
        oct(mode),
        oct(fallback_mode),
    )
    os.chmod(path, fallback_mode)


def _ensure_writable_when_chown_unavailable(path: Path, owner_applied: bool) -> None:
    if owner_applied:
        return

    if path.stat().st_uid == os.getuid():
        return

    mode = path.stat().st_mode & 0o777
    if mode & 0o005 and not mode & 0o002:
        fallback_mode = mode | 0o002
        logger.warning(
            "Relaxing policy path %s permissions from %s to %s because chown failed",
            path,
            oct(mode),
            oct(fallback_mode),
        )
        os.chmod(path, fallback_mode)


def _materialize(
    specs: Sequence[str | DirectoryMount | FileMount],
    root: Path,
    policy: SecurityPolicy,
    uid: int,
    gid: int,
    *,
    is_file: bool,
) -> list[dict[str, str]]:
    root.mkdir(parents=True, exist_ok=True)
    binds = []
    for entry in specs:
        sandbox_path, permissions = _file_spec(entry) if is_file else _directory_spec(entry)
        host_path = root / _host_entry_name(sandbox_path)
        if is_file:
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.touch(exist_ok=True)
        else:
            host_path.mkdir(parents=True, exist_ok=True)
        owner_applied = _apply_path_ownership(host_path, uid, gid)
        _apply_path_permissions(host_path, permissions)
        if is_file and _needs_userns_owner_access_fallback(policy, uid):
            _apply_userns_file_access_fallback(host_path)
        if _needs_userns_write_fallback(policy, uid, permissions):
            _apply_userns_write_fallback(host_path)
        _ensure_writable_when_chown_unavailable(host_path, owner_applied)
        binds.append({"host_path": str(host_path), "sandbox_path": sandbox_path})
    return binds
