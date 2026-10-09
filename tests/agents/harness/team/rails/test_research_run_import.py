# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The import maps only same-concept records and keeps every row traceable."""

from jiuwenswarm.agents.harness.team.rails import research_run_import as imp
from jiuwenswarm.agents.harness.team.rails.workflow_run_log import (
    STAGE_REGISTRY,
    WorkflowRunLog,
)

UNTIL = imp.parse_ts("2026-08-20T19:16:43+08:00")


def _artifact(id_, type_, created="2026-08-18 12:01:43", status="active"):
    return {"id": id_, "type": type_, "status": status, "created_at": created,
            "stage": "init", "title": "", "version": 1}


def test_only_listed_types_satisfy_a_requirement():
    reqs = imp.artifact_requirements([
        _artifact(1, "topic_brief"),
        _artifact(2, "topic_knowledge_snapshot"),  # coverage state, not a literature map
    ])
    assert [(r["stage"], r["kind"]) for r in reqs] == [("init", "topic_brief")]
    assert reqs[0]["ref"] == "rh:artifact/1"
    assert reqs[0]["origin"] == "research-harness"


def test_superseded_and_post_submission_artifacts_are_left_out():
    reqs = imp.artifact_requirements([
        _artifact(1, "draft_pack", status="superseded"),
        _artifact(2, "final_bundle", created="2026-08-25 09:00:00"),
        _artifact(3, "experiment_design", created="2026-08-18 17:56:57"),
    ], UNTIL)
    assert [r["ref"] for r in reqs] == ["rh:artifact/3"]
    assert reqs[0]["kind"] == "study_spec"


def test_rh_writes_naive_timestamps_in_utc():
    # 11:16:43 UTC is 19:16:43 +08:00, so a naive RH timestamp one second later
    # falls after the cutoff.
    assert imp.recorded_by("2026-08-20 11:16:43", UNTIL)
    assert not imp.recorded_by("2026-08-20 11:16:44", UNTIL)


def test_usage_rows_carry_jiuwenswarm_token_keys_and_their_source():
    provenance = [
        {"id": 7, "primitive": "gap_ground_verify", "model_used": "gpt-5.5",
         "prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.01,
         "success": True, "started_at": "2026-08-19T14:38:34+00:00"},
        {"id": 8, "primitive": "paper_search", "prompt_tokens": 0,
         "completion_tokens": 0, "success": True,
         "started_at": "2026-08-25T09:00:45+00:00"},
    ]
    ledger = [{"stage": "blind_audit_grid", "measured": False,
               "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]
    rows = imp.usage_rows(provenance, ledger, UNTIL, {"blind_audit_grid": "gpt-5.5"})
    assert [r["source_record"] for r in rows] == [
        "rh:provenance/7", "ideal_run_log.jsonl:1",
    ]
    first = rows[0]
    assert first["stage"] == "analyze"
    assert (first["input_tokens"], first["output_tokens"], first["total_tokens"]) == (100, 20, 120)
    assert first["source_runtime"] == imp.SOURCE_RUNTIME
    assert rows[1]["stage"] == "experiment" and rows[1]["measured"] is False
    assert rows[1]["model"] == "gpt-5.5"


def test_acquisition_is_judged_by_a_recorded_pdf_hash():
    report = imp.acquisition_report([
        {"id": 1, "pdf_hash": "abc"}, {"id": 2, "pdf_hash": None},
    ])
    assert report == {"paper_count": 2, "pdf_acquired": 1, "pdf_missing_ids": [2]}


def test_replay_stops_at_the_first_stage_with_a_missing_kind(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t")
    reqs = imp.artifact_requirements([_artifact(1, "topic_brief")])
    decisions = imp.replay(reqs, log)
    assert [d.stage for d in decisions] == ["init", "build"]
    assert decisions[-1].decision == "refused"
    assert "literature_map" in decisions[-1].missing_artifacts
    assert log.read()[-1]["decision"] == "refused"


def test_coverage_names_each_source_and_each_gap():
    reqs = imp.artifact_requirements([_artifact(1, "topic_brief")])
    table = {row["stage"]: row for row in imp.coverage(reqs, STAGE_REGISTRY)}
    assert table["init"]["satisfied"]["topic_brief"]["ref"] == "rh:artifact/1"
    assert table["init"]["missing"] == []
    assert table["write"]["satisfied"] == {}
    assert "final_bundle" in table["write"]["missing"]


def test_replay_keeps_source_times_and_origins(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t", clock=lambda: "IMPORT")
    reqs = imp.artifact_requirements([
        _artifact(2, "experiment_design", created="2026-08-18 17:56:57"),
        _artifact(1, "topic_brief", created="2026-08-18 12:01:43"),
    ]) + [{"stage": "build", "kind": "paper_pool_snapshot", "ref": "x", "origin": "derived_at_import"}]
    imp.replay(reqs, log)
    arts = [r for r in log.read() if r["event"] == "artifact_recorded"]
    assert [(r["ts"], r["origin"]) for r in arts] == [
        ("2026-08-18T12:01:43+00:00", "research-harness"),
        ("2026-08-18T17:56:57+00:00", "research-harness"),
        ("IMPORT", "derived_at_import"),
    ]
    assert all(r["ts"] == "IMPORT" for r in log.read() if r["event"] != "artifact_recorded")
    assert all(r["origin"] == "derived_at_import" for r in log.read() if r["event"] == "stage_advance")
