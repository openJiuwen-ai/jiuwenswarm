# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Web 侧 models/config 域 handler 下沉实现（自 gateway app_web_handlers 迁出）。

gateway 拆分：ConfigAdapter（AgentServer 进程）复用这些成熟实现时不再 import
``gateway.channel_manager.web.app_web_handlers``。gateway 侧 ``_register_web_handlers``
通过 ``jiuwenswarm.common.config_panel.models_handlers`` 间接消费同一实现，
保持单一实现源（过渡期 gateway 仓持 common 副本）。

对外入口：
- ``register_models_handlers(channel, *, on_config_saved=None, agent_client=None)``：
  在给定 channel 上注册 ``models.list`` / ``models.replace_all`` /
  ``config.validate_model`` / ``models.validate``（含 AgentOS 多用户 E2A 代理
  分叉语义——由 gateway 侧 ``_register_config_proxy`` 包装，本模块仅提供
  本地 handler）。
- 纯函数（``build_models_defaults_from_frontend`` 等）供单测与 TUI/Web 通道
  模块复用。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from openjiuwen.core.foundation.llm import Model, ProviderType
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.utils.provider_utils import is_openai_account_provider

from jiuwenswarm.common.auth.model_catalog import is_login_model
from jiuwenswarm.common.config import (
    _ensure_model_business_ids,
    get_config,
    get_config_raw,
    get_available_models,
    get_default_models,
    update_default_models_in_config,
    update_kv_cache_affinity_enabled_in_config,
)
from jiuwenswarm.common.context_window import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    parse_positive_int,
)
from jiuwenswarm.common.kv_cache_affinity_config import (
    default_model_client_config_from_entries,
    has_kv_cache_affinity_capability,
    is_affinity_enabled,
)
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_config_validation import probe_model_connection, raise_if_invalid
from jiuwenswarm.common.reasoning_config import (
    effective_endpoint_profile,
    validate_reasoning_level_for_model,
)
from jiuwenswarm.common.reasoning_injector import (
    build_reasoning_model_request_kwargs,
    core_has_context_window_field,
)

logger = logging.getLogger(__name__)

_ENV_VAR_PLACEHOLDER_RE = re.compile(r"^\$\{([^:}]+)(?::-([^}]*))?\}$")


class ConfigPanelBadRequest(ValueError):
    """models/config 域参数校验失败（HTTP 语义 400）。"""


# --------------------------------------------------------------------------- #
# 纯工具函数（自 app_web_handlers 迁出，与 TUI 侧实现保持契约一致）
# --------------------------------------------------------------------------- #
def _is_env_var_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(_ENV_VAR_PLACEHOLDER_RE.match(value.strip()))


def _values_match(parsed_val: Any, resolved_val: Any) -> bool:
    """Compare a frontend-sent value against the resolved value of a model entry.

    Numeric and stringified env-var output (e.g. ``${TEMP:-0.95}`` resolves to ``"0.95"``)
    are normalized so that ``0.95 == "0.95"`` is treated as "unchanged".
    """
    if isinstance(parsed_val, bool) or isinstance(resolved_val, bool):
        return bool(parsed_val) == bool(resolved_val)
    if parsed_val is None and resolved_val is None:
        return True
    try:
        return float(parsed_val) == float(resolved_val)
    except (TypeError, ValueError):
        pass
    return str(parsed_val if parsed_val is not None else "") == str(
        resolved_val if resolved_val is not None else ""
    )


def _serialize_reasoning_level(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    # Always emit a quoted YAML string so the same field never round-trips
    # as a mix of plain scalars and quoted scalars.
    return DoubleQuotedScalarString(text)


def reasoning_level_display(value: Any) -> str:
    """Normalize a stored reasoning_level to its canonical string level.

    Legacy YAML entries hold bare ``on``/``off`` scalars which YAML 1.1
    loaders parse into booleans; map them back so the frontend and the
    replace_all change detection never see raw booleans.
    """
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value or "").strip()


# 供旧调用方/单测引用的兼容别名（原 app_web_handlers 私有符号）
is_env_var_placeholder = _is_env_var_placeholder  # noqa: F401
values_match = _values_match  # noqa: F401
_reasoning_level_display = reasoning_level_display  # noqa: F401


def normalize_provider_value(value: str) -> str:
    """把任意大小写的 provider 值归一化为 ``ProviderType`` 规范大小写。

    与 TUI 侧 ``tui_connect._normalize_provider_value`` 保持一致：TUI 在
    写入/校验 model_provider 时会做大小写归一化，Web 侧此前没有做，导致同一
    份配置（例如历史数据里 model_provider 大小写不规范）在 TUI 能正常识别，
    但 Web 的"测试"/"保存"因大小写敏感的精确匹配而被误判为非法值。
    """
    normalized = value.strip()
    if not normalized:
        return normalized

    available_model_providers = [provider.value for provider in ProviderType]
    lookup = {provider.lower(): provider for provider in available_model_providers}
    return lookup.get(normalized.lower(), normalized)


def resolve_model_config_obj_for_validate(
    model_name: str, params: dict[str, Any]
) -> dict[str, Any]:
    """从热更新后的配置中查找对应模型的 ``model_config_obj``。

    按 ``model_name`` 或 ``alias`` 匹配 ``models.defaults`` 中的条目，
    返回该条目的 ``model_config_obj``；找不到或读取失败时 fallback 到
    空字典（让模型使用自身默认参数，避免 ``temperature=0`` 对部分
    模型不兼容）。前端传入的 ``reasoning_level`` 可覆盖配置值。
    """
    model_config_obj: dict[str, Any] = {}
    try:
        _cfg = get_config()
        _models = get_default_models(_cfg)
        for entry in _models:
            if not isinstance(entry, dict):
                continue
            mcc = entry.get("model_client_config") or {}
            entry_model_name = str(mcc.get("model_name", "")).strip()
            entry_alias = str(entry.get("alias", "")).strip()
            if entry_model_name == model_name or entry_alias == model_name:
                obj = entry.get("model_config_obj")
                if isinstance(obj, dict):
                    model_config_obj = dict(obj)
                # context_window（模型支持的上下文总长度）可配在任意模型条目的
                # model_config_obj 里（defaults / agentos / video / audio / vision /
                # image_gen 均可），供 core 从 ModelRequestConfig 取值。是否在出口
                # 清掉取决于 core 是否已把 context_window 加为 ModelRequestConfig
                # 正式字段（见 reasoning_injector.core_has_context_window_field）：
                # - core 未加字段（过渡期）：context_window 进 extra 会被
                #   base_model_client 经 model_dump 透传给厂商 SDK 报 unexpected
                #   keyword argument -> 需清。
                # - core 已加字段：context_window 作正式字段，core 自行 exclude
                #   不发厂商、可读 -> 不清（否则切掉 core 想读的值）。
                # 不再守 _source=="agentos"：所有条目一视同仁，defaults 配了
                # context_window 同样需要过渡期清防发厂商。_source 标记本身由
                # reasoning_injector._build_model_request_kwargs 统一 pop；此处与
                # 公共出口同口径，覆盖绕过 build_model_from_entry 的 validate 路径。
                if not core_has_context_window_field():
                    model_config_obj.pop("context_window", None)
                logger.info(
                    "[config.validate_model] loaded model_config_obj for '%s' "
                    "(matched_by=%s): %s",
                    model_name,
                    "model_name" if entry_model_name == model_name else "alias",
                    model_config_obj,
                )
                break
        else:
            logger.info(
                "[config.validate_model] no model_config_obj found for '%s', using empty default",
                model_name,
            )
    except Exception as exc:
        logger.warning(
            "[config.validate_model] failed to read model_config_obj from config, using default. %s",
            exc,
        )
    if "reasoning_level" in params:
        model_config_obj["reasoning_level"] = params.get("reasoning_level")
    return model_config_obj


def _merge_models_for_replace_all(
    parsed: list[dict[str, Any]],
    raw_defaults: list[dict[str, Any]],
    resolved_defaults: list[dict[str, Any]],
    crypto: Any,
) -> list[dict[str, Any]]:
    """Merge the frontend draft with the persisted YAML so that env-var placeholders
    (``${VAR:-default}``) survive when the user edits unrelated fields.

    For each frontend entry that carries an ``origin_index`` pointing at a still-existing
    persisted entry, we deep-copy the raw entry (preserving placeholders, custom_headers,
    etc.) and only overwrite the fields whose value differs from the resolved snapshot
    the frontend was originally shown. New entries (no ``origin_index``) fall back to
    encrypting/storing the frontend payload verbatim.
    """
    import copy as _copy

    out: list[dict[str, Any]] = []
    for item in parsed:
        origin_idx = item.get("origin_index")
        raw_entry = None
        resolved_entry = None
        if isinstance(origin_idx, int) and 0 <= origin_idx < len(raw_defaults):
            raw_entry = raw_defaults[origin_idx]
            if 0 <= origin_idx < len(resolved_defaults):
                resolved_entry = resolved_defaults[origin_idx]

        if raw_entry is not None and isinstance(raw_entry, dict):
            new_entry = _copy.deepcopy(raw_entry)
            new_mcc = new_entry.setdefault("model_client_config", {})
            new_mco = new_entry.setdefault("model_config_obj", {})
            resolved_mcc = (resolved_entry or {}).get("model_client_config", {}) or {}
            resolved_mco = (resolved_entry or {}).get("model_config_obj", {}) or {}

            if not _values_match(item["model_name"], resolved_mcc.get("model_name")):
                new_mcc["model_name"] = item["model_name"]
            if not _values_match(item["api_base"], resolved_mcc.get("api_base")):
                new_mcc["api_base"] = item["api_base"]
            # client_provider: 当 YAML 仍是 ${MODEL_PROVIDER} 占位符时，其解析值会与前端
            # 选择（如 OpenAI）一致而被误判为"未改"，导致首次配置后占位符残留。只要原值是
            # 占位符就用前端值固化它。
            if item["model_provider"] and (
                _is_env_var_placeholder(new_mcc.get("client_provider"))
                or not _values_match(item["model_provider"], resolved_mcc.get("client_provider"))
            ):
                new_mcc["client_provider"] = item["model_provider"]
            if item["temperature"] is None:
                new_mco.pop("temperature", None)
            elif not _values_match(item["temperature"], resolved_mco.get("temperature")):
                new_mco["temperature"] = item["temperature"]
            reasoning_level = str(item.get("reasoning_level") or "").strip()
            # 不能用 _values_match：legacy YAML 1.1 会把裸 on/off 读成布尔，
            # 其布尔分支使 bool("")==bool(False) 成立，「清空档位」会被误判为
            # 未修改而让旧值残留。按规范化后的字符串比较。
            if reasoning_level != reasoning_level_display(resolved_mco.get("reasoning_level")):
                if reasoning_level:
                    new_mco["reasoning_level"] = _serialize_reasoning_level(reasoning_level)
                else:
                    new_mco.pop("reasoning_level", None)
            if item.get("context_window_tokens_provided"):
                new_mco["context_window"] = item["context_window_tokens"]
            if not _values_match(item["timeout"], resolved_mcc.get("timeout")):
                new_mcc["timeout"] = item["timeout"]
            if not _values_match(item["alias"], (resolved_entry or {}).get("alias")):
                new_entry["alias"] = item["alias"]
            # vendor_key + plan: persist the exact provider selection identity
            # into model_client_config (or clear it).
            if item.get("vendor_key"):
                new_mcc["vendor_key"] = item["vendor_key"]
            else:
                new_mcc.pop("vendor_key", None)
            if item.get("plan"):
                new_mcc["plan"] = item["plan"]
            else:
                new_mcc.pop("plan", None)
            # endpoint_profile: OpenAI 协议端点方言(deepseek/openrouter/dashscope/...)。
            # 前端透传则落库；不传则清掉(避免残留旧方言)。Anthropic 协议时此字段被 core 忽略。
            if item.get("endpoint_profile"):
                new_mcc["endpoint_profile"] = item["endpoint_profile"]
            else:
                new_mcc.pop("endpoint_profile", None)
            new_entry["is_default"] = item["is_default"]
            # api_key: resolved holds the decrypted plaintext shown to the frontend.
            # Unchanged → keep raw (placeholder or ciphertext); changed → encrypt new value.
            if not _values_match(item["api_key"], resolved_mcc.get("api_key")):
                new_mcc["api_key"] = (
                    crypto.encrypt(item["api_key"]) if (item["api_key"] and crypto) else item["api_key"]
                )
        else:
            # New entry — frontend payload is the source of truth.
            new_entry = {
                "model_client_config": {
                    "api_base": item["api_base"],
                    "api_key": (
                        crypto.encrypt(item["api_key"]) if (item["api_key"] and crypto) else item["api_key"]
                    ),
                    "model_name": item["model_name"],
                    "client_provider": item["model_provider"],
                    "timeout": item["timeout"],
                    "verify_ssl": item["verify_ssl"],
                    # vendor_key + plan identify the exact registry preset so
                    # the UI can restore the provider selection after reload.
                    **({"vendor_key": item["vendor_key"]} if item.get("vendor_key") else {}),
                    **({"plan": item["plan"]} if item.get("plan") else {}),
                    # endpoint_profile: OpenAI 协议端点方言(透传；Anthropic 时 core 忽略)。
                    **({"endpoint_profile": item["endpoint_profile"]} if item.get("endpoint_profile") else {}),
                },
                "model_config_obj": {
                    "context_window": (
                        item["context_window_tokens"]
                        if item.get("context_window_tokens_provided")
                        else DEFAULT_CONTEXT_WINDOW_TOKENS
                    ),
                    **({"temperature": item["temperature"]} if item["temperature"] is not None else {}),
                    **({"reasoning_level": _serialize_reasoning_level(item.get("reasoning_level"))}
                       if item.get("reasoning_level") else {}),
                },
                "is_default": item["is_default"],
                "alias": item["alias"],
            }

        out.append(new_entry)
    return out


merge_models_for_replace_all = _merge_models_for_replace_all  # noqa: F401 — 兼容别名


def _get_crypto_provider() -> Any:
    """经 common 钩子取 crypto provider（未注册/未就绪返回 None）。

    gateway 进程由 ``extensions.registry`` 在 create_instance 时安装桥接；
    AgentServer 进程无 crypto 扩展时 api_key 以明文落库（与原实现一致）。
    """
    try:
        from jiuwenswarm.common.security.base_crypto import get_crypto_provider

        return get_crypto_provider()
    except Exception:  # noqa: BLE001
        return None


def build_models_defaults_from_frontend(raw_models: Any) -> list[dict[str, Any]]:
    """把前端提交的 models 列表解析/校验/合成为 ``models.defaults`` 新值。

    登录送的模型（is_login_model）与 agentos 备份条目不参与替换；带
    ``origin_index`` 的条目按"与解析快照 diff"合并以保留 YAML 占位符；
    新条目原样加密落库。
    """
    if not isinstance(raw_models, list):
        raise ConfigPanelBadRequest("models must be a non-empty list")
    # 登录送的模型是运行时叠加的，前端回传时要去掉，不能写进 config.yaml：
    # 它们的凭据会过期，换账号后模型也该跟着变。
    raw_models = [item for item in raw_models if not is_login_model(item)]
    if not raw_models:
        raise ConfigPanelBadRequest("models must be a non-empty list")

    available_model_providers = [p.value for p in ProviderType]
    parsed: list[dict] = []
    aliases_seen: dict[str, int] = {}
    for idx, item in enumerate(raw_models):
        if not isinstance(item, dict):
            raise ConfigPanelBadRequest(f"models[{idx}] must be object")
        # agentos 备份模型条目不参与 defaults 替换：前端置灰只读展示 agentos，
        # 提交的列表里不应含 agentos；此防御性过滤确保即便误传也不会把
        # agentos 当 defaults 写回 models.defaults（污染 config 结构、丢 _source 标记）
        if item.get("is_agentos") is True:
            continue
        model_name = str(item.get("model_name") or "").strip()
        if not model_name:
            raise ConfigPanelBadRequest(f"models[{idx}].model_name is required")
        origin_index_raw = item.get("origin_index")
        if origin_index_raw is None:
            origin_index = None
        else:
            try:
                origin_index = int(origin_index_raw)
            except (TypeError, ValueError):
                origin_index = None
        api_key = str(item.get("api_key") or "").strip()
        api_base = str(item.get("api_base") or "").strip()
        model_provider = normalize_provider_value(str(item.get("model_provider") or ""))
        # OpenAIAccount uses the token store managed by core OAuth, so it does not
        # carry a user-entered api_key in config.
        if not api_key and origin_index is None and not is_openai_account_provider(model_provider):
            raise ConfigPanelBadRequest(f"models[{idx}].api_key is required")
        if model_provider and model_provider not in available_model_providers:
            raise ConfigPanelBadRequest(
                f"models[{idx}].model_provider must be one of: {available_model_providers}"
            )
        raw_temperature = item.get("temperature")
        if raw_temperature is None or raw_temperature == "":
            temperature = None
        else:
            try:
                temperature = float(raw_temperature)
            except (ValueError, TypeError) as exc:
                raise ConfigPanelBadRequest(
                    f"models[{idx}].temperature must be a number or empty"
                ) from exc
        try:
            timeout = int(item.get("timeout", 1800))
        except (ValueError, TypeError):
            timeout = 1800
        verify_ssl = bool(item.get("verify_ssl", False))
        is_default = bool(item.get("is_default", False))
        alias = str(item.get("alias") or "").strip()
        # 原样透传给共享校验函数：不要用 `or ""` 压平，否则布尔 False
        # （legacy YAML 裸 off / 非前端客户端传的 JSON false）会被当成清空。
        raw_reasoning_level = item.get("reasoning_level")
        context_window_tokens_provided = "context_window_tokens" in item
        context_window_tokens = None
        if context_window_tokens_provided:
            context_window_tokens = parse_positive_int(item.get("context_window_tokens"))
            if context_window_tokens is None:
                raise ConfigPanelBadRequest(
                    f"models[{idx}].context_window_tokens must be a positive integer or a value such as 256K or 1M"
                )
        vendor_key = str(item.get("vendor_key") or "").strip() or None
        plan = str(item.get("plan") or "").strip() or None
        if plan:
            from jiuwenswarm.common.model_vendor_registry import PlanKind

            try:
                plan = PlanKind(plan).value
            except ValueError as exc:
                raise ConfigPanelBadRequest(
                    f"models[{idx}].plan must be one of: token_plan, coding_plan, custom_api"
                ) from exc
            if not vendor_key:
                raise ConfigPanelBadRequest(f"models[{idx}].vendor_key is required when plan is set")

        try:
            reasoning_level = validate_reasoning_level_for_model(
                raw_level=raw_reasoning_level,
                model_name=model_name,
                model_provider=model_provider,
                api_base=api_base,
                endpoint_profile=item.get("endpoint_profile"),
            )
        except ValueError as reasoning_err:
            raise ConfigPanelBadRequest(f"models[{idx}].{reasoning_err}") from reasoning_err

        if alias:
            if alias in aliases_seen:
                prev_idx = aliases_seen[alias]
                raise ConfigPanelBadRequest(
                    f"Alias '{alias}' is used by both models[{prev_idx}] and models[{idx}]"
                )
            aliases_seen[alias] = idx

        parsed.append({
            "model_name": model_name,
            "api_base": api_base,
            "api_key": api_key,
            "model_provider": model_provider,
            "temperature": temperature,
            "is_default": is_default,
            "timeout": timeout,
            "verify_ssl": verify_ssl,
            "alias": alias,
            "reasoning_level": reasoning_level or "",
            "context_window_tokens": context_window_tokens,
            "context_window_tokens_provided": context_window_tokens_provided,
            "origin_index": origin_index,
            # vendor_key is an opaque hint
            # selector; not validated (the selector only ever emits keys
            # present in jiuwenswarm.common.model_vendor_registry). It is
            # persisted so the UI can match a configured entry back to its
            # preset for icon display / re-selection. Not required.
            "vendor_key": vendor_key,
            # plan is the other half of the provider-selection identity.
            # Older entries may have vendor_key only; do not infer a plan.
            "plan": plan,
            # endpoint_profile: OpenAI 协议端点方言(deepseek/openrouter/dashscope/...);
            # opaque passthrough, not validated. Anthropic 协议时 core 忽略此字段。
            # 前端未传时按 api_base host 推断已知自建网关方言(如 vllm)并落库,
            # 否则该类端点的思考开关只会发官方 thinking.type 而被网关忽略。
            "endpoint_profile": effective_endpoint_profile(api_base, item.get("endpoint_profile")),
        })

    # alias 与其他条目的 model_name 冲突校验
    for i, p in enumerate(parsed):
        a = p["alias"]
        if not a:
            continue
        for j, q in enumerate(parsed):
            if i == j:
                continue
            if q["model_name"] == a:
                raise ConfigPanelBadRequest(
                    f"Alias '{a}' on models[{i}] conflicts with model_name on models[{j}]"
                )

    crypto = _get_crypto_provider()

    raw_cfg = get_config_raw()
    raw_defaults = raw_cfg.get("models", {}).get("defaults") if isinstance(raw_cfg, dict) else None
    if not isinstance(raw_defaults, list):
        raw_defaults = []
    resolved_defaults = get_default_models()

    new_models = _merge_models_for_replace_all(parsed, raw_defaults, resolved_defaults, crypto)
    from jiuwenswarm.common.config import _infer_is_default
    return _infer_is_default(new_models)


# --------------------------------------------------------------------------- #
# handler 实现（channel 语义与 app_web_handlers 原实现一致）
# --------------------------------------------------------------------------- #
async def config_validate_model_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    max_tokens_bounds: dict[str, Any] | None = None,
) -> None:
    """Send a minimal chat completion (user message "Hi") using draft default-model fields.

    Tries ``max_tokens=infimum_max_tokens`` first to limit cost. If the API
    rejects it or returns no content, retries with
    ``max_tokens=supremum_max_tokens``.
    """
    if max_tokens_bounds is None:
        max_tokens_bounds = {
            "infimum_max_tokens": 3,
            "supremum_max_tokens": 16,
        }

    if isinstance(max_tokens_bounds, dict):
        infimum_max_tokens = max_tokens_bounds.get("infimum_max_tokens")
        supremum_max_tokens = max_tokens_bounds.get("supremum_max_tokens")
    else:
        infimum_max_tokens = 3
        supremum_max_tokens = 16
    infimum_max_tokens = max(infimum_max_tokens, 3)

    if not isinstance(params, dict):
        await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
        return
    api_base = str(params.get("api_base") or "").strip()
    api_key = str(params.get("api_key") or "").strip()
    model = str(params.get("model") or "").strip()
    model_provider = normalize_provider_value(str(params.get("model_provider") or ""))
    needs_api_key = not is_openai_account_provider(model_provider)
    if not all([api_base, model, model_provider]) or (needs_api_key and not api_key):
        await channel.send_response(
            ws, req_id, ok=False,
            error="api_base, model, model_provider, and api_key for non-OAuth providers are required",
            code="BAD_REQUEST",
        )
        return
    available_model_providers = [provider.value for provider in ProviderType]
    if model_provider not in available_model_providers:
        await channel.send_response(
            ws, req_id, ok=False,
            error=f"Model provider must be one of: {available_model_providers}",
            code="BAD_REQUEST",
        )
        return
    api_base = api_base.rstrip("/")

    verify_ssl = bool(params.get("verify_ssl", False))
    # 未显式传方言时按 api_base host 推断已知自建网关(如 vllm)，
    # 保证"测试连接"与保存后的真实运行走同一条 core 路由。
    endpoint_profile = effective_endpoint_profile(api_base, params.get("endpoint_profile"))

    model_config_obj = resolve_model_config_obj_for_validate(model, params)

    reasoning_mcc = {
        "client_provider": model_provider,
        "endpoint_profile": endpoint_profile,
        "api_base": api_base,
    }
    model_request_config = ModelRequestConfig(
        **build_reasoning_model_request_kwargs(
            model_client_config=reasoning_mcc,
            model_config_obj=model_config_obj,
            model_name=model,
        )
    )
    logger.info(
        "[config.validate_model] final model_request_config for '%s': %s",
        model,
        model_request_config.model_dump(),
    )
    model_client_config = ModelClientConfig(
        client_id="config-validate",
        client_provider=model_provider,
        endpoint_profile=endpoint_profile,
        api_key=api_key,
        api_base=api_base,
        timeout=25.0,
        max_retries=0,
        verify_ssl=verify_ssl,
    )
    # Anthropic-compatible endpoints that send thinking.budget_tokens
    # require max_tokens > budget. Use the actual budget core would emit
    # (effort-mapped or explicit), not a stale 1024 default: many wires
    # (qwen38_anthropic, dashscope_budget) no longer pin 1024, while
    # anthropic_manual maps high → 16384.
    try:
        from openjiuwen.core.foundation.llm.reasoning import resolve_reasoning_plan

        _plan = resolve_reasoning_plan(
            model_client_config,
            model_request_config,
            request_model=model,
        )
        _thinking = (_plan.sdk_params or {}).get("thinking")
        if isinstance(_thinking, dict):
            _budget = _thinking.get("budget_tokens")
            if isinstance(_budget, int) and _budget > 0:
                supremum_max_tokens = max(supremum_max_tokens, _budget + 16)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[config.validate_model] skip budget floor from reasoning plan",
            exc_info=True,
        )
    llm = Model(model_config=model_request_config, model_client_config=model_client_config)

    try:
        await probe_model_connection(
            llm,
            token_limits=(infimum_max_tokens, supremum_max_tokens),
            log_context="config.validate_model",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[config.validate_model] LLM probe failed: %s", exc)
        await channel.send_response(
            ws, req_id, ok=False,
            error=str(exc).strip() or "LLM request failed",
            code="LLM_ERROR",
        )
        return

    await channel.send_response(
        ws, req_id, ok=True,
        payload={"ok": True, "model_provider": model_provider},
    )


async def models_list_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
) -> None:
    """返回已配置的所有默认模型列表（与 config.get 一致，返回解密后的完整值）。

    每条带 ``origin_index`` 指向 ``models.defaults`` 中的位置，配合 replace_all
    在保存时识别"未编辑字段"并保留原 YAML 占位符（如 ``${API_KEY}``）。

    **登录会话 id 必须从这条连接上取。** 免费模型是「这个登录用户」的模型，
    不传的话 ``get_available_models`` 只能退回「当前唯一登录会话」的假设——
    一旦机器上存在两个活跃会话（换个浏览器再登一次就够了），那个假设会拒绝
    猜是谁，于是登录了却一个免费模型都列不出来。
    """
    try:
        config = get_config()
        auth_session = getattr(ws, "_jiuwen_auth_session", "") or None
        # 放到线程池里跑：目录缓存过期或凭据要续期时这里会同步请求 APIG（超时 10～15 秒），
        # 在事件循环上跑会让整个 Gateway 的连接陪着等。
        # Without an authenticated browser session there are no per-user
        # models to merge.  Calling the local symbol directly also keeps
        # this handler compatible with callers that replace the configured
        # model provider (notably the Gateway unit-test seam).
        if auth_session is None:
            models = await asyncio.to_thread(get_default_models, config)
        else:
            models = await asyncio.to_thread(get_available_models, config, auth_session)
        result = []
        active_model = ""
        for idx, entry in enumerate(models):
            mcc = entry.get("model_client_config", {})
            mco = entry.get("model_config_obj", {})
            is_default = entry.get("is_default", False)
            model_name = str(mcc.get("model_name", "") or "").strip()
            result_entry = {
                # 模型稳定 ID（模型组路由依据）；由 _ensure_model_business_ids
                # 迁移/校验后注入，前端据此关联模型组与路由。
                "model_id": entry.get("model_id", ""),
                "model_name": model_name,
                "api_base": mcc.get("api_base", ""),
                # 凭据绝不能随列表下发到浏览器
                "api_key": "" if is_login_model(entry) else mcc.get("api_key", ""),
                "model_provider": mcc.get("client_provider", ""),
                "temperature": mco.get("temperature"),
                "reasoning_level": reasoning_level_display(mco.get("reasoning_level")),
                "is_default": is_default,
                # agentos 备份模型标记：由 get_default_models 经 _source=="agentos"
                # 注入。前端据此区分 defaults / agentos，置灰只读展示 agentos、
                # 并让 agentos 进 ModelSelector 下拉（is_default!==false || is_agentos）
                "is_agentos": bool(mco.get("_source") == "agentos"),
                # 免费模型标记：前端据此归进「免费模型」分组、并从设置页的模型配置里滤掉。
                # 下面追加 Zen 模型那段设的是同一个字段，**两处必须一致**。
                "is_free": bool(entry.get("is_free")),
                "alias": entry.get("alias", ""),
                "origin_index": idx,
                "vendor_key": mcc.get("vendor_key") or entry.get("vendor_key") or "",
                "plan": mcc.get("plan") or entry.get("plan") or "",
                "endpoint_profile": mcc.get("endpoint_profile") or "",
            }
            # An empty template entry is not a configured model yet; do
            # not surface a synthetic context window until the user saves
            # the model configuration.
            if model_name:
                result_entry["context_window_tokens"] = (
                    parse_positive_int(mco.get("context_window"))
                    or DEFAULT_CONTEXT_WINDOW_TOKENS
                )
            if is_login_model(entry):
                # 登录送的模型：前端据此置灰编辑
                result_entry.update(source=entry.get("source"), read_only=True)
            result.append(result_entry)
        # Zen 免费模型仅存在于进程内缓存，不能写回 models.defaults；但需要
        # 与普通模型一同出现在会话选择器中。is_default 保持 None（而不是
        # False），使前端把它视为可选模型，同时不会改变首个配置模型作为
        # active_model 的既有语义。
        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )

            existing_names = {str(item.get("model_name") or "") for item in result}
            for entry in get_zen_free_model_entries():
                mcc = entry.get("model_client_config") or {}
                mco = entry.get("model_config_obj") or {}
                model_name = str(mcc.get("model_name") or "").strip()
                if not model_name or model_name in existing_names:
                    continue
                result.append({
                    "model_name": model_name,
                    "api_base": mcc.get("api_base", ""),
                    "api_key": mcc.get("api_key", ""),
                    "model_provider": mcc.get("client_provider", ""),
                    "temperature": mco.get("temperature"),
                    "reasoning_level": reasoning_level_display(mco.get("reasoning_level")),
                    "is_default": entry.get("is_default"),
                    "is_agentos": False,
                    "is_free": True,
                    "alias": entry.get("alias", ""),
                    # Preserve an explicit context-window value supplied by
                    # the runtime cache; otherwise use the shared default.
                    "context_window_tokens": (
                        parse_positive_int(entry.get("context_window_tokens"))
                        or parse_positive_int(mco.get("context_window"))
                        or DEFAULT_CONTEXT_WINDOW_TOKENS
                    ),
                })
                existing_names.add(model_name)
        except Exception:
            logger.warning("[models.list] append Zen free models failed", exc_info=True)

        # active_model 为列表首位的模型（主对话默认）
        active_model = result[0]["model_name"] if result else ""
        await channel.send_response(ws, req_id, ok=True, payload={
            "models": result,
            "model_groups": ModelCatalog(config).list_public_groups(),
            "active_model": active_model,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("[models.list] %s", exc)
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")


async def models_replace_all_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
) -> None:
    """原子地用提交的列表整体替换 models.defaults。

    前端在保存配置时一次性提交完整的最终列表，避免按 model_name/index 分多步
    save+remove 在同 model_name 多条目场景下出现的位置覆写、漏删等问题。

    每条 entry 可携带 ``origin_index`` 指向 ``models.defaults`` 中的原始位置；
    命中后 raw YAML 中的占位符（如 ``${API_KEY}``）以及 custom_headers 等未在
    前端暴露的字段会被保留，仅当字段值与前端最初看到的解析值不一致时才覆写。
    """
    if not isinstance(params, dict):
        await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
        return
    try:
        new_models = build_models_defaults_from_frontend(params.get("models"))
        if (
            is_affinity_enabled(get_config_raw())
            and not has_kv_cache_affinity_capability(
                default_model_client_config_from_entries(new_models)
            )
        ):
            update_kv_cache_affinity_enabled_in_config(False)
        # Replace only defaults inside a complete candidate so stable IDs,
        # AgentOS entries and model groups are validated and preserved.
        candidate_models = dict((get_config_raw().get("models") or {}))
        candidate_models["defaults"] = new_models
        _ensure_model_business_ids(candidate_models)
        raise_if_invalid(candidate_models)
        update_default_models_in_config(candidate_models["defaults"])

        applied_without_restart = await _apply_models_change(
            on_config_saved, agent_client, changed_keys=["models.defaults"], force=True
        )

        await channel.send_response(ws, req_id, ok=True, payload={
            "count": len(new_models),
            "applied_without_restart": applied_without_restart,
        })
    except ConfigPanelBadRequest as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[models.replace_all] %s", exc)
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")


def _models_reload_options(changed_keys: list[str]) -> dict[str, Any]:
    """models 域热更新选项（与原 gateway ``_ConfigChangeSet.reload_options`` 口径一致）。

    ``models.*`` 键映射 model scope，其余退回 agent_runtime；空集时按
    原 force 语义兜底 agent_runtime。
    """
    scopes: set[str] = set()
    for key in changed_keys:
        key_text = str(key)
        if key_text == "models.defaults" or key_text.startswith("models."):
            scopes.add("model")
        else:
            scopes.add("agent_runtime")
    if not scopes:
        scopes.add("agent_runtime")
    return {
        "target_channel_id": "web",
        "reload_scopes": sorted(scopes),
    }


async def _apply_models_change(
    on_config_saved: Any,
    agent_client: Any,
    *,
    changed_keys: list[str],
    force: bool = False,
) -> bool:
    """写回后同步热更新：优先 on_config_saved 回调，无回调时清 agent 配置缓存。"""
    import inspect

    if not changed_keys and not force:
        return True
    if on_config_saved:
        config_payload = get_config()
        callback_result = on_config_saved(
            changed_keys,
            env_updates={},
            config_payload=config_payload,
            reload_options=_models_reload_options(changed_keys),
        )
        if inspect.isawaitable(callback_result):
            return bool(await callback_result)
        return bool(callback_result)
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod
    import uuid as _uuid

    try:
        if agent_client is not None:
            env = e2a_from_agent_fields(
                request_id=f"cfg-reload-{_uuid.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            )
            await agent_client.send_request(env)
        else:
            get_config()
    except Exception:  # noqa: BLE001
        pass
    return True


def register_models_handlers(
    channel: Any,
    *,
    on_config_saved: Any = None,
    agent_client: Any = None,
) -> None:
    """在 channel 上注册 Web 侧 models/config 域本地 handler。

    注意：AgentOS 多用户的 E2A 代理分叉（单用户本地执行 / 多用户代理到目标
    AgentServer）由 gateway 侧 ``_register_config_proxy`` 包装；ConfigAdapter
    直接本地执行。两侧注册入口各自调用本函数，注册语义保持一致。
    """

    async def _config_validate_model(ws, req_id, params, session_id):
        await config_validate_model_handler(channel, ws, req_id, params)

    async def _models_validate(ws, req_id, params, session_id):
        """测试指定模型配置是否可用（复用 config.validate_model 逻辑）。"""
        await config_validate_model_handler(channel, ws, req_id, params)

    async def _models_list(ws, req_id, params, session_id):
        await models_list_handler(channel, ws, req_id, params, session_id)

    async def _models_replace_all(ws, req_id, params, session_id):
        await models_replace_all_handler(
            channel, ws, req_id, params, session_id,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
        )

    channel.register_method("config.validate_model", _config_validate_model)
    channel.register_method("models.validate", _models_validate)
    channel.register_method("models.list", _models_list)
    channel.register_method("models.replace_all", _models_replace_all)
