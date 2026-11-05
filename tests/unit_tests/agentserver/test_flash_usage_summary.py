# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash model reporting must preserve the shared usage-summary contract."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter


from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _accumulate_usage,
    _new_usage_accumulator,
)


def _adapter(runtime_model):
    adapter = JiuwenSwarmFlashAdapter.__new__(JiuwenSwarmFlashAdapter)
    adapter._model_request_config = SimpleNamespace(model_name="deepseek-v4.1-flash")
    adapter._instance = SimpleNamespace(
        _react_agent=SimpleNamespace(_config=SimpleNamespace(model_name=runtime_model))
    )
    return adapter


def _summary(adapter, *, with_usage=True, perf_usage_fallback=None):
    usage = _new_usage_accumulator()
    if with_usage:
        _accumulate_usage(usage, {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
    return adapter._log_and_make_usage_summary_chunk(
        request_id="r1",
        channel_id="officeclaw",
        session_id="flash-session",
        usage_accumulator=usage,
        perf_usage_fallback=perf_usage_fallback,
    )


@pytest.mark.parametrize("runtime_model", [None, "", "   ", 123])
def test_missing_runtime_model_keeps_requested_model(runtime_model):
    chunk = _summary(_adapter(runtime_model))
    assert chunk is not None
    assert chunk.payload["model"] == "deepseek-v4.1-flash"


def test_missing_runtime_instance_keeps_requested_model():
    adapter = _adapter(None)
    del adapter._instance
    chunk = _summary(adapter)
    assert chunk is not None
    assert chunk.payload["model"] == "deepseek-v4.1-flash"


def test_summary_keeps_usage_and_request_identity_without_changing_request_config():
    adapter = _adapter(" glm-5.3 ")
    chunk = _summary(adapter)
    assert chunk is not None
    assert chunk.request_id == "r1"
    assert chunk.channel_id == "officeclaw"
    assert chunk.payload["event_type"] == "chat.usage_summary"
    assert chunk.payload["session_id"] == "flash-session"
    assert chunk.payload["model"] == "glm-5.3"
    assert chunk.payload["usage"]["total_tokens"] == 12
    assert not chunk.is_complete
    assert adapter._resolve_model_name() == "deepseek-v4.1-flash"


def test_zero_usage_does_not_emit_summary():
    assert _summary(_adapter("glm-5.3"), with_usage=False) is None


def test_perf_usage_fallback_still_reports_runtime_model():
    chunk = _summary(
        _adapter("glm-5.3"),
        with_usage=False,
        perf_usage_fallback={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
    )
    assert chunk is not None
    assert chunk.payload["model"] == "glm-5.3"
    assert chunk.payload["usage"]["total_tokens"] == 23
