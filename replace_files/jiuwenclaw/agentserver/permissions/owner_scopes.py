# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Owner-scoped 工具权限模块 — 独立封装，不修改已有 checker.py / core.py / patterns.py。

使用方式：
  1. interface_react.py 入口处调用 setup_permission_context(request) 设置 ContextVar
  2. react_agent.py 中用 check_tool_permissions_with_context() 替换原 check_tool_permissions()
  3. finally 中调用 cleanup_permission_context(token)

当 ContextVar 为 None 时，完全委托给已有 check_tool_permissions()（原有行为不变）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

logger = logging.getLogger(__name__)

_persist_lock = threading.Lock()


# ---------- PermissionContext（仅在本模块内部使用） ----------


@dataclass
class PermissionContext:
    """数字分身场景下的权限上下文。

    不放入 schema/agent.py，不序列化到 AgentRequest；
    仅在 owner_scopes.py 内部使用，从 metadata 构建 → ContextVar → 匹配。
    """
    channel_id: str = ""
    group_digital_avatar: bool = False
    principal_user_id: str = ""
    triggering_user_id: str = ""

    @property
    def scene(self) -> str:
        if self.group_digital_avatar:
            return "group_digital_avatar"
        if self.channel_id.strip() == "web":
            return "web"
        return "normal_im"

    @property
    def owner_scope_key(self) -> tuple[str, str]:
        """(channel_id, principal_user_id) — 用于 owner_scopes 配置查找."""
        return self.channel_id.strip(), self.principal_user_id.strip()


# ---------- ContextVar ----------

TOOL_PERMISSION_CONTEXT: contextvars.ContextVar[PermissionContext | None] = contextvars.ContextVar(
    "jiuwenclaw_tool_permission_context",
    default=None,
)


# ---------- setup / cleanup ----------

def setup_permission_context(request: Any) -> contextvars.Token | None:
    """从 request.metadata 构造 PermissionContext 并设置 ContextVar。

    metadata 无 avatar_mode 时返回 None（原有行为不受影响）。
    """
    meta = getattr(request, "metadata", None) or {}
    if not meta.get("avatar_mode"):
        return None
    perm_ctx = PermissionContext(
        channel_id=getattr(request, "channel_id", "") or "",
        group_digital_avatar=bool(meta.get("group_digital_avatar")),
        principal_user_id=str(meta.get("principal_user_id", "")),
        triggering_user_id=str(meta.get("triggering_user_id", "")),
    )
    return TOOL_PERMISSION_CONTEXT.set(perm_ctx)


def cleanup_permission_context(token: contextvars.Token | None) -> None:
    if token is not None:
        TOOL_PERMISSION_CONTEXT.reset(token)


# ---------- 包装后的权限检查函数 ----------

async def check_tool_permissions_with_context(
    tool_calls: List[Any],
    channel_id: str = "",
    session_id: str | None = None,
    session: Any = None,
    request_approval_callback: Callable[[Any, Any, Any], Awaitable[str]] | None = None,
) -> tuple[List[Any], List[tuple[Any, str]]]:
    """场景路由包装函数。签名与原 check_tool_permissions 完全一致。

    内部调用关系：
    1. 从 TOOL_PERMISSION_CONTEXT.get() 获取 perm_ctx
    2. perm_ctx 为 None → 委托已有 check_tool_permissions()（原有行为）
    3. scene=normal_im → 跳过检查，全部放行
    4. scene=group_digital_avatar → owner_scopes 匹配（ask→deny 降级）
    5. scene=web → 委托已有 check_tool_permissions()
    """
    from jiuwenclaw.agentserver.permissions.checker import check_tool_permissions

    perm_ctx = TOOL_PERMISSION_CONTEXT.get()

    # 无 context → 原有逻辑
    if perm_ctx is None:
        return await check_tool_permissions(
            tool_calls, channel_id=channel_id, session_id=session_id,
            session=session, request_approval_callback=request_approval_callback,
        )

    scene = perm_ctx.scene

    # normal_im → 委托原有逻辑（而非跳过权限检查）
    if scene == "normal_im":
        logger.info("[PermissionEngine] permission.owner_scope.delegate scene=normal_im")
        return await check_tool_permissions(
            tool_calls, channel_id=channel_id, session_id=session_id,
            session=session, request_approval_callback=request_approval_callback,
        )

    # web → 原有逻辑
    if scene == "web":
        return await check_tool_permissions(
            tool_calls, channel_id=channel_id, session_id=session_id,
            session=session, request_approval_callback=request_approval_callback,
        )

    # group_digital_avatar → owner_scopes 检查
    return await _check_avatar_permissions(
        tool_calls, perm_ctx, channel_id, session_id,
    )


async def _check_avatar_permissions(
    tool_calls: List[Any],
    perm_ctx: PermissionContext,
    channel_id: str,
    session_id: str | None,
) -> tuple[List[Any], List[tuple[Any, str]]]:
    """数字分身场景的权限检查：owner_scopes 与全局权限取交集（最严格者生效），ask→deny 降级。

    设计原则：
    - owner_scopes 独立于 permissions.enabled 主开关，只要有配置就生效。
    - 对每个工具同时评估 owner_scope 级别和全局 permission 级别，取最严格的结果。
      严格程度：deny > ask > allow。
    - ask 在数字分身场景一律降级为 deny（无人可审批）。
    """
    from jiuwenclaw.agentserver.permissions.core import get_permission_engine
    from jiuwenclaw.agentserver.permissions.models import PermissionLevel

    engine = get_permission_engine()
    owner_scopes = engine.config.get("owner_scopes")

    # 无 owner_scopes 配置 → 委托原有逻辑
    if not isinstance(owner_scopes, dict) or not owner_scopes:
        logger.info("[PermissionEngine] permission.owner_scope.skip scene=group_digital_avatar reason=no_owner_scopes")
        return list(tool_calls), []

    # 硬保护：avatar 模式必须有 principal_user_id
    if not perm_ctx.principal_user_id:
        logger.error(
            "[PermissionEngine] permission.owner_scope.fail_closed scene=group_digital_avatar "
            "reason=empty_principal_user_id"
        )
        deny_msg = (
            "[PERMISSION_DENIED] 数字分身未配置 my_user_id，"
            "无法确定权限 owner，所有工具调用被拒绝。"
        )
        return [], [(tc, deny_msg) for tc in tool_calls]

    deny_guidance = engine.config.get(
        "deny_guidance_message",
        "该工具未被授权使用。请在 Web 管理页面配置工具权限。",
    )

    cid, uid = perm_ctx.owner_scope_key
    scope_cfg = (owner_scopes.get(cid) or {}).get(uid)

    # 严格程度映射：数值越大越严格
    _severity = {"allow": 0, "ask": 1, "deny": 2}

    allowed: List[Any] = []
    denied: List[tuple[Any, str]] = []

    for tc in tool_calls:
        tool_name = getattr(tc, "name", "")
        tool_args = getattr(tc, "arguments", {})
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except Exception:
                tool_args = {}

        # 1. owner_scope 级别
        os_level = _resolve_owner_scope_level(scope_cfg, tool_name, tool_args)

        # 2. 全局 permission 级别（无视 enabled 开关，直接走 checker）
        global_level = await _get_global_tool_level(engine, tool_name, tool_args, channel_id, session_id)

        # 3. 取交集：两者中最严格的生效
        if os_level is None:
            # owner_scopes 无匹配 → 仅以全局权限为准
            final_level = global_level
        elif global_level is None:
            final_level = os_level
        else:
            final_level = os_level if _severity.get(os_level, 2) >= _severity.get(global_level, 2) else global_level

        logger.info(
            "[PermissionEngine] permission.owner_scope.result tool=%s owner_scope=%s global=%s "
            "final=%s channel=%s user=%s",
            tool_name,
            os_level,
            global_level,
            final_level,
            cid,
            uid,
        )

        if final_level == "allow":
            allowed.append(tc)
        elif final_level == "deny":
            denied.append((tc, f"[PERMISSION_DENIED] {deny_guidance}"))
        elif final_level == "ask":
            # ask → deny 降级
            denied.append((tc, f"[PERMISSION_DENIED] {deny_guidance}"))
            logger.warning(
                "[PermissionEngine] permission.owner_scope.ask_downgraded tool=%s scene=group_digital_avatar",
                tool_name,
            )
        else:
            denied.append((tc, f"[PERMISSION_DENIED] {deny_guidance}"))

    return allowed, denied


async def _get_global_tool_level(
    engine: Any,
    tool_name: str,
    tool_args: dict[str, Any],
    channel_id: str,
    session_id: str | None,
) -> str | None:
    """获取全局 permission 级别，无视 enabled 主开关。

    直接调用内部 checker，确保即使 permissions.enabled=false，
    已配置的工具规则仍然对 owner_scopes 交集生效。
    主开关关闭且无任何工具规则时返回 None（视为无约束）。
    """
    try:
        permission, _rule = engine.evaluate_global_policy_directly(
            tool_name,
            tool_args,
            channel_id,
            include_external_directory=True,
        )
        if permission is None:
            # 全局无匹配规则 → 无约束（不影响 owner_scope 的判定）
            return None
        return permission.value  # "allow" / "ask" / "deny"
    except Exception as exc:
        logger.warning(
            "[PermissionEngine] permission.owner_scope.global_lookup_failed tool=%s error=%s",
            tool_name,
            exc,
        )
        return None


def _resolve_owner_scope_level(
    scope_cfg: dict | None,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """在 owner-scope 层按优先级匹配，返回 "allow"/"deny"/"ask" 或 None。

    优先级（匹配到即返回，不再 fallback）：
    1. owner_scopes.<channel>.<user>.tools.<tool>.patterns
    2. owner_scopes.<channel>.<user>.tools.<tool>.* (或直接字符串)
    3. owner_scopes.<channel>.<user>.defaults.*
    """
    if not scope_cfg or not isinstance(scope_cfg, dict):
        return None

    tools_cfg = scope_cfg.get("tools", {})

    # 1&2: tool-level
    if tool_name in tools_cfg:
        tool_entry = tools_cfg[tool_name]
        if isinstance(tool_entry, str):
            return tool_entry
        if isinstance(tool_entry, dict):
            # patterns 匹配
            patterns = tool_entry.get("patterns", {})
            if isinstance(patterns, dict):
                from jiuwenclaw.agentserver.permissions.patterns import (
                    match_command, match_path, match_pattern, match_url,
                )
                for pattern, perm in patterns.items():
                    if _match_args(pattern, tool_args):
                        return perm
            # tool default (*)
            if "*" in tool_entry:
                return tool_entry["*"]

    # 3: scope defaults
    defaults_cfg = scope_cfg.get("defaults", {})
    if "*" in defaults_cfg:
        return defaults_cfg["*"]

    return None


def _match_args(pattern: str, tool_args: dict[str, Any]) -> bool:
    """简化的参数模式匹配（复用 patterns 模块）。"""
    try:
        from jiuwenclaw.agentserver.permissions.patterns import (
            match_command, match_path, match_pattern, match_url,
        )
        for key, value in tool_args.items():
            if not isinstance(value, str):
                continue
            if key in ("command", "cmd") and match_command(pattern, value):
                return True
            if key == "url" and match_url(pattern, value):
                return True
            if key in {"path", "file_path"} and match_path(pattern, value):
                return True
            if match_pattern(pattern, value):
                return True
        return False
    except Exception:
        return False


# ---------- 持久化 ----------

def persist_to_owner_scope(
    tool_name: str, pattern: str, channel_id: str, user_id: str, config: dict,
) -> None:
    """将规则持久化到 config.yaml 的 owner_scopes 节点."""
    try:
        from jiuwenclaw.config import get_config_raw, set_config

        with _persist_lock:
            raw = get_config_raw()
            perm_cfg = raw.setdefault("permissions", {})
            scopes = perm_cfg.setdefault("owner_scopes", {})
            ch = scopes.setdefault(channel_id, {})
            user = ch.setdefault(user_id, {})
            tools = user.setdefault("tools", {})
            existing = tools.get(tool_name)
            if isinstance(existing, dict):
                existing["*"] = pattern
            else:
                tools[tool_name] = pattern
            set_config(raw)
    except Exception as e:
        logger.warning("[PermissionEngine] permission.owner_scope.persist_failed error=%s", e)
