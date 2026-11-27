"""Fixed-schema interface logging and cross-layer request timing collection."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from jiuwenclaw.utils import SafeRotatingFileHandler, SensitiveDataFilter


interface_logger = logging.getLogger("jiuwenclaw.channel.vibeskill_channel.interface")

_HANDLER_ATTR = "_jiuwenclaw_interface_log_file_handler"
_HANDLER_PATH_ATTR = "_jiuwenclaw_interface_log_file_path"
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 20


class TimingPoint(str, Enum):
    MESSAGE_HANDLER_QUEUE_ENTERED = "message_handler_queue_entered_ms"
    SANDBOX_CREATE_STARTED = "sandbox_create_started_ms"
    SANDBOX_CREATE_SUCCEEDED = "sandbox_create_succeeded_ms"
    SANDBOX_DCS_WRITTEN = "sandbox_dcs_written_ms"
    SANDBOX_API_KEY_UPLOADED = "sandbox_api_key_uploaded_ms"
    OA_ENDPOINT_OBTAINED = "oa_endpoint_obtained_ms"
    OA_CONNECT_STARTED = "oa_connect_started_ms"
    OA_CONNECTED = "oa_connected_ms"
    OA_REQUEST_SENT = "oa_request_sent_ms"
    OA_FIRST_RESPONSE_RECEIVED = "oa_first_response_received_ms"
    CHANNEL_FIRST_RESPONSE_SENT = "channel_first_response_sent_ms"
    OA_FINAL_RESPONSE_RECEIVED = "oa_final_response_received_ms"
    CHANNEL_FINAL_RESPONSE_SENT = "channel_final_response_sent_ms"


TIMING_POINTS = tuple(TimingPoint)


@dataclass
class RequestTiming:
    request_id: str
    origin_ns: int
    session_id: str = ""
    source: str = "Console"
    destination: str = "SkillCreator"
    interface_type: str = "WebSocket"
    interface_name: str = ""
    http_url: str = ""
    ws_event: str = ""
    ws_key: int | None = None
    points: dict[TimingPoint, int] = field(default_factory=dict)
    last_channel_send_ms: int | None = None


@dataclass
class InboundContext:
    origin_ns: int
    session_id: str
    interface_type: str
    ws_event: str
    ws_key: int | None
    delivered: bool = False
    request_id: str = ""


_records: dict[str, RequestTiming] = {}
_lock = threading.RLock()
_current_request_id: ContextVar[str] = ContextVar("interface_info_request_id", default="")
_current_inbound: ContextVar[InboundContext | None] = ContextVar(
    "interface_info_inbound", default=None
)


def configure_interface_log_path() -> None:
    raw = os.environ.get("INTERFACE_LOG_PATH", "").strip()
    existing = [h for h in interface_logger.handlers if getattr(h, _HANDLER_ATTR, False)]
    if not raw:
        for handler in existing:
            interface_logger.removeHandler(handler)
            handler.close()
        return

    log_path = Path(raw).expanduser().resolve()
    for handler in existing:
        if getattr(handler, _HANDLER_PATH_ATTR, None) == str(log_path):
            return
        interface_logger.removeHandler(handler)
        handler.close()

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingFileHandler(
            filename=str(log_path),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        logging.getLogger(__name__).exception("Cannot configure interface log at %s", log_path)
        return
    setattr(handler, _HANDLER_ATTR, True)
    setattr(handler, _HANDLER_PATH_ATTR, str(log_path))
    handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    handler.addFilter(SensitiveDataFilter())
    interface_logger.addHandler(handler)
    interface_logger.setLevel(logging.INFO)
    interface_logger.propagate = False


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text.replace("|", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _log_time() -> str:
    now = datetime.fromtimestamp(time.time())
    return f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"


def log_interface_event(
    *,
    severity: str,
    session_id: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    interface_type: str,
    interface_name: str | None = None,
    http_url: str | None = None,
    http_status: int | None = None,
    ws_event: str | None = None,
    ws_result: str | None = None,
    timings: dict[TimingPoint, int] | None = None,
) -> None:
    """Write the fixed 24-column interface log row."""
    if not interface_logger.handlers:
        return
    values = timings or {}
    row: list[Any] = [
        _log_time(), severity, session_id,
        source or "Console", destination or "SkillCreator",
        interface_type, interface_name, http_url,
        "" if http_status is None else http_status,
        ws_event, ws_result,
        *(values.get(point, "") for point in TIMING_POINTS),
    ]
    interface_logger.info("|".join(_clean(value) for value in row))


@contextmanager
def inbound_context(
    *, session_id: str, interface_type: str, ws_event: str, ws: Any = None
) -> Iterator[InboundContext]:
    context = InboundContext(
        origin_ns=time.perf_counter_ns(),
        session_id=session_id,
        interface_type=interface_type,
        ws_event=ws_event,
        ws_key=id(ws) if ws is not None else None,
    )
    token = _current_inbound.set(context)
    try:
        yield context
    finally:
        _current_inbound.reset(token)


def register_message(request_id: str, *, session_id: str = "") -> None:
    """Register a MessageHandler-bound request using its Channel inbound origin."""
    register_request(request_id, session_id=session_id)
    mark(request_id, TimingPoint.MESSAGE_HANDLER_QUEUE_ENTERED)


def register_request(
    request_id: str,
    *,
    session_id: str = "",
    source: str = "Console",
    destination: str = "SkillCreator",
    interface_type: str | None = None,
    interface_name: str = "",
    http_url: str = "",
    ws_event: str = "",
) -> None:
    """Register any Channel request without implying MessageHandler queue entry."""
    rid = str(request_id or "").strip()
    if not rid:
        return
    inbound = _current_inbound.get()
    origin_ns = inbound.origin_ns if inbound is not None else time.perf_counter_ns()
    with _lock:
        record = _records.get(rid)
        if record is None:
            record = RequestTiming(
                request_id=rid,
                origin_ns=origin_ns,
                session_id=str(session_id or (inbound.session_id if inbound else "")),
                source=source,
                destination=destination,
                interface_type=(
                    interface_type
                    or (inbound.interface_type if inbound else "WebSocket")
                ),
                interface_name=interface_name,
                http_url=http_url,
                ws_event=ws_event or (inbound.ws_event if inbound else ""),
                ws_key=inbound.ws_key if inbound else None,
            )
            _records[rid] = record
        if inbound is not None:
            inbound.delivered = True
            inbound.request_id = rid


def link_request(request_id: str, parent_request_id: str | None = None) -> bool:
    """Make an internal Agent request ID point at its current Channel request record."""
    child_id = str(request_id or "").strip()
    parent_id = str(parent_request_id or _current_request_id.get()).strip()
    if not child_id or not parent_id:
        return False
    with _lock:
        record = _records.get(parent_id)
        if record is None:
            return False
        _records[child_id] = record
    return True


def update_request_session(request_id: str, session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _lock:
        record = _records.get(str(request_id or "").strip())
        if record is not None:
            record.session_id = sid


def has_request(request_id: str) -> bool:
    with _lock:
        return str(request_id or "") in _records


def mark(request_id: str, point: TimingPoint) -> None:
    rid = str(request_id or "").strip()
    if not rid:
        return
    now_ns = time.perf_counter_ns()
    with _lock:
        record = _records.get(rid)
        if record is None:
            return
        record.points.setdefault(point, max(0, (now_ns - record.origin_ns) // 1_000_000))


def mark_current(point: TimingPoint) -> None:
    mark(_current_request_id.get(), point)


def mark_channel_response_sent() -> None:
    """Record a successful northbound write and retain its exact time for final promotion."""
    rid = _current_request_id.get()
    if not rid:
        return
    now_ns = time.perf_counter_ns()
    with _lock:
        record = _records.get(rid)
        if record is None:
            return
        elapsed_ms = max(0, (now_ns - record.origin_ns) // 1_000_000)
        record.points.setdefault(TimingPoint.CHANNEL_FIRST_RESPONSE_SENT, elapsed_ms)
        record.last_channel_send_ms = elapsed_ms


def promote_last_channel_send_to_final(request_id: str) -> None:
    rid = str(request_id or "").strip()
    with _lock:
        record = _records.get(rid)
        if record is not None and record.last_channel_send_ms is not None:
            record.points.setdefault(
                TimingPoint.CHANNEL_FINAL_RESPONSE_SENT,
                record.last_channel_send_ms,
            )


def mark_channel_final_response_sent(request_id: str) -> None:
    """Record a non-streaming Channel response completion, such as an HTTP response."""
    mark(request_id, TimingPoint.CHANNEL_FINAL_RESPONSE_SENT)


@contextmanager
def bind_request(request_id: str) -> Iterator[None]:
    token = _current_request_id.set(str(request_id or ""))
    try:
        yield
    finally:
        _current_request_id.reset(token)


def set_current_request(request_id: str) -> Any:
    return _current_request_id.set(str(request_id or ""))


def reset_current_request(token: Any) -> None:
    _current_request_id.reset(token)


def finish_request(
    request_id: str,
    *,
    severity: str = "INFO",
    http_status: int | None = None,
    ws_result: str | None = None,
) -> bool:
    rid = str(request_id or "").strip()
    with _lock:
        record = _records.get(rid)
        if record is not None:
            aliases = [key for key, value in _records.items() if value is record]
            for alias in aliases:
                _records.pop(alias, None)
    if record is None:
        return False
    log_interface_event(
        severity=severity,
        session_id=record.session_id,
        source=record.source,
        destination=record.destination,
        interface_type=record.interface_type,
        interface_name=record.interface_name,
        http_url=record.http_url,
        http_status=http_status,
        ws_event=record.ws_event,
        ws_result=ws_result,
        timings=record.points,
    )
    return True


def finish_requests_for_ws(ws: Any, *, severity: str, ws_result: str) -> None:
    ws_key = id(ws)
    with _lock:
        request_ids = [rid for rid, record in _records.items() if record.ws_key == ws_key]
    for rid in request_ids:
        finish_request(rid, severity=severity, ws_result=ws_result)


def reset_for_tests() -> None:
    with _lock:
        _records.clear()
