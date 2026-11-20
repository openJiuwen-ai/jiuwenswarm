"""Abnormal execution endings remain visible without any successful model output."""

import json
import sqlite3

import pytest

from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.failure_notices import failure_notice_text
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_commit_port import (
    NOW, NS, accept_and_start, candidate, final,
)


def start(repository, *, direct=False, channel="feishu"):
    route = RequestRouteSnapshot(
        channel=channel, app_id="cli_original" if channel != "web" else None,
        target_id="oc_original", source_message_id="om_original", source_reply_to="om_parent",
    )
    return accept_and_start(repository, "conversation", "source", route_snapshot=route,
                            request_kind="direct_message" if direct else "scheduled")


def failed(execution, *, seq=1, reason="error", detail=None, outputs=()):
    return core.AgentEvent.create(
        execution, event_type=core.EventType.EXECUTION_PAUSED, local_seq=seq, occurred_at=NOW,
        payload={"reason": reason, "detail": detail or "RuntimeError: injected failure", "output_refs": outputs},
    )


@pytest.mark.parametrize(("detail", "summary"), [
    (
        "FrameworkError: [181001] model call failed, reason: model hit its output limit "
        "(finish_reason=length, output_tokens=8192) before producing an answer or a tool call",
        "模型输出达到上限，未能生成完整回答。",
    ),
    ("FrameworkError: context_length_exceeded", "对话内容超过模型可处理的上下文范围。"),
    ("FrameworkError: maximum context length exceeded", "对话内容超过模型可处理的上下文范围。"),
    ("FrameworkError: ReadTimeout: private upstream response", "模型服务响应超时。"),
    ("FrameworkError: ConnectTimeout: private upstream response", "模型服务请求超时。"),
    ("FrameworkError: TimeoutError: private upstream response", "服务请求超时。"),
    ("FrameworkError: APIConnectionError: private upstream response", "模型服务连接失败。"),
    ("FrameworkError: AuthenticationError: private upstream response", "模型服务认证失败。"),
    ("FrameworkError: RateLimitError: private upstream response", "模型服务请求受限。"),
    ("FrameworkError: insufficient_balance_error", "模型服务余额不足。"),
    ("FrameworkError: insufficient_quota", "模型服务可用额度不足。"),
    ("FrameworkError: UnicodeDecodeError: private response", "模型响应解码失败。"),
    ("RuntimeError: private execution details", "执行发生内部异常。"),
    ("unknown provider response with length=8192", "执行发生内部异常。"),
    (None, "执行发生内部异常。"),
])
def test_failure_notice_shows_only_a_safe_specific_reason_and_request_id(detail, summary):
    private = " api_key=sk-private-secret https://private.invalid/?token=private-token C:/private/config"
    text = failure_notice_text("req-test", reason="error", detail=(detail + private) if detail else None)

    assert text.splitlines() == [f"原因：{summary}", "请求编号：req-test"]


@pytest.mark.parametrize(("reason", "summary"), [
    ("budget_denied", "执行额度不足或预算检查未通过。"),
    ("context_unfit", "上下文无法安全整理到允许的窗口大小。"),
    ("missing_terminal", "执行流意外结束，没有收到终态结果。"),
    ("group_access_lost", "等待或恢复期间，员工的群访问权限已失效。"),
    ("group_access_unavailable", "当前无法确认员工的群访问权限。"),
    ("group_authorization_changed", "处理期间，员工的群访问权限或可读取的历史范围发生变化。"),
    ("group_trigger_out_of_scope", "原消息早于员工当前可读取的群历史范围，本次请求未继续执行。"),
])
def test_host_failure_reasons_keep_the_same_concise_format(reason, summary):
    text = failure_notice_text("req-test", reason=reason, detail="AuthenticationError: private detail")

    assert text.splitlines() == [f"原因：{summary}", "请求编号：req-test"]


@pytest.mark.parametrize("channel", ["web", "feishu", "dingtalk"])
def test_output_limit_notice_is_persisted_and_routed_in_the_same_concise_format(tmp_path, channel):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo, channel=channel)
    repo.record_events([failed(execution, detail="FrameworkError: model hit its output limit")], now=NOW)
    notice = repo.result_deliveries(NS, execution.request_id)[0]

    assert notice["channel"] == channel
    assert notice["notice_text"].splitlines() == [
        "原因：模型输出达到上限，未能生成完整回答。", f"请求编号：{execution.request_id}",
    ]


@pytest.mark.parametrize("direct", [False, True])
def test_decode_failure_without_candidate_creates_one_safe_routed_notice(tmp_path, direct):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo, direct=direct)
    detail = (
        "FrameworkError: [181001] model call failed: UnicodeDecodeError: 'utf-8' codec can't decode "
        "byte 0x8b in position 1; api_key=sk-private-secret https://private.invalid/?token=private-token "
        "C:/Users/private/config.json [[agent:someone_else]]"
    )
    event = failed(execution, detail=detail)
    repo.record_events([event], now=NOW)
    repo.record_events([event], now=NOW)
    rows = repo.result_deliveries(NS, execution.request_id)
    assert len(rows) == 1
    assert repo.candidate_output(NS, rows[0]["output_id"]) is None
    jobs = repo.claim_pending_result_deliveries(NS, now=NOW)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["app_id"] == "cli_original" and job["target_id"] == "oc_original"
    assert job["source_message_id"] == (None if direct else "om_original")
    assert job["source_reply_to"] == "om_parent"
    assert "模型响应解码失败" in job["text"] and execution.request_id in job["text"]
    for private in ("private-secret", "private.invalid", "private-token", "config.json", "[[agent:"):
        assert private not in job["text"]
    assert len(job["text"]) < 600


@pytest.mark.parametrize("reason", ["error", "context_unfit", "budget_denied"])
def test_abnormal_pause_remains_visible_even_with_partial_output(tmp_path, reason):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    proposed = candidate(execution, 1, text="已完成的部分结果")
    repo.record_events([proposed, failed(execution, seq=2, reason=reason, outputs=("out-1",))], now=NOW)
    jobs = repo.claim_pending_result_deliveries(NS, now=NOW)
    assert len(jobs) == 2
    assert jobs[0]["text"] == "已完成的部分结果"
    assert jobs[1]["text"].startswith("原因：")
    assert jobs[1]["text"].splitlines()[-1] == f"请求编号：{execution.request_id}"


@pytest.mark.parametrize("status", ["completed", "ignored", "cancelled", "stopped"])
def test_normal_completion_ignore_and_user_stop_do_not_send_failure_notice(tmp_path, status):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    if status == "stopped":
        event = core.AgentEvent.create(execution, event_type=core.EventType.EXECUTION_PAUSED,
                                      local_seq=1, occurred_at=NOW, payload={"reason": "stopped"})
    else:
        event = core.AgentEvent.create(execution, event_type=core.EventType.EXECUTION_FINAL,
                                      local_seq=1, occurred_at=NOW,
                                      payload={"status": status, "reason": "user decision"})
    repo.record_events([event], now=NOW)
    assert repo.result_deliveries(NS, execution.request_id) == []


@pytest.mark.asyncio
async def test_pending_notice_survives_restart_and_is_archived_only_after_send_ack(tmp_path):
    path = tmp_path / "agents.sqlite3"
    repo = ExecutionRepository(path)
    execution = start(repo)
    event = failed(execution)
    repo.record_events([event], now=NOW)
    sent = []

    async def unavailable(job):
        sent.append(dict(job))
        return None

    service = ResultDeliveryService(repo, namespace_id=NS, send_delivery=unavailable, clock=lambda: NOW)
    assert await service.dispatch_pending() == 1
    assert repo.result_deliveries(NS, execution.request_id)[0]["status"] == "pending"
    reopened = ExecutionRepository(path)
    reopened.record_events([event], now=NOW)

    async def delivered(job):
        sent.append(dict(job))
        return {"stage": "send", "delivery_id": job["delivery_id"], "outcome": "delivered",
                "external_message_id": "om_notice"}

    service = ResultDeliveryService(reopened, namespace_id=NS, send_delivery=delivered, clock=lambda: NOW)
    assert await service.dispatch_pending() == 1
    assert await service.dispatch_pending() == 0
    assert sent[0]["idempotency_key"] == sent[1]["idempotency_key"]
    archive = reopened.pending_delivery_archives(NS)
    assert len(archive) == 1
    assert json.loads(archive[0]["payload_json"])["text"] == sent[1]["text"]
    assert reopened.result_deliveries(NS, execution.request_id)[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_ambiguous_notice_send_is_not_blindly_retried(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    repo.record_events([failed(execution)], now=NOW)

    async def ambiguous(job):
        return {"stage": "send", "delivery_id": job["delivery_id"], "outcome": "unknown"}

    service = ResultDeliveryService(repo, namespace_id=NS, send_delivery=ambiguous, clock=lambda: NOW)
    assert await service.dispatch_pending() == 1
    assert await service.dispatch_pending() == 0
    assert repo.result_deliveries(NS, execution.request_id)[0]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "silent"])
async def test_driver_failure_without_terminal_event_is_visible_in_web_view(tmp_path, mode):
    from tests.unit_tests.server.jiuwenmate.test_session_scheduler import ACCESS, Host

    host = Host(tmp_path)
    session = host.session()
    await host.manager.start()
    try:
        host.core.modes[mode] = mode
        host.core.release(mode)
        state, _ = await host.scheduler.accept(
            session, snapshot=host.service.snapshot(NS, "mate_dev", "v1"), source_ref="failure:" + mode,
            access=ACCESS,
            request_input=core.RequestInput(kind="scheduled", text=mode, reply_target="conversation"),
            received_at=NOW, route_snapshot=RequestRouteSnapshot(channel="web", target_id="conversation"),
        )
        await host.scheduler.drain()
        view = await host.manager.request_view(NS, state.record.request_id)
        assert view.state.outcome == "failed"
        assert view.replies
        assert state.record.request_id in view.replies[0]
        expected = "内部异常" if mode == "raise" else "终态"
        assert expected in view.replies[0]
    finally:
        await host.manager.aclose()


def test_final_failed_without_candidate_also_queues_notice(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    event = core.AgentEvent.create(execution, event_type=core.EventType.EXECUTION_FINAL, local_seq=1,
                                  occurred_at=NOW, payload={"status": "failed", "reason": "RuntimeError: failed"})
    repo.record_events([event], now=NOW)
    assert len(repo.result_deliveries(NS, execution.request_id)) == 1


def test_host_notice_never_reads_a_candidate_that_reuses_its_output_reference(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    repo.record_events([failed(execution)], now=NOW)
    notice = repo.result_deliveries(NS, execution.request_id)[0]
    repo.record_events([candidate(execution, 2, output_id=notice["output_id"], text="private candidate content")], now=NOW)
    job = repo.claim_pending_result_deliveries(NS, now=NOW)[0]
    assert "private candidate content" not in job["text"]
    assert job["text"] == notice["notice_text"]


def test_repeated_driver_failure_uses_one_durable_notice_and_keeps_raw_details_private(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    for detail in ("RuntimeError: secret=first-value", "RuntimeError: secret=second-value"):
        repo.record_execution_failure(NS, execution.request_id, execution.execution_id,
                                      reason="error", detail=detail, now=NOW)
    rows = repo.result_deliveries(NS, execution.request_id)
    assert len(rows) == 1 and rows[0]["attempt_count"] == 0
    assert "内部异常" in rows[0]["notice_text"]
    assert "first-value" not in rows[0]["notice_text"] and "second-value" not in rows[0]["notice_text"]
    assert repo.read_execution_log(NS, execution.session_id, execution.request_id, execution.execution_id) == ()


def test_confirmation_pause_does_not_get_an_error_notice(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    event = core.AgentEvent.create(
        execution, event_type=core.EventType.EXECUTION_PAUSED, local_seq=1, occurred_at=NOW,
        payload={"reason": "awaiting_confirmation", "intercepted": [{
            "tool_call_id": "call-1", "tool_name": "bash", "arguments": "{}",
        }]},
    )
    repo.record_events([event], now=NOW)
    assert repo.result_deliveries(NS, execution.request_id) == []


@pytest.mark.asyncio
async def test_cleanup_exception_does_not_leave_request_dispatched_and_silent(tmp_path, monkeypatch):
    from tests.unit_tests.server.jiuwenmate.test_session_scheduler import ACCESS, Host

    async def fail_cleanup(context):
        raise RuntimeError("cleanup failed with secret=private-value")

    host = Host(tmp_path)
    await host.manager.start()
    try:
        monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.execution_driver.close_bash_runtime", fail_cleanup)
        host.core.release("cleanup")
        state, _ = await host.scheduler.accept(
            host.session(), snapshot=host.service.snapshot(NS, "mate_dev", "v1"), source_ref="failure:cleanup",
            access=ACCESS, request_input=core.RequestInput(kind="scheduled", text="cleanup", reply_target="conv-1"),
            received_at=NOW, route_snapshot=RequestRouteSnapshot(channel="web", target_id="conv-1"),
        )
        await host.scheduler.drain()
        view = await host.manager.request_view(NS, state.record.request_id)
        assert view.state.status == "ended" and view.state.outcome == "failed"
        assert len(view.replies) == 1 and "内部异常" in view.replies[0]
        assert "private-value" not in view.replies[0]
    finally:
        await host.manager.aclose()


def test_failure_notice_rejects_an_execution_owned_by_another_request(tmp_path):
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repo)
    with pytest.raises(PersistentAgentError):
        repo.record_execution_failure(NS, "another-request", execution.execution_id,
                                      reason="error", detail="RuntimeError: fail", now=NOW)
    assert repo.result_deliveries(NS, execution.request_id) == []


def test_driver_failure_status_is_atomic_with_notice_before_scheduler_can_finish(tmp_path):
    path = tmp_path / "agents.sqlite3"
    repo = ExecutionRepository(path)
    execution = start(repo)
    repo.record_execution_failure(NS, execution.request_id, execution.execution_id,
                                  reason="error", detail="RuntimeError: crashed", now=NOW)
    reopened = ExecutionRepository(path)
    state = reopened.get_request(NS, execution.request_id)
    assert state.status == "ended" and state.outcome == "failed"
    assert len(reopened.result_deliveries(NS, execution.request_id)) == 1
    assert reopened.read_execution_log(NS, execution.session_id, execution.request_id, execution.execution_id) == ()


@pytest.mark.asyncio
async def test_legacy_web_request_without_route_still_gets_safe_failure_status(tmp_path):
    from tests.unit_tests.server.jiuwenmate.test_session_scheduler import Host

    host = Host(tmp_path)
    await host.manager.start()
    try:
        host.core.modes["legacy"] = "raise"
        host.core.release("legacy")
        state = await host.send("legacy")
        await host.scheduler.drain()
        view = await host.manager.request_view(NS, state.record.request_id)
        assert view.replies and "内部异常" in view.replies[0]
        assert view.deliveries[0]["status"] == "available"
        assert host.sessions.claim_pending_result_deliveries(NS, now=NOW) == []
    finally:
        await host.manager.aclose()


def test_missing_route_never_exposes_raw_diagnostics_or_guesses_feishu_destination(tmp_path):
    repo = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = accept_and_start(repo, "legacy", "legacy-source")
    repo.record_events([failed(execution, detail="RuntimeError: token=private-token C:/private/config")], now=NOW)
    rows = repo.result_deliveries(NS, execution.request_id)
    assert len(rows) == 1 and rows[0]["status"] == "available"
    assert "private-token" not in rows[0]["notice_text"]
    assert "C:/private" not in rows[0]["notice_text"]
    assert repo.claim_pending_result_deliveries(NS, now=NOW) == []


def test_outbox_migrates_from_previous_schema_without_losing_existing_delivery(tmp_path):
    path = tmp_path / "agents.sqlite3"
    repo = ExecutionRepository(path)
    execution = start(repo)
    repo.record_events([candidate(execution, 1), final(execution, 2)], now=NOW)
    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(result_deliveries)")]
        assert "notice_text" in columns
        conn.execute("ALTER TABLE result_deliveries DROP COLUMN notice_text")
    upgraded = ExecutionRepository(path)
    jobs = upgraded.claim_pending_result_deliveries(NS, now=NOW)
    assert len(jobs) == 1 and jobs[0]["text"] == "进展如下"
