# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Owner-scoped 工具权限（数字分身 / DeepAgent）。

逻辑集中在 ``jiuwenswarm.agents.harness.common.rails.permissions``，与 openjiuwen 工具护栏配合使用。

使用方式：
  1. interface_deep.py 入口处调用 setup_permission_context(request) 设置 ContextVar
  2. AvatarPromptRail 与 PermissionInterruptRail 场景钩子中使用 check_avatar_permission()
  3. finally 中调用 cleanup_permission_context(token)
"""

from __future__ import annotations

import contextvars
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
    仅从 metadata 构建 → ContextVar → 匹配。
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
    "jiuwenswarm_tool_permission_context",
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
    *,
    permission_key: str | None = None,
    owner_scopes: dict | None = None,
) -> str:
    """单工具 owner_scopes 权限检查。返回 "allow" 或 "deny"（ASK 自动降级为 DENY）。

    使用发布时保留的 owner 路由元数据，向引擎只传 ``permission_key`` 做完整判定。

    Args:
        tool_name: 工具名称
        tool_args: 工具参数
        channel_id: 频道 ID
        session_id: 会话 ID（保留兼容，未参与裁决）
        permission_key: 与 PermissionInterruptRail 同一 manager key

    Returns:
        "allow" 或 "deny"
    """
    del channel_id, session_id  # channel comes from ContextVar; session unused
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime import (
        PermissionLevel,
        check_permission_by_key,
    )

    perm_ctx = TOOL_PERMISSION_CONTEXT.get()
    if perm_ctx is None or not perm_ctx.principal_user_id:
        logger.info("[check_avatar_permission] perm_ctx is None or no principal_user_id")
        return "deny"

    if not (isinstance(permission_key, str) and permission_key.strip()):
        logger.warning("[check_avatar_permission] missing permission_key fail_closed=deny")
        return "deny"
    key = permission_key.strip()
    try:
        result = await check_permission_by_key(tool_name, tool_args, key)
    except Exception:
        logger.warning(
            "[check_avatar_permission] unregistered_key key=%s fail_closed=deny",
            key,
        )
        return "deny"
    if result is None:
        logger.warning(
            "[check_avatar_permission] unregistered_key key=%s fail_closed=deny",
            key,
        )
        return "deny"
    logger.info(
        "[check_avatar_permission] tool=%s channel=%s user=%s key=%s owner_scopes_type=%s owner_scopes_keys=%s",
        tool_name, perm_ctx.channel_id, perm_ctx.principal_user_id, key,
        type(owner_scopes).__name__, list(owner_scopes.keys()) if isinstance(owner_scopes, dict) else None
    )
    if not isinstance(owner_scopes, dict) or not owner_scopes:
        return "allow" if result.permission == PermissionLevel.ALLOW else "deny"

    cid, uid = perm_ctx.channel_id.strip(), perm_ctx.principal_user_id.strip()
    scope_cfg = (owner_scopes.get(cid) or {}).get(uid)
    logger.info(
        "[check_avatar_permission] lookup: cid=%s uid=%s scope_cfg=%s",
        cid, uid, scope_cfg is not None
    )
    level = _resolve_owner_scope_level(scope_cfg, tool_name, tool_args)
    logger.info("[check_avatar_permission] resolved level=%s", level)

    global_level_value = result.permission.value

    if level is None:
        if global_level_value == PermissionLevel.ALLOW.value:
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
    """简化的参数模式匹配（复用 openjiuwen harness patterns）。"""
    try:
        from openjiuwen.harness.security.patterns import (
            match_command,
            match_path,
            match_wildcard as match_pattern,
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
    *,
    rail: Any | None = None,
) -> bool:
    """将规则持久化到 permissions.owner_scopes，并按 1B 刷新 rail。

    谁落盘谁更新：写盘成功后若传入 ``rail``，必须 ``update_permission_config``；
    未传 rail 时只落盘并打警告（manager 不刷新）。``config`` 为调用方持有的
    permissions 段，成功后就地同步 ``owner_scopes``。
    """
    try:
        from copy import deepcopy

        from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
            persist_permissions_mutate,
        )

        def _apply_rule(perms: dict[str, Any]) -> None:
            scopes = perms.setdefault("owner_scopes", {})
            if not isinstance(scopes, dict):
                scopes = {}
                perms["owner_scopes"] = scopes
            ch = scopes.setdefault(channel_id, {})
            if not isinstance(ch, dict):
                ch = {}
                scopes[channel_id] = ch
            user = ch.setdefault(user_id, {})
            if not isinstance(user, dict):
                user = {}
                ch[user_id] = user
            tools = user.setdefault("tools", {})
            if not isinstance(tools, dict):
                tools = {}
                user["tools"] = tools
            existing = tools.get(tool_name)
            if isinstance(existing, dict):
                existing["*"] = pattern
            else:
                tools[tool_name] = pattern

        if isinstance(config, dict):
            _apply_rule(config)

        with _persist_lock:
            persisted = persist_permissions_mutate(
                _apply_rule,
                persist_scope="base",
                source="persist_to_owner_scope",
            )

        if isinstance(config, dict) and isinstance(persisted, dict):
            config["owner_scopes"] = deepcopy(persisted.get("owner_scopes") or {})

        if rail is not None:
            from jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime import (
                update_permission_config,
            )

            update_permission_config(rail, persisted)
        else:
            logger.warning(
                "persist_to_owner_scope ok but rail missing; manager not refreshed"
            )
        return True
    except Exception as e:
        logger.warning("persist_to_owner_scope failed: %s", e)
        return False
