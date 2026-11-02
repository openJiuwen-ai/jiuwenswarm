"""Cron live bus: local replay + Redis cross-pod fanout."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.gateway.cron import live_bus as live_bus_module
from jiuwenswarm.gateway.cron.live_bus import CronLiveBus


def test_late_subscribe_replays_buffered_frames():
    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    sid = "cron_late_join_1"

    bus.publish(sid, {"event": "chat.reasoning", "payload": {"content": "先想一下"}})
    bus.publish(sid, {"event": "chat.ask_user_question", "payload": {"request_id": "ask-1"}})

    queue = bus.subscribe(sid)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait()["event"])

    assert events == ["chat.reasoning", "chat.ask_user_question"]

    bus.publish(sid, {"event": "chat.delta", "payload": {"content": "继续"}})
    assert queue.get_nowait()["event"] == "chat.delta"
    bus.unsubscribe(sid, queue)


def test_replay_buffer_caps_size():
    bus = CronLiveBus(replay_size=3, replay_ttl_seconds=60.0)
    sid = "cron_cap_1"
    for i in range(5):
        bus.publish(sid, {"event": "chat.delta", "payload": {"content": str(i)}})

    queue = bus.subscribe(sid)
    contents = []
    while not queue.empty():
        contents.append(queue.get_nowait()["payload"]["content"])
    assert contents == ["2", "3", "4"]
    bus.unsubscribe(sid, queue)


def test_replay_expires_after_ttl_without_subscribers():
    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=0.05)
    sid = "cron_ttl_1"
    bus.publish(sid, {"event": "chat.reasoning", "payload": {"content": "x"}})
    queue = bus.subscribe(sid)
    bus.unsubscribe(sid, queue)
    time.sleep(0.2)
    late = bus.subscribe(sid)
    assert late.empty()
    bus.unsubscribe(sid, late)


def test_origin_unique_even_when_gateway_instance_id_shared(monkeypatch):
    """Deploy sets the same GATEWAY_INSTANCE_ID on every replica; origins must still differ."""
    monkeypatch.setenv("POD_IP", "10.0.0.1")
    monkeypatch.setattr(
        "jiuwenswarm.extensions.redis.redis_runtime.get_gateway_instance_id",
        lambda: "jiuwenclaw-gateway-hjh",
        raising=False,
    )
    a = live_bus_module._gateway_origin_id()
    monkeypatch.setenv("POD_IP", "10.0.0.2")
    b = live_bus_module._gateway_origin_id()
    assert a != b
    assert a.startswith("jiuwenclaw-gateway-hjh:")
    assert b.startswith("jiuwenclaw-gateway-hjh:")


def test_processing_status_true_false_fingerprints_differ_with_large_cron_meta():
    """Large cron blobs must not make true/false processing_status collide after truncate."""
    cron_meta = {
        "job_id": "j" * 80,
        "job_name": "n" * 80,
        "run_id": "r" * 80,
        "push_at": "2026-01-01T00:00:00+00:00",
        "wake_at": "2026-01-01T00:00:00+00:00",
        "exec_channel_id": "__cron__",
        "exec_session_id": "cron_abc",
        "is_placeholder": False,
        "status": "running",
    }
    true_frame = {
        "event": "chat.processing_status",
        "payload": {
            "event_type": "chat.processing_status",
            "is_processing": True,
            "request_id": "cron-1",
            "cron": cron_meta,
        },
    }
    false_frame = {
        "event": "chat.processing_status",
        "payload": {
            "event_type": "chat.processing_status",
            "is_processing": False,
            "request_id": "cron-1",
            "cron": cron_meta,
        },
    }
    assert live_bus_module._frame_fingerprint(true_frame) != live_bus_module._frame_fingerprint(
        false_frame
    )


def test_identical_delta_content_different_live_seq_fingerprints_differ():
    """Repeated LLM tokens (「的」「，」) must not collide after content-only fingerprinting."""
    first = {
        "event": "chat.delta",
        "payload": {"content": "的", "live_seq": 10, "request_id": "r1"},
    }
    second = {
        "event": "chat.delta",
        "payload": {"content": "的", "live_seq": 11, "request_id": "r1"},
    }
    assert live_bus_module._frame_fingerprint(first) != live_bus_module._frame_fingerprint(second)


def test_repeated_identical_deltas_both_reach_subscriber():
    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0, queue_size=8)
    sid = "cron_repeat_delta"
    queue = bus.subscribe(sid)
    bus.publish(
        sid,
        {"event": "chat.delta", "payload": {"content": "的", "live_seq": 1}},
    )
    bus.publish(
        sid,
        {"event": "chat.delta", "payload": {"content": "的", "live_seq": 2}},
    )
    assert queue.qsize() == 2
    assert queue.get_nowait()["payload"]["live_seq"] == 1
    assert queue.get_nowait()["payload"]["live_seq"] == 2
    bus.unsubscribe(sid, queue)


def test_queue_full_drops_oldest_keeps_newest():
    bus = CronLiveBus(replay_size=4, replay_ttl_seconds=60.0, queue_size=2)
    sid = "cron_queue_full"
    queue = bus.subscribe(sid)
    # Drain subscribe-time empty replay.
    while not queue.empty():
        queue.get_nowait()
    for i in range(4):
        bus.publish(
            sid,
            {"event": "chat.delta", "payload": {"content": str(i), "live_seq": i + 1}},
        )
    # queue_size=2 with drop-oldest → newest two frames remain.
    assert queue.qsize() == 2
    assert queue.get_nowait()["payload"]["live_seq"] == 3
    assert queue.get_nowait()["payload"]["live_seq"] == 4
    bus.unsubscribe(sid, queue)


def test_subscribe_skip_replay_does_not_drain_buffer():
    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    sid = "cron_skip_replay_1"
    bus.publish(
        sid,
        {"event": "chat.ask_user_question", "payload": {"request_id": "ask-1", "live_seq": 1}},
    )
    bus.publish(
        sid,
        {"event": "chat.reasoning", "payload": {"content": "上一轮思考", "live_seq": 2}},
    )
    queue = bus.subscribe(sid, replay=False)
    assert queue.empty()
    # Buffer itself must remain for a later catch-up subscribe.
    late = bus.subscribe(sid, replay=True)
    assert late.qsize() == 2
    bus.unsubscribe(sid, queue)
    bus.unsubscribe(sid, late)


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.channels: dict[str, list[asyncio.Queue[str]]] = {}
        self.ttls: dict[str, int] = {}

    async def rpush(self, key: str, *values: Any) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.extend(str(v) for v in values)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        items = self.lists.get(key) or []
        # Redis LTRIM semantics with negative indexes.
        n = len(items)
        if n == 0:
            return True
        s = start if start >= 0 else n + start
        e = stop if stop >= 0 else n + stop
        s = max(0, s)
        e = min(n - 1, e)
        self.lists[key] = items[s : e + 1] if s <= e else []
        return True

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        self.ttls[key] = int(ttl_seconds)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.lists:
                del self.lists[key]
                removed += 1
            self.ttls.pop(key, None)
        return removed

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key) or []
        n = len(items)
        if n == 0:
            return []
        s = start if start >= 0 else n + start
        e = stop if stop >= 0 else n + stop
        if stop == -1:
            e = n - 1
        s = max(0, s)
        e = min(n - 1, e)
        return items[s : e + 1] if s <= e else []

    async def publish(self, channel: str, message: str) -> int:
        waiters = list(self.channels.get(channel, ()))
        for q in waiters:
            await q.put(message)
        return len(waiters)

    async def subscribe(self, channel: str):
        q: asyncio.Queue[str] = asyncio.Queue()
        self.channels.setdefault(channel, []).append(q)
        try:
            while True:
                yield await q.get()
        finally:
            bucket = self.channels.get(channel) or []
            if q in bucket:
                bucket.remove(q)


@pytest.mark.asyncio
async def test_redis_fanout_delivers_to_other_origin(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(live_bus_module, "_redis_client", lambda: redis)

    publisher = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    publisher._origin = "gw-publisher"
    subscriber = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    subscriber._origin = "gw-subscriber"

    sid = "cron_cross_pod_1"
    queue = subscriber.subscribe(sid)
    # Let bridge task subscribe to fake redis.
    await asyncio.sleep(0.05)

    publisher.publish(
        sid,
        {"event": "chat.reasoning", "payload": {"content": "跨副本思考"}},
    )
    # Redis publish is scheduled as a task.
    await asyncio.sleep(0.05)

    frame = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert frame["event"] == "chat.reasoning"
    assert frame["payload"]["content"] == "跨副本思考"
    subscriber.unsubscribe(sid, queue)


@pytest.mark.asyncio
async def test_redis_replay_fills_late_subscriber_on_other_pod(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(live_bus_module, "_redis_client", lambda: redis)

    publisher = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    publisher._origin = "gw-publisher"
    publisher.publish(
        "cron_cross_pod_2",
        {"event": "chat.ask_user_question", "payload": {"request_id": "ask-9"}},
    )
    await asyncio.sleep(0.05)
    assert any(redis.lists.values())

    late = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    late._origin = "gw-late"
    queue = late.subscribe("cron_cross_pod_2")
    await asyncio.sleep(0.05)

    assert not queue.empty()
    frame = queue.get_nowait()
    assert frame["event"] == "chat.ask_user_question"
    assert frame["payload"]["request_id"] == "ask-9"
    late.unsubscribe("cron_cross_pod_2", queue)


@pytest.mark.asyncio
async def test_publisher_skips_redis_echo_to_local_subscribers(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(live_bus_module, "_redis_client", lambda: redis)

    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    bus._origin = "gw-same"
    sid = "cron_echo_1"
    queue = bus.subscribe(sid)
    await asyncio.sleep(0.05)

    bus.publish(sid, {"event": "chat.delta", "payload": {"content": "once"}})
    await asyncio.sleep(0.05)

    # Local deliver once; redis echo must not duplicate.
    assert queue.qsize() == 1
    assert queue.get_nowait()["payload"]["content"] == "once"
    assert queue.empty()
    bus.unsubscribe(sid, queue)
