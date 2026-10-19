# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rail for retrying stalled or looping LLM streams."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.harness.rails.base import DeepAgentRail

_STREAM_CHUNK_INSPECTORS_KEY = "_stream_chunk_inspectors"
_LLM_RETRY_STATE_KEY = "_llm_retry_state"
_REPEAT_ERROR_MARKER = "LLM repeated stream output detected"
_STREAM_TIMEOUT_MARKERS = (
    "LLM stream timeout",
    "stream frame timeout",
)
# Model-call errors raised by the model clients as MODEL_CALL_FAILED
# (code 181001). Both streaming ("async stream error") and non-streaming
# ("async invoke error", e.g. HTTP 408) calls are transient upstream failures;
# retrying re-reads the same context window, so the conversation history is
# preserved.
_MODEL_CALL_FAILED_MARKERS = (
    "async stream error",
    "async invoke error",
)
# Whole-call retry backoff (seconds) before the 1st/2nd/... retry attempt.
# The last value is reused if there are more retries than entries.
_DEFAULT_BACKOFF_SECONDS = (0.5, 1.0, 2.0)


class LLMRetryRail(DeepAgentRail):
    """Retry selected streaming model failures.

    The rail handles three model-call failure modes:
      - repeated output suffixes in reasoning/content streams
      - stream frame timeout errors raised by ``Model.stream``
      - upstream model-call stream errors (code 181001)

    Repetition detection is provider-agnostic. It only keeps a bounded tail of
    text and checks whether that tail ends with a short snippet repeated many
    times, so the cost is independent of the full response length.
    """

    priority = 70

    def __init__(
            self,
            *,
            max_retries: int = 2,
            repeat_min_pattern_chars: int = 2,
            repeat_max_pattern_chars: int = 64,
            repeat_min_count: int = 6,
            repeat_min_total_chars: int = 160,
            repeat_window_chars: int = 1024,
            single_char_repeat_count: int = 100,
            backoff_seconds: Optional[List[float]] = None,
    ) -> None:
        super().__init__()
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        backoff = list(backoff_seconds) if backoff_seconds is not None else list(_DEFAULT_BACKOFF_SECONDS)
        if any(delay < 0 for delay in backoff):
            raise ValueError("backoff_seconds entries must be >= 0")
        if repeat_min_pattern_chars < 1:
            raise ValueError("repeat_min_pattern_chars must be >= 1")
        if repeat_max_pattern_chars < repeat_min_pattern_chars:
            raise ValueError("repeat_max_pattern_chars must be >= repeat_min_pattern_chars")
        if repeat_min_count < 2:
            raise ValueError("repeat_min_count must be >= 2")
        if repeat_min_total_chars < 1:
            raise ValueError("repeat_min_total_chars must be >= 1")
        if single_char_repeat_count < 2:
            raise ValueError("single_char_repeat_count must be >= 2")
        min_window = max(repeat_max_pattern_chars * repeat_min_count, repeat_min_total_chars, single_char_repeat_count)
        if repeat_window_chars < min_window:
            raise ValueError("repeat_window_chars is too small for the configured repetition thresholds")

        self.max_retries = max_retries
        self.repeat_min_pattern_chars = repeat_min_pattern_chars
        self.repeat_max_pattern_chars = repeat_max_pattern_chars
        self.repeat_min_count = repeat_min_count
        self.repeat_min_total_chars = repeat_min_total_chars
        self.repeat_window_chars = repeat_window_chars
        self.single_char_repeat_count = single_char_repeat_count
        self.backoff_seconds = backoff
        self.repeat_retry_count = 0
        self.stream_timeout_retry_count = 0
        self.model_call_failed_retry_count = 0

    def backoff_delay(self, retry_index: int) -> float:
        """Return the sleep (seconds) before the retry at ``retry_index`` (0-based).

        Uses the configured backoff schedule, clamped to its last entry.
        """
        if not self.backoff_seconds:
            return 0.0
        return self.backoff_seconds[min(retry_index, len(self.backoff_seconds) - 1)]

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Reset retry counters at the start of each agent invocation."""
        self.repeat_retry_count = 0
        self.stream_timeout_retry_count = 0
        self.model_call_failed_retry_count = 0

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Install a per-model-call stream chunk inspector."""
        # 重试额度按「单次模型调用」计算：只有新一轮模型调用（retry_attempt==0）才重置，
        # 重试 attempt 不重置，避免无限重试。
        if not getattr(ctx, "retry_attempt", 0):
            self.repeat_retry_count = 0
            self.stream_timeout_retry_count = 0
            self.model_call_failed_retry_count = 0
        ctx.extra[_LLM_RETRY_STATE_KEY] = {
            "reasoning_content": "",
            "content": "",
        }
        inspectors = ctx.extra.get(_STREAM_CHUNK_INSPECTORS_KEY)
        if not isinstance(inspectors, list):
            inspectors = []
        inspectors = [
            inspector
            for inspector in inspectors
            if getattr(inspector, "__self__", None) is not self
        ]
        inspectors.append(self.inspect_stream_chunk)
        ctx.extra[_STREAM_CHUNK_INSPECTORS_KEY] = inspectors

    async def inspect_stream_chunk(self, ctx: AgentCallbackContext, chunk: Any) -> None:
        """Inspect reasoning/content chunks for repeated suffixes."""
        state = ctx.extra.setdefault(_LLM_RETRY_STATE_KEY, {})
        for field_name in ("reasoning_content", "content"):
            text = getattr(chunk, field_name, None)
            if not text:
                continue
            self._append_and_check(state, field_name, str(text))

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        """Retry only LLM retry rail model-call failures."""
        if self._is_repeat_exception(ctx.exception):
            await self._request_retry_or_reset(ctx, "repeat")
            return

        if self._is_stream_timeout_exception(ctx.exception):
            await self._request_retry_or_reset(ctx, "stream_timeout")
            return

        if self._is_model_call_failed_exception(ctx.exception):
            await self._request_retry_or_reset(ctx, "model_call_failed")
            return

        # 兼容兜底：其余任何模型调用异常（如未命中上面 marker 的 APIStatusError
        # 408/429/5xx 等瞬态或未知错误）统一按 model_call_failed 重试；
        # 重试耗尽后异常照常上抛，透传/rewind 行为不变。
        await self._request_retry_or_reset(ctx, "model_call_failed")

    def _append_and_check(self, state: Dict[str, str], field_name: str, text: str) -> None:
        tail = (state.get(field_name, "") + text)[-self.repeat_window_chars:]
        state[field_name] = tail
        detected = self._detect_repeated_suffix(tail)
        if detected is None:
            return

        repeated_unit, repeat_count = detected
        raise build_error(
            StatusCode.MODEL_CALL_FAILED,
            error_msg=(
                f"{_REPEAT_ERROR_MARKER}: field={field_name}, "
                f"unit={self._format_unit(repeated_unit)!r}, repeat_count={repeat_count}"
            ),
        )

    def _detect_repeated_suffix(self, text: str) -> Optional[Tuple[str, int]]:
        single_char_match = self._detect_single_char_suffix(text)
        if single_char_match is not None:
            return single_char_match

        max_unit_len = min(self.repeat_max_pattern_chars, len(text) // self.repeat_min_count)
        for unit_len in range(self.repeat_min_pattern_chars, max_unit_len + 1):
            unit = text[-unit_len:]
            if not unit.strip() or self._is_single_char_pattern(unit):
                continue

            required_count = max(
                self.repeat_min_count,
                (self.repeat_min_total_chars + unit_len - 1) // unit_len,
            )
            repeat_count = 1
            pos = len(text) - unit_len * 2
            while pos >= 0 and text[pos:pos + unit_len] == unit:
                repeat_count += 1
                pos -= unit_len

            if repeat_count >= required_count:
                return unit, repeat_count

        return None

    def _detect_single_char_suffix(self, text: str) -> Optional[Tuple[str, int]]:
        if not text:
            return None
        last_char = text[-1]
        if not last_char.strip():
            return None

        repeat_count = 1
        for index in range(len(text) - 2, -1, -1):
            if text[index] != last_char:
                break
            repeat_count += 1
            if repeat_count >= self.single_char_repeat_count:
                return last_char, repeat_count
        return None

    @staticmethod
    def _is_single_char_pattern(unit: str) -> bool:
        return len(set(unit)) == 1

    @staticmethod
    def _format_unit(unit: str, limit: int = 80) -> str:
        formatted = unit.replace("\r", "\\r").replace("\n", "\\n")
        if len(formatted) <= limit:
            return formatted
        return formatted[:limit] + "..."

    async def _request_retry_or_reset(self, ctx: AgentCallbackContext, reason: str) -> None:
        if reason == "repeat":
            if self.repeat_retry_count < self.max_retries:
                delay = self.backoff_delay(self.repeat_retry_count)
                self.repeat_retry_count += 1
                await self._emit_retry_notice(ctx, self.repeat_retry_count, reason, delay)
                logger.warning(
                    "[LLMRetryRail] retrying model call after repeated stream output "
                    f"({self.repeat_retry_count}/{self.max_retries}) after {delay:.2f}s backoff"
                )
                ctx.request_retry(delay_seconds=delay)
            else:
                self.repeat_retry_count = 0
            return

        if reason == "stream_timeout":
            if self.stream_timeout_retry_count < self.max_retries:
                delay = self.backoff_delay(self.stream_timeout_retry_count)
                self.stream_timeout_retry_count += 1
                await self._emit_retry_notice(ctx, self.stream_timeout_retry_count, reason, delay)
                logger.warning(
                    "[LLMRetryRail] retrying model call after stream frame timeout "
                    f"({self.stream_timeout_retry_count}/{self.max_retries}) after {delay:.2f}s backoff"
                )
                ctx.request_retry(delay_seconds=delay)
            else:
                self.stream_timeout_retry_count = 0
            return

        if self.model_call_failed_retry_count < self.max_retries:
            delay = self.backoff_delay(self.model_call_failed_retry_count)
            self.model_call_failed_retry_count += 1
            await self._emit_retry_notice(ctx, self.model_call_failed_retry_count, reason, delay)
            logger.warning(
                "[LLMRetryRail] retrying model call after model-call failure "
                f"({self.model_call_failed_retry_count}/{self.max_retries}) after {delay:.2f}s backoff"
            )
            ctx.request_retry(delay_seconds=delay)
        else:
            self.model_call_failed_retry_count = 0

    async def _emit_retry_notice(
            self,
            ctx: AgentCallbackContext,
            retry_count: int,
            reason: str,
            delay: float,
    ) -> None:
        """向前端发重试通知，复用既有的 chat.error 事件通道.

        retry_notice=True 是结构化标记：下游（落盘过滤/展示映射）按标记识别重试通知。
        attempt/max_attempts 供前端按成员路由时显示「重试中 N/M」结构化进度
        （旧版前端无此字段时按文案正则兜底，兼容）。
        """
        session = ctx.session
        if session is None:
            return
        try:
            await session.write_stream(OutputSchema(
                type="error",
                index=0,
                payload={
                    "error": (
                        f"模型调用异常，将在 {delay:.1f} 秒后进行"
                        f"第 {retry_count} 次重试（共 {self.max_retries} 次）"
                    ),
                    "retry_notice": True,
                    "attempt": retry_count,
                    "max_attempts": self.max_retries,
                },
            ))
        except Exception:
            logger.debug("Failed to emit retry notice", exc_info=True)

    @staticmethod
    def _is_repeat_exception(exc: Optional[BaseException]) -> bool:
        return exc is not None and _REPEAT_ERROR_MARKER in str(exc)

    @staticmethod
    def _is_stream_timeout_exception(exc: Optional[BaseException]) -> bool:
        if exc is None:
            return False
        message = str(exc)
        return any(marker in message for marker in _STREAM_TIMEOUT_MARKERS)

    @staticmethod
    def _is_model_call_failed_exception(exc: Optional[BaseException]) -> bool:
        if exc is None:
            return False
        message = str(exc)
        return any(marker in message for marker in _MODEL_CALL_FAILED_MARKERS)


__all__ = [
    "LLMRetryRail",
]
