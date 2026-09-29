"""Credential-free model identity references shared by relay and team routing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

MODEL_IDENTITY_REFERENCE_PREFIX = "model-identity-v1:"


def _normalized(value: Any) -> str:
    return str(value or "").strip()


def _normalized_headers(value: Any) -> dict[str, str]:
    # Header names are case-insensitive.
    if not isinstance(value, dict):
        return {}
    return {str(key).strip().lower(): str(val) for key, val in value.items()}


def build_model_credential_fingerprint(client_config: Any) -> str:
    """Digest of the credential-bearing fields (api_key / custom_headers)."""
    def _field(name: str) -> Any:
        if isinstance(client_config, dict):
            return client_config.get(name)
        return getattr(client_config, name, None)

    payload = {
        "api_key": _normalized(_field("api_key")),
        "custom_headers": _normalized_headers(_field("custom_headers")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build_model_identity_reference(model_name: Any, client_config: dict[str, Any]) -> str:
    identity = {
        "api_base": _normalized(client_config.get("api_base")).rstrip("/"),
        "model_name": _normalized(model_name),
        "provider": _normalized(client_config.get("client_provider")).lower(),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"{MODEL_IDENTITY_REFERENCE_PREFIX}{digest}"


def normalize_model_identity_reference(value: Any) -> str:
    normalized = _normalized(value).lower()
    digest = normalized.removeprefix(MODEL_IDENTITY_REFERENCE_PREFIX)
    if (
        not normalized.startswith(MODEL_IDENTITY_REFERENCE_PREFIX)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError(f"invalid model reference: {normalized!r}")
    return normalized