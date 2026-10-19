import json

from jiuwenswarm.symphony.evolution import session_consumer
from jiuwenswarm.symphony.evolution.service import evolution_status
from jiuwenswarm.symphony.evolution.session_consumer import (
    consume_session_history,
    session_feedback_status,
)
from jiuwenswarm.symphony.evolution.store import read_events, read_overlay


def _write_history(session_root, session_id, records):
    session_dir = session_root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "history.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _plan_records(plan_id="plan-1"):
    return [
        {
            "role": "user",
            "request_id": "req-plan",
            "content": "提取发票并校验真伪",
        },
        {
            "role": "assistant",
            "request_id": "req-plan",
            "event_type": "chat.tool_result",
            "tool_name": "symphony_compose_score",
            "success": True,
            "raw_output": {
                "success": True,
                "plan_id": plan_id,
                "dynamic_graph_enabled": True,
                "plan": {
                    "status": "ready",
                    "steps": [
                        {"skill_id": "ocr-invoice"},
                        {"skill_id": "verify-invoice"},
                    ],
                    "can_feed_edges": [
                        {
                            "source_id": "ocr-invoice",
                            "target_id": "verify-invoice",
                        }
                    ],
                },
            },
        },
        {
            "role": "assistant",
            "request_id": "req-plan",
            "event_type": "chat.final",
            "content": "已生成执行路径",
        },
    ]


def _success_records():
    return [
        {
            "role": "user",
            "request_id": "req-run",
            "content": "确认，按上面的路径继续执行",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "skill_tool",
                "arguments": json.dumps({"skill_name": "ocr-invoice"}),
            },
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_result",
            "tool_name": "skill_tool",
            "success": True,
            "result": "success=True",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "skill_tool",
                "arguments": json.dumps({"skill_name": "verify-invoice"}),
            },
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_result",
            "tool_name": "skill_tool",
            "success": True,
            "result": "success=True",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.final",
            "content": "发票识别和真伪校验完成",
        },
    ]


def test_wait_for_request_history_observes_late_terminal_record(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    session_id = "session-late-final"
    request_id = "req-late-final"
    _write_history(
        session_root,
        session_id,
        [{"role": "user", "request_id": request_id, "content": "continue"}],
    )
    history_path = session_root / session_id / "history.jsonl"
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )
    sleep_calls = []

    def persist_terminal_record(_interval):
        sleep_calls.append(True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "request_id": request_id,
                        "event_type": "chat.final",
                        "content": "done",
                    }
                )
                + "\n"
            )

    monkeypatch.setattr(session_consumer.time, "sleep", persist_terminal_record)

    history_limit = session_consumer._wait_for_request_history(
        session_id,
        request_id,
        history_path,
    )

    assert sleep_calls == [True]
    assert history_limit == history_path.stat().st_size


def test_session_consumer_records_cross_turn_success(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    records = _plan_records() + _success_records()
    _write_history(session_root, "session-1", records)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-1",
        completed_request_id="req-run",
        score_dir=score_dir,
    )

    assert result["success"] is True
    assert result["outcomes"][0]["outcome"] == "success"
    assert result["outcomes"][0]["correlation"] == "planned_skill_observed"
    events = read_events(score_dir)
    assert len(events) == 1
    assert events[0]["source"] == "session_history"
    assert events[0]["session_id"] == "session-1"
    assert events[0]["request_id"] == "req-run"
    overlay = read_overlay(score_dir)
    edge = overlay["edges"]["ocr-invoice->verify-invoice:can_feed"]
    assert edge["success_count"] == 1
    assert edge["runtime_weight"] == 1.05
    feedback = session_feedback_status(score_dir)
    assert feedback["plans_observed"] == 1
    assert feedback["outcomes_recorded"] == 1
    assert feedback["last_result"]["plan_id"] == "plan-1"


def test_session_consumer_maps_package_ids_from_skill_frontmatter(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    run_records = _success_records()
    run_records[1]["tool_call"]["arguments"] = json.dumps(
        {"skill_name": "ocr-invoice-1.1.0"}
    )
    run_records[2]["result"] = (
        "success=True data={'skill_directory': '/skills/ocr-invoice-1.1.0', "
        "'skill_content': '---\nname: ocr-invoice\ndescription: OCR\n---\n'} "
        "error=None"
    )
    run_records[3]["tool_call"]["arguments"] = json.dumps(
        {"skill_name": "vendor-verifier-package-2.4.1"}
    )
    run_records[4]["result"] = (
        "success=True data={'skill_directory': '/skills/vendor-verifier-package-2.4.1', "
        "'skill_content': '---\nname: verify-invoice\ndescription: Verify\n---\n'} "
        "error=None"
    )
    _write_history(
        session_root,
        "session-package-ids",
        _plan_records() + run_records,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-package-ids",
        completed_request_id="req-run",
        score_dir=score_dir,
    )

    assert result["outcomes"][0]["outcome"] == "success"
    event = read_events(score_dir)[0]
    assert event["selected_skill_ids"] == ["ocr-invoice", "verify-invoice"]
    assert event["selected_edges"][0]["source_id"] == "ocr-invoice"
    assert event["selected_edges"][0]["target_id"] == "verify-invoice"
    overlay = read_overlay(score_dir)
    assert overlay["edges"]["ocr-invoice->verify-invoice:can_feed"][
        "success_count"
    ] == 1


def test_session_consumer_does_not_treat_plan_display_as_success(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    _write_history(session_root, "session-plan-only", _plan_records())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-plan-only",
        completed_request_id="req-plan",
        score_dir=score_dir,
    )

    assert result["outcomes"] == []
    assert read_events(score_dir) == []
    feedback = session_feedback_status(score_dir)
    assert feedback["pending_plan_count"] == 1


def test_session_consumer_does_not_trust_confirmation_without_execution(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    records = _plan_records() + [
        {
            "role": "user",
            "request_id": "req-run",
            "content": "确认，按上面的路径继续执行",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.final",
            "content": "已经执行完成",
        },
    ]
    _write_history(session_root, "session-no-execution", records)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-no-execution",
        completed_request_id="req-run",
        score_dir=score_dir,
    )

    assert result["outcomes"] == []
    assert read_events(score_dir) == []
    assert session_feedback_status(score_dir)["pending_plan_count"] == 1


def test_session_consumer_records_tool_failure_and_is_idempotent(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    failure_records = [
        {
            "role": "user",
            "request_id": "req-run",
            "content": "确认，继续执行",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "skill_tool",
                "arguments": json.dumps({"skill_name": "ocr-invoice"}),
            },
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "skill_tool",
                "arguments": json.dumps({"skill_name": "verify-invoice"}),
            },
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_result",
            "tool_name": "verify_invoice",
            "success": False,
            "status": "error",
            "error": "schema mismatch",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.final",
            "content": "校验失败",
        },
    ]
    _write_history(session_root, "session-failure", _plan_records() + failure_records)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    first = consume_session_history(
        "session-failure",
        completed_request_id="req-run",
        score_dir=score_dir,
    )
    second = consume_session_history(
        "session-failure",
        completed_request_id="req-run",
        score_dir=score_dir,
    )

    assert first["outcomes"][0]["outcome"] == "failure"
    assert second["outcomes"] == []
    assert len(read_events(score_dir)) == 1
    overlay = read_overlay(score_dir)
    edge = overlay["edges"]["ocr-invoice->verify-invoice:can_feed"]
    assert edge["failure_count"] == 1
    assert edge["runtime_weight"] == 0.95
    status = evolution_status(score_dir)
    assert status["session_feedback"]["outcomes_recorded"] == 1


def test_session_consumer_does_not_learn_unobserved_plan_edges(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    score_dir = tmp_path / "score"
    records = _plan_records() + [
        {
            "role": "user",
            "request_id": "req-run",
            "content": "确认，按上面的路径继续执行",
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "skill_tool",
                "arguments": json.dumps({"skill_name": "ocr-invoice"}),
            },
        },
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.final",
            "content": "只完成了识别",
        },
    ]
    _write_history(session_root, "session-partial", records)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-partial",
        completed_request_id="req-run",
        score_dir=score_dir,
    )

    assert result["outcomes"] == []
    assert read_events(score_dir) == []
    assert session_feedback_status(score_dir)["pending_plan_count"] == 1
