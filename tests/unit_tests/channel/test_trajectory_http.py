# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Gateway single-Agent trajectory HTTP API."""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.gateway.channel_manager.web import trajectory_http
from jiuwenswarm.gateway.channel_manager.web.trajectory_http import (
    TRAJECTORY_API_PREFIX,
    TrajectoryHttpService,
    attach_trajectory_routes,
)
from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    session_database_path,
)
from jiuwenswarm.observability.models import TraceRecordData
from jiuwenswarm.observability.store import AsyncTrajectoryReader, TrajectoryStore

test_logger = logging.getLogger("tests.trajectory_http")

_TRACE_ID = "4" * 32
_SPAN_ID = "d" * 16
_LATE_SPAN_ID = "e" * 16


def _settings(database_path: Path, *, enabled: bool = True) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=enabled,
        database_path=database_path,
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=20,
        poll_interval_ms=2000,
    )


def _raw_record() -> bytes:
    return (
        b'{"resourceSpans":[{"resource":{},"scopeSpans":[{"scope":{},"spans":['
        b'{"traceId":"44444444444444444444444444444444",'
        b'"spanId":"dddddddddddddddd","parentSpanId":"","name":"agent.run",'
        b'"startTimeUnixNano":"100","endTimeUnixNano":"200","status":{}}]}]}]}'
    )


def _seed(database_path: Path, *, session_id: str = "session-1") -> bytes:
    raw_json = _raw_record()
    core_record = SimpleNamespace(
        raw_json=raw_json,
        trace_id=_TRACE_ID,
        span_id=_SPAN_ID,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id=session_id,
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([TraceRecordData.from_core_record(core_record)])
    finally:
        store.close()
    return raw_json


def _append_late_span(database_path: Path, *, session_id: str = "session-1") -> None:
    raw_json = _raw_record().replace(_SPAN_ID.encode("ascii"), _LATE_SPAN_ID.encode("ascii"))
    core_record = SimpleNamespace(
        raw_json=raw_json,
        trace_id=_TRACE_ID,
        span_id=_LATE_SPAN_ID,
        parent_span_id=_SPAN_ID,
        start_time_unix_nano=210,
        end_time_unix_nano=300,
        session_id=session_id,
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([TraceRecordData.from_core_record(core_record)])
    finally:
        store.close()


def _metadata_loader(mode: str = "agent.work.normal"):
    def _load(session_id: str) -> dict[str, str]:
        if session_id in {"session-1", "session-2"}:
            return {"session_id": session_id, "mode": mode, "team_name": ""}
        return {}

    return _load


def _response_json(response) -> dict:
    return json.loads(bytes(response.body))


@contextmanager
def _serve_asgi(app: FastAPI) -> Iterator[int]:
    """Serve a FastAPI app on a real loopback socket for proxy tests."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("trajectory test Gateway did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@contextmanager
def _serve_web_proxy(api_port: int, directory: Path) -> Iterator[int]:
    """Serve the built-in Web UI reverse proxy on a loopback socket."""

    class _TestProxyHandler(_SpaStaticHandler):
        def log_message(self, message: str, *args) -> None:
            test_logger.debug(message, *args)

    _TestProxyHandler.api_target = f"http://127.0.0.1:{api_port}"
    _TestProxyHandler.ws_target = f"ws://127.0.0.1:{api_port}"
    handler = partial(_TestProxyHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _proxy_get(
    proxy_port: int,
    path: str,
    *,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    """Issue one browser-facing request through the real Web proxy."""
    connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        response_headers = {
            key.lower(): value
            for key, value in response.getheaders()
        }
        return response.status, response_headers, body
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_http_list_detail_and_raw_preserve_contract(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    expected_raw = _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(),
    )

    list_response = await service.list_traces("session-1", limit=30, cursor=None)
    list_payload = _response_json(list_response)
    detail_response = await service.get_trace(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
    )
    detail_payload = _response_json(detail_response)
    raw_response = await service.get_raw_record("session-1", _TRACE_ID, _SPAN_ID)

    assert list_response.status_code == 200
    assert list_response.headers["cache-control"] == "no-store"
    assert list_payload["items"][0]["start_time_unix_nano"] == "100"
    assert list_payload["items"][0]["end_time_unix_nano"] == "200"
    assert isinstance(list_payload["revision_cursor"], str)
    assert list_payload["revision_cursor"]
    assert isinstance(list_payload["store_epoch"], str)
    assert list_payload["store_epoch"]
    assert detail_response.status_code == 200
    assert detail_payload["trace_id"] == _TRACE_ID
    detail_record = detail_payload["records"][0]
    assert detail_record["record_id"] == f"{_TRACE_ID}:{_SPAN_ID}"
    assert detail_record["record_revision"] == 1
    assert detail_record["lifecycle"] == "final"
    assert detail_record["operation"] == "upsert"
    assert detail_record["change_seq"] == detail_payload["revision"]
    assert detail_record["observed_time_unix_nano"] == "200"
    assert detail_record["otlp"]["resourceSpans"]
    assert raw_response.status_code == 200
    assert raw_response.headers["cache-control"] == "no-store"
    assert raw_response.headers["content-type"] == "application/json; charset=utf-8"
    assert bytes(raw_response.body) == expected_raw
    test_logger.info("HTTP contract returned list, detail, and exact raw bytes")


@pytest.mark.asyncio
async def test_http_revision_feed_reports_late_trace_change(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(),
    )
    list_response = await service.list_traces("session-1", limit=30, cursor=None)
    revision_cursor = _response_json(list_response)["revision_cursor"]
    _append_late_span(database_path)

    revision_response = await service.list_revisions(
        "session-1",
        after_revision=revision_cursor,
        limit=100,
    )
    payload = _response_json(revision_response)

    assert revision_response.status_code == 200
    assert revision_response.headers["cache-control"] == "no-store"
    assert payload["session_id"] == "session-1"
    assert payload["items"][0]["trace_id"] == _TRACE_ID
    assert payload["items"][0]["span_count"] == 2
    assert payload["items"][0]["start_time_unix_nano"] == "100"
    assert payload["items"][0]["end_time_unix_nano"] == "300"
    assert payload["has_more"] is False
    assert payload["reset"] is False
    assert payload["store_epoch"] == _response_json(list_response)["store_epoch"]
    assert payload["next_cursor"] == payload["watermark"]
    assert payload["next_cursor"] != revision_cursor
    test_logger.info("HTTP revision feed exposed a late update to an old trace")


@pytest.mark.asyncio
async def test_http_archive_exports_all_current_records_beyond_list_window(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    records: list[TraceRecordData] = []
    for index in range(105):
        trace_id = f"{index + 1:032x}"
        span_id = f"{index + 1:016x}"
        raw_json = _raw_record().replace(
            _TRACE_ID.encode("ascii"),
            trace_id.encode("ascii"),
        ).replace(
            _SPAN_ID.encode("ascii"),
            span_id.encode("ascii"),
        )
        core_record = SimpleNamespace(
            raw_json=raw_json,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            start_time_unix_nano=100 + index,
            end_time_unix_nano=200 + index,
            session_id="session-1",
            request_id=f"request-{index + 1}",
            run_id="run-archive",
            agent_mode="agent.work.normal",
            schema_version="1",
            record_revision=3,
            observed_time_unix_nano=200 + index,
        )
        records.append(TraceRecordData.from_core_record(core_record))
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        result = store.write_records(records)
        live_trace_id = f"{1:032x}"
        live_span_id = "f" * 16
        live_parent_span_id = f"{1:016x}"
        live_raw = _raw_record().replace(
            _TRACE_ID.encode("ascii"),
            live_trace_id.encode("ascii"),
        ).replace(
            _SPAN_ID.encode("ascii"),
            live_span_id.encode("ascii"),
        ).replace(
            b'"parentSpanId":""',
            f'"parentSpanId":"{live_parent_span_id}"'.encode("ascii"),
        ).replace(
            b',"endTimeUnixNano":"200"',
            b"",
        )
        for revision in (1, 2):
            snapshot = SimpleNamespace(
                raw_json=live_raw,
                trace_id=live_trace_id,
                span_id=live_span_id,
                parent_span_id=live_parent_span_id,
                start_time_unix_nano=150,
                observed_time_unix_nano=150 + revision,
                record_revision=revision,
                update_kind="stream_chunk",
                session_id="session-1",
                request_id="request-live",
                run_id="run-archive",
                agent_mode="agent.work.normal",
                schema_version="1",
                lifecycle="running",
            )
            store.write_records([TraceRecordData.from_core_snapshot(snapshot)])
    finally:
        store.close()
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(),
    )

    list_payload = _response_json(
        await service.list_traces("session-1", limit=30, cursor=None)
    )
    response = await service.export_archive("session-1")
    payload = _response_json(response)

    assert result.inserted == 105
    assert len(list_payload["items"]) == 30
    assert list_payload["next_cursor"] is not None
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="trajectory-session-1.archive.json"'
    )
    assert payload["format"] == "openjiuwen.trajectory.archive"
    assert payload["archive_version"] == 1
    assert payload["session_id"] == "session-1"
    assert payload["exported_at"].endswith("Z")
    assert isinstance(payload["store_epoch"], str)
    assert payload["revision"].isdecimal()
    assert len(payload["records"]) == 106
    assert len({record["record_id"] for record in payload["records"]}) == 106
    assert {record["operation"] for record in payload["records"]} == {"upsert"}
    assert all(
        isinstance(record["change_seq"], str) and record["change_seq"].isdecimal()
        for record in payload["records"]
    )
    live_records = [
        record for record in payload["records"] if record["span_id"] == live_span_id
    ]
    assert len(live_records) == 1
    assert live_records[0]["lifecycle"] == "running"
    assert live_records[0]["record_revision"] == 2
    final_records = [
        record for record in payload["records"] if record["span_id"] != live_span_id
    ]
    assert {record["lifecycle"] for record in final_records} == {"final"}
    assert {record["record_revision"] for record in final_records} == {3}
    assert all(record["otlp"]["resourceSpans"] for record in payload["records"])
    assert all(record["raw_valid"] is True for record in payload["records"])
    assert all(record["raw_json_base64"] for record in payload["records"])
    test_logger.info("archive exported every current record beyond the trace list window")


@pytest.mark.asyncio
async def test_http_archive_preserves_invalid_otlp_as_raw_base64(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    malformed_raw = b'{"resourceSpans":[invalid-json'
    core_record = SimpleNamespace(
        raw_json=malformed_raw,
        trace_id=_TRACE_ID,
        span_id=_SPAN_ID,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id="session-1",
        request_id="request-invalid",
        run_id="run-invalid",
        agent_mode="agent.work.normal",
        schema_version="1",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([TraceRecordData.from_core_record(core_record)])
    finally:
        store.close()
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(),
    )

    response = await service.export_archive("session-1")
    payload = _response_json(response)
    record = payload["records"][0]

    assert response.status_code == 200
    assert record["operation"] == "upsert"
    assert record["otlp"] is None
    assert record["raw_valid"] is False
    assert base64.b64decode(record["raw_json_base64"], validate=True) == malformed_raw
    test_logger.info("archive retained malformed OTLP bytes for offline diagnostics")


@pytest.mark.asyncio
async def test_archive_get_download_preserves_execution_subject_and_access(
    tmp_path: Path,
) -> None:
    database_root = tmp_path / "sessions"
    database_path = session_database_path(database_root, "session-1")
    raw_payload = json.loads(_raw_record())
    raw_span = raw_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    subject_attributes = {
        "openjiuwen.execution.subject.id": "subagent:research:invocation-7",
        "openjiuwen.execution.subject.display_name": "Research Agent",
        "openjiuwen.execution.subject.kind": "subagent",
        "openjiuwen.execution.subject.parent_id": "main",
        "openjiuwen.execution.subject.session_id": "subsession-research-7",
    }
    raw_span["attributes"] = [
        {"key": key, "value": {"stringValue": value}}
        for key, value in subject_attributes.items()
    ]
    raw_json = json.dumps(raw_payload, separators=(",", ":")).encode("utf-8")
    core_record = SimpleNamespace(
        raw_json=raw_json,
        trace_id=_TRACE_ID,
        span_id=_SPAN_ID,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id="session-1",
        request_id="request-subagent",
        run_id="run-subagent",
        agent_mode="agent.work.normal",
        schema_version="1",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([TraceRecordData.from_core_record(core_record)])
        connection = store._require_connection()
        stored_raw = bytes(
            connection.execute(
                "SELECT raw_json FROM otlp_span_records WHERE trace_id = ? AND span_id = ?",
                (_TRACE_ID, _SPAN_ID),
            ).fetchone()["raw_json"]
        )
        current_raw = bytes(
            connection.execute(
                "SELECT raw_json FROM trajectory_current_records WHERE trace_id = ? AND span_id = ?",
                (_TRACE_ID, _SPAN_ID),
            ).fetchone()["raw_json"]
        )
        change_raw = bytes(
            connection.execute(
                "SELECT raw_json FROM trajectory_changes WHERE trace_id = ? AND span_id = ?",
                (_TRACE_ID, _SPAN_ID),
            ).fetchone()["raw_json"]
        )
    finally:
        store.close()
    app = FastAPI()
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(database_root),
        metadata_loader=_metadata_loader(),
    )
    team_app = FastAPI()
    attach_trajectory_routes(
        team_app,
        SimpleNamespace(),
        settings=_settings(database_root),
        metadata_loader=_metadata_loader("team.plan.normal"),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/archive"
        )
        missing = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-missing/archive"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=team_app),
        base_url="http://test",
    ) as client:
        forbidden = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/archive"
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == (
        'attachment; filename="trajectory-session-1.archive.json"'
    )
    assert int(response.headers["content-length"]) == len(response.content)
    assert len(response.content) > len(raw_json)
    assert stored_raw == raw_json
    assert current_raw == raw_json
    assert change_raw == b""
    payload = response.json()
    assert payload["format"] == "openjiuwen.trajectory.archive"
    assert payload["archive_version"] == 1
    assert len(payload["records"]) == 1
    archived_record = payload["records"][0]
    assert base64.b64decode(
        archived_record["raw_json_base64"],
        validate=True,
    ) == raw_json
    archived_span = archived_record["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in archived_span["attributes"]
    } == subject_attributes
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "UNSUPPORTED_SESSION_MODE"
    test_logger.info("archive GET downloaded non-empty subject-preserving single-Agent data")


@pytest.mark.asyncio
async def test_http_revision_feed_exposes_epoch_reset_after_retention(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(),
    )
    baseline_payload = _response_json(
        await service.list_traces("session-1", limit=30, cursor=None)
    )

    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        assert store.delete_expired(now=int(time.time()) + 2 * 86400) == 1
    finally:
        store.close()

    response = await service.list_revisions(
        "session-1",
        after_revision=baseline_payload["revision_cursor"],
        limit=100,
    )
    payload = _response_json(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["items"] == []
    assert payload["reset"] is True
    assert payload["store_epoch"] != baseline_payload["store_epoch"]
    assert payload["next_cursor"] == payload["watermark"]
    test_logger.info("HTTP revision response exposed retention as an epoch reset")


@pytest.mark.asyncio
async def test_http_revision_feed_rejects_invalid_cursor(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader(),
    )

    response = await service.list_revisions(
        "session-1",
        after_revision="not-a-cursor",
        limit=100,
    )

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert _response_json(response)["code"] == "BAD_REQUEST"
    test_logger.info("invalid revision cursors failed with the stable error envelope")


@pytest.mark.asyncio
async def test_http_forbids_auto_harness_sessions(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    harness_service = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader("auto_harness"),
    )
    harness_plan_service = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader("auto_harness.plan"),
    )

    harness_response = await harness_service.list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )
    harness_plan_response = await harness_plan_service.list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )

    assert harness_response.status_code == 403
    assert harness_response.headers["cache-control"] == "no-store"
    assert _response_json(harness_response)["code"] == "UNSUPPORTED_SESSION_MODE"
    assert harness_plan_response.status_code == 403
    assert harness_plan_response.headers["cache-control"] == "no-store"
    assert _response_json(harness_plan_response)["code"] == "UNSUPPORTED_SESSION_MODE"
    test_logger.info("non-single-Agent and non-Team sessions rejected by the server")


@pytest.mark.asyncio
async def test_http_accepts_team_sessions_with_team_name(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)

    def _team_metadata(session_id: str) -> dict[str, str]:
        if session_id in {"session-1", "session-2"}:
            return {
                "session_id": session_id,
                "mode": "team.plan.normal",
                "team_name": "research-team",
            }
        return {}

    team_service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_team_metadata,
    )
    response = await team_service.list_traces("session-1", limit=30, cursor=None)
    assert response.status_code == 200
    payload = _response_json(response)
    assert payload["session_id"] == "session-1"
    assert len(payload["items"]) == 1
    test_logger.info("Team sessions with a team_name accepted by the trajectory server")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "agent.work.normal",
        "agent.work.plan",
        "agent.code.normal",
        "agent.code.plan",
    ],
)
async def test_http_accepts_new_single_agent_canonical_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(mode),
    )

    response = await service.list_traces("session-1", limit=30, cursor=None)

    assert response.status_code == 200
    assert len(_response_json(response)["items"]) == 1
    test_logger.info("new canonical single-Agent mode can read trajectory: %s", mode)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["agent", "agent.fast", "agent.plan", "code", "code.normal", "code.plan"],
)
async def test_http_rejects_legacy_single_agent_mode_names(
    tmp_path: Path,
    mode: str,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    service = TrajectoryHttpService(
        _settings(database_path),
        reader=AsyncTrajectoryReader(database_path),
        metadata_loader=_metadata_loader(mode),
    )

    response = await service.list_traces("session-1", limit=30, cursor=None)

    assert response.status_code == 403
    assert _response_json(response)["code"] == "UNSUPPORTED_SESSION_MODE"
    test_logger.info("legacy single-Agent mode rejected: %s", mode)


@pytest.mark.asyncio
async def test_http_fails_closed_for_unknown_or_missing_session_mode(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    unknown = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader("future.mode"),
    )
    missing = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=lambda _session_id: {"session_id": "session-1"},
    )

    unknown_response = await unknown.list_traces("session-1", limit=30, cursor=None)
    missing_response = await missing.list_traces("session-1", limit=30, cursor=None)

    assert unknown_response.status_code == 403
    assert missing_response.status_code == 403
    assert _response_json(unknown_response)["code"] == "UNSUPPORTED_SESSION_MODE"
    assert _response_json(missing_response)["code"] == "UNSUPPORTED_SESSION_MODE"
    test_logger.info("unknown session modes failed closed")


@pytest.mark.asyncio
async def test_http_prevents_cross_session_raw_access(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path, session_id="session-1")
    service = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader(),
    )

    response = await service.get_raw_record("session-2", _TRACE_ID, _SPAN_ID)

    assert response.status_code == 404
    assert _response_json(response)["code"] == "NOT_FOUND"
    test_logger.info("raw identity cannot cross the path session")


@pytest.mark.asyncio
async def test_http_reports_disabled_and_invalid_session(tmp_path: Path) -> None:
    disabled = TrajectoryHttpService(
        _settings(tmp_path / "trajectory.sqlite3", enabled=False),
        metadata_loader=_metadata_loader(),
    )
    enabled = TrajectoryHttpService(
        _settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )

    disabled_response = await disabled.list_traces("session-1", limit=30, cursor=None)
    invalid_response = await enabled.list_traces("../session", limit=30, cursor=None)

    assert disabled_response.status_code == 503
    assert disabled_response.headers["cache-control"] == "no-store"
    assert _response_json(disabled_response)["code"] == "TRAJECTORY_DISABLED"
    assert invalid_response.status_code == 400
    assert invalid_response.headers["cache-control"] == "no-store"
    assert _response_json(invalid_response)["code"] == "BAD_REQUEST"
    test_logger.info("disabled and invalid-session states are explicit")


@pytest.mark.asyncio
async def test_http_follows_a_runtime_settings_toggle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "sessions"
    session_path = session_database_path(store_root, "session-1")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(session_path, session_id="session-1")
    live = {"settings": _settings(store_root, enabled=False)}
    monkeypatch.setattr(
        trajectory_http,
        "load_trajectory_store_settings",
        lambda: live["settings"],
    )
    # No pinned snapshot: this is how the gateway mounts the service.
    service = TrajectoryHttpService(metadata_loader=_metadata_loader())

    disabled_response = await service.list_traces("session-1", limit=30, cursor=None)
    live["settings"] = _settings(store_root)
    enabled_response = await service.list_traces("session-1", limit=30, cursor=None)

    assert disabled_response.status_code == 503
    assert _response_json(disabled_response)["code"] == "TRAJECTORY_DISABLED"
    assert enabled_response.status_code == 200
    assert len(_response_json(enabled_response)["items"]) == 1
    test_logger.info("a mounted service follows the runtime trajectory toggle")


@pytest.mark.asyncio
async def test_http_empty_database_returns_an_empty_list(tmp_path: Path) -> None:
    database_path = tmp_path / "not-created" / "trajectory.sqlite3"
    service = TrajectoryHttpService(
        _settings(database_path),
        metadata_loader=_metadata_loader(),
    )

    response = await service.list_traces("session-1", limit=30, cursor=None)
    payload = _response_json(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["items"] == []
    assert payload["next_cursor"] is None
    assert database_path.exists() is False
    test_logger.info("missing trajectory database remained a successful empty state")


def test_attach_trajectory_routes_registers_all_paths(tmp_path: Path) -> None:
    app = FastAPI()
    channel = SimpleNamespace()
    attach_trajectory_routes(
        app,
        channel,
        settings=_settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )
    paths = {getattr(route, "path", None) for route in app.router.routes}

    assert f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/traces" in paths
    assert f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/revisions" in paths
    assert f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/archive" in paths
    assert f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/traces/{{trace_id}}" in paths
    assert (
        f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/traces/{{trace_id}}/spans/{{span_id}}/raw"
        in paths
    )
    test_logger.info("trajectory routes mounted on the WebChannel FastAPI app")


@pytest.mark.asyncio
async def test_archive_route_is_export_only_and_cannot_import_into_sqlite(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _seed(database_path)
    app = FastAPI()
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(database_path),
        metadata_loader=_metadata_loader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/archive",
            json={
                "format": "openjiuwen.trajectory.archive",
                "archive_version": 1,
                "records": [{"record_id": "attacker:record"}],
            },
        )

    with sqlite3.connect(str(database_path)) as connection:
        current_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM trajectory_current_records"
            ).fetchone()[0]
        )
        raw_count = int(
            connection.execute("SELECT COUNT(*) FROM otlp_span_records").fetchone()[0]
        )
    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert current_count == 1
    assert raw_count == 1
    test_logger.info("archive API exposed no import mutation path")


@pytest.mark.asyncio
async def test_route_query_validation_keeps_no_store_header(tmp_path: Path) -> None:
    app = FastAPI()
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_response = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces?limit=invalid",
        )
        detail_response = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces/{_TRACE_ID}"
            "?since_revision=invalid",
        )
        huge_limit_response = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            params={"limit": "9" * 5000},
        )
        huge_revision_response = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces/{_TRACE_ID}",
            params={"since_revision": "9" * 5000},
        )

    assert list_response.status_code == 400
    assert list_response.headers["cache-control"] == "no-store"
    assert detail_response.status_code == 400
    assert detail_response.headers["cache-control"] == "no-store"
    assert huge_limit_response.status_code == 400
    assert huge_limit_response.headers["cache-control"] == "no-store"
    assert huge_limit_response.json() == {
        "error": "limit must be an integer",
        "code": "BAD_REQUEST",
    }
    assert huge_revision_response.status_code == 400
    assert huge_revision_response.headers["cache-control"] == "no-store"
    assert huge_revision_response.json() == {
        "error": "since_revision must be an integer",
        "code": "BAD_REQUEST",
    }
    test_logger.info("route-level query errors used the trajectory error envelope")


@pytest.mark.asyncio
async def test_framework_trajectory_errors_are_json_and_non_cacheable(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )

    @app.get(f"{TRAJECTORY_API_PREFIX}/validation-probe")
    async def validation_probe(required: int):
        return {"required": required}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        not_found = await client.get(f"{TRAJECTORY_API_PREFIX}/missing")
        method_not_allowed = await client.post(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces"
        )
        validation_error = await client.get(
            f"{TRAJECTORY_API_PREFIX}/validation-probe"
        )
        trace_not_found = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces/{_TRACE_ID}"
        )

    assert not_found.status_code == 404
    assert not_found.headers["cache-control"] == "no-store"
    assert not_found.json() == {
        "error": "trajectory route not found",
        "code": "NOT_FOUND",
    }
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.headers["cache-control"] == "no-store"
    assert method_not_allowed.json() == {
        "error": "trajectory method not allowed",
        "code": "METHOD_NOT_ALLOWED",
    }
    assert validation_error.status_code == 400
    assert validation_error.headers["cache-control"] == "no-store"
    assert validation_error.json() == {
        "error": "invalid trajectory request",
        "code": "BAD_REQUEST",
    }
    assert trace_not_found.status_code == 404
    assert trace_not_found.headers["cache-control"] == "no-store"
    assert trace_not_found.json() == {
        "error": "trace not found",
        "code": "NOT_FOUND",
    }
    test_logger.info("framework 404, 405, and 422 responses used the HTTP envelope")


@pytest.mark.asyncio
async def test_routes_reuse_webchannel_origin_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_ENABLE_ORIGIN_CHECK", "1")
    monkeypatch.setenv("JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS", "trusted.example")
    app = FastAPI()
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={"Origin": "https://evil.example"},
        )
        allowed = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={"Origin": "https://trusted.example"},
        )
        same_origin_without_origin = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={
                "Host": "trusted.example",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        spoofed_host_without_origin = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={
                "Host": "evil.example",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Host": "trusted.example",
            },
        )
        cross_site_without_origin = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={
                "Host": "trusted.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        cross_site_with_allowed_origin = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={
                "Host": "trusted.example",
                "Origin": "https://trusted.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        allowed_referer_without_origin = await client.get(
            f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces",
            headers={
                "Host": "trusted.example",
                "Referer": "https://trusted.example/app",
            },
        )

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "FORBIDDEN_ORIGIN"
    assert allowed.status_code == 200
    assert same_origin_without_origin.status_code == 200
    assert spoofed_host_without_origin.status_code == 403
    assert cross_site_without_origin.status_code == 403
    assert cross_site_with_allowed_origin.status_code == 403
    assert allowed_referer_without_origin.status_code == 200
    for response in (
        rejected,
        allowed,
        same_origin_without_origin,
        spoofed_host_without_origin,
        cross_site_without_origin,
        cross_site_with_allowed_origin,
        allowed_referer_without_origin,
    ):
        assert response.headers["cache-control"] == "no-store"
    test_logger.info("trajectory HTTP applied the configured browser Origin allowlist")


@pytest.mark.asyncio
async def test_http_internal_failures_return_stable_generic_messages(tmp_path: Path) -> None:
    class _FailingReader:
        async def list_traces_with_revision_cursor(self, *args, **kwargs):
            raise ValueError("secret sqlite path /private/trajectory.sqlite3")

        async def list_trace_revisions(self, *args, **kwargs):
            raise ValueError("secret revision query text")

        async def get_session_archive_records(self, *args, **kwargs):
            raise ValueError("secret archive query text")

        async def get_trace_records(self, *args, **kwargs):
            raise ValueError("secret detail query text")

        async def get_raw_record(self, *args, **kwargs):
            raise ValueError("secret raw query text")

    query_service = TrajectoryHttpService(
        _settings(tmp_path / "trajectory.sqlite3"),
        reader=_FailingReader(),
        metadata_loader=_metadata_loader(),
    )
    metadata_service = TrajectoryHttpService(
        _settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=lambda _session_id: (_ for _ in ()).throw(
            RuntimeError("secret metadata path")
        ),
    )

    query_response = await query_service.list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )
    valid_revision_cursor = _response_json(
        await TrajectoryHttpService(
            _settings(tmp_path / "baseline.sqlite3"),
            metadata_loader=_metadata_loader(),
        ).list_traces("session-1", limit=30, cursor=None)
    )["revision_cursor"]
    revision_response = await query_service.list_revisions(
        "session-1",
        after_revision=valid_revision_cursor,
        limit=100,
    )
    archive_response = await query_service.export_archive("session-1")
    detail_response = await query_service.get_trace(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
    )
    raw_response = await query_service.get_raw_record(
        "session-1",
        _TRACE_ID,
        _SPAN_ID,
    )
    metadata_response = await metadata_service.list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )

    for response in (
        query_response,
        revision_response,
        archive_response,
        detail_response,
        raw_response,
    ):
        assert response.status_code == 500
        assert response.headers["cache-control"] == "no-store"
        assert _response_json(response) == {
            "error": "trajectory query failed",
            "code": "TRAJECTORY_QUERY_FAILED",
        }
    assert metadata_response.status_code == 500
    assert _response_json(metadata_response) == {
        "error": "session lookup failed",
        "code": "SESSION_LOOKUP_FAILED",
    }
    test_logger.info("internal exception details remained server-side")


@pytest.mark.asyncio
async def test_http_rejects_noncanonical_and_oversized_cursors(tmp_path: Path) -> None:
    service = TrajectoryHttpService(
        _settings(tmp_path / "trajectory.sqlite3"),
        metadata_loader=_metadata_loader(),
    )

    responses = [
        await service.list_traces("session-1", limit=30, cursor="e30!!!"),
        await service.list_traces("session-1", limit=30, cursor="e30="),
        await service.list_traces("session-1", limit=30, cursor="A" * 513),
        await service.list_traces("session-1", limit=30, cursor="e30"),
        await service.list_revisions(
            "session-1",
            after_revision="e30!!!",
            limit=100,
        ),
        await service.list_revisions(
            "session-1",
            after_revision="e30",
            limit=100,
        ),
    ]

    for response in responses:
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
        assert _response_json(response)["code"] == "BAD_REQUEST"
    test_logger.info("noncanonical cursor text was rejected before SQLite decoding")


def test_web_proxy_preserves_canonical_outer_host_without_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        status = 200
        reason = "OK"

        @staticmethod
        def read() -> bytes:
            return b'{"ok":true}'

        @staticmethod
        def getheaders() -> list[tuple[str, str]]:
            return [("Content-Type", "application/json"), ("Connection", "close")]

    class _FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            captured["target"] = (host, port, timeout)
            captured["closed"] = False

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            captured["request"] = (method, path, body, headers)

        @staticmethod
        def getresponse() -> _FakeResponse:
            return _FakeResponse()

        @staticmethod
        def close() -> None:
            captured["closed"] = True

    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConnection)
    handler = object.__new__(_SpaStaticHandler)
    handler.headers = http.client.HTTPMessage()
    handler.headers.add_header("Host", "Trusted.Example.:8443")
    handler.headers.add_header("Sec-Fetch-Site", "same-origin")
    handler.headers.add_header("Forwarded", "host=evil.example")
    handler.headers.add_header("X-Forwarded-Host", "evil.example")
    handler.headers.add_header("X-Original-Host", "evil.example")
    handler.headers.add_header("X-JiuwenSwarm-Original-Host", "evil.example")
    handler.headers.add_header("Connection", "keep-alive")
    handler.command = "GET"
    handler.path = f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces"
    handler.api_target = "http://127.0.0.1:19090"
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    sent_status: list[tuple[int, str | None]] = []
    sent_headers: list[tuple[str, str]] = []
    handler.send_response = lambda status, reason=None: sent_status.append((status, reason))
    handler.send_header = lambda key, value: sent_headers.append((key, value))
    handler.end_headers = lambda: None
    handler.log_error = lambda *_args: None

    handler._proxy_http()

    assert captured["target"][:2] == ("127.0.0.1", 19090)
    method, path, body, forwarded = captured["request"]
    assert (method, path, body) == ("GET", handler.path, b"")
    assert forwarded["Host"] == "trusted.example:8443"
    assert forwarded["Sec-Fetch-Site"] == "same-origin"
    for untrusted_header in (
        "Forwarded",
        "X-Forwarded-Host",
        "X-Original-Host",
        "X-JiuwenSwarm-Original-Host",
        "Connection",
    ):
        assert untrusted_header not in forwarded
    assert captured["closed"] is True
    assert sent_status == [(200, "OK")]
    assert ("Connection", "close") not in sent_headers
    assert handler.wfile.getvalue() == b'{"ok":true}'

    invalid_handler = object.__new__(_SpaStaticHandler)
    invalid_handler.headers = http.client.HTTPMessage()
    invalid_handler.headers.add_header("Host", "trusted.example")
    invalid_handler.headers.add_header("Host", "evil.example")
    assert invalid_handler._clean_outer_host() is None


def test_real_web_proxy_preserves_outer_host_and_guards_all_trajectory_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_ENABLE_ORIGIN_CHECK", "1")
    monkeypatch.setenv("JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS", "trusted.example")
    database_root = tmp_path / "sessions"
    database_path = session_database_path(database_root, "session-1")
    expected_raw = _seed(database_path)
    captured_headers: list[dict[str, str | None]] = []
    app = FastAPI()

    @app.middleware("http")
    async def capture_proxy_headers(request, call_next):
        captured_headers.append(
            {
                "host": request.headers.get("host"),
                "forwarded": request.headers.get("forwarded"),
                "x-forwarded-host": request.headers.get("x-forwarded-host"),
                "x-original-host": request.headers.get("x-original-host"),
                "x-jiuwenswarm-original-host": request.headers.get(
                    "x-jiuwenswarm-original-host"
                ),
            }
        )
        return await call_next(request)

    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=_settings(database_root),
        metadata_loader=_metadata_loader(),
    )

    with _serve_asgi(app) as gateway_port:
        with _serve_web_proxy(gateway_port, tmp_path) as proxy_port:
            list_path = f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces"
            status, headers, body = _proxy_get(
                proxy_port,
                list_path,
                headers={
                    "Host": "trusted.example",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            assert status == 200
            assert headers["cache-control"] == "no-store"
            revision_cursor = json.loads(body)["revision_cursor"]
            paths = (
                list_path,
                f"{TRAJECTORY_API_PREFIX}/sessions/session-1/revisions"
                f"?after_revision={quote(revision_cursor, safe='')}",
                f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces/{_TRACE_ID}",
                f"{TRAJECTORY_API_PREFIX}/sessions/session-1/traces/{_TRACE_ID}"
                f"/spans/{_SPAN_ID}/raw",
            )
            for path in paths:
                allowed_status, allowed_headers, allowed_body = _proxy_get(
                    proxy_port,
                    path,
                    headers={
                        "Host": "trusted.example",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                assert allowed_status == 200
                assert allowed_headers["cache-control"] == "no-store"
                if path.endswith("/raw"):
                    assert allowed_body == expected_raw

                denied_host_status, denied_host_headers, _ = _proxy_get(
                    proxy_port,
                    path,
                    headers={
                        "Host": "evil.example",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                assert denied_host_status == 403
                assert denied_host_headers["cache-control"] == "no-store"

                denied_origin_status, denied_origin_headers, _ = _proxy_get(
                    proxy_port,
                    path,
                    headers={
                        "Host": "trusted.example",
                        "Origin": "https://evil.example",
                    },
                )
                assert denied_origin_status == 403
                assert denied_origin_headers["cache-control"] == "no-store"

                cross_site_status, cross_site_headers, _ = _proxy_get(
                    proxy_port,
                    path,
                    headers={
                        "Host": "trusted.example",
                        "Origin": "https://trusted.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                assert cross_site_status == 403
                assert cross_site_headers["cache-control"] == "no-store"

            spoofed_status, spoofed_headers, _ = _proxy_get(
                proxy_port,
                list_path,
                headers={
                    "Host": "evil.example",
                    "Sec-Fetch-Site": "same-origin",
                    "Forwarded": "host=trusted.example",
                    "X-Forwarded-Host": "trusted.example",
                    "X-Original-Host": "trusted.example",
                    "X-JiuwenSwarm-Original-Host": "trusted.example",
                },
            )

    assert spoofed_status == 403
    assert spoofed_headers["cache-control"] == "no-store"
    assert captured_headers[-1] == {
        "host": "evil.example",
        "forwarded": None,
        "x-forwarded-host": None,
        "x-original-host": None,
        "x-jiuwenswarm-original-host": None,
    }
    assert any(item["host"] == "trusted.example" for item in captured_headers)
    test_logger.info("real proxy chain guarded list, revision, detail, and raw reads")
