# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real package handlers surround the existing durable cross-session mailbox."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.events import Event, SessionContext
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import ValidationError

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.session_receive import SessionReceiveInput, SessionReceiveOrigin
from jiuwenswarm.extensions_v2.event_types.session_send import SessionSendInput
from jiuwenswarm.server.runtime.session.session_message_events import receive_invocation, send_invocation
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult, SessionMessageService, SessionMessageSource, SessionMessagingError,
)
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore

from .management.support import write_package


USER_CODE = '''import asyncio
from dataclasses import replace
import json
from pathlib import Path
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_send import SessionSendDenied
from jiuwenswarm.extensions_v2.event_types.session_receive import (
    SessionReceiveConsumed, SessionReceiveEvent, SessionReceiveOrigin, SessionReceiveResult,
)

TRACE = Path(__TRACE__)
RELEASE = Path(__RELEASE__)

class Extension:
    def __init__(self):
        self.tasks = set()

    def register(self, registrar):
        registrar.register_event_handler("session.send", self.send, {}, "user")
        registrar.register_event_handler("session.send", self.observe, {}, "user")
        registrar.register_event_handler("session.receive", self.receive, {"origin.kind": "peer-send-message"}, "user")
        registrar.register_event_handler("session.receive", self.observe, {}, "user")

    def record(self, **values):
        with TRACE.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(values) + "\\n")

    async def enter(self, event, ops):
        self.record(phase=event.event_type, session=ops.session_context.session_id,
                    text=event.event_input.text, cwd=await ops.session.cwd(),
                    root=await ops.session.root())

    async def observe(self, event, ops, next_handler):
        self.record(phase=event.event_type + ".observer", session=ops.session_context.session_id,
                    text=event.event_input.text)
        return await next_handler(event)

    async def send(self, event, ops, next_handler):
        await self.enter(event, ops)
        text = event.event_input.text
        if text.startswith("[deny-send]"):
            return SessionSendDenied("sender policy")
        if text.startswith("[bad-send-reason]"):
            return SessionSendDenied(" ")
        if text.startswith("[fake-send]"):
            return {"accepted": True, "message_id": "fake"}
        if text.startswith("[error-send]"):
            raise RuntimeError("user policy failed")
        if text.startswith("[bad-send-target]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, target_session_id="other")))
        if text.startswith("[bad-send-mode]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, input_mode="follow_up")))
        if text.startswith("[bad-send-text]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, text=" ")))
        if text.startswith("[bad-send-size]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, text="界" * 12000)))
        if text.startswith("[pause-send]"):
            while not RELEASE.exists():
                await asyncio.sleep(0.005)
        if text.startswith("[escape-send]"):
            async def late():
                while not RELEASE.exists():
                    await asyncio.sleep(0.005)
                try:
                    await next_handler(event)
                except Exception as exc:
                    self.record(phase="escaped", error=type(exc).__name__)
            task = asyncio.create_task(late())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return SessionSendDenied("no deferred delivery")
        if text.startswith("[swallow-send-cancel]"):
            try:
                return await next_handler(event)
            except asyncio.CancelledError:
                return SessionSendDenied("cannot undo a queued delivery")
        if text.startswith("[retry-after-failure]"):
            for attempt in range(2):
                try:
                    return await next_handler(event)
                except Exception:
                    pass
            return SessionSendDenied("cannot hide a failed CORE attempt")
        if text.startswith("[rewrite]"):
            event = Event(event.event_type, replace(event.event_input, text=text.replace("13800138000", "[phone]")))
        if text.startswith("[send-concurrent]"):
            return (await asyncio.gather(next_handler(event), next_handler(event)))[0]
        result = await next_handler(event)
        if text.startswith("[send-twice]"):
            return await next_handler(event)
        if text.startswith("[late-send-deny]"):
            return SessionSendDenied("too late")
        if text.startswith("[mutate-send-receipt]"):
            result["message_id"] = "forged"
        return result

    async def receive(self, event, ops, next_handler):
        await self.enter(event, ops)
        text = event.event_input.text
        if text.startswith("[consume]"):
            return SessionReceiveConsumed("receiver handled it")
        if text.startswith("[bad-receive-reason]"):
            return SessionReceiveConsumed(" ")
        if text.startswith("[fake-receive]"):
            return SessionReceiveResult("fake")
        if text.startswith("[error-receive]"):
            raise RuntimeError("user policy failed")
        if text.startswith("[bad-receive-origin]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, origin=SessionReceiveOrigin("bridge"))))
        if text.startswith("[bad-receive-event]"):
            external = SessionReceiveEvent("github", "pull_request.closed", {}, ())
            return await next_handler(Event(event.event_type, replace(event.event_input, event=external)))
        if text.startswith("[bad-receive-text]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, text="")))
        if text.startswith("[bad-receive-size]"):
            return await next_handler(Event(event.event_type, replace(event.event_input, text="界" * 12000)))
        if text.startswith("[pause-receive]"):
            while not RELEASE.exists():
                await asyncio.sleep(0.005)
        if text.startswith("[swallow-receive-cancel]"):
            try:
                return await next_handler(event)
            except asyncio.CancelledError:
                return SessionReceiveConsumed("cannot undo a queued delivery")
        if text.startswith("[rewrite]"):
            event = Event(event.event_type, replace(event.event_input, text=text + " [received]"))
        if text.startswith("[receive-concurrent]"):
            return (await asyncio.gather(next_handler(event), next_handler(event)))[0]
        result = await next_handler(event)
        if text.startswith("[receive-twice]"):
            return await next_handler(event)
        if text.startswith("[late-receive-consume]"):
            return SessionReceiveConsumed("too late")
        return result

def create_extension():
    return Extension()
'''


@pytest.fixture
async def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "packages")
    host = await assembly.install_extensions()
    assert host is not None
    trace = tmp_path / "trace.jsonl"
    release = tmp_path / "release"
    source = USER_CODE.replace("__TRACE__", repr(str(trace))).replace("__RELEASE__", repr(str(release)))
    package = write_package(tmp_path / "source", package_id="delivery.policy", module_source=source)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    metadata = {}
    for sid in ("source-1", "target-1", "target-2"):
        directory = tmp_path / sid
        directory.mkdir()
        metadata[sid] = {
            "session_id": sid, "title": sid, "user_id": "user-1", "channel_id": "web",
            "mode": "agent.code.normal", "project_dir": str(directory),
        }
    executed, notified = [], []
    finished = asyncio.Event()

    async def execute(record):
        executed.append(record)
        return SessionMessageExecutionResult(status="succeeded")

    async def notify(record):
        notified.append(record.status)
        if record.status == "succeeded":
            finished.set()

    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "mailbox.sqlite3"), admission=SessionRunAdmission(),
        execute=execute, status_callback=notify, available=False,
    )
    monkeypatch.setattr(service, "_session_metadata", lambda sid: metadata.get(sid, {}))
    value = SimpleNamespace(host=host, service=service, trace=trace, release=release,
                            metadata=metadata, executed=executed, notified=notified, finished=finished)
    try:
        yield value
    finally:
        release.touch()
        await service.stop()
        await assembly.shutdown_extensions()
        assert send_invocation() is None and receive_invocation() is None


def _trace(delivery):
    return [json.loads(line) for line in delivery.trace.read_text(encoding="utf-8").splitlines()] if delivery.trace.exists() else []


def _rows(delivery):
    return delivery.service.store.list_messages(owner_scope_id="user-1", session_id="source-1")


async def _send(delivery, text="hello", *, key="call-1", target="target-1", mode="steer"):
    return await delivery.service.send_message(
        SessionMessageSource("source-1", "request-1", key, key, user_id="user-1"),
        target_session_id=target, message=text, input_mode=mode,
    )


async def _wait_for(delivery, phase):
    async def wait():
        while not any(item["phase"] == phase for item in _trace(delivery)):
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 3)


async def test_real_business_send_runs_both_user_chains_and_binds_distinct_contexts(delivery):
    receipt = await _send(delivery, "[rewrite] phone=13800138000")
    row, = _rows(delivery)
    assert row.message_id == receipt["message_id"] and receipt["accepted"]
    assert row.content == "[rewrite] phone=[phone] [received]"
    assert row.source_session_id == "source-1" and row.target_session_id == "target-1"
    trace = _trace(delivery)
    assert [item["phase"] for item in trace] == ["session.send", "session.send.observer", "session.receive", "session.receive.observer"]
    assert [item["session"] for item in trace] == ["source-1", "source-1", "target-1", "target-1"]
    assert trace[0]["cwd"] == delivery.metadata["source-1"]["project_dir"]
    assert trace[2]["root"] == delivery.metadata["target-1"]["project_dir"]
    assert "13800138000" not in trace[2]["text"]


@pytest.mark.parametrize("text,code,last_phase", [
    ("[deny-send] sensitive", "SESSION_SEND_DENIED", "session.send"),
    ("[consume] duplicate", "SESSION_RECEIVE_CONSUMED", "session.receive"),
])
async def test_explicit_policy_short_circuit_does_not_queue_notify_or_execute(delivery, text, code, last_phase):
    result = await _send(delivery, text)
    assert result["accepted"] is False and result["code"] == code
    assert "message_id" not in result and _rows(delivery) == []
    assert not delivery.executed and not delivery.notified
    assert _trace(delivery)[-1]["phase"] == last_phase


@pytest.mark.parametrize("marker", [
    "bad-send-target", "bad-send-mode", "bad-send-text", "bad-send-size", "bad-send-reason",
    "bad-receive-origin", "bad-receive-event", "bad-receive-text", "bad-receive-size", "bad-receive-reason",
    "fake-send", "fake-receive", "error-send", "error-receive",
])
async def test_invalid_user_changes_and_fake_receipts_fall_back_to_the_real_original(delivery, marker):
    text = f"[{marker}] original"
    receipt = await _send(delivery, text)
    row, = _rows(delivery)
    assert receipt["accepted"] and receipt["message_id"] == row.message_id
    assert row.content == text and row.target_session_id == "target-1" and row.input_mode == "steer"


@pytest.mark.parametrize("marker", [
    "send-twice", "receive-twice", "send-concurrent", "receive-concurrent",
    "late-send-deny", "late-receive-consume", "mutate-send-receipt",
])
async def test_repeated_next_and_post_commit_changes_preserve_one_real_receipt(delivery, marker):
    receipt = await _send(delivery, f"[{marker}] once")
    row, = _rows(delivery)
    assert receipt["accepted"] and receipt["message_id"] == row.message_id
    assert row.content == f"[{marker}] once" and delivery.notified == ["queued"]


async def test_retry_preserves_existing_idempotency_and_rejects_conflicting_body(delivery):
    first = await _send(delivery)
    second = await _send(delivery)
    assert second["message_id"] == first["message_id"] and second["deduplicated"]
    assert len(_rows(delivery)) == 1
    with pytest.raises(SessionMessagingError, match="different") as error:
        await _send(delivery, "another body")
    assert error.value.code == "IDEMPOTENCY_CONFLICT" and len(_rows(delivery)) == 1


@pytest.mark.parametrize("marker,phase", [("pause-send", "session.send"), ("pause-receive", "session.receive")])
async def test_cancel_before_queue_leaves_no_row_and_releases_invocation(delivery, marker, phase):
    task = asyncio.create_task(_send(delivery, f"[{marker}] wait"))
    await _wait_for(delivery, phase)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _rows(delivery) == [] and not delivery.notified
    delivery.release.touch()
    assert (await _send(delivery, "fresh", key="fresh"))["accepted"]


@pytest.mark.parametrize("session", ["source-1", "target-1"])
async def test_owner_changed_during_user_hook_is_rejected_at_real_queue_boundary(delivery, session):
    task = asyncio.create_task(_send(delivery, "[pause-receive] wait"))
    await _wait_for(delivery, "session.receive")
    delivery.metadata[session]["user_id"] = "another-user"
    delivery.release.touch()
    with pytest.raises(SessionMessagingError) as error:
        await task
    assert error.value.code == "NOT_FOUND_OR_FORBIDDEN" and _rows(delivery) == []


async def test_source_authorization_fails_before_either_user_handler(delivery):
    delivery.metadata["target-1"]["user_id"] = "another-user"
    with pytest.raises(SessionMessagingError) as error:
        await _send(delivery)
    assert error.value.code == "NOT_FOUND_OR_FORBIDDEN" and _trace(delivery) == []


async def test_escaped_next_cannot_queue_after_its_invocation_ended(delivery):
    result = await _send(delivery, "[escape-send] deferred")
    assert result["accepted"] is False
    delivery.release.touch()
    await _wait_for(delivery, "escaped")
    assert _rows(delivery) == [] and not delivery.notified


async def test_real_inflight_use_blocks_uninstall_until_the_policy_returns(delivery):
    task = asyncio.create_task(_send(delivery, "[pause-receive] wait"))
    await _wait_for(delivery, "session.receive")
    blocked = await delivery.host.manager.uninstall("delivery.policy")
    assert blocked.status != "SUCCESS"
    delivery.release.touch()
    assert (await task)["accepted"]
    assert (await delivery.host.manager.uninstall("delivery.policy")).status == "SUCCESS"
    assert (await _send(delivery, "[deny-send] handler removed", key="new"))["accepted"]
    assert len(_rows(delivery)) == 2


@pytest.mark.parametrize("event_type", ["session.send", "session.receive"])
async def test_bare_trigger_cannot_forge_the_platform_business_entrance(delivery, event_type):
    value = SessionSendInput("target-1", "hello", "steer") if event_type == "session.send" else SessionReceiveInput(SessionReceiveOrigin("peer-send-message"), "hello")
    with pytest.raises(ValidationError) as error:
        await delivery.host.bus.trigger(Event(event_type, value), SessionContext("source-1"))
    assert error.value.status is StatusCode.EVENTS_PAYLOAD_INVALID
    assert _trace(delivery) == [] and _rows(delivery) == []


async def test_storage_failure_propagates_without_a_fake_success_or_retry(delivery, monkeypatch):
    attempts = []

    def broken_enqueue(**kwargs):
        attempts.append(kwargs)
        raise OSError("fixture storage failure")

    monkeypatch.setattr(delivery.service.store, "enqueue", broken_enqueue)
    with pytest.raises(SessionMessagingError) as error:
        await _send(delivery)
    assert error.value.code == "PERSISTENCE_UNAVAILABLE" and len(attempts) == 1


@pytest.mark.parametrize("marker", ["swallow-send-cancel", "swallow-receive-cancel"])
async def test_policy_cannot_turn_cancellation_after_durable_queue_into_a_refusal(delivery, marker):
    queued = asyncio.Event()

    async def hold_notification(record):
        if record.status == "queued":
            queued.set()
            await asyncio.Event().wait()

    delivery.service._status_callback = hold_notification
    task = asyncio.create_task(_send(delivery, f"[{marker}] queued"))
    await asyncio.wait_for(queued.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row, = _rows(delivery)
    assert row.content == f"[{marker}] queued"


async def test_user_retry_after_core_failure_does_not_repeat_the_store_operation(delivery, monkeypatch):
    attempts = []

    def broken_enqueue(**kwargs):
        attempts.append(kwargs)
        raise OSError("fixture storage failure")

    monkeypatch.setattr(delivery.service.store, "enqueue", broken_enqueue)
    with pytest.raises(SessionMessagingError) as error:
        await _send(delivery, "[retry-after-failure] broken")
    assert error.value.code == "PERSISTENCE_UNAVAILABLE" and len(attempts) == 1


async def test_nested_business_send_is_rejected_before_a_second_policy_chain(delivery, monkeypatch):
    original = delivery.service._enqueue_message

    async def nested(*args, **kwargs):
        return await _send(delivery, "nested", key="nested", target="target-2")

    monkeypatch.setattr(delivery.service, "_enqueue_message", nested)
    with pytest.raises(SessionMessagingError) as error:
        await _send(delivery)
    assert error.value.code == "SESSION_SEND_REENTRANT" and _rows(delivery) == []
    monkeypatch.setattr(delivery.service, "_enqueue_message", original)
    assert (await _send(delivery, "fresh", key="fresh"))["accepted"]


async def test_concurrent_business_calls_keep_their_own_destinations_and_receipts(delivery):
    results = await asyncio.gather(*(
        _send(delivery, f"item-{i}", key=f"call-{i}", target="target-1" if i % 2 else "target-2")
        for i in range(8)
    ))
    rows = {row.content: row for row in _rows(delivery)}
    assert len(rows) == 8 and len({result["message_id"] for result in results}) == 8
    for i, result in enumerate(results):
        row = rows[f"item-{i}"]
        assert row.message_id == result["message_id"] and row.target_session_id == result["target_session_id"]


async def test_accepted_body_reaches_the_existing_asynchronous_executor(delivery):
    await delivery.service.set_available(True)
    receipt = await _send(delivery, "[rewrite] 13800138000", mode="follow_up")
    await asyncio.wait_for(delivery.finished.wait(), 3)
    assert receipt["accepted"] and len(delivery.executed) == 1
    assert delivery.executed[0].content == "[rewrite] [phone] [received]"
    assert delivery.notified == ["queued", "running", "succeeded"]


async def test_disabled_extensions_keep_the_original_mailbox_path(delivery, monkeypatch):
    monkeypatch.setattr(assembly, "get_extension_bus", lambda: None)
    receipt = await _send(delivery, "[deny-send] no bus")
    assert receipt["accepted"] and _rows(delivery)[0].content == "[deny-send] no bus"
    assert _trace(delivery) == []
