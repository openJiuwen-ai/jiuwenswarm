"""AgentOS cron snapshot sync must be turn-only and owner-scoped.

A read-only stream (``history.get``) never runs cron tools, so refreshing the
AgentServer snapshot would only pay the per-owner project-admission RPC.  When
it does run, the Gateway must query just the requesting user's jobs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.message_handler import message_handler as mh_module
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _CronController:
    def __init__(self, jobs: list[dict] | None = None) -> None:
        self.jobs = jobs or []
        self.list_calls: list[str | None] = []

    async def list_jobs(self, user_id: str | None = None) -> list[dict]:
        self.list_calls.append(user_id)
        if user_id:
            return [job for job in self.jobs if job.get("user_id") == user_id]
        return list(self.jobs)


def _handler(controller: _CronController) -> MessageHandler:
    handler = object.__new__(MessageHandler)
    handler.agent_client = SimpleNamespace()
    handler._cron_controller = controller
    return handler


def _env(method: str) -> SimpleNamespace:
    return SimpleNamespace(
        method=method,
        request_id="req-1",
        user_id="alice",
        channel="web",
        session_id="sess-1",
        params={},
    )


def _route_to_agentos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )


@pytest.mark.asyncio
async def test_readonly_stream_skips_cron_snapshot(monkeypatch) -> None:
    controller = _CronController()
    handler = _handler(controller)
    _route_to_agentos(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(mh_module, "send_agent_request_with_timeout", send)

    await handler._sync_agentos_cron_jobs(_env("history.get"))

    assert controller.list_calls == []
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_turn_syncs_only_current_users_jobs(monkeypatch) -> None:
    controller = _CronController([
        {"id": "job_a", "user_id": "alice"},
        {"id": "job_b", "user_id": "bob"},
    ])
    handler = _handler(controller)
    _route_to_agentos(monkeypatch)
    captured: dict[str, object] = {}

    async def _send(_client, env, *, label: str) -> SimpleNamespace:
        captured["env"] = env
        return SimpleNamespace(ok=True, payload={})

    monkeypatch.setattr(mh_module, "send_agent_request_with_timeout", _send)

    await handler._sync_agentos_cron_jobs(_env("chat.send"))

    assert controller.list_calls == ["alice"]
    snapshot_env = captured["env"]
    assert [job["id"] for job in snapshot_env.params["jobs"]] == ["job_a"]
    assert snapshot_env.user_id == "alice"
