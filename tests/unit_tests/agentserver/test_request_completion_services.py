# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Relocated execution owners retain durable completion and user-turn contracts."""
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio

import pytest

from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
from jiuwenswarm.server.runtime.agent_adapter.request_completion import RequestCompletion, _await_history_receipt


def completion(monkeypatch, *, mailbox=False):
    receipt = Future()
    forks = SimpleNamespace(request_started=Mock(), turn_ended=Mock())
    monkeypatch.setattr(prompt_suggestion, "forks", forks)
    ports = SimpleNamespace(
        person_drove_turn=Mock(return_value=True),
        complete_history_request=Mock(return_value=receipt),
        session_message_owns_completion=lambda request: mailbox,
        await_history_receipt=_await_history_receipt,
    )
    request = SimpleNamespace(request_id="turn", params={})
    state = RequestCompletion(request, "session", is_team_mode=False, ports=ports)
    return state, ports, receipt, forks


async def test_stream_completion_waits_for_durable_receipt(monkeypatch):
    state, ports, receipt, forks = completion(monkeypatch)
    state.observe({"event_type": "chat.final", "content": "answer"})
    state.complete()
    waiter = asyncio.create_task(state.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    state.complete("cancelled")
    ports.complete_history_request.assert_called_once_with("session", "turn", terminal_status="success")
    receipt.set_result(None)
    await waiter
    forks.turn_ended.assert_called_once()


async def test_cancelled_wait_does_not_cancel_history_writer_receipt(monkeypatch):
    state, _, receipt, _ = completion(monkeypatch)
    state.complete()
    waiter = asyncio.create_task(state.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not receipt.cancelled()
    receipt.set_result(None)


def test_interaction_pause_and_error_preserve_status(monkeypatch):
    state, ports, _, forks = completion(monkeypatch)
    event = next(iter(prompt_suggestion.USER_INTERACTION_EVENTS))
    state.observe({"event_type": event})
    state.observe({"event_type": "chat.error", "error": "model failed"})
    state.complete()
    ports.complete_history_request.assert_called_once_with("session", "turn", terminal_status="error")
    assert forks.turn_ended.call_args.kwargs["waiting_for_user"] is True


def test_mailbox_retains_its_existing_completion_owner(monkeypatch):
    state, ports, _, forks = completion(monkeypatch, mailbox=True)
    state.complete("cancelled")
    ports.complete_history_request.assert_not_called()
    forks.turn_ended.assert_not_called()


def test_error_is_not_replaced_by_disconnect_after_a_final(monkeypatch):
    state, ports, _, _ = completion(monkeypatch)
    state.observe({"event_type": "chat.final", "content": "earlier round"})
    state.observe({"event_type": "chat.error", "error": "later round failed"})
    state.complete("cancelled")
    ports.complete_history_request.assert_called_once_with("session", "turn", terminal_status="error")
