# coding: utf-8
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwenswarm.common.reasoning_config import (
    LEVEL_MAPPING,
    ReasoningEffort,
    normalize_reasoning_level,
    resolve_reasoning_target,
)


def _model_config_to_dict(model_config_obj: Any) -> dict[str, Any]:
    if model_config_obj is None:
        return {}
    if isinstance(model_config_obj, dict):
        return dict(model_config_obj)
    if hasattr(model_config_obj, "model_dump"):
        return model_config_obj.model_dump(exclude_none=True)
    if isinstance(model_config_obj, Mapping):
        return dict(model_config_obj)
    return {}


def _resolve_model_name(model_name: str, model_config_obj: Any) -> str:
    if model_name:
        return str(model_name).strip()
    if isinstance(model_config_obj, Mapping):
        configured_name = model_config_obj.get("model") or model_config_obj.get("model_name")
        return str(configured_name or "").strip()
    return ""


def _copy_extra_body(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _runtime_config_copy(model_config_dict: dict[str, Any]) -> dict[str, Any]:
    runtime_model_config = dict(model_config_dict)
    # Internal hint only; must not be sent as a raw OpenAI SDK parameter.
    runtime_model_config.pop("reasoning_level", None)
    return runtime_model_config


def inject_deepseek_official_payload(
    model_config_obj: dict[str, Any],
    mapped_level: ReasoningEffort,
) -> None:
    model_config_obj.pop("reasoning_effort", None)

    extra_body = _copy_extra_body(model_config_obj.get("extra_body"))
    extra_body["thinking"] = {
        "type": "disabled" if mapped_level == "off" else "enabled",
    }
    model_config_obj["extra_body"] = extra_body

    if mapped_level == "high":
        model_config_obj["reasoning_effort"] = mapped_level


def inject_dashscope_bailian_payload(
    model_config_obj: dict[str, Any],
    mapped_level: ReasoningEffort,
) -> None:
    model_config_obj.pop("reasoning_effort", None)

    extra_body = _copy_extra_body(model_config_obj.get("extra_body"))
    extra_body["enable_thinking"] = mapped_level != "off"
    model_config_obj["extra_body"] = extra_body

    if mapped_level == "high":
        model_config_obj["reasoning_effort"] = mapped_level


def inject_reasoning_params(
    *,
    model_client_config: dict[str, Any],
    model_config_obj: Any,
) -> dict[str, Any]:
    model_config_dict = _model_config_to_dict(model_config_obj)
    level = normalize_reasoning_level(model_config_dict.get("reasoning_level"))
    runtime_model_config = _runtime_config_copy(model_config_dict)
    if level is None:
        return runtime_model_config

    target = resolve_reasoning_target(
        client_provider=model_client_config.get("client_provider"),
        api_base=(
            model_client_config.get("api_base")
            or model_client_config.get("base_url")
        ),
        model_name=model_client_config.get("model_name"),
    )
    if target is None:
        return runtime_model_config

    provider_kind, _model = target
    mapped_level = LEVEL_MAPPING[level]

    if provider_kind == "deepseek_official":
        inject_deepseek_official_payload(runtime_model_config, mapped_level)
    elif provider_kind == "dashscope_bailian":
        inject_dashscope_bailian_payload(runtime_model_config, mapped_level)

    return runtime_model_config


def _build_model_request_kwargs(
    *,
    model_name: str,
    model_config_obj: Any,
) -> dict[str, Any]:
    request_kwargs = _model_config_to_dict(model_config_obj)
    request_kwargs.pop("model", None)
    request_kwargs.pop("model_name", None)
    request_kwargs.pop("reasoning_level", None)
    request_kwargs["model"] = _resolve_model_name(model_name, model_config_obj)
    return request_kwargs


def build_reasoning_model_request_kwargs(
    *,
    model_client_config: dict[str, Any],
    model_config_obj: Any,
    model_name: str,
) -> dict[str, Any]:
    effective_model_name = _resolve_model_name(model_name, model_config_obj)
    reasoning_client_config = dict(model_client_config or {})
    if effective_model_name:
        reasoning_client_config["model_name"] = effective_model_name
    runtime_model_config = inject_reasoning_params(
        model_client_config=reasoning_client_config,
        model_config_obj=model_config_obj,
    )
    return _build_model_request_kwargs(
        model_name=effective_model_name,
        model_config_obj=runtime_model_config,
    )


__all__ = [
    "build_reasoning_model_request_kwargs",
    "inject_dashscope_bailian_payload",
    "inject_deepseek_official_payload",
    "inject_reasoning_params",
]
