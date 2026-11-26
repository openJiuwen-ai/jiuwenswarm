# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
import weakref

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

from jiuwenswarm.acp.session_protocol import ACP_THINKING_PARAM
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_acp_thinking_applies_to_turn_and_resets_to_configured_default(monkeypatch):
    base = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_base="https://api.openai.com", api_key="test",
        ),
        model_config=ModelRequestConfig(model="gpt-5", reasoning={"mode": "enabled", "effort": "low"}),
    )
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._moa_runtime = None
    adapter._acp_thinking_model = None
    adapter._acp_thinking_bases = weakref.WeakKeyDictionary()
    adapter._last_resolved_model = base
    monkeypatch.setattr(adapter, "_resolve_base_model_for_request", lambda _: adapter._last_resolved_model)
    request = SimpleNamespace(channel_id="acp", params={ACP_THINKING_PARAM: "high"})

    adjusted = adapter._resolve_model_for_request(request)
    assert adjusted.model_config.reasoning == {"mode": "enabled", "effort": "high"}
    assert base.model_config.model_dump()["reasoning"]["effort"] == "low"
    assert adapter._resolve_model_for_request(request) is adjusted

    adapter._last_resolved_model = adjusted
    request.params[ACP_THINKING_PARAM] = None
    assert adapter._resolve_model_for_request(request) is base

    request.params[ACP_THINKING_PARAM] = "bogus"
    with pytest.raises(ValueError, match="no longer supported"):
        adapter._resolve_model_for_request(request)
    request.channel_id = "web"
    assert adapter._resolve_model_for_request(request) is adjusted


def test_acp_thinking_resets_an_older_override_after_an_unapplied_one(monkeypatch):
    base = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_base="https://api.openai.com", api_key="test",
        ),
        model_config=ModelRequestConfig(model="gpt-5", reasoning={"mode": "enabled", "effort": "low"}),
    )
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._moa_runtime = None
    adapter._acp_thinking_model = None
    adapter._acp_thinking_bases = weakref.WeakKeyDictionary()
    adapter._last_resolved_model = base
    monkeypatch.setattr(adapter, "_resolve_base_model_for_request", lambda _: adapter._last_resolved_model)
    request = SimpleNamespace(channel_id="acp", params={ACP_THINKING_PARAM: "high"})

    applied = adapter._resolve_model_for_request(request)
    adapter._last_resolved_model = applied
    # Resolved for another level but never applied: the remembered model is
    # still the older override, no longer the cached one.
    request.params[ACP_THINKING_PARAM] = "medium"
    assert adapter._resolve_model_for_request(request) is not applied

    request.params[ACP_THINKING_PARAM] = None
    assert adapter._resolve_model_for_request(request) is base
