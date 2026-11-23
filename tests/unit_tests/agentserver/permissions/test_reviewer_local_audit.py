"""Exercise Reviewer diagnostics through the real permission observation path."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

from jiuwenswarm.agents.harness.common.rails.permissions import persistent_audit
from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    clear_send_file_execution_grant,
    current_send_file_execution_grant,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import classify_permission_result
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import PolicyEvaluation
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY,
    RootPermissionCompletionRail,
    RootPermissionQueueRail,
    bind_root_permission_request,
    reset_root_permission_request,
    tool_invocation_key_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import SendFilePathGuardResult
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_REQUEST_ID,
)
from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _Session, _rail, _runtime_ctx


@pytest.fixture
def audited_rail(tmp_path, monkeypatch):
    # Exercise the Linux rule with real descriptors without changing global sys.platform.
    monkeypatch.setattr(
        persistent_audit, "sys", SimpleNamespace(platform="linux", maxsize=persistent_audit.sys.maxsize),
    )
    rail, policy, reviewer, _ = _rail(tmp_path, PolicyEvaluation(level="ask", reason="policy_ask"))
    rail.persistent_audit_writer = persistent_audit.PersistentAuditWriter(data_root=tmp_path / "audit")
    reviewer.reason_code = "authorized_task"
    reviewer.rationale = "用户授权本次操作。"
    return rail, policy, reviewer


def send_arguments(policy, tmp_path, count):
    paths = tuple(tmp_path / f"report-{i}.txt" for i in range(count))
    for path in paths:
        path.write_text("report", encoding="utf-8")
    policy.result = PolicyEvaluation(
        level="ask",
        reason="policy_ask",
        path_result=SendFilePathGuardResult(
            status="evaluated", level="allow", reason="workspace", canonical_paths=tuple(str(path) for path in paths),
        ),
    )
    return {"abs_file_path_list": json.dumps([str(path) for path in paths]), "target_channels": ["web"]}


@pytest.mark.parametrize("tool_name", ["bash", "send_file_to_user"])
async def test_fixed_review_and_terminal_share_invocation_id(audited_rail, tmp_path, caplog, tool_name):
    rail, policy, reviewer = audited_rail
    args = (
        {"command": "cd sub && printf output > output.txt", "workdir": str(tmp_path)}
        if tool_name == "bash" else send_arguments(policy, tmp_path, 1)
    )
    ctx = _runtime_ctx(_Session(), tool_name=tool_name, tool_args=args)
    queue = RootPermissionQueue()
    token = bind_root_permission_request(
        root_session_id="session-a", request_id="request-a", enabled=True, queue=queue,
    )
    channel = TOOL_PERMISSION_CHANNEL_ID.set("web")
    request = TOOL_PERMISSION_REQUEST_ID.set("request-a")
    try:
        await RootPermissionQueueRail(queue).before_tool_call(ctx)
        key = tool_invocation_key_from_context(ctx)
        assert classify_permission_result(await rail.before_tool_call(ctx)) == "allow"
        assert len(reviewer.requests) == 1
        assert "model_recommendation" not in str(ctx.extra) + caplog.text
        await RootPermissionCompletionRail(queue).before_tool_call(ctx)
    finally:
        clear_send_file_execution_grant()
        TOOL_PERMISSION_REQUEST_ID.reset(request)
        TOOL_PERMISSION_CHANNEL_ID.reset(channel)
        reset_root_permission_request(token)
    assessment, terminal = map(json.loads, rail.persistent_audit_writer.audit_path.read_text().splitlines())
    assert assessment["invocation_id"] == terminal["invocation_id"] == key.invocation_id
    assert assessment["model_recommendation"] == {
        "outcome": "allow_once", "reason_code": reviewer.reason_code, "rationale": reviewer.rationale,
    }
    assert terminal["record_kind"] == "permission_terminal"
    assert terminal["authorization_outcome"] == "allow"
    assert "model_recommendation" not in terminal


@pytest.mark.parametrize("approved", [True, False])
async def test_host_manual_and_exact_resume_keep_original_local_recommendation(audited_rail, tmp_path, approved):
    rail, policy, reviewer = audited_rail
    args = send_arguments(policy, tmp_path, 4)
    session = _Session()
    ctx = _runtime_ctx(session, tool_name="send_file_to_user", tool_args=args)
    queue = RootPermissionQueue()
    entry = RootPermissionQueueRail(queue)
    binding = bind_root_permission_request(
        root_session_id="session-a", request_id="request-a", enabled=True, queue=queue,
    )
    channel = TOOL_PERMISSION_CHANNEL_ID.set("web")
    request = TOOL_PERMISSION_REQUEST_ID.set("request-a")
    try:
        await entry.before_tool_call(ctx)
        key = tool_invocation_key_from_context(ctx)
        with pytest.raises(AbortError) as paused:
            await rail.before_tool_call(ctx)
        assert reviewer.requests[0].allowed_outcomes == ("manual", "deny")
        assert current_send_file_execution_grant() is None
        card = paused.value.cause.request
        assert "model_recommendation" not in str(card)
        ctx.exception = paused.value
        await entry.after_tool_call(ctx)
        queue.reconcile({
            "result_type": "interrupt", "interrupt_ids": [key.tool_call_id],
            "state": [{"id": key.tool_call_id, "value": card}],
        }, root_session_id="session-a")
        answer = InteractiveInput()
        answer.update(key.tool_call_id, {"approved": approved, "auto_confirm": False, "feedback": ""})
        reserved = queue.reserve_answer("session-a", answer)
        resumed = _runtime_ctx(session, tool_name="send_file_to_user", tool_args=args)
        resumed.extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY] = reserved.interactive_input
        binding_resume = bind_root_permission_request(
            root_session_id="session-a", request_id="request-b", enabled=True, queue=queue,
        )
        request_resume = TOOL_PERMISSION_REQUEST_ID.set("request-b")
        try:
            await entry.before_tool_call(resumed)
            assert tool_invocation_key_from_context(resumed) == key
            result = await rail.before_tool_call(resumed)
            assert len(reviewer.requests) == 1
            assert classify_permission_result(result) == ("allow" if approved else "user_rejection")
            assert (current_send_file_execution_grant() is not None) == approved
            await RootPermissionCompletionRail(queue).before_tool_call(resumed)
        finally:
            TOOL_PERMISSION_REQUEST_ID.reset(request_resume)
            reset_root_permission_request(binding_resume)
    finally:
        clear_send_file_execution_grant()
        TOOL_PERMISSION_REQUEST_ID.reset(request)
        TOOL_PERMISSION_CHANNEL_ID.reset(channel)
        reset_root_permission_request(binding)
    rows = [json.loads(line) for line in rail.persistent_audit_writer.audit_path.read_text().splitlines()]
    assessment, pending, terminal = (row for row in rows if row.get("invocation_id") == key.invocation_id)
    assert assessment["reviewer_outcome"] == "manual"
    assert assessment["reviewer_reason_code"] == "reviewer_outcome_not_allowed"
    assert assessment["model_recommendation"] == {
        "outcome": "allow_once", "reason_code": reviewer.reason_code, "rationale": reviewer.rationale,
    }
    assert pending["stage_outcome"] == "manual_wait"
    assert terminal["authorization_outcome"] == ("allow" if approved else "cancel")
    assert terminal["decision_source"] == "manual_approval"
    assert "model_recommendation" not in pending and "model_recommendation" not in terminal


async def test_local_assessment_redacts_sensitive_model_reason(audited_rail, tmp_path, caplog):
    rail, _policy, reviewer = audited_rail
    path = "/home/example/SECRET_PATH_SENTINEL.txt"
    token = "sk-synthetic-secret-only"
    reviewer.rationale = f"读取 {path}，凭据 {token}。"
    ctx = _runtime_ctx(_Session(), tool_name="read_file", tool_args={"path": str(tmp_path / "report.md")})
    queue = RootPermissionQueue()
    binding = bind_root_permission_request(
        root_session_id="session-a", request_id="request-a", enabled=True, queue=queue,
    )
    try:
        await RootPermissionQueueRail(queue).before_tool_call(ctx)
        assert classify_permission_result(await rail.before_tool_call(ctx)) == "allow"
        await RootPermissionCompletionRail(queue).before_tool_call(ctx)
    finally:
        reset_root_permission_request(binding)
    rows = rail.persistent_audit_writer.audit_path.read_text(encoding="utf-8")
    assert path not in rows + caplog.text and token not in rows + caplog.text
    assert "model_recommendation" not in caplog.text
    assert json.loads(rows.splitlines()[0])["model_recommendation"]["rationale"] == (
        "读取 [path]，凭据 [redacted]。"
    )
