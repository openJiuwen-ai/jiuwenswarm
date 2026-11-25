# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextOverflowRecoveryRail.

Covers the Huawei MaaS overflow variant ``input token limit is N`` and the
proactive compaction hook added for flash mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.core.context_engine.context.context_utils import DEFAULT_CONTEXT_MAX_TOKENS

from jiuwenswarm.agents.harness.common.rails.context_overflow_recovery_rail import (
    RECOVERY_THRESHOLD_RATIO,
    ContextOverflowRecoveryRail,
    _parse_token_limits,
)

MODELARTS_INPUT_TOKEN_LIMIT_ERROR = (
    "[181001] model call failed, reason: openAI API async stream error: "
    "BadRequestError: Error code: 400 - {'error': {'code': 'ModelArts.81001', "
    "'message': 'input token limit is 1048576', 'param': None, "
    "'type': 'invalid_request_error'}, 'error_code': 'ModelArts.81001', "
    "'error_msg': 'input token limit is 1048576'}"
)


class _FakeFullCompactProcessor:
    def __init__(self) -> None:
        self.force_compact = False
        self.overflow_threshold_override: int | None = None

    @staticmethod
    def processor_type() -> str:
        return "FullCompactProcessor"

    def set_force_compact(self, value: bool = True) -> None:
        self.force_compact = value

    def set_overflow_threshold_override(self, value: int) -> None:
        self.overflow_threshold_override = value


def _make_ctx(
    *,
    input_tokens: int,
    processor: _FakeFullCompactProcessor,
    model_name: str = "deepseek-v4.1-flash",
    usage=None,
):
    if usage is None:
        usage = SimpleNamespace(input_tokens=input_tokens)
    message = SimpleNamespace(usage_metadata=usage)
    return SimpleNamespace(
        extra={},
        inputs=SimpleNamespace(messages=[message]),
        context=SimpleNamespace(_processors=[processor]),
        agent=SimpleNamespace(_config=SimpleNamespace(model_name=model_name)),
        exception=None,
    )


class _ModelDumpUsage:
    def model_dump(self):
        return {"input_tokens": 1_000_000}


def test_detects_modelarts_input_token_limit_with_status_code():
    exc = Exception(MODELARTS_INPUT_TOKEN_LIMIT_ERROR)
    exc.status_code = 400

    assert ContextOverflowRecoveryRail._is_context_overflow_error(exc) is True


def test_detects_modelarts_input_token_limit_without_status_code():
    exc = Exception(MODELARTS_INPUT_TOKEN_LIMIT_ERROR)

    assert ContextOverflowRecoveryRail._is_context_overflow_error(exc) is True


def test_does_not_treat_chat_template_error_as_overflow():
    exc = Exception(
        "ModelArts.81001 chat template failed: Error code: 400 invalid_request_error"
    )
    exc.status_code = 400

    assert ContextOverflowRecoveryRail._is_context_overflow_error(exc) is False


def test_parse_modelarts_input_token_limit():
    assert _parse_token_limits(Exception(MODELARTS_INPUT_TOKEN_LIMIT_ERROR)) == (
        None,
        1048576,
    )


def test_threshold_override_uses_parsed_limit():
    assert ContextOverflowRecoveryRail._resolve_threshold_override(None, 1048576) == int(
        1048576 * RECOVERY_THRESHOLD_RATIO
    )


@pytest.mark.asyncio
async def test_proactive_compact_sets_force_compact_once_per_invoke():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(input_tokens=1_000_000, processor=processor)

    await rail.before_model_call(ctx)

    assert processor.force_compact is True
    assert processor.overflow_threshold_override == int(
        1048576 * RECOVERY_THRESHOLD_RATIO
    )
    assert ctx.extra["_context_overflow_proactive_done"] is True

    processor.force_compact = False
    await rail.before_model_call(ctx)
    assert processor.force_compact is False


@pytest.mark.asyncio
async def test_proactive_compact_skips_below_threshold():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(input_tokens=800_000, processor=processor)

    await rail.before_model_call(ctx)

    assert processor.force_compact is False
    assert processor.overflow_threshold_override is None
    assert ctx.extra.get("_context_overflow_proactive_done") is None


@pytest.mark.asyncio
async def test_proactive_compact_skips_without_usage_metadata():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(input_tokens=0, processor=processor)
    ctx.inputs.messages = [SimpleNamespace(content="no usage")]

    await rail.before_model_call(ctx)

    assert processor.force_compact is False


@pytest.mark.asyncio
async def test_proactive_compact_reads_dict_usage():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=0,
        processor=processor,
        usage={"input_tokens": 1_000_000},
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_reads_prompt_tokens_fallback():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=0,
        processor=processor,
        usage={"prompt_tokens": 1_000_000},
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_reads_model_dump_usage():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=0,
        processor=processor,
        usage=_ModelDumpUsage(),
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_falls_back_to_default_window_for_unknown_model():
    """未知模型按 agent-core 的 200000 默认窗口触发，而不是静默跳过。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="custom-enterprise-alias",
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True
    assert processor.overflow_threshold_override == int(
        DEFAULT_CONTEXT_MAX_TOKENS * RECOVERY_THRESHOLD_RATIO
    )


def test_resolve_model_context_limit_returns_default_for_unknown_model():
    ctx = _make_ctx(
        input_tokens=0,
        processor=_FakeFullCompactProcessor(),
        model_name="custom-enterprise-alias",
    )

    assert (
        ContextOverflowRecoveryRail._resolve_model_context_limit(ctx)
        == DEFAULT_CONTEXT_MAX_TOKENS
    )


@pytest.mark.asyncio
async def test_proactive_compact_skips_tier_keyword_model():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="fast",
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is False


@pytest.mark.asyncio
async def test_proactive_compact_uses_explicit_context_window():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="custom-enterprise-alias",
    )
    ctx.context._context_window_tokens = 1048576

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_uses_model_window_mapping():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="custom-enterprise-alias",
    )
    ctx.context._model_context_window_tokens = {"custom-enterprise-alias": 1048576}

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_uses_context_engine_config_window():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="custom-enterprise-alias",
    )
    ctx.agent._config.context_engine_config = SimpleNamespace(
        context_window_tokens=1048576
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


@pytest.mark.asyncio
async def test_proactive_compact_prefers_routed_model_name():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="fast",
    )
    ctx.extra["run_context"] = SimpleNamespace(
        extra={"model_routing_applied_model": "deepseek-v4.1-flash"}
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True


# ---------------------------------------------------------------------------
# PR-B: warmup seed + unchanged-payload circuit break
# ---------------------------------------------------------------------------

_WARMUP_SEED_ATTR = "_jiuwenswarm_warmup_prompt_tokens"


def _make_overflow_ctx(
    *,
    messages,
    processor: _FakeFullCompactProcessor | None = None,
    seed: int | None = None,
    window: int = 512000,
    session=None,
):
    context = SimpleNamespace(_processors=[processor] if processor else [])
    context._context_window_tokens = window
    if seed is not None:
        setattr(context, _WARMUP_SEED_ATTR, seed)
    return SimpleNamespace(
        extra={},
        inputs=SimpleNamespace(messages=list(messages)),
        context=context,
        agent=SimpleNamespace(_config=SimpleNamespace(model_name="deepseek-v4.1-flash-1017")),
        exception=None,
        request_retry=Mock(),
        session=session,
    )


def _overflow_error(limit: int = 512000) -> Exception:
    exc = Exception(f"OpenAI API error: input token limit is {limit}")
    exc.status_code = 400
    return exc


@pytest.mark.asyncio
async def test_proactive_compact_uses_warmup_seed_without_usage():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_overflow_ctx(
        messages=[SimpleNamespace(content="restored history")],
        processor=processor,
        seed=703_840,
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is True
    assert processor.overflow_threshold_override == int(512000 * RECOVERY_THRESHOLD_RATIO)


def test_last_prompt_tokens_prefers_usage_over_seed():
    ctx = _make_overflow_ctx(
        messages=[SimpleNamespace(usage_metadata={"input_tokens": 100_000})],
        seed=703_840,
    )

    assert ContextOverflowRecoveryRail.last_prompt_tokens(ctx) == 100_000


@pytest.mark.asyncio
async def test_after_model_call_clears_seed_and_payload_fingerprint():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    ctx = _make_overflow_ctx(
        messages=[SimpleNamespace(content="history")],
        seed=703_840,
    )
    ctx.extra["_overflow_payload_fp"] = (1, (("user", 7),), 0)

    await rail.after_model_call(ctx)

    assert not hasattr(ctx.context, _WARMUP_SEED_ATTR)
    assert "_overflow_payload_fp" not in ctx.extra


@pytest.mark.asyncio
async def test_repeated_payload_overflow_circuit_breaks_after_first_retry():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    messages = [SimpleNamespace(role="user", content="same payload")]
    written: list = []

    async def _write_stream(output) -> None:
        written.append(output)

    ctx = _make_overflow_ctx(
        messages=messages, session=SimpleNamespace(write_stream=_write_stream)
    )
    ctx.exception = _overflow_error()

    await rail.on_model_exception(ctx)
    assert ctx.request_retry.call_count == 1

    await rail.on_model_exception(ctx)
    # 第二次同一 payload：压缩没产生替换 → 熔断，不再重试
    assert ctx.request_retry.call_count == 1
    assert len(written) == 1
    assert written[0].payload["error_type"] == "context_overflow_circuit_break"


@pytest.mark.asyncio
async def test_changed_payload_after_compact_retries_again():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    messages = [SimpleNamespace(role="user", content="same payload")]
    ctx = _make_overflow_ctx(messages=messages)
    ctx.exception = _overflow_error()

    await rail.on_model_exception(ctx)
    ctx.inputs.messages.append(SimpleNamespace(role="user", content="summary injected"))
    await rail.on_model_exception(ctx)

    assert ctx.request_retry.call_count == 2


def test_resolve_model_context_limit_uses_512k_model_window():
    ctx = _make_overflow_ctx(messages=[], window=0)
    ctx.context._model_context_window_tokens = {"deepseek-v4.1-flash-1017": 512000}

    assert ContextOverflowRecoveryRail._resolve_model_context_limit(ctx) == 512000
