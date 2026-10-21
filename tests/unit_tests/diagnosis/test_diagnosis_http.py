# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Gateway diagnosis proxy routes, wired end to end to the Worker host."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from tests.unit_tests.diagnosis.stub_engine import StubDiagnosisEngine
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import ERROR_STATUS_BY_CODE, ModelInfo
from jiuwenswarm.diagnosis.service import DiagnosisService
from jiuwenswarm.diagnosis.store import DiagnosisStore
from jiuwenswarm.gateway.channel_manager.web.diagnosis_http import attach_diagnosis_routes, error_status
from jiuwenswarm.gateway.channel_manager.web.trajectory_http import TRAJECTORY_API_PREFIX
from jiuwenswarm.server.diagnosis.host import DiagnosisHost
from tests.unit_tests.diagnosis.diagnosis_fixtures import (
    SESSION_ID,
    FakeReader,
    make_row,
    make_span,
)

_BASE = f"{TRAJECTORY_API_PREFIX}/sessions/{SESSION_ID}/diagnoses"
_FULL = {"scope": {"kind": "full"}, "locale": "en"}


def _first_span() -> dict[str, Any]:
    span = make_span(1, 100, 200, operation="chat", output_text="plan", tool_calls=True)
    span["attributes"].append(
        {
            "key": "gen_ai.input.messages",
            "value": {
                "stringValue": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "fix the build"}]}]),
            },
        },
    )
    return span


def _reader() -> FakeReader:
    return FakeReader(
        {
            "main": [
                make_row([_first_span()]),
                make_row([make_span(2, 210, 300, tool_name="shell", tool_result="boom", status_code=2)]),
            ],
        },
    )


class _Forward:
    async def __call__(self, session_id: str, signals: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
        return True, {"results": {item["id"]: {"result": "triggered"} for item in signals}}


async def _no_push(_message: dict[str, Any]) -> bool:
    return True


def _fetch_via(host: DiagnosisHost) -> Any:
    """Return a Gateway ``fetch`` that talks to the Worker host like ``fetch_agent_unary``."""

    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        request = AgentRequest(
            request_id="r-1",
            channel_id="web",
            session_id=session_id,
            req_method=method,
            params={"session_id": session_id, **params},
        )
        response = await host.handle(request)
        payload = dict(response.payload or {})
        if not response.ok:
            return False, {"error": str(payload.get("error")), "code": str(payload.get("code"))}
        return True, payload

    return fetch


def _app(fetch: Any) -> FastAPI:
    app = FastAPI()
    attach_diagnosis_routes(app, None, fetch=fetch)
    return app


def _build(tmp_path: Path, *, enabled: bool = True, mode: str = "agent.work.normal", busy: bool = False) -> FastAPI:
    async def push(session_id: str, event: str, payload: dict[str, Any]) -> None:
        return None

    def metadata(session_id: str) -> dict[str, str]:
        return {"session_id": session_id, "mode": mode, "team_name": ""} if session_id == SESSION_ID else {}

    service = DiagnosisService(
        store=DiagnosisStore(tmp_path),
        reader_provider=_reader,
        push=push,
        is_busy=lambda _sid: busy,
        metadata_loader=metadata,
        engine=StubDiagnosisEngine(step_delay_s=0),
        model_resolver=lambda session_id, *, team_name=None: ResolvedDiagnosisModel(
            info=ModelInfo(type="model", id="m"),
            resolved=None,
        ),
        ttse_trigger=_Forward(),
        is_enabled=lambda: enabled,
    )
    host = DiagnosisHost(get_agent_manager=lambda: None, send_push=_no_push, service=service)
    return _app(_fetch_via(host))


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_build(tmp_path)), base_url="http://test") as http:
        yield http


async def _wait_completed(client: httpx.AsyncClient, diagnosis_id: str) -> dict[str, Any]:
    for _ in range(300):
        body = (await client.get(f"{_BASE}/{diagnosis_id}")).json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("diagnosis did not finish")


def test_routes_are_registered_under_the_trajectory_prefix(tmp_path: Path) -> None:
    app = _build(tmp_path)
    paths = {getattr(route, "path", None) for route in app.router.routes}

    assert _BASE.replace(SESSION_ID, "{session_id}") in paths
    assert f"{_BASE.replace(SESSION_ID, '{session_id}')}/{{diagnosis_id}}/ttse" in paths
    assert f"{_BASE.replace(SESSION_ID, '{session_id}')}/{{diagnosis_id}}/cancel" in paths


@pytest.mark.asyncio
async def test_run_poll_trigger_ttse_and_delete(client: httpx.AsyncClient) -> None:
    started = await client.post(_BASE, json=_FULL)
    assert started.status_code == 202
    assert started.headers["cache-control"] == "no-store"
    record = started.json()
    assert record["status"] == "running"
    assert record["input"]["mode_profile"] == "agent.work"

    finished = await _wait_completed(client, record["diagnosis_id"])
    assert finished["status"] == "completed"
    assert finished["root_causes"]
    assert "ttse" not in finished["suggestions"]

    listing = (await client.get(_BASE)).json()
    assert [item["diagnosis_id"] for item in listing["items"]] == [record["diagnosis_id"]]
    assert listing["items"][0]["status"] == "completed"

    cause_id = next(item["id"] for item in finished["root_causes"] if item["category"].startswith("ttse_"))
    triggered = await client.post(f"{_BASE}/{record['diagnosis_id']}/ttse", json={"root_cause_ids": [cause_id]})
    assert triggered.status_code == 200
    assert triggered.json()["results"][cause_id]["result"] == "triggered"

    deleted = await client.delete(f"{_BASE}/{record['diagnosis_id']}")
    assert deleted.status_code == 204
    missing = await client.get(f"{_BASE}/{record['diagnosis_id']}")
    assert (missing.status_code, missing.json()["code"]) == (404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_validation_errors_use_the_contract_body(client: httpx.AsyncClient) -> None:
    not_json = await client.post(_BASE, content=b"not json", headers={"Content-Type": "application/json"})
    bad_scope = await client.post(_BASE, json={"scope": {"kind": "selection", "ranges": []}})
    bad_time = await client.post(
        _BASE,
        json={
            "scope": {
                "kind": "selection",
                "ranges": [{"subject_id": "main", "start_unix_nano": "9", "end_unix_nano": "1"}],
            },
        },
    )
    too_long = await client.post(_BASE, json={**_FULL, "user_note": "x" * 2001})
    no_causes = await client.post(f"{_BASE}/dg_0123456789abcdef/ttse", json={"root_cause_ids": []})

    for response in (not_json, bad_scope, bad_time, too_long, no_causes):
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert set(response.json()) == {"error", "code"}


@pytest.mark.asyncio
async def test_unknown_diagnosis_and_session_answer_404(client: httpx.AsyncClient) -> None:
    unknown = await client.get(f"{_BASE}/dg_ffffffffffffffff")
    malformed = await client.get(f"{_BASE}/not-an-id")
    cancel = await client.post(f"{_BASE}/dg_ffffffffffffffff/cancel")
    other_session = await client.get(f"{TRAJECTORY_API_PREFIX}/sessions/session-2/diagnoses")

    assert [item.status_code for item in (unknown, malformed, cancel, other_session)] == [404, 404, 404, 404]
    assert other_session.json()["code"] == "SESSION_NOT_FOUND"
    assert unknown.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_feature_flag_off_answers_403(tmp_path: Path) -> None:
    app = _build(tmp_path, enabled=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        responses = [await http.get(_BASE), await http.post(_BASE, json=_FULL)]

    assert [item.status_code for item in responses] == [403, 403]
    assert {item.json()["code"] for item in responses} == {"FEATURE_DISABLED"}


@pytest.mark.asyncio
async def test_unsupported_mode_answers_403(tmp_path: Path) -> None:
    app = _build(tmp_path, mode="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(_BASE, json=_FULL)

    assert (response.status_code, response.json()["code"]) == (403, "UNSUPPORTED_SESSION_MODE")


@pytest.mark.asyncio
async def test_full_scope_while_busy_answers_409(tmp_path: Path) -> None:
    app = _build(tmp_path, busy=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(_BASE, json=_FULL)

    assert (response.status_code, response.json()["code"]) == (409, "FULL_SCOPE_WHILE_BUSY")


@pytest.mark.asyncio
async def test_empty_selection_answers_422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        _BASE,
        json={
            "scope": {
                "kind": "selection",
                "ranges": [{"subject_id": "main", "start_unix_nano": "1", "end_unix_nano": "2"}],
            },
        },
    )

    assert (response.status_code, response.json()["code"]) == (422, "EMPTY_SCOPE")


@pytest.mark.asyncio
async def test_origin_gate_rejects_untrusted_origins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_ENABLE_ORIGIN_CHECK", "1")
    monkeypatch.setenv("JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS", "trusted.example")
    app = _build(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        rejected = await http.get(_BASE, headers={"Origin": "https://evil.example"})
        allowed = await http.get(_BASE, headers={"Origin": "https://trusted.example"})

    assert rejected.status_code == 403
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_ttse_before_completion_of_for_code_mode_answers_409(tmp_path: Path) -> None:
    app = _build(tmp_path, mode="agent.code.normal")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        record = (await http.post(_BASE, json=_FULL)).json()
        finished = await _wait_completed(http, record["diagnosis_id"])
        response = await http.post(
            f"{_BASE}/{record['diagnosis_id']}/ttse",
            json={"root_cause_ids": ["tt-1"]},
        )

    assert not [item for item in finished["root_causes"] if item["category"].startswith("ttse_")]
    assert (response.status_code, response.json()["code"]) == (409, "TTSE_NOT_SUPPORTED")


# ── proxy behaviour ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_forwards_method_session_and_body() -> None:
    calls: list[tuple[ReqMethod, str, dict[str, Any]]] = []

    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        calls.append((method, session_id, params))
        return True, {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(fetch)), base_url="http://test") as http:
        await http.post(_BASE, json=_FULL)
        await http.get(_BASE)
        await http.get(f"{_BASE}/dg_1")
        await http.post(f"{_BASE}/dg_1/cancel")
        deleted = await http.delete(f"{_BASE}/dg_1")
        await http.post(f"{_BASE}/dg_1/ttse", json={"root_cause_ids": ["a"]})

    assert deleted.status_code == 204
    assert calls == [
        (ReqMethod.DIAGNOSIS_RUN, SESSION_ID, {"request": _FULL}),
        (ReqMethod.DIAGNOSIS_LIST, SESSION_ID, {}),
        (ReqMethod.DIAGNOSIS_GET, SESSION_ID, {"diagnosis_id": "dg_1"}),
        (ReqMethod.DIAGNOSIS_CANCEL, SESSION_ID, {"diagnosis_id": "dg_1"}),
        (ReqMethod.DIAGNOSIS_DELETE, SESSION_ID, {"diagnosis_id": "dg_1"}),
        (ReqMethod.DIAGNOSIS_TTSE_TRIGGER, SESSION_ID, {"diagnosis_id": "dg_1", "request": {"root_cause_ids": ["a"]}}),
    ]


@pytest.mark.asyncio
async def test_proxy_rejects_bad_bodies_before_forwarding() -> None:
    calls: list[Any] = []

    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        calls.append(method)
        return True, {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(fetch)), base_url="http://test") as http:
        not_object = await http.post(_BASE, json=["x"])
        too_large = await http.post(_BASE, content=b"{" + b" " * (64 * 1024) + b"}")

    assert [item.status_code for item in (not_object, too_large)] == [400, 400]
    assert {item.json()["code"] for item in (not_object, too_large)} == {"BAD_REQUEST"}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("FEATURE_DISABLED", 403),
        ("DIAGNOSIS_ALREADY_RUNNING", 409),
        ("MODEL_UNAVAILABLE", 503),
        ("SERVICE_UNAVAILABLE", 503),
        ("RUNTIME_WARMING", 503),
        ("AGENT_SERVER_TIMEOUT", 504),
        ("SOMETHING_NEW", 500),
    ],
)
async def test_proxy_maps_error_codes_to_http_status(code: str, status: int) -> None:
    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        return False, {"error": "boom", "code": code}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(fetch)), base_url="http://test") as http:
        response = await http.get(_BASE)

    assert response.status_code == status
    assert response.json() == {"error": "boom", "code": code}


@pytest.mark.asyncio
async def test_proxy_answers_500_when_the_forward_raises() -> None:
    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        raise RuntimeError("socket closed")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(fetch)), base_url="http://test") as http:
        response = await http.get(_BASE)

    assert (response.status_code, response.json()["code"]) == (500, "INTERNAL_ERROR")


def test_every_service_error_code_has_a_status() -> None:
    root = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "diagnosis"
    codes: set[str] = set()
    for source in root.glob("*.py"):
        codes |= set(re.findall(r'DiagnosisHostError\(\s*"([A-Z_]+)"', source.read_text(encoding="utf-8")))

    assert codes
    assert codes <= set(ERROR_STATUS_BY_CODE)