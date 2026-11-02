"""PermissionRail 构建、热更新与 Agent 级 permissions 绑定。"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
    get_base_permissions_config,
    get_effective_permissions_config,
    lookup_standard_permissions_agent_id,
    merge_session_permissions_overlay,
    resolve_permissions_body_from_enterprise,
    resolve_yaml_agent_permissions_body,
    setup_permissions_agent_base,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    PermissionRailBuildOptions,
    build_permission_rail,
)
from jiuwenswarm.common.utils import (
    collapse_nested_agent_workspace_dir,
    get_agent_workspace_dir,
)
from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)


def _bind_skill_permission_scope_resolver(adapter, permission_rail, skill_rail) -> None:
    if permission_rail is None or skill_rail is None:
        return
    setter = getattr(permission_rail, "set_skill_scope_resolver", None)
    if not callable(setter):
        return

    def resolve_scope(ctx):
        # 权限 Rail 会被子 Agent 按引用继承；仅对所属 Agent 补齐缺失上下文，
        # 子 Agent 保留自己的授权链，不能回落到主 Agent 的作用域。
        owner = getattr(adapter, "_instance", None)
        if ctx.agent is None or owner is None:
            return None
        if ctx.agent is not owner and ctx.agent is not getattr(owner, "react_agent", None):
            return None
        return skill_rail.resolve_scope(ctx)

    setter(resolve_scope)


def _update_permission_rail(
    adapter,
    config_base: dict[str, Any] | None,
    *,
    session_id: str | None = None,
) -> None:
    """原地更新已有 PermissionRail 配置，或在首次启用时新建。

    企业版优先使用 Agent template_ref.permissions 模板 body（与模型槽位同级）。
    """
    permission_config = adapter._resolve_permission_config_for_agent(
        session_id=session_id
    )
    if adapter._permission_rail is not None:
        adapter._sync_permission_workspace(adapter._permission_rail)
        adapter._permission_rail.update_config(permission_config)
        logger.info("[JiuWenSwarmDeepAdapter] _permission_rail config hot-updated")
    elif permission_config.get("enabled", False):
        adapter._permission_rail = adapter._build_permission_rail_for_agent(
            config=config_base or {},
            llm=adapter._model,
            model_name=config_base.get("models", {})
            .get("default", {})
            .get("model_client_config", {})
            .get("model_name", "gpt-4")
            if isinstance(config_base, dict)
            else "gpt-4",
        )
        if adapter._permission_rail is not None:
            logger.info("[JiuWenSwarmDeepAdapter] _permission_rail newly created on hot-reload")
            # 冷启动时权限系统未启用则授权 Rail 的 engine 为 None（fail-closed）；
            # 权限 Rail 热更新建后同步引擎，恢复 DENY 预判能力。
            # agent-core 的 SkillAuthorizationRail 未提供引擎写入口，
            # 用 setattr 做原地同步，避免重建 rail 丢失与运行中 Agent 的绑定。
            if adapter._skill_authorization_rail is not None:
                setattr(
                    adapter._skill_authorization_rail,
                    "_engine",
                    getattr(adapter._permission_rail, "_engine", None),
                )
                adapter._bind_skill_permission_scope_resolver(
                    adapter._permission_rail, adapter._skill_authorization_rail
                )


def _resolve_permission_config_for_agent(
    adapter,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """解析本 Agent 生效 permissions：企业模板 body 优先，否则 yaml/DB 回落。

    模板 body 仅作为本 Agent 基线，不写入进程级 permissions 缓存；
    企业版在提供 ``session_id`` 时仍叠加会话 overlay。
    """
    template_body = adapter._agent_permissions_body
    if template_body is None:
        template_body = resolve_permissions_body_from_enterprise(
            adapter._enterprise_config
        )
        if template_body is not None:
            adapter._agent_permissions_body = template_body
    if template_body is not None:
        if is_enterprise():
            return merge_session_permissions_overlay(
                template_body, session_id=session_id
            )
        return copy.deepcopy(template_body)
    if is_enterprise() and session_id is None:
        return get_base_permissions_config()
    return get_effective_permissions_config(session_id=session_id)


def _resolve_skill_authorization_base_config(adapter) -> dict[str, Any]:
    """Return this Agent's permission baseline without relying on ContextVar propagation.

    An empty explicit session id keeps session overlays out of the provider result;
    ``SkillAuthorizationRail`` merges the target session overlay separately.
    """
    return adapter._resolve_permission_config_for_agent(session_id="")


def _permission_workspace_dir(adapter) -> Path:
    user_ws = getattr(adapter, "_user_workspace_dir", None)
    if user_ws is not None:
        return collapse_nested_agent_workspace_dir(
            Path(user_ws) / "agent" / "jiuwenclaw_workspace"
        )
    ws = getattr(adapter, "_workspace_dir", None)
    if ws:
        return collapse_nested_agent_workspace_dir(ws)
    return collapse_nested_agent_workspace_dir(get_agent_workspace_dir())


def _sync_permission_workspace(adapter, rail: Any | None) -> None:
    """把 file_guard.workspace 绑到本 Agent 工作区，不重建。

    重建由随后的 ``update_config`` / ``set_trusted_dirs`` 完成。
    """
    if rail is None:
        return
    workspace = adapter._permission_workspace_dir()
    host = getattr(rail, "_host", None)
    if host is not None:
        host.resolve_workspace_dir = adapter._permission_workspace_dir
    engine = getattr(rail, "_engine", None)
    if engine is None:
        return
    setattr(engine, "_workspace_root", workspace)


def _build_permission_rail_for_agent(
    adapter,
    config: dict[str, Any] | None = None,
    llm: Any = None,
    model_name: str | None = None,
) -> Any | None:
    """冷启动构建 permission rail：注入 Agent 级模板 body。"""
    return build_permission_rail(
        config=config or {},
        options=PermissionRailBuildOptions(
            llm=llm if llm is not None else adapter._model,
            model_name=model_name,
            permission_config=adapter._agent_permissions_body,
            resolve_workspace_dir=adapter._permission_workspace_dir,
            persist_target_agent_id_provider=adapter._permissions_persist_target,
        ),
    )


def _permissions_persist_target(adapter) -> str | None:
    return getattr(adapter, "_permissions_persist_agent_id", None)


def _lookup_permissions_agent_id(adapter, request: Any | None = None) -> str | None:
    """标准版 permissions 查找键：request ``extract_ids`` 或 Manager ``_env_agent_id``。

    不使用仅企业版赋值的 ``adapter._agent_id``。无明确 id 时返回 ``None``（回落全局），
    避免合成 ``"default"`` 误命中 ``agents.default``。进池后若
    ``resolve_control_rpc_tenant`` 重映射，与选中的 AgentManager 使用同一 key。
    """
    extracted = None
    if request is not None:
        try:
            from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

            agent_id, _service_id, _workspace_key = TenantAgentPool.extract_ids(request)
            if agent_id:
                extracted = str(agent_id).strip() or None
        except Exception:  # noqa: BLE001
            raw = getattr(request, "agent_id", None)
            if raw is not None and str(raw).strip():
                extracted = str(raw).strip()
    env_id = getattr(adapter, "_env_agent_id", None)
    return lookup_standard_permissions_agent_id(extracted, env_id)


def _refresh_standard_agent_permissions_body(adapter, request: Any | None = None) -> None:
    """标准版：按 agent_id 绑定 yaml ``permissions.agents[id]``；未命中清空 Agent base。"""
    if is_enterprise():
        return
    agent_id = adapter._lookup_permissions_agent_id(request)
    body = resolve_yaml_agent_permissions_body(agent_id)
    if body is not None:
        adapter._agent_permissions_body = body
        adapter._permissions_persist_agent_id = agent_id
        return
    adapter._agent_permissions_body = None
    adapter._permissions_persist_agent_id = None


def _bind_agent_permissions_base(adapter) -> Any:
    """将 Agent 模板 permissions body 绑定到当前 Task（供 snapshot/生效读路径）。"""
    return setup_permissions_agent_base(adapter._agent_permissions_body)
