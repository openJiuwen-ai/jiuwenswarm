# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP projection of the active model's reasoning controls."""

from __future__ import annotations

from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
    get_reasoning_capability,
)
from openjiuwen.core.foundation.llm.reasoning import (
    provider_identity,
    resolve_reasoning_plan,
    resolve_reasoning_protocol,
)
from pydantic import ValidationError

from jiuwenswarm.acp.session_protocol import DEFAULT_THINKING, THINKING_CONFIG_ID
from jiuwenswarm.common.reasoning_config import (
    ReasoningLevel,
    effective_endpoint_profile,
    normalize_reasoning_level,
    reasoning_config_for_level,
    reasoning_level_options,
)


def _levels(client: ModelClientConfig, model_name: str) -> tuple[str, ...]:
    return reasoning_level_options(get_reasoning_capability(
        provider=provider_identity(client),
        model=model_name,
        protocol=resolve_reasoning_protocol(client),
        api_base=client.api_base or "",
        endpoint_profile=effective_endpoint_profile(client.api_base, client.endpoint_profile),
    ))


def thinking_config_options(selection: str) -> list[dict]:
    from jiuwenswarm.common.config import get_default_models
    from jiuwenswarm.runtime.model_catalog import _resolve_configured_model_entry

    entry = _resolve_configured_model_entry(get_default_models(), selection)
    if entry is None:
        return []
    client_fields = dict(entry["model_client_config"])
    model_name = client_fields.pop("model_name")
    client_fields["client_provider"] = client_fields.get("client_provider") or "OpenAI"
    model_fields = dict(entry.get("model_config_obj") or {})
    model_fields["model"] = model_name
    try:
        client = ModelClientConfig(**client_fields)
        model_config = ModelRequestConfig(**model_fields)
    except ValidationError:
        return []
    levels = _levels(client, model_name)
    if not levels:
        return []

    probe_level = normalize_reasoning_level(levels[0])
    if probe_level is None:
        return []
    probe = _reasoning_override(model_config, probe_level)
    plan = resolve_reasoning_plan(client, probe)
    if not (plan.sdk_params or plan.extra_body):
        # Core preserves explicit legacy wire fields. Do not advertise an
        # override that would be ignored by the model client.
        return []
    configured = normalize_reasoning_level(model_fields.get("reasoning_level"))
    return [{
        "id": THINKING_CONFIG_ID,
        "category": "thought_level",
        "type": "select",
        "name": "Thinking",
        "currentValue": configured if configured in levels else DEFAULT_THINKING,
        "options": [
            {"value": level, "name": level.capitalize()} for level in (DEFAULT_THINKING, *levels)
        ],
    }]


def _reasoning_override(config: ModelRequestConfig, level: ReasoningLevel) -> ModelRequestConfig:
    return config.model_copy(update={"reasoning": reasoning_config_for_level(level)})


def apply_thinking_level(model: Model, level: str) -> Model:
    """Return a turn-local model with the requested reasoning configuration."""
    canonical = normalize_reasoning_level(level)
    unsupported = ValueError("Thinking level is no longer supported by the selected model")
    if canonical is None or canonical != level:
        raise unsupported
    if canonical not in _levels(model.model_client_config, model.model_config.model_name):
        raise unsupported
    config = _reasoning_override(model.model_config, canonical)
    plan = resolve_reasoning_plan(model.model_client_config, config)
    if not (plan.sdk_params or plan.extra_body):
        raise ValueError("Thinking configuration is unavailable for the selected model")
    return Model(model_client_config=model.model_client_config, model_config=config)
