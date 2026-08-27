# coding: utf-8
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwenswarm.common.reasoning_config import (
    LEVEL_MAPPING,
    ReasoningEffort,
    normalize_reasoning_level,
    resolve_reasoning_target,
    resolve_sampling_override,
)


def core_has_context_window_field() -> bool:
    """core 的 ``ModelRequestConfig`` 是否已声明 ``context_window`` 正式字段。

    用于决定 jiuwenswarm 是否需要在出口 pop 掉 ``context_window``
    （任意模型条目的，含 defaults / agentos / video / audio / vision / image_gen）：

    - **False（过渡期，core 未加正式字段）**：``context_window`` 进
      ``ModelRequestConfig`` 的 extra，core 的
      ``base_model_client._build_request_params`` 会把 extra 经
      ``model_dump(exclude={model_name,model,temperature,top_p,max_tokens,stop})``
      透传给厂商 SDK（base_model_client.py:444-449），SDK 不认该 kwarg 报
      unexpected keyword argument -> jiuwenswarm 需在出口 pop 防发厂商。
    - **True（core 已加正式字段）**：``context_window`` 作为正式字段进
      ``ModelRequestConfig``，core 自行决定是否发厂商（按 core 现有 max_tokens
      模式，"不发厂商"语义的字段会被 core 纳入上述 exclude、不进 params），
      ``self.model_config.context_window`` 可被 core 读取 -> jiuwenswarm
      **不得** pop，否则会切掉 core 想读的值。

    自动适配 core 两种状态，无需 jiuwenswarm 与 core 人工同步去 pop。
    """
    try:
        from openjiuwen.core.foundation.llm.schema.config import ModelRequestConfig
        return "context_window" in ModelRequestConfig.model_fields
    except Exception:
        # core 不可用（如独立测试环境）按过渡期处理：pop 防发厂商
        return False


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
    # 强制采样参数覆盖:某些厂商(如 Moonshot/api.moonshot.cn 的 kimi-k2.6)
    # 对 temperature/top_p 有硬性约束,传 core 默认值(0.95)或用户填的任意其它值
    # 都会 400。此处按 api_base 识别后强制写死,无视用户填值——因为传别的必死,
    # 无协商余地。必须早于 reasoning 的 level early-return,否则无 reasoning_level
    # 的普通调用(绝大多数)不会走到下面的 target 注入分支。
    override = resolve_sampling_override(
        model_client_config.get("api_base") or model_client_config.get("base_url")
    )
    if override:
        runtime_model_config.update(override)
    if level is None:
        return runtime_model_config

    target = resolve_reasoning_target(
        client_provider=model_client_config.get("client_provider"),
        endpoint_profile=model_client_config.get("endpoint_profile"),
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
    # _source 是 jiuwenswarm 内部标记（如 agentos 备份模型），不得进入 core 的
    # ModelRequestConfig；core 侧 extra="allow" 会静默收下它，但下游 SDK 调
    # AsyncCompletions.create(**params) 时不认该 kwarg 会抛 "unexpected keyword
    # argument"。统一在此清理，覆盖 build_model_from_entry / config.validate_model
    # / image_modality_warmup 等所有走本函数的路径。
    request_kwargs.pop("_source", None)
    # context_window（模型支持的上下文总长度）可配在任意模型条目的 model_config_obj
    # 里（defaults / agentos / video / audio / vision / image_gen 均可），供 core
    # 从 ModelRequestConfig 取值。是否在出口 pop 取决于 core 是否已把 context_window
    # 加为 ModelRequestConfig 正式字段（见 core_has_context_window_field）：
    # - core 未加字段（过渡期）：context_window 进 extra，会被 base_model_client
    #   经 model_dump 透传给厂商 SDK 报 unexpected keyword argument -> 需 pop。
    # - core 已加字段：context_window 作正式字段，core 自行 exclude 不发厂商、
    #   self.model_config.context_window 可读 -> 不得 pop（否则切掉 core 想读的值）。
    if "context_window" in request_kwargs:
        if not core_has_context_window_field():
            request_kwargs.pop("context_window", None)
        else:
            raw_context_window = request_kwargs.get("context_window")
            if raw_context_window is not None:
                try:
                    normalized_context_window = (
                        None
                        if isinstance(raw_context_window, bool)
                        else int(raw_context_window)
                    )
                except (TypeError, ValueError):
                    normalized_context_window = None
                if normalized_context_window is None or normalized_context_window <= 0:
                    request_kwargs.pop("context_window", None)
                else:
                    request_kwargs["context_window"] = normalized_context_window
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
