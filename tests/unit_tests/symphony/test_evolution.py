from jiuwenswarm.symphony.evolution.aggregate import (
    RUNTIME_WEIGHT_POLICY,
    _runtime_weight,
    build_overlay_from_events,
)
from jiuwenswarm.symphony.evolution.models import PLAN_CREATED, PLAN_OUTCOME
from jiuwenswarm.symphony.evolution.service import (
    evolution_status,
    record_plan_outcome,
)


def test_evolution_overlay_reinforces_success_and_downranks_failures():
    events = [
        {
            "event_type": PLAN_OUTCOME,
            "ts": "2026-06-20T01:00:00+00:00",
            "plan_id": "p1",
            "query": "compose",
            "outcome": "success",
            "selected_edges": [{"source_id": "skill-a", "target_id": "skill-b"}],
        },
        {
            "event_type": PLAN_OUTCOME,
            "ts": "2026-06-20T01:01:00+00:00",
            "plan_id": "p2",
            "query": "compose",
            "outcome": "success",
            "selected_edges": [{"source_id": "skill-a", "target_id": "skill-b"}],
        },
        {
            "event_type": PLAN_OUTCOME,
            "ts": "2026-06-20T01:02:00+00:00",
            "plan_id": "p3",
            "query": "compose",
            "outcome": "failure",
            "failure_type": "schema_mismatch",
            "selected_edges": [{"source_id": "skill-b", "target_id": "skill-c"}],
        },
        {
            "event_type": PLAN_OUTCOME,
            "ts": "2026-06-20T01:03:00+00:00",
            "plan_id": "p4",
            "query": "compose",
            "outcome": "failure",
            "failure_type": "schema_mismatch",
            "selected_edges": [{"source_id": "skill-b", "target_id": "skill-c"}],
        },
        {
            "event_type": PLAN_OUTCOME,
            "ts": "2026-06-20T01:04:00+00:00",
            "plan_id": "p5",
            "query": "compose",
            "outcome": "failure",
            "failure_type": "schema_mismatch",
            "selected_edges": [{"source_id": "skill-b", "target_id": "skill-c"}],
        },
    ]

    overlay = build_overlay_from_events(events)

    reinforced = overlay["edges"]["skill-a->skill-b:can_feed"]
    downranked = overlay["edges"]["skill-b->skill-c:can_feed"]
    assert reinforced["runtime_weight"] > 1
    assert reinforced["representative_queries"] == ["compose"]
    assert downranked["runtime_weight"] < 1
    assert overlay["weight_policy"]["name"] == RUNTIME_WEIGHT_POLICY


def test_runtime_weight_uses_symmetric_linear_evidence_and_neutral_missing_input():
    assert _runtime_weight(1, 0, 0) == 1.05
    assert _runtime_weight(0, 1, 0) == 0.95
    assert _runtime_weight(3, 1, 0) == 1.1
    assert _runtime_weight(0, 0, 20) == 1.0
    assert _runtime_weight(100, 0, 0) == 2.0
    assert _runtime_weight(0, 100, 0) == 0.2


def test_evolution_overlay_aggregates_query_conditioned_complete_paths():
    events = [
        {
            "event_type": PLAN_OUTCOME,
            "plan_id": "path-1",
            "query": "extract the invoice and verify it",
            "outcome": "success",
            "selected_skill_ids": ["ocr", "invoice-verifier"],
            "selected_edges": [
                {"source_id": "ocr", "target_id": "invoice-verifier"}
            ],
        },
        {
            "event_type": PLAN_OUTCOME,
            "plan_id": "path-2",
            "query": "read this receipt then validate it",
            "outcome": "success",
            "selected_skill_ids": ["ocr", "invoice-verifier"],
            "selected_edges": [
                {"source_id": "ocr", "target_id": "invoice-verifier"}
            ],
        },
    ]

    overlay = build_overlay_from_events(events)

    assert overlay["schema_version"] == "symphony.dynamic_overlay.v3"
    assert overlay["stats"]["path_count"] == 1
    path = next(iter(overlay["paths"].values()))
    assert path["selected_skill_ids"] == ["invoice-verifier", "ocr"]
    assert path["success_count"] == 2
    assert path["failure_count"] == 0
    assert path["representative_queries"] == [
        "extract the invoice and verify it",
        "read this receipt then validate it",
    ]


def test_evolution_path_uses_plan_created_context_for_single_skill_success():
    events = [
        {
            "event_type": PLAN_CREATED,
            "plan_id": "single-1",
            "query": "summarize this article",
            "selected_skill_ids": ["summarizer"],
            "selected_edges": [],
        },
        {
            "event_type": PLAN_OUTCOME,
            "plan_id": "single-1",
            "query": "",
            "outcome": "success",
            "selected_edges": [],
        },
    ]

    overlay = build_overlay_from_events(events)

    path = next(iter(overlay["paths"].values()))
    assert path["selected_skill_ids"] == ["summarizer"]
    assert path["representative_queries"] == ["summarize this article"]
    assert path["can_feed_edges"] == []


def test_evolution_status_reads_recorded_outcomes_and_overlay(tmp_path):
    record_plan_outcome(
        tmp_path,
        plan_id="p1",
        query="invoice verification",
        outcome="no_plan",
        selected_edges=[],
    )
    record_plan_outcome(
        tmp_path,
        plan_id="p2",
        query="invoice verification",
        outcome="no_plan",
        selected_edges=[],
    )

    status = evolution_status(tmp_path)

    assert status["success"] is True
    assert status["event_count"] == 2
    assert status["overlay_exists"] is True
    assert status["overlay_stats"]["no_plan_count"] == 2
    assert status["weight_policy"]["name"] == RUNTIME_WEIGHT_POLICY


def test_evolution_failure_attribution_defaults_to_terminal_edge():
    events = []
    for index in range(3):
        events.append(
            {
                "event_type": PLAN_OUTCOME,
                "ts": f"2026-06-20T02:0{index}:00+00:00",
                "plan_id": f"p{index}",
                "query": "forward email",
                "outcome": "failure",
                "failure_type": "workbench_outcome_miss",
                "failure_attribution": "terminal_edge",
                "selected_edges": [
                    {"source_id": "email.search", "target_id": "directory.lookup"},
                    {"source_id": "directory.lookup", "target_id": "email.send"},
                ],
            }
        )

    overlay = build_overlay_from_events(events)

    upstream = overlay["edges"]["email.search->directory.lookup:can_feed"]
    terminal = overlay["edges"]["directory.lookup->email.send:can_feed"]
    assert upstream["success_count"] == 3
    assert upstream["failure_count"] == 0
    assert upstream["runtime_weight"] > 1
    assert terminal["success_count"] == 0
    assert terminal["failure_count"] == 3
    assert terminal["runtime_weight"] < 1
