# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""沙箱域 handler"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import (
    DEFAULT_SANDBOX_POLICY_FILE,
    DEFAULT_SANDBOX_STARTUP_MODE,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
    resolve_preserve_file_sharing_mode_default,
    resolve_sandbox_policy_path,
    update_sandbox_endpoint,
    update_sandbox_runtime,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    build_yuanrong_sandbox_status_view,
    effective_files_from_policy,
    find_auto_managed_match,
    find_nested_files_conflict,
    list_effective_sandbox_files,
    validate_sandbox_files_runtime,
)

logger = logging.getLogger(__name__)


_SANDBOX_FILES_PARAMS = frozenset(
    {
        "sub",
        "path",
        "session_id",
        "trusted_dirs",
        "project_dir",
        "cwd",
        "mode",  # injected by gateway for agent routing
    }
)


def _resolve_active_project_dir(
    ctx, channel_id: str, params: dict[str, Any] | None = None
) -> str | None:
    """Resolve the user project dir for the current ``/sandbox`` view.

        Lookup order, falling through on empty/missing:

        1. ``params["project_dir"]`` -- stable client project identity.
        2. ``adapter._project_dir`` / ``adapter._instance_overrides``.
        3. ``params["cwd"]`` -- legacy/dynamic fallback.
        4. ``params["trusted_dirs"][0]`` -- final compatibility fallback.

        Returns ``None`` only when none of the above yield a usable path; we
        deliberately do NOT fall back to ``Path.cwd()`` of the agent-server
        process because that's typically ``~/.jiuwenswarm`` and would
        mislabel the displayed ``files.allow_write`` entry.
        """
    if isinstance(params, dict):
        project_dir = params.get("project_dir")
        if isinstance(project_dir, str) and project_dir.strip():
            return project_dir.strip()
    try:
        agent = ctx.services.agent_manager.get_agent_nowait(channel_id)
    except Exception as exc:
        logger.info("[command.sandbox] get_agent_nowait failed: %s", exc)
        return None
    adapter = ctx.services.resolve_adapter(agent)
    if adapter is None:
        return None
    direct = getattr(adapter, "_project_dir", None)
    if direct:
        return str(direct)
    overrides = getattr(adapter, "_instance_overrides", None)
    if isinstance(overrides, dict):
        value = overrides.get("project_dir")
        if value:
            return str(value)
    if isinstance(params, dict):
        cwd_value = params.get("cwd")
        if isinstance(cwd_value, str) and cwd_value.strip():
            return cwd_value.strip()
        trusted_dirs = params.get("trusted_dirs")
        if isinstance(trusted_dirs, (list, tuple)) and trusted_dirs:
            first = str(trusted_dirs[0]).strip()
            if first:
                return first
    return None


def _resolve_active_is_code_agent(ctx, channel_id: str) -> bool:
    """Look up whether ``channel_id``'s adapter is the code-agent flavor.

        Mirrors :meth:`_resolve_active_project_dir`'s adapter lookup so the
        three sandbox call sites (``_dry_run_files_policy``,
        ``_handle_sandbox_files_set`` / ``_remove``'s ``find_auto_managed_
        match``, ``_attach_effective_sandbox_files``'s
        ``list_effective_sandbox_files``) all hand the same flag into
        ``sysop_builder``. Without this, the dry-run / display side would
        always assume non-code-agent and mismatch the actual mount layout
        a Code adapter produces at sandbox-start time (project_dir vs
        ``get_agent_workspace_dir``).

        Returns ``False`` on any failure path (no agent, no adapter, attr
        absent) — that matches the base class default and keeps the dry-run
        / display strictly aligned with what :class:`JiuWenSwarmDeepAdapter`
        emits when ``_is_code_agent`` was never set.
        """
    try:
        agent = ctx.services.agent_manager.get_agent_nowait(channel_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[command.sandbox] is_code_agent lookup: get_agent_nowait failed: %s", exc)
        return False
    adapter = ctx.services.resolve_adapter(agent)
    if adapter is None:
        return False
    return bool(getattr(adapter, "_is_code_agent", False))


def allocate_internal_jiuwenbox_port(
    services,
    host: str,
    preferred_port: int,
) -> int:
    """internal 模式下确定 jiuwenbox 实际监听端口。

    - 若本 runner 已经在 ``host:preferred_port`` 上拥有一个仍在跑的 jiuwenbox,
      直接复用 (避免重复 spawn);
    - 否则若 ``preferred_port`` 当前无人占用, 用之;
    - 再否则让内核挑一个空闲端口返回。
    """
    if services.jiuwenbox_runner.is_owned_listener(host, preferred_port):
        return preferred_port
    if services.is_tcp_port_bindable(host, preferred_port):
        return preferred_port
    new_port = services.pick_free_tcp_port(host)
    logger.warning(
        "[command.sandbox] preferred port %s:%d is busy; "
        "allocating fresh port %d for new jiuwenbox instance",
        host,
        preferred_port,
        new_port,
    )
    return new_port


def _dry_run_files_policy(
    ctx,
    channel_id: str,
    params: dict[str, Any],
    files: dict[str, Any],
) -> None:
    project_dir = _resolve_active_project_dir(ctx, channel_id, params)
    is_code_agent = _resolve_active_is_code_agent(ctx, channel_id)
    try:
        build_filesystem_policy(
            files,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from exc


def _read_landlock_compatibility(policy_path: Path | None) -> str:
    if policy_path is None or not policy_path.is_file():
        return "best_effort"
    try:
        import yaml
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            landlock = data.get("landlock")
            if isinstance(landlock, dict):
                compat = landlock.get("compatibility")
                if isinstance(compat, str) and compat.strip():
                    return compat.strip()
    except Exception as exc:
        logger.debug("[command.sandbox] read landlock compatibility failed: %s", exc)
    return "best_effort"


def _effective_files_from_adapter(adapter: Any) -> dict[str, list[dict[str, str]]] | None:
    """Read effective sandbox file mounts from the adapter's active sysop card."""
    card = getattr(adapter, "_sys_operation_card", None)
    if card is None:
        return None
    gateway_config = getattr(card, "gateway_config", None)
    launcher = getattr(gateway_config, "launcher_config", None) if gateway_config else None
    extra_params = getattr(launcher, "extra_params", None) if launcher else None
    if not isinstance(extra_params, dict):
        return None
    policy = extra_params.get("policy")
    if not isinstance(policy, dict):
        return None
    return effective_files_from_policy(policy)


async def _apply_sandbox_runtime_patch(
    ctx, channel_id: str, runtime: dict[str, Any], *, files_changed: bool
) -> None:
    agent = ctx.services.agent_manager.get_agent_nowait(channel_id)
    adapter = ctx.services.resolve_adapter(agent)
    if adapter is None or not hasattr(adapter, "apply_sandbox_runtime_patch"):
        return
    try:
        await adapter.apply_sandbox_runtime_patch(runtime, files_changed=files_changed)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    except Exception as exc:
        logger.warning("[command.sandbox] apply_sandbox_runtime_patch failed: %s", exc)


def parse_sandbox_host_port(url: str) -> tuple[str, int]:
    """从 sandbox url 解析 host:port; 默认 127.0.0.1:8321."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8321
    except Exception:
        host, port = "127.0.0.1", 8321
    return host, int(port)


def _require_sandbox_supported() -> None:
    """Reject ``/sandbox`` commands on unsupported hosts.

    jiuwenbox 底层依赖平台专属隔离能力 (Linux: bwrap / Landlock / 命名空间 /
    ``PR_SET_CHILD_SUBREAPER``; Windows: jbx-sandbox 用户 + WFP + Job Object)。
    macOS 等不支持的平台在 WS 命令入口前置拒绝, 让用户看到清晰
    ``SANDBOX_BAD_REQUEST`` 错误, 而不是被 "拉起子进程失败 / 端口连接超时"
    之类的下游报错搪塞。

    Raises:
        ValueError: 当 ``sys.platform`` 不是 ``"linux"`` 或 ``"win32"`` 时。
    """
    if sys.platform not in ("linux", "win32"):
        raise ValueError(
            f"/sandbox is only supported on Linux/Windows (current platform: {sys.platform!r}); "
            "jiuwenbox depends on platform-specific isolation features and cannot "
            "run on macOS or other platforms."
        )


async def _handle_sandbox_enable(ctx, channel_id: str) -> dict[str, Any]:
    # 1. 解析 sandbox endpoint: 优先 config.yaml::sandbox.url/type, 缺省走本地 jiuwenbox.
    # ``get_sandbox_endpoint`` 已经把 startup_mode / policy_file 的归一化值一并返回:
    # - startup_mode 缺省/非法 → "internal"
    # - policy_file 缺省 → "" (此处再回落到 DEFAULT_SANDBOX_POLICY_FILE)
    endpoint = get_sandbox_endpoint()
    url = endpoint.get("url") or "http://127.0.0.1:8321"
    sandbox_type = endpoint.get("type") or "jiuwenbox"
    # startup_mode:
    # - internal: agent-server 通过 JiuwenBoxRunner 拉起 jiuwenbox (默认行为);
    # - external: 用户自己启动 jiuwenbox (例如需要 sudo + network.mode: isolated),
    #   本侧只做健康检查, 不可达直接报错并提示如何手动启动。
    startup_mode = endpoint.get("startup_mode") or DEFAULT_SANDBOX_STARTUP_MODE
    # policy_file:
    # - 仅文件名 → 在 jiuwenbox/configs 下查找; 含路径 / 绝对路径 → 整路径使用;
    # - 未配置 → 回落到 DEFAULT_SANDBOX_POLICY_FILE (即 code-agent-policy.yaml),
    #   并在下方与 url/type 一起写回 config.yaml, 让重启后无需再走 fallback 路径。
    raw_policy = endpoint.get("policy_file") or ""
    effective_policy_file = raw_policy or DEFAULT_SANDBOX_POLICY_FILE
    policy_path = resolve_sandbox_policy_path(effective_policy_file)
    if policy_path is None:
        raise RuntimeError(
            f"sandbox.policy_file={effective_policy_file!r} 无法解析: "
            f"仅给出文件名时需能定位到 jiuwenbox/configs 目录, "
            f"否则请在 config.yaml::sandbox.policy_file 里配置绝对路径。",
        )
    if not policy_path.is_file():
        raise RuntimeError(
            f"sandbox policy 文件不存在: {policy_path} "
            f"(原始配置 sandbox.policy_file="
            f"{raw_policy or f'<default:{DEFAULT_SANDBOX_POLICY_FILE}>'!r})",
        )
    # 2. 解析 host:port 并 (internal 模式下) 完成端口分配。
    # external 模式: 直接用配置里的 url, 由用户保证 jiuwenbox 监听在此处。
    # internal 模式: 期望端口被占就换一个随机空闲端口, 不去探测占用方是谁。
    host, preferred_port = parse_sandbox_host_port(url)
    if startup_mode == "internal":
        port = allocate_internal_jiuwenbox_port(ctx.services, host, preferred_port)
        if port != preferred_port:
            # 端口换过, 同步刷新 url 以便后续落盘 / 透传给前端
            url = f"http://{host}:{port}"
            logger.info(
                "[command.sandbox] jiuwenbox effective url changed to %s "
                "(preferred port %d was busy)",
                url,
                preferred_port,
            )
    else:
        port = preferred_port
    # 3. 启动 / 健康检查本地 jiuwenbox; 失败直接报错
    ok = await ctx.services.jiuwenbox_runner.ensure_running(
        host=host,
        port=port,
        startup_mode=startup_mode,
        policy_path=policy_path,
    )
    if not ok:
        if startup_mode == "external":
            raise RuntimeError(
                f"jiuwenbox 未在 {host}:{port} 监听 (sandbox.startup_mode=external); "
                f"请在另一终端先启动 jiuwenbox-server, 例如:\n"
                f"  sudo -E .venv/bin/python -m uvicorn jiuwenbox.server.app:app "
                f"--host {host} --port {port}\n"
                f"  (JIUWENBOX_POLICY_PATH={policy_path})"
            )
        stderr_tail = ctx.services.jiuwenbox_runner.get_stderr_tail(20)
        hint = "\n--- jiuwenbox stderr (tail) ---\n" + stderr_tail if stderr_tail else (
            " (no stderr captured; jiuwenbox / uvicorn 可能未安装)"
        )
        raise RuntimeError(
            f"jiuwenbox 启动或健康检查失败 ({host}:{port}){hint}"
        )
    # 4. 把 endpoint 写回 config.yaml, 保证 agent 重建 / agent-server 重启后能直接读到。
    # url 此时已是端口分配后的最终值; startup_mode / policy_file / preserve_file_sharing_mode 一并落盘。
    preserve_mode = resolve_preserve_file_sharing_mode_default()
    try:
        update_sandbox_endpoint(
            url,
            sandbox_type,
            startup_mode=startup_mode,
            policy_file=effective_policy_file,
            preserve_file_sharing_mode=preserve_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[command.sandbox] persist sandbox endpoint failed: %s", exc)
    runtime = update_sandbox_runtime({"enabled": True})
    await ctx.services.agent_manager.recreate_agent(channel_id, immediate=True)
    return {
        "runtime": runtime,
        "endpoint": {
            "url": url,
            "type": sandbox_type,
            "preserve_file_sharing_mode": preserve_mode,
            "startup_mode": startup_mode,
            "policy_file": effective_policy_file,
        },
        "jiuwenbox": {
            "host": host,
            "port": port,
            "ready": True,
            "startup_mode": startup_mode,
            "policy_path": str(policy_path),
        },
        "agent_recreated": True
    }


async def _handle_sandbox_disable(ctx, channel_id: str) -> dict[str, Any]:
    runtime = update_sandbox_runtime({"enabled": False})
    await ctx.services.agent_manager.recreate_agent(channel_id, immediate=True)
    # 记录关闭前的端点用于回执 (external 模式下 runner 没拥有进程, 会是 None)。
    owned_endpoint = ctx.services.jiuwenbox_runner.get_owned_endpoint()
    jiuwenbox_stopped = False
    if owned_endpoint is not None:
        try:
            await ctx.services.jiuwenbox_runner.stop()
            jiuwenbox_stopped = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] /sandbox disable: jiuwenbox stop failed: %s",
                exc,
            )
    else:
        logger.debug(
            "[AgentWebSocketServer] /sandbox disable: no owned jiuwenbox to stop "
            "(external startup_mode or never started)"
        )
    payload: dict[str, Any] = {
        "runtime": runtime,
        "agent_recreated": True,
        "jiuwenbox_stopped": jiuwenbox_stopped,
    }
    if owned_endpoint is not None:
        host, port = owned_endpoint
        payload["jiuwenbox"] = {"host": host, "port": port, "ready": False}
    return payload


async def _handle_sandbox_exclude_add(
    ctx, channel_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    pattern = str(params.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("pattern is required")
    current = get_sandbox_runtime()
    patterns = list(current.get("excluded_commands") or [])
    if pattern in patterns:
        raise ValueError(
            f"excluded_commands already contains {pattern!r}; "
            "use a different pattern or remove it first"
        )
    patterns.append(pattern)
    runtime = update_sandbox_runtime({"excluded_commands": patterns})
    await _apply_sandbox_runtime_patch(ctx, channel_id, runtime, files_changed=False)
    return {"runtime": runtime}


async def _handle_sandbox_exclude_remove(
    ctx, channel_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    pattern = str(params.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("pattern is required")
    current = get_sandbox_runtime()
    existing = list(current.get("excluded_commands") or [])
    if pattern not in existing:
        raise ValueError(
            f"excluded_commands does not contain {pattern!r}; "
            "nothing to remove"
        )
    patterns = [p for p in existing if p != pattern]
    runtime = update_sandbox_runtime({"excluded_commands": patterns})
    await _apply_sandbox_runtime_patch(ctx, channel_id, runtime, files_changed=False)
    return {"runtime": runtime}


async def _handle_sandbox_files_set(
    ctx, channel_id: str, params: dict[str, Any], *, bucket: str
) -> dict[str, Any]:
    _reject_extra_sandbox_files_params(params)
    path = str(params.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    # 把 path 展开成 absolute resolved 形式, 让 ``./foo`` / ``~/data`` /
    # 含 ``..`` 之类写法在入口就被归一化到稳定路径, 避免后续 stat / 入库
    # / 比较行为依赖 jiuwenswarm server 当前 cwd; 见
    # :func:`_canonicalize_sandbox_files_path` 的文档说明。
    canonical = _canonicalize_sandbox_files_path(path)
    if canonical != path:
        logger.info(
            "[sandbox] files %s: canonicalize path %r -> %r",
            bucket, path, canonical,
        )
        path = canonical
    # 拒绝把"自动配置且不可变"的路径 (intrinsic AGENT.md / HEARTBEAT.md /...
    # / daily_memory / 项目目录 / jiuwenswarm config.yaml) 再次写进
    # config.yaml::sandbox.files。 它们由 sysop_builder 在每次
    # build_filesystem_policy 时按需重建; 让用户能 add 只会污染配置, 而且
    # 若一个路径同时在 auto-allow 和用户-deny 里 (反之亦然), 实际行为难以
    # 预期, 不如直接在入口阻断。``params`` 透传给 ``_resolve_active_
    # project_dir`` 以便 TUI 通过 ``trusted_dirs`` / ``cwd`` 显式声明的
    # 项目目录也参与 auto 路径的判定。
    project_dir = _resolve_active_project_dir(ctx, channel_id, params)
    is_code_agent = _resolve_active_is_code_agent(ctx, channel_id)
    match = find_auto_managed_match(
        path,
        project_dir=project_dir,
        is_code_agent=is_code_agent,
        startup_mode=get_sandbox_startup_mode(),
    )
    if match is not None:
        matched_bucket, canonical = match
        raise ValueError(
            f"path is auto-managed (always in {matched_bucket}): {canonical}; "
            f"cannot add via /sandbox files {bucket}"
        )
    current = get_sandbox_runtime()
    files = dict(current.get("files") or {})
    files.setdefault("allow", [])
    files.setdefault("deny", [])
    # 1) 同 bucket 内已经存在等价条目 → 直接报错, 不做 "先删后加" 的隐式覆盖。
    target_list: list[Any] = list(files.get(bucket) or [])
    for existing in target_list:
        if _file_entry_matches_path(existing, path):
            raise ValueError(
                f"sandbox.files.{bucket} already contains {path!r}; "
                f"use `/sandbox files remove {path}` first if you want to change it"
            )
    # 2) 反方向 bucket 已经登记了同一条 → allow / deny 在 Landlock 层语义直接
    #    冲突, 拒绝。 用户得先把它从对侧 ``remove`` 掉再加, 显式表达 "我要
    #    切换权限方向" 的意图。
    opposite_bucket = "deny" if bucket == "allow" else "allow"
    for existing in files.get(opposite_bucket) or []:
        if _file_entry_matches_path(existing, path):
            raise ValueError(
                f"sandbox.files.{opposite_bucket} already contains {path!r}; "
                f"cannot add the same path to {bucket}. "
                f"`/sandbox files remove {path}` first if you want to flip it"
            )
    nested_error = find_nested_files_conflict(path, bucket, files)
    if nested_error is not None:
        raise ValueError(nested_error)
    entry: dict[str, Any] = {"path": path}
    target_list.append(entry)
    files[bucket] = target_list
    # 在写盘前做一次 dry-run, 防止后续 build_filesystem_policy 抛错时,
    # yaml 已经被更新成一份永远 build 不出 policy 的中间态 (见
    # :meth:`_dry_run_files_policy` 的文档说明)。
    _dry_run_files_policy(ctx, channel_id, params, files)
    runtime = update_sandbox_runtime({"files": files})
    await _apply_sandbox_runtime_patch(ctx, channel_id, runtime, files_changed=True)
    return {"runtime": runtime}


async def _handle_sandbox_files_remove(
    ctx, channel_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    _reject_extra_sandbox_files_params(params)
    path = str(params.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    # 与 _handle_sandbox_files_set 保持同一份 canonicalize, 让 ``remove
    # ./foo`` 能命中以 absolute 形式入库的 entry; 兼容旧 yaml 残留写法的
    # 兜底由 :func:`_file_entry_matches_path` 双侧 canonicalize 比较负责。
    canonical = _canonicalize_sandbox_files_path(path)
    if canonical != path:
        logger.info(
            "[sandbox] files remove: canonicalize path %r -> %r",
            path, canonical,
        )
        path = canonical
    # 同 _handle_sandbox_files_set: auto-managed 条目由 sysop_builder 在
    # 每次 build_filesystem_policy 时重建, 用户不能也不必通过 /sandbox 删除
    # 它们。如果旧版本 config.yaml 里残留了这些路径, 提示用户直接改 yaml,
    # 而不是让 /sandbox 默默地把同一个 auto-managed 名字从用户配置里抹掉
    # ——后者会让用户误以为他/她真的把 sandbox 自动条目摘掉了。
    project_dir = _resolve_active_project_dir(ctx, channel_id, params)
    is_code_agent = _resolve_active_is_code_agent(ctx, channel_id)
    match = find_auto_managed_match(
        path,
        project_dir=project_dir,
        is_code_agent=is_code_agent,
        startup_mode=get_sandbox_startup_mode(),
    )
    if match is not None:
        matched_bucket, canonical = match
        raise ValueError(
            f"path is auto-managed (always in {matched_bucket}): {canonical}; "
            f"cannot remove via /sandbox files remove"
        )
    current = get_sandbox_runtime()
    files = dict(current.get("files") or {})
    files.setdefault("allow", [])
    files.setdefault("deny", [])
    matched_buckets: list[str] = []
    for bucket in ("allow", "deny"):
        kept: list[Any] = []
        removed = False
        for entry in files.get(bucket) or []:
            if _file_entry_matches_path(entry, path):
                removed = True
                continue
            kept.append(entry)
        if removed:
            matched_buckets.append(bucket)
            files[bucket] = kept
    if not matched_buckets:
        raise ValueError(
            f"sandbox.files has no entry for {path!r}; nothing to remove"
        )
    # 与 _handle_sandbox_files_set 对齐: 在写盘前 dry-run, 避免 build 失败
    # 时 yaml 已被写成 build 不出 policy 的死局 (见 :meth:`_dry_run_files
    # _policy` 的文档说明)。
    _dry_run_files_policy(ctx, channel_id, params, files)
    runtime = update_sandbox_runtime({"files": files})
    await _apply_sandbox_runtime_patch(ctx, channel_id, runtime, files_changed=True)
    return {"runtime": runtime}


def _attach_effective_sandbox_files(
    ctx,
    payload: dict[str, Any],
    channel_id: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Inject ``effective_files`` into the ``/sandbox`` response payload.

        Prefer the filesystem policy cached on the active adapter's sysop card
        (same payload jiuwenbox uses at exec time). Fall back to a fresh build
        when no matching agent/sysop exists yet.
        """
    try:
        project_dir = _resolve_active_project_dir(ctx, channel_id, params)
        adapter = None
        try:
            agent = ctx.services.agent_manager.get_agent_nowait(
                channel_id,
                project_dir=project_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[command.sandbox] get_agent_nowait failed: %s", exc)
            agent = None
        if agent is not None:
            adapter = ctx.services.resolve_adapter(agent)
        if adapter is not None:
            adapter_project_dir = getattr(adapter, "_project_dir", None)
            if (
                project_dir
                and adapter_project_dir
                and str(adapter_project_dir) != str(project_dir)
            ):
                logger.warning(
                    "[command.sandbox] project_dir mismatch for effective_files: "
                    "client=%r adapter=%r",
                    project_dir,
                    adapter_project_dir,
                )
            cached = _effective_files_from_adapter(adapter)
            if cached is not None:
                payload["effective_files"] = cached
                return
        files_runtime: dict[str, Any] | None = None
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            rt_files = runtime.get("files")
            if isinstance(rt_files, dict):
                files_runtime = rt_files
        if files_runtime is None:
            files_in_payload = payload.get("files")
            if isinstance(files_in_payload, dict):
                files_runtime = files_in_payload
        if files_runtime is None:
            files_runtime = get_sandbox_runtime().get("files") or {}
        is_code_agent = _resolve_active_is_code_agent(ctx, channel_id)
        payload["effective_files"] = list_effective_sandbox_files(
            files_runtime,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[command.sandbox] attach effective_files failed: %s", exc)


async def _attach_landlock_status(ctx, payload: dict[str, Any]) -> None:
    """Attach jiuwenbox Landlock capability summary to sandbox responses."""
    try:
        endpoint = get_sandbox_endpoint()
        jb = payload.get("jiuwenbox")
        if isinstance(jb, dict) and jb.get("host") and jb.get("port"):
            host = str(jb["host"])
            port = int(jb["port"])
        else:
            url = endpoint.get("url") or "http://127.0.0.1:8321"
            host, port = parse_sandbox_host_port(url)
        health = await ctx.services.jiuwenbox_runner.fetch_health(host, port)
        landlock_supported = bool(health.get("landlock_supported")) if health else False
        policy_file = endpoint.get("policy_file") or DEFAULT_SANDBOX_POLICY_FILE
        policy_path = resolve_sandbox_policy_path(policy_file)
        compatibility = _read_landlock_compatibility(policy_path)
        payload["landlock"] = {
            "supported": landlock_supported,
            "compatibility": compatibility,
        }
    except Exception as exc:
        logger.warning("[command.sandbox] attach landlock status failed: %s", exc)


def _canonicalize_sandbox_files_path(path: str) -> str:
    """把 TUI 传来的 ``path`` 展开成 absolute resolved 形式 (绝对、去 ``..``、
    展开 ``~``、按需展开 symlink) 后作为 ``sandbox.files.{allow,deny}`` 的
    canonical key.

    历史上这个函数只做「按宿主文件类型自动补尾斜杠」, 因为 ``sysop_builder``
    旧版本靠尾斜杠区分文件/目录; 现在 ``build_filesystem_policy`` 已经统一
    用 ``Path.is_file()`` / ``is_dir()`` 实际 stat 磁盘判断, 尾斜杠的语义
    彻底失效, 那套补斜杠逻辑就没意义了。

    保留并扩成「绝对化 + resolve」是因为:
        - 用户在 TUI 输 ``./mydir`` / ``~/data`` / ``foo/bar`` 这类非绝对
      写法时, jiuwenswarm server 直接拿去 stat / 入库 / 比较, 行为依赖
      server 当前 cwd 与运行用户 home, 不同次重启之间会静默漂移;
    - ``_file_entry_matches_path`` 走字符串相等比较, 同一文件如果一次以
      ``~/foo`` 形式入库、下一次 ``remove /home/<user>/foo`` 就匹配不到,
      用户视角"删不掉";
    - ``sysop_builder`` 拿到非绝对路径后 ``Path(path).exists()`` 又会基于
      cwd 解析, 跟 server 视角再错位一次。

    一次 ``expanduser().resolve()`` 把所有这些不一致摊平在入口, 下游全部
    看到稳定的 absolute path。 解析失败 (例如非法字符) 时静默 fallback 到
    原字面值, 不阻塞命令; 真正"路径不存在"由 ``build_filesystem_policy``
    的 dry-run 在写盘前拦截, 见 :meth:`_dry_run_files_policy`。
    """
    if not path:
        return path
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return path


def _file_entry_matches_path(entry: Any, path: str) -> bool:
    """判断 ``sandbox.files.{allow,deny}`` 中的一项是否指向给定 ``path``.

    支持两种存储格式 (历史兼容):
    - ``dict``: ``{"path": "/foo", "permissions": "ro"}``;
    - ``str``: 直接路径字符串 ``"/foo"``。

    抽离出来主要是给 ``_handle_sandbox_files_set`` /
    ``_handle_sandbox_files_remove`` 的列表推导式简化条件 (G.EXP.04: 推导式
    不应同时使用多个子句或跨多行的复杂条件)。

    比较时两端都先 canonicalize 一次 (见 :func:`_canonicalize_sandbox_files
    _path`), 保证历史 yaml 里残留的 ``~/...`` / 相对路径 / 含 ``..`` / 含
    尾斜杠 之类写法仍能跟新 canonical 化后的输入命中, 让 ``/sandbox files
    remove`` 不会因为「字面写法不同」失效。
    """
    if isinstance(entry, dict):
        entry_path = str(entry.get("path") or "")
    elif isinstance(entry, str):
        entry_path = entry
    else:
        return False
    if entry_path == path:
        return True
    return (
        _canonicalize_sandbox_files_path(entry_path)
        == _canonicalize_sandbox_files_path(path)
    )


def _reject_extra_sandbox_files_params(params: dict[str, Any]) -> None:
    extra = set(params.keys()) - _SANDBOX_FILES_PARAMS
    if extra:
        raise ValueError(
            f"unexpected parameter(s): {', '.join(sorted(extra))}; "
            "/sandbox files allow|deny|remove accepts a single path only"
        )


async def handle_command_sandbox(ctx: RequestContext) -> None:
    """处理 ``/sandbox`` 命令.

    子命令通过 ``params["sub"]`` 路由:
    - ``status`` / ``enable`` / ``disable``
    - ``exclude.add`` / ``exclude.remove`` / ``exclude.list``
    - ``files.allow`` / ``files.deny`` / ``files.list``

    ``enable``/``disable`` 走 ``agent_manager.recreate_agent`` (重建 sys_operation 类型);
    其他写动作通过 ``adapter.apply_sandbox_runtime_patch()`` 立即热更,
    不重建 agent.

    当 ``sandbox.type=yuanrong`` 时仅允许 ``status`` (裸 ``/sandbox`` 查看
    enabled/executor/mounts); 任意子指令一律拒绝。
    """
    request = ctx.request
    params = request.params or {}
    sub = str(params.get("sub", "status")).strip().lower() or "status"
    channel_id = request.channel_id or "default"
    try:
        # 平台守卫: ``/sandbox`` 全家桶仅在 Linux 上可用。 放在 try 内部是
        # 故意的, 让 ValueError 命中下方 ``except ValueError`` 分支转成
        # ``SANDBOX_BAD_REQUEST`` 回执, 跟其它入参校验失败的处理一致。
        _require_sandbox_supported()
        endpoint = get_sandbox_endpoint()
        sandbox_type = str(endpoint.get("type") or "").strip().lower()
        if sandbox_type == "yuanrong":
            if sub != "status":
                raise ValueError(
                    "sandbox.type=yuanrong: only /sandbox (view config) is "
                    "supported; subcommands are disabled"
                )
            payload = build_yuanrong_sandbox_status_view()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        else:
            validate_sandbox_files_runtime(get_sandbox_runtime().get("files"))
            if sub == "status":
                payload = {"runtime": get_sandbox_runtime()}
            elif sub == "enable":
                payload = await _handle_sandbox_enable(ctx, channel_id)
            elif sub == "disable":
                payload = await _handle_sandbox_disable(ctx, channel_id)
            elif sub == "exclude.add":
                payload = await _handle_sandbox_exclude_add(ctx, channel_id, params)
            elif sub == "exclude.remove":
                payload = await _handle_sandbox_exclude_remove(ctx, channel_id, params)
            elif sub == "exclude.list":
                payload = {
                    "excluded_commands": list(
                        get_sandbox_runtime().get("excluded_commands") or []
                    )
                }
            elif sub == "files.allow":
                payload = await _handle_sandbox_files_set(ctx, 
                    channel_id, params, bucket="allow"
                )
            elif sub == "files.deny":
                payload = await _handle_sandbox_files_set(ctx, 
                    channel_id, params, bucket="deny"
                )
            elif sub == "files.remove":
                payload = await _handle_sandbox_files_remove(ctx, channel_id, params)
            elif sub == "files.list":
                payload = {"files": dict(get_sandbox_runtime().get("files") or {})}
            else:
                raise ValueError(f"unknown sub: {sub!r}")
            _attach_effective_sandbox_files(ctx, payload, channel_id, params)
            await _attach_landlock_status(ctx, payload)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "SANDBOX_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.sandbox failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "SANDBOX_INTERNAL"},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
