# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic service-lifetime permissions for explicitly configured shared paths."""

import hashlib
import json
import logging
import os
import sys
import threading
import time
from functools import partial
from pathlib import Path

logger = logging.getLogger(__name__)
_SHARED_ACL_LOCK = threading.Lock()


def _absolute_paths(raw, name: str) -> list[str]:
    if not isinstance(raw, list) or any(
        not isinstance(p, str) or not p.strip() or any(c in p for c in "\x00\r\n*?") for p in raw
    ):
        raise ValueError(f"{name} must be a JSON list of absolute directory paths")
    paths = []
    for value in raw:
        path = Path(os.path.expandvars(os.path.expanduser(value.strip())))
        if path.is_absolute():
            path = path.resolve()
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError(f"{name} requires absolute non-root directory paths")
        normalized = str(path)
        if os.path.normcase(normalized) not in {os.path.normcase(p) for p in paths}:
            paths.append(normalized)
    return paths


def configured_shared_paths() -> list[str]:
    """Exact host-configured paths; command request env never grants access."""
    raw = (os.environ.get("JIUWENBOX_PATH_ALLOWLIST") or "").strip() or "[]"
    return _absolute_paths(json.loads(raw), "JIUWENBOX_PATH_ALLOWLIST")


def configured_tool_paths() -> tuple[list[str], list[str]]:
    """Trusted service-level RO roots and PATH prefixes, independent of products.

    Command env is deliberately not an input. Executable directories must be
    covered by a declared tool root, so declaring PATH cannot grant more ACLs.
    """
    raw = json.loads((os.environ.get("JIUWENBOX_TOOL_PATHS") or "").strip() or "{}")
    if not isinstance(raw, dict) or raw.keys() - {"read_roots", "executable_dirs"}:
        raise ValueError("JIUWENBOX_TOOL_PATHS requires read_roots and executable_dirs lists")
    roots = _absolute_paths(raw.get("read_roots", []), "JIUWENBOX_TOOL_PATHS.read_roots")
    directories = _absolute_paths(raw.get("executable_dirs", []), "JIUWENBOX_TOOL_PATHS.executable_dirs")
    if any(not any(Path(directory).is_relative_to(root) for root in roots) for directory in directories):
        raise ValueError("JIUWENBOX_TOOL_PATHS executable_dirs must be covered by read_roots")
    return roots, directories


def configured_service_read_roots() -> list[str]:
    """Trusted service code/resources; never taken from a command request."""
    raw = json.loads((os.environ.get("JIUWENBOX_SERVICE_READ_ROOTS") or "").strip() or "[]")
    return _absolute_paths(raw, "JIUWENBOX_SERVICE_READ_ROOTS")


def shared_tool_paths(read_roots: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Explicit shared roots plus optional generic runtime Python paths."""
    roots = [*configured_tool_paths()[0], *configured_service_read_roots(), *(read_roots or [])]
    writable = configured_shared_paths()
    roots.extend(writable)
    for name in ("JIUWENBOX_BUNDLED_PYTHON", "JIUWENBOX_VENV_DIR"):
        raw = (os.environ.get(name) or "").strip()
        path = Path(raw)
        if raw and path.is_absolute() and path.is_dir():
            root = str(path.resolve())
            roots.append(root)
            if name == "JIUWENBOX_VENV_DIR":
                writable.append(root)
    roots = list({os.path.normcase(root): root for root in roots}.values())
    roots = [root for root in roots if not any(
        root != parent and Path(root).is_relative_to(parent) for parent in writable
    )]
    return roots, list({os.path.normcase(root): root for root in writable}.values())


def _shared_acl_cache_path() -> Path:
    from jiuwenbox.server.workspace import JIUWENBOX_HOME

    return JIUWENBOX_HOME / "shared-tools-acl.json"


def _shared_acl_fingerprint(root: str, sid: str, synthetic_sid: str, policy: dict, watch_roots: list[str]) -> str:
    from jiuwenbox.supervisor import win_acl

    path = Path(root)
    # Fixed-size checks only: replacing a runtime/venv, installing dependencies
    # or changing root ACLs invalidates the stamp without scanning the tree.
    markers = []
    for watched in [path, *(Path(p) for p in watch_roots if Path(p).is_relative_to(path))]:
        markers.extend([watched, watched / "python.exe", watched / "node.exe",
                        watched / "pyvenv.cfg", watched / "Scripts/python.exe",
                        watched / "Lib/site-packages", watched / "package-lock.json"])
    state = []
    for marker in markers:
        try:
            stat = marker.stat()
            state.append((str(marker), stat.st_ino, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            state.append((str(marker), None))
        except OSError as exc:
            win_acl.log_acl_failure(root, str(marker), "stat_runtime_metadata", exc, "not_attempted", required=True)
            raise
    security, _, _ = win_acl._ensure_pywin32()
    operation = "read_root_dacl"
    try:
        sd = security.GetFileSecurity(root, security.DACL_SECURITY_INFORMATION)
        operation = "serialize_root_dacl"
        acl = security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            sd, 1, security.DACL_SECURITY_INFORMATION,
        )
    except Exception as exc:
        win_acl.log_acl_failure(root, root, operation, exc, "not_attempted", required=True)
        raise
    value = {"version": 2, "sid": sid, "synthetic_sid": synthetic_sid,
             "policy": policy, "state": state, "acl": acl}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def prepare_shared_tool_acl(policy, *, read_roots: list[str] | None = None) -> None:
    """Prepare persistent configured permissions before serving sessions.

    Stamps are written only after a complete successful walk. Never add these
    roots to a sandbox's revoke list. Policy/account/runtime changes invalidate
    the stamp; ordinary service restarts use fixed-size checks only.
    """
    if sys.platform != "win32":
        return
    from jiuwenbox.supervisor import win_acl, win_constants as const, win_setup

    read_roots = [*configured_tool_paths()[0], *configured_service_read_roots(), *(read_roots or [])]
    roots, writable = shared_tool_paths(read_roots)
    # A parent's repair also visits protected descendants. Apply children last
    # so a writable child cannot be changed back to RX by a later parent walk.
    roots = sorted(roots, key=lambda root: len(Path(root).parts))
    cache_file = _shared_acl_cache_path()
    if not roots and not cache_file.exists():
        return
    for root in roots:
        if win_acl._is_acl_forbidden_path(root):
            raise ValueError(f"Refusing shared ACL on protected path: {root}")
        Path(root).mkdir(parents=True, exist_ok=True)
    sid = win_setup.get_sandbox_user_sid()
    if not sid:
        raise RuntimeError("Shared tool ACL requires an installed sandbox account")
    synthetic = win_acl.get_synthetic_write_sid()
    fs = policy.windows.filesystem
    rules = {"allow_read": list(fs.allow_read),
             "allow_write": list(fs.allow_write),
             "deny_read": list(fs.deny_read),
             "deny_write": [*fs.deny_write, *(policy.filesystem_policy.read_only or [])]}
    # Shared roots are default grants, not explicit policy exceptions. Carry
    # effective ancestor denials down to each root before adding those defaults;
    # otherwise the longer automatic allow prefix would override the denial.
    inherited_denials = [(root, win_acl._policy_deny_rights(Path(root), rules)) for root in roots]
    for axis, mask in (("read", const.DENY_READ_RIGHTS), ("write", const.DENY_WRITE_RIGHTS)):
        rules["deny_" + axis].extend(root for root, denied in inherited_denials if denied & mask)
    rules["allow_read"].extend(roots)
    rules["allow_write"].extend(writable)
    with _SHARED_ACL_LOCK:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or any(
                not Path(root).is_absolute() or not isinstance(stamp, str) or len(stamp) != 64
                for root, stamp in cached.items()
            ):
                cached = {}
        except (OSError, ValueError):
            cached = {}
        # Configuration removal is a permission change too. Preserve any
        # current nested roots while retiring the old shared grant.
        current = {os.path.normcase(root) for root in roots}
        retired = [root for root in cached if root not in current]
        if retired:
            win_acl.revoke_sandbox_acl(retired, sandbox_user_sid=sid, preserve_roots=roots, strict=True)
        # Stale session cleanup may have removed ancestor traverse ACEs. This
        # fixed-depth repair must also run on a cache hit, without tree walks.
        for root in roots:
            for trustee in (sid, synthetic):
                win_acl.grant_parent_traverse(root, trustee, filesystem_policy=rules)
        updated = {}
        for root in roots:
            started = time.perf_counter()
            key = os.path.normcase(root)
            fingerprint = _shared_acl_fingerprint(root, sid, synthetic, rules, read_roots or [])
            if cached.get(key) == fingerprint:
                logger.info("Shared tool ACL unchanged, skip tree: %s cache=hit elapsed=%.3fs",
                            root, time.perf_counter() - started)
                updated[key] = fingerprint
                continue
            # Migration shares the cold ACL walk and only runs on a denied object.
            readonly = not any(Path(root).is_relative_to(path) for path in writable)
            migration = None
            if readonly:
                migration = partial(win_acl.migrate_sandbox_bytecode, root=root, sandbox_sid=sid)
            win_acl.apply_tool_acl([root], sid, rules, writable_roots=writable, strict=True,
                                   on_access_denied=migration)
            # Parent workspace/data-tree operations must not propagate into
            # this service-owned subtree or invalidate the persistent grant.
            security, _, _ = win_acl._ensure_pywin32()
            operation = "read_root_dacl"
            try:
                sd = security.GetFileSecurity(root, security.DACL_SECURITY_INFORMATION)
                operation = "protect_root_dacl"
                win_acl._write_file_dacl(root, sd.GetSecurityDescriptorDacl(), protect=True, sd=sd)
            except Exception as exc:
                win_acl.log_acl_failure(root, root, operation, exc, "not_attempted", required=True)
                raise
            updated[key] = _shared_acl_fingerprint(root, sid, synthetic, rules, read_roots or [])
            logger.info("Shared tool ACL prepared: %s cache=miss elapsed=%.3fs",
                        root, time.perf_counter() - started)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(updated, sort_keys=True), encoding="utf-8")
        temporary.replace(cache_file)


