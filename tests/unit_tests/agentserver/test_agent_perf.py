# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for unified AgentPerf phase logging."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime import agent_perf as ap


@pytest.fixture(autouse=True)
def _reset_agent_perf():
    ap.reset_for_tests()
    yield
    ap.reset_for_tests()


@pytest.mark.asyncio
async def test_invoke_start_measures_gap_after_agent_ready(monkeypatch):
    lines: list[str] = []

    def _capture(msg, *args):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(ap.logger, "info", _capture)

    ap.bind_request("req-1")
    await asyncio.sleep(0.01)
    ap.mark_agent_ready("req-1")
    await asyncio.sleep(0.02)
    await ap.log_invoke_start()

    assert lines
    line = lines[-1]
    assert "phase=invoke_start" in line
    assert "request_id=req-1" in line
    assert "elapsed_ms=" in line
    assert "event_loop_lag_ms=" in line
    elapsed = float(line.split("elapsed_ms=")[1].split()[0])
    assert elapsed >= 15.0


@pytest.mark.asyncio
async def test_restore_survives_new_task(monkeypatch):
    lines: list[str] = []

    def _capture(msg, *args):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(ap.logger, "info", _capture)

    ap.bind_request("req-2")
    ap.mark_agent_ready("req-2")

    async def worker():
        ap.restore("req-2")
        await ap.log_invoke_start()
        await ap.log_runner_start()
        t_http = await ap.log_llm_http_start(body_bytes=70000)
        await asyncio.sleep(0.01)
        ap.log_llm_first_token_ms(t_http)
        ap.clear("req-2")

    await asyncio.create_task(worker())

    text = "\n".join(lines)
    assert "phase=invoke_start" in text
    assert "phase=runner_start" in text
    assert "phase=pre_llm_ms" in text
    assert "llm_http_start" in text
    assert "llm_first_token" in text
    assert "body_bytes=70000" in text


@pytest.mark.asyncio
async def test_iter_llm_stream_first_token(monkeypatch):
    lines: list[str] = []

    def _capture(msg, *args):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(ap.logger, "info", _capture)

    ap.bind_request("req-3")
    ap.mark_agent_ready("req-3")
    await ap.log_invoke_start()
    await ap.log_runner_start()

    async def gen():
        yield "a"
        yield "b"

    out = [x async for x in ap.iter_llm_stream(gen(), body_bytes=12)]

    assert out == ["a", "b"]
    text = "\n".join(lines)
    assert "llm_http_start" in text
    assert "llm_first_token" in text


def test_approx_json_bytes():
    n = ap.approx_json_bytes({"a": "你好"})
    assert n > 0
