# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External-channel output gate contract tests (BUG-1 F-1/F-2).

Covers the verbosity ladder, the always-pass classes (final answer, failures,
owner interactions), channel gating scope, and the Front forwarding choke-point
integration: gated noise is dropped and acked while must-land frames deliver.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import KIND_WIRE
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.output_policy import (
    VERBOSITY_FINAL,
    VERBOSITY_PROGRESS,
    VERBOSITY_VERBOSE,
    OutputPolicy,
    classify_event,
    event_type_of,
    load_output_policy,
    passes_gate,
)
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


class _Connection:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        self.reads = 0
        self.closed = False

    async def recv(self) -> dict:
        message = await self.messages.get()
        self.reads += 1
        return message

    async def close(self) -> None:
        self.closed = True


class _WebSocket:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, raw: str) -> None:
        import json

        self.frames.append(json.loads(raw))


async def _until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def _running(supervisor: RuntimeSupervisor):
    conn = _Connection()
    supervisor._conn = conn
    supervisor._generation = supervisor._peer_generation = 1
    supervisor._last_rx = time.monotonic()
    pump = asyncio.create_task(supervisor._pump())
    try:
        yield conn, pump
    finally:
        supervisor._stopping = True
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


def _wire(request_id: str, sequence: int, event: str) -> dict:
    return {
        "kind": KIND_WIRE,
        "generation": 1,
        "request_id": request_id,
        "payload": {
            "request_id": request_id,
            "sequence": sequence,
            "body": {"event_type": event},
        },
    }


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize("event", [
    "chat.final",
    "chat.error",
    "chat.ask_user_question",
    "chat.permission_request",
    "chat.approval_result",
    "goal.confirm_required",
    "chat.interrupt_result",
    "chat.file",
])
def test_must_land_events_classify_always(event: str) -> None:
    assert classify_event(event) == "always"


@pytest.mark.parametrize("event", [
    "chat.delta",
    "chat.processing_status",
    "chat.reasoning",
    "chat.tool_call",
    "chat.subagent_activity",
    "todo.updated",
    "team.member",
])
def test_noise_events_classify_progress(event: str) -> None:
    assert classify_event(event) == "progress"


def test_unknown_event_classifies_unknown() -> None:
    assert classify_event("brand.new_event") == "unknown"
    assert classify_event("") == "unknown"


def test_event_type_of_unwraps_nested_delta() -> None:
    wire = {"body": {"delta": {"event_type": "chat.reasoning"}}}
    assert event_type_of(wire) == "chat.reasoning"
    assert event_type_of({"payload": {"event_type": "chat.delta"}}) == "chat.delta"
    assert event_type_of({"response_kind": "chat.final"}) == "chat.final"


# ---------------------------------------------------------------- gate matrix


def test_final_drops_noise_keeps_must_land() -> None:
    assert passes_gate(VERBOSITY_FINAL, "chat.final") is True
    assert passes_gate(VERBOSITY_FINAL, "chat.error") is True
    assert passes_gate(VERBOSITY_FINAL, "chat.ask_user_question") is True
    assert passes_gate(VERBOSITY_FINAL, "chat.delta") is False
    assert passes_gate(VERBOSITY_FINAL, "chat.tool_call") is False
    # Unknown frames fail closed under final.
    assert passes_gate(VERBOSITY_FINAL, "brand.new_event") is False


def test_progress_and_verbose_pass_noise() -> None:
    assert passes_gate(VERBOSITY_PROGRESS, "chat.delta") is True
    assert passes_gate(VERBOSITY_PROGRESS, "brand.new_event") is True
    assert passes_gate(VERBOSITY_VERBOSE, "chat.tool_call") is True


def test_invalid_verbosity_falls_back_to_final() -> None:
    assert passes_gate("nonsense", "chat.delta") is False
    assert passes_gate("nonsense", "chat.final") is True


def test_gate_for_channel_scope() -> None:
    gate = OutputPolicy(verbosity=VERBOSITY_FINAL)
    assert gate.gate_for("feishu") == VERBOSITY_FINAL
    assert gate.gate_for("FeiShu ") == VERBOSITY_FINAL
    assert gate.gate_for("web") is None
    assert gate.gate_for("__cron__") is None
    assert gate.gate_for("") is None


def test_load_policy_defaults_without_config(monkeypatch, tmp_path) -> None:
    from jiuwenswarm.common import utils as common_utils

    monkeypatch.delenv("JIUWENSWARM_OUTPUT_VERBOSITY", raising=False)
    monkeypatch.setattr(common_utils, "get_config_file", lambda: tmp_path / "missing.yaml")
    parsed = load_output_policy()
    assert parsed.verbosity == VERBOSITY_FINAL
    assert "feishu" in parsed.channels
    assert parsed.source == "default"


def test_load_policy_reads_config_section(monkeypatch, tmp_path) -> None:
    from jiuwenswarm.common import utils as common_utils

    cfg = tmp_path / "config.yaml"
    cfg.write_text("output:\n  external:\n    verbosity: progress\n    channels: [feishu, mattermost]\n", encoding="utf-8")
    monkeypatch.delenv("JIUWENSWARM_OUTPUT_VERBOSITY", raising=False)
    monkeypatch.setattr(common_utils, "get_config_file", lambda: cfg)
    parsed = load_output_policy()
    assert parsed.verbosity == VERBOSITY_PROGRESS
    assert parsed.channels == frozenset({"feishu", "mattermost"})
    assert parsed.source == "config"


def test_load_policy_env_override_beats_config(monkeypatch, tmp_path) -> None:
    from jiuwenswarm.common import utils as common_utils

    cfg = tmp_path / "config.yaml"
    cfg.write_text("output:\n  external:\n    verbosity: verbose\n", encoding="utf-8")
    monkeypatch.setenv("JIUWENSWARM_OUTPUT_VERBOSITY", "final")
    monkeypatch.setattr(common_utils, "get_config_file", lambda: cfg)
    parsed = load_output_policy()
    assert parsed.verbosity == VERBOSITY_FINAL
    assert parsed.source == "env"


def test_load_policy_invalid_config_value_falls_back(monkeypatch, tmp_path) -> None:
    from jiuwenswarm.common import utils as common_utils

    cfg = tmp_path / "config.yaml"
    cfg.write_text("output:\n  external:\n    verbosity: yolo\n", encoding="utf-8")
    monkeypatch.delenv("JIUWENSWARM_OUTPUT_VERBOSITY", raising=False)
    monkeypatch.setattr(common_utils, "get_config_file", lambda: cfg)
    assert load_output_policy().verbosity == VERBOSITY_FINAL


# ---------------------------------------------------------------- choke-point


def _supervisor(gate: OutputPolicy) -> RuntimeSupervisor:
    return RuntimeSupervisor(Readiness(), forwarder=EventForwarder(), output_policy=gate)


@pytest.mark.asyncio
async def test_final_verbosity_drops_noise_and_delivers_must_land() -> None:
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    supervisor._route_channels["r1"] = "feishu"
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0, "chat.delta"))
        await conn.messages.put(_wire("r1", 1, "chat.tool_call"))
        await conn.messages.put(_wire("r1", 2, "chat.final"))
        await _until(lambda: conn.reads == 3)
        assert [frame["body"]["event_type"] for frame in ws.frames] == ["chat.final"]
        assert supervisor._dropped_output_total == 2


@pytest.mark.asyncio
async def test_ask_user_lands_under_final_verbosity() -> None:
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    supervisor._route_channels["r1"] = "feishu"
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0, "chat.ask_user_question"))
        await conn.messages.put(_wire("r1", 1, "chat.error"))
        await _until(lambda: len(ws.frames) == 2)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_ungated_channel_is_never_touched() -> None:
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    supervisor._route_channels["r1"] = "web"
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0, "chat.delta"))
        await conn.messages.put(_wire("r1", 1, "chat.reasoning"))
        await _until(lambda: len(ws.frames) == 2)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_unattributed_wire_fails_open() -> None:
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["push"] = (ws, lock)
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("push", 0, "chat.delta"))
        await _until(lambda: len(ws.frames) == 1)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_unary_answer_frame_fails_open_under_final() -> None:
    """GMK 2026-11-17 incident pin: the accept path attributes only
    conversation streams to a channel, so a unary call's answer — the
    ``e2a.complete`` frame the caller blocks on (IM attachment persist
    ``im.file_persist``/``file.upload_chunk``) — carries no channel
    attribution and fails open instead of hanging the caller to its 600s
    non-stream timeout.
    """
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    rid = "feishu:om_x100b60f5427a20b0b3c7164c2d50aaa"
    supervisor._routes[rid] = (ws, lock)
    # deliberately no _route_channels entry: non-stream calls never register one
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire(rid, 0, "e2a.complete"))
        await _until(lambda: len(ws.frames) == 1)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_verbose_channel_delivers_everything() -> None:
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_VERBOSE))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    supervisor._route_channels["r1"] = "feishu"
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(_wire("r1", 0, "chat.tool_result"))
        await conn.messages.put(_wire("r1", 1, "brand.new_event"))
        await _until(lambda: len(ws.frames) == 2)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_gate_drop_logs_are_throttled_and_summarized(caplog) -> None:
    """#6793: 首条 + 每 N 汇总 + 流终汇总；dropped_total 不受节流影响。"""
    import logging

    import jiuwenswarm.server.supervisor.runtime_supervisor as rs

    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._routes["r1"] = (ws, lock)
    supervisor._route_channels["r1"] = "feishu"
    every = rs._OUTPUT_GATE_NOTICE_EVERY

    def noise():
        return {"request_id": "r1", "body": {"event_type": "chat.delta"}}

    caplog.set_level(logging.INFO, logger=rs.__name__)
    for _ in range(every * 2):
        assert supervisor._output_gate_drops(noise(), "r1", "r1") is True
    drop_logs = [r.getMessage() for r in caplog.records if "output gate dropped frame" in r.getMessage()]
    assert len(drop_logs) == 2, f"expected notices at 1 and {every + 1}, got {len(drop_logs)}"
    assert f"dropped_request_total={every + 1}" in drop_logs[1]  # notices at 1 and every+1
    assert supervisor._dropped_output_total == every * 2

    assert supervisor._output_gate_drops(
        {"request_id": "r1", "body": {"event_type": "chat.final"}}, "r1", "r1") is False
    summary = [r.getMessage() for r in caplog.records if "output gate summary" in r.getMessage()]
    assert len(summary) == 1 and f"dropped_request_total={every * 2}" in summary[0]
    assert supervisor._gate_drop_notices == {}


@pytest.mark.asyncio
async def test_unary_answer_not_attributed_via_request_id_fallback() -> None:
    """atomgit 2026-11-18 #10309：一元应答不得经 request_id 回退命中并发流归属。

    一元 Gateway fetch-* 调用与同会话受门流共用同一 request_id（渠道:消息 id
    形态）时，旧实现 route_key(dispatch_id) 查空后无条件回退 request_id 查 →
    一元 e2a.complete 判 unknown 被门丢弃 → 调用方挂到 600s 非流式超时
    （GMK 2026-11-17 附件事故复发路径）。
    """
    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    rid = "feishu:om_shared1"
    supervisor._routes["disp-unary"] = (ws, lock)
    supervisor._route_channels["disp-stream"] = "feishu"
    supervisor._route_channels[rid] = "feishu"  # 并发流的 accept 侧归属（含 request_id 拷贝）
    msg = _wire(rid, 0, "e2a.complete")
    msg["dispatch_id"] = "disp-unary"  # 一元调用未被归属（dispatch_id 不在归属表）
    async with _running(supervisor) as (conn, _pump):
        await conn.messages.put(msg)
        await _until(lambda: len(ws.frames) == 1)
        assert supervisor._dropped_output_total == 0


@pytest.mark.asyncio
async def test_accept_path_attributes_only_streams() -> None:
    """atomgit 2026-11-18 #10309：accept 路径归属条件本身受用例锁定。

    事故 pin 用例只钉住「无归属时门放行」，把 runtime_supervisor.py accept
    路径的条件改回无条件登记依旧全绿；本用例驱动 dispatch_parsed_request
    真实登记流程——is_stream=False 不写任何归属键；is_stream=True 且渠道受管
    时 dispatch_id 与 request_id 双键登记。
    """
    from contextlib import suppress
    from types import SimpleNamespace

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.ipc.protocol import KIND_REQUEST

    supervisor = _supervisor(OutputPolicy(verbosity=VERBOSITY_FINAL))
    ws, lock = _WebSocket(), asyncio.Lock()
    sent: list[dict] = []

    async def _send(message: dict) -> None:
        sent.append(message)

    async with _running(supervisor) as (conn, _pump):
        conn.send = _send  # type: ignore[method-assign]
        supervisor._validated_conn = conn
        supervisor._affinity = SimpleNamespace(worker_for=lambda sid: "w-gate")
        supervisor._registry = SimpleNamespace(get=lambda wid: object() if wid == "w-gate" else None)
        supervisor._stream_worker_identity = lambda: None  # type: ignore[assignment]

        async def _dispatch(rid: str, *, stream: bool) -> dict[str, str | None]:
            """驱动一次真实 accept 登记，返回在飞期归属快照。

            归属键随请求收尾在 finally 中弹出，故断言面取「在飞快照」
            （future 未决、任务挂起时抓取）；收尾后再核验清理契约。
            """
            request = AgentRequest(
                request_id=rid, channel_id="feishu", session_id="sess-gate",
                req_method=ReqMethod.CHAT_SEND, params={"query": "hi"},
                user_id="u-1", is_stream=stream,
            )
            task = asyncio.create_task(
                supervisor.dispatch_parsed_request(ws, request, lock))
            await _until(lambda: any(m.get("kind") == KIND_REQUEST for m in sent))
            disp = [m for m in sent if m.get("kind") == KIND_REQUEST][-1]["dispatch_id"]
            live = {
                "disp": supervisor._route_channels.get(disp),
                "rid": supervisor._route_channels.get(rid),
                "disp_key": disp in supervisor._route_channels,
                "rid_key": rid in supervisor._route_channels,
            }
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # 收尾契约：无论流式与否，finally 必须成对弹清，不跨请求泄漏归属
            assert disp not in supervisor._route_channels
            assert rid not in supervisor._route_channels
            return live

        live_stream = await _dispatch("req-stream", stream=True)
        assert live_stream["disp"] == "feishu" and live_stream["rid"] == "feishu"
        sent.clear()
        live_unary = await _dispatch("req-unary", stream=False)
        assert not live_unary["disp_key"] and not live_unary["rid_key"]
