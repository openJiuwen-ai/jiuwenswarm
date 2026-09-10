from jiuwenswarm.common.reasoning_injector import (
    build_reasoning_model_request_kwargs,
)


def _build(model_config_obj):
    return build_reasoning_model_request_kwargs(
        model_client_config={"client_provider": "OpenAI"},
        model_config_obj=model_config_obj,
        model_name="test-model",
    )


def test_missing_generation_values_use_jiuwenswarm_defaults():
    request = _build({})

    assert request["temperature"] == 0.95
    assert request["top_p"] == 0.9


def test_agentos_model_uses_the_same_generation_defaults():
    request = _build({"_source": "agentos"})

    assert request["temperature"] == 0.95
    assert request["top_p"] == 0.9
    assert "_source" not in request


def test_explicit_generation_values_take_precedence():
    request = _build({"temperature": 0.2, "top_p": 0.7})

    assert request["temperature"] == 0.2
    assert request["top_p"] == 0.7
