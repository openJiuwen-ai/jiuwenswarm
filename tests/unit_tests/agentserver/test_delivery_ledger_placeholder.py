# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""交付台账与占位卡（P6 静默吞回复兜底）单测 A~K。

契约：specs/jiuwenswarm/server/supervisor/S_01_delivery-ledger.md
（台账字段/落盘分级/白名单计时/sweep 生命周期/占位帧线缆五要素/
KIND_SUBSCRIBE_BY_ID 身份型重放/互斥与占位状态机）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    parse_agent_server_wire_chunk,
)
from e2a.message import EventType

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.protocol import (
    KIND_SUBSCRIBE_BY_ID,
    KIND_WIRE,
)
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor as module
from jiuwenswarm.server.supervisor.delivery_ledger import (
    DELIVERY_IDENTITY_METADATA_KEYS,
    PLACEHOLDER_FAILED,
    PLACEHOLDER_PENDING,
    PLACEHOLDER_PENDING_FLUSH,
    PLACEHOLDER_RETRACTED,
    PLACEHOLDER_SENT,
    DeliveryLedger,
    build_placeholder_wire,
)
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


class _Connection:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.reads = 0
        self.closed = False

    async def recv(self) -> dict:
        message = await self.messages.get()
        self.reads += 1
        return message

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class _WebSocket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.frames: list[dict] = []
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def send(self, raw: str) -> None:
        await self.release.wait()
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


def _request(rid: str = "req-1") -> AgentRequest:
    return AgentRequest(
        request_id=rid,
        channel_id="feishu",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "查看待办和提醒"},
        user_id="u-1",
        metadata={
            "feishu_chat_id": "oc_x",
            "feishu_open_id": "ou_y",
            "app_id": "app_z",
            "chat_type": "group",
        },
    )


def _final_payload(rid: str, content: str = "done") -> dict:
    chunk_wire = encode_agent_chunk_for_wire(
        AgentRequestChunkShim(rid, {"event_type": "chat.final", "content": content}),
        response_id=rid,
        sequence=1,
    )
    return chunk_wire


def AgentRequestChunkShim(rid, payload):  # noqa: N802 (局部便捷构造)
    from jiuwenswarm.common.schema.agent import AgentResponseChunk

    return AgentResponseChunk(
        request_id=rid, channel_id="feishu", payload=payload, is_complete=True, agent_ref=None
    )


def _registered_supervisor(ledger_dir: Path, *, idle: float = 600.0) -> RuntimeSupervisor:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    supervisor._delivery_ledger = DeliveryLedger(directory=ledger_dir, idle_seconds=idle)
    return supervisor


def _register(supervisor: RuntimeSupervisor, *, rid: str = "req-1",
              deadline_in: float = 600.0, dispatch_id: str = "d-1") -> None:
    supervisor._delivery_ledger.register(
        _request(rid), dispatch_id=dispatch_id, worker_instance="w-1",
        deadline=time.time() + deadline_in,
    )


# ---------------------------------------------------------------- A
async def test_a_online_expiry_pushes_placeholder(tmp_path) -> None:
    """在线+worker 卡死（零可见输出）：sweep 到期经网关 socket 直发占位卡。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, deadline_in=0.0)
    entry = supervisor._delivery_ledger.entry("req-1")
    assert entry.placeholder_state == PLACEHOLDER_PENDING

    await supervisor._delivery_sweep_once()

    assert len(ws.frames) == 1
    wire = ws.frames[0]
    metadata = wire.get("metadata") or {}
    # 五要素（C-1）：push 键 / 顶层 request_id / 顶层 session+channel / chat.error / 身份键
    assert metadata.get(E2A_WIRE_SERVER_PUSH_KEY) is True
    assert wire["request_id"] == "req-1:p6"
    assert wire["session_id"] == "sess-1" and wire["channel_id"] == "feishu"
    chunk = parse_agent_server_wire_chunk(wire)
    assert chunk.payload["event_type"] == "chat.error"
    assert chunk.payload["request_id"] == "req-1"
    assert chunk.payload.get("resend_hint")
    for key in ("feishu_chat_id", "feishu_open_id", "app_id"):
        assert metadata.get(key) == {"feishu_chat_id": "oc_x", "feishu_open_id": "ou_y",
                                     "app_id": "app_z"}[key]
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_SENT


# ---------------------------------------------------------------- B
async def test_b_liveness_resets_on_any_frame_visible_marks_output(tmp_path) -> None:
    """P7 活性语义：任意送达帧重置 deadline；白名单帧额外记可见时间戳。

    纯工具执行的长轮次零可见帧属健康形态（帧流在推进=worker 未中断），
    占位卡只对「管线整体静默」报警。
    """
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)

    nonvisible = {"payload": {"event_type": "processing_status"}, "metadata": {
        "runtime_event": {"sequence": 7}}}
    ledger.on_wire_delivered(request_id="req-1", payload=nonvisible, now=1000.0)
    assert ledger.entry("req-1").deadline == pytest.approx(1000.0 + 600.0)
    assert ledger.entry("req-1").sequence == 7
    # 活性重置不更新可见时间戳（可见静默时长仍可观测）。
    assert ledger.entry("req-1").last_visible_output_ts == 0.0
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_PENDING

    visible = _final_payload("req-1")
    visible.setdefault("metadata", {}).setdefault("runtime_event", {})["sequence"] = 8
    ledger.on_wire_delivered(request_id="req-1", payload=visible, now=1001.0)
    assert ledger.entry("req-1").deadline == pytest.approx(1001.0 + 600.0)
    assert ledger.entry("req-1").last_visible_output_ts == 1001.0
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED


async def test_b2_pipeline_silence_still_alarms(tmp_path) -> None:
    """P7 反向面：帧流停止后 deadline 不再被推进，到期照发占位卡。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=1000.0 + 600.0)
    nonvisible = {"payload": {"event_type": "processing_status"}, "metadata": {
        "runtime_event": {"sequence": 1}}}
    ledger.on_wire_delivered(request_id="req-1", payload=nonvisible, now=1000.0)
    # 之后再无任何帧：deadline 停在最后一次活性时刻，sweep 判到期。
    assert ledger.due_entries(now=1000.0 + 600.0 + 1.0) == [ledger.entry("req-1")]


async def test_b3_control_plane_requests_skip_registration(tmp_path) -> None:
    """P7 控制面豁免：cron/healthcheck 渠道与 interrupt_ 前缀请求不建条目。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    for rid, channel in (("cron-job-1:1794003600", "__cron__"),
                         ("healthcheck-abc_000813", "__health_check__"),
                         ("interrupt_1a1b290647e_e85ed3", "feishu")):
        request = _request(rid)
        request.channel_id = channel
        assert ledger.register(request, dispatch_id="d-1", worker_instance="w-1",
                               deadline=time.time() + 600.0) is None
        assert ledger.entry(rid) is None
    # 渠道与 request_id 双判据各自独立生效（feishu 渠道 interrupt 前缀同样豁免）。
    assert ledger.entries() == []



async def test_b5_cancel_and_interrupt_hyphen_exempt(tmp_path) -> None:
    """SF-4 豁免三分支锁定（AIReview SF-5）：CHAT_CANCEL 短路豁免（process_cli
    复用原 rid 形态）与 interrupt- 连字符前缀豁免（CLI 形态）均不建条目。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    # ① CHAT_CANCEL 短路：无论 rid 形态，取消请求一律豁免。
    cancel_req = _request("req-cancel-1")
    cancel_req.req_method = ReqMethod.CHAT_CANCEL
    assert ledger.register(cancel_req, dispatch_id="d-c", worker_instance="w-1",
                           deadline=time.time() + 600.0) is None
    assert ledger.entry("req-cancel-1") is None
    # ② interrupt- 连字符前缀（CLI 取消 rid 形态）。
    hyphen = _request("interrupt-a1b2c3d4")
    assert ledger.register(hyphen, dispatch_id="d-h", worker_instance="w-1",
                           deadline=time.time() + 600.0) is None
    assert ledger.entry("interrupt-a1b2c3d4") is None
    assert ledger.entries() == []


async def test_b6_reregister_same_rid_keeps_max_sequence(tmp_path) -> None:
    """SF-4-③ 游标保护锁定：同 rid 重登记游标取 max，复用原 rid 的覆盖登记
    不清零原请求已推进的 sequence（after=sequence 重放契约不被破坏）。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    entry = ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                            deadline=time.time() + 600.0)
    assert entry is not None and entry.sequence == 0
    # 帧送达推进游标至 7。
    ledger.on_wire_delivered(request_id="req-1", payload={
        "metadata": {"runtime_event": {"sequence": 7}}})
    assert ledger.entry("req-1").sequence == 7
    # 同 rid 重登记（模拟 process_cli 取消复用原 rid 的覆盖路径）：游标保留。
    entry2 = ledger.register(_request("req-1"), dispatch_id="d-2", worker_instance="w-1",
                             deadline=time.time() + 600.0)
    assert entry2 is not None
    assert entry2.sequence == 7
    assert ledger.entry("req-1").sequence == 7



async def test_b7_cron_intermediate_control_requests_exempt(tmp_path) -> None:
    """cron 派发的中间控制请求（命令回执/内部取数）豁免登记（GMK 2026-11-11
    上午 2 次占位卡误发实锚）：channel=web 封装逃过 __cron__ 渠道豁免，
    worker 从不回帧（seq=0），600s sweep 到期误发。前缀豁免锁定。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    for rid in ("cron-command-ack-cron-65e65b1eeaa94852953986b991ccea0a",
                "fetch-1794363124618066376-67b033e8"):
        request = _request(rid)
        request.channel_id = "web"  # GMK 实锚形态：cron 中间请求经 web 渠道封装
        assert ledger.register(request, dispatch_id="d-1", worker_instance="w-1",
                               deadline=time.time() + 600.0) is None
        assert ledger.entry(rid) is None
    assert ledger.entries() == []


async def test_b4_user_channel_requests_still_register(tmp_path) -> None:
    """P7 豁免不外溢：用户渠道普通请求照常登记。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    entry = ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                            deadline=time.time() + 600.0)
    assert entry is not None and ledger.entry("req-1") is entry


# ---------------------------------------------------------------- C
async def test_c_front_restart_reloads_and_judges_wall_clock(tmp_path) -> None:
    """Front 重启：台账重载（无注册丢失）并按墙钟 deadline 判到期执行动作。"""
    directory = tmp_path / "ledger"
    old = DeliveryLedger(directory=directory)
    old.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                 deadline=time.time() - 1.0)  # 已到期（墙钟判定面）

    new = DeliveryLedger(directory=directory)
    assert new.load() == 1
    supervisor = _registered_supervisor(directory)
    supervisor._delivery_ledger = new
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    await supervisor._delivery_sweep_once()
    assert len(ws.frames) == 1 and ws.frames[0]["request_id"] == "req-1:p6"
    assert new.entry("req-1").placeholder_state == PLACEHOLDER_SENT


# ---------------------------------------------------------------- D
async def test_d_not_attached_pending_flush_then_attach_repush(tmp_path) -> None:
    """未 attach 到期→pending_flush；attach 后 sweep 周期补推。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, deadline_in=0.0)
    await supervisor._delivery_sweep_once()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_PENDING_FLUSH

    supervisor._gateway_ws, supervisor._gateway_lock = _WebSocket(), asyncio.Lock()
    await supervisor._delivery_sweep_once()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_SENT
    assert len(supervisor._gateway_ws.frames) == 1


# ---------------------------------------------------------------- E
class _FakeIpcConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


def _worker_with_frames(rid: str = "req-e") -> tuple[WorkerDispatcher, WorkerGatewayBridge]:
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = _request(rid)
    bridge.events.register(request, "disp-e")
    bridge.events.append({"request_id": rid, "is_stream": True,
                          "payload": {"payload": {"event_type": "chat.delta", "content": "a"}}},
                         "disp-e")
    bridge.events.append({"request_id": rid,
                          "payload": {"payload": {"event_type": "chat.final", "content": "done"}}},
                         "disp-e")
    return dispatcher, bridge


def _identity(channel: str = "feishu", user: str = "u-1", app: str = "app_z") -> dict:
    return {"channel_id": channel, "user_id": user, "app_id": app,
            "agent_ref": json.dumps({"mode": "agent", "id": "default"}, sort_keys=True),
            "session_id": "sess-1"}


def _wire_event_of(frame: dict) -> str | None:
    """取回放帧的 event_type（wire payload→body/payload→嵌套 delta/result 展平）。"""
    wire = frame.get("payload") or {}
    body = wire.get("body") or wire.get("payload") or {}
    if isinstance(body, dict) and isinstance(body.get("payload"), dict) and "event_type" not in body:
        body = body["payload"]
    if isinstance(body, dict):
        nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
        if isinstance(nested, dict):
            body = {**body, **nested}
        event = body.get("event_type")
        if isinstance(event, str):
            return event
    return None


async def test_e_replay_branches_miss_history_and_ok() -> None:
    """KIND_SUBSCRIBE_BY_ID：反查 miss／worker_instance 失配 history_required／命中 ok+回放。"""
    dispatcher, bridge = _worker_with_frames()
    conn = _FakeIpcConn()
    generation = bridge.holder.open_session(conn)

    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-e",
         "payload": _identity(app="wrong-app"), "after": 0}, conn, generation)
    assert conn.sent[-1]["status"] == "miss"

    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-e", "payload": _identity(),
         "worker_instance": "another-instance", "after": 0}, conn, generation)
    assert conn.sent[-1]["status"] == "history_required"

    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-e", "payload": _identity(),
         "after": 0}, conn, generation)
    reply = conn.sent[0]
    assert reply["status"] == "ok" and reply["count"] == 2
    wires = [frame for frame in conn.sent[1:] if frame.get("kind") == "wire"]
    assert [_wire_event_of(frame) for frame in wires] == ["chat.delta", "chat.final"]
    assert all(frame.get("wire_id") for frame in wires)

    # 游标之后：after=首帧 sequence → 只回 chat.final
    first_sequence = wires[0]["sequence"]
    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-e", "payload": _identity(),
         "after": first_sequence}, conn, generation)
    wires = [frame for frame in conn.sent[1:] if frame.get("kind") == "wire"]
    assert [_wire_event_of(frame) for frame in wires] == ["chat.final"]


# ---------------------------------------------------------------- F
def test_f_graded_persistence(tmp_path) -> None:
    """分级落盘：注册同步写；游标批量（阈值内不落盘）；状态转移同步写。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    path = tmp_path / "ledger" / "req-1.json"
    assert path.exists()
    before = path.read_text(encoding="utf-8")

    payload = {"payload": {"event_type": "processing_status"}, "metadata": {
        "runtime_event": {"sequence": 3}}}
    ledger.mark_sequence_only("req-1", payload, now=time.time())
    assert path.read_text(encoding="utf-8") == before  # 阈值内批量不落盘

    ledger.transition("req-1", PLACEHOLDER_PENDING_FLUSH)
    assert json.loads(path.read_text(encoding="utf-8"))["placeholder_state"] == PLACEHOLDER_PENDING_FLUSH

    # 重启无注册丢失
    reloaded = DeliveryLedger(directory=tmp_path / "ledger")
    assert reloaded.load() == 1
    assert reloaded.entry("req-1").dispatch_id == "d-1"


# ---------------------------------------------------------------- G
async def test_g_mutex_retract_only_unsent(tmp_path) -> None:
    """互斥：真实回复送达取消未送达占位（pending/pending_flush→retracted）；sent 保持。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    visible = _final_payload("req-1")
    ledger.on_wire_delivered(request_id="req-1", payload=visible)
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED

    ledger.register(_request("req-2"), dispatch_id="d-2", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    deadline_before = ledger.entry("req-2").deadline
    ledger.transition("req-2", PLACEHOLDER_SENT)
    ledger.on_wire_delivered(request_id="req-2", payload=visible)
    entry = ledger.entry("req-2")
    assert entry.placeholder_state == PLACEHOLDER_SENT  # 吸收终态，不可线缆撤回
    assert entry.deadline == pytest.approx(deadline_before)  # sent 后 deadline 停止重置


# ---------------------------------------------------------------- H
async def test_h_replay_route_rebind_and_new_cleanup_point(tmp_path) -> None:
    """Front 重启后非流式重放送达：路由重绑主路径（无 server-push 标记）；终态送达后新清理点 pop。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    async with _running(supervisor) as (conn, _pump):
        ws, lock = _WebSocket(), asyncio.Lock()
        supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
        supervisor.attach_gateway_connection(ws, lock)
        await _until(lambda: "d-1" in supervisor._routes)
        assert supervisor._routes["d-1"] == (ws, lock)
        assert "d-1" in supervisor._delivery_replay_routes
        assert conn.sent and conn.sent[0]["kind"] == KIND_SUBSCRIBE_BY_ID
        assert conn.sent[0]["payload"].keys() >= {"channel_id", "user_id", "app_id", "session_id"}
        assert "查看待办" not in json.dumps(conn.sent[0])  # 载荷仅身份字段，无用户正文

        # 重放的非流式 chat.final 经重绑路由送达（无 server-push 标记、无 is_stream）
        frame = {"kind": KIND_WIRE, "generation": 1, "request_id": "req-1",
                 "dispatch_id": "d-1", "wire_id": "w-1",
                 "payload": {"payload": {"event_type": "chat.final", "content": "late answer"}}}
        await conn.messages.put(frame)
        await _until(lambda: len(ws.frames) == 1)
        # forwarder 发送的是 wire payload（网关线形态）；非流式 chat.final 无 server-push 标记
        assert ws.frames[0]["payload"]["event_type"] == "chat.final"
        assert not (ws.frames[0].get("metadata") or {}).get(E2A_WIRE_SERVER_PUSH_KEY)
        # 新清理点：重放终态送达后 pop 路由
        await _until(lambda: "d-1" not in supervisor._routes)
        assert not supervisor._delivery_replay_routes


async def test_h2_replay_delivery_failure_falls_back(tmp_path, monkeypatch) -> None:
    """回执链 delivered=False：回落台账重试标记。"""
    monkeypatch.setattr(module, "_FORWARD_TIMEOUT", 0.05, raising=False)
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    async with _running(supervisor) as (conn, _pump):
        blocked = _WebSocket(blocked=True)
        supervisor._gateway_ws = blocked
        supervisor._gateway_lock = asyncio.Lock()
        supervisor._routes["d-1"] = (blocked, supervisor._gateway_lock)
        supervisor._delivery_replay_routes.add("d-1")
        frame = {"kind": KIND_WIRE, "generation": 1, "request_id": "req-1",
                 "dispatch_id": "d-1", "wire_id": "w-1",
                 "payload": {"payload": {"event_type": "chat.delta", "content": "x"}}}
        await conn.messages.put(frame)
        await _until(lambda: supervisor._delivery_replay_retry)
        await _until(lambda: "d-1" not in supervisor._routes)
        blocked.release.set()


# ---------------------------------------------------------------- I
async def test_i_failed_worker_placeholder_still_outbound(tmp_path) -> None:
    """worker FAILED（粘滞态）：deadline 内占位卡经网关 socket 必达；sweep 不随代际取消。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    supervisor._fatal = True
    supervisor._fatal_reason = "runtime worker failed"
    supervisor._conn = None
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, deadline_in=0.0)
    await supervisor._delivery_sweep_once()
    assert len(ws.frames) == 1 and ws.frames[0]["request_id"] == "req-1:p6"
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_SENT

    # sweep 生命周期独立：不在 side tasks 面，_cancel_side_tasks 不取消
    supervisor._ensure_delivery_sweep()
    sweep = supervisor._delivery_sweep_task
    await supervisor._cancel_side_tasks()
    assert sweep is supervisor._delivery_sweep_task and not sweep.done()
    sweep.cancel()
    await asyncio.gather(sweep, return_exceptions=True)


async def test_i2_start_side_tasks_does_not_own_sweep(tmp_path) -> None:
    """sweep 不进入 _start_side_tasks 面。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    supervisor._start_side_tasks()
    assert supervisor._delivery_sweep_task is None
    supervisor._ensure_delivery_sweep()
    sweep = supervisor._delivery_sweep_task
    supervisor._ensure_delivery_sweep()
    assert supervisor._delivery_sweep_task is sweep  # 幂等
    sweep.cancel()
    await asyncio.gather(sweep, return_exceptions=True)


# ---------------------------------------------------------------- J
async def test_j_late_real_reply_delivered_after_placeholder(tmp_path) -> None:
    """占位卡送达后迟到的真实回复仍送达（占位只作用于自身键，端到端）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, rid="req-1", deadline_in=0.0)
    await supervisor._delivery_sweep_once()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_SENT

    async with _running(supervisor) as (conn, _pump):
        supervisor._routes["d-1"] = (ws, lock)
        late_final = {"kind": KIND_WIRE, "generation": 1, "request_id": "req-1",
                      "dispatch_id": "d-1", "wire_id": "w-2",
                      "payload": _final_payload("req-1", "late real answer")}
        await conn.messages.put(late_final)
        await _until(lambda: len(ws.frames) == 2)
        assert ws.frames[0]["request_id"] == "req-1:p6"  # 占位卡（自身键）
        # 迟到真实回复（原键，仍送达）：forwarder 发 wire payload（encode body 形态）
        late = parse_agent_server_wire_chunk(ws.frames[1])
        assert late.request_id == "req-1"
        assert late.payload["event_type"] == "chat.final"
        assert late.payload["content"] == "late real answer"
        entry = supervisor._delivery_ledger.entry("req-1")
        assert entry.placeholder_state == PLACEHOLDER_SENT  # sent 保持不变


# ---------------------------------------------------------------- K
class _PushFramesWebSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self) -> str:
        return await self.frames.get()

    async def send(self, _data: str) -> None:
        pass

    async def close(self) -> None:
        pass


class _AgentClientHarness(WebSocketAgentServerClient):
    """复用 gateway 测试的 harness 形制（agent_client 接收循环真跑，真实构造器）。"""

    def set_ws_for_test(self, ws) -> None:
        self._ws = ws

    def set_running_for_test(self, running: bool) -> None:
        self._running = running

    def set_server_push_handler(self, handler) -> None:
        self._on_server_push = handler

    async def run_receiver_for_test(self) -> None:
        await self._message_receiver_loop()


async def test_k_placeholder_wire_contract_end_to_end(tmp_path) -> None:
    """契约测试：五要素→网关 push 分支受理→Message.id=rid+":p6"→飞书接收者可解析。

    覆盖：N-3 wire 解码版本固定（venv e2a 正典包，网关同源）；
    N-4 占位 chat.error 不触发真实回复的请求级失败处理与流状态变更；
    N-5 push 键常量两仓同源同值；身份键镜像与网关 _DELIVERY_IDENTITY_METADATA_KEYS 同值。
    """
    import e2a
    from agent_gateway.message_handler.message_handler import (
        MessageHandler,
        _DELIVERY_IDENTITY_METADATA_KEYS,
    )
    from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel

    # N-3：解码实现与网关 import 同源（venv 安装版 e2a 正典包）
    assert "site-packages" in e2a.__file__
    # N-5：push 键常量唯一真源＝e2a.constants（E2A 迁移后；与网关侧同 pin 同对象，字面互引保证 is）
    from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY as KEY_JW

    assert KEY_JW is E2A_WIRE_SERVER_PUSH_KEY
    # 身份键镜像同值（防漂移）
    assert _DELIVERY_IDENTITY_METADATA_KEYS == DELIVERY_IDENTITY_METADATA_KEYS

    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    wire = build_placeholder_wire(ledger.entry("req-1"))

    # 1) 真实 agent_client 接收循环：push 分支受理（agent_client.py:385 判定面）
    ws = _PushFramesWebSocket()
    client = _AgentClientHarness()
    client.set_ws_for_test(ws)
    client.set_running_for_test(True)
    pushed: asyncio.Queue[dict] = asyncio.Queue()

    async def on_push(frame: dict) -> None:
        pushed.put_nowait(frame)

    client.set_server_push_handler(on_push)
    ws.frames.put_nowait(json.dumps(wire))
    receiver = asyncio.create_task(client.run_receiver_for_test())
    try:
        frame = await asyncio.wait_for(pushed.get(), 2)
        assert frame["request_id"] == "req-1:p6"
        # 无 unary 等待队列可走（rid+":p6" 没有队列）——push 分支是唯一受理面
        assert "req-1:p6" not in client._message_queues
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

    # 2) 解码 + Message 重建：Message.id=rid+":p6"（N-4：占位非流终止哨兵）
    chunk = parse_agent_server_wire_chunk(wire)
    assert chunk.request_id == "req-1:p6"
    assert not MessageHandler._is_terminal_stream_chunk(chunk)
    message = MessageHandler._chunk_to_message(
        chunk, "sess-1", metadata=dict(ledger.entry("req-1").identity_metadata), app_id="app_z")
    assert message.id == "req-1:p6"
    # 按**线值**比较（跨包安全）：jws 侧 e2a 与网关侧枚举分属两包时成员对象互不等价，
    # 契约本体是 wire 值 'chat.error'，成员对象身份不参与契约。
    assert getattr(message.event_type, "value", message.event_type) == EventType.CHAT_ERROR.value and message.ok is False

    # 3) 飞书接收者可解析（_extract_receive_info 读 metadata 平台身份）
    shim = FeishuChannel.__new__(FeishuChannel)
    shim.config = SimpleNamespace(chat_id="", last_chat_id="")
    receive_id, id_type = FeishuChannel._extract_receive_info(shim, message)
    assert receive_id == "oc_x" and id_type == "chat_id"

    # 4) 真实回复键不受占位影响（键分离 + ok=True）
    real_wire = _final_payload("req-1", "real answer")
    real_chunk = parse_agent_server_wire_chunk(real_wire)
    assert real_chunk.request_id == "req-1"
    real_message = MessageHandler._chunk_to_message(
        real_chunk, "sess-1", metadata=dict(ledger.entry("req-1").identity_metadata), app_id="app_z")
    assert real_message.id == "req-1" and real_message.ok is True


# ---------------------------------------------------------------- sweep 独立行为补证
async def test_placeholder_push_failures_exhaust_to_failed(tmp_path) -> None:
    """连续发送失败留痕：达到阈值转 failed（过期审计面）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws = _WebSocket(blocked=True)
    supervisor._gateway_ws, supervisor._gateway_lock = ws, asyncio.Lock()
    _register(supervisor, rid="req-1", deadline_in=0.0)
    for _ in range(3):
        await supervisor._delivery_sweep_once()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_FAILED
    ws.release.set()


async def test_loopback_reload_skips_replay_and_audits(tmp_path, monkeypatch) -> None:
    """LOOPBACK 模式：启动重载跳过重放，遗留条目当场过期审计（不悬挂）。"""
    from jiuwenswarm.server.ipc.session import FRONT_LOOPBACK

    directory = tmp_path / "ledger"
    old = DeliveryLedger(directory=directory)
    old.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                 deadline=time.time() + 600.0)

    supervisor = _registered_supervisor(directory)
    monkeypatch.setattr(module, "FRONT_LOOPBACK", FRONT_LOOPBACK)
    supervisor._ipc_mode = FRONT_LOOPBACK
    supervisor._reload_delivery_ledger()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_FAILED


async def test_replay_rebinds_stale_gateway_route(tmp_path) -> None:
    """网关重连后重放路由必须重绑当前 socket（atomgit 9722 id=199785194）：
    旧 ws 路由占位时不重绑，worker 重放帧经 _wire_route 命中旧连接被静默丢弃。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    supervisor._gateway_ws, supervisor._gateway_lock = object(), asyncio.Lock()
    stale_ws, stale_lock = object(), asyncio.Lock()
    supervisor._routes["d-1"] = (stale_ws, stale_lock)  # 重连前旧路由
    conn = _FakeIpcConn()

    async def _run():
        await supervisor._replay_delivery_entries_locked(supervisor._delivery_ledger, conn)

    await _run()
    assert supervisor._routes["d-1"][0] is not stale_ws  # 重绑到当前 gateway socket
    assert "d-1" in supervisor._delivery_replay_routes


async def test_subscribe_reply_marks_unrecoverable(tmp_path) -> None:
    """worker 回执 miss/history_required：按不可恢复处理，不再重试重放。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    supervisor._delivery_subscribe_by_id_reply(
        {"kind": KIND_SUBSCRIBE_BY_ID, "status": "history_required", "request_id": "req-1",
         "dispatch_id": "d-1"})
    assert "req-1" in supervisor._delivery_unrecoverable
    assert "d-1" not in supervisor._delivery_replay_routes


async def test_emit_error_retracts_pending_entry(tmp_path) -> None:
    """_emit_error 直发成功同按可见帧记账：pending 台账转 retracted（互斥面）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    ws, lock = _WebSocket(), asyncio.Lock()
    await supervisor._emit_error(ws, _request("req-1"), lock, "runtime worker closed")
    assert len(ws.frames) == 1
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED


async def test_emit_error_send_failure_keeps_pending_entry(tmp_path) -> None:
    """_emit_error 发送失败（delivered=False）不得置 retracted 终态（CR-1）。

    网关已 detach 时 forwarder.send 返回 False 而非抛异常；若无视返回值照常
    on_wire_delivered，条目转 retracted 后 sweep 永不再发占位卡——用户重连后
    既无错误帧也无兜底卡，即错误路径上的静默吞回复。
    """

    class _FailForwarder:
        async def send(self, *args: object, **kwargs: object) -> bool:
            return False

    supervisor = _registered_supervisor(tmp_path / "ledger")
    supervisor._forwarder = _FailForwarder()
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    ws, lock = _WebSocket(), asyncio.Lock()
    await supervisor._emit_error(ws, _request("req-1"), lock, "runtime worker closed")
    entry = supervisor._delivery_ledger.entry("req-1")
    assert entry.placeholder_state == PLACEHOLDER_PENDING


async def test_note_forward_failure_keeps_sequence_cursor(tmp_path) -> None:
    """delivered=False 不推进重放游标：失败帧必须保留在重放窗口内（CR-2）。

    mark_sequence_only 系「传输成功」语义；失败帧误推进游标后，重放
    after=sequence 过滤会永久跳过该帧——迟到真实回复无法经重放恢复。
    """
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    message = {
        "request_id": "req-1",
        "dispatch_id": "d-1",
        "payload": {"payload": {"event_type": "chat.delta"},
                    "metadata": {"runtime_event": {"sequence": 7}}},
    }
    before = supervisor._delivery_ledger.entry("req-1").sequence
    supervisor._delivery_note_forward(message, False)
    assert supervisor._delivery_ledger.entry("req-1").sequence == before
    supervisor._delivery_note_forward(dict(message), True)
    assert supervisor._delivery_ledger.entry("req-1").sequence == 7


async def test_subscribe_reply_unrecoverable_pops_route(tmp_path) -> None:
    """miss/history_required 回执成对清理 _routes（CR-3），防条目与旧 socket 引用泄漏。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    route = (object(), asyncio.Lock())
    supervisor._routes["d-1"] = route
    supervisor._delivery_replay_route_stamps["d-1"] = route
    supervisor._delivery_replay_routes.add("d-1")
    supervisor._delivery_subscribe_by_id_reply(
        {"kind": KIND_SUBSCRIBE_BY_ID, "status": "miss", "request_id": "req-1",
         "dispatch_id": "d-1"})
    assert "req-1" in supervisor._delivery_unrecoverable
    assert "d-1" not in supervisor._delivery_replay_routes
    assert "d-1" not in supervisor._routes


async def test_worker_instance_uuid_unique() -> None:
    """幂等面：worker instance 唯一（重放失配判定依赖）。"""
    assert (WorkerGatewayBridge(ConnHolder()).events.instance
            != WorkerGatewayBridge(ConnHolder()).events.instance)


def test_identity_payload_has_no_user_content(tmp_path) -> None:
    """KIND_SUBSCRIBE_BY_ID 请求载荷仅身份字段（不存/不传用户正文）。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    entry = ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                            deadline=time.time() + 600.0)
    serialized = json.dumps({"payload": {
        "channel_id": entry.channel_id, "user_id": entry.user_id, "app_id": entry.app_id,
        "agent_ref": entry.agent_ref_json, "session_id": entry.session_id,
        "request_id": entry.request_id, "worker_instance": entry.worker_instance,
        "after": entry.sequence,
    }}, ensure_ascii=False)
    assert "查看待办" not in serialized and "query" not in serialized


def _unused_uuid_guard() -> str:  # pragma: no cover
    return uuid.uuid4().hex


# ---------------------------------------------------------------- 检视整改面（!9693 CR-001/002/004）
def test_l_terminal_visible_frames_batched_not_per_frame(tmp_path, monkeypatch) -> None:
    """CR-001：终态条目迟到白名单帧只更新内存簿记，deadline/游标随批量阈值落盘（禁逐帧 fsync）。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    ledger.on_wire_delivered(request_id="req-1", payload=_final_payload("req-1"), now=1000.0)
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED

    flushes: list[str] = []
    monkeypatch.setattr(ledger, "_flush", lambda entry: flushes.append(entry.request_id))
    delta = {"payload": {"event_type": "chat.delta", "content": "x"}}
    for index in range(30):  # <50 帧阈值且 <2s 跨度：零同步落盘
        delta["metadata"] = {"runtime_event": {"sequence": 10 + index}}
        ledger.on_wire_delivered(request_id="req-1", payload=delta, now=1001.0 + index * 0.05)
    assert flushes == []
    entry = ledger.entry("req-1")
    assert entry.last_visible_output_ts == pytest.approx(1001.0 + 29 * 0.05)
    assert entry.deadline == pytest.approx(1001.0 + 29 * 0.05 + 600.0)
    assert entry.sequence == 10 + 29

    # 真实落盘面：终态簿记（sequence/deadline/terminal_at）随批量写持久化
    monkeypatch.undo()
    ledger._flush(entry)
    data = json.loads((tmp_path / "ledger" / "req-1.json").read_text(encoding="utf-8"))
    assert data["sequence"] == entry.sequence
    assert data["deadline"] == pytest.approx(entry.deadline)
    assert data["last_visible_output_ts"] == pytest.approx(entry.last_visible_output_ts)
    assert data["terminal_at"] == pytest.approx(1000.0)


def test_m_terminal_entries_reaped_after_retention(tmp_path, caplog) -> None:
    """CR-002：终态条目超保留窗回收内存与磁盘副本（日志留痕）；未终态条目不回收。"""
    import logging

    ledger = DeliveryLedger(directory=tmp_path / "ledger", reap_seconds=100.0)
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    ledger.on_wire_delivered(request_id="req-1", payload=_final_payload("req-1"), now=time.time())
    ledger.register(_request("req-2"), dispatch_id="d-2", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    with caplog.at_level(logging.INFO, logger="jiuwenswarm.server.supervisor.delivery_ledger"):
        assert ledger.reap_terminal(now=time.time() + 1000.0) == 1
    assert ledger.entry("req-1") is None
    assert not (tmp_path / "ledger" / "req-1.json").exists()
    assert ledger.entry("req-2") is not None
    assert any("reaped terminal" in record.message for record in caplog.records)


def test_n_transition_rejected_from_terminal(tmp_path) -> None:
    """CR-004 守卫：transition 拒绝从终态迁出（retracted/sent 均吸收，防快照竞态覆盖）。"""
    ledger = DeliveryLedger(directory=tmp_path / "ledger")
    ledger.register(_request("req-1"), dispatch_id="d-1", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    ledger.on_wire_delivered(request_id="req-1", payload=_final_payload("req-1"))
    assert ledger.transition("req-1", PLACEHOLDER_SENT) is False
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED
    assert ledger.transition("req-1", PLACEHOLDER_FAILED) is False
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED

    ledger.register(_request("req-2"), dispatch_id="d-2", worker_instance="w-1",
                    deadline=time.time() + 600.0)
    assert ledger.transition("req-2", PLACEHOLDER_SENT) is True
    assert ledger.transition("req-2", PLACEHOLDER_FAILED) is False
    assert ledger.entry("req-2").placeholder_state == PLACEHOLDER_SENT


async def test_o_sweep_skips_entry_terminal_before_send(tmp_path, monkeypatch) -> None:
    """CR-004：sweep 快照到 send 之间真实回复已撤回占位 → send 前复查跳过，占位卡不出站。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, rid="req-1", deadline_in=0.0)
    ledger = supervisor._delivery_ledger
    stale = list(ledger.due_entries())  # 快照（此刻 pending）
    ledger.on_wire_delivered(request_id="req-1", payload=_final_payload("req-1"))  # 真实回复送达
    monkeypatch.setattr(ledger, "due_entries", lambda now=None: stale)

    sent_calls: list[dict] = []

    async def _fake_send(wire, ws=None, send_lock=None):
        sent_calls.append(wire)
        return True

    supervisor._forwarder = SimpleNamespace(send=_fake_send)
    await supervisor._delivery_sweep_once()
    assert sent_calls == []  # 终态条目 send 前复查直接跳过
    assert ws.frames == []
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED


async def test_o2_reply_during_send_not_overwritten_to_sent(tmp_path) -> None:
    """CR-004 端到端：send 期间真实回复送达（retracted）后，快照竞态不得覆盖成 sent。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, rid="req-1", deadline_in=0.0)
    ledger = supervisor._delivery_ledger

    async def _fake_send(wire, ws=None, send_lock=None):
        ledger.on_wire_delivered(request_id="req-1",
                                 payload=_final_payload("req-1"))  # 竞态：send 窗口内真实回复
        return True

    supervisor._forwarder = SimpleNamespace(send=_fake_send)
    await supervisor._delivery_sweep_once()
    assert ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED


async def test_note_forward_clears_replay_route_stamps(tmp_path) -> None:
    """终态清理点同步 pop _delivery_replay_route_stamps（AIReview 9722
    NH-003：stamps 条目随终态送达轮次缓慢累积）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    ws, lock = _WebSocket(), asyncio.Lock()
    stamp = (ws, lock)
    supervisor._routes["d-1"] = stamp
    supervisor._delivery_replay_routes.add("d-1")
    supervisor._delivery_replay_route_stamps["d-1"] = stamp
    supervisor._delivery_note_forward(
        {"request_id": "req-1", "dispatch_id": "d-1",
         "payload": {"payload": {"event_type": "chat.delta"}}}, delivered=False)
    assert "d-1" not in supervisor._routes
    assert "d-1" not in supervisor._delivery_replay_routes
    assert "d-1" not in supervisor._delivery_replay_route_stamps


async def test_r3_replay_does_not_mutate_retained_frame() -> None:
    """重放不得写回 event_store 保留帧：replayed 标记只进发送副本
    （atomgit 9722 Medium：原地 payload 写污染日志，其后真实首投会被
    消费侧误认已重放帧——恰是本 PR 要修的静默吞回复类失效）。"""
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    bridge.events.register(_request("req-mut"), "disp-mut")
    bridge.events.append({"request_id": "req-mut", "payload": {
        "event_type": "chat.delta", "content": "x",
        "metadata": {"runtime_event": {"sequence": 1}}}}, "disp-mut")
    class _Runtime:
        def recoverable_control(self, session_id, control_id, execution_id):
            return None
    await dispatcher.bind_runtime(_Runtime())
    conn = _FakeIpcConn()
    generation = bridge.holder.open_session(conn)
    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-mut",
         "payload": _identity(), "after": 0}, conn, generation)
    sent = [f for f in conn.sent[1:] if f.get("kind") == "wire"]
    assert sent, "重放帧应已发出"
    rt_sent = ((sent[0]["payload"].get("metadata") or {}).get("runtime_event") or {})
    assert rt_sent.get("replayed") is True
    retained = bridge.events.replay_by_request(
        bridge.events.records["disp-mut"].scope, "req-mut", after=0)[0]
    rt_kept = (((retained["payload"].get("metadata") or {}).get("runtime_event")) or {})
    assert "replayed" not in rt_kept


async def test_r4_sweep_consumes_deferred_replay_flag(tmp_path) -> None:
    """延迟标记必须有消费方：conn 缺席置位后，sweep 周期在 conn 恢复时补放重放
    并清位——「只置死标记不重试」失效面闭合（atomgit 9722 id=200378287 同批）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-r4", deadline_in=999.0)
    supervisor._ipc_mode = "resident"
    supervisor._conn = None
    await supervisor._replay_delivery_entries()
    assert supervisor._delivery_replay_requested is True
    calls: list[int] = []
    real_replay = supervisor._replay_delivery_entries

    async def _spy():
        calls.append(1)
        await real_replay()

    supervisor._replay_delivery_entries = _spy  # type: ignore[method-assign]
    async with _running(supervisor) as (_conn, _pump):
        supervisor._ipc_mode = "resident"
        await supervisor._delivery_sweep_once()
        assert calls, "sweep 必须消费 _delivery_replay_requested 补放重放"
        assert supervisor._delivery_replay_requested is False


async def test_r5_replay_skips_expired_question_by_response_kind() -> None:
    """wire 层 response_kind 提问帧（body 无 event_type）交互已终结时同样不重投——
    判据与 _question_recovery_filter 同源（event_type 缺失回落 response_kind，
    atomgit 9722 id=200378287）。"""
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    bridge.events.register(_request("req-rk"), "disp-rk")
    bridge.events.append({"request_id": "req-rk", "payload": {
        "response_kind": "chat.ask_user_question",
        "body": {"interaction_id": "ctl-rk", "execution_id": "exec-old"}}}, "disp-rk")
    conn = _FakeIpcConn()
    generation = bridge.holder.open_session(conn)
    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-rk",
         "payload": _identity(), "after": 0}, conn, generation)
    assert conn.sent[0]["status"] == "ok"

    def _inner(f):
        pl = f.get("payload") or {}
        return pl.get("payload") if isinstance(pl.get("payload"), dict) else pl
    leaked = [f for f in conn.sent[1:] if f.get("kind") == "wire"
              and isinstance(_inner(f), dict)
              and (_inner(f).get("response_kind") == "chat.ask_user_question"
                   or (_inner(f).get("body") or {}).get("interaction_id") == "ctl-rk")]
    assert leaked == []


class _StubControlRuntime:
    """提问控制面 authority 替身：recoverable_control 返回预置值（None＝该 control
    已不属当前 generation 在飞，提问已失效）。"""

    def __init__(self, current):
        self._current = current

    def recoverable_control(self, session_id, control_id, execution_id):
        return self._current


async def _dispatcher_with_question(rid):
    """在保留帧里放一条提问帧＋record 携其它残留控制交互的 dispatcher 场景。"""
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    bridge.events.register(_request(rid), "disp-q")
    bridge.events.append({"request_id": rid, "payload": {
        "event_type": "chat.ask_user_question", "interaction_id": "ctl-9",
        "execution_id": "exec-old", "content": "确认删除？"}}, "disp-q")
    record = bridge.events.records["disp-q"]
    # 残留非提问控制（waiting 控制集过滤后仍在字典里）——提问早已终结字典也不为空
    record.interactions = {"ctl-other": {"event_type": "chat.interrupt_result"}}
    record.last_execution_id = "exec-old"
    return dispatcher, bridge, record


async def test_q1_replay_skips_expired_question_with_residue() -> None:
    """失效提问不得因 record.interactions 非空被当作在飞重投（openjiuwen-AIReview
    9722 id=200409667：粗启发式与 9707 authority 判定分叉）——control 被当前
    generation 拒认即失效，提问帧不随重放重投。"""
    dispatcher, bridge, _record = await _dispatcher_with_question("req-q-exp")
    await dispatcher.bind_runtime(_StubControlRuntime(None))
    conn = _FakeIpcConn()
    generation = bridge.holder.open_session(conn)
    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-q-exp",
         "payload": _identity(), "after": 0}, conn, generation)
    assert conn.sent[0]["status"] == "ok"
    events = [_wire_event_of(f) for f in conn.sent[1:] if f.get("kind") == "wire"]
    assert "chat.ask_user_question" not in events


async def test_q2_replay_keeps_inflight_question() -> None:
    """反向锁：control 仍在当前 generation 在飞时提问帧照常重放，修复不误杀在飞提问。"""
    dispatcher, bridge, _record = await _dispatcher_with_question("req-q-live")
    await dispatcher.bind_runtime(_StubControlRuntime(
        SimpleNamespace(generation=1, execution_id="exec-old")))
    conn = _FakeIpcConn()
    generation = bridge.holder.open_session(conn)
    conn.sent.clear()
    await dispatcher._subscribe_by_id(
        {"kind": KIND_SUBSCRIBE_BY_ID, "request_id": "req-q-live",
         "payload": _identity(), "after": 0}, conn, generation)
    assert conn.sent[0]["status"] == "ok"
    events = [_wire_event_of(f) for f in conn.sent[1:] if f.get("kind") == "wire"]
    assert "chat.ask_user_question" in events


def test_z_non_session_work_requests_not_registered(tmp_path):
    """attach_goal 页面恢复与被动 Goal 订阅请求豁免台账登记（atomgit id=200347100）。

    这类请求带 request_id、渠道非控制面、按设计零用户可见帧（worker 侧
    _is_session_work_request 同判据）；登记后必然 600s 到期 → 占位卡向用户
    误报「本次处理可能已中断」。
    """
    ledger = DeliveryLedger(directory=tmp_path / "ledger", idle_seconds=600.0)
    common = dict(dispatch_id="d-ag", worker_instance="w-1", deadline=time.time() + 600)

    attach_goal = AgentRequest(
        request_id="req-attach", channel_id="web", session_id="s-1",
        req_method=ReqMethod.CHAT_SEND, params={"attach_goal": True},
        user_id="u-1", metadata={"app_id": "a"},
    )
    assert ledger.register(attach_goal, **common) is None, \
        "attach_goal 页面恢复零可见帧，登记必致 600s 占位卡误报"

    passive_goal = AgentRequest(
        request_id="req-goal-get", channel_id="web", session_id="s-1",
        req_method=ReqMethod.COMMAND_GOAL, params={"action": "get"},
        user_id="u-1", metadata={"app_id": "a"},
    )
    assert ledger.register(passive_goal, **common) is None, \
        "被动 Goal 订阅（action 非 set/resume）零可见帧，不得登记"

    # 反向锁：active Goal 指令（set/resume 有可见回复）与常规 CHAT_SEND 仍登记。
    active_goal = AgentRequest(
        request_id="req-goal-set", channel_id="web", session_id="s-1",
        req_method=ReqMethod.COMMAND_GOAL, params={"action": "set"},
        user_id="u-1", metadata={"app_id": "a"},
    )
    assert ledger.register(active_goal, **common) is not None
    assert ledger.register(_request("req-normal"),
                           dispatch_id="d-n", worker_instance="w-1",
                           deadline=time.time() + 600) is not None
