# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team evolution monitor helpers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.agent_teams.schema.team import TeamRole

from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers
from jiuwenswarm.server.runtime.agent_adapter import team_helpers


def test_persist_team_history_event_keeps_human_spawn_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        team_helpers,
        "append_history_record",
        lambda **kwargs: persisted.append(kwargs),
    )

    team_helpers._persist_team_history_event(
        "web",
        "sess_werewolf",
        {
            "event_type": "team.member",
            "event": {
                "type": "team.member.spawned",
                "team_id": "werewolf",
                "member_id": "villager-human",
                "name": "人类玩家",
                "mode": "human",
                "status": "idle",
            },
        },
    )

    assert len(persisted) == 1
    assert persisted[0]["event_type"] == "team.member"
    assert persisted[0]["mode"] == "team"
    assert persisted[0]["extra"] == {
        "session_id": "sess_werewolf",
        "event": {
            "type": "team.member.spawned",
            "team_id": "werewolf",
            "member_id": "villager-human",
            "name": "人类玩家",
            "mode": "human",
            "status": "idle",
        },
    }


class _InactiveTeamRuntimeManagerMixin:
    """Provide the session-scoped runtime state API for inactive test managers."""

    def __init__(self) -> None:
        self._seen_team_events: dict[str, bool] = {}
        self._workflow_completed: dict[str, bool] = {}
        self._round_complete_emitted: dict[str, bool] = {}

    def mark_seen_team_events(self, session_id: str) -> None:
        self._seen_team_events[session_id] = True

    def has_seen_team_events(self, session_id: str) -> bool:
        return self._seen_team_events.get(session_id, False)

    def reset_seen_team_events(self, session_id: str) -> None:
        self._seen_team_events.pop(session_id, None)

    def claim_round_complete(self, session_id: str) -> bool:
        # Inactive runtime must never authorize a completion broadcast.
        return False

    def reset_round_complete(self, session_id: str) -> None:
        self._round_complete_emitted.pop(session_id, None)

    def mark_workflow_completed(self, session_id: str) -> None:
        self._workflow_completed[session_id] = True

    def is_workflow_completed(self, session_id: str) -> bool:
        return self._workflow_completed.get(session_id, False)

    def reset_workflow_completed(self, session_id: str) -> None:
        self._workflow_completed.pop(session_id, None)

    @staticmethod
    def is_runtime_active(session_id: str) -> bool:
        _ = session_id
        return False

    @staticmethod
    def is_runtime_pending(session_id: str) -> bool:
        _ = session_id
        return False

    @staticmethod
    def has_waiters(session_id: str) -> bool:
        return False

    @staticmethod
    def get_waiters(session_id: str) -> list[tuple[str, asyncio.Queue]]:
        return []

    @staticmethod
    def add_waiter(session_id: str, request_id: str, queue: asyncio.Queue) -> None:
        pass

    @staticmethod
    def remove_waiter(session_id: str, request_id: str) -> None:
        pass

    @staticmethod
    def broadcast_event(session_id: str, event: dict) -> None:
        pass

    @staticmethod
    def is_session_initialized(session_id: str) -> bool:
        return False

    @staticmethod
    def clear_session_initialized(session_id: str) -> None:
        pass

    @staticmethod
    def setdefault_cron_completion(session_id: str, default: dict) -> dict:
        return default

    @staticmethod
    def pop_cron_completion(session_id: str) -> dict | None:
        return None

    @staticmethod
    def get_cron_completion(session_id: str) -> dict | None:
        return None

    @staticmethod
    def get_team_evolution_watcher(session_id: str) -> asyncio.Task | None:
        return None

    @staticmethod
    def get_team_skill_rail(session_id: str):
        return None

    @staticmethod
    def get_monitor(session_id: str):
        return None

    @staticmethod
    def register_monitor(session_id: str, handler) -> None:
        pass

    @staticmethod
    def get_workflow_handler(session_id: str):
        return None

    @staticmethod
    def pop_workflow_handler(session_id: str):
        return None

    @staticmethod
    def register_workflow_handler(session_id: str, handler) -> None:
        pass

    @staticmethod
    def has_stream_task(session_id: str) -> bool:
        return False

    @staticmethod
    def commit_runtime_ready(session_id: str, team_name: str) -> None:
        pass

    @staticmethod
    async def attach_distributed_hooks_for_runner_runtime(session_id: str, *args, **kwargs) -> None:
        pass

    @staticmethod
    def register_team_evolution_watcher(session_id: str, task) -> None:
        pass

    @staticmethod
    def pop_team_evolution_watcher(session_id: str):
        return None

    @staticmethod
    def pop_stream_task(session_id: str) -> asyncio.Task | None:
        return None


class _CompletingFollowupRuntimeManagerMixin(_InactiveTeamRuntimeManagerMixin):
    """Finish follow-up waiters immediately in input-routing tests."""

    @staticmethod
    def add_waiter(session_id: str, request_id: str, queue: asyncio.Queue) -> None:
        queue.put_nowait({"event_type": "team.completed"})


class _FakeTransport:
    pushes: list[dict] = []

    def __init__(self):
        self.pushes = self.__class__.pushes

    async def send_push(self, payload: dict) -> None:
        self.pushes.append(payload)


class _FakeRail:
    def __init__(self, batches: list[list[object]], *, pending_first: bool = True):
        self._batches = list(batches)
        self._pending_first = pending_first
        self._drain_calls = 0
        self.drain_waits: list[bool] = []
        self.review_trigger = False

    async def drain_pending_approval_events(self, wait: bool = False, timeout: float | None = None):
        self._drain_calls += 1
        self.drain_waits.append(wait)
        if self._batches:
            return self._batches.pop(0)
        return []


class _FakeProgressOnlyRail(_FakeRail):
    def __init__(self):
        super().__init__(
            [
                [
                    SimpleNamespace(
                        type="llm_reasoning",
                        payload={
                            "request_id": "team_skill_evolve_timeout",
                            "content": "[Team Skill Evolution] progress",
                        },
                    )
                ],
            ],
            pending_first=False,
        )
        self.cleanup_calls = 0

    async def cleanup_background_tasks(self) -> None:
        self.cleanup_calls += 1


class _TeamHelpersTestApi:
    @staticmethod
    async def watch_team_evolution_and_push(
            channel_id: str | None,
            session_id: str,
            rail: object,
    ) -> None:
        watcher = getattr(team_helpers, "_watch_team_evolution_and_push")
        await watcher(channel_id, session_id, rail)

    @staticmethod
    def ensure_team_evolution_watcher(
            channel_id: str | None,
            session_id: str,
            *,
            source: str = "unknown",
    ) -> None:
        ensure_watcher = getattr(team_helpers, "ensure_team_evolution_watcher")
        ensure_watcher(channel_id, session_id, source=source)

    @staticmethod
    async def handle_team_slash_command(
            channel_id: str | None,
            session_id: str,
            query: str,
            **kwargs,
    ) -> dict[str, object] | None:
        handler = getattr(team_helpers, "_handle_team_slash_command")
        return await handler(channel_id, session_id, query, **kwargs)

    @staticmethod
    async def consume_stream_with_query(
            channel_id: str | None,
            session_id: str,
            spec: object,
            query: str,
            **kwargs,
    ) -> None:
        consumer = getattr(team_helpers, "_consume_stream_with_query")
        kwargs.setdefault("round_id", 1)
        await consumer(channel_id, session_id, spec, query, **kwargs)

    @staticmethod
    async def team_round_settled(channel_id: str | None, session_id: str) -> bool:
        predicate = getattr(team_helpers, "_team_round_settled")
        return await predicate(channel_id, session_id)

    @staticmethod
    async def consume_monitor_events(
            channel_id: str | None,
            session_id: str,
            monitor_handler: object,
    ) -> None:
        consumer = getattr(team_helpers, "_consume_monitor_events")
        await consumer(channel_id, session_id, monitor_handler)

    @staticmethod
    def extract_query_directives(query: str) -> tuple[str, bool, bool]:
        fn = getattr(team_helpers, "_extract_query_directives")
        return fn(query)

    @staticmethod
    async def consume_workflow_events(
            channel_id: str | None,
            session_id: str,
            workflow_handler: object,
    ) -> None:
        consumer = getattr(team_helpers, "_consume_workflow_events")
        await consumer(channel_id, session_id, workflow_handler)

    @staticmethod
    def seed_cron_team_waiter(
            waiter_key: tuple[str, str],
            request_id: str,
    ) -> None:
        """Seed a cron waiter on the shared _CronFakeManager.

        waiter_key is (channel_id, session_id); the waiter is registered by
        session_id (matching production TeamManager.get_waiters).
        """
        _channel_id, session_id = waiter_key
        _CronFakeManager._waiters[session_id] = [(request_id, asyncio.Queue())]
        _CronFakeManager._completion.clear()

    @staticmethod
    def try_finish_cron_team_stream(
            channel_id: str | None,
            session_id: str,
            event: dict[str, object],
    ) -> None:
        fn = getattr(team_helpers, "_try_finish_cron_team_stream")
        fn(channel_id, session_id, event)

    @staticmethod
    def clear_cron_team_waiter(waiter_key: tuple[str, str]) -> None:
        _channel_id, session_id = waiter_key
        _CronFakeManager._waiters.pop(session_id, None)
        _CronFakeManager._completion.pop(session_id, None)


class _CronFakeManager(_InactiveTeamRuntimeManagerMixin):
    """Fake TeamManager with waiter + completion state for cron team stream tests."""

    _waiters: dict[str, list[tuple[str, asyncio.Queue]]] = {}
    _completion: dict[str, dict] = {}
    _stream_tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def reset(cls) -> None:
        cls._waiters.clear()
        cls._completion.clear()
        cls._stream_tasks.clear()

    @classmethod
    def get_waiters(cls, session_id: str) -> list[tuple[str, asyncio.Queue]]:
        return cls._waiters.get(session_id, [])

    @classmethod
    def setdefault_cron_completion(cls, session_id: str, default: dict) -> dict:
        return cls._completion.setdefault(session_id, default)

    @classmethod
    def pop_cron_completion(cls, session_id: str) -> dict | None:
        return cls._completion.pop(session_id, None)

    @classmethod
    def get_cron_completion(cls, session_id: str) -> dict | None:
        return cls._completion.get(session_id)

    @classmethod
    def pop_stream_task(cls, session_id: str) -> asyncio.Task | None:
        return cls._stream_tasks.pop(session_id, None)


def _write_team_skill(tmp_path, name: str, *, records: list[dict] | None = None) -> str:
    skills_dir = tmp_path / "team-workspace" / "skills"
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "kind: swarm-skill\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": name,
                "version": "1.0.0",
                "updated_at": "2026-06-11T00:00:00Z",
                "entries": records or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(skills_dir)


def _write_regular_skill(tmp_path, name: str, *, records: list[dict] | None = None) -> str:
    skills_dir = tmp_path / "team-workspace" / "skills"
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": name,
                "version": "1.0.0",
                "updated_at": "2026-06-11T00:00:00Z",
                "entries": records or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(skills_dir)


def _evolution_record(content: str, *, score: float = 1.0) -> dict:
    return {
        "id": "ev_test",
        "source": "user_correction",
        "timestamp": "2026-06-11T00:00:00Z",
        "context": "test",
        "change": {
            "section": "Instructions",
            "action": "append",
            "content": content,
            "target": "body",
        },
        "applied": False,
        "score": score,
        "usage_stats": {
            "times_presented": 0,
            "times_used": 0,
            "times_positive": 0,
            "times_negative": 0,
        },
        "summary": content,
    }


@pytest.mark.anyio
async def test_team_evolution_monitor_pushes_status_with_real_request_id(monkeypatch):
    _FakeTransport.pushes = []
    approval_event = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_req1", "questions": [{"header": "x"}]},
    )
    reasoning_event = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "[Team Skill Evolution] started"},
    )
    rail = _FakeRail([[reasoning_event, approval_event]], pending_first=False)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda evt: {"event_type": "chat.reasoning", "content": evt.payload.get("content", "")},
    )

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-1", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    event_types = [push["payload"]["event_type"] for push in _FakeTransport.pushes]
    assert event_types == [
        "chat.evolution_status",
        "chat.ask_user_question",
        "chat.evolution_status",
    ]
    assert _FakeTransport.pushes[0]["request_id"] == "team_skill_evolve_req1"
    assert _FakeTransport.pushes[0]["payload"]["request_id"] == "team_skill_evolve_req1"
    assert _FakeTransport.pushes[2]["payload"]["status"] == "end"
    assert _FakeTransport.pushes[2]["payload"]["stage"] == "approval_required"
    assert rail.drain_waits
    assert set(rail.drain_waits) == {False}


@pytest.mark.anyio
async def test_team_evolution_monitor_waits_for_real_request_id(monkeypatch):
    _FakeTransport.pushes = []
    reasoning_event = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "[Team Skill Evolution] started"},
    )
    approval_event = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_real", "questions": [{"header": "x"}]},
    )
    rail = _FakeRail([[reasoning_event], [approval_event]], pending_first=False)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda evt: {"event_type": "chat.reasoning", "content": evt.payload.get("content", "")},
    )

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-1", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert [push["request_id"] for push in status_pushes] == [
        "team_skill_evolve_real",
        "team_skill_evolve_real",
    ]


@pytest.mark.anyio
async def test_team_evolution_monitor_starts_cycle_for_started_progress_without_request_id(
        monkeypatch,
):
    _FakeTransport.pushes = []
    progress_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "started"},
            "content": "[Team Skill Evolution] started",
        },
    )
    outcome_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "outcome", "status": "failed"},
            "content": "failed before approval",
        },
    )
    rail = _FakeRail([[progress_event], [outcome_event]], pending_first=False)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-progress", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []


@pytest.mark.anyio
async def test_team_evolution_monitor_maps_sdk_progress_stages(monkeypatch):
    _FakeTransport.pushes = []
    detecting_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "detecting_signals",
                "request_id": "team_skill_evolve_stages",
            },
            "request_id": "team_skill_evolve_stages",
            "content": "[Team Skill Evolution] detecting",
        },
    )
    generating_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "generating_updates",
                "request_id": "team_skill_evolve_stages",
            },
            "request_id": "team_skill_evolve_stages",
            "content": "[Team Skill Evolution] generating",
        },
    )
    outcome_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "outcome", "status": "completed"},
            "request_id": "team_skill_evolve_stages",
            "content": "done",
        },
    )
    rail = _FakeRail([[detecting_event], [generating_event], [outcome_event]], pending_first=False)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-stages", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == [
        "start",
        "end",
    ]
    assert [push["payload"]["stage"] for push in status_pushes] == [
        "generating",
        "completed",
    ]
    assert {push["request_id"] for push in status_pushes} == {"team_skill_evolve_stages"}


@pytest.mark.anyio
async def test_team_evolution_monitor_uses_meta_request_id_and_ends_on_cancelled(monkeypatch):
    _FakeTransport.pushes = []
    detecting_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "detecting_signals",
                "request_id": "team_skill_evolve_meta",
            },
            "content": "[Team Skill Evolution] detecting",
        },
    )
    generating_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "generating_updates",
                "request_id": "team_skill_evolve_meta",
            },
            "content": "[Team Skill Evolution] generating",
        },
    )
    cancelled_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "cancelled",
                "request_id": "team_skill_evolve_meta",
            },
            "content": "no actionable evolution signals detected",
        },
    )
    rail = _FakeRail(
        [[detecting_event], [generating_event], [cancelled_event]],
        pending_first=False,
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-meta", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == [
        "start",
        "end",
    ]
    assert [push["payload"]["stage"] for push in status_pushes] == [
        "generating",
        "hidden",
    ]
    assert {push["request_id"] for push in status_pushes} == {"team_skill_evolve_meta"}


@pytest.mark.anyio
async def test_team_evolution_monitor_filters_progress_by_request_id(monkeypatch):
    _FakeTransport.pushes = []
    request_a_detecting = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "detecting_signals",
                "request_id": "team_skill_evolve_a",
            },
            "content": "[Team Skill Evolution] detecting A",
        },
    )
    request_b_generating = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "generating_updates",
                "request_id": "team_skill_evolve_b",
            },
            "content": "[Team Skill Evolution] generating B",
        },
    )
    request_a_generating = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "generating_updates",
                "request_id": "team_skill_evolve_a",
            },
            "content": "[Team Skill Evolution] generating A",
        },
    )
    request_a_completed = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": "completed",
                "request_id": "team_skill_evolve_a",
            },
            "content": "done A",
        },
    )
    rail = _FakeRail(
        [[request_a_detecting], [request_a_generating, request_b_generating], [request_a_completed]],
        pending_first=False,
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-filter", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [(push["request_id"], push["payload"]["status"], push["payload"]["stage"]) for push in status_pushes] == [
        ("team_skill_evolve_a", "start", "generating"),
        ("team_skill_evolve_a", "end", "completed"),
    ]
    assert all("generating B" not in push["payload"].get("message", "") for push in status_pushes)


@pytest.mark.anyio
async def test_team_evolution_monitor_uses_delivery_context_metadata(monkeypatch):
    _FakeTransport.pushes = []
    approval_event = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_meta", "questions": [{"header": "x"}]},
    )
    rail = _FakeRail([[approval_event]], pending_first=False)
    recorded_calls: list[dict] = []

    def _fake_build_server_push_message(**kwargs):
        recorded_calls.append(dict(kwargs))
        message = dict(kwargs)
        message["channel_id"] = kwargs["fallback_channel_id"]
        message["metadata"] = {"route": "from-delivery-context"}
        return message

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(
        team_helpers,
        "build_server_push_message",
        _fake_build_server_push_message,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-meta", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert recorded_calls
    assert all(call["session_id"] == "sess-meta" for call in recorded_calls)
    assert all(call["fallback_channel_id"] == "web" for call in recorded_calls)
    assert _FakeTransport.pushes
    assert all(push["metadata"] == {"route": "from-delivery-context"} for push in _FakeTransport.pushes)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected_stage"),
    [
        ("completed", "completed"),
        ("failed", "hidden"),
        ("timed_out", "hidden"),
    ],
)
async def test_team_evolution_monitor_reads_terminal_outcome_from_host_events(
        monkeypatch,
        status: str,
        expected_stage: str,
):
    _FakeTransport.pushes = []
    outcome_event = SimpleNamespace(
        type="chat.evolution_status",
        payload={
            "_evolution_meta": {"event_kind": "outcome", "status": status},
            "request_id": "team_skill_evolve_outcome",
            "content": status,
        },
    )
    rail = _FakeRail([[outcome_event]], pending_first=True)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-outcome", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert {push["request_id"] for push in status_pushes} == {"team_skill_evolve_outcome"}
    assert status_pushes[-1]["payload"]["stage"] == expected_stage
    assert status_pushes[-1]["payload"]["message"] == status


@pytest.mark.anyio
async def test_team_evolution_monitor_maps_noop_progress_to_no_evolution_generated(
        monkeypatch,
):
    _FakeTransport.pushes = []
    progress_event = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
            "content": "No evolution signals detected",
        },
    )
    rail = _FakeRail([[progress_event]], pending_first=False)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-noop", rail)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert status_pushes[-1]["payload"]["stage"] == "no_evolution_no_signal"


@pytest.mark.anyio
async def test_team_evolution_monitor_uses_approval_request_id_without_provisional_start(monkeypatch):
    _FakeTransport.pushes = []
    approval_event = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_real", "questions": [{"header": "x"}]},
    )

    class _PendingThenApprovalRail:
        def __init__(self):
            self._drain_calls = 0
            self.review_trigger = False

        async def drain_pending_approval_events(self, wait: bool = False, timeout: float | None = None):
            assert wait is False
            self._drain_calls += 1
            if self._drain_calls == 1:
                return [approval_event]
            return []

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "parse_stream_chunk", lambda evt: None)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push(
            "web",
            "sess-rebind",
            _PendingThenApprovalRail(),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status_starts = []
    approval_pushes = []
    for push in _FakeTransport.pushes:
        event_type = push["payload"]["event_type"]
        if event_type == "chat.ask_user_question":
            approval_pushes.append(push)
        if (
                event_type == "chat.evolution_status"
                and push["payload"]["status"] == "start"
        ):
            status_starts.append(push)
    assert len(status_starts) == 1
    assert status_starts[0]["request_id"] == "team_skill_evolve_real"
    assert approval_pushes[0]["request_id"] == "team_skill_evolve_real"
    assert approval_pushes[0]["payload"]["request_id"] == "team_skill_evolve_real"


@pytest.mark.anyio
async def test_team_evolution_monitor_keeps_idle_listener_after_timeout(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeRail([], pending_first=True)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    task = asyncio.create_task(
        _TeamHelpersTestApi.watch_team_evolution_and_push(
            "web",
            "sess-idle",
            rail,
        )
    )
    await asyncio.sleep(0.03)

    assert task.done() is False
    assert _FakeTransport.pushes == []
    assert rail.drain_waits
    assert set(rail.drain_waits) == {False}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_team_evolution_monitor_times_out_after_idle_progress(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeProgressOnlyRail()

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    await _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-timeout", rail)

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert status_pushes[0]["request_id"] == "team_skill_evolve_timeout"
    assert status_pushes[-1]["payload"]["stage"] == "hidden"
    assert "timed out" in status_pushes[-1]["payload"]["message"]
    assert rail.cleanup_calls == 1


@pytest.mark.anyio
async def test_team_evolution_monitor_uses_sdk_timeout_before_legacy_fallback(monkeypatch):
    class _SdkTimeoutProgressRail(_FakeProgressOnlyRail):
        @property
        def evolution_total_timeout_secs(self) -> float:
            return 0.01

    _FakeTransport.pushes = []
    rail = _SdkTimeoutProgressRail()

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(team_helpers, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 100.0)
    monkeypatch.setattr(evolution_helpers, "TEAM_EVOLUTION_EVENT_TIMEOUT_GRACE_SEC", 0.001)

    await asyncio.wait_for(
        _TeamHelpersTestApi.watch_team_evolution_and_push("web", "sess-sdk-timeout", rail),
        timeout=0.2,
    )

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert status_pushes[-1]["payload"]["stage"] == "hidden"
    assert rail.cleanup_calls == 1


@pytest.mark.anyio
async def test_ensure_team_evolution_watcher_starts_without_reasoning_gate(monkeypatch):
    registered: dict[str, asyncio.Task] = {}

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def get_team_evolution_watcher(session_id: str):
            return None

        @staticmethod
        def get_team_skill_rail(session_id: str):
            return SimpleNamespace(review_trigger=False)

        @staticmethod
        def register_team_evolution_watcher(
                session_id: str,
                task: asyncio.Task,
        ) -> None:
            registered[session_id] = task

        @staticmethod
        def pop_team_evolution_watcher(session_id: str):
            return registered.pop(session_id, None)

    async def _fake_watch(channel_id, session_id, rail):
        await asyncio.sleep(3600)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_watch_team_evolution_and_push", _fake_watch)

    _TeamHelpersTestApi.ensure_team_evolution_watcher("web", "sess-2")

    watcher = registered["sess-2"]
    assert isinstance(watcher, asyncio.Task)
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher


@pytest.mark.anyio
async def test_ensure_team_evolution_watcher_defers_when_rail_missing(monkeypatch):
    deferred: list[str] = []

    class _FakeManager:
        @staticmethod
        def get_team_evolution_watcher(session_id: str):
            return None

        @staticmethod
        def get_team_skill_rail(session_id: str):
            return None

        @staticmethod
        def mark_team_evolution_watcher_deferred(session_id: str) -> None:
            deferred.append(session_id)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    _TeamHelpersTestApi.ensure_team_evolution_watcher("web", "sess-missing", source="runtime_ready")

    assert deferred == ["sess-missing"]


@pytest.mark.anyio
async def test_ensure_team_evolution_watcher_starts_when_rail_mounted(
        monkeypatch,
):
    """Mounted evolution rail enables passive scan; watcher starts regardless of review_trigger."""
    registered: dict[str, asyncio.Task] = {}

    class _Rail:
        review_trigger = False

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def get_team_evolution_watcher(session_id: str):
            return None

        @staticmethod
        def get_team_skill_rail(session_id: str):
            return _Rail()

        @staticmethod
        def register_team_evolution_watcher(
                session_id: str,
                task: asyncio.Task,
        ) -> None:
            registered[session_id] = task

        @staticmethod
        def pop_team_evolution_watcher(session_id: str):
            return registered.pop(session_id, None)

    async def _fake_watch(channel_id, session_id, rail):
        await asyncio.sleep(3600)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_watch_team_evolution_and_push", _fake_watch)

    _TeamHelpersTestApi.ensure_team_evolution_watcher("web", "sess-1")

    watcher = registered["sess-1"]
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher


@pytest.mark.anyio
async def test_consume_stream_with_query_launches_watcher_after_runtime_ready(monkeypatch):
    calls: list[str] = []

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def commit_runtime_ready(session_id: str, team_name: str) -> None:
            calls.append(f"commit:{session_id}:{team_name}")

        @staticmethod
        async def attach_distributed_hooks_for_runner_runtime(**kwargs) -> None:
            calls.append(
                f"hooks:{kwargs['session_id']}:{kwargs['team_name']}:{kwargs['channel_id']}"
            )

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            calls.append(f"clear:{session_id}")

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            calls.append(f"clear_active:{session_id}")

        @staticmethod
        def pop_stream_task(session_id: str):
            calls.append(f"pop:{session_id}")
            return None

        @staticmethod
        def resolve_team_agent(session_id: str):
            return None

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

        @staticmethod
        def register_workflow_handler(session_id: str, handler: object) -> None:
            calls.append(f"register_workflow_handler:{session_id}")

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(kind="ready")

    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _fake_stream)
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda chunk: {
            "event_type": "team.runtime_ready",
            "team_name": "ready-team",
            "activation_kind": "resume",
        },
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(
        team_helpers,
        "sync_team_identity_metadata",
        lambda **kwargs: calls.append(f"sync:{kwargs['session_id']}:{kwargs['ready_team_name']}"),
    )

    async def _fake_monitor(
            channel_id: str | None,
            session_id: str,
            team_name: str,
            hide_dm: bool = False,
            enable_swarmflow: bool = False,
    ) -> None:
        calls.append(f"monitor:{session_id}:{team_name}")

    monkeypatch.setattr(team_helpers, "ensure_monitor_handlers_for_active_runtime", _fake_monitor)
    monkeypatch.setattr(
        team_helpers,
        "ensure_team_evolution_watcher",
        lambda channel_id, session_id, *, source="unknown": calls.append(
            f"watcher:{session_id}:{source}"
        ),
    )

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-runtime",
        SimpleNamespace(team_name="spec-team"),
        "hello",
    )

    assert calls[:5] == [
        "sync:sess-runtime:ready-team",
        "commit:sess-runtime:ready-team",
        "hooks:sess-runtime:ready-team:web",
        "monitor:sess-runtime:ready-team",
        "watcher:sess-runtime:runtime_ready",
    ]
    assert calls[-3:] == [
        "clear:sess-runtime",
        "clear_active:sess-runtime",
        "pop:sess-runtime",
    ]


def test_sync_team_identity_metadata_persists_ready_team_for_any_activation(monkeypatch):
    updates: list[dict[str, object]] = []

    monkeypatch.setattr(team_helpers, "get_session_metadata", lambda session_id: {})
    monkeypatch.setattr(team_helpers, "update_session_metadata", lambda **kwargs: updates.append(kwargs))

    team_helpers.sync_team_identity_metadata(
        channel_id="web",
        session_id="sess-runtime",
        mode="team",
        ready_team_name="ready-team",
        activation_kind="resume",
    )

    assert updates == [
        {
            "session_id": "sess-runtime",
            "channel_id": "web",
            "mode": "team",
            "team_name": "ready-team",
        }
    ]


def test_sync_team_identity_metadata_keeps_existing_conflicting_team(monkeypatch):
    updates: list[dict[str, object]] = []

    monkeypatch.setattr(
        team_helpers,
        "get_session_metadata",
        lambda session_id: {"team_name": "existing-team"},
    )
    monkeypatch.setattr(team_helpers, "update_session_metadata", lambda **kwargs: updates.append(kwargs))

    team_helpers.sync_team_identity_metadata(
        channel_id="web",
        session_id="sess-runtime",
        mode="team",
        ready_team_name="ready-team",
        activation_kind="resume",
    )

    assert updates == []


@pytest.mark.anyio
async def test_consume_monitor_events_only_broadcasts_monitor_events(monkeypatch):
    broadcasted: list[dict[str, object]] = []
    event = {"event_type": "team.task", "event": {"type": "team.task.completed", "task_id": "task-1"}}

    class _FakeMonitorEventHandler:
        def __init__(self, events: list[dict[str, object]]):
            self._events = list(events)

        async def events(self):
            for item in self._events:
                yield item

    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *args: broadcasted.append(args[2]))

    monitor_handler = _FakeMonitorEventHandler([event])
    await _TeamHelpersTestApi.consume_monitor_events("web", "sess-monitor", monitor_handler)

    assert broadcasted == [event]


@pytest.mark.anyio
async def test_handle_team_slash_command_returns_team_evolve_list_summary(tmp_path):
    skills_dir = _write_team_skill(
        tmp_path,
        "demo-skill",
        records=[_evolution_record("Improve retry flow\nSecond line", score=0.88)],
    )

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-list",
        "/evolve_list demo-skill",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert 'Skill "demo-skill"' in result["output"]
    assert "Improve retry flow" in result["output"]


@pytest.mark.anyio
async def test_handle_team_slash_command_allows_evolve_rollback(monkeypatch, tmp_path):
    skills_dir = _write_team_skill(tmp_path, "demo-skill")

    async def _fake_handler(query: str, context: object) -> dict[str, object]:
        assert query == "/evolve_rollback demo-skill latest"
        assert getattr(context, "mode") == "team"
        assert getattr(context, "skills_dir") == skills_dir
        return {"output": "team rollback handled", "result_type": "answer"}

    monkeypatch.setattr(team_helpers, "handle_evolution_slash_command", _fake_handler)

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-rollback",
        "/evolve_rollback demo-skill latest",
        skills_dir=skills_dir,
    )

    assert result == {"output": "team rollback handled", "result_type": "answer"}


@pytest.mark.anyio
async def test_process_team_message_stream_handles_team_evolve_list(monkeypatch, tmp_path):
    _write_team_skill(
        tmp_path,
        "demo-skill",
        records=[_evolution_record("First summary line")],
    )
    captured_spec: list[object] = []
    captured_context: list[dict] = []

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            captured_context.append(kwargs)
            spec = SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )
            captured_spec.append(spec)
            return spec

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-stream",
        request_id="req-team-stream",
        channel_id="web",
        metadata=None,
    )
    inputs = {"query": "/evolve_list demo-skill"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
            request,
            inputs,
            object(),
            config_base={"models": {"defaults": []}},
            sessions_root=tmp_path / "tenant-sessions",
    ):
        chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].payload is not None
    assert chunks[0].payload["event_type"] == "chat.final"
    assert 'Skill "demo-skill"' in chunks[0].payload["content"]
    assert chunks[1].payload == {
        "event_type": "chat.processing_status",
        "session_id": "sess-team-stream",
        "is_processing": False,
        "is_complete": True,
    }
    assert chunks[1].is_complete is False
    assert chunks[2].is_complete is True
    assert captured_spec
    assert captured_context[0]["config_base"] == {"models": {"defaults": []}}
    assert captured_context[0]["sessions_root"] == tmp_path / "tenant-sessions"


@pytest.mark.anyio
async def test_process_team_message_stream_handles_evolve_rebuild(monkeypatch, tmp_path):
    _write_team_skill(tmp_path, "demo-skill")
    rebuild_calls: list[dict] = []

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )

    async def _fake_handler(query: str, context: object) -> dict[str, object]:
        assert query == "/evolve_rebuild demo-skill tighten examples"
        assert getattr(context, "mode") == "team"
        return {
            "result_type": "rebuild_request",
            "action": "run_rebuild_inline",
            "skill_name": "demo-skill",
            "user_intent": "tighten examples",
        }

    async def _fake_rebuild(params: dict) -> dict:
        rebuild_calls.append(dict(params))
        return {
            "success": True,
            "name": "demo-skill",
            "new_version": "2.0.0",
            "cleared": True,
        }

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "handle_evolution_slash_command", _fake_handler)

    request = SimpleNamespace(
        session_id="sess-team-rebuild",
        request_id="req-team-rebuild",
        channel_id="web",
        metadata=None,
    )
    inputs = {"query": "/evolve_rebuild demo-skill tighten examples"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
        rebuild_skill=_fake_rebuild,
    ):
        chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].payload is not None
    assert chunks[0].payload["event_type"] == "chat.final"
    assert "2.0.0" in chunks[0].payload["content"]
    assert chunks[0].payload.get("slash_command") == "evolve_rebuild"
    assert chunks[2].is_complete is True
    assert rebuild_calls == [
        {
            "name": "demo-skill",
            "user_intent": "tighten examples",
            "skills_dirs": [str(tmp_path / "team-workspace" / "skills")],
        }
    ]


@pytest.mark.anyio
async def test_process_team_message_stream_followup_owns_waiter_until_team_completed(monkeypatch):
    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        interact_calls: list[tuple[str, str]] = []

        def __init__(self) -> None:
            super().__init__()
            self.waiters: dict[tuple[str, str], asyncio.Queue] = {}
            self.emitter_tasks: list[asyncio.Task] = []

        def has_waiters(self, session_id: str) -> bool:
            return any(sid == session_id for sid, _rid in self.waiters)

        def add_waiter(
            self,
            session_id: str,
            request_id: str,
            queue: asyncio.Queue,
        ) -> None:
            self.waiters[(session_id, request_id)] = queue

        def remove_waiter(self, session_id: str, request_id: str) -> None:
            self.waiters.pop((session_id, request_id), None)

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-followup"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        async def interact(self, session_id: str, query: str):
            type(self).interact_calls.append((session_id, query))
            queue = self.waiters.get((session_id, "req-team-followup"))
            assert queue is not None
            queue.put_nowait(
                {
                    "event_type": "chat.final",
                    "role": "teammate",
                    "member_name": "human-reporter",
                    "content": "follow-up result",
                }
            )
            queue.put_nowait({"event_type": "team.completed"})
            return True, None

    manager = _FakeManager()
    manager.mark_seen_team_events("sess-team-followup")
    manager.mark_workflow_completed("sess-team-followup")
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: manager)

    request = SimpleNamespace(
        session_id="sess-team-followup",
        request_id="req-team-followup",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )
    inputs = {"query": "$human-reporter claim task"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
    ):
        chunks.append(chunk)
    await asyncio.gather(*manager.emitter_tasks)

    assert _FakeManager.interact_calls == [
        ("sess-team-followup", "$human-reporter claim task"),
    ]
    assert [chunk.payload and chunk.payload.get("event_type") for chunk in chunks] == [
        "chat.final",
        "team.completed",
        None,
    ]
    assert chunks[0].request_id == "req-team-followup"
    assert chunks[0].agent_ref == {"mode": "team", "id": "human-reporter"}
    assert chunks[0].metadata["fan_out_targets"]
    assert chunks[-1].is_complete is True
    assert manager.has_seen_team_events("sess-team-followup") is False
    assert manager.is_workflow_completed("sess-team-followup") is False
    assert manager.waiters == {}


@pytest.mark.anyio
async def test_process_team_message_stream_control_followup_reuses_existing_waiter(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    approval_input = InteractiveInput()
    approval_input.update(
        "permission-call-1",
        {"approved": True},
    )

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        def __init__(self) -> None:
            super().__init__()
            self.stream_active = True
            self.waiters = {
                ("sess-team-control-followup", "original-request"): asyncio.Queue(),
            }
            self.added_waiters: list[tuple[str, str]] = []

        def has_waiters(self, session_id: str) -> bool:
            return any(sid == session_id for sid, _rid in self.waiters)

        def add_waiter(
            self,
            session_id: str,
            request_id: str,
            queue: asyncio.Queue,
        ) -> None:
            self.added_waiters.append((session_id, request_id))
            self.waiters[(session_id, request_id)] = queue

        def remove_waiter(self, session_id: str, request_id: str) -> None:
            self.waiters.pop((session_id, request_id), None)

        def has_stream_task(self, session_id: str) -> bool:
            assert session_id == "sess-team-control-followup"
            return self.stream_active

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        async def interact(self, session_id: str, query: Any):
            assert query is approval_input
            self.stream_active = False
            return True, None

    manager = _FakeManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: manager)

    request = SimpleNamespace(
        session_id="sess-team-control-followup",
        request_id="req-team-control-followup",
        channel_id="web",
        metadata=None,
        params={"mode": "team", "source": "permission_interrupt"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": approval_input},
        object(),
    ):
        chunks.append(chunk)

    assert manager.added_waiters == []
    assert set(manager.waiters) == {
        ("sess-team-control-followup", "original-request"),
    }
    assert [chunk.payload and chunk.payload.get("event_type") for chunk in chunks] == [
        "chat.processing_status_deferred",
        None,
    ]
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_retries_followup_while_native_starts(monkeypatch):
    class _FakeManager(_CompletingFollowupRuntimeManagerMixin):
        interact_calls: list[tuple[str, str]] = []

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-followup-starting"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        @classmethod
        async def interact(cls, session_id: str, query: str):
            cls.interact_calls.append((session_id, query))
            return (
                False,
                "deliver_to_leader_failed:[123023] deepagent runtime error, "
                "reason: NativeHarness not started.",
            )

    async def _fake_retry(
        team_manager: object,
        session_id: str,
        query: str,
        *,
        initial_reason: str | None = None,
    ):
        assert team_manager is not None
        assert initial_reason == (
            "deliver_to_leader_failed:[123023] deepagent runtime error, "
            "reason: NativeHarness not started."
        )
        _FakeManager.interact_calls.append((session_id, f"retry:{query}"))
        return team_helpers._FollowupInteractBoundaryResult(
            success=True,
            reason=None,
            first_request_ready=False,
        )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_deliver_followup_interact_across_boundary", _fake_retry)

    request = SimpleNamespace(
        session_id="sess-team-followup-starting",
        request_id="req-team-followup-starting",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "启动中追问"},
        object(),
    ):
        chunks.append(chunk)

    assert _FakeManager.interact_calls == [
        ("sess-team-followup-starting", "启动中追问"),
        ("sess-team-followup-starting", "retry:启动中追问"),
    ]
    assert chunks[0].payload == {"event_type": "team.completed"}
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_restarts_round_after_shutdown_race(monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        interact_calls: list[tuple[str, str]] = []
        skills_ready_calls: list[tuple[str, str]] = []
        stream_active = True

        @classmethod
        def has_stream_task(cls, session_id: str) -> bool:
            assert session_id == "sess-team-followup-stopped"
            return cls.stream_active

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team", enable_swarmflow=False)

        @classmethod
        async def interact(cls, session_id: str, query: str):
            cls.interact_calls.append((session_id, query))
            return (
                False,
                "deliver_to_leader_failed:[123023] deepagent runtime error, "
                "reason: NativeHarness already stopped.",
            )

        @classmethod
        def ensure_team_shared_skills_ready_for_session(cls, session_id: str, team_spec: object):
            cls.skills_ready_calls.append((session_id, team_spec.team_name))

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            captured["prepared"] = (session_id, team_name)

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            captured["registered"] = session_id

    async def _fake_retry(
        team_manager: object,
        session_id: str,
        query: str,
        *,
        initial_reason: str | None = None,
    ):
        assert team_manager is not None
        assert session_id == "sess-team-followup-stopped"
        assert query
        assert initial_reason == (
            "deliver_to_leader_failed:[123023] deepagent runtime error, "
            "reason: NativeHarness already stopped."
        )
        _FakeManager.stream_active = False
        return team_helpers._FollowupInteractBoundaryResult(
            success=False,
            reason=initial_reason,
            first_request_ready=True,
        )

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        _ = channel_id, spec, envs
        captured["consumed"] = (session_id, query, round_id)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_deliver_followup_interact_across_boundary", _fake_retry)
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 7)
    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-team-followup-stopped",
        request_id="req-team-followup-stopped",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "查询杭州天气"},
        object(),
    ):
        chunks.append(chunk)
    await asyncio.sleep(0)

    assert _FakeManager.interact_calls == [
        ("sess-team-followup-stopped", "查询杭州天气"),
    ]
    assert _FakeManager.skills_ready_calls == [
        ("sess-team-followup-stopped", "unit-team"),
    ]
    assert captured["prepared"] == ("sess-team-followup-stopped", "unit-team")
    assert captured["registered"] == "sess-team-followup-stopped"
    assert captured["consumed"] == ("sess-team-followup-stopped", "查询杭州天气", 7)
    assert chunks[-1].is_complete is True
    assert not any(
        chunk.payload
        and chunk.payload.get("error") == "Failed to send message, please try again later"
        for chunk in chunks
    )


@pytest.mark.anyio
async def test_process_team_message_stream_fallback_reuses_first_request_directives(monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        interact_calls: list[tuple[str, str]] = []
        stream_active = True

        @classmethod
        def has_stream_task(cls, session_id: str) -> bool:
            assert session_id == "sess-team-followup-directives"
            return cls.stream_active

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team", enable_swarmflow=False)

        @classmethod
        async def interact(cls, session_id: str, query: str):
            cls.interact_calls.append((session_id, query))
            return False, "gate_closed"

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            captured["prepared"] = (session_id, team_name)

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            captured["registered"] = session_id

    async def _fake_retry(
        team_manager: object,
        session_id: str,
        query: str,
        *,
        initial_reason: str | None = None,
    ):
        assert team_manager is not None
        assert session_id == "sess-team-followup-directives"
        assert query == "/hide_dm /debug weather"
        assert initial_reason == "gate_closed"
        _FakeManager.stream_active = False
        return team_helpers._FollowupInteractBoundaryResult(
            success=False,
            reason=initial_reason,
            first_request_ready=True,
        )

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        _ = channel_id, spec
        captured["consumed"] = (session_id, query, round_id, envs)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_deliver_followup_interact_across_boundary", _fake_retry)
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 9)
    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-team-followup-directives",
        request_id="req-team-followup-directives",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "/hide_dm /debug weather"},
        object(),
    ):
        chunks.append(chunk)
    await asyncio.sleep(0)

    assert _FakeManager.interact_calls == [
        ("sess-team-followup-directives", "/hide_dm /debug weather"),
    ]
    assert captured["prepared"] == ("sess-team-followup-directives", "unit-team")
    assert captured["registered"] == "sess-team-followup-directives"
    assert captured["consumed"][:3] == ("sess-team-followup-directives", "weather", 9)
    stream_envs = captured["consumed"][3]
    assert stream_envs["hide_dm"] is True
    assert stream_envs["JIUWENSWARM_TEAM_STREAM_TRACE"] == "1"
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_silences_gate_closed_when_shutdown_race_times_out(monkeypatch):
    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-followup-timeout"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        @staticmethod
        async def interact(session_id: str, query: str):
            assert session_id == "sess-team-followup-timeout"
            assert query == "还在收尾"
            return False, "gate_closed"

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            raise AssertionError("timed-out shutdown race should not start a new stream")

    async def _fake_retry(
        team_manager: object,
        session_id: str,
        query: str,
        *,
        initial_reason: str | None = None,
    ):
        assert team_manager is not None
        assert session_id == "sess-team-followup-timeout"
        assert query
        assert initial_reason == "gate_closed"
        return team_helpers._FollowupInteractBoundaryResult(
            success=False,
            reason=initial_reason,
            first_request_ready=False,
        )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_deliver_followup_interact_across_boundary", _fake_retry)

    request = SimpleNamespace(
        session_id="sess-team-followup-timeout",
        request_id="req-team-followup-timeout",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "还在收尾"},
        object(),
    ):
        chunks.append(chunk)

    # gate_closed 静默处理：不报 chat.error，直接以 is_complete=True 结束流
    assert chunks[-1].is_complete is True
    assert not any(
        chunk.payload and chunk.payload.get("event_type") == "chat.error"
        for chunk in chunks
    )


@pytest.mark.anyio
async def test_process_team_message_stream_passes_interactive_input_to_followup(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    approval_input = InteractiveInput()
    approval_input.update(
        "exit_plan_mode_call_1",
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )

    class _FakeManager(_CompletingFollowupRuntimeManagerMixin):
        interact_calls: list[tuple[str, Any]] = []

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-plan-followup"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        @classmethod
        async def interact(cls, session_id: str, query: Any):
            cls.interact_calls.append((session_id, query))
            return True, None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-plan-followup",
        request_id="req-team-plan-followup",
        channel_id="web",
        metadata=None,
        params={"mode": "team.plan"},
    )
    inputs = {"query": approval_input}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
    ):
        chunks.append(chunk)

    assert _FakeManager.interact_calls == [
        ("sess-team-plan-followup", approval_input),
    ]
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_resumes_active_session_without_stream_task(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    ask_answer_input = InteractiveInput()
    ask_answer_input.update(
        "tool-ask-1",
        {
            "answers": {"你希望用什么技术实现？": "浏览器（HTML/CSS/JS）"},
            "original_request": "做一个斗地主游戏",
        },
    )

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        interact_calls: list[tuple[str, Any]] = []

        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "sess-team-ask-followup"
            return True

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-ask-followup"
            return False

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-ask-followup"
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        @classmethod
        async def interact(cls, session_id: str, query: Any):
            cls.interact_calls.append((session_id, query))
            return True, None

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(*_args, **_kwargs):
            pytest.fail("active team sessions should not be treated as first requests")

        @staticmethod
        async def prepare_runtime_activation(*_args, **_kwargs):
            pytest.fail("active team sessions should not be recreated")

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-ask-followup",
        request_id="req-team-ask-followup",
        channel_id="web",
        metadata=None,
        params={"mode": "team.plan", "source": "ask_user_interrupt"},
    )
    inputs = {"query": ask_answer_input}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
    ):
        chunks.append(chunk)

    assert _FakeManager.interact_calls == [
        ("sess-team-ask-followup", ask_answer_input),
    ]
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_routes_evolution_interrupt_to_active_runtime(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    approval_input = InteractiveInput()
    approval_input.update(
        "team_skill_evolve_req1",
        {"answers": {"approve": True}},
    )

    class _FakeManager(_CompletingFollowupRuntimeManagerMixin):
        interact_calls: list[tuple[str, Any]] = []

        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "sess-team-evolution-resume"
            return True

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-evolution-resume"
            return False

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-evolution-resume"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team", enable_swarmflow=False)

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, spec: Any):
            pytest.fail("active evolution interrupt resume should not prepare shared skills")

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            pytest.fail("active evolution interrupt resume should not recreate runtime")

        @staticmethod
        def register_stream_task(session_id: str, task: asyncio.Task) -> None:
            pytest.fail("active evolution interrupt resume should not start a stream task")

        @classmethod
        async def interact(cls, session_id: str, query: Any):
            cls.interact_calls.append((session_id, query))
            return True, None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-evolution-resume",
        request_id="req-team-evolution-resume",
        channel_id="web",
        metadata=None,
        params={"mode": "team", "source": "evolution_interrupt"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": approval_input},
        object(),
    ):
        chunks.append(chunk)

    assert _FakeManager.interact_calls == [
        ("sess-team-evolution-resume", approval_input),
    ]
    assert not any(
        chunk.payload
        and chunk.payload.get("error") == "Failed to send message, please try again later"
        for chunk in chunks
    )
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_resumes_structured_team_plan_confirm_after_runtime_recovery(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    approval_input = InteractiveInput()
    approval_input.update(
        "exit_plan_mode_call_1",
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )

    captured: dict[str, Any] = {}

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        runtime_ready = False
        interact_calls: list[tuple[str, Any]] = []

        @classmethod
        def is_runtime_active(cls, session_id: str) -> bool:
            assert session_id == "sess-team-plan-resume"
            return cls.runtime_ready

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-plan-resume"
            return False

        @classmethod
        async def session_has_runtime(cls, session_id: str) -> bool:
            assert session_id == "sess-team-plan-resume"
            return cls.runtime_ready

        @classmethod
        async def wait_for_resumable_runtime(cls, session_id: str, **_kwargs) -> bool:
            assert session_id == "sess-team-plan-resume"
            cls.runtime_ready = True
            return True

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-plan-resume"
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team", enable_swarmflow=False)

        @classmethod
        async def interact(cls, session_id: str, query: Any):
            cls.interact_calls.append((session_id, query))
            return True, None

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, spec: Any):
            captured["skills_ready"] = (session_id, spec.team_name)

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            raise AssertionError("prepare_runtime_activation should not run for resumed approval")

        @staticmethod
        def register_stream_task(session_id: str, task: asyncio.Task) -> None:
            raise AssertionError("register_stream_task should not run for resumed approval")

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "ensure_team_evolution_watcher", lambda *args, **kwargs: None)

    request = SimpleNamespace(
        session_id="sess-team-plan-resume",
        request_id="req-team-plan-resume",
        channel_id="web",
        metadata=None,
        params={
            "mode": "team.plan",
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# 团队计划",
            "plan_language": "cn",
        },
    )
    inputs = {"query": approval_input}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
    ):
        chunks.append(chunk)

    await asyncio.sleep(0)

    assert _FakeManager.interact_calls == [
        ("sess-team-plan-resume", approval_input),
    ]
    assert "skills_ready" not in captured
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_rejects_orphaned_interactive_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    ask_answer_input = InteractiveInput()
    ask_answer_input.update(
        "tool-ask-1",
        {
            "answers": {"你希望用什么技术实现？": "浏览器（HTML/CSS/JS）"},
            "original_request": "做一个斗地主游戏",
        },
    )

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "sess-team-orphan-answer"
            return False

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-orphan-answer"
            return False

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-orphan-answer"
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**_kwargs):
            pytest.fail("orphaned interactive inputs should not recreate team runtime")

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(*_args, **_kwargs):
            pytest.fail("orphaned interactive inputs should not activate team runtime")

        @staticmethod
        async def prepare_runtime_activation(*_args, **_kwargs):
            pytest.fail("orphaned interactive inputs should not activate team runtime")

        @staticmethod
        async def interact(*_args, **_kwargs):
            pytest.fail("orphaned interactive inputs cannot resume a missing runtime")

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-orphan-answer",
        request_id="req-team-orphan-answer",
        channel_id="web",
        metadata=None,
        params={"mode": "team.plan", "source": "ask_user_interrupt"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": ask_answer_input},
        object(),
    ):
        chunks.append(chunk)

    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": "Team runtime is not active, please restart the task",
    }
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_recovers_paused_runtime_for_interactive_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    approval_input = InteractiveInput()
    approval_input.update(
        "exit_plan_mode_call_1",
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )

    class _FakeManager(_CompletingFollowupRuntimeManagerMixin):
        runtime_ready = False
        interact_calls: list[tuple[str, Any]] = []

        @classmethod
        def is_runtime_active(cls, session_id: str) -> bool:
            assert session_id == "sess-team-plan-recover"
            return cls.runtime_ready

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-plan-recover"
            return False

        @classmethod
        async def session_has_runtime(cls, session_id: str) -> bool:
            assert session_id == "sess-team-plan-recover"
            return cls.runtime_ready

        @classmethod
        async def wait_for_resumable_runtime(cls, session_id: str, **_kwargs) -> bool:
            assert session_id == "sess-team-plan-recover"
            cls.runtime_ready = True
            return True

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-plan-recover"
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**_kwargs):
            return SimpleNamespace(team_name="unit-team")

        @classmethod
        async def interact(cls, session_id: str, query: Any):
            cls.interact_calls.append((session_id, query))
            return True, None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-plan-recover",
        request_id="req-team-plan-recover",
        channel_id="web",
        metadata=None,
        params={"mode": "team.plan", "source": "confirm_interrupt"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": approval_input},
        object(),
    ):
        chunks.append(chunk)

    assert _FakeManager.interact_calls == [
        ("sess-team-plan-recover", approval_input),
    ]
    assert chunks[0].payload == {"event_type": "team.completed"}
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_treats_plain_query_as_first_request_after_round_end(monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "sess-team-new-round"
            return False

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "sess-team-new-round"
            return False

        @staticmethod
        async def session_has_runtime(session_id: str) -> bool:
            assert session_id == "sess-team-new-round"
            return True

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-new-round"
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**_kwargs):
            return SimpleNamespace(team_name="unit-team", enable_swarmflow=False)

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, spec: Any):
            captured["skills_ready"] = (session_id, spec.team_name)

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str):
            captured["prepared"] = (session_id, team_name)

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            captured["registered"] = session_id

        @staticmethod
        async def interact(*_args, **_kwargs):
            raise AssertionError("plain text query after round end should start a new team round")

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        _ = channel_id, spec, envs
        captured["consumed"] = (session_id, query, round_id)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 1)
    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-team-new-round",
        request_id="req-team-new-round",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "你好"},
        object(),
    ):
        chunks.append(chunk)
    await asyncio.sleep(0)

    assert captured["prepared"] == ("sess-team-new-round", "unit-team")
    assert captured["registered"] == "sess-team-new-round"
    assert captured["consumed"] == ("sess-team-new-round", "你好", 1)
    assert captured["skills_ready"] == ("sess-team-new-round", "unit-team")
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_converts_a2ui_followup_event(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    class _FakeManager(_CompletingFollowupRuntimeManagerMixin):
        interact_calls: list[tuple[str, str]] = []

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "sess-team-a2ui"
            return True

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(team_name="unit-team")

        @classmethod
        async def interact(cls, session_id: str, query: str):
            cls.interact_calls.append((session_id, query))
            return True, None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    request = SimpleNamespace(
        session_id="sess-team-a2ui",
        request_id="req-team-a2ui",
        channel_id="web",
        metadata={"language": "zh"},
        params={"mode": "team"},
    )
    inputs = {
        "query": {
            "type": "a2ui.client_event",
            "protocolVersion": "0.8",
            "event": {
                "userAction": {
                    "name": "submitDietForm",
                    "surfaceId": "diet-preferences",
                    "sourceComponentId": "submit-btn",
                    "context": {"name": "Codex", "dietType": ["balanced"]},
                },
            },
        },
    }

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        inputs,
        object(),
    ):
        chunks.append(chunk)

    assert len(_FakeManager.interact_calls) == 1
    session_id, prompt = _FakeManager.interact_calls[0]
    assert session_id == "sess-team-a2ui"
    assert isinstance(prompt, str)
    assert "A2UI" in prompt
    assert "submitDietForm" in prompt
    assert "dietType" in prompt
    assert chunks[0].payload == {"event_type": "team.completed"}
    assert chunks[1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_defers_first_evolve_until_team_runtime_exists(monkeypatch, tmp_path):
    captured_queries: list[str] = []
    user_intent = "没有特殊要求时格式尽量简洁，如果使用颜色也需要保持美观"
    _write_team_skill(tmp_path, "xlsx")

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @classmethod
        def has_stream_task(cls, session_id: str) -> bool:
            return False

        @classmethod
        async def get_swarm_enriched_team_spec(cls, **kwargs):
            return SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, team_spec: object) -> None:
            return None

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str) -> None:
            return None

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            return None

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        captured_queries.append(query)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 1)
    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-first-evolve",
        request_id="req-first-evolve",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )
    inputs = {"query": f"/evolve xlsx {user_intent}"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(request, inputs, object()):
        chunks.append(chunk)
    await asyncio.sleep(0)

    assert len(captured_queries) == 1
    assert "prepare_skill_evolution" in captured_queries[0]
    assert user_intent in captured_queries[0]
    assert not any(
        chunk.payload
        and chunk.payload.get("event_type") == "chat.error"
        and "TeamSkillEvolutionRail" in str(chunk.payload.get("error", ""))
        for chunk in chunks
    )


@pytest.mark.anyio
async def test_process_team_message_stream_syncs_team_skills_before_evolve_slash(monkeypatch, tmp_path):
    captured_queries: list[str] = []
    user_intent = "没有特殊要求时格式尽量简洁，如果使用颜色也需要保持美观"

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, team_spec: object) -> None:
            _write_team_skill(tmp_path, "xlsx")

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str) -> None:
            return None

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            return None

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        captured_queries.append(query)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 1)
    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-sync-evolve",
        request_id="req-sync-evolve",
        channel_id="web",
        metadata=None,
        params={"mode": "team"},
    )
    inputs = {"query": f"/evolve xlsx {user_intent}"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(request, inputs, object()):
        chunks.append(chunk)
    await asyncio.sleep(0)

    assert captured_queries
    assert "prepare_skill_evolution" in captured_queries[0]
    assert user_intent in captured_queries[0]
    assert not any(
        chunk.payload
        and chunk.payload.get("event_type") == "chat.error"
        and "未找到 Skill 'xlsx'" in str(chunk.payload.get("error", ""))
        for chunk in chunks
    )


@pytest.mark.anyio
async def test_process_team_message_stream_runs_evolve_followup_without_rail(monkeypatch, tmp_path):
    captured_queries: list[str] = []
    _write_team_skill(tmp_path, "demo-skill")

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, team_spec: object) -> None:
            return None

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str) -> None:
            return None

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 1)

    async def _fake_consume_stream_with_query(
        channel_id: str | None,
        session_id: str,
        spec: object,
        query: str,
        *,
        round_id: int,
        envs: dict | None = None,
    ) -> None:
        captured_queries.append(query)

    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-team-evolve",
        request_id="req-team-evolve",
        channel_id="web",
        metadata=None,
    )
    inputs = {"query": "/evolve demo-skill improve review flow"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
            request,
            inputs,
            object(),
    ):
        chunks.append(chunk)

    await asyncio.sleep(0)
    assert captured_queries
    assert "prepare_skill_evolution" in captured_queries[0]
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
async def test_process_team_message_stream_does_not_emit_evolution_status_for_no_evolve_records(monkeypatch, tmp_path):
    _write_team_skill(tmp_path, "demo-skill")

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            return False

        @staticmethod
        async def get_swarm_enriched_team_spec(**kwargs):
            return SimpleNamespace(
                team_name="unit-team",
                workspace=SimpleNamespace(root_path=str(tmp_path / "team-workspace")),
            )

        @staticmethod
        def ensure_team_shared_skills_ready_for_session(session_id: str, team_spec: object) -> None:
            return None

        @staticmethod
        async def prepare_runtime_activation(session_id: str, team_name: str) -> None:
            return None

        @staticmethod
        def register_stream_task(session_id: str, task: object) -> None:
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "increment_session_round_count", lambda session_id: 1)

    async def _fake_consume_stream_with_query(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(team_helpers, "_consume_stream_with_query", _fake_consume_stream_with_query)

    request = SimpleNamespace(
        session_id="sess-team-evolve-noop",
        request_id="req-team-evolve-noop",
        channel_id="web",
        metadata=None,
    )
    inputs = {"query": "/evolve demo-skill improve review flow"}

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
            request,
            inputs,
            object(),
    ):
        chunks.append(chunk)

    assert [chunk.payload["event_type"] for chunk in chunks if chunk.payload] == []
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "snapshot",
    [
        {"members": [{"member_id": "analyst", "status": "busy"}], "tasks": []},
        {
            "members": [{"member_id": "analyst", "status": "ready"}],
            "tasks": [{"task_id": "task-1", "status": "in_progress"}],
        },
    ],
)
async def test_team_round_settled_rejects_active_work(monkeypatch, snapshot):
    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            return snapshot

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert not await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-active")


@pytest.mark.anyio
@pytest.mark.parametrize("monitor_state", [None, [], RuntimeError("snapshot failed")])
async def test_team_round_settled_rejects_missing_or_invalid_monitor_state(
    monkeypatch,
    monitor_state,
):
    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            if isinstance(monitor_state, Exception):
                raise monitor_state
            return monitor_state

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            if monitor_state is None:
                return None
            return _FakeMonitorHandler()

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert not await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-invalid")


@pytest.mark.anyio
async def test_team_round_settled_accepts_idle_empty_board(monkeypatch):
    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return False

    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )

        async def get_team_snapshot(self):
            return {
                "members": [
                    {"member_id": "leader", "status": "ready"},
                    {"member_id": "analyst", "status": "unstarted"},
                ],
                "tasks": [],
            }

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-idle")


@pytest.mark.anyio
async def test_team_round_settled_rejects_unread_messages(monkeypatch):
    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return True

    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )

        async def get_team_snapshot(self):
            return {"members": [{"member_id": "leader", "status": "ready"}], "tasks": []}

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert not await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-unread")


@pytest.mark.anyio
async def test_team_round_settled_rejects_active_workflow(monkeypatch):
    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            return {"members": [], "tasks": []}

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            return SimpleNamespace(
                get_run_states=lambda: {
                    "run-1": team_helpers.WorkflowRunState(status="running"),
                }
            )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert not await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-workflow")


@pytest.mark.anyio
async def test_team_round_settled_accepts_terminal_workflow(monkeypatch):
    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            return {"members": [], "tasks": []}

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            return SimpleNamespace(
                get_run_states=lambda: {
                    "run-1": team_helpers.WorkflowRunState(status="completed"),
                }
            )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    assert await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-workflow-done")


def test_team_stream_factory_uses_core_global_runner(monkeypatch):
    expected_stream = object()
    calls: list[dict] = []

    class _FakeGlobalRunner:
        @staticmethod
        def run_agent_team_streaming(**kwargs):
            calls.append(kwargs)
            return expected_stream

    monkeypatch.setattr(
        team_helpers,
        "_get_global_team_runner",
        lambda: _FakeGlobalRunner(),
        raising=False,
    )

    factory = getattr(team_helpers, "_run_agent_team_streaming")
    stream = factory(agent_team="demo-team", inputs={"query": "hello"}, session="sess-1")

    assert stream is expected_stream
    assert calls == [
        {
            "agent_team": "demo-team",
            "inputs": {"query": "hello"},
            "session": "sess-1",
        }
    ]


@pytest.mark.anyio
async def test_leader_direct_answer_finishes_settled_round(monkeypatch):
    broadcasted: list[dict] = []
    closed: list[str] = []
    popped: list[str] = []

    async def _hanging_stream(**kwargs):
        try:
            yield {"event_type": "chat.delta", "content": "你好"}
            yield {"event_type": "chat.final", "content": "你好，我是团队负责人。"}
            await asyncio.Event().wait()
        finally:
            closed.append("closed")

    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            return {"members": [], "tasks": []}

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_hanging_stream)

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            assert closed == ["closed"]
            popped.append(session_id)

    fake_mgr = _FakeManager()
    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: fake_mgr)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: broadcasted.append(event),
    )

    await asyncio.wait_for(
        _TeamHelpersTestApi.consume_stream_with_query(
            "officeclaw",
            "sess-direct-answer",
            SimpleNamespace(team_name="demo-team"),
            "你好",
        ),
        timeout=0.5,
    )

    event_types = [event.get("event_type") for event in broadcasted]
    assert event_types.index("chat.final") < event_types.index("team.completed")
    assert popped == ["sess-direct-answer"]
    assert closed == ["closed"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "blocker",
    ["busy_member", "open_task", "unread_message", "active_workflow"],
)
async def test_leader_final_keeps_unsettled_round_open(monkeypatch, blocker):
    final_broadcasted = asyncio.Event()
    broadcasted: list[dict] = []
    closed: list[str] = []

    async def _hanging_stream(**kwargs):
        try:
            yield {"event_type": "chat.final", "content": "阶段性结论"}
            await asyncio.Event().wait()
        finally:
            closed.append("closed")

    snapshot = {"members": [{"member_id": "leader", "status": "ready"}], "tasks": []}
    if blocker == "busy_member":
        snapshot["members"].append({"member_id": "analyst", "status": "busy"})
    elif blocker == "open_task":
        snapshot["tasks"].append({"task_id": "task-1", "status": "in_progress"})

    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return blocker == "unread_message"

    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )

        async def get_team_snapshot(self):
            return snapshot

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_hanging_stream)

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_workflow_handler(session_id: str):
            if blocker != "active_workflow":
                return None
            return SimpleNamespace(
                get_run_states=lambda: {
                    "run-1": team_helpers.WorkflowRunState(status="running"),
                }
            )

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

    def _record_broadcast(channel_id, session_id, event):
        broadcasted.append(event)
        if event.get("event_type") == "chat.final":
            final_broadcasted.set()

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_broadcast_event", _record_broadcast)

    consumer = asyncio.create_task(
        _TeamHelpersTestApi.consume_stream_with_query(
            "officeclaw",
            f"sess-{blocker}",
            SimpleNamespace(team_name="demo-team"),
            "继续处理",
        )
    )
    await asyncio.wait_for(final_broadcasted.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert not consumer.done()
    assert not any(event.get("event_type") == "team.completed" for event in broadcasted)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert closed == ["closed"]


@pytest.mark.anyio
async def test_member_settle_edge_closes_stream_without_second_leader_final(monkeypatch):
    """Repro for the 10:41 hang: the leader's chat.final arrives while a member
    is still busy (first settle check False -> stream stays open), then a member
    transitions to ready — the member-settle edge re-evaluates settle and closes
    the stream with exactly one is_complete, without a second leader chat.final.
    """
    broadcasted: list[dict] = []
    closed: list[str] = []

    async def _hanging_stream(**kwargs):
        try:
            # Leader final arrives while a member is busy -> first settle check
            # is False, so the stream must stay open (no is_complete yet).
            yield {"event_type": "chat.final", "content": "等待回复"}
            # Member transitions to ready -> the settle edge must fire and close.
            yield {
                "event_type": "team.member",
                "event": {
                    "type": "team.member.status_changed",
                    "member_id": "run1:analyst",
                    "old_status": "busy",
                    "new_status": "ready",
                },
            }
            await asyncio.Event().wait()  # hang if the edge does not fire
        finally:
            closed.append("closed")

    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return False

    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )

        def __init__(self) -> None:
            self._calls = 0

        async def get_team_snapshot(self):
            # First settle check (leader chat.final): a member is busy -> not settled.
            # Subsequent check (member->ready edge): all members ready -> settled.
            self._calls += 1
            if self._calls <= 1:
                return {"members": [{"member_id": "run1:analyst", "status": "busy"}], "tasks": []}
            return {"members": [{"member_id": "run1:analyst", "status": "ready"}], "tasks": []}

    shared_monitor = _FakeMonitorHandler()

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor_handler(session_id: str):
            return shared_monitor

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

        @staticmethod
        def claim_round_complete(session_id: str) -> bool:
            return True

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _hanging_stream)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "append_history_record", lambda **kwargs: None)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: broadcasted.append(event),
    )

    await asyncio.wait_for(
        _TeamHelpersTestApi.consume_stream_with_query(
            "officeclaw",
            "sess-member-settle",
            SimpleNamespace(team_name="demo-team"),
            "你好",
        ),
        timeout=0.5,
    )

    # Exactly one is_complete frame, emitted by the member->ready edge.
    is_complete = [
        e for e in broadcasted
        if e.get("event_type") == "chat.processing_status" and e.get("is_complete")
    ]
    assert len(is_complete) == 1
    # Exactly one leader chat.final (the one that arrived while busy); the
    # member->ready edge must NOT require a second leader chat.final.
    finals = [e for e in broadcasted if e.get("event_type") == "chat.final"]
    assert len(finals) == 1
    # The stream closed (member-settle edge fired, did not hang).
    assert closed == ["closed"]


@pytest.mark.anyio
async def test_consume_stream_with_query_broadcasts_leader_and_teammate_outputs(monkeypatch):
    broadcasted: list[dict] = []
    ready_calls: list[tuple[str, str]] = []

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="team.runtime_ready",
            payload={
                "event_type": "team.runtime_ready",
                "team_name": "demo-team",
                "activation_kind": "create",
            },
            role=TeamRole.LEADER,
        )
        yield SimpleNamespace(
            type="llm_usage",
            payload={
                "metadata": {
                    "usage_metadata": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                    },
                },
            },
            role=TeamRole.LEADER,
        )
        yield SimpleNamespace(
            type="answer",
            payload={"output": {"output": "leader answer"}, "result_type": "answer"},
            role=TeamRole.LEADER,
        )
        yield SimpleNamespace(
            type="llm_reasoning",
            payload={"content": "teammate private reasoning"},
            role=TeamRole.TEAMMATE,
            source_member="analyst",
        )
        yield SimpleNamespace(
            type="tool_call",
            payload={"tool_call": {"name": "read_file", "arguments": {"path": "notes.md"}}},
            role=TeamRole.TEAMMATE,
            member_name="analyst",
        )
        yield SimpleNamespace(
            type="answer",
            payload={"output": {"output": "teammate answer"}, "result_type": "answer"},
            role=TeamRole.TEAMMATE,
            source_member="analyst",
        )
        yield SimpleNamespace(
            type="answer",
            payload={"output": {"output": "human answer"}, "result_type": "answer"},
            role=SimpleNamespace(value=TeamRole.HUMAN_AGENT.value),
            source_member="human_agent",
        )
        yield SimpleNamespace(
            type="team.completed",
            payload={"event_type": "team.completed"},
            role=TeamRole.LEADER,
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

        @staticmethod
        async def get_agent_team_monitor(team_name: str, session_id: str, hide_dm: bool = False):
            return None

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def commit_runtime_ready(session_id: str, team_name: str) -> None:
            ready_calls.append((session_id, team_name))

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor(session_id: str):
            return None

        @staticmethod
        async def attach_distributed_hooks_for_runner_runtime(
                team_name: str,
                session_id: str,
                channel_id: str,
        ) -> None:
            pass

        @staticmethod
        def resolve_team_agent(session_id: str):
            return None

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

        @staticmethod
        def register_workflow_handler(session_id: str, handler: object) -> None:
            pass

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    fake_mgr = _FakeManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: fake_mgr)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: (
            broadcasted.append(event),
            fake_mgr.mark_seen_team_events(session_id)
            if event.get("event_type") in team_helpers._TEAM_BUILDING_EVENT_TYPES
            else None,
        ),
    )
    monkeypatch.setattr(team_helpers, "ensure_team_evolution_watcher", lambda *args, **kwargs: None)
    monkeypatch.setattr(team_helpers, "get_session_metadata", lambda session_id: {})
    monkeypatch.setattr(team_helpers, "update_session_metadata", lambda **kwargs: None)

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-leader-only",
        SimpleNamespace(team_name="demo-team"),
        "hello",
    )

    assert ready_calls == [("sess-leader-only", "demo-team")]
    # The inactive runtime never settles (_team_round_settled is False: the
    # manager exposes no monitor_handler) and never authorises completion
    # (claim_round_complete returns False). Until the team settles, NO
    # is_complete frame may be emitted — the "before-settle" invariant of
    # the per-round dedup contract.
    assert [event["event_type"] for event in broadcasted] == [
        "chat.processing_status",
        "team.runtime_ready",
        "chat.usage_metadata",
        'chat.final',
        'chat.reasoning',
        'chat.tool_call',
        'chat.final',
        'chat.final',
        'team.completed',
    ]
    usage_event = next(
        event for event in broadcasted if event["event_type"] == "chat.usage_metadata"
    )
    assert usage_event == {
        "event_type": "chat.usage_metadata",
        "metadata": {
            "usage_metadata": {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
            },
        },
        "session_id": "sess-leader-only",
        "rid": 1,
    }
    assert not any(event["event_type"] == "chat.llm_usage" for event in broadcasted)
    reasoning_event = next(event for event in broadcasted if event["event_type"] == "chat.reasoning")
    assert reasoning_event["role"] == "teammate"
    assert reasoning_event["member_name"] == "analyst"
    tool_event = next(event for event in broadcasted if event["event_type"] == "chat.tool_call")
    assert tool_event["role"] == "teammate"
    assert tool_event["member_name"] == "analyst"
    # Round-start processing_status
    assert broadcasted[0]["is_processing"] is True
    assert broadcasted[0]["is_complete"] is False
    # Explicit "before-settle" invariant: no is_complete frame is emitted
    # while the round is unsettled — the leader chat.final keeps the round
    # open (helper returns False) and the team.completed poll path is
    # blocked by claim_round_complete.
    is_complete_frames = [
        event
        for event in broadcasted
        if event.get("event_type") == "chat.processing_status"
        and event.get("is_complete") is True
    ]
    assert is_complete_frames == []
    # The post-loop team.completed marker is still broadcast as the round's
    # terminal control event (carries no is_complete payload — the dedup
    # only governs processing_status(is_complete=True) frames).
    assert broadcasted[-1]["event_type"] == "team.completed"


@pytest.mark.anyio
async def test_consume_stream_with_query_broadcasts_leader_task_failed_detail_and_final(monkeypatch):
    broadcasted: list[dict] = []
    detail = (
        "[181001] model call failed, reason: openAI API async stream error: "
        "BadRequestError: deepseek-v4-X is invalid, use deepseek-v4-pro or deepseek-v4-flash"
    )

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="controller_output",
            payload={
                "type": "task_failed",
                "data": [{"type": "text", "text": detail}],
            },
            role=TeamRole.LEADER,
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    fake_mgr = _FakeManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: fake_mgr)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: (
            broadcasted.append(event),
            fake_mgr.mark_seen_team_events(session_id)
            if event.get("event_type") in team_helpers._TEAM_BUILDING_EVENT_TYPES
            else None,
        ),
    )

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-leader-error",
        SimpleNamespace(team_name="demo-team"),
        "hello",
    )

    assert [event["event_type"] for event in broadcasted] == [
        "chat.processing_status",
        "chat.error",
        "chat.final",
        "team.completed",
    ]
    assert "deepseek-v4-X" in broadcasted[1]["error"]
    assert "deepseek-v4-pro" in broadcasted[1]["error"]
    assert broadcasted[1]["rid"] == 1
    assert broadcasted[2] == {
        "event_type": "chat.final",
        "content": "",
        "session_id": "sess-leader-error",
        "rid": 1,
    }
    assert not any(
        event.get("event_type") == "chat.processing_status" and event.get("is_processing") is False
        for event in broadcasted
    )


@pytest.mark.anyio
async def test_consume_stream_with_query_does_not_final_teammate_task_failed(monkeypatch):
    broadcasted: list[dict] = []
    detail = (
        "[181001] model call failed, reason: openAI API async stream error: "
        "BadRequestError: deepseek-v4-X is invalid, use deepseek-v4-pro or deepseek-v4-flash"
    )

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="controller_output",
            payload={
                "type": "task_failed",
                "data": [{"type": "text", "text": detail}],
            },
            role=TeamRole.TEAMMATE,
            source_member="analyst",
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    fake_mgr = _FakeManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: fake_mgr)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: (
            broadcasted.append(event),
            fake_mgr.mark_seen_team_events(session_id)
            if event.get("event_type") in team_helpers._TEAM_BUILDING_EVENT_TYPES
            else None,
        ),
    )

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-teammate-error",
        SimpleNamespace(team_name="demo-team"),
        "hello",
    )

    assert [event["event_type"] for event in broadcasted] == [
        "chat.processing_status",
        "chat.error",
        "team.completed",
    ]
    assert "deepseek-v4-X" in broadcasted[1]["error"]
    assert broadcasted[1]["role"] == TeamRole.TEAMMATE.value
    assert broadcasted[1]["member_name"] == "analyst"


def test_extract_query_directives_strips_hide_dm_prefix_and_flags():
    cleaned, hide_dm, debug = _TeamHelpersTestApi.extract_query_directives(
        "/hide_dm please summarize"
    )
    assert hide_dm is True
    assert debug is False
    assert cleaned == "please summarize"


@pytest.mark.anyio
async def test_consume_stream_with_query_deduplicates_ask_user_questions(monkeypatch):
    broadcasted: list[dict] = []

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="chat.ask_user_question",
            payload={
                "request_id": "tool-ask-1",
                "questions": [{"question": "请选择", "header": "选择", "options": []}],
            },
            role=TeamRole.LEADER,
        )
        yield SimpleNamespace(
            type="chat.ask_user_question",
            payload={
                "request_id": "tool-ask-1",
                "questions": [{"question": "请选择", "header": "选择", "options": []}],
            },
            role=TeamRole.LEADER,
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    fake_mgr = _FakeManager()
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: fake_mgr)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda channel_id, session_id, event: (
            broadcasted.append(event),
            fake_mgr.mark_seen_team_events(session_id)
            if event.get("event_type") in team_helpers._TEAM_BUILDING_EVENT_TYPES
            else None,
        ),
    )

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-ask-dedupe",
        SimpleNamespace(team_name="demo-team"),
        "hello",
    )

    ask_events = [
        event
        for event in broadcasted
        if event.get("event_type") == "chat.ask_user_question"
    ]
    assert len(ask_events) == 1
    assert ask_events[0]["request_id"] == "tool-ask-1"


def test_extract_query_directives_ignores_non_prefix():
    cleaned, hide_dm, debug = _TeamHelpersTestApi.extract_query_directives(
        "/hide_dmsomething else"
    )
    assert hide_dm is False
    assert debug is False
    assert cleaned == "/hide_dmsomething else"


def test_extract_query_directives_handles_bare_hide_dm():
    cleaned, hide_dm, debug = _TeamHelpersTestApi.extract_query_directives("/hide_dm")
    assert hide_dm is True
    assert debug is False
    assert cleaned == ""


@pytest.mark.anyio
async def test_consume_stream_with_query_propagates_hide_dm_to_monitor(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="team.runtime_ready",
            payload={
                "event_type": "team.runtime_ready",
                "team_name": "demo-team",
                "activation_kind": "create",
            },
            role=TeamRole.LEADER,
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

        @staticmethod
        async def get_agent_team_monitor(team_name: str, session_id: str, hide_dm: bool = False):
            captured["team_name"] = team_name
            captured["session_id"] = session_id
            captured["hide_dm"] = hide_dm
            return None

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def commit_runtime_ready(session_id: str, team_name: str) -> None:
            pass

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor(session_id: str):
            return None

        @staticmethod
        async def attach_distributed_hooks_for_runner_runtime(
                team_name: str,
                session_id: str,
                channel_id: str,
        ) -> None:
            pass

        @staticmethod
        def resolve_team_agent(session_id: str):
            return None

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(team_helpers, "get_session_metadata", lambda session_id: {})
    monkeypatch.setattr(team_helpers, "update_session_metadata", lambda **kwargs: None)

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-hide-dm",
        SimpleNamespace(team_name="demo-team"),
        "hello",
        round_id=1,
        envs={"hide_dm": True},
    )

    assert captured == {
        "team_name": "demo-team",
        "session_id": "sess-hide-dm",
        "hide_dm": True,
    }


@pytest.mark.anyio
async def test_handle_team_slash_command_requires_skill_name_for_bare_evolve():
    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve",
    )

    assert result == {
        "output": "请补充 Skill 名称：`/evolve <skill_name> [user_query]`",
        "result_type": "error",
    }


@pytest.mark.anyio
async def test_handle_team_slash_command_submits_evolve_request_without_intent(tmp_path):
    skills_dir = _write_team_skill(tmp_path, "demo-skill")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve demo-skill",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "demo-skill"
    assert "prepare_skill_evolution" in result["followup_prompt"]
    assert 'subject={"kind": "swarm-skill", "name": "demo-skill"}' in result["followup_prompt"]
    assert 'user_intent=""' in result["followup_prompt"]


@pytest.mark.anyio
async def test_handle_team_slash_command_submits_explicit_evolve_request(tmp_path):
    skills_dir = _write_team_skill(tmp_path, "demo-skill")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve demo-skill improve review flow",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "demo-skill"
    assert "prepare_skill_evolution" in result["followup_prompt"]
    assert 'subject={"kind": "swarm-skill", "name": "demo-skill"}' in result["followup_prompt"]
    assert "improve review flow" in result["followup_prompt"]


@pytest.mark.anyio
async def test_handle_team_slash_command_uses_regular_skill_subject_kind(tmp_path):
    skills_dir = _write_regular_skill(tmp_path, "regular-skill")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve regular-skill improve review flow",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "followup"
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "regular-skill"
    assert 'subject={"kind": "skill", "name": "regular-skill"}' in result["followup_prompt"]
    assert "swarm-skill" not in result["followup_prompt"]


@pytest.mark.anyio
async def test_handle_team_slash_command_returns_agent_driven_followup_for_xlsx(tmp_path):
    user_intent = "没有特殊要求时格式尽量简洁，如果使用颜色也需要保持美观"
    skills_dir = _write_team_skill(tmp_path, "xlsx")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        f"/evolve xlsx {user_intent}",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["action"] == "run_evolve_followup"
    assert result["skill_name"] == "xlsx"
    assert result["result_type"] == "followup"
    assert user_intent in result["followup_prompt"]


@pytest.mark.anyio
async def test_handle_team_slash_command_reports_missing_skill(tmp_path):
    skills_dir = str(tmp_path / "team-workspace" / "skills")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve demo-skill improve review flow",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "error"
    assert "未找到 Skill 'demo-skill'" in result["output"]


@pytest.mark.anyio
async def test_handle_team_slash_command_simplify_reports_noop(tmp_path):
    skills_dir = _write_team_skill(tmp_path, "demo-skill")

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-simplify",
        "/evolve_simplify demo-skill",
        skills_dir=skills_dir,
    )

    assert result == {
        "output": "Skill 'demo-skill' 暂无演进经验，无需整理。",
        "result_type": "answer",
    }


@pytest.mark.anyio
async def test_handle_team_slash_command_lists_regular_skill_records(tmp_path):
    skills_dir = _write_regular_skill(
        tmp_path,
        "regular-skill",
        records=[_evolution_record("Improve regular retry flow")],
    )

    result = await _TeamHelpersTestApi.handle_team_slash_command(
        "web",
        "sess-team-evolve",
        "/evolve_list regular-skill",
        skills_dir=skills_dir,
    )

    assert result is not None
    assert result["result_type"] == "answer"
    assert 'Skill "regular-skill"' in result["output"]
    assert "Improve regular retry flow" in result["output"]


# ---------------------------------------------------------------------------
# WorkflowMonitorHandler integration tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ensure_monitor_handlers_rebinds_replaced_pool_agent(monkeypatch):
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamMonitorHandler,
    )
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    class _FakeMonitor:
        def __init__(self, team_agent: object) -> None:
            self._team_agent = team_agent
            self.team_name = "demo-team"
            self.stopped = False

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stopped = True

        async def events(self):
            while not self.stopped:
                await asyncio.sleep(0.01)
            if False:
                yield None

    manager = TeamManager()
    old_agent = object()
    new_agent = object()
    stale = TeamMonitorHandler(_FakeMonitor(old_agent), "sess-monitor-rebind")
    await stale.start()
    manager.register_monitor("sess-monitor-rebind", stale)
    created: list[_FakeMonitor] = []

    async def fake_get_monitor(**kwargs):
        monitor = _FakeMonitor(new_agent)
        created.append(monitor)
        return monitor

    async def fake_pool_agent(team_name: str):
        assert team_name == "demo-team"
        return new_agent

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: manager)
    monkeypatch.setattr(team_helpers.Runner, "get_agent_team_monitor", fake_get_monitor)
    monkeypatch.setattr(
        team_helpers,
        "_current_pool_team_agent",
        fake_pool_agent,
        raising=False,
    )

    await team_helpers.ensure_monitor_handlers_for_active_runtime(
        "web",
        "sess-monitor-rebind",
        "demo-team",
    )

    current = manager.get_monitor("sess-monitor-rebind")
    assert stale._monitor.stopped is True
    assert current is not stale
    assert len(created) == 1
    assert current._monitor._team_agent is new_agent
    await current.stop()


@pytest.mark.anyio
async def test_ensure_monitor_handlers_keeps_current_pool_agent(monkeypatch):
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamMonitorHandler,
    )
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    class _FakeMonitor:
        def __init__(self, team_agent: object) -> None:
            self._team_agent = team_agent
            self.team_name = "demo-team"
            self.stopped = False

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stopped = True

        async def events(self):
            while not self.stopped:
                await asyncio.sleep(0.01)
            if False:
                yield None

    manager = TeamManager()
    current_agent = object()
    handler = TeamMonitorHandler(_FakeMonitor(current_agent), "sess-monitor-current")
    await handler.start()
    manager.register_monitor("sess-monitor-current", handler)

    async def fail_get_monitor(**kwargs):
        raise AssertionError("current monitor must not be rebuilt")

    async def fake_pool_agent(team_name: str):
        assert team_name == "demo-team"
        return current_agent

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: manager)
    monkeypatch.setattr(team_helpers.Runner, "get_agent_team_monitor", fail_get_monitor)
    monkeypatch.setattr(
        team_helpers,
        "_current_pool_team_agent",
        fake_pool_agent,
        raising=False,
    )

    await team_helpers.ensure_monitor_handlers_for_active_runtime(
        "web",
        "sess-monitor-current",
        "demo-team",
    )

    assert manager.get_monitor("sess-monitor-current") is handler
    assert handler._monitor.stopped is False
    await handler.stop()


@pytest.mark.anyio
async def test_ensure_monitor_handlers_creates_workflow_handler_when_swarmflow_enabled(monkeypatch):
    """creates a WorkflowMonitorHandler and registers it when enable_swarmflow=True."""
    registered_handlers: dict[str, object] = {}
    channel_id = "web"
    session_id = "sess-wf-int"
    team_name = "demo-team"

    class _FakeMonitor:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def workflow_events(self):
            return
            yield

        async def events(self):
            return
            yield

    class _FakeRunner:
        @staticmethod
        async def get_agent_team_monitor(team_name: str, session_id: str, **kwargs):
            return _FakeMonitor()

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def get_monitor(sid: str):
            return None

        @staticmethod
        def register_monitor(sid: str, handler: object) -> None:
            pass

        @staticmethod
        def get_workflow_handler(sid: str):
            return registered_handlers.get(sid)

        @staticmethod
        def register_workflow_handler(sid: str, handler: object) -> None:
            registered_handlers[sid] = handler

    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda cid: _FakeManager())

    consume_calls: list[tuple] = []

    async def _fake_consume_wf(cid, sid, handler):
        consume_calls.append((cid, sid, handler))

    async def _fake_consume_monitor(cid, sid, handler):
        pass

    monkeypatch.setattr(team_helpers, "_consume_workflow_events", _fake_consume_wf)
    monkeypatch.setattr(team_helpers, "_consume_monitor_events", _fake_consume_monitor)

    await team_helpers.ensure_monitor_handlers_for_active_runtime(
        channel_id, session_id, team_name, enable_swarmflow=True,
    )

    wf_handler = registered_handlers.get(session_id)
    assert wf_handler is not None
    assert wf_handler.session_id == session_id
    assert wf_handler.is_running is True

    await asyncio.sleep(0)
    assert len(consume_calls) == 1
    assert consume_calls[0][0] == channel_id
    assert consume_calls[0][1] == session_id


@pytest.mark.anyio
async def test_ensure_monitor_handlers_skips_workflow_handler_when_swarmflow_disabled(monkeypatch):
    """not create WorkflowMonitorHandler when enable_swarmflow=False (the default)."""
    registered_wf_handlers: dict[str, object] = {}
    channel_id = "web"
    session_id = "sess-no-swarmflow"
    team_name = "demo-team"

    class _FakeMonitor:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def events(self):
            return
            yield

    class _FakeRunner:
        @staticmethod
        async def get_agent_team_monitor(team_name: str, session_id: str, **kwargs):
            return _FakeMonitor()

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def get_monitor(sid: str):
            return None

        @staticmethod
        def register_monitor(sid: str, handler: object) -> None:
            pass

        @staticmethod
        def get_workflow_handler(sid: str):
            return registered_wf_handlers.get(sid)

        @staticmethod
        def register_workflow_handler(sid: str, handler: object) -> None:
            registered_wf_handlers[sid] = handler

    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda cid: _FakeManager())
    monkeypatch.setattr(team_helpers, "_consume_monitor_events", lambda *a: None)

    await team_helpers.ensure_monitor_handlers_for_active_runtime(
        channel_id, session_id, team_name, enable_swarmflow=False,
    )

    assert registered_wf_handlers.get(session_id) is None


@pytest.mark.anyio
async def test_consume_workflow_events_broadcasts_raw_for_tui(monkeypatch):
    """On the TUI channel _consume_workflow_events broadcasts workflow.updated as-is."""
    broadcasted: list[dict[str, object]] = []
    event = {
        "event_type": "workflow.updated",
        "session_id": "sess-wf-consume",
        "workflow": {"name": "test-flow", "status": "running"},
    }

    class _FakeWorkflowHandler:
        is_running = True

        async def events(self):
            yield event

    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *args: broadcasted.append(args[2]))

    handler = _FakeWorkflowHandler()
    await _TeamHelpersTestApi.consume_workflow_events(
        "tui", "sess-wf-consume", handler,
    )

    assert broadcasted == [event]


@pytest.mark.anyio
async def test_consume_workflow_events_converts_to_team_events_for_web(monkeypatch):
    """On a web channel _consume_workflow_events converts workflow.updated into team events."""
    broadcasted: list[dict[str, object]] = []
    event = {
        "event_type": "workflow.updated",
        "session_id": "sess-wf-web",
        "workflow": {
            "id": "run-9",
            "name": "test-flow",
            "status": "running",
            "phases": [
                {
                    "id": "planning-1",
                    "name": "planning",
                    "status": "running",
                    "agents": [
                        {"id": "researcher-1", "name": "researcher", "status": "running"}
                    ],
                }
            ],
        },
    }

    class _FakeWorkflowHandler:
        is_running = True

        async def events(self):
            yield event

    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *args: broadcasted.append(args[2]))

    handler = _FakeWorkflowHandler()
    await _TeamHelpersTestApi.consume_workflow_events(
        "web", "sess-wf-web", handler,
    )

    # No raw workflow.updated leaks to web; only team.* envelopes.
    assert broadcasted
    assert all(e["event_type"] in ("team.member", "team.task") for e in broadcasted)
    types = [e["event"]["type"] for e in broadcasted]
    assert "team.task.claimed" in types
    assert "team.member.spawned" in types


@pytest.mark.anyio
async def test_consume_stream_with_query_calls_ensure_workflow_handler_after_runtime_ready(monkeypatch):
    """After runtime_ready, calls ensure_workflow_handler_for_active_runtime if a team_agent is available."""
    calls: list[str] = []

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="team.runtime_ready",
            payload={
                "event_type": "team.runtime_ready",
                "team_name": "demo-team",
                "activation_kind": "create",
            },
            role=TeamRole.LEADER,
        )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    def _record_add_event_listener(cb):
        calls.append("add_event_listener")

    fake_team_agent = SimpleNamespace()
    fake_team_agent.add_event_listener = _record_add_event_listener

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def commit_runtime_ready(session_id: str, team_name: str) -> None:
            calls.append(f"commit:{session_id}:{team_name}")

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def pop_stream_task(session_id: str) -> None:
            pass

        @staticmethod
        def get_monitor(session_id: str):
            return None

        @staticmethod
        async def attach_distributed_hooks_for_runner_runtime(**kwargs) -> None:
            calls.append("hooks")

        @staticmethod
        def resolve_team_agent(session_id: str):
            return fake_team_agent

        @staticmethod
        def get_workflow_handler(session_id: str):
            return None

        @staticmethod
        def register_workflow_handler(session_id: str, handler: object) -> None:
            calls.append("register_workflow_handler")

        @staticmethod
        def register_stream_task(session_id: str, task: asyncio.Task) -> None:
            calls.append("register_stream_task")

    async def _fake_monitor(*args, **kwargs):
        calls.append("ensure_monitor_handlers")

    monkeypatch.setattr(
        team_helpers,
        "_run_agent_team_streaming",
        _FakeRunner.run_agent_team_streaming,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda cid: _FakeManager())
    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *a, **kw: None)
    monkeypatch.setattr(team_helpers, "sync_team_identity_metadata", lambda **kw: calls.append("sync"))
    monkeypatch.setattr(team_helpers, "ensure_monitor_handlers_for_active_runtime", _fake_monitor)
    monkeypatch.setattr(team_helpers, "ensure_team_evolution_watcher", lambda *a, **kw: calls.append("watcher"))
    monkeypatch.setattr(team_helpers, "get_session_metadata", lambda sid: {})
    monkeypatch.setattr(team_helpers, "update_session_metadata", lambda **kw: None)
    monkeypatch.setattr(team_helpers, "_consume_workflow_events", lambda *a: None)

    await _TeamHelpersTestApi.consume_stream_with_query(
        "web",
        "sess-wf-ready",
        SimpleNamespace(team_name="demo-team"),
        "hello",
    )

    # The merged function is called once for both monitor and workflow handler
    assert "ensure_monitor_handlers" in calls


class _CancellableStreamTask:
    """Minimal asyncio.Task stand-in: supports done/cancel/await like production code expects."""

    def __init__(self, *, cancelled: list[str], session_id: str) -> None:
        self._cancelled = cancelled
        self._session_id = session_id
        self._done = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self._done = True

    def __await__(self):
        async def _finish() -> None:
            if self._done:
                self._cancelled.append(self._session_id)
                raise asyncio.CancelledError()
            await asyncio.Event().wait()

        return _finish().__await__()


def _make_cancellable_stream_task(*, cancelled: list[str], session_id: str) -> _CancellableStreamTask:
    return _CancellableStreamTask(cancelled=cancelled, session_id=session_id)


@pytest.mark.anyio
async def test_try_finish_cron_team_stream_cancels_background_task(monkeypatch):
    """Cron waiter should end the team stream once workflow completes and leader reports."""
    _CronFakeManager.reset()
    channel_id = "tui"
    session_id = "sess-cron-finish"
    waiter_key = (channel_id, session_id)

    cancelled: list[str] = []
    processing_done: list[dict[str, object]] = []

    _CronFakeManager._stream_tasks[session_id] = _make_cancellable_stream_task(
        cancelled=cancelled, session_id=session_id,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda cid: _CronFakeManager)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda cid, sid, event: processing_done.append(event),
    )

    _TeamHelpersTestApi.seed_cron_team_waiter(waiter_key, "cron-job-1:123")

    _TeamHelpersTestApi.try_finish_cron_team_stream(
        channel_id,
        session_id,
        {
            "event_type": "chat.final",
            "content": "最终报告即将生成，请稍候。",
            "rid": 7,
        },
    )
    _TeamHelpersTestApi.try_finish_cron_team_stream(
        channel_id,
        session_id,
        {
            "event_type": "workflow.updated",
            "workflow": {"status": "completed"},
        },
    )
    await asyncio.sleep(0)
    assert cancelled == []

    _TeamHelpersTestApi.try_finish_cron_team_stream(
        channel_id,
        session_id,
        {
            "event_type": "chat.final",
            "content": "## 审查完成\n\n最终建议: approve",
            "rid": 7,
        },
    )
    await asyncio.sleep(0)

    assert cancelled == [session_id]
    assert processing_done[-1]["event_type"] == "chat.processing_status"
    assert processing_done[-1]["is_processing"] is False
    _TeamHelpersTestApi.clear_cron_team_waiter(waiter_key)


@pytest.mark.anyio
async def test_try_finish_cron_team_stream_on_leader_final_without_team_completed(monkeypatch):
    """Harness teams may emit chat.final without team.completed."""
    _CronFakeManager.reset()
    channel_id = "tui"
    session_id = "sess-cron-final-only"
    waiter_key = (channel_id, session_id)

    cancelled: list[str] = []
    processing_done: list[dict[str, object]] = []

    _CronFakeManager._stream_tasks[session_id] = _make_cancellable_stream_task(
        cancelled=cancelled, session_id=session_id,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda cid: _CronFakeManager)
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda cid, sid, event: processing_done.append(event),
    )
    monkeypatch.setattr(team_helpers, "_CRON_DELEGATION_GRACE_SECONDS", 0.0)

    _TeamHelpersTestApi.seed_cron_team_waiter(waiter_key, "cron-job-2:456")

    _TeamHelpersTestApi.try_finish_cron_team_stream(
        channel_id,
        session_id,
        {
            "event_type": "chat.final",
            "content": "## GLM vs DeepSeek\n\n对比汇总完成。",
            "rid": 3,
        },
    )
    await asyncio.sleep(0.05)

    assert cancelled == [session_id]
    assert processing_done[-1]["event_type"] == "chat.processing_status"
    assert processing_done[-1]["is_processing"] is False
    _TeamHelpersTestApi.clear_cron_team_waiter(waiter_key)


@pytest.mark.anyio
async def test_broadcast_team_state_snapshot_broadcasts_member_and_task_status(monkeypatch):
    broadcast_events: list[dict] = []

    class _FakeMonitorHandler:
        @staticmethod
        async def get_team_snapshot():
            return {
                "team_id": "team-snapshot-test",
                "members": [
                    {"member_id": "agent1", "status": "ready"},
                    {"member_id": "agent2", "status": "busy"},
                ],
                "tasks": [
                    {"task_id": "task-1", "status": "completed", "assignee": "agent1"},
                    {"task_id": "task-2", "status": "in_progress", "assignee": "agent2"},
                ],
            }

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_active_team_name(session_id: str):
            return ""

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda cid, sid, event: broadcast_events.append(event),
    )

    await team_helpers._broadcast_team_state_snapshot("web", "sess-snapshot-test")

    # Verify member snapshots
    member_events = [e for e in broadcast_events if e.get("event_type") == "team.member"]
    assert len(member_events) == 2
    assert member_events[0]["event"]["member_id"] == "agent1"
    assert member_events[0]["event"]["new_status"] == "ready"
    assert member_events[1]["event"]["member_id"] == "agent2"
    assert member_events[1]["event"]["new_status"] == "busy"

    # Verify task snapshots
    task_events = [e for e in broadcast_events if e.get("event_type") == "team.task"]
    assert len(task_events) == 2
    assert task_events[0]["event"]["task_id"] == "task-1"
    assert task_events[0]["event"]["status"] == "completed"
    assert task_events[1]["event"]["task_id"] == "task-2"
    assert task_events[1]["event"]["status"] == "in_progress"


@pytest.mark.anyio
async def test_broadcast_team_state_snapshot_noop_when_no_monitor(monkeypatch):
    broadcast_events: list[dict] = []

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda cid, sid, event: broadcast_events.append(event),
    )

    await team_helpers._broadcast_team_state_snapshot("web", "sess-no-monitor")
    assert broadcast_events == []


@pytest.mark.anyio
async def test_broadcast_team_state_snapshot_task_event_carries_title_content_and_flags(monkeypatch):
    """The ``team.task.status_snapshot`` event must carry title/content and, when
    the snapshot task dict carries truncation flags (added by
    ``get_team_snapshot`` in T2.1), pass them through via ``t.get(...)`` so they
    are present ONLY when the field was actually truncated (absent otherwise)."""
    broadcast_events: list[dict] = []

    long_title = "T" * 542  # > 512 → truncated
    short_content = "do the research"  # ≤ 512 → not truncated

    class _FakeMonitorHandler:
        @staticmethod
        async def get_team_snapshot():
            # Mirror the shape produced by TeamMonitorHandler.get_team_snapshot
            # after T2.1: title truncated + flags, content plain (no flags).
            return {
                "team_id": "team-snap-trunc",
                "members": [],
                "tasks": [
                    {
                        "task_id": "task-trunc",
                        "team_name": "team-snap-trunc",
                        "title": "T" * 512 + "…(truncated, total 542 chars)",
                        "title_truncated": True,
                        "title_original_size": 542,
                        "content": short_content,
                        "status": "in_progress",
                        "assignee": "agent1",
                        "updated_at": None,
                    },
                    {
                        "task_id": "task-plain",
                        "team_name": "team-snap-trunc",
                        "title": "plain title",
                        "content": "plain content",
                        "status": "completed",
                        "assignee": None,
                        "updated_at": None,
                    },
                ],
            }

    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id: str):
            return _FakeMonitorHandler()

        @staticmethod
        def get_active_team_name(session_id: str):
            return ""

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(
        team_helpers,
        "_broadcast_event",
        lambda cid, sid, event: broadcast_events.append(event),
    )
    # _persist_team_history_event hits a real DB path; stub it to a no-op so
    # the test stays isolated (production call unchanged).
    monkeypatch.setattr(team_helpers, "_persist_team_history_event", lambda *args, **kwargs: None)

    await team_helpers._broadcast_team_state_snapshot("web", "sess-snap-trunc")

    task_events = [e for e in broadcast_events if e.get("event_type") == "team.task"]
    assert len(task_events) == 2

    # Truncated task: title/content ride along, flags ride along (present).
    trunc_evt = task_events[0]["event"]
    assert trunc_evt["type"] == "team.task.status_snapshot"
    assert trunc_evt["task_id"] == "task-trunc"
    assert trunc_evt["title"] == "T" * 512 + "…(truncated, total 542 chars)"
    assert trunc_evt["content"] == short_content
    assert trunc_evt["title_truncated"] is True
    assert trunc_evt["title_original_size"] == 542
    # content was NOT truncated in the source dict, so the source has no
    # content_* flag keys. The broadcast uses ``t.get(...)`` (per spec) so
    # the event carries them as ``None`` (falsy) — the frontend treats them
    # as optional/truthy, so None is equivalent to "not truncated".
    assert trunc_evt["content_truncated"] is None
    assert trunc_evt["content_original_size"] is None
    # Non-body fields still present.
    assert trunc_evt["status"] == "in_progress"
    assert trunc_evt["assignee"] == "agent1"

    # Plain task: title/content ride along; flag keys present as None (t.get
    # on a source dict that omits them). Frontend reads truthiness → falsy.
    plain_evt = task_events[1]["event"]
    assert plain_evt["task_id"] == "task-plain"
    assert plain_evt["title"] == "plain title"
    assert plain_evt["content"] == "plain content"
    assert plain_evt["title_truncated"] is None
    assert plain_evt["title_original_size"] is None
    assert plain_evt["content_truncated"] is None
    assert plain_evt["content_original_size"] is None


# ---------------------------------------------------------------------------
# swarmflow workflow.updated -> web team.member / team.task conversion
# ---------------------------------------------------------------------------


def _wf_event(phases: list[dict], *, run_id: str = "run-1", name: str = "wf") -> dict:
    return {
        "event_type": "workflow.updated",
        "session_id": "sess-wf",
        "workflow": {"id": run_id, "name": name, "phases": phases},
    }


def test_workflow_updated_to_team_events_ignores_non_workflow_events():
    out = team_helpers._workflow_updated_to_team_events(
        {"event_type": "team.member", "event": {}}, "sess-wf", {}, {}, set()
    )
    assert out == []


def test_workflow_updated_to_team_events_planned_phase_creates_task():
    seen_phase, seen_agent, spawned = {}, {}, set()
    out = team_helpers._workflow_updated_to_team_events(
        _wf_event([{"id": "planning-1", "name": "planning", "status": "planned"}]),
        "sess-wf",
        seen_phase,
        seen_agent,
        spawned,
    )
    assert len(out) == 1
    ev = out[0]
    assert ev["event_type"] == "team.task"
    assert ev["session_id"] == "sess-wf"
    assert ev["event"]["type"] == "team.task.created"
    assert ev["event"]["task_id"] == "run-1:planning-1"
    assert ev["event"]["title"] == "planning"
    assert ev["event"]["team_id"] == "wf"


def test_workflow_updated_to_team_events_running_agent_spawns_member_and_claims_task():
    seen_phase, seen_agent, spawned = {}, {}, set()
    out = team_helpers._workflow_updated_to_team_events(
        _wf_event(
            [
                {
                    "id": "planning-1",
                    "name": "planning",
                    "status": "running",
                    "agents": [
                        {"id": "researcher-1", "name": "researcher", "status": "running"}
                    ],
                }
            ]
        ),
        "sess-wf",
        seen_phase,
        seen_agent,
        spawned,
    )
    types = [e["event"]["type"] for e in out]
    assert "team.task.claimed" in types
    assert "team.member.spawned" in types
    member = next(e for e in out if e["event"]["type"] == "team.member.spawned")
    assert member["event"]["member_id"] == "run-1:researcher-1"
    assert member["event"]["name"] == "researcher"
    # running agent should not also emit a status_changed
    assert "team.member.status_changed" not in types


def test_workflow_updated_to_team_events_dedups_repeated_running_delta():
    seen_phase, seen_agent, spawned = {}, {}, set()
    phases = [
        {
            "id": "planning-1",
            "name": "planning",
            "status": "running",
            "agents": [{"id": "researcher-1", "name": "researcher", "status": "running"}],
        }
    ]
    first = team_helpers._workflow_updated_to_team_events(
        _wf_event(phases), "sess-wf", seen_phase, seen_agent, spawned
    )
    assert first  # first delta emits events
    # Same delta again (e.g. another agent_started re-includes the running phase)
    second = team_helpers._workflow_updated_to_team_events(
        _wf_event(phases), "sess-wf", seen_phase, seen_agent, spawned
    )
    assert second == []  # no status change -> nothing re-emitted


def test_workflow_updated_to_team_events_agent_completed_changes_member_status():
    seen_phase, seen_agent, spawned = {}, {}, set()
    # First: agent running
    team_helpers._workflow_updated_to_team_events(
        _wf_event(
            [
                {
                    "id": "planning-1",
                    "name": "planning",
                    "status": "running",
                    "agents": [{"id": "researcher-1", "name": "researcher", "status": "running"}],
                }
            ]
        ),
        "sess-wf",
        seen_phase,
        seen_agent,
        spawned,
    )
    # Then: agent completed, phase completed
    out = team_helpers._workflow_updated_to_team_events(
        _wf_event(
            [
                {
                    "id": "planning-1",
                    "name": "planning",
                    "status": "completed",
                    "agents": [{"id": "researcher-1", "name": "researcher", "status": "completed"}],
                }
            ]
        ),
        "sess-wf",
        seen_phase,
        seen_agent,
        spawned,
    )
    types = [e["event"]["type"] for e in out]
    assert "team.task.completed" in types
    status_changed = next(e for e in out if e["event"]["type"] == "team.member.status_changed")
    assert status_changed["event"]["member_id"] == "run-1:researcher-1"
    assert status_changed["event"]["new_status"] == "completed"
    assert status_changed["event"]["old_status"] == "running"
    # already spawned -> no second spawn
    assert "team.member.spawned" not in types


def test_workflow_updated_to_team_events_first_sight_terminal_spawns_then_status():
    seen_phase, seen_agent, spawned = {}, {}, set()
    out = team_helpers._workflow_updated_to_team_events(
        _wf_event(
            [
                {
                    "id": "exec-1",
                    "name": "execution",
                    "status": "failed",
                    "agents": [{"id": "coder-1", "name": "coder", "status": "failed"}],
                }
            ]
        ),
        "sess-wf",
        seen_phase,
        seen_agent,
        spawned,
    )
    member_types = [e["event"]["type"] for e in out if e["event_type"] == "team.member"]
    assert member_types == ["team.member.spawned", "team.member.status_changed"]
    task = next(e for e in out if e["event_type"] == "team.task")
    assert task["event"]["type"] == "team.task.cancelled"


def test_persist_team_file_monitor_roots_replaces_stale_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """_persist_team_file_monitor_roots 应替换旧 root,而非累积合并。"""
    written: list[dict[str, Any]] = []
    write_kwargs: list[dict[str, Any]] = []

    def _fake_read_metadata(session_id: str, cache_bust: bool = False) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "team_file_monitor_roots": ["/old/stale/workspace"],
        }

    def _fake_enqueue_write(
        session_id: str, metadata: dict[str, Any], **kwargs: Any
    ) -> None:
        written.append(metadata)
        write_kwargs.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata._read_metadata",
        _fake_read_metadata,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata._enqueue_write",
        _fake_enqueue_write,
    )
    fake_home = Path("/team/home/unit-team")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers.team_home",
        lambda name: fake_home,
    )
    # independent_member_workspace 内部调用 openjiuwen 的 get_openjiuwen_home(),
    # 无法被 team_home patch 覆盖,这里显式 patch 以保持测试路径稳定。
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers.independent_member_workspace",
        lambda name: fake_home / f"{name}_workspace",
    )

    team_spec = SimpleNamespace(
        team_name="unit-team",
        workspace=SimpleNamespace(root_path="/team/home/unit-team/team-workspace"),
        agents={
            "worker": SimpleNamespace(
                workspace=SimpleNamespace(root_path="/team/home/unit-team/workspaces/worker_workspace"),
            ),
        },
    )
    team_helpers._persist_team_file_monitor_roots("sess-1", team_spec)

    assert len(written) == 1
    persisted_roots = written[0]["team_file_monitor_roots"]
    # 旧 root 应被清除
    assert "/old/stale/workspace" not in persisted_roots
    # 新 root 应包含 team-workspace 和 member workspace(路径经 resolve 归一化)
    home = fake_home.resolve()
    assert str(home / "team-workspace") in persisted_roots
    assert str(home / "workspaces") in persisted_roots
    # session_id 经 _safe_segment sanitize(此处 "sess-1" 无特殊字符,保持原样)
    assert str(home / "sessions" / "sess-1" / "worktrees") in persisted_roots
    assert str(home / "workspaces" / "worker_workspace") in persisted_roots
    # 漏洞4修复: independent_member_workspace 路径也应被收集
    assert str(home / "worker_workspace") in persisted_roots
    assert write_kwargs[0]["sync_write"] is True
    assert write_kwargs[0]["preserve_pin_fields"] is True


def test_persist_team_file_monitor_roots_noop_when_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """当 roots 未变化时不应触发写盘。"""
    written: list[dict[str, Any]] = []
    fake_home = Path("/team/home/unit-team")

    expected_roots = [
        str(fake_home.resolve() / "team-workspace"),
        str(fake_home.resolve() / "workspaces"),
        str(fake_home.resolve() / "sessions" / "sess-1" / "worktrees"),
        str(fake_home.resolve() / "workspaces" / "worker_workspace"),
        # 漏洞4修复: independent_member_workspace 路径
        str(fake_home.resolve() / "worker_workspace"),
    ]

    def _fake_read_metadata(session_id: str, cache_bust: bool = False) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "team_file_monitor_roots": list(expected_roots),
        }

    def _fake_enqueue_write(
        session_id: str, metadata: dict[str, Any], **kwargs: Any
    ) -> None:
        written.append(metadata)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata._read_metadata",
        _fake_read_metadata,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata._enqueue_write",
        _fake_enqueue_write,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers.team_home",
        lambda name: fake_home,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers.independent_member_workspace",
        lambda name: fake_home / f"{name}_workspace",
    )

    team_spec = SimpleNamespace(
        team_name="unit-team",
        workspace=SimpleNamespace(root_path="/team/home/unit-team/team-workspace"),
        agents={
            "worker": SimpleNamespace(
                workspace=SimpleNamespace(root_path="/team/home/unit-team/workspaces/worker_workspace"),
            ),
        },
    )
    team_helpers._persist_team_file_monitor_roots("sess-1", team_spec)

    assert len(written) == 0


@pytest.mark.anyio
async def test_consume_stream_injects_session_id_into_leader_ask_user_question(monkeypatch):
    """leader 的 chat.ask_user_question 透传事件必须带 session_id。

    relay 桥接提问/审批回答时优先取 payload.session_id；缺失会退化为
    sha256(userId+agentId+threadId) 哈希兜底，把回答寄到不存在的会话。
    """
    broadcasted: list[dict[str, Any]] = []

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def broadcast_event(session_id: str, event: dict) -> None:
            broadcasted.append(event)

        @staticmethod
        def get_monitor_handler(session_id: str):
            return None

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            pass

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(
            type="__interaction__",
            role=TeamRole.LEADER,
            payload={"id": "call_test_ask_1", "value": {}},
        )

    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _fake_stream)
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda chunk: {
            "event_type": "chat.ask_user_question",
            "request_id": "call_test_ask_1",
            "questions": [{"question": "q1", "options": []}],
            "source": "ask_user_interrupt",
        },
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    await _TeamHelpersTestApi.consume_stream_with_query(
        "officeclaw",
        "sess-ask-1",
        SimpleNamespace(team_name="spec-team"),
        "hello",
    )

    ask_events = [e for e in broadcasted if e.get("event_type") == "chat.ask_user_question"]
    assert len(ask_events) == 1
    assert ask_events[0]["session_id"] == "sess-ask-1"


@pytest.mark.anyio
async def test_consume_stream_does_not_inject_session_id_into_other_events(monkeypatch):
    """非 chat.ask_user_question 的透传事件不被注入 session_id（控制爆炸半径）。"""
    broadcasted: list[dict[str, Any]] = []

    class _FakeManager(_InactiveTeamRuntimeManagerMixin):
        @staticmethod
        def broadcast_event(session_id: str, event: dict) -> None:
            broadcasted.append(event)

        @staticmethod
        def get_monitor_handler(session_id: str):
            return None

        @staticmethod
        def clear_pending_runtime(session_id: str) -> None:
            pass

        @staticmethod
        def clear_active_runtime(session_id: str) -> None:
            pass

    async def _fake_stream(**kwargs):
        yield SimpleNamespace(type="llm_output", role=TeamRole.LEADER, payload="hi")

    monkeypatch.setattr(team_helpers, "_run_agent_team_streaming", _fake_stream)
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda chunk: {"event_type": "chat.delta", "content": "hi"},
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())

    await _TeamHelpersTestApi.consume_stream_with_query(
        "officeclaw",
        "sess-delta-1",
        SimpleNamespace(team_name="spec-team"),
        "hello",
    )

    delta_events = [e for e in broadcasted if e.get("event_type") == "chat.delta"]
    assert len(delta_events) == 1
    assert "session_id" not in delta_events[0]


@pytest.mark.anyio
async def test_round_complete_dedup_claim_then_reset():
    """claim_round_complete returns True once per round, False after; reset re-arms."""
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    # Lightest path: bypass __init__, set only the dict under test.
    # If test_team_manager_registry.py has a real fixture, prefer it.
    tm = TeamManager.__new__(TeamManager)
    tm._round_complete_emitted = {}

    assert tm.claim_round_complete("s1") is True   # first claim
    assert tm.claim_round_complete("s1") is False   # second claim same round → idempotent
    tm.reset_round_complete("s1")
    assert tm.claim_round_complete("s1") is True    # re-armed after reset
    tm.reset_round_complete("never-existed")        # no-op, no KeyError


@pytest.mark.anyio
async def test_finish_round_if_settled_broadcasts_once_when_settled(monkeypatch):
    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return False
    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )
        async def get_team_snapshot(self):
            return {"members": [{"member_id": "leader", "status": "ready"}], "tasks": []}
    class _FakeManager:
        _claimed = {}
        @staticmethod
        def get_monitor_handler(session_id):
            return _FakeMonitorHandler()
        @staticmethod
        def get_workflow_handler(session_id):
            return None
        @staticmethod
        def claim_round_complete(session_id):
            if _FakeManager._claimed.get(session_id):
                return False
            _FakeManager._claimed[session_id] = True
            return True
        @staticmethod
        def reset_round_complete(session_id):
            _FakeManager._claimed.pop(session_id, None)

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    broadcasts: list[dict] = []
    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *a, **k: broadcasts.append(k.get("payload") or a[-1]))

    finish = getattr(team_helpers, "_finish_round_if_settled")
    closed = await finish("officeclaw", "sess", 1, reason="test")
    assert closed is True
    is_complete = [p for p in broadcasts if isinstance(p, dict) and p.get("event_type") == "chat.processing_status" and p.get("is_complete")]
    assert len(is_complete) == 1


@pytest.mark.anyio
async def test_finish_round_if_settled_no_broadcast_when_not_settled(monkeypatch):
    class _FakeMonitorHandler:
        async def get_team_snapshot(self):
            return {"members": [{"member_id": "leader", "status": "busy"}], "tasks": []}
    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id):
            return _FakeMonitorHandler()
        @staticmethod
        def get_workflow_handler(session_id):
            return None
        @staticmethod
        def claim_round_complete(session_id):
            raise AssertionError("must not claim when not settled")

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    broadcasts: list[dict] = []
    monkeypatch.setattr(team_helpers, "_broadcast_event", lambda *a, **k: broadcasts.append(k.get("payload") or a[-1]))

    finish = getattr(team_helpers, "_finish_round_if_settled")
    closed = await finish("officeclaw", "sess", 1, reason="test")
    assert closed is False
    assert broadcasts == []


@pytest.mark.anyio
async def test_team_round_settled_empty_board_blocked_when_toggle_false(monkeypatch):
    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return False
    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )
        async def get_team_snapshot(self):
            return {"members": [{"member_id": "leader", "status": "ready"}], "tasks": []}
    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id):
            return _FakeMonitorHandler()
        @staticmethod
        def get_workflow_handler(session_id):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_taskless_completion_enabled", lambda session_id: False)

    assert not await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-toggle")


@pytest.mark.anyio
async def test_team_round_settled_empty_board_passes_when_toggle_true(monkeypatch):
    class _FakeMessageManager:
        @staticmethod
        async def has_unread_messages(*, include_broadcast: bool) -> bool:
            return False
    class _FakeMonitorHandler:
        _monitor = SimpleNamespace(
            _team_agent=SimpleNamespace(
                team_backend=SimpleNamespace(message_manager=_FakeMessageManager())
            )
        )
        async def get_team_snapshot(self):
            return {"members": [{"member_id": "leader", "status": "ready"}], "tasks": []}
    class _FakeManager:
        @staticmethod
        def get_monitor_handler(session_id):
            return _FakeMonitorHandler()
        @staticmethod
        def get_workflow_handler(session_id):
            return None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel_id: _FakeManager())
    monkeypatch.setattr(team_helpers, "_taskless_completion_enabled", lambda session_id: True)

    assert await _TeamHelpersTestApi.team_round_settled("officeclaw", "sess-toggle")
