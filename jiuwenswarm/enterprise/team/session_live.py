# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版团队：一个会话一个等待者，正文推给所有已连网关。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_lives: dict[str, "_SessionLive"] = {}

# 这些事件在广播之外已经写过历史，直播转发再写就会有两份。
_HISTORY_SKIP_EVENT_TYPES = frozenset({"chat.notice", "team.member", "team.task"})
_FLUSH_PENDING_EVENT_TYPES = frozenset({"chat.tool_call", "chat.ask_user_question"})


def team_session_live_request_id(session_id: str) -> str:
    return f"team-session-live:{session_id}"


def publish_team_session_file(message: dict[str, Any]) -> bool:
    """把 chat.file 交给已经在跑的团队直播。

    不放进直播队列。发送工具自己会写历史，队列再观察一遍就会有两份。
    """
    session_id = str(message.get("session_id") or "").strip()
    payload = message.get("payload")
    if (
        not session_id
        or not isinstance(payload, dict)
        or payload.get("event_type") != "chat.file"
        or not payload.get("files")
    ):
        return False
    live = _lives.get(session_id)
    if live is None or live.stopped or live.task.done():
        return False
    from jiuwenswarm.enterprise.team.push_fanout import publish
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    metadata = dict(message.get("metadata") or {})
    metadata["delivery_scope"] = "session_live"
    try:
        publish(
            build_server_push_wire(
                {
                    "request_id": str(
                        message.get("request_id") or team_session_live_request_id(session_id)
                    ),
                    "channel_id": str(message.get("channel_id") or ""),
                    "session_id": session_id,
                    "payload": payload,
                    "is_complete": False,
                    "metadata": metadata,
                }
            )
        )
    except Exception:
        logger.exception(
            "[TeamSession] failed to publish chat.file session_id=%s",
            session_id,
        )
        return False
    return True


@dataclass
class _SessionLive:
    queue: asyncio.Queue
    task: asyncio.Task
    stopped: bool = False


async def ensure_team_session_live(
    team_manager: Any,
    session_id: str,
    channel_id: str | None,
) -> asyncio.Queue:
    """保证这个会话有且只有一个直播等待者。已经在跑就复用它的队列。"""
    live = _lives.get(session_id)
    if live is not None and not live.task.done() and not live.stopped:
        return live.queue
    if live is not None:
        team_manager.remove_waiter(session_id, team_session_live_request_id(session_id))
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        _new_team_event_queue,
    )

    queue = _new_team_event_queue()
    request_id = team_session_live_request_id(session_id)
    team_manager.add_waiter(session_id, request_id, queue)
    task = asyncio.create_task(
        _forward_session_live(team_manager, session_id, channel_id, queue, request_id),
        name=f"team-session-live-{session_id}",
    )
    _lives[session_id] = _SessionLive(queue=queue, task=task)
    return queue


def _discard_queued(queue: asyncio.Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


async def stop_team_session_live(session_id: str, *, drop_queued: bool = False) -> None:
    """拆掉这个会话的直播等待者。

    提交没能把 runner 跑起来时，队列里已有的帧仍按原顺序交出去。
    用户取消时 drop_queued=True：先丢掉还没发出的帧，再让转发任务退出。
    """
    live = _lives.get(session_id)
    if live is None:
        return
    live.stopped = True
    if drop_queued:
        _discard_queued(live.queue)
        live.task.cancel()
    else:
        try:
            live.queue.put_nowait(None)
        except asyncio.QueueFull:
            live.task.cancel()
    try:
        await live.task
    except asyncio.CancelledError:
        pass


async def _forward_session_live(
    team_manager: Any,
    session_id: str,
    channel_id: str | None,
    queue: asyncio.Queue,
    request_id: str,
) -> None:
    from jiuwenswarm.enterprise.team.push_fanout import publish
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        _build_team_event_chunk_meta,
    )

    seen_stream = False
    history = _LiveHistory(session_id=session_id, channel_id=channel_id)
    try:
        while True:
            current = _lives.get(session_id)
            if current is not None and current.stopped:
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if team_manager.has_stream_task(session_id):
                    seen_stream = True
                    continue
                if seen_stream and queue.empty():
                    break
                continue
            except asyncio.CancelledError:
                break
            if event is None:
                break
            if not isinstance(event, dict):
                continue
            if team_manager.has_stream_task(session_id):
                seen_stream = True
            try:
                history.observe(event)
            except Exception:
                logger.exception(
                    "[TeamSession] failed to persist live frame session_id=%s",
                    session_id,
                )
            try:
                agent_ref, metadata = _build_team_event_chunk_meta(event)
                wire_metadata = dict(metadata or {})
                wire_metadata["delivery_scope"] = "session_live"
                publish(
                    build_server_push_wire(
                        {
                            "request_id": request_id,
                            "channel_id": str(channel_id or ""),
                            "session_id": session_id,
                            "payload": event,
                            "is_complete": False,
                            "agent_ref": agent_ref,
                            "metadata": wire_metadata,
                        }
                    )
                )
            except Exception:
                logger.exception(
                    "[TeamSession] failed to publish live frame session_id=%s",
                    session_id,
                )
            if seen_stream and not team_manager.has_stream_task(session_id) and queue.empty():
                break
    finally:
        try:
            history.flush()
        except Exception:
            logger.exception(
                "[TeamSession] failed to flush live history session_id=%s",
                session_id,
            )
        current = _lives.get(session_id)
        if current is not None and current.task is asyncio.current_task():
            _lives.pop(session_id, None)
            team_manager.remove_waiter(session_id, request_id)


@dataclass
class _LiveHistory:
    """按 chat.final / chat.file 的既有规则，把直播帧补进会话历史。"""

    session_id: str
    channel_id: str | None
    pending: list[str] = field(default_factory=list)
    started_at: float | None = None
    _seq: int = 0

    def observe(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            return
        if event_type in _HISTORY_SKIP_EVENT_TYPES:
            return
        if event_type == "chat.delta":
            content = str(event.get("content") or "")
            if content:
                if self.started_at is None:
                    self.started_at = time.time()
                self.pending.append(content)
            return
        if event_type == "chat.reasoning":
            return
        if event_type in _FLUSH_PENDING_EVENT_TYPES:
            self.flush()
        if event_type == "chat.final":
            content = str(event.get("content") or "")
            if content:
                started_at = self.started_at
                self.pending.clear()
                self.started_at = None
                self._write(event, event_type="chat.final", content=content, started_at=started_at)
            else:
                self.flush()
            return
        if event_type.startswith("chat.") or event_type == "context.usage":
            self._write(
                event,
                event_type=event_type,
                content=str(event.get("content") or event.get("error") or ""),
                started_at=None,
            )

    def flush(self) -> None:
        if not self.pending:
            return
        content = "".join(self.pending)
        started_at = self.started_at
        self.pending.clear()
        self.started_at = None
        if not content:
            return
        self._write(
            {},
            event_type="chat.final",
            content=content,
            started_at=started_at,
        )

    def _write(
        self,
        event: dict[str, Any],
        *,
        event_type: str,
        content: str,
        started_at: float | None,
    ) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            _resolve_channel_id,
        )
        from jiuwenswarm.server.runtime.session.session_history import (
            append_history_record,
        )

        extra = {
            key: value
            for key, value in event.items()
            if key not in ("event_type", "content")
        }
        timestamp = time.time()
        if event_type == "chat.final" and started_at and started_at < timestamp:
            extra.setdefault("completed_at", timestamp)
            timestamp = started_at
        self._seq += 1
        append_history_record(
            session_id=self.session_id,
            request_id=f"team-session-live:{self.session_id}:{self._seq}",
            channel_id=_resolve_channel_id(self.channel_id),
            role="assistant",
            event_type=event_type,
            content=content,
            timestamp=timestamp,
            extra=extra or None,
            mode="team",
        )
