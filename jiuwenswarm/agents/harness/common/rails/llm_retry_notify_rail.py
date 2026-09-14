# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side LLM retry rail that surfaces retry progress to the user stream."""

from __future__ import annotations

import inspect
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.llm_retry_rail import LLMRetryRail

_REASON_LABELS: dict[str, str] = {
    "repeat": "重复输出",
    "stream_timeout": "流式超时",
    "transient_invoke": "连接/超时",
}
_TRANSIENT_MESSAGE_MARKERS = (
    "connection error",
    "connection reset",
    "connection refused",
    "request timed out",
    "read timeout",
    "connect timeout",
)
_MEMBER_NETWORK_404_MARKERS = (
    "async stream error",
    "notfounderror",
    "error code: 404",
)


def _parent_accepts_retry_transient_invoke_errors() -> bool:
    return "retry_transient_invoke_errors" in inspect.signature(LLMRetryRail.__init__).parameters


class NotifyingLLMRetryRail(LLMRetryRail):
    """Extend :class:`LLMRetryRail` with jiuwenswarm user-visible retry notifications."""

    def __init__(
        self,
        *,
        notify_user_on_retry: bool = True,
        notify_user_on_exhausted: bool = True,
        retry_transient_invoke_errors: bool = True,
        **kwargs: Any,
    ) -> None:
        self.notify_user_on_retry = notify_user_on_retry
        self.notify_user_on_exhausted = notify_user_on_exhausted
        self.retry_transient_invoke_errors = retry_transient_invoke_errors
        self._parent_supports_transient_invoke = _parent_accepts_retry_transient_invoke_errors()

        parent_kwargs = dict(kwargs)
        if self._parent_supports_transient_invoke:
            parent_kwargs["retry_transient_invoke_errors"] = retry_transient_invoke_errors
        super().__init__(**parent_kwargs)

        if not hasattr(self, "transient_invoke_retry_count"):
            self.transient_invoke_retry_count = 0

    @classmethod
    def _looks_like_transient_invoke(cls, exc: BaseException | None) -> bool:
        checker = getattr(LLMRetryRail, "_is_transient_invoke_exception", None)
        if callable(checker):
            return bool(checker(exc))
        message = str(exc or "").lower()
        for marker in _TRANSIENT_MESSAGE_MARKERS:
            if marker in message:
                return True
        return False

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        reason: str | None = None
        if self._is_repeat_exception(ctx.exception):
            reason = "repeat"
        elif self._is_stream_timeout_exception(ctx.exception):
            reason = "stream_timeout"
        elif self.retry_transient_invoke_errors and self._looks_like_transient_invoke(
            ctx.exception
        ):
            reason = "transient_invoke"
        if reason is None:
            return

        will_retry, attempt, max_attempts, delay = self._peek_retry(reason)
        if will_retry and self.notify_user_on_retry:
            await self._emit_retry_notification(ctx, reason, attempt, max_attempts, delay)
        elif not will_retry and self.notify_user_on_exhausted:
            await self._emit_retry_exhausted(ctx, reason, ctx.exception)

        self._request_retry_or_reset(ctx, reason)

    def _request_retry_or_reset(self, ctx: AgentCallbackContext, reason: str) -> None:
        if reason == "transient_invoke" and not self._parent_supports_transient_invoke:
            if self.transient_invoke_retry_count < self.max_retries:
                delay = self.backoff_delay(self.transient_invoke_retry_count)
                self.transient_invoke_retry_count += 1
                logger.warning(
                    "[NotifyingLLMRetryRail] retrying model call after transient invoke error "
                    f"({self.transient_invoke_retry_count}/{self.max_retries}) after {delay:.2f}s backoff: "
                    f"{ctx.exception!r}"
                )
                ctx.request_retry(delay_seconds=delay)
            else:
                self.transient_invoke_retry_count = 0
            return
        super()._request_retry_or_reset(ctx, reason)

    def _peek_retry(self, reason: str) -> tuple[bool, int, int, float]:
        """Return ``(will_retry, attempt_number, max_attempts, delay_seconds)``."""
        if reason == "repeat":
            count = self.repeat_retry_count
            if count < self.max_retries:
                return True, count + 1, self.max_retries, self.backoff_delay(count)
            return False, count, self.max_retries, 0.0

        if reason == "transient_invoke":
            count = self.transient_invoke_retry_count
            if count < self.max_retries:
                return True, count + 1, self.max_retries, self.backoff_delay(count)
            return False, count, self.max_retries, 0.0

        count = self.stream_timeout_retry_count
        if count < self.max_retries:
            return True, count + 1, self.max_retries, self.backoff_delay(count)
        return False, count, self.max_retries, 0.0

    @staticmethod
    async def _emit_retry_notification(
        ctx: AgentCallbackContext,
        reason: str,
        attempt: int,
        max_attempts: int,
        delay: float,
    ) -> None:
        session = ctx.session
        if session is None or not hasattr(session, "write_stream"):
            return

        label = _REASON_LABELS.get(reason, reason)
        message = (
            f"\n\n⚠️ 模型调用异常 [{label}]，"
            f"将在 {delay:.1f} 秒后进行第 {attempt}/{max_attempts} 次重试..."
        )
        try:
            await session.write_stream(
                OutputSchema(
                    type="retry_notification",
                    index=999,
                    payload={
                        "output": {
                            "output": message,
                            "result_type": "text",
                        },
                        "reason": reason,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay_seconds": delay,
                    },
                )
            )
        except Exception as exc:
            logger.warning(
                "[NotifyingLLMRetryRail] retry notification failed: %s",
                exc,
            )

    @staticmethod
    async def _emit_retry_exhausted(
        ctx: AgentCallbackContext,
        reason: str,
        exc: BaseException | None,
    ) -> None:
        session = ctx.session
        if session is None or not hasattr(session, "write_stream"):
            return

        label = _REASON_LABELS.get(reason, reason)
        detail = str(exc) if exc is not None else "unknown error"
        message = f"模型调用失败 [{label}]，已用尽自动重试：{detail}"
        try:
            await session.write_stream(
                OutputSchema(
                    type="error",
                    index=999,
                    payload={
                        "error": message,
                        "error_type": "llm_retry_exhausted",
                        "reason": reason,
                    },
                )
            )
        except Exception as write_exc:
            logger.warning(
                "[NotifyingLLMRetryRail] retry exhaustion notification failed: %s",
                write_exc,
            )


class TeamMemberNotifyingLLMRetryRail(NotifyingLLMRetryRail):
    """Team-agent retry rail for the observed async-stream 404 transport shape."""

    @classmethod
    def _looks_like_transient_invoke(cls, exc: BaseException | None) -> bool:
        if super()._looks_like_transient_invoke(exc):
            return True
        message = str(exc or "").lower()
        return all(marker in message for marker in _MEMBER_NETWORK_404_MARKERS)


__all__ = ["NotifyingLLMRetryRail", "TeamMemberNotifyingLLMRetryRail"]
