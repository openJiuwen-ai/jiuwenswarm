# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side stream timeout defaults for model_client_config assembly.

Single-agent construction historically applied ``stream_idle_timeout=180`` inside
``_build_model_from_entry``. Team pack / model_pool / routing fallback builders
copied raw MCC and fell through to the SDK defaults (idle 60s / first_chunk 300s).
This module is the shared setdefault so every assembly path stays consistent
without mutating the caller's shared config tree.

``stream_first_chunk_timeout`` defaults to 180s so a no-first-token hang raises
``LLM stream timeout`` and enters ``LLMRetryRail`` before the relay team stall
watchdog (default 300s) cancels the round with non-retryable ``CancelledError``.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

# Align with jiuwenswarm/resources/config.yaml models.defaults template.
# Relay-delivered entries omit these fields; without host defaults the SDK uses
# idle=60 / first_chunk=300.
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 180.0
DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS = 180.0

_STREAM_IDLE_TIMEOUT_KEY = "stream_idle_timeout"
_STREAM_FIRST_CHUNK_TIMEOUT_KEY = "stream_first_chunk_timeout"


def _needs_host_default(value: Any) -> bool:
    """True when the field is missing-equivalent (absent key or explicit null)."""
    return value is None


def apply_stream_timeout_defaults(
    model_client_config: Mapping[str, Any] | MutableMapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a shallow copy of *model_client_config* with host stream defaults.

    Fills ``stream_idle_timeout`` / ``stream_first_chunk_timeout`` when the key
    is absent **or** the value is ``None`` (YAML/JSON null / relay empty).
    Explicit numeric values are preserved.
    """
    out = dict(model_client_config or {})
    if _needs_host_default(out.get(_STREAM_IDLE_TIMEOUT_KEY)):
        out[_STREAM_IDLE_TIMEOUT_KEY] = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    if _needs_host_default(out.get(_STREAM_FIRST_CHUNK_TIMEOUT_KEY)):
        out[_STREAM_FIRST_CHUNK_TIMEOUT_KEY] = DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    return out
