"""Route-trio close-out faces (AIReview 9722 id=200846554) + replay dispatch filter.

Face A: a delivered visible non-terminal frame (chat.ask_user_question) that
closes the entry terminal pops the replay-route trio. Face B: the
placeholder-SENT close-out pops the trio. Stamp guard: an older close-out
never pops a newer rebinding. Plus the identity-replay dispatch-generation
filter (atomgit-bot 10206 id=200876804).
"""
import asyncio
import json
import time

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.delivery_ledger import (
    DeliveryLedger,
    PLACEHOLDER_PENDING,
    PLACEHOLDER_RETRACTED,
    PLACEHOLDER_SENT,
)
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


class _WebSocket:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.release = asyncio.Event()
        self.release.set()

    async def send(self, raw: str) -> None:
        await self.release.wait()
        self.frames.append(json.loads(raw))


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
        },
    )


def _registered_supervisor(ledger_dir) -> RuntimeSupervisor:
    supervisor = RuntimeSupervisor(Readiness(), forwarder=EventForwarder())
    supervisor._delivery_ledger = DeliveryLedger(directory=ledger_dir, idle_seconds=600.0)
    return supervisor


def _register(supervisor, *, rid="req-1", deadline_in=600.0, dispatch_id="d-1") -> None:
    supervisor._delivery_ledger.register(
        _request(rid), dispatch_id=dispatch_id, worker_instance="w-1",
        deadline=time.time() + deadline_in,
    )


def _trio(supervisor, dispatch_id="d-1", stamp=None):
    if stamp is None:
        stamp = (_WebSocket(), asyncio.Lock())
    supervisor._routes[dispatch_id] = stamp
    supervisor._delivery_replay_routes.add(dispatch_id)
    supervisor._delivery_replay_route_stamps[dispatch_id] = stamp
    return stamp


def test_face_a_route_cleanup_when_visible_frame_closes_entry(tmp_path) -> None:
    """face A：可见非终态帧（chat.ask_user_question）送达且条目随即终态
    （retracted）→ 重放路由三件套就地清理，旧 (ws, lock) 不再滞留。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    _trio(supervisor)
    supervisor._delivery_note_forward(
        {"request_id": "req-1", "d": None, "dispatch_id": "d-1",
         "payload": {"payload": {"event_type": "chat.ask_user_question", "content": "q?"}}},
        delivered=True)
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_RETRACTED
    assert "d-1" not in supervisor._routes
    assert "d-1" not in supervisor._delivery_replay_routes
    assert "d-1" not in supervisor._delivery_replay_route_stamps


def test_face_a_route_kept_while_entry_pending(tmp_path) -> None:
    """face A 反向锁：条目仍 pending（流式中 delta 送达）→ 路由保留。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    _trio(supervisor)
    supervisor._delivery_note_forward(
        {"request_id": "req-1", "dispatch_id": "d-1",
         "payload": {"payload": {"event_type": "chat.delta"}}},
        delivered=True)
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_PENDING
    assert "d-1" in supervisor._routes
    assert "d-1" in supervisor._delivery_replay_routes
    assert "d-1" in supervisor._delivery_replay_route_stamps


async def test_face_b_route_cleanup_on_placeholder_sent_closeout(tmp_path) -> None:
    """face B：占位卡 sent 收尾（重放 ok 后 worker 全静默）→ 路由三件套清理。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    ws, lock = _WebSocket(), asyncio.Lock()
    supervisor._gateway_ws, supervisor._gateway_lock = ws, lock
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=0.0)
    _trio(supervisor)
    await supervisor._delivery_sweep_once()
    assert supervisor._delivery_ledger.entry("req-1").placeholder_state == PLACEHOLDER_SENT
    assert ws.frames, "占位卡应已送达"
    assert "d-1" not in supervisor._routes
    assert "d-1" not in supervisor._delivery_replay_routes
    assert "d-1" not in supervisor._delivery_replay_route_stamps


def test_face_stamp_guard_preserves_newer_binding(tmp_path) -> None:
    """stamp 守卫：旧帧收尾不得误清新绑定路由（新绑定值≠登记 stamp）。"""
    supervisor = _registered_supervisor(tmp_path / "ledger")
    _register(supervisor, rid="req-1", dispatch_id="d-1", deadline_in=600.0)
    old_stamp = (_WebSocket(), asyncio.Lock())
    supervisor._routes["d-1"] = old_stamp
    supervisor._delivery_replay_routes.add("d-1")
    supervisor._delivery_replay_route_stamps["d-1"] = old_stamp
    new_stamp = (_WebSocket(), asyncio.Lock())
    supervisor._routes["d-1"] = new_stamp
    supervisor._delivery_note_forward(
        {"request_id": "req-1", "dispatch_id": "d-1",
         "payload": {"payload": {"event_type": "chat.ask_user_question"}}},
        delivered=True)
    assert supervisor._routes["d-1"] is new_stamp
    assert "d-1" not in supervisor._delivery_replay_routes
    assert "d-1" not in supervisor._delivery_replay_route_stamps
