# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    build_model_from_entry,
)


def _adapter():
    adapter = JiuWenSwarmDeepAdapter()
    model = build_model_from_entry(
        {"model_name": "test-model", "client_provider": "OpenAI",
         "api_base": "https://example.invalid/v1", "api_key": "test-only"}, {},
    )
    engine = SimpleNamespace(rebind_context_model=MagicMock(return_value=1))
    adapter._instance = SimpleNamespace(
        _react_agent=SimpleNamespace(
            set_llm=MagicMock(), _config=SimpleNamespace(), context_engine=engine,
        ),
    )
    return adapter, model, engine


@pytest.mark.parametrize("rebound", [0, 1])
def test_repeated_request_skips_tokenizer_rebinding(rebound):
    adapter, model, engine = _adapter()
    engine.rebind_context_model.return_value = rebound
    for _ in range(50):
        adapter._apply_model_to_react_agent(model, session_id="session-a")
    assert engine.rebind_context_model.call_count == 1
    assert adapter._active_request_model is model


@pytest.mark.parametrize("change", ["session", "model_config", "client_config", "context_config", "engine", "model"])
def test_changed_binding_is_applied(change):
    adapter, model, engine = _adapter()
    adapter._apply_model_to_react_agent(model, session_id="session-a")
    session = "session-a"
    if change == "session":
        session = "session-b"
    elif change == "model_config":
        model.model_config.model_name = "other-model"
    elif change == "client_config":
        model.model_client_config.api_base = "https://other.invalid/v1"
    elif change == "context_config":
        adapter._config_cache = {"context_engine_config": {"context_window_tokens": 123456}}
    elif change == "engine":
        engine = SimpleNamespace(rebind_context_model=MagicMock(return_value=1))
        adapter._instance._react_agent.context_engine = engine
    elif change == "model":
        _other_adapter, model, _other_engine = _adapter()
    adapter._apply_model_to_react_agent(model, session_id=session)
    assert engine.rebind_context_model.call_count == (1 if change == "engine" else 2)


def test_failed_binding_can_retry():
    adapter, model, engine = _adapter()
    engine.rebind_context_model.side_effect = [RuntimeError("injected binding failure"), 1]
    adapter._apply_model_to_react_agent(model, session_id="session-a")
    adapter._apply_model_to_react_agent(model, session_id="session-a")
    assert engine.rebind_context_model.call_count == 2


def test_failed_model_switch_does_not_reuse_the_previous_binding_snapshot():
    adapter, model, engine = _adapter()
    _other_adapter, other_model, _other_engine = _adapter()
    engine.rebind_context_model.side_effect = [1, RuntimeError("partially rebound"), 1]
    adapter._apply_model_to_react_agent(model, session_id="session-a")
    adapter._apply_model_to_react_agent(other_model, session_id="session-a")
    adapter._apply_model_to_react_agent(model, session_id="session-a")
    assert engine.rebind_context_model.call_count == 3
