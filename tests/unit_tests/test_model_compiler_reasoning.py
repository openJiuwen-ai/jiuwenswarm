"""Model-selection compilation must translate Swarm's internal reasoning hint."""

from copy import deepcopy
from inspect import signature
from typing import Any

import pytest
from openai.resources.chat.completions import AsyncCompletions
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient

from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedRoute
from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection


def _model(request_defaults: dict[str, Any]) -> ResolvedModel:
    return ResolvedModel(
        model_id="mdl_glm",
        source="defaults",
        model_name="GLM-5.3",
        provider="OpenAI",
        api_base="https://example.com/v1",
        api_key="test-key",
        endpoint_profile="zhipu",
        request_defaults=request_defaults,
    )


@pytest.mark.parametrize(
    "level,expected",
    [("high", {"mode": "enabled", "effort": "high"}), ("off", {"mode": "disabled"}),
     (False, {"mode": "disabled"}), ("", None)],
)
@pytest.mark.parametrize("selection_type", ["model", "model_group"])
def test_compiled_reasoning_reaches_sdk_as_provider_controls(
    level: str | bool,
    expected: dict[str, str] | None,
    selection_type: str,
) -> None:
    resolved = _model({"reasoning_level": level, "temperature": 0.7, "context_window": 200000})
    if selection_type == "model_group":
        resolved = ResolvedModelGroup(
            model_group_id="mgp_glm",
            routes=[ResolvedRoute(route_id="primary", model=resolved)],
        )
    before = resolved.model_dump()

    compiled = compile_model_selection(resolved)
    request = compiled.model_request_config.model_dump(exclude_none=True)

    assert "reasoning_level" not in request
    assert request.get("reasoning") == expected
    assert request["temperature"] == 0.7
    assert request["context_window"] == 200000
    assert resolved.model_dump() == before

    client = OpenAIModelClient(compiled.model_request_config, compiled.model_client_config)
    params = client._build_request_params(
        messages=[{"role": "user", "content": "Hi"}],
        tools=None,
        temperature=None,
        top_p=None,
        model=None,
        stop=None,
        max_tokens=None,
        stream=True,
    )
    # Validate against the real SDK signature without sending a model request.
    signature(AsyncCompletions.create).bind(None, **params)
    assert "reasoning_level" not in params
    if expected is not None:
        expected_type = "disabled" if expected["mode"] == "disabled" else "enabled"
        assert params["extra_body"]["thinking"]["type"] == expected_type


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("override", [{}, {"reasoning_level": "off"}])
def test_group_reasoning_overrides_are_normalized_before_core_merges(
    routed: bool,
    override: dict[str, str],
) -> None:
    resolved = ResolvedModelGroup(
        model_group_id="mgp_glm",
        routes=[ResolvedRoute(
            route_id="primary",
            model=_model({"reasoning_level": "off", "temperature": 0.7}),
            request_overrides=override,
        )],
        request_config={"reasoning_level": "high", "temperature": 0.5},
        routing={"strategy": "ordered-failover"} if routed else {},
    )
    before = deepcopy(resolved.model_dump())

    compiled = compile_model_selection(resolved)

    if routed:
        deployments = compiled.model_client_config.intelli_router.deployments
        request = deployments[0].request_defaults
        expected_type = "disabled" if override else "enabled"
        assert request["thinking"]["type"] == expected_type
        assert "reasoning" not in request
    else:
        request = compiled.model_request_config.model_dump(exclude_none=True)
        assert request["reasoning"]["mode"] == ("disabled" if override else "enabled")
    assert "reasoning_level" not in request
    assert request["temperature"] == 0.5
    assert resolved.model_dump() == before
