# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The run log must record a refusal, not skip the stage that failed."""

import json

from jiuwenswarm.agents.harness.team.rails.delivery_gate import delivery_ready
from jiuwenswarm.agents.harness.team.rails.workflow_run_log import (
    STAGE_ORDER,
    WorkflowRunLog,
)


def test_stage_order_is_the_formal_six(tmp_path):
    assert STAGE_ORDER == (
        "init", "build", "analyze", "propose", "experiment", "write",
    )


def test_advance_refuses_and_records_the_missing_artifact(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1")
    decision = log.advance()
    assert decision.decision == "refused"
    assert decision.missing_artifacts == ("topic_brief",)
    assert log.stage == "init"
    saved = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    assert saved[-1]["decision"] == "refused"
    assert saved[-1]["missing_artifacts"] == ["topic_brief"]


def test_advance_leaves_only_after_the_required_artifact_is_recorded(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1")
    log.record_artifact("topic_brief", stage="init", ref="briefs/t1.md")
    decision = log.advance()
    assert decision.decision == "advanced"
    assert decision.next_stage == "build"
    assert log.stage == "build"


def test_delivery_refusal_is_in_the_same_log(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1")
    findings = delivery_ready(r"see \cite{missing}", {}, {})
    assert log.record_delivery(findings) is False
    saved = log.read()
    assert saved[-1]["event"] == "delivery_gate"
    assert saved[-1]["decision"] == "refused"
    assert saved[-1]["findings"]


def test_spending_past_the_token_budget_stops_the_run(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1", token_budget=100)
    log.record_artifact("topic_brief", stage="init", ref="briefs/t1.md")
    log.record_usage(stage="init", total_tokens=101, source_record="usage.jsonl:1")
    decision = log.advance()
    assert decision.decision == "refused"
    assert decision.reason == "token_budget_exceeded"
    assert log.stage == "init"
    assert log.read()[-1]["reason"] == "token_budget_exceeded"


def test_fall_back_returns_to_the_registered_stage(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1")
    assert log.fall_back() is None  # init has no fallback
    log.record_artifact("topic_brief", stage="init", ref="briefs/t1.md")
    log.advance()
    assert log.stage == "build"
    assert log.fall_back() == "init"
    assert log.read()[-1]["event"] == "stage_fallback"


def _run_to_build(path):
    log = WorkflowRunLog(path, topic_id="t1", token_budget=500)
    log.record_artifact("topic_brief", stage="init", ref="briefs/t1.md")
    log.record_usage(stage="init", total_tokens=120, source_record="usage.jsonl:1")
    log.advance()
    log.record_artifact("literature_map", stage="build", ref="maps/t1.json")
    return log


def test_resume_rebuilds_stage_artifacts_and_spend(tmp_path):
    _run_to_build(tmp_path / "run.jsonl")
    resumed = WorkflowRunLog.resume(tmp_path / "run.jsonl")
    assert resumed.stage == "build"
    assert resumed.tokens_used == 120 and resumed.token_budget == 500
    # the resumed run knows what is still missing in build
    assert resumed.advance().missing_artifacts == (
        "paper_pool_snapshot", "citation_expansion_report", "acquisition_report")


def test_resume_cuts_a_line_left_half_written_by_a_crash(tmp_path):
    path = tmp_path / "run.jsonl"
    _run_to_build(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-27T00:00:00", "topic_id": "t1", "event": "artifact_rec')
    resumed = WorkflowRunLog.resume(path)
    assert resumed.stage == "build"
    resumed.record_artifact("paper_pool_snapshot", stage="build", ref="pool.json")
    assert resumed.read()[-1]["kind"] == "paper_pool_snapshot"  # the file is whole again


def test_a_damaged_line_inside_the_log_is_not_silently_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    _run_to_build(path)
    lines = path.read_text().splitlines()
    lines[1] = lines[1][:20]
    path.write_text("\n".join(lines) + "\n")
    try:
        WorkflowRunLog.resume(path)
    except ValueError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("a damaged record was skipped")


def test_resume_selects_one_run_from_a_shared_file(tmp_path):
    path = tmp_path / "run.jsonl"
    _run_to_build(path)
    other = WorkflowRunLog(path, topic_id="t2")
    other.record_artifact("topic_brief", stage="init", ref="briefs/t2.md")
    assert WorkflowRunLog.resume(path, topic_id="t2").stage == "init"
    assert WorkflowRunLog.resume(path, topic_id="t1").stage == "build"


def test_artifact_without_a_ref_is_refused(tmp_path):
    log = WorkflowRunLog(tmp_path / "run.jsonl", topic_id="t1")
    try:
        log.record_artifact("topic_brief", stage="init", ref="")
    except ValueError as exc:
        assert "no ref" in str(exc)
    else:
        raise AssertionError("an artifact with no ref was stored")
