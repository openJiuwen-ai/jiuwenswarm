# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Fanout for cron execution frames (local + optional Redis).

Enterprise HTTP pages are not gateway WebSocket clients, so a cron run's
reasoning and deltas never reach the open ``cron_*`` window through
``WebChannel`` broadcast. The scheduler mirrors those frames here, and the
session live SSE subscribes while that window is open.

``run_now`` often returns the session id *after* the agent has already
streamed the first reasoning tokens. Without a short replay buffer those
frames are gone by the time the browser opens ``/sessions/{cron_*}/live``,
so the UI jumps straight to the permission card and only shows thinking
after approve (via the resume ``chat.send`` SSE).

Multi-replica gateways: the scheduler publishes on one pod while the browser
``GET .../live`` may land on another. When Gateway Redis is active we also
PUBLISH frames and keep a short Redis list for late joiners, so any pod can
serve the live SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


def _process_unique_suffix() -> str:
    """Per-pod / per-process id so Redis echo-skip cannot drop cross-replica frames.

    Enterprise deploy sets the same ``GATEWAY_INSTANCE_ID`` on every Gateway
    replica (``<<GATEWAY_NAME>>-<<NAMESPACE>>``). Using that alone as the wire
    ``origin`` makes every pod treat every other pod's PUBLISH as a local echo
    and drop it — live SSE on a non-publisher replica stays empty even when
    Redis fanout is healthy.
    """
    for key in ("POD_IP", "HOSTNAME"):
        raw = str(os.environ.get(key) or "").strip()
        if raw:
            return raw
    return uuid.uuid4().hex[:12]


def _gateway_origin_id() -> str:
    base = ""
    try:
        from jiuwenswarm.extensions.redis.redis_runtime import get_gateway_instance_id

        iid = get_gateway_instance_id()
        if iid:
            base = str(iid).strip()
    except Exception:  # noqa: BLE001
        base = ""
    suffix = _process_unique_suffix()
    if base:
        return f"{base}:{suffix}"
    return f"local-{suffix}"


_warned_no_redis = False


def _redis_client() -> Any | None:
    """Best-effort Redis for cross-pod live; warn once when unavailable."""
    global _warned_no_redis
    client: Any | None = None
    try:
        from jiuwenswarm.extensions.redis.redis_runtime import (
            get_declared_deployment_mode,
            get_gateway_redis_client_if_open,
        )

        client = get_gateway_redis_client_if_open()
        if client is None and not _warned_no_redis:
            _warned_no_redis = True
            mode = ""
            try:
                mode = str(get_declared_deployment_mode() or "")
            except Exception:  # noqa: BLE001
                mode = "?"
            logger.warning(
                "[CronLiveBus] Gateway Redis unavailable; cron live stays process-local "
                "(multi-replica subscribers will miss frames). "
                "deployment_mode=%s — need distributed|active-standby with "
                "[GatewayRedis] connected",
                mode,
            )
    except Exception:  # noqa: BLE001
        if not _warned_no_redis:
            _warned_no_redis = True
            logger.warning(
                "[CronLiveBus] Gateway Redis lookup failed; cron live stays process-local",
                exc_info=True,
            )
        return None
    return client


def _channel_rel(session_id: str) -> str:
    from jiuwenswarm.extensions.redis.redis_keys import cron_live_channel_rel

    return cron_live_channel_rel(session_id)


def _replay_key_rel(session_id: str) -> str:
    from jiuwenswarm.extensions.redis.redis_keys import cron_live_replay_key_rel

    return cron_live_replay_key_rel(session_id)


def _encode_wire(*, origin: str, frame: dict[str, Any]) -> str:
    return json.dumps({"o": origin, "f": frame}, ensure_ascii=False, separators=(",", ":"))


def _decode_wire(raw: str) -> tuple[str, dict[str, Any]] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    frame = parsed.get("f")
    if not isinstance(frame, dict):
        return None
    origin = str(parsed.get("o") or "")
    return origin, frame


def _frame_fingerprint(frame: dict[str, Any]) -> str:
    """Stable dedupe key for live fanout / Redis echo.

    Cron push attaches a large ``cron`` blob to every payload. Dumping the whole
    payload and truncating to 240 chars often cuts off ``is_processing`` and
    other discriminators, so ``processing_status=true`` and ``=false`` collide
    and the terminal false is dropped — UI stays「处理中」after a fast auto-approve run.

    ``live_seq`` must be part of the key: char-level LLM deltas often repeat the
    same token (「的」「，」「\\n」). Fingerprinting only ``content`` then drops the
    second copy → browser sees ``live_seq`` gaps and garbled CJK (e.g. 令命).
    """
    event = str(frame.get("event") or "")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    rid = str(payload.get("request_id") or "")
    proc = payload.get("is_processing")
    proc_part = "" if proc is None else f"|p={proc}"
    seq = payload.get("live_seq")
    seq_part = "" if seq is None else f"|s={seq}"
    content = payload.get("content")
    if isinstance(content, str) and content:
        body = content[:240]
    else:
        compact: dict[str, Any] = {}
        for key in (
            "event_type",
            "is_processing",
            "current_task",
            "tool_call_id",
            "name",
            "status",
            "request_id",
        ):
            if key in payload:
                compact[key] = payload.get(key)
        try:
            body = json.dumps(
                compact or {"_": str(payload)[:120]},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )[:240]
        except Exception:  # noqa: BLE001
            body = str(payload)[:240]
    return f"{event}|{rid}{proc_part}{seq_part}|{body}"


def _queue_put(queue: asyncio.Queue[dict[str, Any]], frame: dict[str, Any]) -> bool:
    """Enqueue ``frame``; on full drop the oldest slot so the newest is kept."""
    try:
        queue.put_nowait(frame)
        return True
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        try:
            queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False


class CronLiveBus:
    """Session-keyed queues with local + optional Redis replay/fanout."""

    def __init__(
        self,
        *,
        replay_size: int = 1024,
        replay_ttl_seconds: float = 120.0,
        queue_size: int = 4096,
    ) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._replay: dict[str, deque[dict[str, Any]]] = {}
        self._replay_deadline: dict[str, float] = {}
        self._replay_size = max(1, int(replay_size))
        self._replay_ttl_seconds = max(0.01, float(replay_ttl_seconds))
        self._queue_size = max(1, int(queue_size))
        self._origin = _gateway_origin_id()
        self._bridge_tasks: dict[str, asyncio.Task[None]] = {}
        self._seen: dict[str, deque[str]] = {}
        # Track fingerprints for a full replay window (plus headroom for in-flight).
        self._seen_size = max(256, self._replay_size * 2)

    def publish(self, session_id: str, frame: dict[str, Any]) -> None:
        sid = str(session_id or "").strip()
        if not sid or not isinstance(frame, dict):
            return
        self._prune_expired_replay()
        buf = self._replay.setdefault(sid, deque(maxlen=self._replay_size))
        buf.append(frame)
        if self._subscribers.get(sid):
            self._replay_deadline.pop(sid, None)
            # Mark seen so the Redis echo on this pod is dropped; do not mark when
            # nobody is subscribed yet or late local replay would be skipped.
            self._fanout_local(sid, frame, mark_seen=True)
        else:
            # Nobody watching yet (typical run-now race): TTL the buffer.
            self._replay_deadline[sid] = time.monotonic() + self._replay_ttl_seconds
        self._schedule(self._redis_publish(sid, frame))

    def subscribe(
        self,
        session_id: str,
        *,
        replay: bool = True,
    ) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to live frames for ``session_id``.

        ``replay=False`` skips the buffered backlog (local + Redis). Use after
        history restore so a page refresh cannot rebuild the previous run's
        thinking / permission card on top of the finished timeline.
        """
        sid = str(session_id or "").strip()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        if not sid:
            return queue
        self._prune_expired_replay()
        if replay:
            # Same-pod late join: fill immediately from the in-process buffer.
            for frame in list(self._replay.get(sid, ())):
                self._enqueue(queue, frame, sid=sid, mark_seen=True)
            # Cross-pod late join: Redis list may hold frames this process never saw.
            # Dedup fingerprints drop overlap with the local buffer above.
            if _redis_client() is not None:
                self._schedule(self._redis_replay_into(sid, queue))
        self._subscribers.setdefault(sid, set()).add(queue)
        self._replay_deadline.pop(sid, None)
        self._schedule(self._ensure_redis_bridge(sid))
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        sid = str(session_id or "").strip()
        bucket = self._subscribers.get(sid)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            self._subscribers.pop(sid, None)
            # Keep replay briefly so a reconnect / slow run-now still catches up.
            if sid in self._replay:
                self._replay_deadline[sid] = time.monotonic() + self._replay_ttl_seconds
            self._stop_redis_bridge(sid)

    def clear_session(self, session_id: str) -> None:
        """Drop local + Redis replay after a run settles.

        Follow-up chat on ``cron_*`` does not use this bus. Leaving the finished
        run's deltas/finals buffered makes the next page refresh replay them on
        top of history and overwrite the latest assistant turn with the first
        run's answer.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._replay.pop(sid, None)
        self._replay_deadline.pop(sid, None)
        self._seen.pop(sid, None)
        self._schedule(self._redis_clear(sid))

    def _fanout_local(self, sid: str, frame: dict[str, Any], *, mark_seen: bool) -> None:
        # Dedup once per frame, then deliver to every local subscriber.
        if mark_seen and self._frame_already_seen(sid, frame):
            return
        delivered = False
        for queue in list(self._subscribers.get(sid, ())):
            if _queue_put(queue, frame):
                delivered = True
        # Only remember after a successful put — otherwise QueueFull + mark_seen
        # would permanently erase the frame (Redis same-origin echo is skipped too).
        if mark_seen and delivered:
            self._remember_frame(sid, frame)
        elif mark_seen and not delivered and self._subscribers.get(sid):
            logger.warning(
                "[CronLiveBus] drop frame: all subscriber queues full session_id=%s event=%s",
                sid,
                frame.get("event"),
            )

    def _enqueue(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        frame: dict[str, Any],
        *,
        sid: str,
        mark_seen: bool,
    ) -> None:
        if mark_seen and self._frame_already_seen(sid, frame):
            return
        if not _queue_put(queue, frame):
            return
        if mark_seen:
            self._remember_frame(sid, frame)

    def _frame_already_seen(self, sid: str, frame: dict[str, Any]) -> bool:
        fp = _frame_fingerprint(frame)
        bucket = self._seen.get(sid)
        return bool(bucket is not None and fp in bucket)

    def _remember_frame(self, sid: str, frame: dict[str, Any]) -> None:
        """Record fingerprint after a frame was successfully queued."""
        fp = _frame_fingerprint(frame)
        bucket = self._seen.setdefault(sid, deque(maxlen=self._seen_size))
        if fp not in bucket:
            bucket.append(fp)

    @staticmethod
    def _schedule(coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Caller already created the coroutine; close it when no loop (sync tests).
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return
        loop.create_task(coro)

    async def _redis_publish(self, sid: str, frame: dict[str, Any]) -> None:
        client = _redis_client()
        if client is None:
            return
        wire = _encode_wire(origin=self._origin, frame=frame)
        try:
            replay_key = _replay_key_rel(sid)
            await client.rpush(replay_key, wire)
            await client.ltrim(replay_key, -self._replay_size, -1)
            await client.expire(replay_key, max(1, int(self._replay_ttl_seconds)))
            await client.publish(_channel_rel(sid), wire)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[CronLiveBus] redis publish failed session_id=%s",
                sid,
                exc_info=True,
            )

    async def _redis_clear(self, sid: str) -> None:
        client = _redis_client()
        if client is None:
            return
        try:
            await client.delete(_replay_key_rel(sid))
        except Exception:  # noqa: BLE001
            logger.warning(
                "[CronLiveBus] redis clear failed session_id=%s",
                sid,
                exc_info=True,
            )

    async def _redis_replay_into(self, sid: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        client = _redis_client()
        if client is None:
            return
        try:
            items = await client.lrange(_replay_key_rel(sid), 0, -1)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[CronLiveBus] redis replay failed session_id=%s",
                sid,
                exc_info=True,
            )
            # Fall back to whatever this pod buffered locally.
            for frame in list(self._replay.get(sid, ())):
                self._enqueue(queue, frame, sid=sid, mark_seen=True)
            return
        for raw in items:
            decoded = _decode_wire(raw)
            if decoded is None:
                continue
            _origin, frame = decoded
            self._enqueue(queue, frame, sid=sid, mark_seen=True)

    async def _ensure_redis_bridge(self, sid: str) -> None:
        existing = self._bridge_tasks.get(sid)
        if existing is not None and not existing.done():
            return
        if _redis_client() is None:
            logger.warning(
                "[CronLiveBus] redis bridge skip session_id=%s; Gateway Redis unavailable "
                "(live stays process-local)",
                sid,
            )
            return

        async def _listen() -> None:
            backoff = 0.5
            logger.info(
                "[CronLiveBus] redis bridge started session_id=%s origin=%s",
                sid,
                self._origin,
            )
            try:
                while self._subscribers.get(sid):
                    client = _redis_client()
                    if client is None:
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        async for raw in client.subscribe(_channel_rel(sid)):
                            if not self._subscribers.get(sid):
                                return
                            decoded = _decode_wire(raw)
                            if decoded is None:
                                logger.debug(
                                    "[CronLiveBus] skip undecodable redis frame session_id=%s head=%r",
                                    sid,
                                    (raw or "")[:120],
                                )
                                continue
                            origin, frame = decoded
                            # Same process already delivered on publish; skip echo.
                            if origin and origin == self._origin:
                                continue
                            self._fanout_local(sid, frame, mark_seen=True)
                        # Generator ended cleanly; retry while viewers remain.
                        backoff = 0.5
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "[CronLiveBus] redis bridge error session_id=%s; retry in %.1fs",
                            sid,
                            backoff,
                            exc_info=True,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2.0, 8.0)
            finally:
                current = self._bridge_tasks.get(sid)
                if current is asyncio.current_task():
                    self._bridge_tasks.pop(sid, None)
                logger.info(
                    "[CronLiveBus] redis bridge stopped session_id=%s",
                    sid,
                )

        self._bridge_tasks[sid] = asyncio.create_task(
            _listen(),
            name=f"cron-live-bridge-{sid}",
        )

    def _stop_redis_bridge(self, sid: str) -> None:
        task = self._bridge_tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()

    def _prune_expired_replay(self) -> None:
        now = time.monotonic()
        for sid, deadline in list(self._replay_deadline.items()):
            if sid in self._subscribers:
                self._replay_deadline.pop(sid, None)
                continue
            if now >= deadline:
                self._replay.pop(sid, None)
                self._replay_deadline.pop(sid, None)
                self._seen.pop(sid, None)


cron_live_bus = CronLiveBus()
