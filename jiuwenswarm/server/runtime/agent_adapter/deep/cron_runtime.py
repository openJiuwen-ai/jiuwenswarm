"""Cron 工具注册与运行时上下文、审批续跑恢复。"""

from __future__ import annotations

import logging
import os
from typing import Any

from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.gateway.cron import CronTargetChannel
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _cron_execution_checkpointer,
    _cron_session_has_context,
)
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey

logger = logging.getLogger(__name__)

_CRON_TOOL_NAMES = frozenset(
    {
        "cron",
        "cron_list_jobs",
        "cron_get_job",
        "cron_create_job",
        "cron_update_job",
        "cron_delete_job",
        "cron_toggle_job",
        "cron_preview_job",
    }
)


def _build_cron_tools(adapter) -> list[Any]:
    """Build cron tools from the shared runtime bridge."""
    return adapter._cron_runtime.build_tools(
        context=adapter._runtime_cron_tool_context,
        agent_id=adapter._tool_owner_id(),
        language=adapter._resolve_runtime_language(),
        service_id=getattr(adapter, "_env_service_id", None),
        tenant_agent_id=getattr(adapter, "_env_agent_id", None),
    )


def _bind_runtime_cron_context(
    adapter,
    *,
    channel_id: str | None,
    session_id: str | None,
    metadata: dict[str, Any] | None,
    request_id: str | None,
    mode: str | None,
    workspace_dir: str | None = None,
    project_dir: str | None = None,
    params: dict[str, Any] | None = None,
) -> _RuntimeCronContextTokens:
    from openjiuwen.core.sys_operation.shell_process_registry import (
        set_shell_session_id,
    )
    from jiuwenswarm.common.request_identity import (
        apply_routing_metadata,
        web_routing_identity,
    )
    from jiuwenswarm.gateway.cron.enterprise_gate import extract_routing_triple
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    _CRON_TOOL_BOUND = deep_mod._CRON_TOOL_BOUND
    _CRON_TOOL_CHANNEL_ID = deep_mod._CRON_TOOL_CHANNEL_ID
    _CRON_TOOL_METADATA = deep_mod._CRON_TOOL_METADATA
    _CRON_TOOL_MODE = deep_mod._CRON_TOOL_MODE
    _CRON_TOOL_SESSION_ID = deep_mod._CRON_TOOL_SESSION_ID
    _DeepResearchRouteContextToken = deep_mod._DeepResearchRouteContextToken
    _RUNTIME_TOOL_A2A_POLICY_ID = deep_mod._RUNTIME_TOOL_A2A_POLICY_ID
    _RUNTIME_TOOL_RESOURCE_ID = deep_mod._RUNTIME_TOOL_RESOURCE_ID
    _RuntimeCronContextTokens = deep_mod._RuntimeCronContextTokens

    normalized_channel = str(channel_id or "").strip() or CronTargetChannel.WEB.value
    normalized_mode = str(mode).strip() if isinstance(mode, str) and mode.strip() else None
    normalized_metadata = dict(metadata) if isinstance(metadata, dict) else None
    if normalized_metadata is None:
        normalized_metadata = {}
    if isinstance(request_id, str) and request_id.strip():
        normalized_metadata["request_id"] = request_id.strip()
    # 权威：顶层 user_id + metadata.routing；params 仅作本地 cron 已 merge 的补充。
    g, b, u = extract_routing_triple(normalized_metadata, params)
    routing = web_routing_identity(normalized_metadata)
    if g:
        routing["group_id"] = g
    if b:
        routing["bot_id"] = b
    if u:
        routing["user_id"] = u
    normalized_metadata = apply_routing_metadata(normalized_metadata, routing)

    session_metadata: dict[str, Any] = {}
    if isinstance(session_id, str) and session_id.strip():
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            loaded_metadata = get_session_metadata(session_id.strip(), cache_bust=True)
            if isinstance(loaded_metadata, dict):
                session_metadata = loaded_metadata
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] failed to load session metadata for cron context: %s",
                exc,
            )
    # 注入 project_dir 供 cron tool 路由解析任务归属项目（设计文档 §5.1）
    if isinstance(project_dir, str) and project_dir.strip():
        normalized_metadata.setdefault("project_dir", project_dir.strip())
    for key in ("project_id", "project_dir", "work_mode"):
        value = normalized_metadata.get(key)
        if isinstance(value, str) and value.strip():
            continue
        session_value = session_metadata.get(key)
        if isinstance(session_value, str) and session_value.strip():
            normalized_metadata[key] = session_value.strip()

    channel_token = _CRON_TOOL_CHANNEL_ID.set(normalized_channel)
    session_token = _CRON_TOOL_SESSION_ID.set(session_id)
    metadata_token = _CRON_TOOL_METADATA.set(normalized_metadata)
    mode_token = _CRON_TOOL_MODE.set(normalized_mode)
    bound_token = _CRON_TOOL_BOUND.set(True)
    resource_id = str(routing.get("bot_id") or "").strip()
    policy_id = ""
    enterprise_config = getattr(adapter, "_enterprise_config", None)
    if (
        enterprise_config is not None
        and str(getattr(enterprise_config, "resource_id", "") or "").strip()
        == resource_id
    ):
        policy_refs = getattr(enterprise_config, "template_ref", {}).get(
            "a2a_access_policy", []
        )
        if isinstance(policy_refs, list) and len(policy_refs) == 1:
            policy_id = str(policy_refs[0] or "").strip()
    resource_token = _RUNTIME_TOOL_RESOURCE_ID.set(resource_id)
    policy_token = _RUNTIME_TOOL_A2A_POLICY_ID.set(policy_id)
    shell_token = set_shell_session_id(session_id)

    # 绑定 send_file 专用路由 ContextVar（与 skill_turbo / test 仓对齐）。
    # 默认值为 None，不会像 _CRON_TOOL_CHANNEL_ID 那样在 reset 后回落成 web。
    send_file_token: Token | None = None
    try:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            set_send_file_request_context,
        )

        send_file_token = set_send_file_request_context(
            request_id=(
                request_id.strip()
                if isinstance(request_id, str) and request_id.strip()
                else None
            ),
            session_id=session_id,
            channel_id=normalized_channel,
            metadata=normalized_metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[JiuWenSwarmDeepAdapter] set send_file request context failed: %s",
            exc,
        )

    scope = RuntimeScopeKey.from_adapter(adapter, session_id=session_id)
    request_workspace = workspace_dir or project_dir or normalized_metadata.get(
        "project_dir"
    )
    try:
        deepresearch_token = deep_mod.push_deepresearch_route(
            request_id=request_id or "",
            channel_id=normalized_channel,
            session_id=scope.session_id,
            service_id=scope.service_id,
            agent_id=scope.agent_id,
            workspace_key=scope.workspace_key,
            output_dir=deep_mod._deepresearch_artifact_output_dir(
                adapter,
                normalized_metadata.get("project_dir"),
            ),
        )
    except BaseException:
        deep_mod._reset_runtime_cron_context(
            _RuntimeCronContextTokens(
                channel=channel_token,
                session=session_token,
                metadata=metadata_token,
                mode=mode_token,
                bound=bound_token,
                shell=shell_token,
                deepresearch=None,
                send_file=send_file_token,
                resource_id=resource_token,
                a2a_policy_id=policy_token,
            ),
            suppress_errors=True,
        )
        raise
    return _RuntimeCronContextTokens(
        channel=channel_token,
        session=session_token,
        metadata=metadata_token,
        mode=mode_token,
        bound=bound_token,
        shell=shell_token,
        deepresearch=_DeepResearchRouteContextToken(deepresearch_token),
        send_file=send_file_token,
        resource_id=resource_token,
        a2a_policy_id=policy_token,
    )


def _ensure_cron_tools_registered(adapter, session_id: str | None) -> None:
    """Register this agent's cron tools once, rebuilding only when they change.

    The tool instances carry no per-request state: their context object and
    owner id are fixed for the adapter's lifetime, and the target channel is
    read from a contextvar at call time (see ``_bind_runtime_cron_context``).
    Only the language is baked into the instances, so that is the whole
    rebuild condition. Registering them per request instead re-bound eight
    ids in the process-global resource manager every turn, each one a
    remove + add pair that logged a refresh warning.

    Args:
        session_id: Session the current turn belongs to. Heartbeat and cron
            sessions drive the scheduler themselves and get no cron tools.
    """
    if session_id is not None and session_id.startswith(("heartbeat", "cron", "diagnosis")):
        return
    if os.getenv("JIUWENCLAW_DISABLE_CRON_TOOLS") == "1":
        for existing in list(adapter._instance.ability_manager.list() or []):
            if getattr(existing, "name", "") in _CRON_TOOL_NAMES:
                adapter._instance.ability_manager.remove(existing.name)
        adapter._cron_tools_registered_language = None
        logger.info(
            "[JiuWenSwarmDeepAdapter] skip cron tool registration: disabled by env"
        )
        return
    language = adapter._resolve_runtime_language()
    registered_names = {
        getattr(existing, "name", "")
        for existing in (adapter._instance.ability_manager.list() or [])
    }
    # The language fingerprint alone is not enough: rebuilding the agent (a
    # skill or plugin install re-runs ``create_instance``) hands this adapter
    # a fresh, empty AbilityManager while the fingerprint still reads as
    # registered, which would silently drop the cron tools for good.
    if adapter._cron_tools_registered_language == language and (registered_names & _CRON_TOOL_NAMES):
        return
    try:
        cron_tools = adapter._build_cron_tools()
        if not cron_tools:
            return
        for existing in list(adapter._instance.ability_manager.list() or []):
            if getattr(existing, "name", "") in _CRON_TOOL_NAMES:
                adapter._instance.ability_manager.remove(existing.name)
        for cron_tool in cron_tools:
            adapter._register_agent_owned_tool(cron_tool, adapter._tool_owner_id())
            adapter._instance.ability_manager.add(cron_tool.card)
        adapter._cron_tools_registered_language = language
        logger.info(
            "[JiuWenSwarmDeepAdapter] %d cron tools registered: language=%s",
            len(cron_tools),
            language,
        )
    except Exception as exc:
        logger.error("[JiuWenSwarmDeepAdapter] 定时工具注册失败: %s", exc)


async def _reload_cron_interrupt(adapter, cron_inner: Any, session_id: str, request_id: str) -> None:
    """审批续跑时，从执行落盘的 checkpointer 把暂停中的工具调用读回来。"""
    if adapter._read_session_interrupt_state() is not None:
        return
    candidates: list[Any] = []
    primary = _cron_execution_checkpointer(cron_inner)
    adapter_cp = adapter._get_adapter_checkpointer()
    factory_cp = CheckpointerFactory.get_checkpointer()
    for candidate in (primary, adapter_cp, factory_cp):
        if candidate is None or any(candidate is item for item in candidates):
            continue
        candidates.append(candidate)
    for checkpointer in candidates:
        try:
            await checkpointer.pre_agent_execute(cron_inner, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CRON-CTX] cron interrupt reload failed: session_id=%s "
                "error=%s request_id=%s",
                session_id, exc, request_id,
            )
            continue
        if adapter._read_session_interrupt_state() is not None:
            logger.info(
                "[CRON-CTX] cron interrupt reloaded: session_id=%s request_id=%s",
                session_id, request_id,
            )
            return


async def _recover_cron_session_context(
    adapter,
    session_id: str,
    request_id: str,
    *,
    reload_interrupt: bool = False,
) -> None:
    """cron_* 续聊 / 审批续跑：从执行时写入的 checkpointer 恢复，空上下文再回灌一次历史。"""
    if not str(session_id).startswith("cron_") or adapter._instance is None:
        return
    cron_session = getattr(adapter._instance, "_interaction_session", None)
    cron_inner = getattr(cron_session, "_inner", None) if cron_session is not None else None
    if cron_inner is None:
        return
    if not _cron_session_has_context(cron_inner):
        try:
            await _cron_execution_checkpointer(cron_inner).pre_agent_execute(
                cron_inner, None
            )
            logger.info(
                "[CRON-CTX] cron session re-recover: session_id=%s request_id=%s",
                session_id, request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CRON-CTX] cron session re-recover failed: session_id=%s "
                "error=%s request_id=%s",
                session_id, exc, request_id,
            )
    if reload_interrupt:
        await _reload_cron_interrupt(adapter, cron_inner, session_id, request_id)
    if _cron_session_has_context(cron_inner):
        return
    await _rehydrate_empty_cron_context(adapter, session_id, request_id)


async def _block_stale_cron_permission_resume(adapter, request: Any) -> bool:
    """cron_* 审批续跑：恢复后仍无 paused tool 则硬拦，避免误开新一轮工具。

    返回 True 表示调用方应拒绝本次 resume，并向用户展示
    ``_STALE_CRON_PERMISSION_TEXT``。
    """
    session_id = str(getattr(request, "session_id", "") or "")
    params = request.params if isinstance(getattr(request, "params", None), dict) else {}
    reload_interrupt = (
        session_id.startswith("cron_")
        and str(params.get("source") or "") == "permission_interrupt"
    )
    if not reload_interrupt:
        return False
    await _recover_cron_session_context(adapter, 
        session_id,
        str(getattr(request, "request_id", "") or ""),
        reload_interrupt=True,
    )
    if adapter._read_session_interrupt_state() is not None:
        return False
    logger.warning(
        "[CRON-CTX] blocking stale cron permission resume (no paused tool): "
        "session_id=%s bound_session_id=%s request_id=%s",
        session_id,
        adapter._bound_interaction_session_id(),
        getattr(request, "request_id", ""),
    )
    return True


async def _rehydrate_empty_cron_context(adapter, session_id: str, request_id: str) -> None:
    """仅 cron_*：checkpoint 仍空、内存 context 也没有消息时，用历史回灌一次。"""
    react_agent = getattr(adapter._instance, "react_agent", None)
    context_engine = getattr(react_agent, "context_engine", None) if react_agent is not None else None
    if context_engine is None:
        return
    ctx = context_engine.get_context(session_id=session_id)
    if ctx is not None:
        try:
            if list(ctx.get_messages() or []):
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            await context_engine.clear_context(session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CRON-CTX] clear empty cron context failed: session_id=%s error=%s",
                session_id, exc,
            )
            return
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        warmup_session_context,
    )

    await warmup_session_context(
        deep_agent=adapter._instance,
        session_id=session_id,
        history_before_request_id=request_id,
    )


async def _persist_cron_checkpoint(adapter, session_id: str, request_id: str) -> None:
    """非流式 cron 执行收尾补 commit：把 context_engine 内存 context 落盘到 checkpointer。

    根因：cron 走非流式 ``_inner_invoke``，其 commit 挂在 ``need_cleanup`` 下；而 adapter 在
    ``start_interaction`` 已提前绑定 session，``invoke`` 收到现成 session 致 ``need_cleanup=False``，
    commit 被跳过，导致 ``cron_*`` 在 checkpointer 恒为空、web 恢复读不到历史。此处对齐流式路径
    （``_inner_stream`` 的 ``is_agent_session`` 分支会自动 commit），显式补一次落盘。
    姿势参照流式收尾：save_contexts 之后走 ``commit()``。
    ``post_run()`` 在 ``_post_run_done`` 后直接返回，cron 非流式收尾会把落盘跳掉。
    """
    try:
        if adapter._instance is None:
            return
        react_agent = getattr(adapter._instance, "react_agent", None)
        if react_agent is None:
            return
        context_engine = react_agent.context_engine
        # 无可落盘的 context 则跳过，避免写空状态。
        _ctx = context_engine.get_context(session_id=session_id)
        if _ctx is None:
            return
        try:
            _ctx_msg_count = len(list(_ctx.get_messages() or []))
        except Exception:  # noqa: BLE001
            _ctx_msg_count = 0
        if _ctx_msg_count <= 0:
            return
        # 用运行中的 _interaction_session（而非新建 session）：
        # save_contexts 内部 _save_state_to_session 会把 context messages 存回该 session 的
        # global_state['context']，commit 再落盘 checkpointer。跨 channel 的 web adapter
        # 复用本会话时会通过上方的 re-recover 从 checkpoint 读回这些 messages。
        session = getattr(adapter._instance, "_interaction_session", None)
        if session is None:
            logger.warning(
                "[CRON-CTX] cron checkpoint skip: no active _interaction_session "
                "session_id=%s request_id=%s",
                session_id, request_id,
            )
            return
        await context_engine.save_contexts(session)
        # post_run 在 _post_run_done 后会直接返回，cron 非流式收尾必须走 commit。
        await session.commit()
        logger.info(
            "[CRON-CTX] cron checkpoint committed: session_id=%s request_id=%s "
            "msg_count=%s",
            session_id, request_id, _ctx_msg_count,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CRON-CTX] cron checkpoint commit failed: session_id=%s "
            "request_id=%s error=%s",
            session_id, request_id, exc,
        )
