# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Worker-side diagnosis host."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from tests.unit_tests.diagnosis.stub_engine import StubDiagnosisEngine
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import ModelInfo
from jiuwenswarm.diagnosis.service import DiagnosisService
from jiuwenswarm.diagnosis.store import DiagnosisStore
from jiuwenswarm.server.diagnosis import host as host_module
from jiuwenswarm.server.diagnosis.host import DiagnosisHost, is_session_busy
from tests.unit_tests.diagnosis.diagnosis_fixtures import (
    SESSION_ID,
    FakeReader,
    make_row,
    make_span,
)

_FULL = {"scope": {"kind": "full"}, "locale": "en"}


def _reader() -> FakeReader:
    first = make_span(1, 100, 200, operation="chat", output_text="plan", tool_calls=True)
    first["attributes"].append(
        {
            "key": "gen_ai.input.messages",
            "value": {
                "stringValue": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "fix the build"}]}]),
            },
        },
    )
    return FakeReader(
        {
            "main": [
                make_row([first]),
                make_row([make_span(2, 210, 300, tool_name="shell", tool_result="boom", status_code=2)]),
            ],
        },
    )


def _model(session_id: str, *, team_name: str | None = None) -> ResolvedDiagnosisModel:
    return ResolvedDiagnosisModel(info=ModelInfo(type="model", id="m"), resolved=None)


def _metadata(session_id: str) -> dict[str, str]:
    return {"session_id": session_id, "mode": "agent.work.normal", "team_name": ""} if session_id == SESSION_ID else {}


def _request(method: ReqMethod, **params: Any) -> AgentRequest:
    return AgentRequest(
        request_id="r-1",
        channel_id="web",
        session_id=SESSION_ID,
        req_method=method,
        params={"session_id": SESSION_ID, **params},
    )


class _Pushes:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> bool:
        self.messages.append(message)
        return True


def _host(tmp_path: Path, pushes: _Pushes, **overrides: Any) -> DiagnosisHost:
    async def trigger_ttse(session_id: str, signals: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
        return True, {"results": {item["id"]: {"result": "triggered"} for item in signals}}

    async def push(session_id: str, event: str, payload: dict[str, Any]) -> None:
        await pushes({"session_id": session_id, "event": event, "payload": payload})

    service = DiagnosisService(
        store=DiagnosisStore(tmp_path),
        reader_provider=_reader,
        push=push,
        is_busy=lambda _sid: False,
        metadata_loader=_metadata,
        engine=StubDiagnosisEngine(step_delay_s=0),
        model_resolver=_model,
        ttse_trigger=trigger_ttse,
        is_enabled=lambda: True,
    )
    return DiagnosisHost(get_agent_manager=lambda: None, send_push=pushes, service=service, **overrides)


async def _wait_finished(host: DiagnosisHost, diagnosis_id: str) -> dict[str, Any]:
    for _ in range(300):
        response = await host.handle(_request(ReqMethod.DIAGNOSIS_GET, diagnosis_id=diagnosis_id))
        if response.payload["status"] != "running" and host.service._running.get(SESSION_ID) is None:
            return response.payload
        await asyncio.sleep(0.01)
    raise AssertionError("diagnosis did not finish")


@pytest.mark.asyncio
async def test_run_get_list_ttse_trigger_and_delete_found_trip(tmp_path: Path) -> None:
    host = _host(tmp_path, _Pushes())

    started = await host.handle(_request(ReqMethod.DIAGNOSIS_RUN, request=_FULL))
    assert started.ok is True
    diagnosis_id = started.payload["diagnosis_id"]
    finished = await _wait_finished(host, diagnosis_id)
    listing = await host.handle(_request(ReqMethod.DIAGNOSIS_LIST))
    signal_id = next(item["id"] for item in finished["root_causes"] if item["category"].startswith("ttse_"))
    triggered = await host.handle(
        _request(ReqMethod.DIAGNOSIS_TTSE_TRIGGER, diagnosis_id=diagnosis_id, request={"root_cause_ids": [signal_id]}),
    )
    deleted = await host.handle(_request(ReqMethod.DIAGNOSIS_DELETE, diagnosis_id=diagnosis_id))
    missing = await host.handle(_request(ReqMethod.DIAGNOSIS_GET, diagnosis_id=diagnosis_id))

    assert finished["status"] == "completed"
    assert [item["diagnosis_id"] for item in listing.payload["items"]] == [diagnosis_id]
    assert triggered.payload["results"][signal_id]["result"] == "triggered"
    assert (deleted.ok, deleted.payload) == (True, {})
    assert (missing.ok, missing.payload["code"]) == (False, "NOT_FOUND")


@pytest.mark.asyncio
async def test_errors_become_error_payloads(tmp_path: Path) -> None:
    host = _host(tmp_path, _Pushes())

    not_object = await host.handle(_request(ReqMethod.DIAGNOSIS_RUN, request=["x"]))
    invalid = await host.handle(_request(ReqMethod.DIAGNOSIS_RUN, request={"scope": {"kind": "selection", "ranges": []}}))
    no_signals = await host.handle(
        _request(ReqMethod.DIAGNOSIS_TTSE_TRIGGER, diagnosis_id="dg_0123456789abcdef", request={"root_cause_ids": []}),
    )
    other_session = await host.handle(
        AgentRequest(
            request_id="r-2",
            channel_id="web",
            req_method=ReqMethod.DIAGNOSIS_LIST,
            params={"session_id": "session-2"},
        ),
    )
    unsupported = await host.handle(_request(ReqMethod.CHAT_SEND))

    assert [item.ok for item in (not_object, invalid, no_signals, other_session, unsupported)] == [False] * 5
    assert [item.payload["code"] for item in (not_object, invalid, no_signals)] == ["BAD_REQUEST"] * 3
    assert other_session.payload["code"] == "SESSION_NOT_FOUND"
    assert unsupported.payload["code"] == "BAD_REQUEST"
    assert set(not_object.payload) == {"error", "code"}


@pytest.mark.asyncio
async def test_session_id_falls_back_to_the_request_envelope(tmp_path: Path) -> None:
    host = _host(tmp_path, _Pushes())
    request = AgentRequest(
        request_id="r-3",
        channel_id="web",
        session_id=SESSION_ID,
        req_method=ReqMethod.DIAGNOSIS_LIST,
        params={},
    )

    response = await host.handle(request)

    assert (response.ok, response.payload) == (True, {"items": []})


@pytest.mark.asyncio
async def test_progress_and_status_are_pushed_with_the_event_name(tmp_path: Path) -> None:
    pushes = _Pushes()
    host = _host(tmp_path, pushes)
    # The service pushes through ``push``; the production closure is covered below.
    started = await host.handle(_request(ReqMethod.DIAGNOSIS_RUN, request=_FULL))
    await _wait_finished(host, started.payload["diagnosis_id"])

    events = [message["event"] for message in pushes.messages]
    assert events[-1] == "trajectory.diagnosis.status"
    assert "trajectory.diagnosis.progress" in events
    seqs = [message["payload"]["seq"] for message in pushes.messages]
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_production_wiring_pushes_to_the_gateway_and_triggers_ttse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[tuple[str, list[dict[str, Any]]]] = []

    async def fake_trigger(*, agent_manager: Any, channel_id: str, session_id: str, signals: Any) -> dict[str, Any]:
        forwarded.append((channel_id, signals))
        return {"ok": True, "payload": {"results": {item["id"]: {"result": "triggered"} for item in signals}}}

    monkeypatch.setattr(host_module, "DiagnosisStore", lambda: DiagnosisStore(tmp_path))
    monkeypatch.setattr(host_module, "_TrajectoryReaderProvider", lambda: _reader)
    monkeypatch.setattr(host_module, "_load_session_metadata", _metadata)
    monkeypatch.setattr("jiuwenswarm.server.diagnosis.ttse_trigger.trigger_ttse_induction", fake_trigger)
    monkeypatch.setattr("jiuwenswarm.observability.config.is_trajectory_diagnosis_enabled", lambda *_a, **_k: True)
    pushes = _Pushes()
    host = DiagnosisHost(get_agent_manager=lambda: None, send_push=pushes)
    service = host.service
    service._model_resolver = _model
    service._engine = StubDiagnosisEngine(step_delay_s=0)

    started = await host.handle(_request(ReqMethod.DIAGNOSIS_RUN, request=_FULL))
    finished = await _wait_finished(host, started.payload["diagnosis_id"])
    signal_id = next(item["id"] for item in finished["root_causes"] if item["category"].startswith("ttse_"))
    triggered = await host.handle(
        _request(
            ReqMethod.DIAGNOSIS_TTSE_TRIGGER,
            diagnosis_id=started.payload["diagnosis_id"],
            request={"root_cause_ids": [signal_id]},
        ),
    )

    assert triggered.payload["results"][signal_id]["result"] == "triggered"
    assert forwarded[0][0] == "web"
    assert forwarded[0][1][0]["id"] == signal_id
    last = pushes.messages[-1]
    assert (last["channel_id"], last["session_id"], last["is_complete"]) == ("web", SESSION_ID, False)
    assert last["payload"]["event_type"] == "trajectory.diagnosis.status"
    assert last["request_id"].startswith(f"diagnosis:{SESSION_ID}:")
    assert {message["request_id"] for message in pushes.messages}.__len__() == len(pushes.messages)


@pytest.mark.asyncio
async def test_shutdown_is_a_no_op_before_first_use_and_cancels_afterwards(tmp_path: Path) -> None:
    unused = DiagnosisHost(get_agent_manager=lambda: None, send_push=_Pushes())
    await unused.shutdown()

    host = _host(tmp_path, _Pushes())
    await host.shutdown()


def _agent(*, runtime: bool = True, active: bool = False, task_done: bool | None = None) -> Any:
    task = None if task_done is None else SimpleNamespace(done=lambda: task_done)
    return SimpleNamespace(
        has_session_runtime=lambda _sid: runtime,
        _adapter=SimpleNamespace(is_session_active=lambda _sid: active),
        _session_manager=SimpleNamespace(get_current_task=lambda _sid: task),
    )


def test_is_session_busy_checks_adapter_activity_and_running_tasks() -> None:
    def manager(*agents: Any) -> Any:
        return SimpleNamespace(agents={"web": dict(enumerate(agents))})

    assert is_session_busy(None, SESSION_ID) is False
    assert is_session_busy(manager(), SESSION_ID) is False
    assert is_session_busy(manager(_agent(active=True)), SESSION_ID) is True
    assert is_session_busy(manager(_agent(task_done=False)), SESSION_ID) is True
    assert is_session_busy(manager(_agent(task_done=True)), SESSION_ID) is False
    assert is_session_busy(manager(_agent(runtime=False, active=True)), SESSION_ID) is False
    assert is_session_busy(manager(_agent(runtime=False), _agent(active=True)), SESSION_ID) is True


def test_is_session_busy_treats_probe_failures_as_idle() -> None:
    def boom(_sid: str) -> bool:
        raise RuntimeError("probe failed")

    broken = SimpleNamespace(has_session_runtime=boom)

    assert is_session_busy(SimpleNamespace(agents={"web": {0: broken}}), SESSION_ID) is False
