# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit, fail-closed Ark JSON adapter for topic recommendation background jobs."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable

_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})
# A result whose consumer fails before record_validation keeps its receipt here; bound how many
# such unconsumed results stay referenced, since a caller is not obliged to call aclose().
_MAX_PENDING_VALIDATION_RECEIPTS = 64


@dataclass(frozen=True)
class ArkBackgroundConfig:
    """One background call. Decoding stays on the values given for this model."""

    api_key: str = field(repr=False)
    model: str
    base_url: str = ""
    temperature: float = 0.0
    top_p: float = 1.0
    context_window_tokens: int | None = None
    expected_model: str | None = None

    def __post_init__(self) -> None:
        # Decoding is part of cache identities: 0 and 0.0 must spell the same.
        object.__setattr__(self, "temperature", float(self.temperature))
        object.__setattr__(self, "top_p", float(self.top_p))

    @property
    def effective_temperature(self) -> float:
        return self.temperature

    @property
    def effective_top_p(self) -> float:
        return self.top_p

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ArkBackgroundConfig | None:
        """Use an explicit environment override only when the endpoint is set too.

        A missing ``ARK_BASE_URL`` does not invent a vendor endpoint. The caller
        then keeps the chat model chosen in settings.
        """

        values = os.environ if environ is None else environ
        api_key = str(values.get("ARK_API_KEY") or "").strip()
        model = str(values.get("ARK_MODEL") or "").strip()
        base_url = str(values.get("ARK_BASE_URL") or "").strip()
        try:
            context_window_tokens = int(
                str(values.get("ARK_CONTEXT_WINDOW_TOKENS") or "").strip()
            )
        except ValueError:
            context_window_tokens = 0
        if not api_key or not model or not base_url:
            return None
        if context_window_tokens <= 0:
            return None
        try:
            temperature = float(str(values.get("ARK_TEMPERATURE") or "0"))
            top_p = float(str(values.get("ARK_TOP_P") or "1"))
        except ValueError:
            return None
        expected_model = str(values.get("ARK_EXPECTED_MODEL") or "").strip() or None
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            top_p=top_p,
            context_window_tokens=context_window_tokens,
            expected_model=expected_model,
        )

    def safe_provenance(self) -> dict[str, Any]:
        provenance = {
            "model": self.model,
            "base_url": self.base_url,
            "requested_temperature": self.temperature,
            "requested_top_p": self.top_p,
            "effective_temperature": self.effective_temperature,
            "effective_top_p": self.effective_top_p,
            "decoding_parameter_source": "request_parameters",
        }
        if self.context_window_tokens is not None:
            provenance.update(
                {
                    "context_window_tokens": self.context_window_tokens,
                    "context_window_source": "ARK_CONTEXT_WINDOW_TOKENS",
                }
            )
        if self.expected_model is not None:
            provenance["expected_model"] = self.expected_model
        return provenance


def background_configuration_status(
    config: ArkBackgroundConfig | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Snapshot the configuration in force without exposing credentials."""
    values = os.environ if environ is None else environ
    missing = []
    for name in ("ARK_API_KEY", "ARK_MODEL"):
        if not str(values.get(name) or "").strip():
            missing.append(name)
    try:
        context = int(values.get("ARK_CONTEXT_WINDOW_TOKENS") or "0")
    except ValueError:
        context = 0
    if context <= 0:
        missing.append("ARK_CONTEXT_WINDOW_TOKENS")
    if config is not None:
        missing = []
        context = config.context_window_tokens or 0
    engines = {
        name: str(values.get(f"FREE_SEARCH_{name.upper()}_ENABLED") or "").strip().lower() == "true"
        for name in ("ddg", "bing")
    }
    # The search lane itself needs no setting: unset selects the native free-search lane. The
    # toggles below are the user-facing search switches, on the settings page.
    _source_items = (
        ("FREE_SEARCH_DDG_ENABLED=true", engines["ddg"]),
        ("FREE_SEARCH_BING_ENABLED=true", engines["bing"]),
    )
    search_missing = [
        setting for setting, present in _source_items if not present
    ]
    return {
        "provider": "ark",
        "model": config.model if config else str(values.get("ARK_MODEL") or ""),
        "context_window_tokens": context if context > 0 else None,
        "configured": config is not None,
        "missing_fields": missing,
        "native_search": engines,
        "native_search_missing": search_missing,
        "status": ("missing_configuration" if missing else "invalid_configuration"
                   if config is None else "native_search_unconfigured"
                   if search_missing else "ready"),
        "last_failure": None,
    }


PROVIDER_REFUSALS = frozenset({"invalid_key", "access_denied", "account_overdue", "model_unavailable"})


def provider_failure_reason(exc: BaseException) -> str:
    """Name a failed provider call; refusals hold for every later call until the setup changes."""

    code = getattr(exc, "code", None)
    if code == "AccountOverdueError":
        return "account_overdue"
    # Ark answers a closed model endpoint with 400 rather than 404.
    if code == "InvalidEndpoint.ClosedEndpoint":
        return "model_unavailable"
    # A model that rejects the stages' strict structured output cannot serve the chain at all.
    if getattr(exc, "status_code", None) == 400 and any(
        "response_format" in str(getattr(exc, field, None) or "") for field in ("param", "message")
    ):
        return "model_unavailable"
    reasons: dict[object, str] = {
        401: "invalid_key", 403: "access_denied",
        404: "model_unavailable", 429: "rate_limited",
    }
    return reasons.get(getattr(exc, "status_code", None), "provider_request_failed")


def _default_client_factory(**kwargs: Any) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(max_retries=0, **kwargs)


def build_ark_stage_invoker(
    config: ArkBackgroundConfig,
    *,
    client_factory: Callable[..., Any] = _default_client_factory,
    response_schema_for_stage: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    require_response_metadata: bool = False,
    receipt_sink: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort_for_stage: Callable[[str], str | None] | None = None,
    max_tokens_for_stage: Callable[[str], int | None] | None = None,
    prompt_version_for_stage: Callable[[str], str | None] | None = None,
    invocation_lineage_for_stage: Callable[[str, str, Any], Mapping[str, Any] | None]
    | None = None,
    return_response_metadata: bool = False,
) -> Callable[[str, str, str], Awaitable[Any]]:
    """Build one request-independent adapter with explicit model/decoding."""

    client = client_factory(api_key=config.api_key, base_url=config.base_url)
    invocation_receipts: list[dict[str, Any]] = []
    validation_receipts: dict[int, tuple[Any, dict[str, Any]]] = {}

    async def invoke(stage: str, system_prompt: str, payload: str) -> Any:
        schema = (
            response_schema_for_stage(stage, payload)
            if response_schema_for_stage is not None
            else None
        )
        response_format: dict[str, Any]
        if schema is None:
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": stage.replace("-", "_")[:64],
                    "strict": True,
                    "schema": dict(schema),
                },
            }
        reasoning_effort = (
            reasoning_effort_for_stage(stage)
            if reasoning_effort_for_stage is not None
            else None
        )
        if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
            raise ValueError("unsupported Ark reasoning effort")
        max_tokens = max_tokens_for_stage(stage) if max_tokens_for_stage is not None else None
        started_at = time.perf_counter()
        request: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload},
            ],
            "temperature": config.effective_temperature,
            "top_p": config.effective_top_p,
            "response_format": response_format,
        }
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        # 推荐链的后台调用不消费推理预算：GLM 系兼容端点认 thinking.type=disabled。
        # 必须经 SDK 的 extra_body 传入——thinking 不是 create() 的类型化参数，
        # 直接放顶层会在 OpenAI SDK 校验时抛 TypeError（请求不出网即失败）。
        # 不发 enable_thinking/chat_template_kwargs——DashScope 代理会对"始终思考"的
        # 模型以 400 拒绝 enable_thinking=false（litellm: 请使用 low、high 或 max）。
        # 阶段的 reasoning_effort 也不发：豆包对 thinking=disabled 同时带 low、medium、
        # high 返回 400；它只记在回执里，作为该阶段的策略身份。
        request["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
        request_id = data.get("request_id") if isinstance(data, dict) else None
        call_context = {**_INVOCATION_CONTEXT.get(), "provider": "ark",
                        "model": config.expected_model or config.model}
        attempt_id = f"ark-attempt-{uuid.uuid4().hex}"
        status = "started"
        receipt: dict[str, Any] = {
            "stage": stage,
            "provider_response_id": None,
            "usage": None,
            "structured_output_mode": (
                "json_schema_strict" if schema is not None else "json_object"
            ),
        }
        if reasoning_effort is not None:
            receipt["reasoning_effort"] = reasoning_effort
        prompt_version = (
            prompt_version_for_stage(stage)
            if prompt_version_for_stage is not None
            else None
        )
        if prompt_version is not None:
            receipt["prompt_version"] = prompt_version

        def save() -> None:
            receipt["latency_ms"] = round(
                (time.perf_counter() - started_at) * 1_000, 3
            )
            record = {
                **receipt,
                "attempt_id": attempt_id,
                "request_id": request_id if isinstance(request_id, str) else None,
                "status": status,
                **call_context,
            }
            if receipt_sink is not None:
                receipt_sink(record)

        invocation_receipts.append(receipt)
        save()
        try:
            response = await client.chat.completions.create(**request)
            usage = getattr(response, "usage", None)
            usage_dumper = getattr(usage, "model_dump", None)
            choices = getattr(response, "choices", None)
            choice = choices[0] if choices else None
            message = getattr(choice, "message", None)
            receipt.update({
                "provider_response_id": getattr(response, "id", None),
                "response_object": getattr(response, "object", None),
                "actual_model": getattr(response, "model", None),
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage": (
                    usage_dumper(exclude_none=True)
                    if callable(usage_dumper)
                    else dict(usage) if isinstance(usage, Mapping) else None
                ),
            })
            status = "received"
            save()
            status = (
                "refused" if getattr(message, "refusal", None)
                else "truncated" if receipt["finish_reason"] == "length"
                else "metadata_failed"
            )
            if require_response_metadata:
                expected_model = config.expected_model or config.model
                _rejected2 = (
                    not isinstance(receipt["provider_response_id"], str)
                    or not receipt["provider_response_id"]
                    or receipt["response_object"] != "chat.completion"
                    or receipt["actual_model"] != expected_model
                    or usage is None
                    or receipt["finish_reason"] != "stop"
                )
                if (
                    _rejected2
                ):
                    raise RuntimeError("Ark response metadata contract failed")
            status = "json_failed"
            content = getattr(message, "content", None)
            if isinstance(content, list):
                content = "".join(
                    part if isinstance(part, str) else str(part.get("text") or "")
                    for part in content
                    if isinstance(part, (str, dict))
                )
            if getattr(message, "refusal", None):
                status = "refused"
            result = json.loads(content if isinstance(content, str) else "")
            status = "lineage_failed"
            lineage = (
                invocation_lineage_for_stage(stage, payload, result)
                if invocation_lineage_for_stage is not None
                else None
            )
            if lineage is not None:
                receipt["lineage"] = dict(lineage)
            status = (
                "refused" if getattr(message, "refusal", None)
                else "truncated" if receipt["finish_reason"] == "length"
                else "parsed"
            )
            if receipt_sink is not None:
                validation_receipts[id(result)] = (result, {
                    **receipt, "attempt_id": attempt_id, "request_id": request_id,
                    **call_context,
                })
                while len(validation_receipts) > _MAX_PENDING_VALIDATION_RECEIPTS:
                    validation_receipts.pop(next(iter(validation_receipts)))
            if return_response_metadata:
                return {
                    "result": result,
                    "provider_response_id": receipt.get("provider_response_id"),
                    "usage": receipt.get("usage"),
                    "actual_model": receipt.get("actual_model"),
                }
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            if status == "started":
                status = "request_failed"
                receipt["failure_reason"] = provider_failure_reason(exc)
            raise
        finally:
            save()

    def record_validation(response: Any, outcome: str) -> None:
        entry = validation_receipts.pop(id(response), None)
        if entry is not None and entry[0] is response and receipt_sink is not None:
            receipt_sink({**entry[1], "status": outcome})

    async def aclose() -> None:
        validation_receipts.clear()
        await client.close()

    setattr(invoke, "record_validation", record_validation)
    setattr(invoke, "invocation_receipts", invocation_receipts)
    setattr(invoke, "aclose", aclose)
    return invoke


PROVIDER_INVOCATION_EVENT_TYPE = "provider_invocation"


def persist_provider_invocation(store: Any, receipt: dict[str, Any]) -> None:
    """Keep request, received usage, and adapter outcome as separate facts."""
    from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
        current_background_generation_attempt_id,
    )

    payload = dict(receipt)
    payload["generation_attempt_id"] = current_background_generation_attempt_id()
    store.record_event({
        "session_id": receipt["request_id"] or receipt["attempt_id"],
        "slate_id": receipt["attempt_id"],
        "event_type": PROVIDER_INVOCATION_EVENT_TYPE,
        "topic_id": receipt["status"],
        "policy_id": "topic-starter-provider-invocation-v1",
        "selection_mode": "provider_invocation",
        "propensity_reason": "not_estimated",
        "payload": payload,
        "created_at": time.time(),
    })



_INVOCATION_CONTEXT: ContextVar[dict[str, Any]] = ContextVar(
    "topic_starter_provider_invocation_context", default={}
)


@contextmanager
def provider_invocation_context(**identities: str):
    token = _INVOCATION_CONTEXT.set({**_INVOCATION_CONTEXT.get(), **identities})
    try:
        yield
    finally:
        _INVOCATION_CONTEXT.reset(token)


def record_provider_validation(invoke_stage: Any, response: Any, outcome: str) -> None:
    recorder = getattr(invoke_stage, "record_validation", None)
    if callable(recorder):
        recorder(response, outcome)


@contextmanager
def provider_validation(invoke_stage: Any, response: Any):
    """Record the existing consumer's decision without changing validation."""
    try:
        yield
    except Exception:
        record_provider_validation(invoke_stage, response, "business_validation_failed")
        raise
    else:
        record_provider_validation(invoke_stage, response, "business_validated")
