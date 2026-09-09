# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""子 Agent Skill 动态授权装配层。

0708 ``jiuwenclaw...tools/subagent_executor/executor.py`` 中 ForkAgentExecutor
装配逻辑（``_resolve_child_authorization_session`` / ``_build_*_rail`` /
``_build_direct_subagent_authorization_rails`` / ``_cleanup_skill_authorization_scope``）
的 dev-stable 平移。dev-stable 差异：

- dev-stable 无 ForkAgentExecutor：子代理由 agent-core ``TaskTool.invoke`` 经
  ``DeepAgent.create_subagent`` 惰性创建。装配点因此改为对父 Agent
  ``create_subagent`` 的实例级包装（``install_subagent_authorization_wiring``，
  与 ``interface_deep._bind_subagent_model_resolver`` 的实例绑定先例一致），
  不改 agent-core；
- 子 Agent scope id 取 ``create_subagent`` 的 ``subsession_id``（对齐 0708
  ``task.task_id`` 的每调用唯一语义）；仅当父 Context 为 main scope 时才装配；
- 委托审批下发复用 ``subagent_skill_authorization_rail`` 的默认 sender
  （发送时从 ``_subagent_parent_session`` ContextVar 取父 session，
  该 ContextVar 由父 Agent ``StreamEventRail.before_tool_call`` 在 task_tool
  调用期间绑定），本模块不再自行构造 ``_emit_subagent_approval`` 闭包；
- 子 Agent 的 Skill 注册表直接复用随父 rails 继承（``inherit_to_subagents``）
  进子 Agent 的 ``SkillUseRail`` 实例，不再重建 0708 的 SubagentSkillUseRail；
- 生命周期清理（``clear_scope`` + ``cancel_scope``）挂在子 Agent
  ``invoke``/``stream`` 结束的 finally 上，覆盖正常完成与异常路径。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: 父 Agent 上标记 create_subagent 已包装的实例属性（幂等）。
_AGENT_WIRING_MARKER = "_jiuwenswarm_skill_authz_wiring_installed"
#: 子 Agent 上标记已装配 scope 的实例属性（幂等）。
_CHILD_ASSEMBLED_ATTR = "_jiuwenswarm_skill_authz_scope"


PermissionConfigProvider = Callable[[], dict[str, Any]]


def skill_authorization_enabled(
    config_provider: PermissionConfigProvider | None = None,
) -> bool:
    """动态授权总开关；优先读取所属 Agent 的权限模板。"""
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
            get_effective_permissions_config,
        )
        from openjiuwen.harness.security.skill_authorization import (
            is_skill_authorization_enabled,
        )

        provider = config_provider or get_effective_permissions_config
        return is_skill_authorization_enabled(provider())
    except Exception:  # noqa: BLE001
        logger.warning(
            "[skill_authorization] subagent wiring flag read failed; preserve legacy path",
            exc_info=True,
        )
        return False


def resolve_main_authorization_session(
    config_provider: PermissionConfigProvider | None = None,
) -> str | None:
    """仅当功能开启且父 Context 为 main scope 时返回主会话 id（对齐 0708 语义）。

    子 Agent 任务可能继承父请求的授权 Context；只有 scope=="main" 的父 Context
    才允许装配委托授权链，杜绝任何形式的授权继承。
    """
    if not skill_authorization_enabled(config_provider):
        return None
    try:
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_context,
        )
        from openjiuwen.harness.rails.skills.skill_authorization_rail import (
            MAIN_AGENT_SCOPE_ID,
        )

        parent_authz = get_skill_authorization_context()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[skill_authorization] subagent wiring parent context read failed; "
            "preserve legacy path",
            exc_info=True,
        )
        return None
    if (
        parent_authz is None
        or not parent_authz.session_id
        or parent_authz.agent_scope_id != MAIN_AGENT_SCOPE_ID
    ):
        return None
    return parent_authz.session_id


def _find_child_skill_use_rail(child: Any) -> Any | None:
    """在子 Agent（含待注册）rail 列表中定位继承自父 Agent 的 SkillUseRail。"""
    try:
        from openjiuwen.harness.rails import SkillUseRail
    except Exception:  # noqa: BLE001
        logger.warning(
            "[skill_authorization] subagent wiring SkillUseRail import failed",
            exc_info=True,
        )
        return None
    finder = getattr(child, "find_rails_by_type", None)
    if callable(finder):
        try:
            found = finder((SkillUseRail,))
        except Exception:  # noqa: BLE001
            found = []
        if found:
            return found[0]
    configured = getattr(child, "configured_rails", None)
    if callable(configured):
        try:
            for rail in configured() or []:
                if isinstance(rail, SkillUseRail):
                    return rail
        except Exception:  # noqa: BLE001
            logger.warning(
                "[skill_authorization] subagent wiring rail scan failed",
                exc_info=True,
            )
    return None


def _find_child_permission_engine(child: Any) -> Any | None:
    """取子 Agent 继承的 PermissionInterruptRail 引擎（门禁 fail-closed 判定用）。"""
    try:
        from openjiuwen.harness.rails.security.tool_security_rail import (
            PermissionInterruptRail,
        )
    except Exception:  # noqa: BLE001
        return None
    finder = getattr(child, "find_rails_by_type", None)
    candidates: list[Any] = []
    if callable(finder):
        try:
            candidates = list(finder((PermissionInterruptRail,)) or [])
        except Exception:  # noqa: BLE001
            candidates = []
    if not candidates:
        configured = getattr(child, "configured_rails", None)
        if callable(configured):
            try:
                candidates = [
                    rail
                    for rail in configured() or []
                    if isinstance(rail, PermissionInterruptRail)
                ]
            except Exception:  # noqa: BLE001
                candidates = []
    for rail in candidates:
        engine = getattr(rail, "_engine", None)
        if engine is not None:
            return engine
    return None


def _child_workspace_root(child: Any) -> str | None:
    """子 Agent workspace 根（SubagentPermissionRail 裁决模板用）。"""
    deep_config = getattr(child, "deep_config", None)
    workspace = getattr(deep_config, "workspace", None)
    if workspace is None:
        return None
    root = getattr(workspace, "root_path", None)
    if root is None and isinstance(workspace, str):
        root = workspace
    root = str(root or "").strip()
    return root or None


def build_subagent_authorization_rails(
    agent_scope_id: str,
    session_id: str,
    *,
    skill_use_rail: Any | None = None,
    engine: Any | None = None,
    workspace_root: Any | None = None,
    config_provider: PermissionConfigProvider | None = None,
) -> tuple[Any, ...]:
    """装配产物：给子 Agent 追加的委托 rail 对。

    返回 ``(SubagentSkillAuthorizationRail, SubagentPermissionRail)``；
    开关关闭、参数缺失或任一 rail 构建失败时返回空元组（0708：rail 对
    不完整则整体回退 legacy 路径）。委托下发 sender 走移植版默认实现
    （``build_context_approval_sender``，发送时取父 session ContextVar）。
    """
    scope = str(agent_scope_id or "").strip()
    main_session_id = str(session_id or "").strip()
    if not scope or not main_session_id:
        return ()
    effective_config_provider = config_provider
    if effective_config_provider is None:
        from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
            get_effective_permissions_config,
        )

        effective_config_provider = get_effective_permissions_config
    if not skill_authorization_enabled(effective_config_provider):
        logger.info(
            "[skill_authorization] subagent wiring skipped scope=%s reason=disabled",
            scope,
        )
        return ()
    try:
        from openjiuwen.harness.rails.skills.skill_authorization_rail import (
            build_skill_registry_resolver,
        )
        from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
            SubagentPermissionRail,
        )
        from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
            SubagentSkillAuthorizationRail,
        )
        from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
            merge_session_permissions_overlay,
        )

        skill_resolver = None
        if skill_use_rail is not None:
            # dev-stable：SkillUseRail.skills_meta 是 property（0708 为方法）。
            skill_resolver = build_skill_registry_resolver(
                lambda: skill_use_rail.skills_meta,
                skill_dirs_provider=lambda: skill_use_rail.skills_dir,
            )
        # engine 未注入时门禁按策略评估失败处理（fail-closed），与主 Agent 一致。
        gate_rail = SubagentSkillAuthorizationRail(
            agent_scope_id=scope,
            session_id=main_session_id,
            engine=engine,
            skill_resolver=skill_resolver,
            config_provider=effective_config_provider,
            session_config_merger=merge_session_permissions_overlay,
        )
        permission_rail = SubagentPermissionRail(
            agent_scope_id=scope,
            session_id=main_session_id,
            engine=engine,
            workspace_root=workspace_root,
            config_provider=effective_config_provider,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[skill_authorization] subagent wiring rail pair build failed scope=%s",
            scope,
            exc_info=True,
        )
        return ()
    return gate_rail, permission_rail


def cleanup_skill_authorization_scope(
    agent_scope_id: str,
    session_id: str | None,
) -> None:
    """子 Agent 销毁 / 完成：清理其 ``agent_scope_id`` 下全部 Grant 与待决审批。

    best-effort 且幂等：空作用域 / 重复调用均为 no-op（对齐 0708
    ``_cleanup_skill_authorization_scope``）。
    """
    try:
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_grant_store,
            get_subagent_approval_registry,
        )

        store = get_skill_grant_store()
        if session_id:
            store.clear_scope(session_id, agent_scope_id)
            get_subagent_approval_registry().cancel_scope(session_id, agent_scope_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[skill_authorization] subagent grant cleanup failed scope=%s",
            agent_scope_id,
            exc_info=True,
        )


def _wrap_child_lifecycle(
    child: Any,
    agent_scope_id: str,
    session_id: str,
) -> None:
    """在子 Agent invoke/stream 结束（含异常）时清理授权作用域。"""
    original_invoke = getattr(child, "invoke", None)
    if callable(original_invoke):

        async def invoke_with_cleanup(*args: Any, **kwargs: Any) -> Any:
            try:
                return await original_invoke(*args, **kwargs)
            finally:
                cleanup_skill_authorization_scope(agent_scope_id, session_id)

        child.invoke = invoke_with_cleanup

    original_stream = getattr(child, "stream", None)
    if callable(original_stream):

        def stream_with_cleanup(*args: Any, **kwargs: Any) -> Any:
            async def _wrapped() -> Any:
                try:
                    async for item in original_stream(*args, **kwargs):
                        yield item
                finally:
                    cleanup_skill_authorization_scope(agent_scope_id, session_id)

            return _wrapped()

        child.stream = stream_with_cleanup


def attach_subagent_authorization(
    child: Any,
    agent_scope_id: str,
    session_id: str,
    *,
    config_provider: PermissionConfigProvider | None = None,
) -> bool:
    """把委托 rail 对挂到子 Agent 并接管其生命周期清理。

    返回是否完成装配。子 Agent 已完成初始化时挂 rail 不会生效（pending
    rails 仅在首次 ``ensure_initialized`` 时注册），此时跳过并告警。
    幂等：同一 child 同一 scope 重复装配直接返回。
    """
    scope = str(agent_scope_id or "").strip()
    main_session_id = str(session_id or "").strip()
    if child is None or not scope or not main_session_id:
        return False
    if getattr(child, _CHILD_ASSEMBLED_ATTR, None) == scope:
        return True
    if getattr(child, "_initialized", False):
        logger.warning(
            "[skill_authorization] subagent wiring skipped scope=%s "
            "reason=child_already_initialized",
            scope,
        )
        return False
    add_rail = getattr(child, "add_rail", None)
    if not callable(add_rail):
        return False
    engine = _find_child_permission_engine(child)
    rails = build_subagent_authorization_rails(
        scope,
        main_session_id,
        skill_use_rail=_find_child_skill_use_rail(child),
        engine=engine,
        workspace_root=_child_workspace_root(child),
        config_provider=config_provider,
    )
    if not rails:
        return False
    for rail in rails:
        add_rail(rail)
    _wrap_child_lifecycle(child, scope, main_session_id)
    setattr(child, _CHILD_ASSEMBLED_ATTR, scope)
    logger.info(
        "[skill_authorization] subagent authorization rails attached "
        "session=%s scope=%s engine=%s",
        main_session_id,
        scope,
        engine is not None,
    )
    return True


def install_subagent_authorization_wiring(
    agent: Any,
    *,
    config_provider: PermissionConfigProvider | None = None,
) -> bool:
    """包装父 Agent ``create_subagent``：子 Agent 创建时按需装配委托授权链。

    仅当父 Context 为 main scope（``resolve_main_authorization_session``）
    才装配，对齐 0708 语义；开关关闭或非 main scope 时零侵入。
    幂等：重复安装直接返回。返回是否完成安装。
    """
    if agent is None:
        return False
    if getattr(agent, _AGENT_WIRING_MARKER, False):
        return True
    original_create = getattr(agent, "create_subagent", None)
    if not callable(original_create):
        return False

    def create_subagent_with_authorization(
        subagent_type: str,
        subsession_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        child = original_create(subagent_type, subsession_id, *args, **kwargs)
        try:
            main_session_id = resolve_main_authorization_session(config_provider)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[skill_authorization] subagent wiring scope resolve failed",
                exc_info=True,
            )
            main_session_id = None
        if main_session_id:
            try:
                attach_subagent_authorization(
                    child,
                    str(subsession_id or ""),
                    main_session_id,
                    config_provider=config_provider,
                )
            except Exception:  # noqa: BLE001 — 装配失败不得击穿子 Agent 创建
                logger.warning(
                    "[skill_authorization] subagent wiring attach failed scope=%s",
                    subsession_id,
                    exc_info=True,
                )
        return child

    agent.create_subagent = create_subagent_with_authorization
    setattr(agent, _AGENT_WIRING_MARKER, True)
    logger.info("[skill_authorization] subagent authorization wiring installed")
    return True


__all__ = [
    "attach_subagent_authorization",
    "build_subagent_authorization_rails",
    "cleanup_skill_authorization_scope",
    "install_subagent_authorization_wiring",
    "resolve_main_authorization_session",
    "skill_authorization_enabled",
]
