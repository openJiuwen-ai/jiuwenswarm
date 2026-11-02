"""沙箱 SysOperation 创建、引用计数、rail 绑定与 runtime 热更新。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import OperationMode, SysOperation, SysOperationCard

from jiuwenswarm.common.config import (
    agent_file_read_backend_is_local,
    get_agent_file_read_backend,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
)
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _get_registered_sys_operation_by_isolation_key,
    _sys_operation_isolation_key,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.rail_factory import (
    _is_sandbox_bound_rail,
    _set_rail_sys_operation,
)
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    create_local_sysop_card,
    create_sandbox_sysop_card,
)
from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

logger = logging.getLogger(__name__)


def _create_sandbox_sys_operation(
    adapter,
    sandbox_url: str,
    sandbox_type: str,
    *,
    runtime: dict[str, Any] | None = None,
    project_dir: str | None = None,
) -> SysOperationCard | None:
    """Create a sandbox SysOperationCard.

    Delegates the actual construction to ``sysop_builder.py`` so that both
    ``interface_deep.py`` and ``interface_code.py`` share one implementation.

    历史上这是 ``@staticmethod``——但 sysop_builder 现在需要知道适配器形态
    (``is_code_agent`` 决定是否把 ``project_dir`` 挂为 rw bind), 这个信号是
    instance state (``adapter._is_code_agent``, 基类默认 False, ``JiuwenSwarm
    CodeAdapter`` 子类 override 成 True), staticmethod 拿不到。 因此必须降
    成 instance method 才能透传; 调用方相应把 ``JiuWenSwarmDeepAdapter
    ._create_sandbox_sys_operation(...)`` 改成 ``adapter._create_sandbox_
    sys_operation(...)``, 走类 MRO 让 Code 子类覆写时也能命中。

    Args:
        sandbox_url: jiuwenbox HTTP base url.
        sandbox_type: provider 名 (jiuwenbox).
        runtime: ``sandbox`` 字段字典 (含 enabled / files / excluded_commands
            / idle_ttl_seconds / idle_check_interval), 来自
            ``get_sandbox_runtime``.
        project_dir: 用户项目目录 (一般是 ``trusted_dirs[0]``); 仅在
            ``adapter._is_code_agent=True`` 时被 :func:`build_filesystem_policy`
            消费作为 rw bind mount, 否则 (deep adapter 等通用形态) 完全
            忽略——sysop_builder 不会有 cwd / env 之类的 fallback 接管。
    """
    runtime = runtime or {}
    shared_dir: str | None = None
    if is_enterprise() and adapter._workspace_dir:
        # 企业多租户：挂载当前 workspace 根，修复下载路径权限
        shared_dir = str(Path(adapter._workspace_dir).resolve().parent.parent)
    card = create_sandbox_sysop_card(
        sandbox_url,
        sandbox_type,
        files_runtime=runtime.get("files"),
        excluded_commands=runtime.get("excluded_commands"),
        idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
        idle_check_interval=runtime.get("idle_check_interval"),
        fallback_on_failure=bool(runtime.get("fallback_on_failure", False)),
        project_dir=project_dir,
        is_code_agent=adapter._is_code_agent,
        startup_mode=get_sandbox_startup_mode(),
        shared_dir=shared_dir,
    )
    return card


def _resolve_project_dir_for_sandbox(adapter) -> str | None:
    """Best-effort lookup of the user project directory for sandbox builds.

    Prefers ``adapter._project_dir``,
    then falls back to ``adapter._instance_overrides["project_dir"]`` which
    :meth:`AgentManager.get_agent` populates from ``trusted_dirs[0]``.
    Returning ``None`` lets :func:`build_filesystem_policy` use its own
    fallback chain, but the agent-server cwd usually isn't what we want
    so callers should treat ``None`` as "policy will mount cwd, which
    may shadow secrets" and at minimum log it.
    """
    direct = getattr(adapter, "_project_dir", None)
    if direct:
        return str(direct)
    overrides = getattr(adapter, "_instance_overrides", None)
    if isinstance(overrides, dict):
        value = overrides.get("project_dir")
        if value:
            return str(value)
    return None


def _create_sys_operation(adapter) -> SysOperation | None:
    """Resolve this adapter's sys operation and take a reference on it.

    Wraps :meth:`_resolve_sys_operation` with the bookkeeping that keeps
    ``Runner.resource_mgr`` from growing without bound: the resolved id is
    retained here and released in :meth:`cleanup`. The new reference is taken
    *before* the previous one is dropped, so rebuilding the agent (a skill or
    plugin install re-runs ``create_instance``) onto the same sandbox id never
    lets the refcount hit zero and unregister a resource still in use.

    Returns:
        The resolved SysOperation, or None when registration failed.
    """
    previously_retained = list(adapter._retained_sys_operation_ids)
    # 读盘用的本地 sysop 与 agent 沙箱 sysop 可能是两份：重建 agent 时不能
    # 把仍在用的 local 一并 release，否则 rail 会拿着已注销引用。
    local_keep_id: str | None = None
    if adapter._local_sys_operation is not None:
        local_keep_id = str(adapter._local_sys_operation.id)

    sys_operation = adapter._resolve_sys_operation()
    if sys_operation is not None:
        adapter._retain_sys_operation(str(sys_operation.id))
        try:
            from jiuwenswarm.server.runtime.workspace.fs_quota_guard import (
                install_write_quota_guard,
            )

            install_write_quota_guard(sys_operation)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] install write quota guard failed",
                exc_info=True,
            )

    to_release = [sid for sid in previously_retained if sid != local_keep_id]
    # agent 与 local 共用同一张卡时：新 retain 已加上，只丢掉上一轮那一票。
    same_card_as_local = (
        sys_operation is not None
        and local_keep_id is not None
        and str(sys_operation.id) == local_keep_id
    )
    prior_local_retain = (
        local_keep_id is not None
        and previously_retained.count(local_keep_id) >= 1
    )
    if same_card_as_local and prior_local_retain:
        to_release.append(local_keep_id)
    adapter._release_sys_operations(to_release)
    return sys_operation


def _retain_sys_operation(adapter, sys_operation_id: str) -> None:
    """Record one adapter-held reference on a registered sys operation.

    Args:
        sys_operation_id: Id of the sys operation this adapter now depends on.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _SYS_OPERATION_REFCOUNT_LOCK,
        _SYS_OPERATION_REFCOUNTS,
    )

    adapter._retained_sys_operation_ids.append(sys_operation_id)
    with _SYS_OPERATION_REFCOUNT_LOCK:
        _SYS_OPERATION_REFCOUNTS[sys_operation_id] = (
            _SYS_OPERATION_REFCOUNTS.get(sys_operation_id, 0) + 1
        )


def _release_sys_operations(adapter, sys_operation_ids: list[str] | None = None) -> None:
    """Drop adapter-held references and unregister the ones left unused.

    Removing a sys operation also removes the ~16 fs/shell/code tools derived
    from it (``ResourceMgr.remove_sys_operation``), which is exactly the state
    that used to survive every evicted session adapter. Failures are logged
    and swallowed: this runs on the cleanup path and must not stop it.

    Args:
        sys_operation_ids: Subset of this adapter's retained ids to release.
            Defaults to every id it still holds, which is what ``cleanup``
            wants.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _SYS_OPERATION_REFCOUNT_LOCK,
        _SYS_OPERATION_REFCOUNTS,
    )

    if sys_operation_ids is None:
        ids_to_release = list(adapter._retained_sys_operation_ids)
    else:
        ids_to_release = list(sys_operation_ids)
    if not ids_to_release:
        return

    unused_ids: list[str] = []
    unheld_ids: list[str] = []
    with _SYS_OPERATION_REFCOUNT_LOCK:
        for sys_operation_id in ids_to_release:
            if sys_operation_id in adapter._retained_sys_operation_ids:
                adapter._retained_sys_operation_ids.remove(sys_operation_id)
            held = _SYS_OPERATION_REFCOUNTS.get(sys_operation_id, 0)
            if held <= 0:
                # Releasing a reference this adapter never took. Removing the
                # resource here could pull it out from under a live holder,
                # so leave it registered and surface the bookkeeping bug.
                unheld_ids.append(sys_operation_id)
                continue
            remaining = held - 1
            if remaining > 0:
                _SYS_OPERATION_REFCOUNTS[sys_operation_id] = remaining
                continue
            _SYS_OPERATION_REFCOUNTS.pop(sys_operation_id, None)
            unused_ids.append(sys_operation_id)

    for sys_operation_id in unheld_ids:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] sys_operation release skipped, no reference held: %s",
            sys_operation_id,
        )

    for sys_operation_id in unused_ids:
        try:
            # Quiet idempotence: only remove what is still registered, so a
            # second cleanup (or one after Runner.stop cleared everything) is
            # a no-op rather than an error.
            if Runner.resource_mgr.get_sys_operation(sys_operation_id) is None:
                continue
            Runner.resource_mgr.remove_sys_operation(sys_operation_id)
            logger.info(
                "[JiuWenSwarmDeepAdapter] sys_operation released: %s",
                sys_operation_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] sys_operation release failed: id=%s error=%s",
                sys_operation_id,
                exc,
            )

    # 本地读盘 sysop 若已不在本 adapter retain 列表里，清掉缓存，避免下次
    # 误以为还能复用已注销实例。
    local = adapter._local_sys_operation
    if local is not None:
        local_id = str(getattr(local, "id", "") or "")
        if not local_id or local_id not in adapter._retained_sys_operation_ids:
            adapter._local_sys_operation = None


def _resolve_sys_operation(adapter) -> SysOperation | None:
    """Create a sys operation.

    是否走沙箱由 ``config.yaml::sandbox.enabled`` 决定（同时要求
    ``sandbox.url`` / ``sandbox.type`` 已配置）。其他 sandbox 字段
    (``excluded_commands`` / ``files`` / ``idle_ttl_seconds`` /
    ``idle_check_interval``) 透传给 ``create_sandbox_sysop_card``,
    分别写入 ``launcher_config.extra_params`` 与 ``launcher_config`` 上
    的同名字段。

    注意: 每次都从 ``get_sandbox_endpoint()`` 读最新 sandbox.url/type, 因为
    ``/sandbox enable`` 会动态写入这两个字段; yuanrong 也会填默认占位 url。

    副作用: 在 ``adapter._sys_operation_card`` 保存生成或复用的 SysOperationCard，
    供 ``apply_sandbox_runtime_patch`` 等运行时热更使用。
    """
    try:
        endpoint = get_sandbox_endpoint()
        sandbox_type = endpoint.get("type") or None
        # box-server 端口运行时分配且可变, 优先取 runner 本轮存活的 endpoint,
        # 避免陈旧 url 钉进 isolation key 与 card base_url; 端口进 key 后,
        # 端口变 → 新 key → 新 card, 不被 registry 去重复用旧端口。
        # 仅 jiuwenbox 覆盖: get_owned_endpoint 与 sandbox.type 无关, 对其它
        # type 会误用 jiuwenbox 端点污染其 isolation key。runner 未 spawn
        # (冷启动窗口) / 进程死亡 / 非本 runner 拥有时返回 None, 回落 config。
        sandbox_url = endpoint.get("url") or None
        if sandbox_type == "jiuwenbox":
            try:
                owned = JiuwenBoxRunner.instance().get_owned_endpoint()
                if owned is not None:
                    sandbox_url = f"http://{owned[0]}:{owned[1]}"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[sandbox_lifecycle] resolve live sandbox endpoint from runner "
                    "failed, fall back to config/holder url: %s", exc,
                )
        runtime = get_sandbox_runtime()
        sysop_card: SysOperationCard | None
        if runtime.get("enabled") and sandbox_url and sandbox_type:
            # 走 ``adapter.`` 而不是 ``JiuWenSwarmDeepAdapter.``——_create_sandbox_
            # sys_operation 已从 staticmethod 改成 instance method (要透传
            # ``adapter._is_code_agent``), 用类名直接调会绕过 MRO 把 Code 子类
            # 的 override (如果将来需要的话) 静默吃掉, 且 staticmethod 时代
            # 的 caller 风格不再适用。
            sysop_card = adapter._create_sandbox_sys_operation(
                sandbox_url,
                sandbox_type,
                runtime=runtime,
                project_dir=adapter._resolve_project_dir_for_sandbox(),
            )
        else:
            sysop_card = create_local_sysop_card()
        if sysop_card is None:
            logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: sysop_card is None")
            return None
        adapter._sys_operation_card = sysop_card
        isolation_key_template = _sys_operation_isolation_key(sysop_card)
        registered_sys_operation = (
            _get_registered_sys_operation_by_isolation_key(
                isolation_key_template
            )
        )
        if registered_sys_operation is not None:
            logger.info(
                "[JiuWenSwarmDeepAdapter] reuse registered sys_operation: %s",
                registered_sys_operation.id,
            )
            return registered_sys_operation

        result = Runner.resource_mgr.add_sys_operation(sysop_card)
        if result.is_err():
            registered_sys_operation = (
                _get_registered_sys_operation_by_isolation_key(
                    isolation_key_template
                )
            )
            if registered_sys_operation is not None:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] reuse registered sys_operation after add failure: %s",
                    registered_sys_operation.id,
                )
                return registered_sys_operation
            logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: %s", result.msg())
            return None
        # 仅在真正 add 成功（非复用）时打 create_sandbox UA
        if runtime.get("enabled") and sandbox_url and sandbox_type:
            from jiuwenswarm.common.audit_emit import emit_audit_ua
            from jiuwenswarm.common.audit_net import resolve_peer_ip

            emit_audit_ua(
                SUBMDL="sandbox",
                PROC="create_sandbox",
                sandbox_type=sandbox_type,
                DSTIP=resolve_peer_ip(sandbox_url),
            )
        return Runner.resource_mgr.get_sys_operation(sysop_card.id)
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: %s", exc)
        return None


def _create_local_sys_operation(adapter) -> SysOperation | None:
    """只创建/复用本地 SysOperation，给读盘类 rail 用；与沙箱实例分离。"""
    _t0 = time.monotonic()
    cached = adapter._local_sys_operation
    if cached is not None:
        cached_id = str(getattr(cached, "id", "") or "")
        still_held = bool(
            cached_id and cached_id in adapter._retained_sys_operation_ids
        )
        still_registered = bool(
            cached_id
            and Runner.resource_mgr.get_sys_operation(cached_id) is not None
        )
        if still_held and still_registered:
            logger.info(
                "[SandboxPerf] create_local_sys_operation: agent_id=%s reuse=1 "
                "elapsed_ms=%.1f",
                adapter._agent_id,
                (time.monotonic() - _t0) * 1000,
            )
            return cached
        # 上一轮 recreate 已 release/unregister：丢掉脏缓存，下面重建。
        adapter._local_sys_operation = None
    existing = adapter._sys_operation
    if existing is not None and getattr(existing, "mode", None) == OperationMode.LOCAL:
        existing_id = str(getattr(existing, "id", "") or "")
        if existing_id and existing_id not in adapter._retained_sys_operation_ids:
            adapter._retain_sys_operation(existing_id)
        adapter._local_sys_operation = existing
        logger.info(
            "[SandboxPerf] create_local_sys_operation: agent_id=%s reuse_agent=1 "
            "elapsed_ms=%.1f",
            adapter._agent_id,
            (time.monotonic() - _t0) * 1000,
        )
        return existing
    try:
        work_dir = adapter._workspace_dir or str(get_agent_root_dir())
        logger.info(
            "[JiuWenSwarmDeepAdapter] create local-only sys_operation (work_dir=%s)",
            work_dir,
        )
        sysop_card = create_local_sysop_card(work_dir=work_dir)
        if sysop_card is None:
            logger.info(
                "[SandboxPerf] create_local_sys_operation: agent_id=%s ok=0 "
                "elapsed_ms=%.1f reason=card_none",
                adapter._agent_id,
                (time.monotonic() - _t0) * 1000,
            )
            return None
        isolation_key = _sys_operation_isolation_key(sysop_card)
        if isolation_key:
            registered = _get_registered_sys_operation_by_isolation_key(
                isolation_key
            )
            if registered is not None:
                adapter._retain_sys_operation(str(registered.id))
                adapter._local_sys_operation = registered
                logger.info(
                    "[SandboxPerf] create_local_sys_operation: agent_id=%s ok=1 "
                    "reuse_registered=1 elapsed_ms=%.1f",
                    adapter._agent_id,
                    (time.monotonic() - _t0) * 1000,
                )
                return registered
        result = Runner.resource_mgr.add_sys_operation(sysop_card)
        if result.is_err():
            registered = (
                _get_registered_sys_operation_by_isolation_key(isolation_key)
                if isolation_key
                else None
            )
            if registered is not None:
                adapter._retain_sys_operation(str(registered.id))
                adapter._local_sys_operation = registered
                return registered
            logger.warning(
                "[JiuWenSwarmDeepAdapter] add local sys_operation failed: %s",
                result.msg(),
            )
            logger.info(
                "[SandboxPerf] create_local_sys_operation: agent_id=%s ok=0 "
                "elapsed_ms=%.1f reason=add_err",
                adapter._agent_id,
                (time.monotonic() - _t0) * 1000,
            )
            return None
        sysop_obj = Runner.resource_mgr.get_sys_operation(sysop_card.id)
        if sysop_obj is not None:
            adapter._retain_sys_operation(str(sysop_obj.id))
        adapter._local_sys_operation = sysop_obj
        if sysop_obj is not None:
            try:
                from jiuwenswarm.server.runtime.workspace.fs_quota_guard import (
                    install_write_quota_guard,
                )

                install_write_quota_guard(sysop_obj)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] install local write quota guard failed",
                    exc_info=True,
                )
        logger.info(
            "[SandboxPerf] create_local_sys_operation: agent_id=%s ok=%s "
            "elapsed_ms=%.1f",
            adapter._agent_id,
            1 if sysop_obj is not None else 0,
            (time.monotonic() - _t0) * 1000,
        )
        return sysop_obj
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] create local sys_operation failed: %s", exc
        )
        logger.info(
            "[SandboxPerf] create_local_sys_operation: agent_id=%s ok=0 "
            "elapsed_ms=%.1f reason=exc",
            adapter._agent_id,
            (time.monotonic() - _t0) * 1000,
        )
        return None


def _iter_adapter_rails(adapter) -> list[Any]:
    """收集 adapter 上持有的 rail 实例。"""
    rail_attrs = (
        "_filesystem_rail",
        "_skill_rail",
        "_stream_event_rail",
        "_task_execution_rail",
        "_task_planning_rail",
        "_context_assemble_rail",
        "_context_processor_rail",
        "_runtime_prompt_rail",
        "_response_prompt_rail",
        "_skill_protocol_prompt_rail",
        "_security_rail",
        "_memory_rail",
        "_external_memory_rail",
        "_heartbeat_rail",
        "_skill_evolution_rail",
        "_subagent_rail",
        "_disabled_tools_rail",
        "_permission_rail",
        "_avatar_rail",
        "_progressive_tool_rail",
        "_skill_authorization_rail",
        "_skill_active_state_rail",
        "_skill_sleep_rail",
        "_skill_credential_injection_rail",
        "_llm_retry_rail",
        "_skill_create_rail",
        "_ask_user_rail",
    )
    rails: list[Any] = []
    for attr in rail_attrs:
        rail = getattr(adapter, attr, None)
        if rail is not None:
            rails.append(rail)
    return rails


def _apply_local_sysop_to_all_rails(adapter) -> None:
    """按 ``AGENT_FILE_READ_BACKEND`` 把读文件类 rail 切到本地 sysop。"""
    _t0 = time.monotonic()
    backend = get_agent_file_read_backend()
    if not agent_file_read_backend_is_local():
        logger.info(
            "[JiuWenSwarmDeepAdapter] AGENT_FILE_READ_BACKEND=%s, skip local "
            "sysop rail override (agent_id=%s)",
            backend,
            adapter._agent_id,
        )
        logger.info(
            "[SandboxPerf] apply_rail_sysop: agent_id=%s backend=%s skipped=1 "
            "elapsed_ms=%.1f",
            adapter._agent_id,
            backend,
            (time.monotonic() - _t0) * 1000,
        )
        return

    local_sysop = adapter._create_local_sys_operation()
    if local_sysop is None:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] skip applying local sysop to rails: "
            "local sysop unavailable"
        )
        logger.info(
            "[SandboxPerf] apply_rail_sysop: agent_id=%s backend=%s skipped=1 "
            "elapsed_ms=%.1f",
            adapter._agent_id,
            backend,
            (time.monotonic() - _t0) * 1000,
        )
        return

    agent_sysop = adapter._sys_operation
    if adapter._instance is not None:
        deep_cfg = getattr(adapter._instance, "deep_config", None)
        if deep_cfg is not None and agent_sysop is not None:
            deep_cfg.sys_operation = agent_sysop

    rails: list[Any] = []
    if adapter._instance is not None:
        configured = getattr(adapter._instance, "configured_rails", None)
        if callable(configured):
            try:
                rails.extend(configured() or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] configured_rails() failed: %s", exc
                )
    rails.extend(adapter._iter_adapter_rails())

    seen: set[int] = set()
    local_applied = 0
    sandbox_kept = 0
    for rail in rails:
        rid = id(rail)
        if rid in seen:
            continue
        seen.add(rid)
        if _is_sandbox_bound_rail(rail):
            if agent_sysop is not None and _set_rail_sys_operation(
                rail, agent_sysop
            ):
                sandbox_kept += 1
            continue
        if _set_rail_sys_operation(rail, local_sysop):
            local_applied += 1

    logger.info(
        "[JiuWenSwarmDeepAdapter] rail sysop: backend=%s local=%d sandbox_bound=%d "
        "(deep_config keeps agent sysop)",
        backend,
        local_applied,
        sandbox_kept,
    )
    logger.info(
        "[SandboxPerf] apply_rail_sysop: agent_id=%s backend=%s local=%d "
        "sandbox_bound=%d elapsed_ms=%.1f",
        adapter._agent_id,
        backend,
        local_applied,
        sandbox_kept,
        (time.monotonic() - _t0) * 1000,
    )


def _rebind_late_read_rails_to_local(adapter) -> None:
    """把请求期才注册的读盘 rail 从沙箱通道切回本地。

    ``register_rail`` 会用 agent 自己的 sysop（留给写文件、跑命令）覆盖 rail。
    只换 rail 自己的通道，给今日/昨日日记这类只读。记忆工具手里那份通道
    仍跟沙箱，搜索和写入不离开沙箱。
    """
    if not agent_file_read_backend_is_local():
        return
    local_sysop = adapter._create_local_sys_operation()
    if local_sysop is None:
        return
    rebound: list[str] = []
    for rail in (
        adapter._context_assemble_rail,
        adapter._memory_rail,
        adapter._external_memory_rail,
    ):
        if rail is None or _is_sandbox_bound_rail(rail):
            continue
        if not _set_rail_sys_operation(rail, local_sysop):
            continue
        rebound.append(type(rail).__name__)
    if not rebound:
        return
    logger.info(
        "[SandboxPerf] late_read_rails local: agent_id=%s rails=%s",
        adapter._agent_id,
        ",".join(rebound),
    )


async def _init_workspace_on_host(adapter) -> None:
    """在宿主机初始化工作区，避免沙箱 DirectoryBuilder 串行建目录。

    仅当 ``AGENT_FILE_READ_BACKEND=local``（默认）时执行。
    整段落盘丢 ``asyncio.to_thread``，避免堵事件循环。
    """
    _t0 = time.monotonic()
    backend = get_agent_file_read_backend()
    if not agent_file_read_backend_is_local():
        logger.info(
            "[SandboxPerf] init_workspace_on_host: agent_id=%s backend=%s "
            "skipped=1 reason=backend_sandbox elapsed_ms=%.1f",
            adapter._agent_id,
            backend,
            (time.monotonic() - _t0) * 1000,
        )
        return

    instance = adapter._instance
    if instance is None:
        return
    deep_cfg = getattr(instance, "deep_config", None)
    if deep_cfg is None:
        return
    if not getattr(deep_cfg, "auto_create_workspace", True):
        return
    workspace = getattr(deep_cfg, "workspace", None)
    if workspace is None:
        return

    root_path = getattr(workspace, "root_path", None) or adapter._workspace_dir
    if not root_path:
        return

    directories = list(getattr(workspace, "directories", None) or [])
    language = str(
        getattr(workspace, "language", None)
        or adapter._resolve_runtime_language()
        or "cn"
    )
    try:
        from jiuwenswarm.server.runtime.agent_adapter.workspace_host_init import (
            host_init_workspace_sync,
        )

        result = await asyncio.to_thread(
            host_init_workspace_sync,
            str(root_path),
            directories,
            language=language,
        )
        status = str((result or {}).get("status") or "")
        if status == "skipped_marker":
            logger.info(
                "[SandboxPerf] init_workspace_on_host: agent_id=%s backend=%s "
                "skipped=1 reason=marker_exists elapsed_ms=%.1f",
                adapter._agent_id,
                backend,
                (time.monotonic() - _t0) * 1000,
            )
            return
        if status == "quota_exceeded":
            logger.warning(
                "[JiuWenSwarmDeepAdapter] init_workspace_on_host blocked by quota: "
                "agent_id=%s path=%s",
                adapter._agent_id,
                root_path,
            )
            raise RuntimeError("WORKSPACE_QUOTA_EXCEEDED")
        logger.info(
            "[SandboxPerf] init_workspace_on_host: agent_id=%s backend=%s ok=1 "
            "mode=%s dirs=%d elapsed_ms=%.1f path=%s",
            adapter._agent_id,
            backend,
            status,
            int((result or {}).get("dirs") or len(directories)),
            (time.monotonic() - _t0) * 1000,
            root_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[JiuWenSwarmDeepAdapter] init_workspace_on_host failed: %s",
            exc,
        )
        logger.info(
            "[SandboxPerf] init_workspace_on_host: agent_id=%s backend=%s ok=0 "
            "elapsed_ms=%.1f reason=exc",
            adapter._agent_id,
            backend,
            (time.monotonic() - _t0) * 1000,
        )


async def apply_sandbox_runtime_patch(
    adapter, runtime: dict[str, Any], *, files_changed: bool
) -> None:
    """轻量级热更新沙箱 runtime 参数（无需重建 agent）.

    - 通过 mutate 已构建 SysOperationCard 的 ``launcher_config.extra_params``
      字典让 provider 下次 exec 时读到新值（provider 持 dict 引用）。
    - ``files_changed=True`` 时:
      - jiuwenbox: 调用 ``force_recreate_jiuwenbox_sandbox``
      - yuanrong: 调用 ``delete_yuanrong_sandbox``，下次 op 懒创建新实例

    Args:
        runtime: ``get_sandbox_runtime()`` 当前完整 runtime。
        files_changed: 是否触发文件 policy 变更; 仅 files.* 子命令需要 True。
    """
    card = adapter._sys_operation_card
    if card is None or card.mode != OperationMode.SANDBOX:
        logger.info(
            "[JiuWenSwarmDeepAdapter] apply_sandbox_runtime_patch skipped: "
            "no active sandbox sys_operation"
        )
        return

    launcher = card.gateway_config.launcher_config if card.gateway_config else None
    if launcher is None:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] apply_sandbox_runtime_patch: missing launcher_config"
        )
        return

    sandbox_type = str(getattr(launcher, "sandbox_type", "") or "").strip().lower()
    if sandbox_type == "yuanrong":
        logger.info(
            "[JiuWenSwarmDeepAdapter] yuanrong runtime patch "
            "(files_changed=%s; policy/exclude ignored)",
            files_changed,
        )
        if files_changed:
            try:
                from openjiuwen.extensions.sys_operation.sandbox.providers.yuanrong import (
                    build_yuanrong_shared_scope_key,
                    delete_yuanrong_sandbox,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] delete_yuanrong_sandbox import "
                    "failed: %s",
                    exc,
                )
                return
            try:
                isolation_key = _sys_operation_isolation_key(card)
                shared_key = (
                    build_yuanrong_shared_scope_key(
                        str(launcher.base_url), str(isolation_key)
                    )
                    if isolation_key
                    else None
                )
                deleted = await delete_yuanrong_sandbox(
                    shared_key=shared_key,
                    base_url=(
                        str(launcher.base_url)
                        if launcher.base_url and not shared_key
                        else None
                    ),
                    reason="files_changed",
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] yuanrong sandbox deleted for "
                    "recreate: %s",
                    deleted,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] delete_yuanrong_sandbox failed: %s",
                    exc,
                )
        return

    extra = launcher.extra_params or {}
    extra["excluded_commands"] = list(runtime.get("excluded_commands") or [])
    extra["fallback_on_failure"] = bool(runtime.get("fallback_on_failure", False))
    shared_dir: str | None = None
    if is_enterprise() and adapter._workspace_dir:
        shared_dir = str(Path(adapter._workspace_dir).resolve().parent.parent)
    new_policy, upload_list = build_filesystem_policy(
        runtime.get("files") or {},
        project_dir=adapter._resolve_project_dir_for_sandbox(),
        is_code_agent=adapter._is_code_agent,
        startup_mode=get_sandbox_startup_mode(),
        shared_dir=shared_dir,
    )
    extra["policy"] = new_policy
    # provider 侧契约: 沙箱 sysop 永远带这两个 key, mode 固定 ``mount``,
    # upload_list 当前一定是空 list。
    extra["preserve_files_upload"] = upload_list
    extra["preserve_file_sharing_mode"] = "mount"
    extra.setdefault("policy_mode", "append")
    launcher.extra_params = extra
    logger.info(
        "[JiuWenSwarmDeepAdapter] sandbox runtime patched "
        "(exclude=%d, files_changed=%s, uploads=%d)",
        len(extra["excluded_commands"]),
        files_changed,
        len(upload_list),
    )

    if files_changed:
        try:
            from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import (
                force_recreate_jiuwenbox_sandbox,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] force_recreate_jiuwenbox_sandbox import "
                "failed: %s",
                exc,
            )
            return
        try:
            new_sandbox_id = await force_recreate_jiuwenbox_sandbox(
                launcher.base_url,
                policy=new_policy,
                policy_mode=extra.get("policy_mode", "append"),
                preserve_files_upload=upload_list,
            )
            extra["sandbox_id"] = new_sandbox_id
            logger.info(
                "[JiuWenSwarmDeepAdapter] sandbox instance recreated: %s",
                new_sandbox_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] force_recreate_jiuwenbox_sandbox "
                "failed: %s",
                exc,
            )
