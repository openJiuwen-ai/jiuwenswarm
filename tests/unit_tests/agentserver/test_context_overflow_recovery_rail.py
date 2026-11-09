# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextOverflowRecoveryRail.

Covers the Huawei MaaS overflow variant ``input token limit is N`` and the
proactive compaction hook added for flash mode.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
async def test_proactive_compact_skips_unknown_model():
    """未知模型不能落到 agent-core 的 200000 默认窗口上。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _make_ctx(
        input_tokens=1_000_000,
        processor=processor,
        model_name="custom-enterprise-alias",
    )

    await rail.before_model_call(ctx)

    assert processor.force_compact is False


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
