# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers


def test_evolution_helpers_parse_approval_and_outcome_events():
    approval = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_req1", "questions": [{"header": "x"}]},
    )
    outcome = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "outcome", "status": "completed"},
            "content": "done",
        },
    )

    assert evolution_helpers.is_evolution_approval_event(approval) is True
    assert evolution_helpers.evolution_event_kind(outcome) == "outcome"
    assert evolution_helpers.is_evolution_outcome_event(outcome) is True
    assert evolution_helpers.evolution_outcome_from_event(outcome) == {
        "status": "completed",
        "message": "done",
    }
    assert evolution_helpers.extract_evolution_request_id(approval) == "team_skill_evolve_req1"


def test_evolution_helpers_parse_noop_outcome_status_from_sdk_metadata():
    outcome = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "outcome",
                "rail_kind": "regular",
                "status": "no_evolution_no_records",
                "stage": "completed",
                "skill_name": "powerpoint-pptx",
                "source": "experience_updater",
            },
            "content": "[Evolution] no applied updates for skill=powerpoint-pptx\n",
        },
    )

    assert evolution_helpers.evolution_outcome_from_event(outcome) == {
        "status": "no_evolution_no_records",
        "message": "[Evolution] no applied updates for skill=powerpoint-pptx\n",
    }


@pytest.mark.parametrize(
    ("rail", "expected"),
    [
        (SimpleNamespace(evolution_total_timeout_secs=600), 605),
        (object(), 900),
    ],
)
def test_resolve_evolution_event_timeout_matches_sdk_budget(rail, expected):
    assert evolution_helpers.resolve_evolution_event_timeout_sec(
        rail,
        fallback_sec=900,
        grace_sec=5,
    ) == expected


def test_evolution_helpers_extract_request_id_from_evolution_meta():
    progress = SimpleNamespace(
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

    assert evolution_helpers.extract_evolution_request_id(progress) == "team_skill_evolve_meta"


@pytest.mark.parametrize(
    ("raw_stage", "expected_stage", "expected_terminal"),
    [
        ("started", "detecting", False),
        ("detecting_signals", "detecting", False),
        ("staging", "generating", False),
        ("generating_updates", "generating", False),
        ("approval_required", "approval_required", False),
        ("auto_approved", "completed", True),
        ("cancelled", "hidden", True),
        ("completed", "completed", True),
        ("failed", "failed", True),
    ],
)
def test_evolution_helpers_normalize_sdk_progress_stages(
    raw_stage: str,
    expected_stage: str,
    expected_terminal: bool,
):
    progress = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {
                "event_kind": "progress",
                "stage": raw_stage,
                "request_id": "skill_evolve_req1",
            },
            "content": f"[Skill Evolution] {raw_stage}",
        },
    )

    status = evolution_helpers.evolution_progress_status_from_event(progress)

    assert status is not None
    assert status.stage == expected_stage
    assert status.message == f"[Skill Evolution] {raw_stage}"
    assert status.request_id == "skill_evolve_req1"
    assert status.terminal is expected_terminal


@pytest.mark.parametrize("stage", ["failed", "timed_out"])
def test_evolution_helpers_hide_failed_and_timed_out_team_status_updates(stage: str):
    update = evolution_helpers.team_evolution_end_update(
        "team_skill_evolve_req1",
        {"stage": stage, "message": "boom"},
    )

    assert update.request_id == "team_skill_evolve_req1"
    assert update.status == "end"
    assert update.stage == "hidden"
    assert update.message == "boom"


def test_evolution_helpers_map_noop_progress_to_no_evolution_generated():
    progress = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
            "content": "No evolution signals detected",
        },
    )

    terminal = evolution_helpers.team_evolution_terminal_progress(progress)
    update = evolution_helpers.team_evolution_end_update(
        "team_skill_evolve_req1",
        terminal,
    )

    assert terminal == {
        "status": "completed",
        "stage": "no_evolution_no_signal",
        "message": "No evolution signals detected",
    }
    assert update.status == "end"
    assert update.stage == "no_evolution_no_signal"
    assert update.message == "No evolution signals detected"


@pytest.mark.parametrize(
    ("content", "expected_stage"),
    [
        ("no skill usage of a regular skill detected", "no_evolution_no_skill"),
        ("no actionable evolution signals detected", "no_evolution_no_signal"),
        ("no evolution records generated", "no_evolution_no_records"),
    ],
)
def test_evolution_helpers_map_noop_reasons_to_specific_stages(
    content: str,
    expected_stage: str,
):
    progress = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
            "content": content,
        },
    )

    terminal = evolution_helpers.team_evolution_terminal_progress(progress)

    assert terminal is not None
    assert terminal["stage"] == expected_stage


def test_evolution_helpers_map_cancelled_progress_to_hidden():
    progress = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "cancelled"},
            "content": "no skill usage of a regular skill detected",
        },
    )

    terminal = evolution_helpers.team_evolution_terminal_progress(progress)
    update = evolution_helpers.team_evolution_end_update(
        "skill_evolve_req1",
        terminal,
    )

    assert terminal == {
        "status": "hidden",
        "stage": "hidden",
        "message": "no skill usage of a regular skill detected",
    }
    assert update.status == "end"
    assert update.stage == "hidden"


def test_evolution_helpers_regular_start_progress_excludes_scan_and_noop_stages():
    events = [
        SimpleNamespace(
            type="llm_reasoning",
            payload={
                "_evolution_meta": {"event_kind": "progress", "stage": "detecting_signals"},
                "content": "checking regular skill(s) for evolution signals",
            },
        ),
        SimpleNamespace(
            type="llm_reasoning",
            payload={
                "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
                "content": "no evolution records generated",
            },
        ),
        SimpleNamespace(
            type="llm_reasoning",
            payload={
                "_evolution_meta": {"event_kind": "progress", "stage": "generating_updates"},
                "content": "generating evolution records",
            },
        ),
    ]

    visible = evolution_helpers.visible_evolution_progress_from_events(events)
    regular_start = evolution_helpers.visible_regular_evolution_start_progress(visible)

    assert [progress.stage for progress in visible] == [
        "no_evolution_no_records",
        "generating",
    ]
    assert [progress.stage for progress in regular_start] == ["generating"]


def test_evolution_helpers_group_approvals_skips_missing_request_ids():
    missing_request_id = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"questions": [{"header": "missing"}]},
    )
    real_request_id = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_real", "questions": [{"header": "real"}]},
    )
    skipped_stream = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "progress"},
    )
    warnings: list[str] = []

    grouped, missing_request_ids = evolution_helpers.group_evolution_approvals(
        "sess-1",
        [missing_request_id, skipped_stream, real_request_id],
        warn_missing_request_id=lambda session_id: warnings.append(session_id),
    )

    assert missing_request_ids == []
    assert warnings == ["sess-1"]
    assert list(grouped) == ["team_skill_evolve_real"]
    assert grouped["team_skill_evolve_real"] == [real_request_id]


def test_evolution_helpers_resolve_approved_record_ids_by_answer_index():
    accepted, approved_ids = evolution_helpers.approved_record_ids_from_answers(
        [
            {"selected_options": ["接收"]},
            {"selected_options": ["拒绝"]},
            {"selected_options": ["Accept"]},
        ],
        evolution_helpers.EVOLUTION_ACCEPT_LABELS,
        ["rec-1", "rec-2", "rec-3"],
    )

    assert accepted is True
    assert approved_ids == ["rec-1", "rec-3"]


def test_evolution_helpers_preserve_legacy_whole_request_without_record_ids():
    accepted, approved_ids = evolution_helpers.approved_record_ids_from_answers(
        [{"selected_options": ["接收"]}],
        evolution_helpers.EVOLUTION_ACCEPT_LABELS,
    )

    assert accepted is True
    assert approved_ids is None


@pytest.mark.parametrize("label", ["allow_once", "allow_always", "本次允许", "总是允许"])
def test_evolution_helpers_accept_interrupt_approval_labels(label: str):
    accepted, approved_ids = evolution_helpers.approved_record_ids_from_answers(
        [{"selected_options": [label]}],
        evolution_helpers.EVOLUTION_ACCEPT_LABELS,
    )

    assert accepted is True
    assert approved_ids is None


@pytest.mark.parametrize("label", ["reject", "拒绝"])
def test_evolution_helpers_do_not_accept_reject_labels(label: str):
    accepted, approved_ids = evolution_helpers.approved_record_ids_from_answers(
        [{"selected_options": [label]}],
        evolution_helpers.EVOLUTION_ACCEPT_LABELS,
    )

    assert accepted is False
    assert approved_ids == []


def test_evolution_helpers_do_not_whole_approve_when_snapshot_id_is_missing():
    accepted, approved_ids = evolution_helpers.approved_record_ids_from_answers(
        [{"selected_options": ["接收"]}],
        evolution_helpers.EVOLUTION_ACCEPT_LABELS,
        [""],
    )

    assert accepted is True
    assert approved_ids == []


def test_evolution_helpers_extract_record_ids_from_pending_snapshot():
    rail = SimpleNamespace(
        _pending_approval_snapshots={
            "skill_evolve_req1": SimpleNamespace(
                payload=[
                    SimpleNamespace(id="rec-1"),
                    SimpleNamespace(id="rec-2"),
                ],
            ),
        },
    )

    assert evolution_helpers.record_ids_from_pending_approval(
        rail,
        "skill_evolve_req1",
    ) == ["rec-1", "rec-2"]


def test_evolution_helpers_builds_team_cycle_request_id():
    assert (
        evolution_helpers.make_team_evolution_cycle_request_id("sess-1", 2)
        == "team_evolve_sess-1_2"
    )


@pytest.mark.asyncio
async def test_evolution_helpers_push_status_can_omit_payload_request_id():
    pushes: list[dict] = []

    class _Transport:
        @staticmethod
        async def send_push(payload: dict) -> None:
            pushes.append(payload)

    def _build_push_message(**kwargs):
        return kwargs

    await evolution_helpers.push_evolution_status(
        evolution_helpers.EvolutionPushContext(
            transport=_Transport(),
            channel_id="web",
            session_id="sess-1",
        ),
        evolution_helpers.EvolutionStatusUpdate(
            request_id="stream-rid",
            status="start",
            stage="collecting",
            message="started",
        ),
        _build_push_message,
        include_payload_request_id=False,
    )

    assert pushes == [
        {
            "session_id": "sess-1",
            "request_id": "stream-rid",
            "fallback_channel_id": "web",
            "payload": {
                "event_type": "chat.evolution_status",
                "status": "start",
                "stage": "collecting",
                "message": "started",
            },
        }
    ]


@pytest.mark.asyncio
async def test_evolution_helpers_broadcast_progress_skips_non_stream_evolution_events():
    approval = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "team_skill_evolve_req1"},
    )
    outcome = SimpleNamespace(
        type="chat.evolution_status",
        payload={"_evolution_meta": {"event_kind": "outcome"}, "message": "done"},
    )
    terminal = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
            "content": "No evolution signals detected",
        },
    )
    stream = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "thinking"},
    )
    broadcasts: list[tuple[str | None, str, dict]] = []

    await evolution_helpers.broadcast_evolution_progress(
        "web",
        "sess-1",
        [approval, outcome, terminal, stream],
        parse_stream_chunk=lambda evt: {
            "event_type": "chat.reasoning",
            "content": evt.payload["content"],
        },
        broadcast_event=lambda channel_id, session_id, payload: broadcasts.append(
            (channel_id, session_id, payload)
        ),
    )

    assert broadcasts == [
        (
            "web",
            "sess-1",
            {"event_type": "chat.reasoning", "content": "thinking"},
        )
    ]


@pytest.mark.asyncio
async def test_evolution_helpers_push_progress_skips_non_stream_evolution_events():
    approval = SimpleNamespace(
        type="chat.ask_user_question",
        payload={"request_id": "skill_evolve_req1"},
    )
    outcome = SimpleNamespace(
        type="chat.evolution_status",
        payload={"_evolution_meta": {"event_kind": "outcome"}, "message": "done"},
    )
    terminal = SimpleNamespace(
        type="llm_reasoning",
        payload={
            "_evolution_meta": {"event_kind": "progress", "stage": "completed"},
            "content": "No evolution signals detected",
        },
    )
    stream = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": "thinking"},
    )
    ignored = SimpleNamespace(
        type="llm_reasoning",
        payload={"content": ""},
    )
    pushes: list[dict] = []

    class _Transport:
        @staticmethod
        async def send_push(payload: dict) -> None:
            pushes.append(payload)

    def _build_push_message(**kwargs):
        return kwargs

    await evolution_helpers.push_evolution_progress(
        evolution_helpers.EvolutionPushContext(
            transport=_Transport(),
            channel_id="web",
            session_id="sess-1",
        ),
        "stream-rid",
        [approval, outcome, terminal, stream, ignored],
        parse_stream_chunk=lambda evt: (
            None
            if not evt.payload.get("content")
            else {"event_type": "chat.reasoning", "content": evt.payload["content"]}
        ),
        build_push_message=_build_push_message,
    )

    assert pushes == [
        {
            "session_id": "sess-1",
            "request_id": "stream-rid",
            "fallback_channel_id": "web",
            "payload": {"event_type": "chat.reasoning", "content": "thinking"},
        }
    ]
