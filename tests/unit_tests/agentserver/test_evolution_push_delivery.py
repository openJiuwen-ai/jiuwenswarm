# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeTransport:
    pushes: list[dict] = []

    def __init__(self):
        self.pushes = self.__class__.pushes

    async def send_push(self, payload: dict) -> None:
        self.pushes.append(payload)


def _approval_event(request_id: str = "team_skill_evolve_req1") -> SimpleNamespace:
    return SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": request_id, "questions": [{"header": "x"}]},
    )


def _outcome_event(status: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "outcome", "status": status},
            "content": message,
        },
    )


def _sdk_noop_outcome_event(skill_name: str = "powerpoint-pptx") -> SimpleNamespace:
    return SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "outcome",
                "rail_kind": "regular",
                "status": "no_evolution_no_records",
                "stage": "completed",
                "skill_name": skill_name,
                "source": "experience_updater",
            },
            "content": f"[Evolution] no applied updates for skill={skill_name}\n",
        },
    )


def _progress_event(content: str, *, stage: str | None = None) -> SimpleNamespace:
    payload = {"content": content}
    if stage is not None:
        payload["_evolution_meta"] = {"event_kind": "progress", "stage": stage}
    return SimpleNamespace(type="llm_reasoning", payload=payload)


class _FakeEvolutionRail:
    def __init__(self, batches: list[list[object]] | None = None) -> None:
        self._batches = list(
            batches
            if batches is not None
            else [[_approval_event(), _outcome_event("completed", "done")]]
        )
        self.drain_waits: list[bool] = []
        self.cleanup_calls = 0
        self.signal_trigger = True
        self.auto_save = False
        self.llm_updates: list[tuple[object, str | None]] = []

    def update_llm(self, model: object, model_name: str | None) -> None:
        self.llm_updates.append((model, model_name))

    async def drain_pending_approval_events(
        self,
        wait: bool = False,
        timeout: float | None = None,
    ):
        self.drain_waits.append(wait)
        if self._batches:
            return self._batches.pop(0)
        return []

    async def cleanup_background_tasks(self) -> None:
        self.cleanup_calls += 1


class _FakeApprovalRail:
    def __init__(
        self,
        *,
        request_id: str = "",
        record_ids: list[str] | None = None,
    ) -> None:
        self.approved: list[tuple[str, list[str] | None]] = []
        self.rejected: list[str] = []
        if record_ids is not None:
            self._pending_approval_snapshots = {
                request_id: SimpleNamespace(
                    payload=[SimpleNamespace(id=record_id) for record_id in record_ids],
                ),
            }

    async def approve_record(
        self,
        request_id: str,
        *,
        approved_record_ids: list[str] | None = None,
    ) -> None:
        self.approved.append((request_id, approved_record_ids))

    async def reject_record(self, request_id: str) -> None:
        self.rejected.append(request_id)


class _TestAdapter(JiuWenSwarmDeepAdapter):
    @classmethod
    def build_with_rail(cls, rail: _FakeEvolutionRail) -> "_TestAdapter":
        adapter = object.__new__(cls)
        setattr(adapter, "_skill_evolution_rail", rail)
        return adapter

    async def watch_evolution_and_push(
        self,
        request_id: str,
        channel_id: str,
        session_id: str,
    ) -> None:
        watcher = getattr(self, "_watch_evolution_and_push")
        await watcher(request_id, channel_id, session_id)


@pytest.mark.asyncio
async def test_normal_evolution_watcher_skips_status_when_signal_trigger_disabled(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail()
    rail.signal_trigger = False
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-disabled")

    assert _FakeTransport.pushes == []
    assert rail.drain_waits == []
    assert rail.cleanup_calls == 0


@pytest.mark.asyncio
async def test_normal_evolution_watcher_skips_status_when_auto_save_enabled(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail()
    rail.auto_save = True
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-auto-save")

    assert _FakeTransport.pushes == []
    assert rail.drain_waits == []
    assert rail.cleanup_calls == 0


@pytest.mark.asyncio
async def test_normal_evolution_watcher_uses_delivery_context_metadata(monkeypatch):
    _FakeTransport.pushes = []
    adapter = _TestAdapter.build_with_rail(_FakeEvolutionRail())

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
        interface_deep_module,
        "build_server_push_message",
        _fake_build_server_push_message,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-normal")

    assert recorded_calls
    assert all(call["session_id"] == "sess-normal" for call in recorded_calls)
    assert all(call["fallback_channel_id"] == "web" for call in recorded_calls)
    assert _FakeTransport.pushes
    assert all(
        push["metadata"] == {"route": "from-delivery-context"}
        for push in _FakeTransport.pushes
    )


@pytest.mark.asyncio
async def test_normal_evolution_watcher_does_not_push_status_without_sdk_events(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail([])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-no-events")

    assert _FakeTransport.pushes == []
    assert rail.drain_waits
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_uses_sdk_timeout_before_legacy_fallback(monkeypatch):
    class _SdkTimeoutRail(_FakeEvolutionRail):
        @property
        def evolution_total_timeout_secs(self) -> float:
            return 0.01

    _FakeTransport.pushes = []
    rail = _SdkTimeoutRail([[_progress_event("generating", stage="generating_updates")]])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 100.0)
    monkeypatch.setattr(evolution_helpers, "TEAM_EVOLUTION_EVENT_TIMEOUT_GRACE_SEC", 0.001)

    await asyncio.wait_for(
        adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-timeout"),
        timeout=0.2,
    )

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert status_pushes[-1]["payload"]["stage"] == "hidden"
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_maps_sdk_progress_stages(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail(
        [
            [_progress_event("detecting", stage="detecting_signals")],
            [_progress_event("generating", stage="generating_updates")],
            [_outcome_event("completed", "done")],
        ]
    )
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-stages")

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


@pytest.mark.asyncio
async def test_normal_evolution_watcher_ends_on_cancelled_progress(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail([[_progress_event("no skill usage detected", stage="cancelled")]])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-cancelled")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_does_not_start_for_no_signal_scan(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail(
        [
            [_progress_event("starting regular skill evolution review", stage="started")],
            [
                _progress_event(
                    "checking regular skill(s) for evolution signals",
                    stage="detecting_signals",
                )
            ],
            [
                _progress_event(
                    "no skill usage of a regular skill or actionable evolution signal detected",
                    stage="cancelled",
                )
            ],
        ]
    )
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-no-signal-scan")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_ends_on_auto_approved_progress(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail([[_progress_event("auto saved", stage="auto_approved")]])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-auto")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert [push["payload"]["stage"] for push in status_pushes] == [
        "completed",
        "completed",
    ]
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_reads_outcome_status_from_metadata(monkeypatch):
    _FakeTransport.pushes = []
    adapter = _TestAdapter.build_with_rail(
        _FakeEvolutionRail([[_outcome_event("failed", "failed")]])
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-normal-failed")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []


@pytest.mark.asyncio
async def test_normal_evolution_watcher_hides_sdk_noop_outcome_without_prior_generation(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail([[_sdk_noop_outcome_event()]])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-noop-only")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_ends_sdk_noop_outcome_after_generation(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail(
        [
            [_progress_event("generating", stage="generating_updates")],
            [_sdk_noop_outcome_event()],
        ]
    )
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-sdk-noop-after-generation")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert [push["payload"]["status"] for push in status_pushes] == ["start", "end"]
    assert [push["payload"]["stage"] for push in status_pushes] == [
        "generating",
        "no_evolution_no_records",
    ]
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_pushes_passive_progress_before_approval(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail(
        [
            [_progress_event("evolution progress")],
            [_approval_event("skill_evolve_progress_req")],
        ]
    )
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-progress")

    event_types = [push["payload"]["event_type"] for push in _FakeTransport.pushes]
    assert event_types == [
        "chat.reasoning",
        "chat.evolution_status",
        "chat.ask_user_question",
        "chat.evolution_status",
    ]
    assert _FakeTransport.pushes[0]["payload"]["content"] == "evolution progress"
    assert _FakeTransport.pushes[1]["payload"]["status"] == "start"
    assert _FakeTransport.pushes[1]["payload"]["stage"] == "approval_required"
    assert _FakeTransport.pushes[2]["payload"]["request_id"] == "skill_evolve_progress_req"
    assert _FakeTransport.pushes[3]["payload"]["status"] == "end"
    assert _FakeTransport.pushes[3]["payload"]["stage"] == "approval_required"
    assert rail.cleanup_calls == 1
    assert rail.drain_waits
    assert set(rail.drain_waits) == {False}


@pytest.mark.asyncio
async def test_normal_evolution_watcher_times_out_after_idle_progress(monkeypatch):
    _FakeTransport.pushes = []
    rail = _FakeEvolutionRail([[_progress_event("evolution progress")]])
    adapter = _TestAdapter.build_with_rail(rail)

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_IDLE_SLEEP_SEC", 0.001)
    monkeypatch.setattr(interface_deep_module, "TEAM_EVOLUTION_EVENT_TIMEOUT_SEC", 0.01)

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-timeout")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []
    assert rail.cleanup_calls == 1


@pytest.mark.asyncio
async def test_normal_evolution_watcher_hides_timed_out_terminal_progress(monkeypatch):
    _FakeTransport.pushes = []
    adapter = _TestAdapter.build_with_rail(
        _FakeEvolutionRail([[_progress_event("timed out", stage="timed_out")]])
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    await adapter.watch_evolution_and_push("stream-rid", "web", "sess-terminal-timeout")

    status_pushes = [
        push for push in _FakeTransport.pushes
        if push["payload"]["event_type"] == "chat.evolution_status"
    ]
    assert status_pushes == []


@pytest.mark.asyncio
async def test_team_skill_evolve_approval_keeps_legacy_whole_request_without_record_ids(monkeypatch):
    rail = _FakeApprovalRail()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "find_team_skill_rail",
        staticmethod(lambda request_id, channel_id=None: rail),
    )
    handled = await adapter.handle_team_skill_evolve_approval(
        "team_skill_evolve_req1",
        [{"selected_options": ["accept"]}],
        session_id="sess-1",
        channel_id="web",
    )

    assert handled is True
    assert rail.approved == [("team_skill_evolve_req1", None)]
    assert rail.rejected == []


@pytest.mark.asyncio
async def test_team_skill_evolve_approval_passes_selected_record_ids(monkeypatch):
    rail = _FakeApprovalRail(
        request_id="team_skill_evolve_req1",
        record_ids=["team-rec-1", "team-rec-2", "team-rec-3"],
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "find_team_skill_rail",
        staticmethod(lambda request_id, channel_id=None: rail),
    )
    handled = await adapter.handle_team_skill_evolve_approval(
        "team_skill_evolve_req1",
        [
            {"selected_options": ["accept"]},
            {"selected_options": ["reject"]},
            {"selected_options": ["接收"]},
        ],
        session_id="sess-1",
        channel_id="web",
    )

    assert handled is True
    assert rail.approved == [("team_skill_evolve_req1", ["team-rec-1", "team-rec-3"])]
    assert rail.rejected == []


@pytest.mark.asyncio
async def test_team_skill_evolve_approval_pushes_terminal_status(monkeypatch):
    _FakeTransport.pushes = []
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "find_team_skill_rail",
        staticmethod(lambda request_id, channel_id=None: _FakeApprovalRail()),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.gateway_push.WebSocketGatewayPushTransport",
        _FakeTransport,
    )

    handled = await adapter.handle_team_skill_evolve_approval(
        "team_skill_evolve_req1",
        [{"selected_options": ["接收"]}],
        session_id="sess-1",
        channel_id="web",
    )

    assert handled is True
    assert _FakeTransport.pushes == [
        {
            "session_id": "sess-1",
            "request_id": "team_skill_evolve_req1",
            "channel_id": "web",
            "payload": {
                "event_type": "chat.evolution_status",
                "request_id": "team_skill_evolve_req1",
                "status": "end",
                "stage": "completed",
                "message": "Team skill evolution accepted",
            },
        }
    ]
