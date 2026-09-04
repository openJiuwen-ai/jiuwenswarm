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
# Per-call budget lives on ctx.extra so concurrent call_llm/stream_llm that
# share one rail instance (SkillTurbo PPT page gather) cannot steal or reset
# each other's retry counts. Instance attrs are mirrored for observability only.
_RETRY_COUNTS_KEY = "_llm_retry_counts"
_COUNT_ATTRS: dict[str, str] = {
    "repeat": "repeat_retry_count",
    "stream_timeout": "stream_timeout_retry_count",
    "transient_invoke": "transient_invoke_retry_count",
}


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

        parent_kwargs = dict(kwargs)
        if _parent_accepts_retry_transient_invoke_errors():
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

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Reset per-ctx retry budgets at the start of each call/invoke."""
        ctx.extra[_RETRY_COUNTS_KEY] = {
            "repeat": 0,
            "stream_timeout": 0,
            "transient_invoke": 0,
        }
        await super().before_invoke(ctx)

    def _retry_counts(self, ctx: AgentCallbackContext) -> dict[str, int]:
        counts = ctx.extra.get(_RETRY_COUNTS_KEY)
        if not isinstance(counts, dict):
            counts = {
                "repeat": 0,
                "stream_timeout": 0,
                "transient_invoke": 0,
            }
            ctx.extra[_RETRY_COUNTS_KEY] = counts
        for key in _COUNT_ATTRS:
            raw = counts.get(key, 0)
            try:
                counts[key] = int(raw or 0)
            except (TypeError, ValueError):
                counts[key] = 0
        return counts

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

        will_retry, attempt, max_attempts, delay = self._peek_retry(ctx, reason)
        if will_retry and self.notify_user_on_retry:
            await self._emit_retry_notification(ctx, reason, attempt, max_attempts, delay)
        elif not will_retry and self.notify_user_on_exhausted:
            await self._emit_retry_exhausted(ctx, reason, ctx.exception)

        self._request_retry_or_reset(ctx, reason)

    def _request_retry_or_reset(self, ctx: AgentCallbackContext, reason: str) -> None:
        counts = self._retry_counts(ctx)
        count = counts.get(reason, 0)
        attr = _COUNT_ATTRS.get(reason)
        if count < self.max_retries:
            delay = self.backoff_delay(count)
            counts[reason] = count + 1
            if attr is not None:
                setattr(self, attr, counts[reason])
            logger.warning(
                "[NotifyingLLMRetryRail] retrying model call after %s "
                "(%d/%d) after %.2fs backoff: %r",
                reason,
                counts[reason],
                self.max_retries,
                delay,
                ctx.exception,
            )
            ctx.request_retry(delay_seconds=delay)
            return

        counts[reason] = 0
        if attr is not None:
            setattr(self, attr, 0)

    def _peek_retry(
        self, ctx: AgentCallbackContext, reason: str
    ) -> tuple[bool, int, int, float]:
        """Return ``(will_retry, attempt_number, max_attempts, delay_seconds)``."""
        count = self._retry_counts(ctx).get(reason, 0)
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


__all__ = ["NotifyingLLMRetryRail"]
