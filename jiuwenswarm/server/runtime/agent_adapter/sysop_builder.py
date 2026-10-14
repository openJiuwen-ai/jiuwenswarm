# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Literal

from openjiuwen.agent_teams.schema.deep_agent_spec import SysOperationSpec
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SandboxGatewayConfig,
    SysOperation,
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
    get_config_file,
)
from jiuwenswarm.edition import is_enterprise

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


def _resolve_shared_dir(shared_dir: str | Path | None = None) -> Path | None:
    """Resolve sandbox rw bind root (shared downloads + workspace).

    Prefer an explicit ``shared_dir`` (enterprise multi-tenant workspace).
    Otherwise mount ``get_agent_root_dir()`` (e.g. ``~/.jiuwenswarm/agent``)
    rather than only ``agent/jiuwenclaw_workspace``, so sibling download paths remain writable.
    """
    try:
        if shared_dir is not None and str(shared_dir).strip():
            shared = Path(shared_dir).expanduser().resolve()
        else:
            shared = Path(get_agent_root_dir()).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        logger.debug("[sysop_builder] shared dir resolve failed: %s", exc)
        return None
    if shared == Path(shared.anchor):
        logger.warning(
            "[sysop_builder] refusing to mount filesystem root %s as shared dir",
            shared,
        )
        return None
    try:
        shared.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[sysop_builder] could not ensure shared dir %s: %s",
            shared,
            exc,
        )
        return None
    if not shared.is_dir():
        logger.debug(
            "[sysop_builder] shared dir %s is not a directory; skipping",
            shared,
        )
        return None
    return shared


# Backward-compatible alias
_resolve_workspace_dir = _resolve_shared_dir


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


def _sandbox_isolation_custom_id(
    project_dir: str | Path | None,
    *,
    shared_dir: str | Path | None = None,
    sandbox_url: str | None = None,
) -> str:
    """Stable SysOperation isolation key suffix for per-project sandbox sharing.

    ``sandbox_url`` (box-server endpoint) 折进 digest: 它是沙箱身份的一部分,
    不同端口服务的是不同后端。端口进 key 后, 端口变化生成新 key、注册新 card,
    旧 card 自然被取代, 而非被 registry 按相同 key 复用到陈旧端口的旧 card。
    card 构建侧从 ``JiuwenBoxRunner.instance().get_owned_endpoint()`` 取本轮存活
    端口传入, 保证 key 与 base_url 同源。
    """
    resolved = _resolve_project_dir(project_dir)
    endpoint_token = (sandbox_url or "").strip().lower().rstrip("/")
    if is_enterprise():
        # Match the trusted workspace mounted for this tenant. A default
        # project must not reuse another tenant's cached sandbox client.
        root = Path(shared_dir if shared_dir is not None else get_agent_root_dir()).expanduser().resolve()
        scope = json.dumps(
            [
                str(root),
                str(resolved) if resolved is not None else None,
                endpoint_token,
            ]
        )
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        return f"workspace_project_{digest}"
    if resolved is None:
        base = "project_default"
    else:
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        base = f"project_{digest}"
    if not endpoint_token:
        return base
    ep_digest = hashlib.sha256(endpoint_token.encode("utf-8")).hexdigest()[:8]
    return f"{base}_ep_{ep_digest}"


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


def _skill_state_paths(shared_root: Path | None) -> list[Path]:
    from jiuwenswarm.common.utils import get_agent_sessions_dir, get_user_workspace_dir

    paths = [get_agent_sessions_dir(), get_user_workspace_dir() / ".skill_authorization"]
    if shared_root is not None:
        paths.extend([shared_root / "sessions", shared_root / "agent" / "sessions"])
    return paths


def _protect_yuanrong_skill_state(mounts: list[dict[str, Any]], agent_root: Path) -> list[dict[str, Any]]:
    """Keep service state read-only beneath writable workspace mounts."""
    protected = _skill_state_paths(agent_root)
    for mount in mounts:
        if mount.get("readonly"):
            continue
        source = Path(mount["source"]).expanduser().resolve()
        target = Path(mount["target"]).expanduser()
        if any(source == path.resolve() or path.resolve() in source.parents for path in protected):
            raise ValueError("yuanrong mount cannot override service-owned session state")
        if source != target and any(source in path.resolve().parents for path in protected):
            raise ValueError("yuanrong mount cannot alias service-owned session state")
    result = list(mounts)
    for path in protected:
        path.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
        result.append({"source": resolved, "target": resolved, "readonly": True})
    return result


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
        "mounts": _protect_yuanrong_skill_state(
            _merge_yuanrong_mounts(agent_root_mount, config_mounts), agent_root
        ),
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
                rootfs["mounts"] = _protect_yuanrong_skill_state(
                    _merge_yuanrong_mounts(
                        agent_root_mount,
                        existing if isinstance(existing, list) else None,
                    ),
                    agent_root,
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
    shared_dir: str | Path | None = None,
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
        - The :data:`read_only_promote` entry adds the path to the policy
          ``read_only`` list, granting Landlock read-only access and
          scheduling a trailing ``--remount-ro``. That remount flips the file
          back to read-only when a later rw bind overlays it; it resolves
          because :meth:`BwrapConfig.to_args` emits a ro-bind nested under a
          rw parent *after* the rw parent (see
          ``_partition_nested_ro_binds`` in ``bwrap.py``).
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

    resolved_workspace = _resolve_shared_dir(shared_dir)
    if resolved_workspace is not None:
        _mount_rw_dir(resolved_workspace)

    # Team 成员 workspace 落在 ~/.jiuwenswarm/.agent_teams/<team>/workspaces/<member>/，
    # 是 agent/ 挂载根的兄弟目录、不在其下；挂父目录使 team 派生时才建的子目录
    # 随建随可见可写，绕开"沙箱先于 team 派生"的时间线。单 agent 沙箱也挂（空目录
    # 无害），保证后续 team 复用同一项目级容器时挂载已就位。get_agent_teams_home()
    # 随 app 对 openjiuwen home 的重定向而 tenant-correct。
    try:
        from openjiuwen.agent_teams.paths import get_agent_teams_home
        agent_teams_home = get_agent_teams_home()
        if agent_teams_home is not None:
            agent_teams_home.mkdir(parents=True, exist_ok=True)
            _mount_rw_dir(agent_teams_home.resolve())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sysop_builder] mount .agent_teams rw failed: %s", exc)

    resolved_project = _resolve_project_dir(project_dir)
    if resolved_project is not None:
        _mount_rw_dir(resolved_project)

    if effective_startup_mode == "internal":
        config_path = _resolve_config_ro_path()
        if config_path is not None:
            config_str = str(config_path)
            _record_ro_resource_bind(config_str, config_str)

    # Session metadata is service-owned. A writable tenant/project parent must
    # never let a tool edit, rename or remove Skill authorization files.
    # Cover both default agent roots and explicit enterprise tenant roots.
    protected_paths = _skill_state_paths(resolved_workspace)
    if resolved_project is not None and any(
        resolved_project == path.resolve() or path.resolve() in resolved_project.parents
        for path in protected_paths
    ):
        raise ValueError("project_dir cannot be a service-owned session directory")
    for protected in protected_paths:
        protected.mkdir(parents=True, exist_ok=True)
        protected_str = str(protected.resolve())
        _record_ro_resource_bind(protected_str, protected_str)

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
        resolved_host = host.resolve()
        if resolved_host != host and any(
            resolved_host == protected.resolve() or resolved_host in protected.resolve().parents
            for protected in protected_paths
        ):
            raise ValueError("sandbox files.allow cannot alias service-owned session state")
        if any(resolved_host == protected.resolve() or protected.resolve() in resolved_host.parents
               for protected in protected_paths):
            raise ValueError("sandbox files.allow cannot override service-owned session state")
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


def build_process_policy() -> dict[str, Any]:
    """获取当前进程的有效用户名与用户组名。

    仅企业版（Linux 容器）调用：使用 ``geteuid`` / ``getegid`` 解析有效身份；
    若 passwd/group 中无对应条目, 则回退为 UID/GID 的字符串形式。
    非 POSIX 平台（如 Windows 单机版）返回空 dict，调用方跳过 process 段。
    """
    if not hasattr(os, "geteuid"):
        return {}
    import grp
    import pwd

    uid = os.geteuid()
    gid = os.getegid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    try:
        groupname = grp.getgrgid(gid).gr_name
    except KeyError:
        groupname = str(gid)

    process_policy: dict[str, Any] = {
        "run_as_user": username,
        "run_as_group": groupname,
    }
    return process_policy


def resolve_sandbox_endpoint() -> tuple[str | None, str | None]:
    """解析当前生效的沙箱类型与 base url，与主 agent ``_resolve_sys_operation`` 同源。

    读 ``get_sandbox_endpoint()``；jiuwenbox 时用 ``JiuwenBoxRunner`` 本轮存活端口
    覆盖 config url（端口会进 isolation key，必须与主 agent 取同一来源，否则 team
    成员与主 agent 的 isolation key 不一致 → 各起独立沙箱）。runner 未 spawn /
    进程死亡时返回 None，回落 config url。

    Returns:
        ``(sandbox_type, sandbox_url)``，任一不可得时对应元素为 None。
    """
    endpoint = get_sandbox_endpoint()
    sandbox_type = endpoint.get("type") or None
    sandbox_url = endpoint.get("url") or None
    if (sandbox_type or "").strip().lower() == "jiuwenbox":
        try:
            from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner
            owned = JiuwenBoxRunner.instance().get_owned_endpoint()
            if owned is not None:
                sandbox_url = f"http://{owned[0]}:{owned[1]}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[sysop_builder] resolve live sandbox endpoint failed, "
                "fall back to config url: %s", exc,
            )
    return sandbox_type, sandbox_url


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
    shared_dir: str | Path | None = None,
) -> SysOperationCard | None:
    """Create sandbox SysOperationCard (jiuwenbox or yuanrong)."""
    _t0 = time.monotonic()
    # 触发 sandbox provider 注册（@SandboxRegistry.provider 装饰器副作用）
    import openjiuwen.extensions.sys_operation.sandbox.providers  # noqa: F401

    normalized_type = str(sandbox_type or "").strip().lower()
    isolation_custom_id = ""
    try:
        if normalized_type == "yuanrong":
            extra_params = _build_yuanrong_extra_params()
            isolation_custom_id = _sandbox_isolation_custom_id(
                project_dir, shared_dir=shared_dir, sandbox_url=sandbox_url,
            )
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
            logger.info(
                "[SandboxPerf] sysop_builder.create_sandbox_card: agent_id=%s "
                "sandbox_type=yuanrong total_ms=%.1f",
                isolation_custom_id,
                (time.monotonic() - _t0) * 1000,
            )
            return sysop_card

        _t_policy0 = time.monotonic()
        policy, upload_list = build_filesystem_policy(
            files_runtime,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=startup_mode,
            shared_dir=shared_dir,
        )
        policy_ms = (time.monotonic() - _t_policy0) * 1000
        if is_enterprise():
            process_policy = build_process_policy()
            if process_policy:
                policy['process'] = process_policy
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

        isolation_custom_id = _sandbox_isolation_custom_id(
            project_dir, shared_dir=shared_dir, sandbox_url=sandbox_url,
        )
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

        logger.info(
            "[sysop_builder] sandbox SysOperationCard created:\n"
            "  base_url=%s sandbox_type=%s\n"
            "  isolation_custom_id=%s\n"
            "  idle_ttl=%s idle_check_interval=%s\n"
            "  preserve_file_sharing_mode=%s\n"
            "  excluded_commands(%d)=%s\n"
            "  policy=%s\n"
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
            json.dumps(policy, ensure_ascii=False, indent=2),
            len(upload_list),
            upload_list or [],
            extra_params["policy_mode"],
        )
        logger.info(
            "[SandboxPerf] sysop_builder.create_sandbox_card: agent_id=%s "
            "policy_ms=%.1f total_ms=%.1f",
            isolation_custom_id,
            policy_ms,
            (time.monotonic() - _t0) * 1000,
        )
        return sysop_card
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sysop_builder] create sandbox sysop card failed: %s", exc)
        logger.info(
            "[SandboxPerf] sysop_builder.create_sandbox_card: agent_id=%s ok=0 "
            "total_ms=%.1f",
            isolation_custom_id or "-",
            (time.monotonic() - _t0) * 1000,
        )
        return None


def sandbox_isolation_key_of(sysop_card: SysOperationCard) -> str | None:
    """由 card 算出 isolation key template；无注册副作用。

    ⚠️ 全仓唯一计算 isolation key 的入口。key 的全部输入
    （``container_scope`` / ``launcher_type`` / ``sandbox_type`` / ``custom_id``）
    由本模块 ``create_sandbox_sysop_card`` 与 ``_sandbox_isolation_custom_id``
    决定；其他模块一律调用本函数，**不要**自行构 ``SysOperation`` 推导——
    那会让 key 的定义散落到多处，上游一旦调整组成方式必然漏改。

    零副作用依据：``SysOperation.__init__`` 只赋值 ``id`` / ``mode`` /
    ``_run_config`` / ``_instances``（``sys_operation.py:170-185``），
    ``isolation_key_template`` 是只读 property（``:197-202``），故此探针不触碰
    ``Runner`` / ``ResourceMgr`` 任何状态。

    Args:
        sysop_card: 任意 ``SysOperationCard``。注意 key **与 card.id 无关**，
            只取决于 ``mode`` + ``gateway_config``——这正是借用句柄可以与
            注册 id 解耦的前提。

    Returns:
        isolation key template；构造失败时返回 ``None``。
    """
    try:
        from openjiuwen.core.sys_operation.sys_operation import SysOperation

        return SysOperation(sysop_card).isolation_key_template
    except Exception as exc:  # noqa: BLE001
        logger.debug("[sysop_builder] compute sandbox isolation key failed: %s", exc)
        return None


def registered_sandbox_owner_by_key(
    isolation_key_template: str | None,
) -> "SysOperation | None":  # type: ignore[name-defined]
    """返回持有该 isolation key 且仍注册着的 SANDBOX ``SysOperation`` 实例。

    ⚠️ 全仓唯一触碰 ``Runner.resource_mgr._resource_registry.sys_operation()
    ._sandbox_key_owner_map`` 的地方——那是 openjiuwen 的**私有 API**，没有
    public 出口。上游一旦提供 public 查询，本函数应整体删除。

    存在的理由：库 ``SysOperationMgr``（``sys_operation_manager.py:38-49``）
    强制"一个 isolation key template 只属于一个 sysop id"：同 key 异 id 的
    ``add_sys_operation`` 抛 ``ValueError``，且被 ``_inner_add_resource`` 的
    try/except 吞成 ``Error`` 返回（``resource_manager.py:1449``）——而库
    ``SysOperationSpec.resolve``（``deep_agent_spec.py:259``）**不检查该 Result**
    就丢弃，260 行再 get 只拿到 ``None``，harness factory 随即兜底出 LOCAL sysop
    （成员工具落宿主机）。本函数给出唯一合规出口：按 owner map 查已注册实例直接
    复用，零 add 零冲突。

    查不到（owner map 与 registry 失配 / 指向非沙箱 / 私有 API 变更）一律返回
    ``None``，由调用方决定降级。**失败一律 warning 级**——私有 API 一旦失效必须
    响亮，否则又退化成无声的隔离降级。
    """
    if not isolation_key_template:
        return None
    try:
        from openjiuwen.core.runner.runner import Runner

        registry = getattr(Runner.resource_mgr, "_resource_registry", None)
        if registry is None:
            logger.warning(
                "[sysop_builder] Runner.resource_mgr._resource_registry 不可达，"
                "无法按 isolation key 复用已注册沙箱：key=%s",
                isolation_key_template,
            )
            return None
        owner_map = getattr(registry.sys_operation(), "_sandbox_key_owner_map", {})
        owner_id = owner_map.get(isolation_key_template)
        if not owner_id:
            return None
        owner = Runner.resource_mgr.get_sys_operation(owner_id)
        if owner is None or getattr(owner, "mode", None) != OperationMode.SANDBOX:
            # owner map 与 registry 失配，或指向非沙箱实例：不可用。
            logger.warning(
                "[sysop_builder] isolation key owner 失配：key=%s owner_id=%s mode=%s",
                isolation_key_template,
                owner_id,
                getattr(owner, "mode", None),
            )
            return None
        return owner
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sysop_builder] lookup registered sandbox owner failed: %s", exc)
        return None


# 自持沙箱（team owner 未命中时注册）的记账表：
# ``运行态(session-scoped) team 名 -> 该 team 名下的注册 id 集合``。
#
# 注册 id 是 ``team_sandbox_owned_<team_id>``，与成员 spec 的借用句柄
# ``team_sandbox_ref_<team_id>`` **刻意不同**——成员 dispose 只看到借用句柄
# （``_BorrowedSandboxHandle``），因此永不触碰自持沙箱；回收由
# release_team_owned_sandbox 在 team 生命周期结束时统一执行。
#
# ⚠️ 必须按**运行态全名精确查表**，不能靠"``team_sandbox_owned_<name>`` 前缀反推
# 归属"：运行态名是 ``{base}_{session_suffix}``，而 team 名几乎无约束
# （``team_binding_store.TEAM_NAME_RE`` 不排除下划线），base 名与运行态名互相是
# 前缀的情形完全合法——``abc`` 与 ``abc_sess1`` 并存时，按前缀释放 ``abc`` 会连带
# 摘掉 ``abc_sess1`` 的沙箱；反向（传运行态名 ``abc_sess1``）同样会误伤名为
# ``abc_sess1_sess2`` 的 team。**两个方向都会越界**，记全名是唯一无歧义的做法。
_TEAM_OWNED_SYSOP_IDS: dict[str, set[str]] = {}
_TEAM_OWNED_SYSOP_IDS_LOCK = threading.Lock()


def release_team_owned_sandbox(team_name: str) -> None:
    """释放该 team 名下自持的沙箱（**按运行态全名精确查表**）。

    运行时 team 名是 session-scoped 的（``team_manager.build_session_scoped_team_name``
    产出 ``{base}_{suffix}``），而 ``create_sandbox_sysop_spec`` 记账时用的
    ``team_id`` 正是这个运行态名（``assembly`` 在 ``_apply_session_scoped_team_name``
    之后才调 enrich）。故调用方必须传**同一个运行态名**才能命中。

    传 base 名会查不到——这是**有意**的：漏释放只是容器多留一会儿（下次
    ``destroy_team`` 仍会兜底），误杀则是把另一个 team 正在用的沙箱连同其
    isolation key 一起摘掉，无法自愈。

    Args:
        team_name: session-scoped 运行态名，如 ``abc_sess1``。
    """
    name = str(team_name or "").strip()
    if not name:
        return
    with _TEAM_OWNED_SYSOP_IDS_LOCK:
        targets = _TEAM_OWNED_SYSOP_IDS.pop(name, None)
    if not targets:
        return
    try:
        from openjiuwen.core.runner.runner import Runner

        for owned_id in targets:
            try:
                if Runner.resource_mgr.get_sys_operation(owned_id) is not None:
                    Runner.resource_mgr.remove_sys_operation(owned_id)
                    logger.info("[sysop_builder] team 自持沙箱已释放：id=%s", owned_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[sysop_builder] team 自持沙箱释放失败：id=%s err=%s", owned_id, exc
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sysop_builder] release team owned sandbox failed: %s", exc)


class _BorrowedSandboxHandle(SysOperation):
    """借用句柄视图：``.id`` 指向**永不注册**的句柄名，其余全部委托真实实例。

    库在成员销毁时（``team_harness.py:215`` → ``native_harness.py:467-474``）
    读 ``config.sys_operation`` 的 ``.id`` 决定是否注销。而 ``config.sys_operation``
    是 ``resolve()`` 的**返回值**（``deep_agent_spec.py:587`` 把 spec 整个换成了
    实例，``factory.py:391`` 原样塞进 DeepAgentConfig）——``spec.id`` 在那一刻
    就被丢弃了，从未进入任何注销判定。故"句柄永不注册"这条保护必须落在**实例的
    ``.id``** 上，而不是 spec 上：句柄未注册 ⇒ 库那句 ``get_sys_operation(id) is
    not None`` 判定恒假 ⇒ 成员 shutdown / team 销毁不摘任何他方资源，也不经
    ``sys_operation_manager.py:67-68`` 释放其 isolation key。

    刻意**不**改真实实例的 ``.id``：``config.sys_operation`` 与
    ``get_sys_operation(owner_id)`` 是**同一个对象**，就地改会污染主 agent 的
    注册簿（owner map 仍指向旧 id 而实例已改名）。

    **必须**继承 ``SysOperation``：``factory.py:337`` 用
    ``isinstance(sys_operation, SysOperation)`` 区分"库已 resolve 的实例"与"未
    设置"，判定失败会把本句柄当 ``None`` 丢弃并重建 LOCAL。

    依赖（上游一旦改动即为失效点，届时需重新审计）：消费方一律把
    ``deep_config.sys_operation`` 当**Python 引用**用，不回查注册表、不读
    ``.id``——``SysOperationRail.init`` 的工具 id 形如 ``write_file_<agent_id>``，
    与 SysOperation 实例无关（``sys_operation_rail.py:52, 87-91`` 的注释明写），
    工具直接持引用调 ``sysop.fs() / .shell() / .code()``。故换 ``.id`` 不影响
    任何工具行为，只影响 dispose 的那一行判定。
    """

    def __init__(self, source: SysOperation, handle_id: str) -> None:
        """构造句柄视图。

        绕开 ``super().__init__``：那会重新校验 gateway_config 并重算 isolation
        key（``sys_operation.py:170-185``），既无必要也可能与真实实例产生偏差。

        Args:
            source: 真实已注册的 SANDBOX 实例（owner 复用的或 team 自持的）。
            handle_id: 借用句柄名（``team_sandbox_ref_<team>``），刻意不注册。
        """
        self.id = handle_id
        self.mode = source.mode
        # 共享而非复制：_run_config 承载 isolation key 与 gateway，_instances 是
        # fs/shell/code 子操作的 memo（``sys_operation.py:228-238``）——复制会各建
        # 一套容器连接。共享保证句柄与真实实例操作同一个容器。
        self._run_config = source._run_config
        self._instances = source._instances


class TeamSharedSandboxSpec(SysOperationSpec):
    """team 成员**借用**已注册沙箱的句柄；自身不持有任何注册。

    ``id`` 是借用句柄名（``team_sandbox_ref_<team>``），**刻意永不注册**进
    ``Runner.resource_mgr``，并经 :class:`_BorrowedSandboxHandle` 成为
    ``resolve()`` 返回实例的 ``.id``——这是 dispose 唯一读到的 id。

    对齐 owner id 来"零 add 命中"是**错的**：那样 dispose 会连带注销 owner 的
    沙箱（并释放其 isolation key），下次 owner 重新 add 出新 uuid 即重新易主，
    构成又一次静默回落 LOCAL。

    ``resolve()`` 分三级：复用 owner → 自持 team 专属沙箱 → ``None``（成员维持
    LOCAL，即本特性上线前的既有行为，零回归）。第二级不可省：分布式 bootstrap
    下 owner 侧的 project_dir 与成员侧不同源，key 必然不匹配，此时若不
    自持就直接把成员丢回宿主机——比"多一个容器"糟得多。

    **前两级都包一层借用句柄**：成员 dispose 时拿到的是包出来的实例，其 ``.id``
    未注册 ⇒ 库那句判定恒假 ⇒ 既不会摘掉别人的沙箱，也**不会摘掉本 team 的自持
    沙箱**（自持沙箱全队共用，由 :func:`release_team_owned_sandbox` 在 team 生命
    周期结束时统一回收）。此前"注册 id 与句柄解耦 ⇒ dispose 碰不到自持沙箱"的
    推理漏了这一层——解耦的是 spec.id，而 dispose 读的是 resolve 后的实例 id。

    ``team_id`` 只用于推导自持 id，不进 ``build_card()``（key 与 id 无关）。
    """

    team_id: str = ""

    def resolve(self) -> "SysOperation | None":  # type: ignore[name-defined]
        """复用 owner；无 owner 时自持；均不可用则返回 None（成员维持 LOCAL）。

        返回的一律是 :class:`_BorrowedSandboxHandle` 句柄视图而非真实实例，
        使 ``.id`` 永不命中注册表。
        """
        card = self.build_card()
        key = sandbox_isolation_key_of(card)
        owner = registered_sandbox_owner_by_key(key)
        if owner is None:
            owner = self._ensure_team_owned_sandbox(card, key)
        if owner is None:
            logger.warning(
                "[sysop_builder] team sandbox 不可用，成员回落 LOCAL；ref_id=%s key=%s",
                self.id,
                key,
            )
            return None
        return _BorrowedSandboxHandle(owner, self.id)

    def _ensure_team_owned_sandbox(
        self,
        card: SysOperationCard,
        key: str | None,
    ) -> "SysOperation | None":  # type: ignore[name-defined]
        """owner 未命中时，为本 team 注册一个自持沙箱。

        注册 id 与借用句柄 ``self.id`` **刻意不同**：前者是我们真正持有的资源，
        后者永不注册。回收由 :func:`release_team_owned_sandbox` 在 team 生命
        周期结束时统一执行。

        ⚠️ "id 解耦 ⇒ 成员 dispose 碰不到自持沙箱" 这条推理**不成立**：库 dispose
        读的是 ``resolve()`` 返回**实例**的 ``.id``（``deep_agent_spec.py:587``
        起 spec 已被丢弃），而本函数返回的正是自持实例本身，其 ``.id`` 就是
        ``owned_id``——解耦只发生在 spec 上。真正兜住的是
        :meth:`TeamSharedSandboxSpec.resolve` 把返回值包成
        :class:`_BorrowedSandboxHandle`，让 dispose 看到未注册的句柄名。
        **本函数不得被绕过 ``resolve()`` 直接使用。**

        Args:
            card: 借用句柄对应的 card；会复制出 ``id`` 为自持 id 的新 card。
            key: 已算出的 isolation key，仅用于日志与排障。

        Returns:
            自持的 SANDBOX 实例；无 team_id 或注册失败时返回 ``None``。
        """
        if not self.team_id:
            return None
        owned_id = f"team_sandbox_owned_{self.team_id}"
        try:
            from openjiuwen.core.runner.runner import Runner

            with _TEAM_OWNED_SYSOP_IDS_LOCK:
                existing = Runner.resource_mgr.get_sys_operation(owned_id)
                if existing is not None:
                    return existing
                result = Runner.resource_mgr.add_sys_operation(
                    card.model_copy(update={"id": owned_id})
                )
                # 必须检查 Result：库 SysOperationSpec.resolve（deep_agent_spec.py:259）
                # 恰恰漏了这一步，同 key 冲突被静默吞成 None 后回落 LOCAL。
                if result.is_err():
                    logger.warning(
                        "[sysop_builder] team 自持沙箱注册失败：id=%s key=%s err=%s",
                        owned_id,
                        key,
                        result.msg(),
                    )
                    return None
                _TEAM_OWNED_SYSOP_IDS.setdefault(self.team_id, set()).add(owned_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[sysop_builder] ensure team owned sandbox failed: id=%s err=%s",
                owned_id,
                exc,
            )
            return None
        logger.info(
            "[sysop_builder] team 自持沙箱已注册（owner 未命中）：id=%s key=%s",
            owned_id,
            key,
        )
        return Runner.resource_mgr.get_sys_operation(owned_id)


def create_sandbox_sysop_spec(
    sandbox_url: str,
    sandbox_type: str,
    *,
    team_id: str,
    files_runtime: dict[str, Any] | None = None,
    excluded_commands: list[str] | None = None,
    idle_ttl_seconds: int | None = None,
    idle_check_interval: int | None = None,
    fallback_on_failure: bool = False,
    project_dir: str | Path | None = None,
    is_code_agent: bool = False,
    startup_mode: str | None = None,
    shared_dir: str | Path | None = None,
) -> "SysOperationSpec | None":  # type: ignore[name-defined]
    """构建 team 成员共享的 SANDBOX ``SysOperationSpec``。

    复用 :func:`create_sandbox_sysop_card` 的 ``gateway_config``（同 policy、
    同 isolation key 输入），保证成员与主 agent 命中同一项目级沙箱容器。spec
    形态供 ``agent_configurator`` 的 ``agent_spec.sys_operation or LOCAL`` override
    命中——成员 spec 设此项后，``DeepAgentSpec.build()`` 会 ``.resolve()`` 成
    SANDBOX ``SysOperation`` 实例，成员的 fs/shell/code 工具随之走沙箱。

    id 策略：``team_sandbox_ref_<team_id>`` 是**借用句柄名**，永不注册。库
    ``resolve()`` 只按 id get-or-create，若此 id 恰好等于已注册的 owner id 固然能
    零 add 命中，但成员销毁时 ``native_harness.dispose()`` 会按同一 id 执行
    ``remove_sys_operation``，连带摘掉主 agent 的沙箱并释放其 isolation key——
    下次主 agent 重新 add 出新 uuid 即重新易主，又构成一次静默回落 LOCAL。
    故 id 刻意与 owner 解耦，由 ``TeamSharedSandboxSpec.resolve()`` 在 harness
    构建期按 owner map 复用实例，并把复用到的实例包成
    :class:`_BorrowedSandboxHandle`——**只有包了它，dispose 才只看到未注册的句柄名**。

    无 owner 且自持也失败时 ``resolve()`` 返回 ``None``，库 factory 兜底出 LOCAL sysop
    ——即本特性上线前的既有行为，零回归。本版复用主 agent 的项目级沙箱，同一
    isolation key 下不存在合法的第二个注册路径（库强制一 key 一 owner）。

    Args:
        team_id: 借用句柄名后缀（用 ``spec.team_name``）。全队共用一个 spec 对象；
            由于 dispose 只看到未注册的句柄名，成员间不存在 add/dispose 争用。
    """
    card = create_sandbox_sysop_card(
        sandbox_url,
        sandbox_type,
        files_runtime=files_runtime,
        excluded_commands=excluded_commands,
        idle_ttl_seconds=idle_ttl_seconds,
        idle_check_interval=idle_check_interval,
        fallback_on_failure=fallback_on_failure,
        project_dir=project_dir,
        is_code_agent=is_code_agent,
        startup_mode=startup_mode,
        shared_dir=shared_dir,
    )
    if card is None or card.gateway_config is None:
        return None
    stable_id = (
        f"team_sandbox_ref_{team_id}"
        if team_id
        else f"team_sandbox_ref_{_sandbox_isolation_custom_id(project_dir, shared_dir=shared_dir, sandbox_url=sandbox_url)}"
    )
    return TeamSharedSandboxSpec(
        id=stable_id,
        mode=OperationMode.SANDBOX,
        work_config=LocalWorkConfig(shell_allowlist=None),
        gateway_config=card.gateway_config,
        team_id=str(team_id or ""),
    )


#: 成员 spec 上 team 沙箱 spec 的 id 前缀。与 :func:`create_sandbox_sysop_spec`
#: 生成的借用句柄名严格一致，是"该字段曾经被本模块写过"的唯一可判据。
_TEAM_SANDBOX_REF_ID_PREFIX = "team_sandbox_ref_"


def rebind_team_sandbox_specs(spec: Any) -> int:
    """把序列化往返中被压平的成员 sys_operation spec 换回 :class:`TeamSharedSandboxSpec`。

    **为什么需要**：``TeamAgentSpec.agents`` 是 ``dict[str, DeepAgentSpec]``，而
    ``DeepAgentSpec.sys_operation`` 的声明类型是 ``Optional[SysOperationSpec]``。
    pydantic 按**字段声明类型**序列化/反序列化，故 ``model_dump()`` 这一步就
    已经把子类压平成基类：``team_id`` 作为子类字段不进入 dump（顶层单独 dump
    ``DeepAgentSpec`` 同样丢），随后 ``model_validate()`` 只能拿到基类形状，
    ``resolve()`` 退回库实现。库实现是纯 get-or-create，会把
    ``team_sandbox_ref_<team>`` **真正注册**进 ``Runner.resource_mgr``——于是
    ① 借用句柄变成真资源，② 该注册不在 :data:`_TEAM_OWNED_SYSOP_IDS` 里，
    ``release_team_owned_sandbox`` 永远收不到（跨会话泄漏），③ 成员 dispose 照摘
    不误，容器反复重建。

    更糟的是这条路径**不报错**：成员与主 agent 同 isolation key 时，
    ``add_sys_operation`` 抛的 "already registered" 被 ``_inner_add_resource``
    吞成 ``Error``（``resource_manager.py:1449``），``SysOperationSpec.resolve()``
    不检查 Result 直接返回 ``None``，``factory.py:337`` 再兜一个 LOCAL——成员静默
    跑在宿主机上。

    ``team_id`` 从句柄 id 的后缀反解——``create_sandbox_sysop_spec`` 构造的 id 形如
    ``team_sandbox_ref_<team_id>``，team_id 本身就是运行态全名，不含前缀，故
    ``id[len(prefix):]`` 精确还原。不依赖任何被丢弃的字段。

    Args:
        spec: ``TeamAgentSpec``（或任何带 ``.agents`` 映射的对象）。就地修改。

    Returns:
        被换回子类的成员 spec 数量；0 表示无需修复或已全部是子类。
    """
    agents = getattr(spec, "agents", None)
    if not isinstance(agents, dict):
        return 0
    rebound = 0
    for key, member_spec in list(agents.items()):
        current = getattr(member_spec, "sys_operation", None)
        if current is None or isinstance(current, TeamSharedSandboxSpec):
            continue
        raw_id = str(getattr(current, "id", "") or "")
        if not raw_id.startswith(_TEAM_SANDBOX_REF_ID_PREFIX):
            continue
        restored = TeamSharedSandboxSpec(
            id=raw_id,
            mode=current.mode,
            work_config=current.work_config,
            gateway_config=current.gateway_config,
            team_id=raw_id[len(_TEAM_SANDBOX_REF_ID_PREFIX):],
        )
        # model_copy 与库的 agent_configurator（agent_configurator.py:473-480）用
        # 同一个手法：不重跑校验，故不会把 DeepAgentSpec 子类压平。
        agents[key] = member_spec.model_copy(update={"sys_operation": restored})
        rebound += 1
        logger.info(
            "[sysop_builder] team sandbox spec 已从压平的 %s 换回 TeamSharedSandboxSpec："
            "member=%s ref_id=%s",
            type(current).__name__,
            key,
            raw_id,
        )
    return rebound


#: ``TeamAgentSpec.model_validate`` 是否已被本模块包装（进程级幂等标记）。
_TEAM_SPEC_REBIND_INSTALLED = False
#: 安装互斥：adapter 构造可并发，裸标志会叠出多层包装。
_TEAM_SPEC_REBIND_LOCK = threading.Lock()


def install_team_sandbox_spec_rebind() -> None:
    """把 :func:`rebind_team_sandbox_specs` 前移到 ``TeamAgentSpec.model_validate`` 出口。

    **为什么只能挂在这里**：库的三条序列化边界
    （``from_spawn_payload`` / ``recover_from_session`` /
    ``spawn.child_process._run_spawned_agent``）都是 ``model_validate`` 与
    ``configure`` **背靠背同步**（``team_agent.py:988`` / ``:1006``），中间没有插销。
    而 ``configure`` 内 ``AgentConfigurator.setup_agent`` 立刻 ``model_copy`` 出
    ``build_spec``（``agent_configurator.py:473``）并交给 ``TeamHarness.build`` →
    ``NativeHarness.__init__`` → ``DeepAgentSpec.resolve_parts`` →
    ``sys_operation.resolve()``（``native_harness.py:170`` /
    ``deep_agent_spec.py:587``），``apply_deep_agent_parts`` 随即把**解析后的实例**
    写进 ``deep_config.sys_operation``（``factory.py:391``）。成员 dispose 读的正是
    该实例的 ``.id``（``native_harness.py`` 的 dispose），harness 后续重建也仍用同一份
    ``build_spec``——**构造之后再改 spec 对象一律无效**。

    ``model_validate`` 的返回值即 ``configure`` 的入参，是唯一既早于 configure、
    又不依赖库内部调用顺序的缝。包装它一次即覆盖全部边界；把 rebind 写在调用点
    之后必然迟到（且会打出"已换回子类"的 INFO 成功日志而实际毫无作用）。

    幂等：重复调用只生效一次；无须修复的 spec 由
    :func:`rebind_team_sandbox_specs` 返回 0，不产生任何副作用。

    Returns:
        None。仅在进程内首次调用时真正改写 ``TeamAgentSpec.model_validate``。
    """
    global _TEAM_SPEC_REBIND_INSTALLED
    with _TEAM_SPEC_REBIND_LOCK:
        if _TEAM_SPEC_REBIND_INSTALLED:
            return

        from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

        original_validate = TeamAgentSpec.model_validate

        def _validate_then_rebind(obj: Any, *args: Any, **kwargs: Any) -> Any:
            spec = original_validate(obj, *args, **kwargs)
            try:
                rebind_team_sandbox_specs(spec)
            except Exception as exc:  # noqa: BLE001 — 修补失败不得连带打挂反序列化
                logger.warning(
                    "[sysop_builder] team sandbox spec rebind after validate failed: %r", exc
                )
            return spec

        # setattr（而非直接赋值）：重绑类上的 pydantic classmethod 直接赋值是 mypy
        # [assignment] 错误，setattr 保持 lint 干净（同 mcp_call_timeout_patch 口径）。
        setattr(TeamAgentSpec, "model_validate", staticmethod(_validate_then_rebind))
        _TEAM_SPEC_REBIND_INSTALLED = True
    logger.info(
        "[sysop_builder] TeamAgentSpec.model_validate 已包装：序列化边界出口即换回 "
        "TeamSharedSandboxSpec（早于 configure/harness 构建）"
    )


def create_local_sysop_card(work_dir: str | None = None) -> SysOperationCard:
    """构造本地模式 SysOperationCard.

    Args:
        work_dir: 本地 work_dir；为空时不在 ``LocalWorkConfig`` 中显式设置。
    """
    _t0 = time.monotonic()
    work_config = (
        LocalWorkConfig(work_dir=work_dir, shell_allowlist=None)
        if work_dir
        else LocalWorkConfig(shell_allowlist=None)
    )
    logger.info(
        "[sysop_builder] local SysOperationCard created (mode=LOCAL, work_dir=%s)",
        work_dir or "<default>",
    )
    logger.info(
        "[SandboxPerf] sysop_builder.create_local_sysop_card: work_dir=%s "
        "elapsed_ms=%.1f",
        work_dir or "<default>",
        (time.monotonic() - _t0) * 1000,
    )
    return SysOperationCard(
        mode=OperationMode.LOCAL,
        work_config=work_config,
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
    shared_dir: str | Path | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Auto-configured sandbox entries that users cannot mutate via ``/sandbox``.

    Mirrors :func:`build_filesystem_policy` auto-managed mounts:

    - ``shared`` (agent root / multi-tenant workspace) → ``allow_write`` (rw)
    - ``project_dir`` (when resolved) → ``allow_write`` (rw)
    - ``config.yaml`` → ``deny_write`` (ro) only when ``startup_mode=internal``
    """
    del is_code_agent
    allow: list[dict[str, str]] = []
    deny: list[dict[str, str]] = []
    effective_startup_mode = (
        startup_mode if startup_mode is not None else get_sandbox_startup_mode()
    )

    workspace = _resolve_shared_dir(shared_dir)
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
    "TeamSharedSandboxSpec",
    "build_filesystem_policy",
    "build_process_policy",
    "build_yuanrong_sandbox_status_view",
    "create_sandbox_sysop_card",
    "create_sandbox_sysop_spec",
    "create_local_sysop_card",
    "effective_files_from_policy",
    "find_auto_managed_match",
    "find_nested_files_conflict",
    "install_team_sandbox_spec_rebind",
    "list_auto_managed_sandbox_paths",
    "list_effective_sandbox_files",
    "registered_sandbox_owner_by_key",
    "rebind_team_sandbox_specs",
    "release_team_owned_sandbox",
    "resolve_sandbox_endpoint",
    "sandbox_isolation_key_of",
    "validate_sandbox_files_runtime",
]
