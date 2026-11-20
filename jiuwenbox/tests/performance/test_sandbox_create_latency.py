"""End-to-end sandbox creation latency against a running jiuwenbox server.

Each sample times ``POST /api/v1/sandboxes`` until the sandbox is ``ready``.
The server returns only after runtime startup, so this includes provisioning.
If the response is still ``provisioning``, the test polls ``GET`` until ready
or the ready timeout elapses. Delete time is not part of ``create_ms``.

Sample count and concurrent width reuse the performance suite knobs:

- ``JIUWENBOX_PERF_LOOP``: sequential create/delete iterations (default 8)
- ``JIUWENBOX_PERF_CONCURRENCY``: sandboxes created together (default 4)

``JIUWENBOX_PERF_SANDBOX_RUNTIME`` selects the create body. ``process`` (default)
appends user and group ``app`` and read-write binds of ``/app/build`` and ``/app/dist``
onto the server default policy. ``conch`` requires ``JIUWENBOX_CONCH_TEMPLATE_NAME``
and skips otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from dataclasses import dataclass

import pytest

from tests.performance.conftest import process_create_body

pytestmark = [pytest.mark.performance, pytest.mark.slow, pytest.mark.asyncio]

logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 180.0
_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class CreateSample:
    index: int
    sandbox_id: str
    create_ms: float


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _create_payload() -> dict:
    runtime = os.environ.get("JIUWENBOX_PERF_SANDBOX_RUNTIME", "process").strip().lower()
    if runtime in {"", "process", "bwrap"}:
        return process_create_body()
    if runtime != "conch":
        pytest.fail(
            "JIUWENBOX_PERF_SANDBOX_RUNTIME must be 'process' or 'conch', "
            f"got {runtime!r}"
        )
    template_name = (os.environ.get("JIUWENBOX_CONCH_TEMPLATE_NAME") or "").strip()
    if not template_name:
        pytest.skip(
            "Set JIUWENBOX_CONCH_TEMPLATE_NAME to measure Conch sandbox create latency"
        )
    return {
        "sandbox_runtime": "conch",
        "policy_mode": "append",
        "policy": {"conch": {"template_name": template_name}},
    }


def _runtime_label(payload: dict) -> str:
    return str(payload.get("sandbox_runtime") or "process")


def _log_summary(
    name: str,
    samples: list[CreateSample],
    elapsed_s: float,
    *,
    runtime: str,
    warmup_ms: float | None,
) -> None:
    totals = [sample.create_ms for sample in samples]
    warmup_text = "n/a" if warmup_ms is None else f"{warmup_ms:.3f}"
    logger.info(
        "[jiuwenbox-perf] %s\n"
        "  runtime=%s  samples=%s  elapsed=%ss  warmup_ms=%s\n"
        "  create_ms: min=%.3f  avg=%.3f  p50=%.3f  p95=%.3f  max=%.3f",
        name,
        runtime,
        len(samples),
        round(elapsed_s, 3),
        warmup_text,
        min(totals),
        statistics.fmean(totals),
        _percentile(totals, 0.50),
        _percentile(totals, 0.95),
        max(totals),
    )


async def _wait_until_ready(client, sandbox_id: str, phase: str) -> None:
    deadline = time.perf_counter() + _READY_TIMEOUT_SECONDS
    while phase != "ready":
        assert phase != "error", f"sandbox {sandbox_id} entered error during create"
        assert time.perf_counter() < deadline, (
            f"sandbox {sandbox_id} was not ready within {_READY_TIMEOUT_SECONDS}s"
        )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        status = await client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status.status_code == 200, status.text
        phase = status.json().get("phase")


async def _create_ready_sandbox(client, payload: dict, index: int) -> CreateSample:
    started = time.perf_counter()
    response = await client.post("/api/v1/sandboxes", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    sandbox_id = body["id"]
    await _wait_until_ready(client, sandbox_id, body.get("phase"))
    create_ms = (time.perf_counter() - started) * 1000
    return CreateSample(index=index, sandbox_id=sandbox_id, create_ms=create_ms)


async def _delete_sandbox(client, sandbox_id: str) -> None:
    response = await client.delete(f"/api/v1/sandboxes/{sandbox_id}")
    assert response.status_code in (200, 202, 204, 404), response.text


async def test_sandbox_create_latency_sequential(perf_client, perf_loop):
    """Create one sandbox at a time and record create-to-ready latency."""
    payload = _create_payload()
    runtime = _runtime_label(payload)

    warmup = await _create_ready_sandbox(perf_client, payload, -1)
    await _delete_sandbox(perf_client, warmup.sandbox_id)

    started = time.perf_counter()
    samples: list[CreateSample] = []
    try:
        for index in range(perf_loop):
            sample = await _create_ready_sandbox(perf_client, payload, index)
            samples.append(sample)
            await _delete_sandbox(perf_client, sample.sandbox_id)
        elapsed_s = time.perf_counter() - started
    finally:
        await perf_client.cleanup_sandboxes()

    assert len(samples) == perf_loop
    _log_summary(
        "sandbox_create_latency_sequential",
        samples,
        elapsed_s,
        runtime=runtime,
        warmup_ms=warmup.create_ms,
    )


async def test_sandbox_create_latency_concurrent(perf_client, perf_concurrency):
    """Create several sandboxes together and record each create-to-ready latency."""
    payload = _create_payload()
    runtime = _runtime_label(payload)
    started = time.perf_counter()
    try:
        samples = list(
            await asyncio.gather(
                *(
                    _create_ready_sandbox(perf_client, payload, index)
                    for index in range(perf_concurrency)
                ),
            ),
        )
        elapsed_s = time.perf_counter() - started
    finally:
        await perf_client.cleanup_sandboxes()

    assert len(samples) == perf_concurrency
    _log_summary(
        "sandbox_create_latency_concurrent",
        samples,
        elapsed_s,
        runtime=runtime,
        warmup_ms=None,
    )
