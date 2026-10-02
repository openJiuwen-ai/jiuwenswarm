# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Output-token-cap truncation reporting in JiuSwarmStreamEventRail."""

from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback import AbortError

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)

_RAIL_MODULE = "jiuwenswarm.agents.harness.common.rails.stream_event_rail"


class _FakeSession:
    def __init__(self):
        self.outputs = []

    async def write_stream(self, output):
        self.outputs.append(output)


class _RecordingLogger:
    """Collect the rendered warning lines the rail emits."""

    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)

    def info(self, message, *args):
        pass

    def debug(self, *args, **kwargs):
        pass


class _Usage:
    """Stand-in for openjiuwen UsageMetadata, which the rail dumps to a dict."""

    def __init__(self, output_tokens: int = 800):
        self.model_name = "test-model"
        self.input_tokens = 1200
        self.output_tokens = output_tokens
        self.total_tokens = 1200 + output_tokens

    def model_dump(self):
        return dict(vars(self))


def _response(finish_reason: str, *, tool_calls=None, content="half a sent"):
    return SimpleNamespace(
        content=content,
        finish_reason=finish_reason,
        response_id="resp-1",
        usage_metadata=_Usage(),
        tool_calls=tool_calls,
    )


def _tool_call(arguments: str, name: str = "write_file"):
    return SimpleNamespace(id="call_0", type="function", name=name, arguments=arguments)


def _context(response, *, max_tokens=1024):
    """Build a callback context that holds *response* and a model output cap."""
    agent = SimpleNamespace(
        _config=SimpleNamespace(
            model_name="test-model",
            model_config_obj=SimpleNamespace(max_tokens=max_tokens),
        )
    )
    return SimpleNamespace(
        session=_FakeSession(),
        context=SimpleNamespace(),
        agent=agent,
        extra={},
        context_usage_report=None,
        inputs=SimpleNamespace(response=response, context_usage_report=None),
    )


@pytest.fixture
def rail_logger(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(f"{_RAIL_MODULE}.logger", recorder)
    monkeypatch.setattr(
        f"{_RAIL_MODULE}.resolve_context_window_tokens", lambda **_kwargs: 10000
    )
    return recorder


@pytest.mark.asyncio
async def test_completed_response_reports_nothing(rail_logger):
    ctx = _context(_response("stop", content="a whole sentence."))

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert rail_logger.warnings == []
    assert [output.type for output in ctx.session.outputs] == ["context.usage"]


@pytest.mark.asyncio
async def test_truncated_reply_is_logged_with_counts_and_cap(rail_logger):
    ctx = _context(_response("length"))

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert len(rail_logger.warnings) == 1
    line = rail_logger.warnings[0]
    assert "output_tokens=800" in line
    assert "max_tokens=1024" in line
    assert "finish_reason=length" in line
    assert "model=test-model" in line


@pytest.mark.asyncio
async def test_truncated_reply_still_reaches_the_reader(rail_logger):
    ctx = _context(_response("length"))

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert ctx.inputs.response.content == "half a sent"
    assert [output.type for output in ctx.session.outputs] == ["context.usage"]


@pytest.mark.asyncio
async def test_truncated_tool_call_fails_the_step_naming_the_cap(rail_logger):
    ctx = _context(_response("length", tool_calls=[_tool_call('{"path": "a.t')]))

    with pytest.raises(AbortError) as raised:
        await JiuSwarmStreamEventRail().after_model_call(ctx)

    reason = raised.value.reason
    assert "max_tokens=1024" in reason
    assert "output_tokens=800" in reason
    assert "write_file" in reason


@pytest.mark.asyncio
async def test_complete_tool_call_at_the_cap_is_logged_but_not_failed(rail_logger):
    ctx = _context(_response("length", tool_calls=[_tool_call('{"path": "a.txt"}')]))

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert len(rail_logger.warnings) == 1


@pytest.mark.asyncio
async def test_unset_cap_is_reported_as_unset(rail_logger):
    ctx = _context(_response("length"), max_tokens=None)

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert "max_tokens=unset" in rail_logger.warnings[0]


@pytest.mark.asyncio
async def test_gateway_cap_name_is_treated_as_truncation(rail_logger):
    ctx = _context(_response("max_tokens"))

    await JiuSwarmStreamEventRail().after_model_call(ctx)

    assert "finish_reason=max_tokens" in rail_logger.warnings[0]
