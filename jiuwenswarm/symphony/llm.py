"""LLM helpers backed by JiuwenSwarm's configured model stack."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional

from jiuwenswarm.common.reasoning_injector import inject_reasoning_params

LLM_IDENTITY_SCHEMA_VERSION = "JiuwenSwarm-llm-identity-v1"


@dataclass(frozen=True)
class LLMConfig:
    """LLM chat configuration resolved from JiuwenSwarm model settings."""

    model: str = ""
    model_client_config: Dict[str, Any] | None = None
    model_config_obj: Dict[str, Any] | None = None
    temperature: float = 0.0
    top_p: float = 1.0

    @classmethod
    def from_default_model(cls) -> "LLMConfig":
        """Resolve the default JiuwenSwarm model declared in config.yaml."""

        from jiuwenswarm.common.config import get_config, get_default_models

        config = get_config()
        models = config.get("models") or {}
        if not isinstance(models, dict):
            models = {}
        default_models = models.get("defaults")
        has_default_model = (
            isinstance(default_models, list) and bool(default_models)
        ) or isinstance(models.get("default"), dict)
        if not has_default_model:
            raise RuntimeError(
                "No JiuwenSwarm default model is configured in config.yaml."
            )

        defaults = get_default_models(config)
        if not defaults:
            raise RuntimeError(
                "No JiuwenSwarm default model is configured in config.yaml."
            )
        entry = next(
            (item for item in defaults if item.get("is_default") is True), defaults[0]
        )
        return cls.from_model_entry(entry or {})

    @classmethod
    def from_model_entry(cls, entry: Dict[str, Any]) -> "LLMConfig":
        """Build orchestration config from one resolved Jiuwen model entry."""

        client_config = entry.get("model_client_config") or {}
        request_config = entry.get("model_config_obj") or {}
        if not isinstance(client_config, dict):
            client_config = {}
        if not isinstance(request_config, dict):
            request_config = {}
        client_config = deepcopy(client_config)
        request_config = deepcopy(request_config)
        model = str(
            request_config.get("model")
            or client_config.get("model_name")
            or client_config.get("model")
            or ""
        ).strip()
        if not model:
            raise RuntimeError("JiuwenSwarm default model is missing model_name.")
        if not str(client_config.get("api_base") or "").strip():
            raise RuntimeError("JiuwenSwarm default model is missing api_base.")
        if not str(client_config.get("api_key") or "").strip():
            raise RuntimeError("JiuwenSwarm default model is missing api_key.")
        if not str(client_config.get("client_provider") or "").strip():
            raise RuntimeError("JiuwenSwarm default model is missing client_provider.")
        request_config = inject_reasoning_params(
            model_client_config=client_config,
            model_config_obj=request_config,
        )
        request_config.pop("reasoning_effort", None)
        extra_body = request_config.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        extra_body = deepcopy(extra_body)
        extra_body.update(thinking_disabled_request_overrides()["extra_body"])
        request_config["extra_body"] = extra_body
        request_config["model"] = model
        return cls(
            model=model,
            model_client_config=client_config,
            model_config_obj=request_config,
        )

    @property
    def backend(self) -> str:
        return "jiuwenswarm"

    @property
    def base_url(self) -> str:
        client_config = self.model_client_config or {}
        return str(client_config.get("api_base") or "").strip().rstrip("/")

    def model_client_kwargs(self) -> Dict[str, Any]:
        client_config = deepcopy(self.model_client_config or {})
        if self.base_url:
            client_config["api_base"] = self.base_url
        return client_config

    def model_request_kwargs(self) -> Dict[str, Any]:
        request_config = deepcopy(self.model_config_obj or {})
        request_config["model"] = self.model
        request_config["temperature"] = self.temperature
        request_config["top_p"] = self.top_p
        return request_config

    def create_model(self):
        """Create the native openJiuwen model used by orchestration."""

        from openjiuwen.core.foundation.llm import (
            Model,
            ModelClientConfig,
            ModelRequestConfig,
        )

        return Model(
            model_client_config=ModelClientConfig(**self.model_client_kwargs()),
            model_config=ModelRequestConfig(**self.model_request_kwargs()),
        )

    def identity_digest(self) -> str:
        """Hash every effective client/request setting without exposing values."""

        return _stable_identity_sha256(
            {
                "schema_version": LLM_IDENTITY_SCHEMA_VERSION,
                "backend": self.backend,
                "client_config": self.model_client_kwargs(),
                "request_config": self.model_request_kwargs(),
            }
        )


@dataclass(frozen=True)
class LLMTokenUsageRecord:
    """Token usage from one LLM request."""

    stage: str
    operation: str
    backend: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    source: str = "provider"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "operation": self.operation,
            "backend": self.backend,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "source": self.source,
        }


class LLMTokenUsageTracker:
    """Thread-safe accumulator for LLM token usage."""

    def __init__(self) -> None:
        self._records: List[LLMTokenUsageRecord] = []
        self._lock = Lock()

    def add(self, record: LLMTokenUsageRecord) -> None:
        with self._lock:
            self._records.append(record)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    def records(self) -> List[LLMTokenUsageRecord]:
        with self._lock:
            return list(self._records)

    def summary(self) -> Dict[str, Any]:
        records = self.records()
        totals = _empty_usage_totals()
        by_stage: Dict[str, Dict[str, Any]] = defaultdict(_empty_usage_totals)
        by_operation: Dict[str, Dict[str, Any]] = defaultdict(_empty_usage_totals)
        for record in records:
            for bucket in (
                totals,
                by_stage[record.stage],
                by_operation[f"{record.stage}.{record.operation}"],
            ):
                bucket["request_count"] += 1
                bucket["prompt_tokens"] += record.prompt_tokens
                bucket["completion_tokens"] += record.completion_tokens
                bucket["total_tokens"] += record.total_tokens
        return {
            "total": totals,
            "by_stage": dict(sorted(by_stage.items())),
            "by_operation": dict(sorted(by_operation.items())),
            "records": [record.to_dict() for record in records],
        }


def _empty_usage_totals() -> Dict[str, int]:
    return {
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


_TOKEN_USAGE_TRACKER = LLMTokenUsageTracker()
_USAGE_STAGE: ContextVar[str] = ContextVar(
    "orchestration_llm_usage_stage",
    default="unknown",
)
_USAGE_OPERATION: ContextVar[Optional[str]] = ContextVar(
    "orchestration_llm_usage_operation",
    default=None,
)


@contextmanager
def llm_usage_context(stage: str, operation: Optional[str] = None) -> Iterator[None]:
    """Tag LLM calls made inside the context for usage aggregation."""

    stage_token = _USAGE_STAGE.set(stage)
    operation_token = _USAGE_OPERATION.set(operation)
    try:
        yield
    finally:
        _USAGE_OPERATION.reset(operation_token)
        _USAGE_STAGE.reset(stage_token)


def reset_llm_token_usage() -> None:
    _TOKEN_USAGE_TRACKER.reset()


def get_llm_token_usage_summary() -> Dict[str, Any]:
    return _TOKEN_USAGE_TRACKER.summary()


def create_llm_client(config: LLMConfig) -> "JiuwenSwarmChatClient":
    if config is None:
        raise ValueError("create_llm_client requires LLMConfig.")
    return JiuwenSwarmChatClient(config)


def create_model_response_observer(config: LLMConfig):
    """Bridge native orchestration model responses to the existing usage tracker."""

    def observe(response: Any, stage: str, operation: str | None) -> None:
        with llm_usage_context(stage, operation):
            _record_usage_from_response(
                config=config,
                response=response,
                operation=operation or "orchestration",
            )

    return observe


def thinking_disabled_request_overrides() -> Dict[str, Any]:
    """Return isolated provider-compatible controls that disable thinking."""
    return {
        "extra_body": {
            "thinking": {"type": "disabled"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }


class JiuwenSwarmChatClient:
    """Adapter over openjiuwen's async model invocation."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._model = None

    async def complete_json_async(
        self,
        *,
        system_prompt: str,
        user_content: str,
        timeout: Optional[int] = None,
        error_context: str = "LLM",
        request_overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        try:
            response = await self._invoke(
                system_prompt=system_prompt,
                user_content=user_content,
                timeout=timeout,
                request_overrides=request_overrides,
            )
        except Exception as exc:
            raise RuntimeError(f"{error_context} request failed: {exc}") from exc

        return self._json_content_from_response(response, error_context)

    async def complete_json_many_async(
        self,
        requests: List[Dict[str, str]],
        *,
        timeout: Optional[int] = None,
        error_context: str = "LLM",
        request_overrides: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        return [
            await self.complete_json_async(
                system_prompt=request["system_prompt"],
                user_content=request["user_content"],
                timeout=timeout,
                error_context=f"{error_context} item {index}",
                request_overrides=request_overrides,
            )
            for index, request in enumerate(requests, start=1)
        ]

    def _json_content_from_response(self, response: Any, error_context: str) -> str:
        from json_repair import repair_json

        content = extract_message_content(response)
        if not content:
            raise RuntimeError(f"{error_context} response content is empty.")
        _record_usage_from_response(
            config=self.config,
            response=response,
            operation=error_context,
        )
        return repair_json(content, return_objects=False)

    async def _invoke(
        self,
        *,
        system_prompt: str,
        user_content: str,
        timeout: Optional[int],
        request_overrides: Optional[Dict[str, Any]],
    ) -> Any:
        if self._model is None:
            self._model = self.config.create_model()

        invoke_kwargs: Dict[str, Any] = {}
        if timeout is not None:
            invoke_kwargs["timeout"] = timeout
        invoke_kwargs["temperature"] = self.config.temperature
        invoke_kwargs["top_p"] = self.config.top_p
        if request_overrides:
            invoke_kwargs.update(request_overrides)

        return await self._model.invoke(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            **invoke_kwargs,
        )


def _stable_identity_sha256(value: Any) -> str:
    canonical = _canonical_identity_value(value)
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _canonical_identity_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_identity_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_identity_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_identity_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _record_usage_from_response(
    *,
    config: LLMConfig,
    response: Any,
    operation: str,
) -> None:
    usage = getattr(response, "usage", None)
    if usage is None and hasattr(response, "raw"):
        usage = getattr(response.raw, "usage", None)
    source = "provider"
    if usage is None:
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            source = "usage_metadata"
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    _record_token_usage(
        config=config,
        operation=operation,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        source=source if usage is not None else "provider_missing_usage",
    )


def _record_token_usage(
    *,
    config: LLMConfig,
    operation: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    source: str,
) -> None:
    operation_override = _USAGE_OPERATION.get()
    _TOKEN_USAGE_TRACKER.add(
        LLMTokenUsageRecord(
            stage=_USAGE_STAGE.get(),
            operation=operation_override or operation,
            backend=config.backend,
            model=config.model,
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            total_tokens=max(0, total_tokens),
            source=source,
        )
    )


def _usage_int(usage: Any, *names: str) -> int:
    if usage is None:
        return 0
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def extract_message_content(message: Any) -> str:
    """Extract text from common openjiuwen/OpenAI-compatible response shapes."""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(_content_part_text(item) for item in content).strip()

    choices = getattr(message, "choices", None)
    if choices:
        first = choices[0]
        choice_message = getattr(first, "message", None)
        if choice_message is not None:
            return extract_message_content(choice_message)

    for attr in ("parsed", "json", "output_text", "text"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)

    if isinstance(message, dict):
        for key in ("content", "output_text", "text"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
    return ""


def _content_part_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("text") or item.get("content") or ""
        return value if isinstance(value, str) else ""
    value = getattr(item, "text", None) or getattr(item, "content", None)
    return value if isinstance(value, str) else ""
