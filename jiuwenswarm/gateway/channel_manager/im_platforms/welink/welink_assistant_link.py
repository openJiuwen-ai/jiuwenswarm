# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One long-lived assistant gateway connection.

WeLink delivers an @ mention and a private chat while this connection stays
registered. Ordinary group messages never arrive here; the employee CLI still
polls those. Replies go out on this same socket: an @ or private answer
finishes the task WeLink opened, and a polled answer is an unsolicited push.
A second connection for the same credential is rejected.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_send import (
    ASSISTANT_WS_URL,
    AssistantSendError,
    Connect,
    build_auth_protocol,
    build_group_frames,
    build_register_frame,
    _open_socket,
    _read_control,
)

logger = logging.getLogger(__name__)

MentionHandler = Callable[["Mention"], Awaitable[bool]]
_HEARTBEAT_SECONDS = 30.0
_SLOT_TTL_SECONDS = 480.0
_MENTION_SPACE = re.compile(r"[\u200b-\u200f\ufeff]")
_MENTION_TOKEN = re.compile(r"@\S+")


@dataclass(frozen=True, kw_only=True)
class Mention:
    message_id: str
    group_id: str
    sender_id: str
    sender_name: str
    text: str
    tool_session_id: str
    trace_id: str
    media: tuple[Any, ...] = ()
    direct: bool = False
    mention_accounts: tuple[str, ...] = ()


@dataclass
class _QaSlot:
    message_id: str
    group_id: str
    text: str
    tool_session_id: str
    trace_id: str
    reply_message_id: str
    part_id: str
    opened: bool
    created_at: float
    sender_id: str = ""
    accepted: bool = False


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _same_text(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        stripped = _MENTION_TOKEN.sub("", _MENTION_SPACE.sub("", value))
        return "".join(stripped.split())

    folded = norm(left)
    return bool(folded) and folded == norm(right)


_GROUP_SESSION_TYPES = {"group", "groupchat", "group_chat", "chat_type_group_msg", "group_msg", "normal_group"}


def _conversation(platform: Mapping[str, Any], payload: Mapping[str, Any], business: Mapping[str, Any], sender_id: str) -> tuple[str, bool]:
    """Return the chat id and whether this task is a private conversation."""
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import _DIRECT_KINDS

    session_type = str(
        platform.get("businessSessionType") or payload.get("chatType") or business.get("chatType") or ""
    ).strip().lower()
    session_id = str(platform.get("businessSessionId") or "").split("#", 1)[0].strip()
    group_id = str(payload.get("imGroupId") or business.get("imGroupId") or "").strip()
    if session_type in _GROUP_SESSION_TYPES or (group_id and group_id != "0" and session_type not in _DIRECT_KINDS):
        return (session_id or group_id, False)
    peer = session_id or sender_id
    if not peer:
        return ("", False)
    return (peer if peer.startswith("p2p:") else f"p2p:{peer}", True)


def parse_mention(frame: dict[str, Any]) -> Mention | None:
    """Read one group @ task or one private chat. Other downstream frames are not mentions."""
    if frame.get("type") != "invoke" or frame.get("action") != "chat":
        return None
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    ext = payload.get("extParameters") if isinstance(payload.get("extParameters"), dict) else {}
    business = _as_mapping(ext.get("businessExtParam"))
    platform = _as_mapping(ext.get("platformExtParam"))
    sender_id = str(business.get("senderId") or payload.get("sendUserAccount") or "").strip()
    group_id, direct = _conversation(platform, payload, business, sender_id)
    sender_name = str(
        business.get("senderName") or platform.get("senderName") or payload.get("sendUserName") or sender_id
    ).strip()
    message_id = str(business.get("businessMessageId") or payload.get("businessMessageId") or "").strip()
    raw_text = payload.get("text")
    text = raw_text
    media: tuple[Any, ...] = ()
    if isinstance(raw_text, str) and raw_text.strip():
        from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import message_text_and_media

        rendered, media = message_text_and_media(raw_text)
        text = rendered or raw_text
    tool_session_id = str(payload.get("toolSessionId") or "").strip()
    trace_id = str(frame.get("traceId") or "").strip()
    if not tool_session_id or not trace_id or not isinstance(text, str) or not text.strip():
        return None
    if not group_id or not sender_id:
        return None
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import at_accounts_in_nodes

    return Mention(
        message_id=message_id or f"mention-{tool_session_id}",
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name or sender_id,
        text=text,
        tool_session_id=tool_session_id,
        trace_id=trace_id,
        media=media,
        direct=direct,
        mention_accounts=at_accounts_in_nodes(payload, business, platform),
    )


def control_reply(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Answer the gateway's housekeeping calls so the assistant stays online."""
    kind = str(frame.get("type") or "")
    trace_id = str(frame.get("traceId") or "")
    if kind == "status_query" and trace_id:
        return [{"type": "status_response", "traceId": trace_id, "opencodeOnline": True}]
    if kind != "invoke":
        return None
    action = str(frame.get("action") or "")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    welink_session_id = str(frame.get("welinkSessionId") or "")
    if action == "create_session" and trace_id and welink_session_id:
        tool_session_id = f"ses_{uuid.uuid4().hex}"
        return [
            {
                "type": "session_created",
                "traceId": trace_id,
                "welinkSessionId": welink_session_id,
                "toolSessionId": tool_session_id,
                "session": {"sessionId": tool_session_id},
            }
        ]
    if action == "query_slash_commands" and trace_id:
        tool_session_id = str(payload.get("toolSessionId") or "").strip() or f"ses_{uuid.uuid4().hex}"
        return [
            {
                "type": "slash_commands_result",
                "toolSessionId": tool_session_id,
                "traceId": trace_id,
                "payload": {"slashCommands": []},
            }
        ]
    return None


class AssistantLink:
    """Keep one assistant registration open and send every reply through it."""

    def __init__(
        self,
        credential_id: str,
        secret: str,
        *,
        connect: Connect | None = None,
        heartbeat_seconds: float = _HEARTBEAT_SECONDS,
        slot_ttl_seconds: float = _SLOT_TTL_SECONDS,
        on_delivery_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_delivery_error = on_delivery_error
        self._credential_id = credential_id.strip()
        self._secret = secret.strip()
        self._connect = connect or _open_socket
        self._heartbeat_seconds = heartbeat_seconds
        self._slot_ttl_seconds = slot_ttl_seconds
        self._commands: queue.Queue[Any] = queue.Queue()
        self._slots: dict[str, _QaSlot] = {}
        self._aliases: dict[tuple[str, str], str] = {}
        self._delivered: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: Any = None
        self._loop: Any = None
        self._on_mention: MentionHandler | None = None
        self._connects = 0

    @property
    def connect_count(self) -> int:
        return self._connects

    def start(self, loop: Any, on_mention: MentionHandler) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = loop
        self._on_mention = on_mention
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="welink-assistant", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_socket()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._ready.clear()

    def send_text(self, group_id: str, text: str, source_message_id: str = "", *,
                  on_prepared: Callable[[str, str, str], None] | None = None,
                  finish_task: bool = True) -> str:
        """Send one reply. An open @ task for this message is finished in place."""
        if not group_id.strip() or not text:
            raise AssistantSendError("missing_assistant_credential")
        box: queue.Queue[Any] = queue.Queue(1)
        self._commands.put(("send", group_id, text, source_message_id, box, on_prepared, finish_task))
        try:
            result = box.get(timeout=25)
        except queue.Empty as exc:
            raise AssistantSendError("send_timeout") from exc
        if isinstance(result, AssistantSendError):
            raise result
        return str(result)

    def claim_polled(self, group_id: str, message_id: str, text: str, *, sender_id: str = "") -> bool:
        """Align one history ID with its mention without replacing the Request's original key."""
        with self._lock:
            canonical = self._aliases.get((group_id, message_id), message_id)
            slot = self._slots.get(canonical)
            if slot is not None and slot.group_id == group_id:
                return slot.accepted
            if (group_id, canonical) in self._delivered:
                return True
            matches = [slot for slot in self._slots.values()
                       if slot.group_id == group_id and sender_id and slot.sender_id == sender_id
                       and _same_text(slot.text, text)
                       and slot.message_id not in self._aliases.values()]
            if len(matches) == 1:
                self._aliases[(group_id, message_id)] = matches[0].message_id
                return matches[0].accepted
        return False

    def canonical_message_id(self, group_id: str, message_id: str) -> str:
        with self._lock:
            return self._aliases.get((group_id, message_id), message_id)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1.0
            except AssistantSendError as exc:
                self._ready.clear()
                logger.warning("[Welink] assistant link rejected: code=%s", exc.code)
            except Exception as exc:
                self._ready.clear()
                logger.warning("[Welink] assistant link down: error_type=%s", type(exc).__name__)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 30.0)

    def _session(self) -> None:
        if not self._credential_id or not self._secret:
            raise AssistantSendError("missing_assistant_credential")
        ts = str(int(time.time()))
        nonce = str(uuid.uuid4())
        protocol = build_auth_protocol(self._credential_id, self._secret, ts=ts, nonce=nonce)
        socket = self._connect(ASSISTANT_WS_URL, subprotocols=[protocol], timeout=20)
        self._connects += 1
        self._socket = socket
        try:
            self._send_raw(socket, build_register_frame())
            registered = _read_control(socket, 20, accept={"register_ok"}, reject={"register_rejected"})
            if registered is None:
                raise AssistantSendError("register_timeout")
            if registered.get("type") == "register_rejected":
                raise AssistantSendError(str(registered.get("reason") or "register_rejected"))
            self._ready.set()
            logger.info("[Welink] assistant link ready")
            last_heartbeat = time.monotonic()
            while not self._stop.is_set():
                self._expire_slots(socket)
                self._drain(socket)
                now = time.monotonic()
                if now - last_heartbeat >= self._heartbeat_seconds:
                    self._send_raw(socket, {"type": "heartbeat", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                    last_heartbeat = now
                wait = min(1.0, max(self._heartbeat_seconds - (time.monotonic() - last_heartbeat), 0.05))
                self._recv(socket, wait)
        finally:
            self._ready.clear()
            self._socket = None
            close = getattr(socket, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _recv(self, socket: Any, wait: float) -> None:
        settimeout = getattr(socket, "settimeout", None)
        if callable(settimeout):
            settimeout(wait)
        try:
            raw = socket.recv()
        except Exception as exc:
            if exc.__class__.__name__ in {"WebSocketTimeoutException", "TimeoutError"} or isinstance(exc, TimeoutError):
                return
            raise
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str) or not raw:
            return
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return
        self._on_frame(socket, frame)

    def _on_frame(self, socket: Any, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "tool_error":
            payload = _as_mapping(frame.get("payload"))
            if self._on_delivery_error is not None:
                self._on_delivery_error(
                    str(frame.get("toolSessionId") or payload.get("toolSessionId") or ""),
                    str(frame.get("traceId") or payload.get("traceId") or ""),
                )
            return
        if kind in {"register_ok", "heartbeat", "tool_done", "tool_event"}:
            return
        reply = control_reply(frame)
        if reply is not None:
            for item in reply:
                self._send_raw(socket, item)
            return
        if kind == "invoke" and frame.get("action") in {"close_session", "abort_session"}:
            self._drop_session(str((frame.get("payload") or {}).get("toolSessionId") or ""))
            return
        mention = parse_mention(frame)
        if mention is None:
            if kind == "invoke" and frame.get("action") == "chat":
                self._close_unparsed(socket, frame)
            elif kind not in {"register_rejected", "tool_error"}:
                logger.info("[Welink] assistant link ignored frame type=%s", kind or "empty")
            return
        self._open_mention(socket, mention)

    def _open_mention(self, socket: Any, mention: Mention) -> None:
        with self._lock:
            if mention.message_id in self._slots:
                return
            if (mention.group_id, mention.message_id) in self._delivered:
                already = True
                slot = None
            else:
                already = False
                suffix = uuid.uuid4().hex[:12]
                slot = _QaSlot(
                    message_id=mention.message_id,
                    group_id=mention.group_id,
                    text=mention.text,
                    tool_session_id=mention.tool_session_id,
                    trace_id=mention.trace_id,
                    reply_message_id=f"msg_notify_{suffix}",
                    part_id=f"prt_notify_{suffix}",
                    opened=False,
                    created_at=time.monotonic(),
                    sender_id=mention.sender_id,
                )
                self._slots[mention.message_id] = slot
        if already:
            self._finish_quiet(socket, mention.tool_session_id, mention.trace_id)
            return
        assert slot is not None
        self._send_qa_event(socket, slot, {"protocol": "cloud", "type": "step.start", "properties": {"messageId": slot.reply_message_id}})
        slot.opened = True
        logger.info(
            "[Welink] mention group_id=%s message_id=%s direct=%s",
            mention.group_id, mention.message_id, mention.direct,
        )
        handler = self._on_mention
        loop = self._loop
        if handler is None or loop is None:
            self._finish_quiet(socket, slot.tool_session_id, slot.trace_id)
            with self._lock:
                self._slots.pop(mention.message_id, None)
            return
        import asyncio

        future = asyncio.run_coroutine_threadsafe(handler(mention), loop)

        def done(finished: Any) -> None:
            accepted = False
            try:
                accepted = bool(finished.result())
            except Exception:
                logger.warning("[Welink] mention handler failed: message_id=%s", mention.message_id)
            with self._lock:
                slot.accepted = accepted
            if not accepted:
                self._commands.put(("close", mention.message_id))

        future.add_done_callback(done)

    def _drain(self, socket: Any) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            kind = command[0]
            if kind == "close":
                self._close_slot(socket, command[1], quiet=True)
                continue
            _kind, group_id, text, source_message_id, box, on_prepared, finish_task = command
            try:
                box.put(self._send_text(socket, group_id, text, source_message_id, on_prepared=on_prepared, finish_task=finish_task))
            except AssistantSendError as exc:
                box.put(exc)
            except Exception as exc:
                box.put(AssistantSendError(type(exc).__name__))

    def _send_text(self, socket: Any, group_id: str, text: str, source_message_id: str, *,
                   on_prepared: Callable[[str, str, str], None] | None = None,
                   finish_task: bool = True) -> str:
        if finish_task:
            slot = self._take_slot(source_message_id, group_id)
        else:
            # A notice must not consume the task needed for the eventual final answer.
            with self._lock:
                canonical = self._aliases.get((group_id, source_message_id), source_message_id)
                slot = self._slots.get(canonical) if source_message_id else None
                if slot is None or slot.group_id != group_id:
                    raise AssistantSendError("direct_session_closed")
                suffix = uuid.uuid4().hex[:12]
                slot = replace(slot, reply_message_id=f"msg_notice_{suffix}", part_id=f"prt_notice_{suffix}")
        if slot is None and group_id.startswith("p2p:"):
            raise AssistantSendError("direct_session_closed")
        if slot is None:
            suffix = uuid.uuid4().hex[:12]
            message_id, frames = build_group_frames(group_id, text, suffix=suffix, trace_id=uuid.uuid4().hex)
            if on_prepared is not None:
                on_prepared(message_id, frames[0]["toolSessionId"], frames[0]["traceId"])
            for frame in frames:
                self._send_raw(socket, frame)
            if source_message_id:
                with self._lock:
                    self._delivered.add((group_id, source_message_id))
            return message_id
        if on_prepared is not None:
            on_prepared(slot.reply_message_id, slot.tool_session_id, slot.trace_id)
        if not finish_task:
            self._send_qa_event(socket, slot, {
                "protocol": "cloud", "type": "step.start", "properties": {"messageId": slot.reply_message_id},
            })
        self._send_qa_event(
            socket,
            slot,
            {
                "protocol": "cloud",
                "type": "text.done",
                "properties": {"messageId": slot.reply_message_id, "partId": slot.part_id, "content": text},
            },
        )
        self._send_qa_event(socket, slot, {"protocol": "cloud", "type": "step.done", "properties": {"messageId": slot.reply_message_id}})
        if not finish_task:
            return slot.reply_message_id
        self._send_raw(
            socket,
            {
                "type": "tool_done",
                "toolSessionId": slot.tool_session_id,
                "traceId": slot.trace_id,
                "businessPushType": "q_a",
            },
        )
        with self._lock:
            self._delivered.add((group_id, slot.message_id))
            if source_message_id:
                self._delivered.add((group_id, source_message_id))
        return slot.reply_message_id

    def _take_slot(self, source_message_id: str, group_id: str) -> _QaSlot | None:
        with self._lock:
            if source_message_id:
                canonical = self._aliases.get((group_id, source_message_id), source_message_id)
                slot = self._slots.get(canonical)
                if slot is not None and slot.group_id == group_id:
                    return self._slots.pop(canonical)
                return None
            matches = [slot for slot in self._slots.values() if slot.group_id == group_id]
            if not matches:
                return None
            oldest = min(matches, key=lambda slot: slot.created_at)
            return self._slots.pop(oldest.message_id)

    def _close_slot(self, socket: Any, message_id: str, *, quiet: bool) -> None:
        with self._lock:
            slot = self._slots.pop(message_id, None)
        if slot is None or not quiet:
            return
        if slot.opened:
            self._send_qa_event(socket, slot, {"protocol": "cloud", "type": "step.done", "properties": {"messageId": slot.reply_message_id}})
        self._finish_quiet(socket, slot.tool_session_id, slot.trace_id)

    def _expire_slots(self, socket: Any) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, slot in self._slots.items() if now - slot.created_at >= self._slot_ttl_seconds]
        for key in expired:
            logger.info("[Welink] mention session closed without a reply message_id=%s", key)
            self._close_slot(socket, key, quiet=True)

    def _drop_session(self, tool_session_id: str) -> None:
        if not tool_session_id:
            return
        with self._lock:
            for key, slot in list(self._slots.items()):
                if slot.tool_session_id == tool_session_id:
                    self._slots.pop(key, None)

    def _close_unparsed(self, socket: Any, frame: dict[str, Any]) -> None:
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        tool_session_id = str(payload.get("toolSessionId") or "").strip()
        trace_id = str(frame.get("traceId") or "").strip()
        if tool_session_id and trace_id:
            self._finish_quiet(socket, tool_session_id, trace_id)

    def _finish_quiet(self, socket: Any, tool_session_id: str, trace_id: str) -> None:
        self._send_raw(
            socket,
            {
                "type": "tool_done",
                "toolSessionId": tool_session_id,
                "traceId": trace_id,
                "businessPushType": "q_a",
            },
        )

    def _send_qa_event(self, socket: Any, slot: _QaSlot, event: dict[str, Any]) -> None:
        self._send_raw(
            socket,
            {
                "type": "tool_event",
                "toolSessionId": slot.tool_session_id,
                "traceId": slot.trace_id,
                "businessPushType": "q_a",
                "event": event,
            },
        )

    def _send_raw(self, socket: Any, frame: dict[str, Any]) -> None:
        socket.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    def _close_socket(self) -> None:
        socket = self._socket
        if socket is None:
            return
        close = getattr(socket, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
