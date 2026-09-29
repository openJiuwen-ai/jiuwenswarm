# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-call extra.paths access range: parse, contain, Windows write caps."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_EXTRA_PATHS = 64


class AccessDeniedError(Exception):
    """Windows extra.paths missing/empty/out of range/sensitive/deny_write → HTTP 403."""


class AccessExtraValidationError(Exception):
    """Malformed extra JSON / non-string path / over limit → HTTP 422."""


class AccessAclError(Exception):
    """Write ACE / cap SID allocation failed → HTTP 500, fail closed."""


@dataclass(frozen=True)
class ResolvedAccessContext:
    extra_roots: tuple[str, ...]
    actual_paths: tuple[str, ...]
    write_roots: tuple[str, ...]
    write_cap_sids: tuple[str, ...]
    sandbox_id: str
    op: str
    windows: bool


def extra_enforced() -> bool:
    """Fail-closed extra.paths + cap SID isolation is Windows-only."""
    return sys.platform == "win32"


def parse_extra_paths(extra: object, *, required: bool) -> list[str]:
    """Normalize ``extra.paths`` from a model, dict, or JSON string.

    ``required``: Windows HTTP APIs demand a non-empty list. MCP may pass
    an explicit empty list for a readonly token.
    """
    if extra is None:
        if required and extra_enforced():
            raise AccessDeniedError("extra.paths is required on Windows")
        return []
    payload = extra
    if isinstance(extra, str):
        text = extra.strip()
        if not text:
            if required and extra_enforced():
                raise AccessDeniedError("extra.paths is required on Windows")
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccessExtraValidationError(f"extra is not valid JSON: {exc}") from exc
    paths_raw = None
    if hasattr(payload, "paths"):
        paths_raw = payload.paths
    elif isinstance(payload, dict):
        if "paths" not in payload:
            raise AccessExtraValidationError("extra must contain 'paths'")
        paths_raw = payload.get("paths")
    else:
        raise AccessExtraValidationError("extra must be an object with 'paths'")
    if not isinstance(paths_raw, list):
        raise AccessExtraValidationError("extra.paths must be a list of strings")
    if len(paths_raw) > MAX_EXTRA_PATHS:
        raise AccessExtraValidationError(
            f"extra.paths exceeds limit ({len(paths_raw)}>{MAX_EXTRA_PATHS})"
        )
    paths: list[str] = []
    for item in paths_raw:
        if not isinstance(item, str):
            raise AccessExtraValidationError("extra.paths entries must be strings")
        text = item.strip()
        if text:
            paths.append(text)
    if required and extra_enforced() and not paths:
        raise AccessDeniedError("extra.paths must be a non-empty list on Windows")
    return paths


def _is_posix_absolute_path(path: str) -> bool:
    """Unix ``/tmp`` / ``/home/...`` (no drive letter).

    On Windows ``os.path.abspath('/tmp')`` becomes ``C:\\tmp``. That is a
    drive-root leftover, not the caller's workspace, and must not become a
    write ACE root.
    """
    raw = (path or "").strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return False
    return True


def _is_unsafe_raw_path(path: str) -> bool:
    raw = path.strip()
    if not raw or "\x00" in raw:
        return True
    lowered = raw.replace("/", "\\")
    if lowered.startswith("\\\\") or raw.startswith("//"):
        return True
    if lowered.startswith("\\\\.\\") or lowered.startswith("\\\\?\\"):
        return True
    return False


def canonicalize_path(path: str, *, must_exist: bool = False) -> str:
    """Expand, reject UNC/device, resolve reparse points when possible."""
    if extra_enforced() and _is_posix_absolute_path(path):
        raise AccessDeniedError(
            f"POSIX path {path!r} is not a Windows path"
        )
    if _is_unsafe_raw_path(path):
        raise AccessDeniedError(f"unsafe path rejected: {path!r}")
    expanded = os.path.expandvars(os.path.expanduser(path.strip()))
    if ".." in PathParts(expanded):
        # normpath first so ``foo\\..\\bar`` is flattened before resolve
        expanded = os.path.normpath(expanded)
    try:
        resolved = os.path.realpath(expanded)
    except OSError:
        resolved = os.path.abspath(os.path.normpath(expanded))
    if must_exist and not os.path.exists(resolved):
        raise AccessDeniedError(f"path does not exist: {path!r}")
    # Re-check after resolve: junction escape onto UNC is still unsafe.
    if _is_unsafe_raw_path(resolved):
        raise AccessDeniedError(f"unsafe path rejected: {path!r}")
    return resolved


class PathParts:
    """Tiny helper so ``'..' in PathParts(p)`` means a ``..`` segment."""

    def __init__(self, path: str) -> None:
        self._parts = os.path.normpath(path).replace("\\", "/").split("/")

    def __contains__(self, item: object) -> bool:
        return item in self._parts


def normalize_compare_path(path: str) -> str:
    p = os.path.normpath(canonicalize_path(path) if path else path)
    if sys.platform == "win32":
        p = os.path.normcase(p)
    return p.rstrip("\\/") or p


def path_in_roots(actual: str, roots: list[str]) -> bool:
    """True when ``actual`` equals a root or is a child (not a prefix sibling)."""
    if not roots:
        return False
    actual_n = normalize_compare_path(actual)
    sep = os.sep
    for root in roots:
        try:
            root_n = normalize_compare_path(root)
        except AccessDeniedError:
            continue
        if actual_n == root_n:
            return True
        if actual_n.startswith(root_n + sep):
            return True
    return False


def _unique_canonical_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            key = normalize_compare_path(raw)
        except AccessDeniedError:
            key = os.path.normcase(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def resolve_access_context(
    *,
    sandbox_id: str,
    extra: object,
    actual_paths: list[str],
    op: str,
    required: bool,
    grant_write: bool,
    deny_write: list[str] | None = None,
    workspace_roots: list[str] | None = None,
) -> ResolvedAccessContext:
    """Parse extra, check containment, optionally allocate write caps.

    Linux: extra is recorded but not enforced. Windows: missing/empty extra
    is 403 when ``required`` unless ``workspace_roots`` is non-empty.
    ``workspace_roots`` are always in the write-cap SID set (restricted token)
    and count as in-range for containment.
    """
    windows = extra_enforced()
    workspace_canonical: list[str] = []
    for raw in workspace_roots or []:
        if not raw:
            continue
        if windows and _is_posix_absolute_path(raw):
            logger.warning("drop POSIX filesystem.workspace on Windows: %s", raw)
            continue
        try:
            workspace_canonical.append(canonicalize_path(raw))
        except AccessDeniedError:
            raise
        except OSError as exc:
            raise AccessDeniedError(
                f"cannot normalize workspace path {raw!r}: {exc}",
            ) from exc

    extra_required = bool(required and windows and not workspace_canonical)
    extra_roots = parse_extra_paths(extra, required=extra_required)
    canonical_roots: list[str] = []
    for raw in extra_roots:
        if windows and _is_posix_absolute_path(raw):
            # Model/tool prompts often list Linux ``/tmp``. Mapping that to
            # ``C:\tmp`` would make every later write reconcile a drive-root ACE.
            logger.warning("drop POSIX extra.paths on Windows: %s", raw)
            continue
        try:
            canonical_roots.append(canonicalize_path(raw))
        except AccessDeniedError:
            raise
        except OSError as exc:
            raise AccessDeniedError(f"cannot normalize extra path {raw!r}: {exc}") from exc

    canonical_actual: list[str] = []
    for raw in actual_paths:
        if not raw:
            continue
        canonical_actual.append(canonicalize_path(raw))

    range_roots = _unique_canonical_paths(canonical_roots + workspace_canonical)
    # required=True: empty extra is allowed when workspace is configured, but
    # actual paths must still land in extra ∪ workspace.
    if windows and canonical_actual and (canonical_roots or (required and workspace_canonical)):
        for actual in canonical_actual:
            if not path_in_roots(actual, range_roots):
                raise AccessDeniedError(
                    f"path {actual!r} is outside extra.paths (sandbox={sandbox_id})"
                )

    if canonical_roots:
        extra_for_ctx = range_roots
    elif required and workspace_canonical:
        extra_for_ctx = list(workspace_canonical)
    else:
        extra_for_ctx = []

    write_roots: list[str] = []
    write_cap_sids: list[str] = []
    if windows and grant_write:
        cap_roots = _unique_canonical_paths(canonical_roots + workspace_canonical)
        if not cap_roots:
            write_cap_sids = []
        else:
            from jiuwenbox.supervisor import win_acl

            try:
                write_roots = win_acl.assert_write_roots(
                    cap_roots, deny_write=deny_write or [],
                )
            except win_acl.WriteRootDenied as exc:
                raise AccessDeniedError(str(exc)) from exc
            try:
                write_cap_sids = win_acl.cap_sids_for_write_roots(write_roots)
            except Exception as exc:  # noqa: BLE001
                raise AccessAclError(f"failed to allocate write cap SIDs: {exc}") from exc
            # 写 ACE 不在这里施加. 调用方紧接着跑 reconcile_live_write_roots,
            # 由它对「存活写根并集」做一次带 single-flight 的 apply + 撤销陈旧根;
            # 在这里再 apply 一遍是重复劳动 (每根多一次根 DACL 读).

    logger.info(
        "access ctx sandbox=%s op=%s extra_roots=%d workspace=%d actual=%d "
        "write_roots=%d caps=%d",
        sandbox_id, op, len(canonical_roots), len(workspace_canonical),
        len(canonical_actual), len(write_roots), len(write_cap_sids),
    )
    return ResolvedAccessContext(
        extra_roots=tuple(extra_for_ctx),
        actual_paths=tuple(canonical_actual),
        write_roots=tuple(write_roots),
        write_cap_sids=tuple(write_cap_sids),
        sandbox_id=sandbox_id,
        op=op,
        windows=windows,
    )
