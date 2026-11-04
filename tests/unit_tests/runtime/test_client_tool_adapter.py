# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Detached client tools: one end per call, failed calls never reach the agent nor run again by themselves."""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.runtime.client_tool_adapter import (
    ClientToolAdapter,
    ClientToolCallStatus,
    ClientToolDeliveryError,
    ClientToolError,
    ClientToolStateStore,
    DeliveryState,
    call_fingerprint,
    config_seconds,
    is_result_envelope,
)

TOOL = {"name": "read_page", "description": "Read one page", "input_schema": {
    "type": "object", "properties": {"page": {"type": "integer"}},
}}
AGENT_TOOL = "client__read_page"


class _Host:
    """Records what the adapter sends to clients and to the agent."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.delivered: list[tuple[str, str]] = []

    async def emit(self, session_id: str, payload: dict) -> None:
        self.events.append({"session_id": session_id, **payload})

    async def deliver(self, call, envelope: str) -> None:
        self.delivered.append((call.call_id, envelope))

    def of_type(self, event_type: str) -> list[dict]:
        return [event for event in self.events if event["event_type"] == event_type]


def _adapter(tmp_path, host: _Host, **kwargs) -> ClientToolAdapter:
    return ClientToolAdapter(
        store=ClientToolStateStore(lambda: tmp_path), emit=host.emit, deliver=host.deliver, **kwargs,
    )


async def _settle_background() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _opened(tmp_path, host: _Host, **kwargs) -> ClientToolAdapter:
    adapter = _adapter(tmp_path, host, **kwargs)
    await adapter.register("s1", "pane-1", [TOOL])
    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})
    return adapter


async def test_a_call_asks_its_client_and_stays_pending(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    [request] = host.of_type("chat.client_tool_dispatch")
    assert request["call_id"] == "call-1"
    assert request["mode"] == "detached"
    assert request["tool_name"] == "read_page"
    assert request["arguments"] == {"page": 1}
    assert request["target_client_id"] == "pane-1"
    assert [event["call_id"] for event in adapter.list_pending("s1")] == ["call-1"]
    assert host.delivered == []


async def test_opening_the_same_call_again_asks_the_client_once(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    await adapter.open_call("s1", "call-1", AGENT_TOOL, {"page": 1})

    assert len(host.of_type("chat.client_tool_dispatch")) == 1


async def test_a_completed_result_is_delivered_to_the_agent_once(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    call = await adapter.submit_result("s1", "call-1", {"status": "completed", "content": "page one"})
    await _settle_background()

    assert call.status is ClientToolCallStatus.COMPLETED
    [(call_id, envelope)] = host.delivered
    assert call_id == "call-1"
    assert is_result_envelope(envelope)
    assert "page one" in envelope and 'status="completed"' in envelope
    assert host.of_type("chat.client_tool_settled")[-1]["accepted"] is True
    with pytest.raises(ClientToolError) as refused:
        await adapter.submit_result("s1", "call-1", "again")
    assert refused.value.code == "CLIENT_TOOL_CALL_SETTLED"
    await _settle_background()
    assert len(host.delivered) == 1


async def test_an_error_the_client_reports_is_delivered_as_a_tool_error(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    call = await adapter.submit_result("s1", "call-1", {"is_error": True, "content": "page not found"})
    await _settle_background()

    assert call.status is ClientToolCallStatus.ERROR
    [(_, envelope)] = host.delivered
    assert 'status="error"' in envelope and "page not found" in envelope


@pytest.mark.parametrize("status", ["timeout", "cancelled", "client_gone"])
async def test_a_failure_the_client_reports_is_never_delivered(tmp_path, status: str) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    call = await adapter.submit_result("s1", "call-1", {"status": status, "content": "partial output"})
    await _settle_background()

    assert call.status.value == status
    assert host.delivered == []
    assert host.of_type("chat.client_tool_settled")[-1]["accepted"] is False


async def test_a_cancelled_call_drops_its_late_result(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    await adapter.cancel("s1", "call-1", reason="user closed the pane")
    with pytest.raises(ClientToolError) as late:
        await adapter.submit_result("s1", "call-1", "page one")
    await _settle_background()

    assert late.value.code == "CLIENT_TOOL_CALL_SETTLED"
    assert host.delivered == []
    assert host.of_type("chat.client_tool_settled")[-1]["status"] == "cancelled"
    assert adapter.list_pending("s1") == []


async def test_a_call_past_its_deadline_times_out_without_reaching_the_agent(tmp_path) -> None:
    host = _Host()
    await _opened(tmp_path, host)
    state_file = tmp_path / "s1" / "client_tools.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["calls"][0]["deadline"] = 0  # long past
    state_file.write_text(json.dumps(state), encoding="utf-8")

    # A restarted server picks the call up again and finds its deadline gone.
    restarted_host = _Host()
    restarted = _adapter(tmp_path, restarted_host)
    restarted.specs("s1")
    await asyncio.sleep(0.05)

    assert restarted_host.of_type("chat.client_tool_settled")[-1]["status"] == "timeout"
    with pytest.raises(ClientToolError):
        await restarted.submit_result("s1", "call-1", "page one")
    assert restarted_host.delivered == []


async def test_a_failed_call_is_not_repeated_until_the_user_speaks(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)
    await adapter.cancel("s1", "call-1")

    assert adapter.check_invocation("s1", AGENT_TOOL, {"page": 1}) is not None
    assert adapter.check_invocation("s1", AGENT_TOOL, {"page": 2}) is None
    assert "do not call it again" in (adapter.reminder("s1") or "")

    await adapter.note_user_input("s1")

    assert adapter.check_invocation("s1", AGENT_TOOL, {"page": 1}) is None


async def test_a_running_call_is_not_started_twice(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    refusal = adapter.check_invocation("s1", AGENT_TOOL, {"page": 1})

    assert refusal is not None and "call-1" in refusal
    assert "running on the client" in (adapter.reminder("s1") or "")


async def test_a_client_that_does_not_come_back_leaves_its_calls_gone(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host, disconnect_grace_s=0)

    adapter.on_client_detached("s1")
    await asyncio.sleep(0.05)

    assert host.of_type("chat.client_tool_settled")[-1]["status"] == "client_gone"
    assert adapter.check_invocation("s1", AGENT_TOOL, {"page": 1}) is not None
    assert host.delivered == []


async def test_a_client_that_comes_back_in_time_keeps_its_calls(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host, disconnect_grace_s=0.05)

    adapter.on_client_detached("s1")
    adapter.on_client_attached("s1", "pane-1")
    await asyncio.sleep(0.1)

    assert [event["call_id"] for event in adapter.list_pending("s1")] == ["call-1"]
    await adapter.aclose()


async def test_only_the_calls_own_client_answers_it(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    with pytest.raises(ClientToolError) as refused:
        await adapter.submit_result("s1", "call-1", "page one", client_id="other-pane")

    assert refused.value.code == "CLIENT_TOOL_NOT_OWNER"
    assert [event["call_id"] for event in adapter.list_pending("s1")] == ["call-1"]
    await adapter.aclose()


async def test_an_unknown_call_or_tool_is_refused(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    with pytest.raises(ClientToolError) as unknown_call:
        await adapter.submit_result("s1", "nope", "x")
    with pytest.raises(ClientToolError) as unknown_tool:
        await adapter.open_call("s1", "call-2", "client__missing", {})

    assert unknown_call.value.code == "CLIENT_TOOL_UNKNOWN_CALL"
    assert unknown_tool.value.code == "CLIENT_TOOL_UNKNOWN_TOOL"
    await adapter.aclose()


@pytest.mark.parametrize("tools", [
    [{"name": "has space"}],
    [{"name": "a"}, {"name": "a"}],
    [{"name": "a", "mode": "blocking"}],
    [{"name": "a", "input_schema": {"type": "string"}}],
    [{"name": "a", "timeout_s": "soon"}],
])
async def test_an_invalid_declaration_is_refused(tmp_path, tools: list) -> None:
    adapter = _adapter(tmp_path, _Host())

    with pytest.raises(ClientToolError) as refused:
        await adapter.register("s1", "pane-1", tools)

    assert refused.value.code == "CLIENT_TOOL_BAD_REQUEST"
    assert adapter.specs("s1") == []


async def test_a_redeclaration_can_replace_what_the_client_offered(tmp_path) -> None:
    adapter = _adapter(tmp_path, _Host())
    await adapter.register("s1", "pane-1", [{"name": "a"}, {"name": "b"}])
    await adapter.register("s1", "pane-2", [{"name": "c"}])

    await adapter.register("s1", "pane-1", [{"name": "b", "timeout_s": 99999}], replace_owned=True)

    specs = {spec.name: spec for spec in adapter.specs("s1")}
    assert set(specs) == {"b", "c"}
    assert specs["b"].timeout_s == 3600.0  # bounded


async def test_tools_and_pending_calls_survive_a_restart(tmp_path) -> None:
    host = _Host()
    await _opened(tmp_path, host)

    restarted = _adapter(tmp_path, _Host())

    assert [spec.tool_name for spec in restarted.specs("s1")] == [AGENT_TOOL]
    assert [event["call_id"] for event in restarted.list_pending("s1")] == ["call-1"]
    await restarted.aclose()


async def test_closing_a_session_cancels_its_calls_and_forgets_its_tools(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    await adapter.close_session("s1")

    assert host.of_type("chat.client_tool_settled")[-1]["status"] == "cancelled"
    assert not (tmp_path / "s1" / "client_tools.json").exists()
    assert adapter.specs("s1") == []


async def test_stopping_the_session_cancels_every_running_call(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)
    await adapter.open_call("s1", "call-2", AGENT_TOOL, {"page": 2})

    assert await adapter.cancel_session("s1", reason="stopped") == 2
    assert adapter.list_pending("s1") == []


def test_a_fingerprint_ignores_argument_order() -> None:
    assert call_fingerprint("t", {"a": 1, "b": 2}) == call_fingerprint("t", {"b": 2, "a": 1})
    assert call_fingerprint("t", {"a": 1}) != call_fingerprint("u", {"a": 1})


def test_only_a_delivered_result_reads_as_an_envelope() -> None:
    assert is_result_envelope('<client_tool_result call_id="c" tool="t" status="completed">\nx')
    assert not is_result_envelope("please read page 1")


@pytest.mark.parametrize("session_id", ["../outside", "a/b", "..", "", "x" * 300])
async def test_a_session_id_that_is_not_one_is_refused_before_touching_disk(tmp_path, session_id: str) -> None:
    sessions = tmp_path / "sessions"
    adapter = ClientToolAdapter(
        store=ClientToolStateStore(lambda: sessions), emit=_Host().emit, deliver=_Host().deliver,
    )

    with pytest.raises(ClientToolError) as refused:
        await adapter.register(session_id, "pane-1", [TOOL])

    assert refused.value.code == "CLIENT_TOOL_BAD_REQUEST"
    assert not any(tmp_path.rglob("client_tools.json"))


def test_the_store_never_builds_a_path_outside_the_sessions_directory(tmp_path) -> None:
    store = ClientToolStateStore(lambda: tmp_path / "sessions")

    with pytest.raises(ValueError):
        store.save("../outside", {})
    assert not (tmp_path / "outside").exists()


async def test_the_client_hears_that_its_result_reached_the_agent(tmp_path) -> None:
    host = _Host()
    adapter = await _opened(tmp_path, host)

    await adapter.submit_result("s1", "call-1", "page one")
    await _settle_background()

    [delivery] = host.of_type("chat.client_tool_delivery")
    assert delivery["call_id"] == "call-1" and delivery["delivered"] is True


async def test_the_client_hears_when_its_accepted_result_could_not_reach_the_agent(tmp_path) -> None:
    host = _Host()

    async def refuse(call, envelope) -> None:
        raise ClientToolDeliveryError("the session no longer takes client tool results")

    host.deliver = refuse
    adapter = await _opened(tmp_path, host)

    call = await adapter.submit_result("s1", "call-1", "page one")
    await _settle_background()

    assert host.of_type("chat.client_tool_settled")[-1]["accepted"] is True
    [delivery] = host.of_type("chat.client_tool_delivery")
    assert delivery["delivered"] is False
    assert "no longer takes" in delivery["reason"]
    assert call.delivery is DeliveryState.PENDING  # the settled record; the outcome came after
    restarted = _adapter(tmp_path, _Host())
    restarted.specs("s1")
    assert restarted._sessions["s1"].calls["call-1"].delivery is DeliveryState.FAILED


async def test_a_result_still_being_delivered_at_a_restart_is_reported_lost(tmp_path) -> None:
    host = _Host()
    blocked = asyncio.Event()

    async def hang(call, envelope) -> None:
        await blocked.wait()

    host.deliver = hang
    adapter = await _opened(tmp_path, host)
    await adapter.submit_result("s1", "call-1", "page one")
    await adapter.aclose()  # the process goes away mid-delivery

    restarted_host = _Host()
    restarted = _adapter(tmp_path, restarted_host)
    restarted.specs("s1")
    await _settle_background()

    [delivery] = restarted_host.of_type("chat.client_tool_delivery")
    assert delivery["delivered"] is False and "restarted" in delivery["reason"]


@pytest.mark.parametrize(("value", "expected"), [
    (None, 120.0), (45, 45.0), (2.5, 2.5), ("120", 120.0), (True, 120.0), (-1, 120.0), (float("nan"), 120.0),
])
def test_a_duration_setting_falls_back_to_its_default_when_it_is_not_one(value, expected: float) -> None:
    settings = {} if value is None else {"default_timeout_s": value}

    assert config_seconds(settings, "default_timeout_s", 120.0) == expected
    assert config_seconds("not a mapping", "default_timeout_s", 120.0) == 120.0
