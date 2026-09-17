import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.session.session_history import (
    SESSION_REQUEST_COMPLETED_EVENT,
)
from jiuwenswarm.symphony.evolution import session_consumer
from jiuwenswarm.symphony.evolution.service import evolution_status
from jiuwenswarm.symphony.evolution.session_consumer import (
    consume_session_history,
    session_feedback_status,
)
from jiuwenswarm.symphony.evolution.store import read_events, read_overlay


def _write_history(session_root, session_id, records):
    session_dir = session_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "history.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _append_history(session_root, session_id, records):
    history_path = session_root / session_id / "history.jsonl"
    with history_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _plan_records(
    plan_id="plan-1",
    request_id="req-plan",
    skill_ids=("ocr-invoice", "verify-invoice"),
):
    edges = [
        {"source_id": source_id, "target_id": target_id}
        for source_id, target_id in zip(skill_ids, skill_ids[1:])
    ]
    return [
        {
            "role": "user",
            "request_id": request_id,
            "content": "提取发票并校验真伪",
        },
        {
            "role": "assistant",
            "request_id": request_id,
            "event_type": "chat.tool_result",
            "tool_name": "symphony_compose_graph",
            "success": True,
            "raw_output": {
                "success": True,
                "plan_id": plan_id,
                "dynamic_graph_enabled": True,
                "plan": {
                    "status": "ready",
                    "steps": [{"skill_id": skill_id} for skill_id in skill_ids],
                    "can_feed_edges": edges,
                },
            },
        },
        {
            "role": "assistant",
            "request_id": request_id,
            "event_type": "chat.final",
            "content": "已生成执行路径",
        },
    ]


def _tool_exchange(
    request_id,
    *,
    call_id,
    tool_name,
    arguments,
    result="loaded",
    success=True,
    result_call_id=None,
):
    tool_result = {
        "role": "assistant",
        "request_id": request_id,
        "event_type": "chat.tool_result",
        "tool_call_id": result_call_id or call_id,
        "tool_name": tool_name,
        "result": result,
    }
    if success is not None:
        tool_result["success"] = success
    return [
        {
            "role": "assistant",
            "request_id": request_id,
            "event_type": "chat.tool_call",
            "tool_call": {
                "tool_call_id": call_id,
                "name": tool_name,
                "arguments": json.dumps(arguments),
            },
        },
        tool_result,
    ]


def _completion_record(request_id, status="success"):
    return {
        "role": "assistant",
        "request_id": request_id,
        "event_type": SESSION_REQUEST_COMPLETED_EVENT,
        "status": status,
        "content": "",
    }


def _skill_activation_records(skill_ids, request_id="req-load"):
    records = [
        {"role": "user", "request_id": request_id, "content": "加载 Skills"}
    ]
    for index, skill_id in enumerate(skill_ids):
        records.extend(
            _tool_exchange(
                request_id,
                call_id=f"load-{index}",
                tool_name="skill_tool",
                arguments={"skill_name": skill_id},
            )
        )
    return [
        *records,
        {
            "role": "assistant",
            "request_id": request_id,
            "event_type": "chat.final",
            "content": "Skills 已加载",
        },
        _completion_record(request_id),
    ]


def _business_execution_records(request_id):
    return [
        {
            "role": "user",
            "request_id": request_id,
            "content": "确认，继续执行",
        },
        *_tool_exchange(
            request_id,
            call_id=f"business-{request_id}",
            tool_name="verify_invoice",
            arguments={"invoice": request_id},
            result="valid",
        ),
        {
            "role": "assistant",
            "request_id": request_id,
            "event_type": "chat.final",
            "content": "发票识别和真伪校验完成",
        },
        _completion_record(request_id),
    ]


def _read_session_state(graph_dir, session_id):
    payload = json.loads(
        session_consumer.session_feedback_state_path(graph_dir).read_text(
            encoding="utf-8"
        )
    )
    return payload["sessions"][session_id]


def _use_session_root(monkeypatch, session_root):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )


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


def test_session_consumer_records_cross_turn_success(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    records = _plan_records() + _success_records()
    _write_history(session_root, "session-1", records)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-1",
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert result["success"] is True
    assert result["outcomes"][0]["outcome"] == "success"
    assert result["outcomes"][0]["correlation"] == "planned_skill_observed"
    events = read_events(graph_dir)
    assert len(events) == 1
    assert events[0]["source"] == "session_history"
    assert events[0]["session_id"] == "session-1"
    assert events[0]["request_id"] == "req-run"
    overlay = read_overlay(graph_dir)
    edge = overlay["edges"]["ocr-invoice->verify-invoice:can_feed"]
    assert edge["success_count"] == 1
    assert edge["runtime_weight"] == 1.05
    feedback = session_feedback_status(graph_dir)
    assert feedback["plans_observed"] == 1
    assert feedback["outcomes_recorded"] == 1
    assert feedback["last_result"]["plan_id"] == "plan-1"


def test_session_consumer_maps_package_ids_from_skill_frontmatter(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
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
        graph_dir=graph_dir,
    )

    assert result["outcomes"][0]["outcome"] == "success"
    event = read_events(graph_dir)[0]
    assert event["selected_skill_ids"] == ["ocr-invoice", "verify-invoice"]
    assert event["selected_edges"][0]["source_id"] == "ocr-invoice"
    assert event["selected_edges"][0]["target_id"] == "verify-invoice"
    overlay = read_overlay(graph_dir)
    assert overlay["edges"]["ocr-invoice->verify-invoice:can_feed"][
        "success_count"
    ] == 1


def test_session_consumer_does_not_treat_plan_display_as_success(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    _write_history(session_root, "session-plan-only", _plan_records())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: session_root,
    )

    result = consume_session_history(
        "session-plan-only",
        completed_request_id="req-plan",
        graph_dir=graph_dir,
    )

    assert result["outcomes"] == []
    assert read_events(graph_dir) == []
    feedback = session_feedback_status(graph_dir)
    assert feedback["pending_plan_count"] == 1


def test_session_consumer_does_not_trust_confirmation_without_execution(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
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
        graph_dir=graph_dir,
    )

    assert result["outcomes"] == []
    assert read_events(graph_dir) == []
    assert session_feedback_status(graph_dir)["pending_plan_count"] == 1


def test_session_consumer_records_tool_failure_and_is_idempotent(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
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
        graph_dir=graph_dir,
    )
    second = consume_session_history(
        "session-failure",
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert first["outcomes"][0]["outcome"] == "failure"
    assert second["outcomes"] == []
    assert len(read_events(graph_dir)) == 1
    overlay = read_overlay(graph_dir)
    edge = overlay["edges"]["ocr-invoice->verify-invoice:can_feed"]
    assert edge["failure_count"] == 1
    assert edge["runtime_weight"] == 0.95
    status = evolution_status(graph_dir)
    assert status["session_feedback"]["outcomes_recorded"] == 1


def test_session_consumer_does_not_learn_unobserved_plan_edges(monkeypatch, tmp_path):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
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
        graph_dir=graph_dir,
    )

    assert result["outcomes"] == []
    assert read_events(graph_dir) == []
    assert session_feedback_status(graph_dir)["pending_plan_count"] == 1


def test_session_consumer_reuses_session_activated_skills_incrementally(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    session_id = "session-reused-skills"
    _use_session_root(monkeypatch, session_root)
    _write_history(
        session_root,
        session_id,
        _skill_activation_records(("ocr-invoice", "verify-invoice")),
    )
    consume_session_history(
        session_id,
        completed_request_id="req-load",
        graph_dir=graph_dir,
    )

    observed_weights = []
    for index in (1, 2):
        plan_request_id = f"req-plan-{index}"
        run_request_id = f"req-run-{index}"
        _append_history(
            session_root,
            session_id,
            [
                *_plan_records(f"plan-{index}", plan_request_id),
                _completion_record(plan_request_id),
            ],
        )
        planned = consume_session_history(
            session_id,
            completed_request_id=plan_request_id,
            graph_dir=graph_dir,
        )
        assert planned["outcomes"] == []

        _append_history(
            session_root,
            session_id,
            _business_execution_records(run_request_id),
        )
        executed = consume_session_history(
            session_id,
            completed_request_id=run_request_id,
            graph_dir=graph_dir,
        )
        assert executed["outcomes"][0]["correlation"] == (
            "session_activation_with_tool_execution"
        )
        edge = read_overlay(graph_dir)["edges"][
            "ocr-invoice->verify-invoice:can_feed"
        ]
        observed_weights.append((edge["success_count"], edge["runtime_weight"]))

    assert observed_weights == [(1, 1.05), (2, 1.1)]
    events = read_events(graph_dir)
    assert len(events) == 2
    assert all(
        event["evidence"]["skill_evidence"] == "session_activation"
        for event in events
    )
    assert _read_session_state(graph_dir, session_id)["activated_skill_ids"] == [
        "ocr-invoice",
        "verify-invoice",
    ]


def test_session_consumer_attributes_single_skill_loaded_with_read_file(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    skills_root = tmp_path / "skills"
    skill_id = "writing-product-descriptions"
    skill_file = skills_root / skill_id / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_content = f"---\nname: {skill_id}\n---\nWrite product copy.\n"
    skill_file.write_text(skill_content, encoding="utf-8")
    session_id = "session-read-skill"
    _use_session_root(monkeypatch, session_root)
    monkeypatch.setattr(
        session_consumer,
        "load_symphony_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(skills_root=skills_root)),
    )
    _write_history(
        session_root,
        session_id,
        [
            {"role": "user", "request_id": "req-load", "content": "加载文案 Skill"},
            *_tool_exchange(
                "req-load",
                call_id="read-skill",
                tool_name="read_file",
                arguments={"file_path": str(skill_file)},
                result=skill_content,
                success=None,
            ),
            {
                "role": "assistant",
                "request_id": "req-load",
                "event_type": "chat.final",
                "content": "Skill 已加载",
            },
            _completion_record("req-load"),
        ],
    )
    consume_session_history(
        session_id,
        completed_request_id="req-load",
        graph_dir=graph_dir,
    )
    _append_history(
        session_root,
        session_id,
        [
            *_plan_records(
                "plan-product-copy",
                "req-plan",
                (skill_id,),
            ),
            _completion_record("req-plan"),
        ],
    )
    consume_session_history(
        session_id,
        completed_request_id="req-plan",
        graph_dir=graph_dir,
    )
    _append_history(
        session_root,
        session_id,
        [
            {"role": "user", "request_id": "req-run", "content": "继续执行"},
            {
                "role": "assistant",
                "request_id": "req-run",
                "event_type": "chat.final",
                "content": "种草不是夸参数，而是把产品写进真实生活场景。",
            },
            _completion_record("req-run"),
        ],
    )

    result = consume_session_history(
        session_id,
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert result["outcomes"][0]["correlation"] == (
        "session_activation_with_final_response"
    )
    event = read_events(graph_dir)[0]
    assert event["selected_skill_ids"] == [skill_id]
    assert event["selected_edges"] == []
    path = next(iter(read_overlay(graph_dir)["paths"].values()))
    assert path["selected_skill_ids"] == [skill_id]
    assert path["success_count"] == 1


def test_session_consumer_attributes_same_turn_read_file_execution(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    skills_root = tmp_path / "skills"
    skill_name = "writing-product-descriptions"
    skill_file = skills_root / skill_name / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_content = f"---\nname: {skill_name}\n---\nWrite product copy.\n"
    skill_file.write_text(skill_content, encoding="utf-8")
    _use_session_root(monkeypatch, session_root)
    monkeypatch.setattr(
        session_consumer,
        "load_symphony_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(skills_root=skills_root)),
    )
    records = [
        *_plan_records("plan-same-turn", "req-run", (skill_name,))[:2],
        *_tool_exchange(
            "req-run",
            call_id="read-skill",
            tool_name="read_file",
            arguments={"file_path": str(skill_file)},
            result=skill_content,
            success=None,
        ),
        {
            "role": "assistant",
            "request_id": "req-run",
            "event_type": "chat.final",
            "content": "把产品写进真实生活场景。",
        },
        _completion_record("req-run"),
    ]
    _write_history(session_root, "session-same-turn", records)

    result = consume_session_history(
        "session-same-turn",
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert result["outcomes"][0]["correlation"] == (
        "session_activation_with_final_response"
    )
    assert read_events(graph_dir)[0]["selected_skill_ids"] == [skill_name]


def test_session_consumer_bootstraps_activations_behind_legacy_cursor(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    session_id = "session-legacy-activation"
    _use_session_root(monkeypatch, session_root)
    prior_records = [
        *_skill_activation_records(("ocr-invoice", "verify-invoice")),
        *_plan_records("legacy-plan", "req-plan"),
        _completion_record("req-plan"),
    ]
    _write_history(session_root, session_id, prior_records)
    history_path = session_root / session_id / "history.jsonl"
    stat = history_path.stat()
    state_path = session_consumer.session_feedback_state_path(graph_dir)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    session_id: {
                        "processed_record_count": len(prior_records),
                        "history_offset": stat.st_size,
                        "history_identity": f"{stat.st_dev}:{stat.st_ino}",
                        "pending_plan": session_consumer._plan_markers(
                            prior_records,
                            graph_dir=graph_dir,
                        )[-1][1],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _append_history(
        session_root,
        session_id,
        _business_execution_records("req-run"),
    )

    result = consume_session_history(
        session_id,
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert result["outcomes"][0]["correlation"] == (
        "session_activation_with_tool_execution"
    )
    assert _read_session_state(graph_dir, session_id)["activated_skill_ids"] == [
        "ocr-invoice",
        "verify-invoice",
    ]


@pytest.mark.parametrize(
    "case",
    ["failed-result", "outside-root", "mismatched-result"],
)
def test_read_file_activation_requires_trusted_successful_pairing(
    monkeypatch,
    tmp_path,
    case,
):
    skills_root = tmp_path / "skills"
    managed_file = skills_root / "writer" / "SKILL.md"
    outside_file = tmp_path / "outside" / "writer" / "SKILL.md"
    for skill_file in (managed_file, outside_file):
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: writer\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        session_consumer,
        "load_symphony_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(skills_root=skills_root)),
    )
    records = _tool_exchange(
        "req-load",
        call_id="read-skill",
        tool_name="read_file",
        arguments={
            "file_path": str(outside_file if case == "outside-root" else managed_file)
        },
        result="---\nname: writer\n---\n",
        success=False if case == "failed-result" else None,
        result_call_id="unknown" if case == "mismatched-result" else None,
    )

    assert session_consumer._activated_skill_ids(records) == []


@pytest.mark.parametrize(
    ("skill_ids", "final_text", "terminal_status"),
    [
        (("ocr-invoice", "verify-invoice"), "全部完成", "success"),
        (("writing-product-descriptions",), "已取消执行。", "cancelled"),
    ],
)
def test_final_only_does_not_reward_ambiguous_or_cancelled_execution(
    monkeypatch,
    tmp_path,
    skill_ids,
    final_text,
    terminal_status,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    session_id = f"session-no-reward-{terminal_status}-{len(skill_ids)}"
    _use_session_root(monkeypatch, session_root)
    _write_history(
        session_root,
        session_id,
        [
            *_skill_activation_records(skill_ids),
            *_plan_records("plan-no-reward", "req-plan", skill_ids),
            _completion_record("req-plan"),
            {"role": "user", "request_id": "req-run", "content": "继续执行"},
            {
                "role": "assistant",
                "request_id": "req-run",
                "event_type": "chat.final",
                "content": final_text,
            },
            _completion_record("req-run", terminal_status),
        ],
    )

    result = consume_session_history(
        session_id,
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert result["outcomes"] == []
    assert read_events(graph_dir) == []
    assert session_feedback_status(graph_dir)["pending_plan_count"] == 1


def test_request_completion_defers_interleaved_execution_until_plan_completes(
    monkeypatch,
    tmp_path,
):
    session_root = tmp_path / "sessions"
    graph_dir = tmp_path / "graph"
    session_id = "session-interleaved-completion"
    skill_id = "writing-product-descriptions"
    _use_session_root(monkeypatch, session_root)
    _write_history(
        session_root,
        session_id,
        _skill_activation_records((skill_id,)),
    )
    consume_session_history(
        session_id,
        completed_request_id="req-load",
        graph_dir=graph_dir,
    )

    plan_records = _plan_records("plan-interleaved", "req-plan", (skill_id,))
    _append_history(
        session_root,
        session_id,
        [
            *plan_records[:2],
            {"role": "user", "request_id": "req-run", "content": "继续执行"},
            {
                "role": "assistant",
                "request_id": "req-run",
                "event_type": "chat.final",
                "content": "已完成产品文案。",
            },
            _completion_record("req-run"),
        ],
    )

    early = consume_session_history(
        session_id,
        completed_request_id="req-run",
        graph_dir=graph_dir,
    )

    assert early["records_consumed"] == 0
    assert early["outcomes"] == []
    assert read_events(graph_dir) == []
    assert _read_session_state(graph_dir, session_id)["deferred_records"]

    _append_history(
        session_root,
        session_id,
        [plan_records[2], _completion_record("req-plan")],
    )
    completed = consume_session_history(
        session_id,
        completed_request_id="req-plan",
        graph_dir=graph_dir,
    )

    assert completed["outcomes"][0]["request_id"] == "req-run"
    assert read_overlay(graph_dir)["stats"]["path_count"] == 1
    assert "deferred_records" not in _read_session_state(graph_dir, session_id)
