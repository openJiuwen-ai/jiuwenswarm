"""Evolution approval coordinator unit tests."""

from types import SimpleNamespace

from jiuwenswarm.gateway.message_handler.evolution_approval import (
    SKILL_EVOLUTION_APPROVAL_SCHEMA,
    SKILL_EVOLUTION_APPROVAL_SOURCE,
    EvolutionApprovalCoordinator,
    ensure_regular_evolution_approval_metadata,
    is_evolution_approval_payload,
    is_interrupt_evolution_approval_answer_payload,
)


_INTERRUPT_META = {
    "event_kind": "approval",
    "rail_kind": "regular",
    "approval_kind": "evolve",
    "approval_transport": "interrupt",
}


def _chunk(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        channel_id="web",
        request_id="stream-1",
        payload=payload,
    )


def _approval_chunk(request_id: str, **extra: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [{"header": "x"}],
        **extra,
    }
    return _chunk(payload)


def test_approval_payload_recognizes_request_id_and_metadata() -> None:
    assert is_evolution_approval_payload({"request_id": "skill_evolve_1"}) is True
    assert is_evolution_approval_payload({"request_id": "team_skill_evolve_1"}) is True
    assert is_evolution_approval_payload({"request_id": "call_1"}) is False
    assert is_evolution_approval_payload({"source": SKILL_EVOLUTION_APPROVAL_SOURCE}) is True
    assert is_evolution_approval_payload({"approval_schema": SKILL_EVOLUTION_APPROVAL_SCHEMA}) is True
    assert is_evolution_approval_payload({"evolution_meta": {"event_kind": "approval"}}) is True
    assert is_evolution_approval_payload("not-a-payload") is False
    assert is_interrupt_evolution_approval_answer_payload({
        "request_id": "call_1",
        "source": SKILL_EVOLUTION_APPROVAL_SOURCE,
    }) is True


def test_regular_metadata_enrichment_preserves_existing_transport() -> None:
    enriched = ensure_regular_evolution_approval_metadata({
        "request_id": "call_123",
        "evolution_meta": {"approval_transport": "interrupt"},
    })

    assert enriched["source"] == SKILL_EVOLUTION_APPROVAL_SOURCE
    assert enriched["approval_schema"] == SKILL_EVOLUTION_APPROVAL_SCHEMA
    assert enriched["evolution_meta"] == _INTERRUPT_META
    assert is_interrupt_evolution_approval_answer_payload(enriched) is True


def test_evolution_status_start_and_end_tracks_in_progress() -> None:
    coordinator = EvolutionApprovalCoordinator()

    coordinator.handle_chunk(
        _chunk({"event_type": "chat.evolution_status", "status": "start"}),
        "sess-1",
        auto_save_enabled=True,
    )
    assert coordinator.is_session_in_progress("sess-1") is True

    coordinator.handle_chunk(
        _chunk({"event_type": "chat.evolution_status", "status": "end"}),
        "sess-1",
        auto_save_enabled=True,
    )
    assert coordinator.is_session_in_progress("sess-1") is False


def test_auto_save_false_incoming_approval_marks_pending() -> None:
    coordinator = EvolutionApprovalCoordinator()

    decision = coordinator.handle_chunk(
        _approval_chunk(
            "call_123",
            source=SKILL_EVOLUTION_APPROVAL_SOURCE,
            approval_schema=SKILL_EVOLUTION_APPROVAL_SCHEMA,
        ),
        "sess-1",
        auto_save_enabled=False,
    )

    assert decision.should_publish_chunk is True
    assert decision.user_message is None
    assert coordinator.pending_request_id("sess-1") == "call_123"


def test_auto_save_true_auto_accepts_first_approval_and_suppresses_chunk() -> None:
    coordinator = EvolutionApprovalCoordinator()

    decision = coordinator.handle_chunk(
        _approval_chunk("team_skill_evolve_new"),
        "sess-1",
        {"k": "v"},
        auto_save_enabled=True,
    )

    assert decision.should_publish_chunk is False
    assert coordinator.pending_request_id("sess-1") is None
    assert decision.user_message is not None
    auto_msg = decision.user_message
    assert auto_msg.session_id == "sess-1"
    assert auto_msg.channel_id == "web"
    assert auto_msg.metadata == {"k": "v"}
    assert auto_msg.params["request_id"] == "team_skill_evolve_new"
    assert auto_msg.params["answers"] == [{"selected_options": ["接收"]}]


def test_auto_save_true_auto_accepts_incoming_approval_with_existing_pending() -> None:
    coordinator = EvolutionApprovalCoordinator()
    coordinator.mark_pending("sess-1", "team_skill_evolve_old")

    decision = coordinator.handle_chunk(
        _approval_chunk("team_skill_evolve_new"),
        "sess-1",
        {"k": "v"},
        auto_save_enabled=True,
    )

    assert decision.should_publish_chunk is False
    assert coordinator.pending_request_id("sess-1") == "team_skill_evolve_old"
    assert decision.user_message is not None
    auto_msg = decision.user_message
    assert auto_msg.session_id == "sess-1"
    assert auto_msg.channel_id == "web"
    assert auto_msg.metadata == {"k": "v"}
    assert auto_msg.params["request_id"] == "team_skill_evolve_new"
    assert auto_msg.params["answers"] == [{"selected_options": ["接收"]}]


def test_auto_save_true_does_not_auto_accept_interrupt_approval() -> None:
    coordinator = EvolutionApprovalCoordinator()

    decision = coordinator.handle_chunk(
        _approval_chunk(
            "call_123",
            source=SKILL_EVOLUTION_APPROVAL_SOURCE,
            approval_schema=SKILL_EVOLUTION_APPROVAL_SCHEMA,
        ),
        "sess-1",
        {"k": "v"},
        auto_save_enabled=True,
    )

    assert decision.should_publish_chunk is True
    assert decision.user_message is None
    assert coordinator.pending_request_id("sess-1") == "call_123"


def test_hidden_regular_auto_save_finish_clears_without_current_pending() -> None:
    coordinator = EvolutionApprovalCoordinator()
    decision = coordinator.handle_chunk(
        _approval_chunk("team_skill_evolve_new"),
        "sess-1",
        auto_save_enabled=True,
    )

    assert decision.should_publish_chunk is False

    result = coordinator.finish_if_current("sess-1", "team_skill_evolve_new")

    assert result is not None
    assert result.queued_supplement is None
    assert result.promoted_approval is None
    assert coordinator.pending_request_id("sess-1") is None


def test_auto_save_false_defers_replaced_approval_and_suppresses_chunk() -> None:
    coordinator = EvolutionApprovalCoordinator()
    coordinator.mark_pending("sess-1", "team_skill_evolve_old")

    decision = coordinator.handle_chunk(
        _approval_chunk("team_skill_evolve_new"),
        "sess-1",
        auto_save_enabled=False,
    )

    assert decision.should_publish_chunk is False
    assert decision.user_message is None
    assert coordinator.pending_request_id("sess-1") == "team_skill_evolve_old"
    assert coordinator.deferred_request_ids("sess-1") == ["team_skill_evolve_new"]


def test_finish_current_clears_state_and_releases_queued_supplement() -> None:
    coordinator = EvolutionApprovalCoordinator()
    coordinator.mark_pending("sess-1", "call_123")
    coordinator.mark_session_in_progress("sess-1")
    coordinator.queue_supplement("sess-1", "继续补充", [{"path": "a.py"}])

    result = coordinator.finish_if_current("sess-1", "call_123")

    assert result is not None
    assert result.queued_supplement == {"new_input": "继续补充", "attachments": [{"path": "a.py"}]}
    assert result.promoted_approval is None
    assert coordinator.pending_request_id("sess-1") is None
    assert coordinator.is_session_in_progress("sess-1") is False
    assert coordinator.queued_supplement("sess-1") is None


def test_stale_finish_keeps_current_pending_and_queued_supplement() -> None:
    coordinator = EvolutionApprovalCoordinator()
    coordinator.mark_pending("sess-1", "call_new")
    coordinator.mark_session_in_progress("sess-1")
    coordinator.queue_supplement("sess-1", "继续补充")

    queued = coordinator.finish_if_current("sess-1", "call_old")

    assert queued is None
    assert coordinator.pending_request_id("sess-1") == "call_new"
    assert coordinator.is_session_in_progress("sess-1") is True
    assert coordinator.queued_supplement("sess-1") == {"new_input": "继续补充"}


def test_finish_current_promotes_deferred_approval() -> None:
    coordinator = EvolutionApprovalCoordinator()
    coordinator.mark_pending("sess-1", "team_skill_evolve_old")
    coordinator.handle_chunk(
        _approval_chunk("team_skill_evolve_new", questions=[{"header": "deferred"}]),
        "sess-1",
        {"k": "v"},
        auto_save_enabled=False,
    )
    coordinator.queue_supplement("sess-1", "继续补充")

    result = coordinator.finish_if_current("sess-1", "team_skill_evolve_old")

    assert result is not None
    assert result.queued_supplement is None
    assert result.promoted_approval is not None
    assert result.promoted_approval.request_id == "team_skill_evolve_new"
    assert result.promoted_approval.channel_id == "web"
    assert result.promoted_approval.payload["questions"] == [{"header": "deferred"}]
    assert result.promoted_approval.metadata == {"k": "v"}
    assert coordinator.pending_request_id("sess-1") == "team_skill_evolve_new"
    assert coordinator.is_session_in_progress("sess-1") is True
    assert coordinator.deferred_request_ids("sess-1") == []
    assert coordinator.queued_supplement("sess-1") == {"new_input": "继续补充"}
