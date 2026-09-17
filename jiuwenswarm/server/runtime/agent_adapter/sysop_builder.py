# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Literal

from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SandboxGatewayConfig,
    SysOperationCard,
)
from openjiuwen.core.sys_operation.config import (
    ContainerScope,
    PreDeployLauncherConfig,
    SandboxIsolationConfig,
)

from jiuwenswarm.common.config import (
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
)
from jiuwenswarm.common.utils import (
    get_agent_root_dir,
    get_agent_workspace_dir,
    get_config_file,
)

logger = logging.getLogger(__name__)


PreserveFileSharingMode = Literal["mount"]
_PRESERVE_FILE_SHARING_MODE: PreserveFileSharingMode = "mount"


def _normalize_fs_entry(entry: Any) -> dict[str, str] | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        path = entry.strip()
        if not path:
            return None
        return {"path": path}
    if isinstance(entry, dict):
        path = str(entry.get("path") or "").strip()
        if not path:
            return None
        return {"path": path}
    return None


def _sandbox_files_entry_path(entry: Any) -> str | None:
    """Extract the path string from a sandbox.files allow/deny entry."""
    normalized = _normalize_fs_entry(entry)
    if normalized is None:
        return None
    return normalized["path"]


def _is_strict_path_prefix(parent: str, child: str) -> bool:
    """Return True when ``parent`` is a strict directory ancestor of ``child``."""
    parent_norm = parent.rstrip("/") or "/"
    child_norm = child.rstrip("/") or "/"
    if parent_norm == child_norm:
        return False
    if parent_norm == "/":
        return child_norm != "/"
    return child_norm.startswith(parent_norm + "/")


def validate_sandbox_files_runtime(files: dict[str, Any] | None) -> None:
    """Reject invalid ``sandbox.files`` shapes."""
    if not isinstance(files, dict):
        return
    for bucket in ("allow", "deny"):
        entries = files.get(bucket)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            _normalize_fs_entry(entry)


def find_nested_files_conflict(
    path: str,
    bucket: str,
    files: dict[str, Any],
) -> str | None:
    """Return an error message when ``path`` would create unsupported nesting.

    Supported: allow parent + deny child (parent rw, child ro).
    Rejected: allow child while deny ancestor exists, or deny parent while allow
    descendant exists (parent deny overrides child allow at mount time).
    """
    target = _resolve_display_path(path)
    if target is None:
        return None

    if bucket == "allow":
        for entry in files.get("deny") or []:
            deny_path = _sandbox_files_entry_path(entry)
            if deny_path is None:
                continue
            deny_resolved = _resolve_display_path(deny_path)
            if deny_resolved is not None and _is_strict_path_prefix(deny_resolved, target):
                return (
                    f"cannot allow {path!r}: ancestor {deny_path!r} is deny_write; "
                    f"remove `/sandbox files remove {deny_path}` first"
                )
    elif bucket == "deny":
        for entry in files.get("allow") or []:
            allow_path = _sandbox_files_entry_path(entry)
            if allow_path is None:
                continue
            allow_resolved = _resolve_display_path(allow_path)
            if allow_resolved is not None and _is_strict_path_prefix(target, allow_resolved):
                return (
                    f"cannot deny {path!r}: descendant {allow_path!r} is allow_write; "
                    f"remove `/sandbox files remove {allow_path}` first"
                )
    return None


def _resolve_workspace_dir() -> Path | None:
    """Resolve agent workspace directory for sandbox rw bind."""
    try:
        workspace = Path(get_agent_workspace_dir()).expanduser().resolve()
    except OSError as exc:
        logger.debug("[sysop_builder] workspace dir resolve failed: %s", exc)
        return None
    if workspace == Path(workspace.anchor):
        logger.warning(
            "[sysop_builder] refusing to mount filesystem root %s as workspace",
            workspace,
        )
        return None
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[sysop_builder] could not ensure workspace dir %s: %s",
            workspace,
            exc,
        )
        return None
    if not workspace.is_dir():
        logger.debug(
            "[sysop_builder] workspace %s is not a directory; skipping",
            workspace,
        )
        return None
    return workspace


def _resolve_config_ro_path() -> Path | None:
    """Resolve jiuwenswarm config.yaml for ro bind (internal startup_mode only)."""
    try:
        raw = get_config_file()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[sysop_builder] config file path resolve failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError as exc:
        logger.warning(
            "[sysop_builder] config file %s could not be resolved: %s",
            raw,
            exc,
        )
        return None
    if not resolved.exists():
        logger.warning(
            "[sysop_builder] config file %s does not exist on host; "
            "skipping from sandbox bind list",
            resolved,
        )
        return None
    return resolved


def _resolve_project_dir(override: str | Path | None) -> Path | None:
    """Resolve the host directory to bind into the sandbox as ``rw``.

    Priority:
      1. ``override`` argument (caller-supplied; useful for tests / explicit
         project pinning when ``cwd`` is not what we want).
      2. ``JIUSWARM_SANDBOX_PROJECT_DIR`` env (allows operations to pin a
         project dir without code changes).
      3. ``Path.cwd()``: the process working directory at the time
         ``build_filesystem_policy`` is called.

    Returns ``None`` when the resolved path doesn't exist, isn't a directory,
    or is the filesystem root (we refuse to ``rw``-bind ``/``; that would
    expose every other host file the user didn't intend to share).
    """
    candidates: list[Path] = []
    if override is not None:
        candidates.append(Path(override))
    env_override = os.getenv("JIUSWARM_SANDBOX_PROJECT_DIR")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path.cwd())

    for cand in candidates:
        try:
            resolved = cand.expanduser().resolve()
        except OSError as exc:
            logger.debug(
                "[sysop_builder] project_dir candidate %s could not be resolved: %s",
                cand, exc,
            )
            continue
        if not resolved.is_dir():
            logger.debug(
                "[sysop_builder] project_dir candidate %s is not a directory; skipping",
                resolved,
            )
            continue
        if resolved == Path(resolved.anchor):
            # Filesystem root (``/`` on POSIX, ``C:\`` on Windows). Refusing
            # to rw-bind root is non-negotiable: it would shadow every other
            # ro mount the policy carefully set up, plus expose host secrets
            # that the user never intended the sandbox to see.
            logger.warning(
                "[sysop_builder] refusing to mount filesystem root %s as rw "
                "project directory; pick a more specific cwd or set "
                "JIUSWARM_SANDBOX_PROJECT_DIR",
                resolved,
            )
            return None
        return resolved
    return None


def _sandbox_isolation_custom_id(project_dir: str | Path | None) -> str:
    """Stable SysOperation isolation key suffix for per-project sandbox sharing."""
    resolved = _resolve_project_dir(project_dir)
    if resolved is None:
        return "project_default"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return f"project_{digest}"


def _resolve_agent_root_dir() -> Path | None:
    """Resolve agent_root for yuanrong identity mount (enterprise_dev-style)."""
    try:
        agent_root = Path(get_agent_root_dir()).expanduser().resolve()
    except OSError as exc:
        logger.debug("[sysop_builder] agent_root resolve failed: %s", exc)
        return None
    if agent_root == Path(agent_root.anchor):
        logger.warning(
            "[sysop_builder] refusing to mount filesystem root %s as agent_root",
            agent_root,
        )
        return None
    try:
        agent_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[sysop_builder] could not ensure agent_root %s: %s",
            agent_root,
            exc,
        )
        return None
    if not agent_root.is_dir():
        logger.warning(
            "[sysop_builder] agent_root %s is not a directory; skipping mount",
            agent_root,
        )
        return None
    return agent_root


def _merge_yuanrong_mounts(
    agent_root_mount: dict[str, Any],
    config_mounts: list[Any] | None,
) -> list[dict[str, Any]]:
    """Merge forced agent_root mount with optional yaml mounts; dedupe by source+target."""
    merged: list[dict[str, Any]] = [dict(agent_root_mount)]
    seen = {
        (
            str(agent_root_mount.get("source") or ""),
            str(agent_root_mount.get("target") or ""),
        )
    }
    for entry in config_mounts or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").strip()
        target = str(entry.get("target") or source).strip()
        if not source or not target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        mount = {
            "source": source,
            "target": target,
            "readonly": bool(entry.get("readonly", False)),
        }
        merged.append(mount)
    return merged


def _build_yuanrong_extra_params() -> dict[str, Any]:
    """Assemble yuanrong provider extra_params; always identity-mount agent_root."""
    endpoint = get_sandbox_endpoint()
    executor = str(endpoint.get("executor") or "docker").strip().lower() or "docker"
    agent_root = _resolve_agent_root_dir()
    if agent_root is None:
        raise ValueError("yuanrong sandbox requires a resolvable agent_root directory")

    agent_root_str = str(agent_root)
    agent_root_mount = {
        "source": agent_root_str,
        "target": agent_root_str,
        "readonly": False,
    }
    config_mounts = endpoint.get("mounts")
    if not isinstance(config_mounts, list):
        config_mounts = None

    extra_params: dict[str, Any] = {
        "executor": executor,
        "mounts": _merge_yuanrong_mounts(agent_root_mount, config_mounts),
    }

    workdir = endpoint.get("workdir")
    if workdir is not None and str(workdir).strip():
        extra_params["workdir"] = str(workdir).strip()
    else:
        extra_params["workdir"] = agent_root_str

    for key in ("image", "cpu", "cpu_limit", "memory", "mem_limit", "rootfs"):
        if key not in endpoint:
            continue
        value = endpoint[key]
        if key == "rootfs":
            if isinstance(value, dict):
                rootfs = dict(value)
                existing = rootfs.get("mounts")
                rootfs["mounts"] = _merge_yuanrong_mounts(
                    agent_root_mount,
                    existing if isinstance(existing, list) else None,
                )
                if "workdir" not in rootfs or not str(rootfs.get("workdir") or "").strip():
                    rootfs["workdir"] = extra_params["workdir"]
                extra_params["rootfs"] = rootfs
            continue
        if value is not None:
            extra_params[key] = value
    return extra_params


def build_yuanrong_sandbox_status_view() -> dict[str, Any]:
    """Read-only yuanrong status for TUI ``/sandbox`` (enabled / executor / mounts)."""
    runtime = get_sandbox_runtime()
    endpoint = get_sandbox_endpoint()
    executor = str(endpoint.get("executor") or "docker").strip().lower() or "docker"
    try:
        mounts = list(_build_yuanrong_extra_params().get("mounts") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sysop_builder] yuanrong status mounts fallback: %s",
            exc,
        )
        config_mounts = endpoint.get("mounts")
        mounts = [
            {
                "source": str(entry.get("source") or "").strip(),
                "target": str(entry.get("target") or entry.get("source") or "").strip(),
                "readonly": bool(entry.get("readonly", False)),
            }
            for entry in (config_mounts if isinstance(config_mounts, list) else [])
            if isinstance(entry, dict) and str(entry.get("source") or "").strip()
        ]
    return {
        "type": "yuanrong",
        "enabled": bool(runtime.get("enabled")),
        "executor": executor,
        "mounts": mounts,
    }


def build_filesystem_policy(
    files_runtime: dict[str, Any] | None,
    *,
    project_dir: str | Path | None = None,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """build jiuwenbox filesystem policy."""
    del is_code_agent  # retained for caller compatibility
    files_runtime = files_runtime or {}
    validate_sandbox_files_runtime(files_runtime)
    effective_startup_mode = (
        startup_mode if startup_mode is not None else get_sandbox_startup_mode()
    )

    allow_files: list[dict[str, Any]] = []
    allow_dirs: list[dict[str, Any]] = []
    bind_mounts: list[dict[str, Any]] = []
    upload_list: list[dict[str, str]] = []
    writable_paths: list[str] = []
    read_write_promote: list[str] = []
    read_only_promote: list[str] = []

    def _record_rw_bind(
        host_path: str,
        sandbox_path: str,
        *,
        is_dir: bool,
        permissions: str,
    ) -> None:
        """Register an rw bind mount (used by intrinsic / project-dir paths).

        We deliberately do NOT also append the path to ``allow_files`` /
        ``allow_dirs``: jiuwenbox's policy validator rejects any
        ``bind_mount.sandbox_path`` that also appears in ``filesystem_policy
        .files`` (or ``.directories``) with
        ``"Filesystem file path '<x>' conflicts with a bind mount"``. Earlier
        revisions duplicated bind-mounted paths into the allow lists so that
        ``/sandbox status`` could read "what's writable" from a single
        place, but that view is now computed independently by
        :func:`list_effective_sandbox_files`, so the redundancy serves no
        purpose and actively breaks sandbox creation in mount mode.

        ``permissions`` is intentionally unused for the same reason -- bind
        mounts carry their own mode; the previous ``permissions`` value is
        kept in the signature so the callers (intrinsic / intrinsic-dir /
        project-dir) stay symmetric and self-documenting at the call site.
        """
        del permissions  # retained on the signature for caller-side symmetry
        bind_mounts.append({
            "host_path": host_path,
            "sandbox_path": sandbox_path,
            "mode": "rw",
        })
        if sandbox_path not in writable_paths:
            writable_paths.append(sandbox_path)

    def _record_user_deny_bind(host_path: str, sandbox_path: str) -> None:
        """Register a deny_write bind: ``bind_mount mode=rw`` + ``read_only`` patch."""
        bind_mounts.append({
            "host_path": host_path,
            "sandbox_path": sandbox_path,
            "mode": "rw",
        })
        if sandbox_path not in read_only_promote:
            read_only_promote.append(sandbox_path)

    def _record_ro_resource_bind(host_path: str, sandbox_path: str) -> None:
        """Register a built-in readonly resource bind (intrinsic host data).

        Used for resources the sandbox must be able to *read* but never
        write — currently only :func:`get_config_file` (sensitive
        credentials). The intent is distinct from :func:`_record_user_deny_bind`,
        which represents a user-driven ``files.deny`` entry where the
        underlying host path is normally rw and deny is just an extra
        constraint. Here the host path is *intrinsically* read-only from
        the sandbox's perspective and there is no user-side rw semantics
        to preserve.

        The built-in skills directory is intentionally **not** routed
        through this helper; it is rw-mountable so the sandboxed agent
        can also edit / install skills. See the call site in
        :func:`build_filesystem_policy`.

        Implementation: ``mode=ro`` bind + ``read_only_promote`` belt-and-
        suspenders.

        - ``mode=ro`` makes the first-pass mount land in bwrap's
          ``--ro-bind`` stage, which is the natural and self-documenting
          encoding for "ro intrinsic resource".
        - The :data:`read_only_promote` entry survives the case where a
          later rw parent bind (e.g. a user-configured ``files.allow`` on
          the parent directory of ``config.yaml``) overlays the same
          subtree and silently upgrades the mount back to rw. bwrap's
          ``created_paths`` set is the union of ro_binds + rw_binds
          destinations (see ``bwrap.py``), so the trailing
          ``--remount-ro <path>`` is guaranteed to fire on this dst and
          flip it back to read-only regardless of which stage owned the
          mount last.
        """
        bind_mounts.append({
            "host_path": host_path,
            "sandbox_path": sandbox_path,
            "mode": "ro",
        })
        if sandbox_path not in read_only_promote:
            read_only_promote.append(sandbox_path)

    mounted_rw_paths: set[str] = set()

    def _mount_rw_dir(resolved: Path) -> None:
        path_str = str(resolved)
        if path_str in mounted_rw_paths:
            return
        _record_rw_bind(path_str, path_str, is_dir=True, permissions="0777")
        mounted_rw_paths.add(path_str)

    resolved_workspace = _resolve_workspace_dir()
    if resolved_workspace is not None:
        _mount_rw_dir(resolved_workspace)

    resolved_project = _resolve_project_dir(project_dir)
    if resolved_project is not None:
        _mount_rw_dir(resolved_project)

    if effective_startup_mode == "internal":
        config_path = _resolve_config_ro_path()
        if config_path is not None:
            config_str = str(config_path)
            _record_ro_resource_bind(config_str, config_str)

    for entry in files_runtime.get("allow") or []:
        normalized = _normalize_fs_entry(entry)
        if normalized is None:
            continue
        path = normalized["path"].rstrip("/") or "/"
        normalized["path"] = path
        host = Path(path)
        if not host.exists():
            raise FileNotFoundError(
                f"sandbox files.allow path does not exist on host: {path!r}"
            )
        _record_rw_bind(
            path,
            path,
            is_dir=host.is_dir(),
            permissions="0666",
        )
        if path not in read_write_promote:
            read_write_promote.append(path)

    for entry in files_runtime.get("deny") or []:
        normalized = _normalize_fs_entry(entry)
        if normalized is None:
            continue
        path = normalized["path"].rstrip("/") or "/"
        normalized["path"] = path
        host = Path(path)
        if not host.exists():
            raise FileNotFoundError(
                f"sandbox files.deny path does not exist on host: {path!r}"
            )
        _record_user_deny_bind(path, path)

    fs_policy: dict[str, Any] = {
        "files": allow_files,
        "directories": allow_dirs,
    }
    if bind_mounts:
        fs_policy["bind_mounts"] = bind_mounts
    if read_write_promote:
        fs_policy["read_write"] = read_write_promote
    if read_only_promote:
        fs_policy["read_only"] = read_only_promote

    return {"filesystem_policy": fs_policy}, upload_list


def create_sandbox_sysop_card(
    sandbox_url: str,
    sandbox_type: str,
    *,
    files_runtime: dict[str, Any] | None = None,
    excluded_commands: list[str] | None = None,
    idle_ttl_seconds: int | None = None,
    idle_check_interval: int | None = None,
    fallback_on_failure: bool = False,
    project_dir: str | Path | None = None,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
) -> SysOperationCard | None:
    """Create sandbox SysOperationCard (jiuwenbox or yuanrong)."""
    # 触发 sandbox provider 注册（@SandboxRegistry.provider 装饰器副作用）
    import openjiuwen.extensions.sys_operation.sandbox.providers  # noqa: F401

    normalized_type = str(sandbox_type or "").strip().lower()
    try:
        if normalized_type == "yuanrong":
            extra_params = _build_yuanrong_extra_params()
            isolation_custom_id = _sandbox_isolation_custom_id(project_dir)
            gateway_config = SandboxGatewayConfig(
                isolation=SandboxIsolationConfig(
                    container_scope=ContainerScope.CUSTOM,
                    custom_id=isolation_custom_id,
                ),
                launcher_config=PreDeployLauncherConfig(
                    base_url=sandbox_url,
                    sandbox_type="yuanrong",
                    idle_ttl_seconds=idle_ttl_seconds,
                    extra_params=extra_params,
                ),
            )
            sysop_card = SysOperationCard(
                mode=OperationMode.SANDBOX,
                work_config=LocalWorkConfig(shell_allowlist=None),
                gateway_config=gateway_config,
            )
            logger.info(
                "[sysop_builder] yuanrong SysOperationCard created:\n"
                "  base_url=%s sandbox_type=yuanrong\n"
                "  isolation_custom_id=%s\n"
                "  idle_ttl=%s\n"
                "  executor=%s workdir=%s\n"
                "  mounts(%d)=%s",
                sandbox_url,
                isolation_custom_id,
                idle_ttl_seconds,
                extra_params.get("executor"),
                extra_params.get("workdir"),
                len(extra_params.get("mounts") or []),
                extra_params.get("mounts") or [],
            )
            return sysop_card

        policy, upload_list = build_filesystem_policy(
            files_runtime,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=startup_mode,
        )
        extra_params = {
            "policy": policy,
            "policy_mode": "append",
            "excluded_commands": list(excluded_commands or []),
            "fallback_on_failure": bool(fallback_on_failure),
            "preserve_file_sharing_mode": _PRESERVE_FILE_SHARING_MODE,
            "preserve_files_upload": upload_list,
        }

        if idle_check_interval is not None:
            extra_params["idle_check_interval"] = idle_check_interval

        isolation_custom_id = _sandbox_isolation_custom_id(project_dir)
        gateway_config = SandboxGatewayConfig(
            isolation=SandboxIsolationConfig(
                container_scope=ContainerScope.CUSTOM,
                custom_id=isolation_custom_id,
            ),
            launcher_config=PreDeployLauncherConfig(
                base_url=sandbox_url,
                sandbox_type=sandbox_type,
                idle_ttl_seconds=idle_ttl_seconds,
                extra_params=extra_params,
            ),
        )
        sysop_card = SysOperationCard(
            mode=OperationMode.SANDBOX,
            work_config=LocalWorkConfig(shell_allowlist=None),
            gateway_config=gateway_config,
        )

        fs_policy = policy.get("filesystem_policy", {}) if isinstance(policy, dict) else {}
        logger.info(
            "[sysop_builder] sandbox SysOperationCard created:\n"
            "  base_url=%s sandbox_type=%s\n"
            "  isolation_custom_id=%s\n"
            "  idle_ttl=%s idle_check_interval=%s\n"
            "  preserve_file_sharing_mode=%s\n"
            "  excluded_commands(%d)=%s\n"
            "  filesystem_policy.files(%d)=%s\n"
            "  filesystem_policy.directories(%d)=%s\n"
            "  filesystem_policy.bind_mounts(%d)=%s\n"
            "  filesystem_policy.read_write(%d)=%s\n"
            "  filesystem_policy.read_only(%d)=%s\n"
            "  preserve_files_upload(%d)=%s\n"
            "  policy_mode=%s",
            sandbox_url,
            sandbox_type,
            isolation_custom_id,
            idle_ttl_seconds,
            idle_check_interval,
            _PRESERVE_FILE_SHARING_MODE,
            len(extra_params["excluded_commands"]),
            extra_params["excluded_commands"] or "[]",
            len(fs_policy.get("files") or []),
            fs_policy.get("files") or [],
            len(fs_policy.get("directories") or []),
            fs_policy.get("directories") or [],
            len(fs_policy.get("bind_mounts") or []),
            fs_policy.get("bind_mounts") or [],
            len(fs_policy.get("read_write") or []),
            fs_policy.get("read_write") or [],
            len(fs_policy.get("read_only") or []),
            fs_policy.get("read_only") or [],
            len(upload_list),
            upload_list or [],
            extra_params["policy_mode"],
        )
        return sysop_card
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sysop_builder] create sandbox sysop card failed: %s", exc)
        return None


def create_local_sysop_card() -> SysOperationCard:
    """构造本地模式 SysOperationCard."""
    logger.info("[sysop_builder] local SysOperationCard created (mode=LOCAL)")
    return SysOperationCard(
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )


def _append_unique(target: list[dict[str, str]], entry: dict[str, str]) -> None:
    """Append ``entry`` to ``target`` if no existing item shares its ``path``.

    Pulled out of :func:`list_auto_managed_sandbox_paths` / :func:`list_effective_
    sandbox_files` so both helpers (and any future caller) dedupe by path the
    same way: first-write-wins, comparison on the literal ``path`` string.
    Auto-managed entries always come first, so user entries cannot override
    them just by replaying the same path.
    """
    if not any(item.get("path") == entry["path"] for item in target):
        target.append(entry)


def _classify_host_kind(path: str) -> str:
    try:
        return "directory" if Path(path).expanduser().is_dir() else "file"
    except OSError:
        return "file"


def _resolve_display_path(raw: str | Path | None) -> str | None:
    """Resolve ``raw`` into the canonical absolute path used in display/compare.

    Expands ``~`` and symlinks the same way :func:`build_filesystem_policy`
    does so that ``list_auto_managed_sandbox_paths`` and
    :func:`find_auto_managed_match` agree on what counts as the "same" entry.
    Returns ``None`` for blank or unresolvable inputs.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve())
    except OSError as exc:
        logger.debug(
            "[sysop_builder] path %r could not be resolved for display: %s",
            text, exc,
        )
        return None


def _filesystem_policy_to_display_entries(
    fs_policy: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    """Convert ``filesystem_policy.bind_mounts`` into ``/sandbox`` display entries."""
    allow: list[dict[str, str]] = []
    deny: list[dict[str, str]] = []
    read_only = {
        str(path)
        for path in (fs_policy.get("read_only") or [])
        if str(path).strip()
    }
    for mount in fs_policy.get("bind_mounts") or []:
        if not isinstance(mount, dict):
            continue
        host_path = str(mount.get("host_path") or mount.get("sandbox_path") or "").strip()
        if not host_path:
            continue
        sandbox_path = str(mount.get("sandbox_path") or host_path).strip()
        mount_mode = str(mount.get("mode") or "rw").lower()
        access = "ro" if sandbox_path in read_only or mount_mode == "ro" else "rw"
        kind = _classify_host_kind(host_path)
        if kind == "directory" and host_path != "/":
            display_path = host_path.rstrip("/") + "/"
        else:
            display_path = host_path
        entry = {"path": display_path, "access": access, "kind": kind}
        bucket = deny if access == "ro" else allow
        _append_unique(bucket, entry)
    return {"allow_write": allow, "deny_write": deny}


def effective_files_from_policy(policy: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Derive ``/sandbox`` display entries from a cached launcher policy dict."""
    fs_policy = policy.get("filesystem_policy") if isinstance(policy, dict) else {}
    if not isinstance(fs_policy, dict):
        fs_policy = {}
    return _filesystem_policy_to_display_entries(fs_policy)


def list_auto_managed_sandbox_paths(
    project_dir: str | Path | None = None,
    *,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Auto-configured sandbox entries that users cannot mutate via ``/sandbox``.

    Mirrors :func:`build_filesystem_policy` auto-managed mounts:

    - ``workspace`` → ``allow_write`` (rw)
    - ``project_dir`` (when resolved) → ``allow_write`` (rw)
    - ``config.yaml`` → ``deny_write`` (ro) only when ``startup_mode=internal``
    """
    del is_code_agent
    allow: list[dict[str, str]] = []
    deny: list[dict[str, str]] = []
    effective_startup_mode = (
        startup_mode if startup_mode is not None else get_sandbox_startup_mode()
    )

    workspace = _resolve_workspace_dir()
    if workspace is not None:
        _append_unique(
            allow,
            {
                "path": str(workspace) + "/",
                "access": "rw",
                "kind": "directory",
            },
        )

    resolved_project: Path | None
    if project_dir is not None:
        try:
            resolved_project = Path(project_dir).expanduser().resolve()
        except OSError as exc:
            logger.debug(
                "[sysop_builder] auto view: project_dir %r resolve failed: %s",
                project_dir,
                exc,
            )
            resolved_project = None
    else:
        resolved_project = _resolve_project_dir(None)

    if (
        resolved_project is not None
        and resolved_project.is_dir()
        and resolved_project != Path(resolved_project.anchor)
    ):
        project_str = str(resolved_project)
        if not any(item.get("path", "").rstrip("/") == project_str for item in allow):
            _append_unique(
                allow,
                {
                    "path": project_str + "/",
                    "access": "rw",
                    "kind": "directory",
                },
            )

    if effective_startup_mode == "internal":
        config_path = _resolve_config_ro_path()
        if config_path is not None:
            _append_unique(
                deny,
                {"path": str(config_path), "access": "ro", "kind": "file"},
            )

    return {"allow_write": allow, "deny_write": deny}


def list_effective_sandbox_files(
    files_runtime: dict[str, Any] | None,
    *,
    project_dir: str | Path | None = None,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Read-only "what will the sandbox actually allow / deny writes to" view.

    The result is the union of :func:`list_auto_managed_sandbox_paths` and the
    user-configured ``files.allow`` / ``files.deny`` entries from
    ``config.yaml::sandbox.files``. Auto entries appear first so any
    duplicate user entry is suppressed (and stays out of the displayed list).

    Mirrors the union :func:`build_filesystem_policy` would assemble, but does
    NOT create empty files or directories on the host. Intended for display
    by ``/sandbox status`` / ``/sandbox files list``.

    Args:
        files_runtime: ``sandbox.files`` dict from ``get_sandbox_runtime()``.
        project_dir: explicit override for the rw bind-mount root. Callers
            should pass the trusted-directory project root cached on the
            adapter (i.e. ``trusted_dirs[0]``); ``None`` means "unknown",
            which suppresses the project-dir entry instead of falling back
            to ``cwd``.

    Returns:
        ``{"allow_write": [...], "deny_write": [...]}`` where every entry is
        ``{"path": str, "access": "rw"|"ro", "kind": "file" | "directory"}``.
    """
    auto = list_auto_managed_sandbox_paths(
        project_dir=project_dir,
        is_code_agent=is_code_agent,
        startup_mode=startup_mode,
    )
    allow = list(auto["allow_write"])
    deny = list(auto["deny_write"])

    files_runtime = files_runtime or {}
    validate_sandbox_files_runtime(files_runtime)

    def _emit(bucket: list[dict[str, str]], entry: Any, *, access: str) -> None:
        normalized = _normalize_fs_entry(entry)
        if normalized is None:
            return
        stripped = str(normalized["path"]).rstrip("/") or "/"
        kind = _classify_host_kind(stripped)
        display = stripped + "/" if kind == "directory" and stripped != "/" else stripped
        _append_unique(
            bucket,
            {
                "path": display,
                "access": access,
                "kind": kind,
            },
        )

    for entry in files_runtime.get("allow") or []:
        _emit(allow, entry, access="rw")
    for entry in files_runtime.get("deny") or []:
        _emit(deny, entry, access="ro")

    return {"allow_write": allow, "deny_write": deny}


def find_auto_managed_match(
    path: str,
    *,
    project_dir: str | Path | None = None,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(bucket, canonical_path)`` if ``path`` is auto-managed; else ``None``.

    Used by ``/sandbox files allow|deny`` to refuse mutations that would
    duplicate or contradict an auto-managed entry. Comparison normalizes
    ``~``, trailing slashes, and ``./`` segments so the user can't sneak the
    same path in by varying its surface form.

    Args:
        path: user-supplied path (may use ``~``, trailing slash, etc.).
        project_dir: project-root override forwarded to
            :func:`list_auto_managed_sandbox_paths`.

    Returns:
        ``(bucket, canonical_path)`` where ``bucket`` is ``"allow_write"`` or
        ``"deny_write"`` and ``canonical_path`` is the entry's displayed
        path (trailing-slash for directories). ``None`` when the path is
        not auto-managed.
    """
    target = _resolve_display_path(path)
    if target is None:
        return None
    auto = list_auto_managed_sandbox_paths(
        project_dir=project_dir,
        is_code_agent=is_code_agent,
        startup_mode=startup_mode,
    )
    for bucket in ("allow_write", "deny_write"):
        for entry in auto.get(bucket, []):
            candidate = _resolve_display_path(entry.get("path", ""))
            if candidate is not None and candidate == target:
                return bucket, str(entry.get("path", ""))
    return None


__all__ = [
    "PreserveFileSharingMode",
    "build_filesystem_policy",
    "build_yuanrong_sandbox_status_view",
    "create_sandbox_sysop_card",
    "create_local_sysop_card",
    "effective_files_from_policy",
    "find_auto_managed_match",
    "find_nested_files_conflict",
    "list_auto_managed_sandbox_paths",
    "list_effective_sandbox_files",
    "validate_sandbox_files_runtime",
]
