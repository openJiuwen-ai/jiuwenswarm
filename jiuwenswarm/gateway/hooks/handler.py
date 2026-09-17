# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""GatewayHookHandler —— 在 Gateway 层执行 session / 生命周期类 hooks."""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenswarm.common.hooks_config import HooksConfig, HookEvent
from jiuwenswarm.server.hooks.executor import HookExecutor

logger = logging.getLogger(__name__)


class GatewayHookHandler:
    """Gateway 层的 hooks 处理器.

    Gateway hooks 特点：
    - 同步串行执行（保证 session 生命周期顺序）
    - 超时默认 10s（短于 AgentServer 30s，避免阻塞消息转发）
    - 失败永不阻塞用户请求（只做日志记录）
    """

    def __init__(self, hooks_config: HooksConfig):
        self._config = hooks_config
        self._executor = HookExecutor()
        self._gateway_timeout = 10
        self._active_sessions: set[str] = set()

    async def on_session_start(self, session_id: str, source: str = "startup") -> None:
        """会话开始时触发 SessionStart hooks."""
        if session_id in self._active_sessions:
            return
        self._active_sessions.add(session_id)

        hook_configs = self._config.match(
            HookEvent.SESSION_START.value, query=source,
        )
        if not hook_configs:
            return

        for cfg in hook_configs:
            cfg.setdefault("timeout", self._gateway_timeout)

        try:
            await self._executor.run_all(
                hook_configs,
                hook_input={
                    "event": "SessionStart",
                    "source": source,
                    "session_id": session_id,
                    "cwd": str(Path.cwd()),
                },
            )
        except Exception as e:
            logger.warning("GatewayHookHandler: SessionStart hook failed: %s", e)

    async def on_user_prompt_submit(self, session_id: str, prompt: str) -> None:
        """用户提交消息时触发 UserPromptSubmit hooks."""
        hook_configs = self._config.match(HookEvent.USER_PROMPT_SUBMIT.value)
        if not hook_configs:
            return

        for cfg in hook_configs:
            cfg.setdefault("timeout", self._gateway_timeout)

        try:
            await self._executor.run_all(
                hook_configs,
                hook_input={
                    "event": "UserPromptSubmit",
                    "prompt": prompt,
                    "session_id": session_id,
                },
            )
        except Exception as e:
            logger.warning("GatewayHookHandler: UserPromptSubmit hook failed: %s", e)

    async def on_session_end(self, session_id: str, reason: str = "clear") -> None:
        """会话结束时触发 SessionEnd hooks."""
        self._active_sessions.discard(session_id)

        hook_configs = self._config.match(
            HookEvent.SESSION_END.value, query=reason,
        )
        if not hook_configs:
            return

        for cfg in hook_configs:
            cfg.setdefault("timeout", self._gateway_timeout)

        try:
            await self._executor.run_all(
                hook_configs,
                hook_input={
                    "event": "SessionEnd",
                    "reason": reason,
                    "session_id": session_id,
                },
            )
        except Exception as e:
            logger.warning("GatewayHookHandler: SessionEnd hook failed: %s", e)

    async def on_notification(self, notification_type: str, message: str,
                              session_id: str = "") -> None:
        """通知发送时触发 Notification hooks."""
        hook_configs = self._config.match(
            HookEvent.NOTIFICATION.value, query=notification_type,
        )
        if not hook_configs:
            return

        for cfg in hook_configs:
            cfg.setdefault("timeout", self._gateway_timeout)

        try:
            await self._executor.run_all(
                hook_configs,
                hook_input={
                    "event": "Notification",
                    "notification_type": notification_type,
                    "message": message,
                    "session_id": session_id,
                },
            )
        except Exception as e:
            logger.warning("GatewayHookHandler: Notification hook failed: %s", e)