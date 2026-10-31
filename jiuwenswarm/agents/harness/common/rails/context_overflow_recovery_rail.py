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

# 主动预压缩阈值：上一轮真实 input_tokens 达到窗口的 90% 时，
# 在本次模型调用前先 force_compact，避免等到服务端 400 才补救。
PROACTIVE_TRIGGER_RATIO = 0.90

# 华为 MaaS 超限变体: "input token limit is 1048576"
_MODELARTS_INPUT_TOKEN_LIMIT_PATTERN = re.compile(
    r"input\s+token\s+limit\s+is\s+(\d+)", re.IGNORECASE
)

# 路由前下发的档位关键字，不能当作真实模型名去查窗口
_TIER_MODEL_KEYWORDS = frozenset({"auto", "fast", "balanced", "extreme"})

# 不同 provider 的 usage 字段差异：OpenAI 风格只暴露 prompt_tokens
_PROMPT_TOKEN_KEYS = ("input_tokens", "prompt_tokens", "total_tokens")


def _coerce_positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _get_int_field(config: Any, key: str) -> int:
    if config is None:
        return 0
    if isinstance(config, dict):
        return _coerce_positive_int(config.get(key))
    return _coerce_positive_int(getattr(config, key, None))


def _has_positive_model_window(mapping: Any, model_name: str) -> bool:
    """判断模型是否在窗口映射中（精确匹配 + 大小写不敏感匹配）。"""
    if not isinstance(mapping, dict) or not model_name:
        return False

    value = mapping.get(model_name)
    if isinstance(value, int) and value > 0:
        return True

    normalized = model_name.lower()
    for candidate, candidate_value in mapping.items():
        if not isinstance(candidate, str) or candidate.lower() != normalized:
            continue
        if isinstance(candidate_value, int) and candidate_value > 0:
            return True
    return False


def _usage_mapping(usage: Any) -> dict[str, Any]:
    """把 dict / Pydantic / dataclass 形态的 usage 统一成 dict。"""
    if isinstance(usage, dict):
        return usage
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ContextOverflowRecovery] usage model_dump failed; fallback to dict/getattr",
                exc_info=True,
            )
    try:
        mapped = dict(usage)
    except (TypeError, ValueError):
        return {}
    return mapped if isinstance(mapped, dict) else {}


def _usage_field(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    mapped = _usage_mapping(usage)
    if key in mapped:
        return mapped.get(key)
    # SimpleNamespace / 自定义对象没有 model_dump 时仍保留 getattr 兜底
    return getattr(usage, key, None)


def _parse_token_limits(exc: Exception) -> tuple[int | None, int | None]:
    """从 413 错误解析 limit_tokens 和 actual_tokens。

        Returns: (actual_tokens, limit_tokens)
    """
    msg = str(exc)

    # 华为 MaaS: "input token limit is N"
    m = _MODELARTS_INPUT_TOKEN_LIMIT_PATTERN.search(msg)
    if m:
        return None, int(m.group(1))

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
    """从 config 读显式上下文窗口上限。

    兼容历史字段 ``context_window_limit_tokens`` 与当前真实生效的
    ``react.context_engine_config.context_window_tokens``。
    """
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return 0

    for attr in ("deep_config", "_config", "_model_request_config"):
        config = getattr(agent, attr, None)
        if config is None:
            continue

        limit = _get_int_field(config, "context_window_limit_tokens")
        if limit > 0:
            return limit

        limit = _get_int_field(config, "context_window_tokens")
        if limit > 0:
            return limit

        context_engine_config = (
            config.get("context_engine_config")
            if isinstance(config, dict)
            else getattr(config, "context_engine_config", None)
        )
        limit = _get_int_field(context_engine_config, "context_window_tokens")
        if limit > 0:
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
    # before_model_call: proactive compaction
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """在模型调用前用上一轮真实 usage 做一次主动预压缩。

        日常压缩器按本地 tokenizer 估算，遇到中文/工具/附件时可能显著低估真实
        prompt（本次线上 751K vs 实际 1.047M）。这里直接使用服务端返回的
        ``usage.input_tokens`` 作为基线，达到 90% 时提前 force_compact，
        避免请求一路发到服务端 1M 硬上限才 400。

        每个 invoke 最多触发一次（通过 ``ctx.extra`` 标记），压缩失败或
        usage 不可用时不改变原有行为。解析不到真实模型窗口时直接跳过，
        不会用 agent-core 的 200000 默认值误触发；该场景仍由
        ``on_model_exception`` 的响应式恢复兜底。
        """
        if ctx.extra.get("_context_overflow_proactive_done"):
            return

        limit = self._resolve_model_context_limit(ctx)
        if limit <= 0:
            return

        used = self._last_prompt_tokens(ctx)
        if used <= 0 or used < int(limit * PROACTIVE_TRIGGER_RATIO):
            return

        threshold_override = int(limit * RECOVERY_THRESHOLD_RATIO)
        if self._set_force_compact_flag(ctx, threshold_override=threshold_override):
            ctx.extra["_context_overflow_proactive_done"] = True
            logger.warning(
                "[ContextOverflowRecovery] Proactive compact before model call: "
                "used_tokens=%d limit_tokens=%d threshold_override=%d",
                used,
                limit,
                threshold_override,
            )
        else:
            logger.warning(
                "[ContextOverflowRecovery] Proactive compact skipped: "
                "used_tokens=%d limit_tokens=%d (FullCompactProcessor unavailable)",
                used,
                limit,
            )

    @staticmethod
    def _last_prompt_tokens(ctx: AgentCallbackContext) -> int:
        """从历史消息的 usage_metadata 取最近一次真实 prompt token 数。

        兼容 dict / Pydantic / 普通对象三种 usage 形态，并按
        input_tokens → prompt_tokens → total_tokens 回退（与 StreamEventRail 一致）。
        """
        inputs = getattr(ctx, "inputs", None)
        messages = list(getattr(inputs, "messages", None) or [])
        for message in reversed(messages):
            usage = getattr(message, "usage_metadata", None)
            if not usage:
                continue
            for key in _PROMPT_TOKEN_KEYS:
                value = _coerce_positive_int(_usage_field(usage, key))
                if value > 0:
                    return value
        if messages:
            logger.debug(
                "[ContextOverflowRecovery] no usage_metadata found in %d messages; "
                "proactive compact skipped",
                len(messages),
            )
        return 0

    @staticmethod
    def _resolve_model_name(ctx: AgentCallbackContext) -> str:
        """优先取路由后的真实模型名，避免把 fast/auto 档位关键字当模型名。"""
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            run_context = extra.get("run_context")
            run_context_extra = getattr(run_context, "extra", None)
            if isinstance(run_context_extra, dict):
                routed = str(
                    run_context_extra.get("model_routing_applied_model") or ""
                ).strip()
                if routed:
                    return routed

        agent = getattr(ctx, "agent", None)
        for attr in ("_model_request_config", "_config", "deep_config"):
            config = getattr(agent, attr, None)
            if config is None:
                continue
            name = (
                config.get("model_name")
                if isinstance(config, dict)
                else getattr(config, "model_name", "")
            )
            name = str(name or "").strip()
            if name:
                return name
        return ""

    @staticmethod
    def _is_known_model(
        model_name: str,
        model_windows: dict[str, int] | None,
        default_windows: dict[str, int],
    ) -> bool:
        """判断模型是否真的在窗口表里，避免把 200000 默认值当成真实窗口。"""
        return _has_positive_model_window(
            model_windows, model_name
        ) or _has_positive_model_window(default_windows, model_name)

    @classmethod
    def _resolve_model_context_limit(cls, ctx: AgentCallbackContext) -> int:
        """按“显式窗口 → 模型窗口表 → 配置”解析真实上下文窗口。

        解析不到真实模型窗口时返回 0，放弃主动预压缩，避免用 agent-core
        的 200000 默认值在远未接近真实窗口时反复触发 FullCompact。
        """
        context = getattr(ctx, "context", None)
        explicit_limit = _coerce_positive_int(
            getattr(context, "_context_window_tokens", None)
        )
        model_windows = getattr(context, "_model_context_window_tokens", None)
        if not isinstance(model_windows, dict):
            model_windows = None

        if explicit_limit <= 0:
            explicit_limit = _get_config_context_limit(ctx)
        if explicit_limit > 0:
            return explicit_limit

        model_name = cls._resolve_model_name(ctx)
        if not model_name or model_name.lower() in _TIER_MODEL_KEYWORDS:
            logger.debug(
                "[ContextOverflowRecovery] no routed model name (model=%r); "
                "proactive compact skipped",
                model_name,
            )
            return 0

        try:
            from openjiuwen.core.context_engine.context.context_utils import (
                DEFAULT_CONTEXT_MAX_TOKENS,
                MODEL_DEFAULT_CONTEXT_WINDOW_TOKENS,
                ContextUtils,
            )

            resolved = _coerce_positive_int(
                ContextUtils.resolve_context_max(
                    model_name=model_name,
                    model_context_window_tokens=model_windows,
                )
            )
            if resolved <= 0:
                return 0
            if resolved == DEFAULT_CONTEXT_MAX_TOKENS and not cls._is_known_model(
                model_name, model_windows, MODEL_DEFAULT_CONTEXT_WINDOW_TOKENS
            ):
                logger.debug(
                    "[ContextOverflowRecovery] unknown context window for model=%s; "
                    "proactive compact skipped",
                    model_name,
                )
                return 0
            return resolved
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ContextOverflowRecovery] resolve model context limit failed",
                exc_info=True,
            )
            return 0

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
            "context length exceeded",
            "input token limit",  # 华为 ModelArts.81001: "input token limit is N"
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
