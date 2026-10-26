# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import re
from typing import Any, Optional

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.common.logging import logger

# 413 恢复时 threshold_override 占模型窗口的比例
# 0.85 表示压缩后上下文最多占模型窗口的 85%，预留 15% 给 LLM 输出
RECOVERY_THRESHOLD_RATIO = 0.85


def _parse_token_limits(exc: Exception) -> tuple[int | None, int | None]:
    """从 413 错误解析 limit_tokens 和 actual_tokens。

        Returns: (actual_tokens, limit_tokens)
    """
    msg = str(exc)

    # Anthropic: "prompt is too long: N tokens > M maximum"
    m = re.search(r'(\d+)\s*tokens?\s*>\s*(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 华为: "prompt length N must less than maximum input length M"
    m = re.search(r'prompt length\s+(\d+).*?maximum input length\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # OpenAI: "maximum context length is N"
    m = re.search(r'maximum context length is\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return None, int(m.group(1))

    return None, None


def _get_config_context_limit(ctx: AgentCallbackContext) -> int:
    """从 config 读 context_window_limit_tokens 作为 fallback。

        Returns 0 if not found (triggers circuit_break_with_model_hint).
    """
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return 0
    deep_config = getattr(agent, "deep_config", None)
    if deep_config is None:
        return 0
    # context_window_limit_tokens 在 deep_config 或 yaml 中
    limit = getattr(deep_config, "context_window_limit_tokens", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return 0


class ContextOverflowRecoveryRail(DeepAgentRail):
    """Context overflow recovery chain (reactive + proactive bridge).

        **Reactive** — when an LLM call fails with a context-overflow error (413, 400 with
        "context_length_exceeded", etc.):
        1. **Detect** — keyword matching on the exception message.
        2. **SessionMemory** — force an incremental notes update (bypass trigger_tokens).
        3. **FullCompact** — set ``force_compact`` flag so the next ``get_context_window``
           unconditionally triggers FullCompact (bypass ``trigger_total_tokens``).
        4. **Retry** — call ``ctx.request_retry()`` to re-enter the LLM call.
        5. **Circuit-break** — after ``max_recovery_attempts`` consecutive failures,
           stop retrying and let the exception propagate with a clear user-facing message.
        Priority: 100 (higher value = runs first; higher than StreamEventRail's 80
        and ContextEngineeringRail's 85), so ``on_model_exception`` fires **before**
        those rails.
    """

    priority = 100

    def __init__(self, max_recovery_attempts: int = 2) -> None:
        super().__init__()
        self._max_recovery_attempts = max_recovery_attempts
        self._consecutive_overflow_count: int = 0
        self._context_engineering_rail: Optional[Any] = None
        self._logged_missing_full_compact_bridge_api = False

    # ------------------------------------------------------------------
    # on_model_exception: core recovery logic
    # ------------------------------------------------------------------

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        exc = ctx.exception
        if exc is None or not self._is_context_overflow_error(exc):
            return

        self._consecutive_overflow_count += 1
        actual_tokens, limit_tokens = _parse_token_limits(exc)
        logger.warning(f"[ContextOverflowRecovery] Context overflow detected "
                       f"(attempt {self._consecutive_overflow_count}/{self._max_recovery_attempts}) "
                       f"actual_tokens={actual_tokens} limit_tokens={limit_tokens}")

        if self._consecutive_overflow_count > self._max_recovery_attempts:
            await self._circuit_break(ctx)
            return

        threshold_override = self._resolve_threshold_override(ctx, limit_tokens)
        logger.warning(f"[ContextOverflowRecovery] Calculate threshold_override = {threshold_override}")
        compact_success = self._set_force_compact_flag(ctx, threshold_override=threshold_override)

        if compact_success:
            ctx.request_retry(delay_seconds=0)
            logger.info("[ContextOverflowRecovery] Recovery actions taken, requesting retry")
        else:
            logger.warning(f"[ContextOverflowRecovery] Could not set force_compact flag or compact context failed; "
                           f"retrying anyway (attempt {self._consecutive_overflow_count}/{self._max_recovery_attempts}"
                           f"threshold_override={threshold_override})")
            ctx.request_retry(delay_seconds=0)

    # ------------------------------------------------------------------
    # after_model_call: reset counter on success
    # ------------------------------------------------------------------

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.exception is None:
            if self._consecutive_overflow_count > 0:
                logger.info(
                    "[ContextOverflowRecovery] LLM call succeeded after %d overflow recovery attempt(s)",
                    self._consecutive_overflow_count,
                )
            self._consecutive_overflow_count = 0

    # ------------------------------------------------------------------
    # Detection: structured → status code → keyword fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """判断异常是否为上下文溢出错误。
            1. status_code=413 — 明确的溢出语义，直接判定
            2. status_code=400 + 溢出关键词 — 400 状态码 对应多种错误类型（参数错误、权限错误等），必须结合关键词,
            才能区分出上下文溢出，避免误判
            3. 无 status_code + 溢出关键词兜底 — 非 SDK 标准异常的纯文本匹配
        """
        status_code = getattr(exc, "status_code", None)
        exc_str = str(exc)
        error_message_lower = exc_str.lower()

        overflow_keys = (
            "prompt is too long",  # Anthropic 精确前缀
            "input too long",  # Anthropic 新格式
            "context_length_exceeded",  # OpenAI 标准 error code
            "maximum context length",  # OpenAI: "maximum context length is N"
            "maximum input length",  # 华为: "maximum input length M"
            "must less than the maximum input",
            "context length exceeded"
        )
        error_key = "Error code: 400"
        error_code = "ModelArts.81001"
        has_overflow_keyword = any(keyword in error_message_lower for keyword in overflow_keys)

        if status_code == 413 or (status_code == 400 and has_overflow_keyword):
            return True

        # 无 status_code 但有溢出关键词
        if status_code is None and has_overflow_keyword:
            return True

        if (error_key in error_message_lower) and (error_code in error_message_lower) and has_overflow_keyword:
            return True

        return False

    @staticmethod
    def _find_full_compact_processor(context: Any) -> Any | None:
        if context is None:
            logger.warning("[ContextOverflowRecovery] No context available when finding FullCompactProcessor")
            return None

        processors = getattr(context, "_processors", None)
        if not processors:
            logger.warning("[ContextOverflowRecovery] Context has no _processors when finding FullCompactProcessor")
            return None
        for processor in processors:
            # 框架以 processor_type() 同名键标识处理器（见 MetaContextProcessor +
            # ContextEngine._PROCESSOR_MAP）。forked.activate() 把 "FullCompactProcessor"
            # 键覆盖为 forked 类，其实例不继承原始类，isinstance 会漏判；改用同名键 +
            # set_force_compact 能力识别，forked/原始实例均可命中。
            proc_type_fn = getattr(processor, "processor_type", None)
            if callable(proc_type_fn) and proc_type_fn() == "FullCompactProcessor":
                return processor
        logger.warning(
            "[ContextOverflowRecovery] No FullCompactProcessor found in context processors: %s",
            [type(processor).__name__ for processor in processors],
        )
        return None

    # ------------------------------------------------------------------
    # threshold_override resolution
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_threshold_override(cls, ctx: AgentCallbackContext, parsed_limit: int | None) -> int | None:
        """Compute threshold_override for FullCompact adaptive chain.

        threshold_override = model_limit * RECOVERY_THRESHOLD_RATIO
        自适应模型窗口大小，按照比例设置压缩阈值。
        """
        if parsed_limit is not None:
            return int(parsed_limit * RECOVERY_THRESHOLD_RATIO)

        config_limit = _get_config_context_limit(ctx)
        if config_limit > 0:
            return int(config_limit * RECOVERY_THRESHOLD_RATIO)

        # No parsed limit, no config — FullCompactProcessor will use default trigger_total_tokens
        logger.info("[ContextOverflowRecovery] No limit parsed, no config limit; "
                    "FullCompactProcessor will use default trigger_total_tokens")
        return None

    # ------------------------------------------------------------------
    # Set force_compact flag on FullCompactProcessor
    # ------------------------------------------------------------------

    def _set_force_compact_flag(self, ctx: AgentCallbackContext, *, threshold_override: int | None = None) -> bool:
        """Set force_compact flag on the FullCompactProcessor attached to context."""
        processor = self._find_full_compact_processor(ctx.context)
        if processor is None:
            return False

        set_force_compact = getattr(processor, "set_force_compact", None)
        if not callable(set_force_compact):
            logger.warning(
                "[ContextOverflowRecovery] FullCompactProcessor has no set_force_compact method"
            )
            return False

        set_force_compact(True)
        logger.info(
            "[ContextOverflowRecovery] Set force_compact=True on %s",
            type(processor).__name__,
        )
        if threshold_override is not None and hasattr(processor, "set_overflow_threshold_override"):
            processor.set_overflow_threshold_override(threshold_override)
            logger.info(f"[ContextOverflowRecovery] Set force_compact=True and "
                        f"threshold_override={threshold_override} on {type(processor).__name__}")
        return True

    # ------------------------------------------------------------------
    # Circuit breaker: overflow recovery exhausted
    # ------------------------------------------------------------------

    async def _circuit_break(self, ctx: AgentCallbackContext) -> None:
        logger.error(f"[ContextOverflowRecovery] Circuit breaker triggered after {self._consecutive_overflow_count} "
                     f"consecutive context overflow errors. Will not retry.")

        session = ctx.session
        if session is not None and hasattr(session, "write_stream"):
            try:
                from openjiuwen.core.session.stream import OutputSchema
                await session.write_stream(
                    OutputSchema(
                        type="error",
                        payload={
                            "error_type": "context_overflow_circuit_break",
                            "message": (
                                f"上下文持续溢出（连续 {self._consecutive_overflow_count} 次），"
                                "自动压缩恢复失败。建议：/compact 手动压缩上下文，或开始新会话。"
                            ),
                            "consecutive_errors": self._consecutive_overflow_count,
                        },
                    )
                )
                self._consecutive_overflow_count = 0
            except Exception as e:
                logger.warning("[ContextOverflowRecovery] Failed to send circuit-break event: %s", e)
