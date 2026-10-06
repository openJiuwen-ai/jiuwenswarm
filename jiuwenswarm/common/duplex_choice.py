# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared typed-choice request transport for Jev and Clef, without retries."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from jiuwenswarm.common.duplex_decision import action_from_answer, probability
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage, env_secret

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_INTERRUPT_THRESHOLD = 0.9


def interrupt_threshold(value: Any, *, provider: str) -> float:
    threshold = probability(value, provider=provider)
    if threshold <= 0.5:
        raise ValueError(f"{provider} interrupt_threshold must be greater than 0.5 and at most 1")
    return threshold


def request_timeout(value: Any, *, provider: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{provider} timeout_seconds must be finite and positive")
    return float(value)


def credential(env_name: str, *, provider: str) -> str:
    value = env_secret(env_name)
    if not value:
        raise ValueError(f"{provider} API key/token environment variable is empty or missing")
    return value


def endpoint_url(value: str, *, provider: str) -> httpx.URL:
    url = httpx.URL(value)
    if (url.scheme not in ("http", "https") or not url.host
            or url.userinfo or url.query or url.fragment):
        raise ValueError(f"invalid {provider} api_base")
    return url


def choice_request(model: str, state: Any, instructions: str) -> dict[str, Any]:
    # Keep provider state encoding and instructions at the adapter boundary.
    return {"model": model, "state": state, "questions": {"action": {
        "type": "choice", "instructions": instructions,
        "criteria": {
            "APPEND": "The message does not invalidate the current plan or next action, or evidence is insufficient.",
            "INTERRUPT": "A valid changed goal, hard constraint, or evidence of a mistake invalidates the current plan or next action.",
        },
    }}}


def decision_from_body(body: Any, threshold: float, *, provider: str) -> dict[str, str]:
    return action_from_answer(body["answers"]["action"], threshold, provider=provider)


def log_decision(body: Any, decision: dict[str, str], threshold: float, *,
                 provider: str, logger: logging.Logger, request_id: str = "standalone",
                 latency_ms: float = 0) -> None:
    # Only log fields after strict validation; never log the response body.
    answer = body["answers"]["action"]
    reason = ("threshold_met" if decision["action"] == "INTERRUPT" else
              "below_threshold" if answer["choice"] == "INTERRUPT" else "model_append")
    logger.info("%s decision request_id=%s choice=%s effective=%s p_interrupt=%.6f p_append=%.6f "
                "threshold=%.6f action=%s reason=%s latency_ms=%.1f",
                provider, request_id, answer["choice"], decision["action"],
                answer["probabilities"]["INTERRUPT"], answer["probabilities"]["APPEND"],
                threshold, decision["action"], reason, latency_ms)


async def classify_choice(
    snapshot: ControlSnapshot,
    messages: tuple[InboundMessage, ...],
    *,
    provider: str,
    logger: logging.Logger,
    timeout_seconds: Any,
    threshold: Any,
    build_request: Callable[[], tuple[httpx.URL, dict[str, Any], str]],
    unwrap: Callable[[Any], Any] = lambda payload: payload,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """One HTTP request; caller owns injected clients and the total deadline."""
    request_id = uuid.uuid4().hex[:16]
    try:
        timeout = request_timeout(timeout_seconds, provider=provider)
        threshold = interrupt_threshold(threshold, provider=provider)
        url, request, api_key = build_request()
    except Exception as exc:
        logger.warning("%s configuration failed request_id=%s error_type=%s",
                       provider, request_id, type(exc).__name__)
        raise
    owns_client = client is None
    client = client if client is not None else httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    started = time.monotonic()
    logger.info("%s request start request_id=%s model=%s message_ids=%s snapshot_version=%s "
                "checkpoint_id=%s phase=%s message_count=%d timeout_seconds=%.2f threshold=%.3f",
                provider, request_id, request["model"], json.dumps([m.message_id for m in messages]),
                snapshot.context_version, snapshot.checkpoint_id, snapshot.phase,
                len(messages), timeout, threshold)
    try:
        response = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=request)
        logger.info("%s response request_id=%s status_code=%s latency_ms=%.1f",
                    provider, request_id, getattr(response, "status_code", "unknown"),
                    (time.monotonic() - started) * 1000)
        response.raise_for_status()
        body = unwrap(response.json())
        decision = decision_from_body(body, threshold, provider=provider)
        log_decision(body, decision, threshold, provider=provider, logger=logger,
                     request_id=request_id, latency_ms=(time.monotonic() - started) * 1000)
        return decision
    except httpx.TimeoutException as exc:
        logger.warning("%s request timeout request_id=%s latency_ms=%.1f",
                       provider, request_id, (time.monotonic() - started) * 1000)
        raise TimeoutError(f"{provider} request timed out") from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("%s request failed request_id=%s status_code=%s latency_ms=%.1f",
                       provider, request_id, getattr(exc.response, "status_code", "unknown"),
                       (time.monotonic() - started) * 1000)
        raise
    except asyncio.CancelledError:
        logger.info("%s request cancelled request_id=%s latency_ms=%.1f reason=caller_cancelled",
                    provider, request_id, (time.monotonic() - started) * 1000)
        raise
    except Exception as exc:
        logger.warning("%s request or response failed request_id=%s error_type=%s latency_ms=%.1f",
                       provider, request_id, type(exc).__name__, (time.monotonic() - started) * 1000)
        raise
    finally:
        if owns_client:
            await client.aclose()
