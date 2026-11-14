# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fetch a model list from a user-configured gateway.

The built-in ``vendors.fetch_models`` RPC resolves the request against the
vendor preset registry, so it can only fetch from endpoints the gateway already
knows. A user who configures a custom gateway (their own ``api_base`` plus
``api_key``) had no way to discover its models and had to type the model id.

This plugin adds one Gateway-local RPC that fetches
``GET {api_base}/models`` for that custom endpoint, so the model selector can
offer a dropdown and keep manual entry as a fallback.

Security notes: the endpoint is user supplied, so requests are made with
redirects disabled, a 12 second timeout and a bounded response body. Credentials
are only sent as an Authorization / x-api-key header and are never echoed back
in the response or logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from agent_gateway.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
)

logger = logging.getLogger(__name__)

FETCH_METHOD = "vendors.fetch_custom_models"

_TIMEOUT_SECONDS = 12.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_API_BASE_CHARS = 512
_MAX_API_KEY_CHARS = 2048
_MAX_MODELS = 2000
# Keep model ids selectable: the settings dialogs reject model_name over 100 chars.
_MAX_MODEL_ID_CHARS = 100

_PROTOCOL_OPENAI = "openai"
_PROTOCOL_ANTHROPIC = "anthropic"

_REASON_API_BASE_REQUIRED = "api_base required for fetch"
_REASON_API_BASE_INVALID = "api_base must be an http(s) URL"
_REASON_API_KEY_REQUIRED = "api_key required for fetch"
_REASON_API_KEY_TOO_LONG = "api_key too long for fetch"
_REASON_NETWORK = "remote fetch failed or empty"
_REASON_NON_JSON = "remote returned non-JSON body"
_REASON_BODY_TOO_LARGE = "remote response body too large"


class _FetchFailure(Exception):
    """Remote fetch failure carrying a stable reason string for the frontend."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_api_base(raw: Any) -> str:
    """Validate the user-supplied base URL and return it without trailing slashes."""
    value = str(raw or "").strip()
    if not value:
        raise _FetchFailure(_REASON_API_BASE_REQUIRED)
    if len(value) > _MAX_API_BASE_CHARS:
        raise _FetchFailure(_REASON_API_BASE_INVALID)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _FetchFailure(_REASON_API_BASE_INVALID)
    if parsed.username or parsed.password:
        raise _FetchFailure(_REASON_API_BASE_INVALID)
    return value.rstrip("/")


def _candidate_endpoints(api_base: str, protocol: str) -> list[str]:
    """Model-list endpoints to try, most likely first.

    OpenAI-compatible gateways expose ``{api_base}/models`` where ``api_base``
    usually already ends with ``/v1``. Some proxies publish the base without the
    version segment, so ``{api_base}/v1/models`` is tried when the first request
    returns 404. Anthropic-format bases are host roots (core appends
    ``/v1/messages``), so ``/v1/models`` comes first there.
    """
    base = api_base.rstrip("/")
    already_versioned = base.endswith("/v1")
    versioned = [f"{base}/models"] if already_versioned else [f"{base}/v1/models", f"{base}/models"]
    if protocol == _PROTOCOL_ANTHROPIC:
        return versioned
    return list(reversed(versioned)) if not already_versioned else versioned


def _auth_headers(api_key: str, protocol: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if protocol == _PROTOCOL_ANTHROPIC:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _read_limited(response: Any) -> bytes:
    """Read the body while enforcing ``_MAX_RESPONSE_BYTES``."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise _FetchFailure(_REASON_BODY_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def _model_id(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("id", "name", "model"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _collect_models(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    models: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        model_id = _model_id(entry)
        if not model_id or len(model_id) > _MAX_MODEL_ID_CHARS or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model_id)
        if len(models) >= _MAX_MODELS:
            break
    return models


def _parse_model_ids(payload: Any) -> list[str]:
    """Extract model ids from the OpenAI, Anthropic, Ollama and list-shaped bodies."""
    if isinstance(payload, list):
        return _collect_models(payload)
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "result"):
        models = _collect_models(payload.get(key))
        if models:
            return models
    return []


def _fetch_sync(endpoint: str, headers: dict[str, str]) -> list[str]:
    """Fetch one endpoint in a worker thread; raises ``_FetchFailure`` on any problem."""
    import httpx  # noqa: PLC0415 - optional dependency kept off the plugin import path

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
            with client.stream("GET", endpoint, headers=headers) as response:
                if response.status_code != 200:
                    raise _FetchFailure(f"remote returned HTTP {response.status_code}")
                body = _read_limited(response)
    except _FetchFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak endpoint or credentials
        logger.debug("[%s] http error: %s", FETCH_METHOD, type(exc).__name__)
        raise _FetchFailure(_REASON_NETWORK) from None
    try:
        payload = httpx.Response(200, content=body).json()
    except ValueError:
        raise _FetchFailure(_REASON_NON_JSON) from None
    models = _parse_model_ids(payload)
    if not models:
        raise _FetchFailure(_REASON_NETWORK)
    return models


def _unavailable_payload(reason: str) -> dict[str, Any]:
    """Failure payload: an empty list plus the stable reason the frontend maps to copy."""
    return {"models": [], "source": "unavailable", "reason": reason}


async def _fetch_custom_models(params: dict[str, Any]) -> dict[str, Any]:
    """Return the payload for the fetch RPC; failures stay in the payload, not exceptions."""
    api_key = str(params.get("api_key") or "").strip()
    if len(api_key) > _MAX_API_KEY_CHARS:
        raise _FetchFailure(_REASON_API_KEY_TOO_LONG)
    protocol = str(params.get("protocol") or "").strip().lower()
    if protocol not in {_PROTOCOL_OPENAI, _PROTOCOL_ANTHROPIC}:
        protocol = _PROTOCOL_OPENAI
    api_base = _normalize_api_base(params.get("api_base"))
    if not api_key:
        raise _FetchFailure(_REASON_API_KEY_REQUIRED)

    headers = _auth_headers(api_key, protocol)
    last_reason = _REASON_NETWORK
    for endpoint in _candidate_endpoints(api_base, protocol):
        try:
            models = await asyncio.to_thread(_fetch_sync, endpoint, headers)
        except _FetchFailure as failure:
            last_reason = failure.reason
            # Only a missing version segment is worth a second request.
            if failure.reason != "remote returned HTTP 404":
                break
            continue
        return {"models": models, "source": "remote"}
    return _unavailable_payload(last_reason)


class CustomModelCatalogPlugin(ApplicationPluginExtension):
    """Gateway-local model-list fetch for user-configured endpoints."""

    plugin_id = "custom-model-catalog"

    def is_enabled(self) -> bool:
        return True

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        del services

        async def fetch_custom_models(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            if not isinstance(params, dict):
                await channel.send_response(
                    ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
                )
                return
            try:
                payload = await _fetch_custom_models(params)
            except _FetchFailure as failure:
                await channel.send_response(ws, req_id, ok=True, payload=_unavailable_payload(failure.reason))
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] unexpected failure: %s", FETCH_METHOD, type(exc).__name__)
                await channel.send_response(ws, req_id, ok=True, payload=_unavailable_payload(_REASON_NETWORK))
                return
            await channel.send_response(ws, req_id, ok=True, payload=payload)

        # Business runtime RPC: never registered with available_when_disabled
        # (docs/zh/application-plugins.md), so a disabled plugin answers nothing.
        channel.register_method(FETCH_METHOD, fetch_custom_models, local_only=True)


async def register_extensions(registry: Any) -> list[CustomModelCatalogPlugin]:
    extension = CustomModelCatalogPlugin()
    registry.register_application_plugin(extension)
    return [extension]
