"""LLM resolution helpers for the prompt optimizer.

Resolves jiuwenswarm's configured default model into the agent-core LLM types
the RLAF-P algorithm (``openjiuwen.dev_tools.tune.optimizer.prompt_search``)
actually consumes — a ready ``Model`` for collaborators that take one directly
(policy, environment, drift judge, history compressor), or a raw
``(ModelRequestConfig, ModelClientConfig)`` pair for collaborators that build
their own ``Model`` internally (``DefaultEvaluator``).
"""

from __future__ import annotations

from dataclasses import replace

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

from jiuwenswarm.symphony.llm import LLMConfig


def resolve_llm_config(model_name: str = "", *, temperature: float | None = None) -> LLMConfig:
    """Resolve an :class:`LLMConfig`, optionally overriding model and temperature.

    ``model_name`` empty -> the JiuwenSwarm default model. When provided, only the
    model identifier is swapped; api_base / api_key / provider stay the default's,
    which matches how a single gateway serves multiple model names.
    """

    config = LLMConfig.from_default_model()
    name = str(model_name or "").strip()
    if name:
        request_config = dict(config.model_config_obj or {})
        request_config["model"] = name
        config = replace(config, model=name, model_config_obj=request_config)
    if temperature is not None:
        config = replace(config, temperature=float(temperature))
    return config


def build_model(model_name: str = "", *, temperature: float | None = None) -> Model:
    """Resolve config and return a ready agent-core ``Model``."""

    return resolve_llm_config(model_name, temperature=temperature).create_model()


def build_model_configs(
    model_name: str = "", *, temperature: float | None = None
) -> tuple[ModelRequestConfig, ModelClientConfig]:
    """Resolve config and return raw ``(ModelRequestConfig, ModelClientConfig)``.

    For collaborators like ``DefaultEvaluator`` that build their own ``Model``
    rather than accepting one.
    """

    config = resolve_llm_config(model_name, temperature=temperature)
    return (
        ModelRequestConfig(**config.model_request_kwargs()),
        ModelClientConfig(**config.model_client_kwargs()),
    )


__all__ = ["resolve_llm_config", "build_model", "build_model_configs"]
