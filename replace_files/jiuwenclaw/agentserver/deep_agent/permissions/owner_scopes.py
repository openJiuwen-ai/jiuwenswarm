# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Owner-scoped 工具权限模块 — DeepAgent 链路专用.

独立封装，不修改已有 checker.py / core.py / patterns.py。
与 React 链路的 owner_scopes.py 保持功能一致，但 import 路径指向 deep_agent/permissions。

使用方式：
  1. interface_deep.py 入口处调用 setup_permission_context(request) 设置 ContextVar
  2. AvatarPromptRail 和 PermissionInterruptRail 中使用 check_avatar_permission()
  3. finally 中调用 cleanup_permission_context(token)
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_persist_lock = threading.Lock()


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
    enable_memory: bool = True
    avatar_principal_name: str = ""
    avatar_mode: bool = False  # 用于判断是否为群聊消息，与 React 模式的 is_group_chat 对应

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


TOOL_PERMISSION_CONTEXT: contextvars.ContextVar[PermissionContext | None] = contextvars.ContextVar(
    "jiuwenclaw_tool_permission_context",
    default=None,
)


def setup_permission_context(request: Any) -> contextvars.Token | None:
    """从 request.metadata 构造 PermissionContext 并设置 ContextVar。

    metadata 无 avatar_mode 时：
    - 如果 enable_memory=False，仍需设置上下文（用于禁用记忆）
    - 否则返回 None（原有行为不受影响）
    """
    meta = getattr(request, "metadata", None) or {}
    avatar_mode = bool(meta.get("avatar_mode", False))
    if not avatar_mode:
        if meta.get("enable_memory") is False:
            perm_ctx = PermissionContext(
                channel_id=getattr(request, "channel_id", "") or "",
                enable_memory=False,
                avatar_mode=False,
            )
            return TOOL_PERMISSION_CONTEXT.set(perm_ctx)
        return None
    perm_ctx = PermissionContext(
        channel_id=getattr(request, "channel_id", "") or "",
        group_digital_avatar=bool(meta.get("group_digital_avatar")),
        principal_user_id=str(meta.get("principal_user_id", "")),
        triggering_user_id=str(meta.get("triggering_user_id", "")),
        enable_memory=meta.get("enable_memory", True),
        avatar_principal_name=str(meta.get("avatar_principal_name", "")),
        avatar_mode=avatar_mode,
    )
    return TOOL_PERMISSION_CONTEXT.set(perm_ctx)


def cleanup_permission_context(token: contextvars.Token | None) -> None:
    if token is not None:
        TOOL_PERMISSION_CONTEXT.reset(token)


async def check_avatar_permission(
    tool_name: str,
    tool_args: dict,
    channel_id: str,
    session_id: str | None,
) -> str:
    """单工具 owner_scopes 权限检查。返回 "allow" 或 "deny"（ASK 自动降级为 DENY）。

    Args:
        tool_name: 工具名称
        tool_args: 工具参数
        channel_id: 频道 ID
        session_id: 会话 ID

    Returns:
        "allow" 或 "deny"
    """
    from jiuwenclaw.agentserver.permissions.core import get_permission_engine
    from jiuwenclaw.agentserver.permissions.models import PermissionLevel

    perm_ctx = TOOL_PERMISSION_CONTEXT.get()
    if perm_ctx is None or not perm_ctx.principal_user_id:
        logger.info("[check_avatar_permission] perm_ctx is None or no principal_user_id")
        return "deny"

    engine = get_permission_engine()
    owner_scopes = engine.config.get("owner_scopes")
    logger.info(
        "[check_avatar_permission] tool=%s channel=%s user=%s owner_scopes_type=%s owner_scopes_keys=%s",
        tool_name, perm_ctx.channel_id, perm_ctx.principal_user_id,
        type(owner_scopes).__name__, list(owner_scopes.keys()) if isinstance(owner_scopes, dict) else None
    )
    if not isinstance(owner_scopes, dict) or not owner_scopes:
        logger.info("[check_avatar_permission] owner_scopes is empty or not dict")
        return "allow"

    cid, uid = perm_ctx.channel_id.strip(), perm_ctx.principal_user_id.strip()
    scope_cfg = (owner_scopes.get(cid) or {}).get(uid)
    logger.info(
        "[check_avatar_permission] lookup: cid=%s uid=%s scope_cfg=%s",
        cid, uid, scope_cfg is not None
    )
    level = _resolve_owner_scope_level(scope_cfg, tool_name, tool_args)
    logger.info("[check_avatar_permission] resolved level=%s", level)

    try:
        global_level, _rule = engine.evaluate_global_policy_directly(
            tool_name,
            tool_args,
            channel_id,
            include_external_directory=True,
        )
        global_level_value = global_level.value if global_level is not None else None
    except Exception as exc:
        logger.warning("[check_avatar_permission] evaluate_global_policy_directly failed: %s", exc)
        global_level_value = None

    if level is None:
        if global_level_value == "allow":
            return "allow"
        return "deny"

    _severity = {"allow": 0, "ask": 1, "deny": 2}
    final_level = level
    if global_level_value is not None and _severity.get(global_level_value, 2) > _severity.get(level, 2):
        final_level = global_level_value

    if final_level == "allow":
        return "allow"
    return "deny"


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

    if tool_name in tools_cfg:
        tool_entry = tools_cfg[tool_name]
        if isinstance(tool_entry, str):
            return tool_entry
        if isinstance(tool_entry, dict):
            patterns = tool_entry.get("patterns", {})
            if isinstance(patterns, dict):
                for pattern, perm in patterns.items():
                    if _match_args(pattern, tool_args):
                        return perm
            if "*" in tool_entry:
                return tool_entry["*"]

    defaults_cfg = scope_cfg.get("defaults", {})
    if "*" in defaults_cfg:
        return defaults_cfg["*"]

    return None


def _match_args(pattern: str, tool_args: dict[str, Any]) -> bool:
    """简化的参数模式匹配（复用 patterns 模块）。"""
    try:
        from jiuwenclaw.agentserver.permissions.patterns import (
            match_command,
            match_path,
            match_pattern,
            match_url,
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


def persist_to_owner_scope(
    tool_name: str,
    pattern: str,
    channel_id: str,
    user_id: str,
    config: dict,
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
        logger.warning("persist_to_owner_scope failed: %s", e)
