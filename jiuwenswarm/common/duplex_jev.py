# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jev endpoint adapter for the shared typed-choice supervisor transport."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from jiuwenswarm.common.duplex_choice import (
    DEFAULT_INTERRUPT_THRESHOLD, DEFAULT_TIMEOUT_SECONDS, choice_request,
    classify_choice, credential, decision_from_body, endpoint_url, log_decision,
)
from jiuwenswarm.common.duplex_router import (
    ROUTING_INSTRUCTIONS, ControlSnapshot, InboundMessage, state_for,
)

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_API_BASE = "https://api.typesafe.ai/v1"
logger = logging.getLogger(__name__)


def _decision(payload: Any, threshold: float) -> dict[str, str]:
    """Compatibility entry point for direct typed-answer validation."""
    decision = decision_from_body(payload, threshold, provider="Jev")
    log_decision(payload, decision, threshold, provider="Jev", logger=logger)
    return decision


def _request(snapshot: ControlSnapshot, messages: tuple[InboundMessage, ...],
             settings: Mapping[str, Any], model_name: str) -> tuple[httpx.URL, dict[str, Any], str]:
    api_key = credential(settings.get("api_key_env", "TYPESAFE_API_KEY"), provider="Jev")
    api_base = settings.get("api_base", DEFAULT_API_BASE).rstrip("/")
    endpoint_path = settings.get("endpoint_path", "systemone")
    if endpoint_path not in ("systemone", "decisions"):
        raise ValueError("invalid Jev endpoint_path")
    url = endpoint_url(api_base + "/" + endpoint_path, provider="Jev")
    request = choice_request(model_name, state_for(snapshot, messages), ROUTING_INSTRUCTIONS)
    return url, request, api_key


async def classify_jev(
    snapshot: ControlSnapshot,
    messages: tuple[InboundMessage, ...],
    *,
    model_name: str = DEFAULT_MODEL,
    settings: Mapping[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Preserve Jev's object-state payload and configurable decisions endpoint."""
    configured = settings if settings is not None else {}
    return await classify_choice(
        snapshot, messages, provider="Jev", logger=logger, timeout_seconds=timeout_seconds,
        threshold=configured.get("interrupt_threshold", DEFAULT_INTERRUPT_THRESHOLD),
        build_request=lambda: _request(snapshot, messages, configured, model_name), client=client,
    )
