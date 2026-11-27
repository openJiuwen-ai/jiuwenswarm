from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenclaw.channel import vibeskill_channel as module
from jiuwenclaw.channel.vibeskill_channel import VibeSkillChannel, VibeSkillConfig
from jiuwenclaw.log import interface_info
from jiuwenclaw.schema.agent import AgentResponseChunk


class FakeRouter:
    def __init__(self) -> None:
        self.delivered: list[Any] = []

    def deliver_to_message_handler(self, msg: Any) -> None:
        self.delivered.append(msg)


class FakeAgentClient:
    def is_openability_reconnecting(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        return False


class FakeRequest:
    method = "GET"
    path_qs = "/api/v1/session/sid-http/messages?x=1"
    headers: dict[str, str] = {}
    path = "/api/v1/messages"
    query_string = ""

    async def read(self) -> bytes:
        return b""


class FakeWsRequest:
    def __init__(self, query_string: str = "") -> None:
        self.path = "/api/v1/messages"
        self.query_string = query_string
        self.headers: dict[str, str] = {}


class FakeWebSocket:
    def __init__(self, messages: list[str] | None = None, close_code: int | None = 1000) -> None:
        self.messages = list(messages or [])
        self.sent: list[dict[str, Any]] = []
        self.request = FakeWsRequest()
        self.remote_address = "127.0.0.1"
        self.closed = False
        self.close_code = close_code
        self.close_reason: str | None = None

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            self.closed = True
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


@pytest.fixture
def interface_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "interface.log"
    monkeypatch.setenv("INTERFACE_LOG_PATH", str(path))
    module._configure_interface_log_path()
    yield path
    monkeypatch.delenv("INTERFACE_LOG_PATH", raising=False)
    module._configure_interface_log_path()


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> VibeSkillChannel:
    monkeypatch.delenv("SANDBOX_DCS_HOST", raising=False)
    monkeypatch.setattr(
        VibeSkillChannel,
        "_get_local_ip",
        staticmethod(lambda: "127.0.0.1"),
    )
    return VibeSkillChannel(
        config=VibeSkillConfig(),
        router=FakeRouter(),
        agent_client=FakeAgentClient(),
    )


def _flush_interface_logger() -> None:
    for handler in module.interface_logger.handlers:
        handler.flush()


def _read_rows(path: Path) -> list[list[str]]:
    _flush_interface_logger()
    return [line.split("|") for line in path.read_text(encoding="utf-8").splitlines()]


def test_interface_log_uses_fixed_pipe_delimited_fields(interface_log_path: Path) -> None:
    module._log_interface_event(
        severity="INFO",
        session_id="sid|with\nbreak",
        interface_type="HTTP",
        http_url="GET /api/v1/session/sid\twith|pipe",
        response_time_ms=12,
        http_status=200,
    )

    rows = _read_rows(interface_log_path)
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 24
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", row[0])
    assert row[1:] == [
        "INFO",
        "sid with break",
        "Console",
        "SkillCreator",
        "HTTP",
        "SessionQuery",
        "GET /api/v1/session/sid with pipe",
        "200",
        "",
        "",
        "", "", "", "", "", "", "", "", "", "", "", "", "",
    ]


def test_internal_outbound_interface_logs_source_destination(
    interface_log_path: Path,
) -> None:
    interface_info.log_interface_event(
        severity="INFO",
        session_id="sid-internal",
        source="SkillCreator",
        destination="OpenAbility",
        interface_type="WebSocket",
        ws_event="connect",
        ws_result="success",
    )
    interface_info.log_interface_event(
        severity="WARN",
        session_id="sid-internal",
        source="SkillCreator",
        destination="SandboxManager",
        interface_type="HTTP",
        http_url="POST http://sandbox-manager/api/sandboxes",
        http_status=503,
    )

    rows = _read_rows(interface_log_path)
    assert all(len(row) == 24 for row in rows)
    assert rows[0][1:11] == [
        "INFO", "sid-internal", "SkillCreator", "OpenAbility",
        "WebSocket", "", "", "", "connect", "success",
    ]
    assert rows[0][11:24] == [""] * 13
    assert rows[1][1:11] == [
        "WARN", "sid-internal", "SkillCreator", "SandboxManager",
        "HTTP", "", "POST http://sandbox-manager/api/sandboxes", "503", "", "",
    ]
    assert rows[1][11:24] == [""] * 13


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/session", "SessionCreate"),
        ("GET", "/api/v1/session/sid-1", "SessionQuery"),
        ("POST", "/api/v1/session/sid-1/abort", "SessionAbort"),
        ("GET", "/api/v1/session/sid-1/messages", "SessionRestore"),
        ("DELETE", "/api/v1/session/sid-1", "SessionDelete"),
        ("GET", "/api/v1/session/sid-1/file", "FileList"),
        ("GET", "/api/v1/session/sid-1/file/content?path=SKILL.md", "FileRead"),
        ("PUT", "/api/v1/session/sid-1/file/content?path=SKILL.md", "FileWrite"),
        ("POST", "/api/v1/session/sid-1/export", "SkillExport"),
        ("GET", "/api/v1/session/sid-1/file/status", ""),
    ],
)
def test_http_interface_name_mapping(method: str, path: str, expected: str) -> None:
    assert module._http_interface_name(path, method) == expected


@pytest.mark.asyncio
async def test_aiohttp_http_handler_logs_success_status_and_session(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    async def fake_http_handler(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "application/json"}, b'{"sessionID":"sid-created"}'

    request = FakeRequest()
    request.method = "POST"
    request.path_qs = "/api/v1/session"
    channel.http_handler = fake_http_handler  # type: ignore[method-assign]

    await channel._aiohttp_http_handler(request)

    row = _read_rows(interface_log_path)[0]
    assert len(row) == 24
    assert row[1] == "INFO"
    assert row[2] == "sid-created"
    assert row[3] == "Console"
    assert row[4] == "SkillCreator"
    assert row[5] == "HTTP"
    assert row[6] == "SessionCreate"
    assert row[7] == "POST /api/v1/session"
    assert row[8] == "200"
    assert row[9:23] == [""] * 14
    assert row[23].isdigit()


@pytest.mark.asyncio
async def test_aiohttp_http_handler_logs_response_sent_to_main_logger(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    async def fake_http_handler(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "application/json"}, b"{}"

    request = FakeRequest()
    channel.http_handler = fake_http_handler  # type: ignore[method-assign]

    captured: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CaptureHandler()
    module.logger.addHandler(handler)
    try:
        await channel._aiohttp_http_handler(request)
    finally:
        module.logger.removeHandler(handler)

    messages = [
        r.getMessage()
        for r in captured
        if "[VibeSkillChannel] HTTP 响应已发送" in r.getMessage()
    ]
    assert len(messages) == 1
    assert "status=200" in messages[0]
    assert "session_id=sid-http" in messages[0]
    assert "path=/api/v1/session/sid-http/messages?x=1" in messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "severity"),
    [(404, "WARN"), (500, "ERROR")],
)
async def test_aiohttp_http_handler_logs_error_severity_by_status(
    channel: VibeSkillChannel,
    interface_log_path: Path,
    status: int,
    severity: str,
) -> None:
    async def fake_http_handler(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        return status, {"Content-Type": "application/json"}, b"{}"

    request = FakeRequest()
    channel.http_handler = fake_http_handler  # type: ignore[method-assign]

    await channel._aiohttp_http_handler(request)

    row = _read_rows(interface_log_path)[0]
    assert len(row) == 24
    assert row[1] == severity
    assert row[2] == "sid-http"
    assert row[3] == "Console"
    assert row[4] == "SkillCreator"
    assert row[6] == "SessionRestore"
    assert row[7] == "GET /api/v1/session/sid-http/messages?x=1"
    assert row[8] == str(status)
    assert row[23].isdigit()


@pytest.mark.asyncio
async def test_aiohttp_messages_without_upgrade_logs_http_426(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    request = FakeRequest()
    request.method = "GET"
    request.path = "/api/v1/messages"
    request.path_qs = "/api/v1/messages"
    request.headers = {}

    response = await channel._aiohttp_ws_handler(request)

    row = _read_rows(interface_log_path)[0]
    assert response.status == 426
    assert len(row) == 24
    assert row[1] == "WARN"
    assert row[3] == "Console"
    assert row[4] == "SkillCreator"
    assert row[5] == "HTTP"
    assert row[6] == ""
    assert row[7] == "GET /api/v1/messages"
    assert row[8] == "426"
    assert row[23].isdigit()


@pytest.mark.asyncio
async def test_http_agent_request_shares_timing_context_with_internal_request_id(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    class TimingAgentClient(FakeAgentClient):
        async def send_request(self, env: Any) -> Any:
            interface_info.mark_current(
                interface_info.TimingPoint.SANDBOX_CREATE_STARTED
            )
            interface_info.mark(
                env.request_id, interface_info.TimingPoint.OA_REQUEST_SENT
            )
            interface_info.mark(
                env.request_id,
                interface_info.TimingPoint.OA_FIRST_RESPONSE_RECEIVED,
            )
            interface_info.mark(
                env.request_id,
                interface_info.TimingPoint.OA_FINAL_RESPONSE_RECEIVED,
            )
            return SimpleNamespace(ok=True, payload={})

    channel._agent_client = TimingAgentClient()

    async def fake_http_handler(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        env = SimpleNamespace(request_id="internal-agent-request")
        await channel._send_agent_request(env)
        return 200, {"Content-Type": "application/json"}, b"{}"

    channel.http_handler = fake_http_handler  # type: ignore[method-assign]
    await channel._aiohttp_http_handler(FakeRequest())

    row = _read_rows(interface_log_path)[0]
    assert row[11] == ""  # HTTP does not enter the MessageHandler queue.
    assert row[12].isdigit()
    assert row[19].isdigit()
    assert row[20].isdigit()
    assert row[21] == ""  # The northbound HTTP response is not streamed.
    assert row[22].isdigit()
    assert row[23].isdigit()
    assert interface_info.has_request("internal-agent-request") is False


@pytest.mark.asyncio
async def test_websocket_logs_inbound_and_close(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    session = await channel._store.get_or_create(session_id="sid-ws", mode="SkillCreate")
    payload = {
        "type": "message.send",
        "id": "req-1",
        "sessionID": session.session_id,
        "parts": [{"type": "text", "text": "build a skill"}],
    }
    ws = FakeWebSocket([json.dumps(payload)])

    await channel._handle_ws_connection(ws)

    rows = _read_rows(interface_log_path)
    assert all(len(row) == 24 for row in rows)
    inbound = [row for row in rows if row[9] == "message.send"]
    assert inbound
    assert inbound[-1][1] == "INFO"
    assert inbound[-1][2] == "sid-ws"
    assert inbound[-1][3] == "Console"
    assert inbound[-1][4] == "SkillCreator"
    assert inbound[-1][5] == "WebSocket"
    assert inbound[-1][10] == "disconnected"
    assert inbound[-1][11].isdigit()
    close = [row for row in rows if row[9] == "close"][-1]
    assert close[1] == "INFO"
    assert close[2] == "sid-ws"
    assert close[3] == "Console"
    assert close[4] == "SkillCreator"
    assert close[10] == "success"
    assert close[11:24] == [""] * 13


@pytest.mark.asyncio
async def test_websocket_logs_invalid_json_as_warn(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    ws = FakeWebSocket(["not-json"])

    await channel._handle_ws_connection(ws)

    rows = _read_rows(interface_log_path)
    invalid = [row for row in rows if row[9] == "invalid_json"]
    assert invalid
    assert invalid[-1][1] == "WARN"
    assert invalid[-1][5] == "WebSocket"
    assert invalid[-1][10] == "error"
    assert invalid[-1][11] == ""


@pytest.mark.asyncio
async def test_send_ws_json_does_not_log_outbound_event(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    ws = FakeWebSocket()

    await channel._send_ws_json(
        ws,
        {"type": "message.updated", "properties": {"sessionID": "sid-out"}},
        source="test",
    )

    assert _read_rows(interface_log_path) == []


def test_interface_log_path_does_not_capture_business_logger(
    interface_log_path: Path,
) -> None:
    module.logger.info("[VibeSkillChannel] business log should not enter interface file")
    module._log_interface_event(severity="INFO", interface_type="HTTP", http_url="GET /ok")

    text = interface_log_path.read_text(encoding="utf-8")
    assert "[VibeSkillChannel]" not in text
    assert "vibeskill_channel.py" not in text
    assert "GET /ok" in text


def test_request_timing_logs_partial_integer_milliseconds(
    interface_log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([1_000_000_000, 1_004_900_000, 1_010_100_000, 1_020_900_000])
    monkeypatch.setattr(interface_info.time, "perf_counter_ns", lambda: next(ticks))

    ws = object()
    with interface_info.inbound_context(
        session_id="sid-timing",
        interface_type="WebSocket",
        ws_event="message.send",
        ws=ws,
    ):
        interface_info.register_message("req-timing", session_id="sid-timing")
        interface_info.mark(
            "req-timing", interface_info.TimingPoint.OA_REQUEST_SENT
        )
        # First-write-wins: a retry must not replace the first timestamp.
        interface_info.mark(
            "req-timing", interface_info.TimingPoint.OA_REQUEST_SENT
        )

    assert interface_info.finish_request("req-timing") is True
    assert interface_info.finish_request("req-timing") is False

    row = _read_rows(interface_log_path)[0]
    assert len(row) == 24
    assert row[2:11] == [
        "sid-timing", "Console", "SkillCreator", "WebSocket", "", "", "", "message.send", "",
    ]
    assert row[11] == "4"
    assert row[19] == "10"
    assert row[12:19] == [""] * 7
    assert row[20:24] == [""] * 4


def test_ws_disconnect_flushes_only_associated_requests(
    interface_log_path: Path,
) -> None:
    ws_one = object()
    ws_two = object()
    with interface_info.inbound_context(
        session_id="sid-1", interface_type="WebSocket", ws_event="message.send", ws=ws_one
    ):
        interface_info.register_message("req-1", session_id="sid-1")
    with interface_info.inbound_context(
        session_id="sid-2", interface_type="WebSocket", ws_event="question.replied", ws=ws_two
    ):
        interface_info.register_message("req-2", session_id="sid-2")

    interface_info.finish_requests_for_ws(
        ws_one, severity="WARN", ws_result="disconnected"
    )
    assert interface_info.has_request("req-1") is False
    assert interface_info.has_request("req-2") is True
    interface_info.finish_request("req-2")

    rows = _read_rows(interface_log_path)
    assert rows[0][1:3] == ["WARN", "sid-1"]
    assert rows[0][10] == "disconnected"
    assert rows[1][2] == "sid-2"


@pytest.mark.asyncio
async def test_terminal_agent_message_logs_actual_first_and_final_ws_writes(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    ws = FakeWebSocket()
    channel._session_to_ws["sid-final"] = ws
    with interface_info.inbound_context(
        session_id="sid-final",
        interface_type="WebSocket",
        ws_event="message.send",
        ws=ws,
    ):
        interface_info.register_message("req-final", session_id="sid-final")

    msg = module.Message(
        id="req-final",
        type="event",
        channel_id="vibeskill",
        session_id="sid-final",
        params={},
        timestamp=0.0,
        ok=True,
        payload={"event_type": "chat.final", "content": "done"},
    )
    await channel.send(msg)

    rows = _read_rows(interface_log_path)
    assert len(rows) == 1
    assert rows[0][10] == "success"
    assert rows[0][21].isdigit()
    assert rows[0][23].isdigit()
    assert int(rows[0][23]) >= int(rows[0][21])
    assert interface_info.has_request("req-final") is False


@pytest.mark.asyncio
async def test_agent_completed_records_final_send_before_closing_ws(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    session = await channel._store.get_or_create(
        session_id="sid-agent-completed",
        mode="SkillCreate",
    )
    ws = FakeWebSocket()
    channel._session_to_ws[session.session_id] = ws
    with interface_info.inbound_context(
        session_id=session.session_id,
        interface_type="WebSocket",
        ws_event="message.send",
        ws=ws,
    ):
        interface_info.register_message(
            "req-agent-completed",
            session_id=session.session_id,
        )

    msg = module.Message(
        id="req-agent-completed",
        type="event",
        channel_id="vibeskill",
        session_id=session.session_id,
        params={},
        timestamp=0.0,
        ok=True,
        payload={"event_type": "skilldev.agent_completed"},
    )
    await channel.send(msg)

    row = _read_rows(interface_log_path)[0]
    assert ws.closed is True
    assert row[9] == "message.send"
    assert row[10] == "success"
    assert row[21].isdigit()
    assert row[23].isdigit()
    assert interface_info.has_request("req-agent-completed") is False


@pytest.mark.asyncio
async def test_stream_request_context_can_close_from_different_task(
    channel: VibeSkillChannel,
    interface_log_path: Path,
) -> None:
    class StreamingAgentClient(FakeAgentClient):
        closed = False

        async def send_request_stream(self, env: Any) -> Any:
            interface_info.mark_current(interface_info.TimingPoint.OA_REQUEST_SENT)
            try:
                yield AgentResponseChunk(
                    request_id=env.request_id,
                    channel_id="vibeskill",
                    payload={"event_type": "restore.chunk"},
                )
                await asyncio.Event().wait()
            finally:
                self.closed = True
                interface_info.mark_current(
                    interface_info.TimingPoint.OA_FINAL_RESPONSE_RECEIVED
                )

    channel._agent_client = StreamingAgentClient()
    request_id = "req-stream-close"
    with interface_info.inbound_context(
        session_id="sid-stream-close",
        interface_type="HTTP",
        ws_event="",
    ):
        interface_info.register_request(
            request_id,
            session_id="sid-stream-close",
            interface_type="HTTP",
            interface_name="SessionRestore",
            http_url="GET /api/v1/session/sid-stream-close/messages",
        )

    stream = channel._send_agent_request_stream(SimpleNamespace(request_id=request_id))
    chunk = await stream.__anext__()
    assert chunk.request_id == request_id

    async def close_stream() -> None:
        await stream.aclose()

    await asyncio.create_task(close_stream())

    assert channel._agent_client.closed is True
    assert interface_info.finish_request(request_id) is True
    row = _read_rows(interface_log_path)[0]
    assert row[6] == "SessionRestore"
    assert row[19].isdigit()
    assert row[22].isdigit()
