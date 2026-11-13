# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed JSON stage invoker for topic recommendation background jobs.

Uses the same OpenAI-compatible core ``Model`` stack as settings connection probes.
``ArkBackgroundConfig`` is the historical name for the dedicated recommendation
connection (api_key / model / base_url / decoding), not a separate vendor lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable

logger = logging.getLogger(__name__)

# One extra_body for every model. Same trio as Symphony's thinking_disabled_request_overrides:
# Qwen ignores thinking.type and reads enable_thinking / chat_template_kwargs; Doubao rejects
# reasoning_effort together with thinking disabled. Always-think models (e.g. some GLM routes)
# reject this body; callers retry once with those fields stripped — no user-facing
# reasoning_level for recommendation.
_THINKING_DISABLED_EXTRA_BODY = {
    "thinking": {"type": "disabled"},
    "enable_thinking": False,
    "chat_template_kwargs": {"enable_thinking": False},
}
_THINKING_DISABLE_BODY_KEYS = frozenset({"thinking", "enable_thinking", "chat_template_kwargs"})
# Match Designer structured-output ceilings: reasoning shares max_tokens with content.
_TOPIC_MAX_TOKENS_CAP = 65536
_TOPIC_DEFAULT_MAX_TOKENS = 16384
_ALWAYS_THINK_UNSUPPORTED_MARKERS = (
    "不支持关闭",
    "始终思考",
    "cannot disable",
    "always think",
    "unsupported",
    "not support",
    "not supported",
    "不允许关闭",
)
_ALWAYS_THINK_DISABLE_MARKERS = (
    "关闭思考",
    "disable",
    "disabled",
    "thinking.type",
    "enable_thinking",
    "thinking",
    "reasoning",
)

# One repair round after business validation fails: same shape as Eternal call_json.
_SHAPE_REPAIR_CHECKLIST = """
Previous output failed host validation (invalid JSON or wrong shape). Return ONE
corrected JSON value only — no markdown fence, no prose, no thinking text.
Keep every required id exactly as in the request.
Do not invent candidates, pairs, or mappings that were not requested.
Fix only the structural problem named in validation_error.
"""

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
    client_provider: str = "OpenAI"
    endpoint_profile: str | None = None

    def __post_init__(self) -> None:
        # Decoding is part of cache identities: 0 and 0.0 must spell the same.
        object.__setattr__(self, "temperature", float(self.temperature))
        object.__setattr__(self, "top_p", float(self.top_p))
        object.__setattr__(
            self,
            "client_provider",
            str(self.client_provider or "OpenAI").strip() or "OpenAI",
        )
        profile = str(self.endpoint_profile or "").strip() or None
        object.__setattr__(self, "endpoint_profile", profile)

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
        name: str(values.get(f"FREE_SEARCH_{name.upper()}_ENABLED") or "").strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
        for name in ("ddg", "bing")
    }
    # Informational only: free-search toggles do not block topic recommendation.
    # Unset still selects the native lane; missing engines just mean web-search
    # grounding may fail at call time while hot-list and presets keep working.
    search_missing = [] if any(engines.values()) else [
        "FREE_SEARCH_DDG_ENABLED=true OR FREE_SEARCH_BING_ENABLED=true"
    ]
    return {
        # Historical status key; value means OpenAI-compatible recommendation model.
        "provider": "recommendation",
        "model": config.model if config else str(values.get("ARK_MODEL") or ""),
        "context_window_tokens": context if context > 0 else None,
        "configured": config is not None,
        "missing_fields": missing,
        "native_search": engines,
        "native_search_missing": search_missing,
        "status": ("missing_configuration" if missing else "invalid_configuration"
                   if config is None else "ready"),
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
    # Legacy: stages no longer send response_format by default; keep the
    # mapping only when a caller still attaches one and the provider rejects it.
    if getattr(exc, "status_code", None) == 400 and any(
        "response_format" in str(getattr(exc, field, None) or "") for field in ("param", "message")
    ):
        return "model_unavailable"
    reasons: dict[object, str] = {
        401: "invalid_key", 403: "access_denied",
        404: "model_unavailable", 429: "rate_limited",
    }
    return reasons.get(getattr(exc, "status_code", None), "provider_request_failed")


def _normalize_model_id(value: str) -> str:
    text = value.strip().lower()
    for prefix in ("models/", "openai/", "account/", "endpoints/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def model_ids_compatible(actual: Any, expected: str) -> bool:
    """Providers often rewrite route ids; require a clear link, not character equality."""

    if not isinstance(actual, str) or not actual.strip() or not expected.strip():
        return False
    actual_id = _normalize_model_id(actual)
    expected_id = _normalize_model_id(expected)
    if actual_id == expected_id:
        return True
    return (
        actual_id.endswith("/" + expected_id)
        or expected_id.endswith("/" + actual_id)
        or actual_id.startswith(expected_id)
        or expected_id.startswith(actual_id)
    )


def _clamp_topic_max_tokens(
    value: int | None,
    *,
    context_window_tokens: int | None = None,
) -> int:
    """Bound completion size so ``prompt + max_tokens`` can still fit the window.

    Stage budgets are Designer-sized (16k–65k). Without a context-window cap,
    small models get ``max_tokens`` larger than the remaining window and many
    OpenAI-compatible / vLLM gateways reject with HTTP 400 instead of truncating.

    Cap completion at half the window (not ``window - 1``): prompt fit reserves the
    same budget, and ``window - 1`` would leave only one token for the prompt on
    small windows, making every non-empty summarizer/RID fit fail closed.
    """

    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        n = _TOPIC_DEFAULT_MAX_TOKENS
    capped = max(256, min(_TOPIC_MAX_TOKENS_CAP, n))
    try:
        window = int(context_window_tokens or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return capped
    room = max(1, window // 2)
    return max(1, min(capped, room))


def _minimal_reasoning(config: ArkBackgroundConfig) -> dict[str, str] | None:
    """Lowest reasoning setting the model accepts (Designer call_model_tool policy).

    Reasoning shares ``max_tokens`` with the visible JSON reply, so stages turn
    thinking off where core allows and otherwise pick the lowest listed level.
    ``None`` means the model exposes no control at all.
    """

    from openjiuwen.core.foundation.llm import get_reasoning_capability

    from jiuwenswarm.common.reasoning_config import (
        SUPPORTED_REASONING_LEVELS,
        effective_endpoint_profile,
        reasoning_config_for_level,
        reasoning_level_options,
    )

    capability = get_reasoning_capability(
        provider=config.client_provider,
        model=config.model,
        protocol=(
            "anthropic"
            if str(config.client_provider).strip().lower() == "anthropic"
            else "openai"
        ),
        api_base=config.base_url,
        endpoint_profile=effective_endpoint_profile(
            config.base_url, config.endpoint_profile,
        ),
    )
    options = set(reasoning_level_options(capability))
    for level in SUPPORTED_REASONING_LEVELS:
        # A bare "on" is a toggle without a lower setting to choose.
        if level != "on" and level in options:
            return reasoning_config_for_level(level)
    return None


def _default_model_factory(config: ArkBackgroundConfig) -> Any:
    """Build the same OpenAI-compatible core Model used by settings probes."""

    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import (
        ModelClientConfig,
        ModelRequestConfig,
    )

    from jiuwenswarm.common.reasoning_config import effective_endpoint_profile

    # Align with Designer structured calls (1200). A previous 120s hard-code
    # aborted long JSON stages; core ModelClientConfig requires timeout > 0.
    client_kwargs: dict[str, Any] = {
        "client_id": "topic-recommendation",
        "client_provider": config.client_provider,
        "api_key": config.api_key,
        "api_base": config.base_url,
        "timeout": 1200.0,
        "max_retries": 0,
    }
    profile = effective_endpoint_profile(config.base_url, config.endpoint_profile)
    if profile:
        client_kwargs["endpoint_profile"] = profile
    return Model(
        model_config=ModelRequestConfig(
            model_name=config.model,
            temperature=config.effective_temperature,
            top_p=config.effective_top_p,
            context_window=config.context_window_tokens,
        ),
        model_client_config=ModelClientConfig(**client_kwargs),
    )


def _usage_as_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    dumper = getattr(usage, "model_dump", None)
    if callable(dumper):
        dumped = dumper(exclude_none=True)
        return dumped if isinstance(dumped, dict) else None
    if isinstance(usage, Mapping):
        return dict(usage)
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text") or "")
            for part in content
            if isinstance(part, (str, dict))
        )
    return ""


_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)\s*```",
    flags=re.IGNORECASE,
)


def _parse_stage_json(text: str) -> Any:
    """Parse stage JSON from chat text the way Designer / prompt-suggestion do.

    Recommendation no longer depends on API ``response_format``; models may wrap
    JSON in fences or add brief prose. Fail closed when nothing decodes.
    """

    raw = (text or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty stage content", raw, 0)
    candidates = [raw]
    fence = _FENCED_JSON_RE.search(raw)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    elif raw.startswith("```"):
        unfenced = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        unfenced = re.sub(r"\s*```\s*$", "", unfenced).strip()
        candidates.insert(0, unfenced)
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
        for opener in ("{", "["):
            start = candidate.find(opener)
            if start < 0:
                continue
            try:
                data, _end = json.JSONDecoder().raw_decode(candidate[start:])
                return data
            except json.JSONDecodeError as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("unparseable stage content", raw, 0)


def _looks_like_always_think_rejection(exc: BaseException) -> bool:
    """Whether a provider refused the hard-disabled thinking request body."""

    text = str(exc).lower()
    if "thinking" not in text and "reasoning" not in text and "思考" not in text:
        return False
    if not any(marker in text for marker in _ALWAYS_THINK_UNSUPPORTED_MARKERS):
        return False
    return any(marker in text for marker in _ALWAYS_THINK_DISABLE_MARKERS)


def _without_thinking_disable_body(extra_body: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in dict(extra_body or {}).items()
        if key not in _THINKING_DISABLE_BODY_KEYS
    }
    chat_kwargs = (extra_body or {}).get("chat_template_kwargs")
    if isinstance(chat_kwargs, Mapping):
        kept = {
            key: value
            for key, value in chat_kwargs.items()
            if key != "enable_thinking"
        }
        if kept:
            cleaned["chat_template_kwargs"] = kept
    return cleaned


async def _create_completion_prefer_thinking_disabled(
    create: Callable[..., Awaitable[Any]],
    request: Mapping[str, Any],
) -> Any:
    """Send disabled-thinking first; if the model forbids it, retry without those fields."""

    try:
        return await create(**dict(request))
    except Exception as exc:
        if not _looks_like_always_think_rejection(exc):
            raise
        retry = dict(request)
        retry["extra_body"] = _without_thinking_disable_body(
            request.get("extra_body")
            if isinstance(request.get("extra_body"), Mapping)
            else None
        )
        return await create(**retry)


def _provider_http_status(detail: BaseException) -> int | None:
    """HTTP status on a provider error, looking through wrapped causes."""

    seen: set[int] = set()
    current: BaseException | None = detail
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "response", None)):
            if candidate is None:
                continue
            for attr in ("status_code", "status"):
                code = getattr(candidate, attr, None)
                try:
                    return int(code)
                except (TypeError, ValueError):
                    continue
        current = current.__cause__ or current.__context__
    return None


async def _invoke_model_prefer_thinking_disabled(
    model: Any,
    messages: list[dict[str, str]],
    invoke_kwargs: Mapping[str, Any],
) -> tuple[Any, bool]:
    """Production Model path: disable thinking when possible; drop ``reasoning`` on 400.

    Core's ``disabled_thinking_fallback_scope`` strips thinking-disable
    ``extra_body`` fields for always-think models. Self-hosted gateways that
    reject the separate ``reasoning`` kwarg need the Designer-style drop-and-retry
    (HTTP 400 → omit ``reasoning`` once).
    """

    from openjiuwen.core.foundation.llm.request_context import (
        disabled_thinking_fallback_scope,
    )

    kwargs = dict(invoke_kwargs)
    with disabled_thinking_fallback_scope():
        try:
            return await model.invoke(messages, **kwargs), False
        except Exception as exc:
            if kwargs.get("reasoning") is None or _provider_http_status(exc) != 400:
                raise
            logger.warning(
                "[Topic] endpoint rejected reasoning=%s; retrying without it. "
                "Set endpoint_profile on self-hosted gateways. (%s)",
                kwargs.get("reasoning"),
                exc,
            )
            kwargs.pop("reasoning", None)
            return await model.invoke(messages, **kwargs), True


def build_ark_stage_invoker(
    config: ArkBackgroundConfig,
    *,
    client_factory: Callable[..., Any] | None = None,
    model_factory: Callable[[ArkBackgroundConfig], Any] | None = None,
    response_schema_for_stage: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    require_response_metadata: bool = False,
    receipt_sink: Callable[[dict[str, Any]], None] | None = None,
    max_tokens_for_stage: Callable[[str], int | None] | None = None,
    prompt_version_for_stage: Callable[[str], str | None] | None = None,
    invocation_lineage_for_stage: Callable[[str, str, Any], Mapping[str, Any] | None]
    | None = None,
    return_response_metadata: bool = False,
) -> Callable[[str, str, str], Awaitable[Any]]:
    """Build one request-independent adapter with explicit model/decoding.

    Production uses core ``Model``. Tests may inject ``client_factory`` returning an
    OpenAI-shaped client (``chat.completions.create``) to avoid a live stack.
    """

    client = (
        None
        if client_factory is None
        else client_factory(api_key=config.api_key, base_url=config.base_url)
    )
    model = None if client is not None else (model_factory or _default_model_factory)(config)
    try:
        reasoning = _minimal_reasoning(config)
    except Exception:  # noqa: BLE001 - capability lookup must not block stages
        logger.warning(
            "[Topic] reasoning capability lookup failed model=%s",
            config.model,
            exc_info=True,
        )
        reasoning = None
    invocation_receipts: list[dict[str, Any]] = []
    validation_receipts: dict[int, tuple[Any, dict[str, Any]]] = {}

    async def invoke(stage: str, system_prompt: str, payload: str) -> Any:
        # response_schema_for_stage remains on the signature for pipeline/RID
        # call sites, but structured output is prompt + parse only — same as
        # designer / proactive / prompt_suggestion — so deepseek-flash and peers
        # that reject response_format stay usable.
        _ = response_schema_for_stage
        requested = (
            max_tokens_for_stage(stage) if max_tokens_for_stage is not None else None
        )
        max_tokens = _clamp_topic_max_tokens(
            requested,
            context_window_tokens=config.context_window_tokens,
        )
        started_at = time.perf_counter()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": payload},
        ]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
        request_id = data.get("request_id") if isinstance(data, dict) else None
        call_context = {
            **_INVOCATION_CONTEXT.get(),
            "provider": "recommendation",
            "model": config.expected_model or config.model,
        }
        attempt_id = f"recommendation-attempt-{uuid.uuid4().hex}"
        status = "started"
        receipt: dict[str, Any] = {
            "stage": stage,
            "provider_response_id": None,
            "usage": None,
            "structured_output_mode": "prompt_json",
            "max_tokens": max_tokens,
        }
        if reasoning is not None:
            receipt["reasoning"] = dict(reasoning)
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
            refusal: Any = None
            if client is not None:
                request: dict[str, Any] = {
                    "model": config.model,
                    "messages": messages,
                    "temperature": config.effective_temperature,
                    "top_p": config.effective_top_p,
                    "max_tokens": max_tokens,
                    "extra_body": dict(_THINKING_DISABLED_EXTRA_BODY),
                }
                response = await _create_completion_prefer_thinking_disabled(
                    client.chat.completions.create,
                    request,
                )
                usage = getattr(response, "usage", None)
                choices = getattr(response, "choices", None)
                choice = choices[0] if choices else None
                message = getattr(choice, "message", None)
                receipt.update({
                    "provider_response_id": getattr(response, "id", None),
                    "response_object": getattr(response, "object", None),
                    "actual_model": getattr(response, "model", None),
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "usage": _usage_as_dict(usage),
                })
                refusal = getattr(message, "refusal", None)
                content = getattr(message, "content", None)
            else:
                if model is None:
                    raise RuntimeError("recommendation model is not configured")
                invoke_kwargs: dict[str, Any] = {
                    "temperature": config.effective_temperature,
                    "top_p": config.effective_top_p,
                    "max_tokens": max_tokens,
                    "extra_body": dict(_THINKING_DISABLED_EXTRA_BODY),
                }
                if reasoning is not None:
                    invoke_kwargs["reasoning"] = dict(reasoning)
                assistant, reasoning_dropped = await _invoke_model_prefer_thinking_disabled(
                    model,
                    messages,
                    invoke_kwargs,
                )
                usage = getattr(assistant, "usage_metadata", None)
                receipt.update({
                    "provider_response_id": getattr(assistant, "response_id", None),
                    "response_object": "chat.completion",
                    "actual_model": (
                        getattr(assistant, "response_model", None) or config.model
                    ),
                    "finish_reason": getattr(assistant, "finish_reason", None),
                    "usage": _usage_as_dict(usage),
                })
                if reasoning_dropped:
                    receipt["reasoning_dropped"] = True
                content = getattr(assistant, "content", None)
            status = "received"
            save()
            status = (
                "refused" if refusal
                else "truncated" if receipt["finish_reason"] == "length"
                else "metadata_failed"
            )
            if require_response_metadata:
                expected_model = config.expected_model or config.model
                finish = receipt["finish_reason"]
                model_ok = model_ids_compatible(
                    receipt["actual_model"], expected_model
                )
                if not model_ok:
                    logger.warning(
                        "[Topic] provider model id mismatch expected=%r actual=%r",
                        expected_model,
                        receipt["actual_model"],
                    )
                if receipt["usage"] is None:
                    logger.warning(
                        "[Topic] provider usage missing stage=%s; continuing",
                        stage,
                    )
                rejected = (
                    not isinstance(receipt["provider_response_id"], str)
                    or not receipt["provider_response_id"]
                    or receipt["response_object"] != "chat.completion"
                    or not model_ok
                    or finish not in {"stop", "length"}
                )
                if rejected:
                    raise RuntimeError("recommendation response metadata contract failed")
            status = "json_failed"
            if refusal:
                status = "refused"
            if receipt["finish_reason"] == "length":
                logger.warning(
                    "[Topic] stage=%s finish_reason=length; attempting JSON parse",
                    stage,
                )
            content_text = _content_text(content)
            try:
                result = _parse_stage_json(content_text)
            except json.JSONDecodeError:
                preview = content_text if len(content_text) <= 800 else f"{content_text[:800]}..."
                logger.warning(
                    "[Topic] stage=%s JSON parse failed preview=%r",
                    stage,
                    preview,
                )
                raise
            status = "lineage_failed"
            lineage = (
                invocation_lineage_for_stage(stage, payload, result)
                if invocation_lineage_for_stage is not None
                else None
            )
            if lineage is not None:
                receipt["lineage"] = dict(lineage)
            status = (
                "refused" if refusal
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
        if client is not None:
            closer = getattr(client, "close", None)
            if callable(closer):
                await closer()

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


async def invoke_stage_with_shape_repair(
    invoke_stage: Callable[[str, str, str], Awaitable[Any]],
    stage: str,
    system_prompt: str,
    payload: str,
    *,
    coerce: Callable[[Any], Any] | None = None,
    validate: Callable[[Any], Any],
    max_repairs: int = 1,
) -> Any:
    """Coerce, validate, then one Eternal-style repair retry.

    Retries once on ``ValueError`` (shape) or ``json.JSONDecodeError`` (unparseable
    model text). Keeps business fail-closed after the last attempt. Used by
    utility / diversity so general models can fix parse/shape drift once.
    """

    def _shape_preview(value: Any) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = repr(value)
        if len(text) > 800:
            return f"{text[:800]}..."
        return text

    def _repair_request_id(original_request: Any) -> str:
        # Host audit (_audited_ark_stage_invoker) requires top-level request_id.
        if isinstance(original_request, dict):
            return str(original_request.get("request_id") or "").strip()
        return ""

    def _original_request() -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    async def _repair_once(*, validation_error: str, previous_output: Any) -> Any:
        original_request = _original_request()
        repair_body: dict[str, Any] = {
            "validation_error": validation_error,
            "previous_output": previous_output,
            "original_request": original_request,
        }
        request_id = _repair_request_id(original_request)
        if request_id:
            repair_body["request_id"] = request_id
        repair_payload = json.dumps(repair_body, ensure_ascii=False, default=str)
        repaired = await invoke_stage(
            stage,
            f"{system_prompt.rstrip()}\n{_SHAPE_REPAIR_CHECKLIST}",
            repair_payload,
        )
        if coerce is not None:
            repaired = coerce(repaired)
        try:
            with provider_validation(invoke_stage, repaired):
                validate(repaired)
        except ValueError:
            logger.warning(
                "[Topic] stage=%s shape repair still invalid preview=%s",
                stage,
                _shape_preview(repaired),
            )
            raise
        return repaired

    try:
        raw = await invoke_stage(stage, system_prompt, payload)
    except json.JSONDecodeError as exc:
        if max_repairs <= 0:
            raise
        previous = (exc.doc or "")[:800]
        logger.warning(
            "[Topic] stage=%s JSON parse failed; repairing once: %s preview=%r",
            stage,
            exc,
            previous,
        )
        return await _repair_once(
            validation_error=f"JSON parse failed: {exc}",
            previous_output=previous or str(exc),
        )

    if coerce is not None:
        raw = coerce(raw)
    try:
        with provider_validation(invoke_stage, raw):
            validate(raw)
        return raw
    except ValueError as exc:
        if max_repairs <= 0:
            raise
        logger.warning(
            "[Topic] stage=%s shape validation failed; repairing once: %s preview=%s",
            stage,
            exc,
            _shape_preview(raw),
        )
        return await _repair_once(
            validation_error=str(exc),
            previous_output=raw,
        )
