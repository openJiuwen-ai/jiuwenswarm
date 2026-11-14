"""Video adapter regressions over the persisted task service (no private queue mirrors)."""

# White-box fixtures inject an isolated persisted service and cached routing state.
# pylint: disable=protected-access

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import queue_manager, video_search
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import parse_qwen_omni_tool_call, qwen_omni_tools
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import task_identity
from jiuwenswarm.extensions.video_duplex.backend.tasks.errors import QueueVersionConflict, TaskRevisionConflict
from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import (
    management_call, run_queue, tool_proposal, until,
)


def test_voice_query_excludes_unbounded_progress_and_preserves_control_identity():
    job = {
        "id": "task-a", "job_id": "task-a", "status": "running", "revision": 4,
        "queue_version": 9, "question": "生成行程", "query": "生成行程",
        "progress_history": [{"reasoning": "私有执行详情" * 100000}],
        "progress": {"reasoning": "不要转发"}, "result": "结论" * 10000,
        "interaction": {"id": "question-a", "state": "pending", "questions": [{"question": "预算？"}]},
    }
    result = video_search.VideoSearchManager.voice_job(job)
    assert result["job_id"] == "task-a"
    assert result["revision"] == 4 and result["queue_version"] == 9
    assert result["interaction"] == job["interaction"]
    assert "progress_history" not in result and "progress" not in result
    assert result["result_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 8000
    assert job["progress_history"], "The UI still owns the full history"


def test_oversized_interaction_requires_ui_instead_of_corrupting_answer_identity():
    result = video_search.VideoSearchManager.voice_job({"interaction": {"questions": ["长问题" * 10000]}})
    assert result["interaction_requires_ui"] is True
    assert "interaction" not in result


def test_voice_job_has_a_total_bound_even_with_many_large_fields():
    job = {"job_id": "a", "question": "详" * 4000, "query": "详" * 4000,
           "error": "详" * 4000, "result": "详" * 4000,
           "adjustments": [{"id": "a", "instruction": "详" * 4000} for _ in range(20)]}
    result = video_search.VideoSearchManager.voice_job(job)
    assert result["job_id"] == "a"
    assert result["details_omitted"] is True
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 12000


@pytest.fixture
async def queue(monkeypatch, tmp_path):
    events, responses, executed, cancels = [], [], [], []
    execution_users = []
    gates = {name: asyncio.Event() for name in "ABC"}
    stop_ack = asyncio.Event()
    stop_ack.set()
    stopped = set()
    reject = [False]

    async def execute(_client, **kwargs):
        name = kwargs["question"]
        with manager.service.store.transaction() as db:
            task = next(
                t
                for t in manager.service.store.rows(db)
                if t["request_id"] == kwargs["request_id"]
            )
        # This fake represents a running Core invocation, after admission.
        manager.service.store.update(task["id"], lambda t: t.update(execution_bound=True))
        executed.append(name)
        execution_users.append(kwargs.get("user_id"))
        await gates[name].wait()
        manager.service.store.update(
            task["id"], lambda t: t.update(execution_settled=True)
        )
        if name in stopped:
            raise RuntimeError("Stopped")
        return {"answer": name}

    async def send_event(ws, name, payload):
        events.append((name, payload))

    async def send_response(ws, req_id, **kwargs):
        responses.append(kwargs)

    async def send_request(env):
        if env.method == "voice.task.files":
            return SimpleNamespace(ok=True, payload={"files": []})
        cancels.append(env)
        await stop_ack.wait()
        if reject[0]:
            return SimpleNamespace(ok=False, payload={})
        with manager.service.store.transaction() as db:
            task = next(
                t
                for t in manager.service.store.rows(db)
                if t["core_session_id"] == env.session_id
            )
        stopped.add(task["instruction"])
        gates[task["instruction"]].set()
        return SimpleNamespace(ok=True, payload={})

    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    manager = video_search.VideoSearchManager(
        SimpleNamespace(send_event=send_event, send_response=send_response),
        SimpleNamespace(send_request=send_request),
        log_event=lambda _: None,
        qwen_active=lambda: True,
        path=tmp_path / "tasks.sqlite",
        authorize=lambda ws, scope: ("alice", scope),
    )

    async def start(name, scope="scope"):
        return (await manager.start(
            None, question=name, query=name, search_session_id=scope, command_id=name
        ))["id"]

    def record(task_id):
        return manager.service.get("alice", "scope", task_id)

    async def control(task_id, action, **extra):
        await manager.handle_control(
            None,
            "control-" + action + task_id,
            {
                "search_session_id": "scope",
                "job_id": task_id,
                "action": action,
                "queue_version": manager.snapshot("alice", "scope")["queue_version"],
                **extra,
            },
            None,
        )
        return responses[-1]

    yield SimpleNamespace(**locals())
    await manager.close()


async def test_reorder_and_cancel_waiter(queue):
    q = queue
    await q.start("A")
    await until(lambda: q.executed == ["A"])
    b, c = await q.start("B"), await q.start("C")
    assert (await q.control(c, "next"))["ok"]
    assert (await q.control(b, "cancel"))["ok"]
    q.gates["A"].set()
    q.gates["C"].set()
    await until(lambda: q.record(c)["status"] == "completed")
    assert q.executed == ["A", "C"] and not q.cancels
    assert q.record(b)["status"] == "cancelled"


@pytest.mark.parametrize("stale_version", [False, True])
async def test_voice_reorder_accepts_missing_version_and_rejects_stale_version(queue, stale_version):
    q = queue
    await q.start("A")
    await until(lambda: q.executed == ["A"])
    b, c = await q.start("B"), await q.start("C")
    args = {"job_id": c, "action": "next"}
    if stale_version:
        args["queue_version"] = q.manager.snapshot("alice", "scope")["queue_version"] - 1
    call = parse_qwen_omni_tool_call({
        "name": "jiuwen_task_reorder", "call_id": "voice-reorder", "arguments": args,
    })
    result = await q.manager.operate("alice", "scope", call)
    if stale_version:
        assert result["operation"]["code"] == "QUEUE_VERSION_CONFLICT"
    else:
        assert result["operation"]["state"] == "reordered"
    queued = sorted(
        (job for job in q.manager.snapshot("alice", "scope")["jobs"] if job["status"] == "queued"),
        key=lambda job: job["queue_position"],
    )
    assert [job["id"] for job in queued] == ([b, c] if stale_version else [c, b])


async def test_preempt_stays_pending_until_exact_stop_ack(queue):
    q = queue
    a = await q.start("A")
    await until(lambda: q.executed == ["A"])
    await q.start("B")
    c = await q.start("C")
    q.stop_ack.clear()
    assert (await q.control(c, "preempt"))["ok"]
    await until(lambda: q.cancels)
    assert q.cancels[0].session_id == q.record(a)["core_session_id"]
    assert q.record(a)["status"] == "cancelling" and q.executed == ["A"]
    q.stop_ack.set()
    q.gates["C"].set()
    await until(lambda: len(q.executed) == 3)
    assert q.executed == ["A", "C", "B"]
    assert q.record(a)["status"] == "cancelled"
    assert not any(
        event == "video.search.completed" and data["job_id"] == a
        for event, data in q.events
    )


@pytest.mark.parametrize("action", ["cancel", "preempt"])
async def test_rejected_stop_blocks_dispatch_without_claiming_stopped(queue, action):
    q = queue
    a = await q.start("A")
    await until(lambda: q.executed)
    b, c = await q.start("B"), await q.start("C")
    q.reject[0] = True
    assert (await q.control(c if action == "preempt" else a, action))["ok"]  # durable admission only
    await until(lambda: q.record(a)["error"])
    assert q.record(a)["status"] == "cancelling" and q.executed == ["A"]
    assert q.record(b)["status"] == q.record(c)["status"] == "queued"
    # The accepted reorder persists, but cannot release an unsettled execution.
    assert (q.record(c)["position"] < q.record(b)["position"]) == (action == "preempt")


async def test_wrong_scope_and_stale_queue_have_no_effect(queue):
    q = queue
    a = await q.start("A")
    await until(lambda: q.executed)
    b = await q.start("B")
    assert not (await q.control(b, "next", queue_version=-1))["ok"]
    assert not (await q.control(a, "cancel", search_session_id="other"))["ok"]
    assert q.record(b)["status"] == "queued" and not q.cancels


async def test_query_control_tools_return_receipts_without_creating_work(queue):
    q = queue
    a = await q.start("A")
    for call, name, arguments in [
        ("find", "jiuwen_task_query", {"job_id": a}),
        (
            "edit",
            "jiuwen_task_modify",
            {"job_id": a, "revision": 1, "instruction": "French"},
        ),
        ("stop", "jiuwen_task_cancel", {"job_id": a}),
    ]:
        await q.manager.handle_qwen_tool(
            None,
            call,
            {
                "search_session_id": "scope",
                "name": name,
                "call_id": call,
                "arguments": arguments,
            },
            None,
        )
        assert q.responses[-1]["ok"]
        assert "tool_result" in q.responses[-1]["payload"]
    assert len(q.manager.snapshot("alice", "scope")["jobs"]) == 1
    await asyncio.sleep(0.02)
    assert q.executed == q.cancels == []


async def test_voice_query_pages_large_history_without_forwarding_it(queue):
    q = queue
    for index in range(7):
        task_id = await q.start(f"pending-{index}")
        q.manager.service.store.update(task_id, lambda t: t.update(
            progress=[{"reasoning": "PRIVATE-HISTORY" * 30000}],
        ))
    found = []
    for offset in (0, 5):
        await q.manager.handle_qwen_tool(None, f"query-{offset}", {
            "search_session_id": "scope", "name": "jiuwen_task_query",
            "call_id": f"query-{offset}", "arguments": {"offset": offset},
        }, None)
        reply = q.responses[-1]
        assert reply["ok"]
        result = reply["payload"]["tool_result"]
        found.extend(t["job_id"] for t in result["jobs"])
        serialized = json.dumps(result, ensure_ascii=False)
        assert "PRIVATE-HISTORY" not in serialized
        assert len(serialized.encode("utf-8")) < 64000
        assert result["next_offset"] == (5 if offset == 0 else None)
    assert len(set(found)) == 7


@pytest.mark.parametrize(
    "remote,origin",
    [
        ("192.0.2.1", "http://127.0.0.1:5173"),
        ("127.0.0.1", "https://untrusted.example"),
        ("127.0.0.1", ""),
    ],
)
def test_task_identity_uses_connection_user_without_extra_address_checks(remote, origin):
    ws = SimpleNamespace(
        remote_address=(remote, 1234),
        request_headers={"Origin": origin},
        _web_connection_user_id="admin",
    )
    assert task_identity(ws, "scope") == ("admin", "scope")


@pytest.mark.parametrize("case", ["owned", "anonymous", "foreign", "missing_user", "unowned", "closing"])
async def test_connection_identity_and_saved_conversation_scope(monkeypatch, case):
    from jiuwenswarm.extensions.video_duplex.backend import task_adapter

    owner = None if case in {"anonymous", "missing_user"} else "alice"
    stored_owner = "" if case in {"anonymous", "unowned"} else "mallory" if case == "foreign" else "alice"

    async def metadata(*_):
        if case == "closing":
            raise ValueError("Conversation is closing")
        return {"user_id": stored_owner}

    monkeypatch.setattr(task_adapter, "task_agent_query", metadata)
    manager = object.__new__(video_search.VideoSearchManager)
    manager._execution_targets = {}
    manager.authorize, manager.client = task_identity, object()
    ws = SimpleNamespace(
        remote_address=("127.0.0.1", 1234), request_headers={"Origin": "http://127.0.0.1:5384"},
        _web_connection_user_id=owner,
    )
    scope = "task-duplex:saved-session"
    if case in {"foreign", "missing_user", "unowned", "closing"}:
        with pytest.raises(ValueError):
            await manager.authorized_scope(ws, scope)
    else:
        assert await manager.authorized_scope(ws, scope) == (owner or "", scope)


@pytest.mark.parametrize("remote", ["192.0.2.1", "2001:db8::1"])
@pytest.mark.parametrize("login", [None, "account-login", "expired-account-login"])
def test_task_identity_does_not_reinterpret_account_login(monkeypatch, remote, login):
    from jiuwenswarm.common.auth import service as auth

    account_lookup = Mock(side_effect=AssertionError("Account login is not the task identity authority"))
    monkeypatch.setattr(auth, "get_auth_service", account_lookup)
    ws = SimpleNamespace(
        remote_address=(remote, 1234), request_headers={"Origin": "https://voice.example"},
        _jiuwen_auth_session=login, _web_connection_user_id=" alice ",
    )

    assert task_identity(ws, "scope") == ("alice", "scope")
    account_lookup.assert_not_called()


@pytest.mark.parametrize("user", [None, "", " "])
def test_absent_connection_user_stays_anonymous(user):
    ws = SimpleNamespace(
        remote_address=("192.0.2.1", 1234), request_headers={"Origin": "https://voice.example"},
        _web_connection_user_id=user,
    )
    assert task_identity(ws, "scope") == ("", "scope")


@pytest.mark.parametrize("operation", ["status", "modify", "cancel", "reorder", "answer"])
@pytest.mark.parametrize("owner,other", [("alice", "bob"), ("alice", None), (None, "bob")])
async def test_remote_task_controls_preserve_connection_user_scope(queue, operation, owner, other):
    q = queue
    q.manager.authorize = task_identity
    owner_ws = SimpleNamespace(
        remote_address=("192.0.2.1", 1234), request_headers={"Origin": "https://voice.example"},
        _web_connection_user_id=owner,
    )
    other_ws = SimpleNamespace(
        remote_address=("192.0.2.2", 1234), request_headers={"Origin": "https://voice.example"},
        _web_connection_user_id=other,
    )
    task = await q.manager.start(owner_ws, question="A", query="A", search_session_id="scope", command_id="A")
    task_id = task.get("id")
    await until(lambda: q.executed == ["A"])

    def record():
        return q.manager.service.get(owner or "", "scope", task_id)

    before = record()
    params = {"search_session_id": "scope", "job_id": task_id}
    if operation == "status":
        await q.manager.handle_status(other_ws, "foreign-query", params, None)
    elif operation == "answer":
        await q.manager.handle_qwen_tool(other_ws, "foreign-answer", {
            "search_session_id": "scope", "name": "jiuwen_task_answer", "call_id": "foreign-answer",
            "arguments": {"job_id": task_id, "interaction_id": "question-a", "answers": ["3"]},
        }, None)
    else:
        await q.manager.handle_control(other_ws, "foreign-control", {
            **params, "action": "next" if operation == "reorder" else operation,
            "revision": before.get("revision"), "instruction": "B",
            "queue_version": q.manager.snapshot(owner or "", "scope").get("queue_version"),
        }, None)

    if operation == "answer":
        assert q.responses[-1].get("ok") is True
        rejection = q.responses[-1]["payload"]["tool_result"]
        assert rejection["code"] == "TASK_NOT_FOUND"
        assert rejection["applied"] is False
        assert "jobs" not in rejection and "result" not in rejection
    else:
        assert q.responses[-1].get("ok") is False
    assert record() == before
    assert q.executed == ["A"] and not q.cancels
    assert q.execution_users == [owner]
    q.gates["A"].set()
    await until(lambda: record().get("status") == "completed")
    await q.manager.handle_status(owner_ws, "owner-query", params, None)
    assert q.responses[-1].get("ok") is True
    assert q.responses[-1].get("payload", {}).get("result") == "A"


@pytest.mark.parametrize("owner", [None, "alice", "local:alice"])
async def test_remote_cancel_forwards_the_same_connection_user(queue, owner):
    q = queue
    q.manager.authorize = task_identity
    ws = SimpleNamespace(_web_connection_user_id=owner)
    task = await q.manager.start(ws, question="A", query="A", search_session_id="scope", command_id="A")
    task_id = task.get("id")
    await until(lambda: q.executed == ["A"])
    await q.manager.handle_control(ws, "stop", {
        "search_session_id": "scope", "job_id": task_id, "action": "cancel",
    }, None)
    assert q.responses[-1].get("ok") is True
    await until(lambda: q.manager.service.get(owner or "", "scope", task_id).get("status") == "cancelled")
    assert q.execution_users == [owner]
    assert len(q.cancels) == 1 and q.cancels[0].user_id == owner


async def test_anonymous_task_records_and_commands_remain_scoped(queue):
    q = queue
    q.manager.authorize = task_identity
    anonymous = SimpleNamespace()
    alice = SimpleNamespace(_web_connection_user_id="alice")
    first = await q.manager.start(anonymous, question="A", query="A", search_session_id="scope", command_id="same")
    retry = await q.manager.start(anonymous, question="A", query="A", search_session_id="scope", command_id="same")
    other = await q.manager.start(alice, question="B", query="B", search_session_id="scope", command_id="same")
    assert first.get("id") == retry.get("id") != other.get("id")
    assert len(q.manager.snapshot("", "scope").get("jobs")) == 1
    assert len(q.manager.snapshot("alice", "scope").get("jobs")) == 1
    for user, scope in [("alice", "scope"), ("", "other")]:
        with pytest.raises(ValueError):
            await q.manager.service.cancel(user, scope, first.get("id"), "stop")
    for task_id, owner in [(first.get("id"), ""), (other.get("id"), "alice")]:
        await q.manager.service.cancel(owner, "scope", task_id, "stop")
    with pytest.raises(ValueError):
        q.manager.service.submit(None, "scope", "missing", "C")
    await asyncio.sleep(0.02)
    assert q.executed == q.cancels == []


@pytest.mark.parametrize("owner", ["", "alice", "local:alice"])
async def test_answer_forwards_task_user_without_local_prefix_special_case(owner):
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor

    sent = []

    async def send_request(request):
        sent.append(request)
        return SimpleNamespace(ok=True, payload={})

    executor = AgentTaskExecutor(SimpleNamespace(send_request=send_request), None, None)
    await executor.answer({
        "id": "task-a", "request_id": "execution-request-a", "resume_answer": True,
        "owner": owner, "core_session_id": "execution-a", "request": {},
        "interaction": {
            "operation_id": "answer-a", "request_id": "question-a", "source": "ask_user_interrupt",
            "answers": [{"question": "Budget?", "answer": "5000"}],
        },
    })
    assert len(sent) == 1
    assert sent[0].user_id == (owner or None)
    assert sent[0].session_id == "execution-a"
    assert sent[0].params["managed_task_request"] == "execution-request-a"
    assert sent[0].params["managed_task_binding"]["task_id"] == "task-a"


async def test_cancel_completed_task_never_sends_cancel_to_following_execution(queue):
    q = queue
    first = await q.start("A")
    await until(lambda: q.executed == ["A"])
    second = await q.start("B")
    q.gates["A"].set()
    await until(lambda: q.executed == ["A", "B"])
    assert q.record(first)["status"] == "completed"
    receipt = await q.control(first, "cancel")
    assert receipt["ok"]
    assert q.cancels == []
    assert q.record(second)["status"] == "running"


@pytest.mark.parametrize(
    "scope", ["task-duplex:..", "task-duplex:C:other", "task-duplex:alias."]
)
def test_saved_conversation_rejects_filesystem_aliases(scope):
    ws = SimpleNamespace(
        remote_address=("127.0.0.1", 1234),
        request_headers={"Origin": "http://127.0.0.1:5173"},
    )
    with pytest.raises(ValueError, match="Invalid saved conversation"):
        task_identity(ws, scope)


async def test_qwen_independent_submission_and_exact_cancel_share_native_adapter(queue):
    q = queue
    a = await q.start('A')
    await until(lambda: q.executed == ['A'])
    await q.manager.handle_qwen_tool(None, 'parallel', {
        'name': 'jiuwen_delegate', 'call_id': 'weather', 'search_session_id': 'scope',
        'arguments': {'task': 'B', 'independent': True, 'resources': []}}, None)
    assert q.responses[-1]['ok']
    b = q.responses[-1]['payload']['search_job']['id']
    await until(lambda: q.executed == ['A', 'B'])
    assert q.record(a)['core_session_id'] != q.record(b)['core_session_id']
    assert (await q.control(b, 'cancel'))['ok']
    await until(lambda: q.record(b)['status'] == 'cancelled')
    assert q.record(a)['status'] == 'running'
    assert len(q.cancels) == 1 and q.cancels[0].session_id == q.record(b)['core_session_id']


async def test_query_summary_covers_unfinished_tasks_beyond_first_page(tmp_path):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
    from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore
    service = TaskService(TaskStore(tmp_path / "summary.sqlite"), None)
    service.started = True  # No executions: use isolated persisted records only.
    service.kick = Mock(return_value=None)
    manager = object.__new__(video_search.VideoSearchManager)
    manager._execution_targets = {}
    manager._service, manager.concurrency = service, 2
    for i in range(7):
        task = service.submit("alice", "scope", str(i), str(i))
        service.store.update(task["id"], lambda t, i=i: t.update(status="completed" if i < 5 else "running"))
    call = SimpleNamespace(name="jiuwen_task_query", arguments={})
    result = await manager.operate("alice", "scope", call)
    assert len(result["jobs"]) == 5 and result["next_offset"] == 5
    assert result["summary"]["unfinished"] == 2
    assert result["summary"]["all_finished"] is False
    assert result["summary"]["status_counts"] == {"completed": 5, "running": 2}
    call.arguments = {"status": "unfinished"}
    filtered = await manager.operate("alice", "scope", call)
    assert len(filtered["jobs"]) == 2 and filtered["next_offset"] is None
    assert filtered["summary"] == result["summary"]
    call.arguments = {"query": "不存在的名称 文件"}
    missing = await manager.operate("alice", "scope", call)
    assert missing["jobs"] == [] and missing["summary"]["all_finished"] is False
    assert "do not infer" in missing["message"]
    call.arguments = {"job_id": result["jobs"][0]["id"]}
    rejected = await manager.operate("mallory", "scope", call)
    assert rejected["code"] == "TASK_NOT_FOUND" and rejected["applied"] is False
    assert "jobs" not in rejected and "result" not in rejected


def test_voice_artifacts_use_file_events_not_answer_claims():
    result = video_search.VideoSearchManager.voice_job({"result": "已保存 /workspace/杭州.md"})
    assert result["artifact_status"] == "not_confirmed" and result["files"] == []
    result = video_search.VideoSearchManager.voice_job({
        "files": [{"name": "杭州.md", "path": "C:/work/杭州.md", "download_token": "secret"}]
    })
    assert result["files"] == [{"name": "杭州.md", "path": "C:/work/杭州.md"}]
    assert "secret" not in json.dumps(result)


async def test_native_file_history_is_bound_to_exact_execution(queue, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    q = queue
    task_id = await q.start("A")
    await until(lambda: q.executed == ["A"])
    task = q.record(task_id)
    seen = []

    def history(session_id):
        seen.append(session_id)
        return [
            {"request_id": task["request_id"], "event_type": "chat.file",
             "files": [{"name": "current.md", "path": "C:/work/current.md"}]},
            {"request_id": "previous-execution", "event_type": "chat.file",
             "files": [{"name": "old.md", "path": "C:/work/old.md"}]},
            {"request_id": task["request_id"], "event_type": "chat.final",
             "files": [{"name": "claimed.md", "path": "C:/work/claimed.md"}]},
        ]

    monkeypatch.setattr(session_history, "load_history_records", history)
    from jiuwenswarm.server.runtime.video_task.server_adapter import VoiceTaskServerAdapter
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.server.runtime.session import session_metadata, lifecycle
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {"user_id": "alice"})
    monkeypatch.setattr(lifecycle, "guard", lambda *_: None)

    async def remote_files(env):
        return await VoiceTaskServerAdapter().handle(e2a_to_agent_request(env))

    q.manager.client.send_request = remote_files
    q.gates["A"].set()
    await until(lambda: q.record(task_id)["status"] == "completed")
    public = q.manager.public(q.record(task_id))
    assert seen == [task["core_session_id"]]
    assert [f["name"] for f in public["files"]] == ["current.md"]


@pytest.mark.parametrize("action", ["next", "before"])
async def test_voice_reorder_rejects_stale_queue_then_accepts_new_request(tmp_path, monkeypatch, action):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
    from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore
    service = TaskService(TaskStore(tmp_path / "reorder.sqlite"), None)
    service.started = True
    service.kick = Mock(return_value=None)
    manager = object.__new__(video_search.VideoSearchManager)
    manager._execution_targets = {}
    manager._service, manager.concurrency = service, 2
    jobs = [service.submit("alice", "scope", str(i), str(i)) for i in range(8)]
    stale_version = manager.snapshot("alice", "scope").get("queue_version")
    for task in jobs[:5]:
        service.store.update(task["id"], lambda t: t.update(status="completed"))
    reorder = service.reorder

    def changed_wording(*args, **kwargs):
        try:
            return reorder(*args, **kwargs)
        except QueueVersionConflict as exc:
            raise QueueVersionConflict("Different wording for the same version conflict") from exc

    attempts = Mock(side_effect=changed_wording)
    monkeypatch.setattr(service, "reorder", attempts)
    call = SimpleNamespace(name="jiuwen_task_reorder", call_id="move-last", arguments={
        "job_id": jobs[7].get("id"), "action": action, "queue_version": stale_version})
    if action == "before":
        call.arguments["before_job_id"] = jobs[5].get("id")
    before = manager.snapshot("alice", "scope")
    with service.store.transaction() as db:
        command_count = db.execute("SELECT COUNT(*) FROM task_commands").fetchone()[0]
    rejected = await manager.operate("alice", "scope", call)
    attempts.assert_called_once()
    assert rejected.get("state") == "rejected" and rejected.get("applied") is False
    assert rejected.get("code") == "QUEUE_VERSION_CONFLICT"
    assert rejected.get("queue_version") == before.get("queue_version")
    assert rejected.get("target", {}).get("queue_position") == 3
    assert [t.get("id") for t in rejected.get("jobs", [])] == [t.get("id") for t in jobs[6:]]
    assert rejected.get("waiting_total") == 3
    assert "not applied" in rejected.get("message", "")
    assert manager.snapshot("alice", "scope") == before
    with service.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM task_commands").fetchone()[0] == command_count
    assert (await manager.operate("alice", "scope", call)).get("applied") is False
    assert manager.snapshot("alice", "scope") == before

    call.call_id = "move-last-fresh"
    call.arguments["queue_version"] = rejected.get("queue_version")
    result = await manager.operate("alice", "scope", call)
    assert [t["id"] for t in result["jobs"]] == [jobs[7]["id"], jobs[5]["id"], jobs[6]["id"]]
    assert result["target"]["queue_position"] == 1

    service.store.update(jobs[7]["id"], lambda t: t.update(status="completed"))
    before_replay = manager.snapshot("alice", "scope")
    replay = await manager.operate("alice", "scope", call)
    assert replay["operation"] == result["operation"]
    assert replay["target"]["status"] == "completed"
    assert manager.snapshot("alice", "scope") == before_replay


@pytest.mark.parametrize("entry", ["voice", "control"])
async def test_stale_reorder_rpc_never_changes_queue_or_starts_work(queue, entry):
    q = queue
    a = await q.start("A")
    stale_version = q.manager.snapshot("alice", "scope").get("queue_version")
    b = await q.start("B")
    before = q.manager.snapshot("alice", "scope")
    if entry == "voice":
        await q.manager.handle_qwen_tool(None, "old-order", {
            "search_session_id": "scope", "name": "jiuwen_task_reorder", "call_id": "old-order",
            "arguments": {"job_id": b, "action": "next", "queue_version": stale_version},
        }, None)
        reply = q.responses[-1].get("payload", {}).get("tool_result", {})
        assert reply.get("state") == "rejected" and reply.get("applied") is False
        assert reply.get("queue_version") == before.get("queue_version")
        assert [t.get("id") for t in reply.get("jobs", [])] == [a, b]
    else:
        response = await q.control(b, "next", queue_version=stale_version)
        assert response.get("ok") is False
    assert q.manager.snapshot("alice", "scope") == before
    assert q.executed == q.cancels == q.events == []


async def test_stale_voice_modification_returns_current_version_without_applying(queue, monkeypatch):
    q = queue
    task_id = await q.start("A")
    await until(lambda: q.executed == ["A"])
    q.manager.service.store.update(task_id, lambda t: t.update(revision=3))
    modify = q.manager.service.modify

    def changed_wording(*args, **kwargs):
        try:
            return modify(*args, **kwargs)
        except TaskRevisionConflict as exc:
            raise TaskRevisionConflict("Different wording for the same revision conflict") from exc

    monkeypatch.setattr(q.manager.service, "modify", changed_wording)
    call = SimpleNamespace(name="jiuwen_task_modify", call_id="old", arguments={
        "job_id": task_id, "revision": 1, "instruction": "new requirements"})
    result = await q.manager.operate("alice", "scope", call)
    assert result["state"] == "rejected" and result["applied"] is False
    assert result["current_task"]["revision"] == 3
    assert q.record(task_id)["changes"] == []
    call.call_id = "fresh"
    call.arguments["revision"] = 3
    accepted = await q.manager.operate("alice", "scope", call)
    assert accepted["state"] == "pending"
    assert len(q.record(task_id)["changes"]) == 1


@pytest.mark.parametrize("operation,message", [
    ("reorder", "Queue changed; query before reordering"),
    ("modify", "Task revision changed; query the current task"),
])
async def test_untyped_validation_errors_are_never_treated_as_conflicts(queue, monkeypatch, operation, message):
    task_id = await queue.start("A")
    before = queue.record(task_id)
    reject = Mock(side_effect=ValueError(message))
    monkeypatch.setattr(queue.manager.service, operation, reject)
    call = SimpleNamespace(name=f"jiuwen_task_{operation}", call_id="invalid", arguments={
        "job_id": task_id, "action": "next", "queue_version": 0, "revision": 1, "instruction": "change",
    })
    with pytest.raises(ValueError, match=message):
        await queue.manager.operate("alice", "scope", call)
    reject.assert_called_once()
    assert queue.record(task_id) == before
    assert queue.executed == queue.cancels == []


async def test_5597_voice_query_sees_scheduled_jobs_created_in_another_conversation(tmp_path, monkeypatch):
    """A Cron job created in conversation A is visible to a voice query in conversation B (#5597).

    The voice task list is conversation-scoped; scheduled jobs live in the Gateway Cron store,
    owner-scoped like the sidebar's cron.job.list. The query must read that store directly.
    """
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
    from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore
    from agent_gateway.cron.controller import CronController
    from jiuwenswarm.runtime.cron.store import CronJobStore

    cron_store = CronJobStore(tmp_path / "cron_jobs.json")
    scheduler = SimpleNamespace(project_execution_allowed=lambda *_: asyncio.sleep(0, result=True))
    monkeypatch.setattr(CronController, "_instance", CronController(store=cron_store, scheduler=scheduler))
    created = await cron_store.create_job(
        name="喝水提醒", cron_expr="0 9 * * *", timezone="Asia/Shanghai",
        description="提醒我喝水", targets="web", session_id="conversation-a", user_id="alice",
    )
    await cron_store.create_job(
        name="别人的提醒", cron_expr="0 8 * * *", timezone="Asia/Shanghai",
        description="other", targets="web", session_id="conversation-x", user_id="mallory",
    )

    service = TaskService(TaskStore(tmp_path / "scheduled.sqlite"), None)
    service.started = True
    service.kick = Mock(return_value=None)
    manager = video_search.VideoSearchManager(
        SimpleNamespace(), SimpleNamespace(), log_event=lambda _: None, qwen_active=lambda: True,
        path=tmp_path / "unused.sqlite",
    )
    manager._service = service

    query = SimpleNamespace(name="jiuwen_task_query", arguments={})
    result = await manager.operate("alice", "task-duplex:conversation-b", query)
    assert result["jobs"] == [], "Core invocations stay conversation-scoped"
    scheduled = result["scheduled_jobs"]
    assert scheduled["available"] is True and scheduled["total"] == 1
    assert [(job["id"], job["name"]) for job in scheduled["jobs"]] == [(created.id, "喝水提醒")]
    assert "all conversations" in scheduled["message"]
    # Same owner scope as the sidebar list.
    assert [job["id"] for job in await CronController.get_instance().list_jobs(user_id="alice")] == [created.id]

    query.arguments = {"query": "定时任务"}
    filtered = await manager.operate("alice", "task-duplex:conversation-b", query)
    assert filtered["scheduled_jobs"]["total"] == 1, "Invocation text filters never hide scheduled jobs"

    other = await manager.operate("bob", "task-duplex:conversation-b", SimpleNamespace(
        name="jiuwen_task_query", arguments={}))
    assert other["scheduled_jobs"]["total"] == 0

    anonymous = await manager.operate("", "task-duplex:conversation-b", SimpleNamespace(
        name="jiuwen_task_query", arguments={}))
    assert anonymous["scheduled_jobs"]["total"] == 0


async def test_5597_unreadable_scheduled_jobs_are_not_reported_as_none(tmp_path):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
    from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore

    async def broken(_owner):
        raise RuntimeError("cron store offline")

    service = TaskService(TaskStore(tmp_path / "broken.sqlite"), None)
    service.started = True
    manager = object.__new__(video_search.VideoSearchManager)
    manager._execution_targets = {}
    manager._service, manager.concurrency = service, 2
    manager.list_scheduled_jobs = broken
    result = await manager.operate("alice", "scope", SimpleNamespace(name="jiuwen_task_query", arguments={}))
    assert result["scheduled_jobs"]["available"] is False
    assert "do not claim there are none" in result["scheduled_jobs"]["message"]


@pytest.mark.parametrize("source", ["confirm_interrupt", "permission_interrupt"])
async def test_voice_is_told_how_to_answer_approval_cards(tmp_path, source):
    # Voice may answer approval cards, but only on explicit consent; the query receipt carries the rules.
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskService
    from jiuwenswarm.extensions.video_duplex.backend.tasks.store import TaskStore
    service = TaskService(TaskStore(tmp_path / "approval.sqlite"), None)
    service.started = True
    service.kick = Mock(return_value=None)
    manager = object.__new__(video_search.VideoSearchManager)
    manager._execution_targets = {}
    manager._service, manager.concurrency = service, 2
    card = {"id": "card-a", "request_id": "core-approval", "state": "pending", "source": source,
            "questions": [{"question": "cua_agent 将操作桌面：打开计算器", "header": "操作确认: cua_agent",
                           "options": [{"label": "允许"}, {"label": "拒绝"}]}]}
    task = service.submit("alice", "scope", "cua", "打开计算器")
    service.store.update(task["id"], lambda t: t.update(status="waiting_user", interaction=card))
    asking = service.submit("alice", "scope", "trip", "规划行程")
    service.store.update(asking["id"], lambda t: t.update(status="waiting_user", interaction={
        "id": "question-a", "request_id": "q", "state": "pending", "source": "ask_user_interrupt",
        "questions": [{"question": "预算是多少？"}]}))

    query = await manager.operate("alice", "scope", SimpleNamespace(name="jiuwen_task_query", arguments={}))
    jobs = {job["id"]: job for job in query["jobs"]}
    if source == "permission_interrupt":
        assert "approval_cards" not in jobs[task["id"]]
        assert "interaction" not in jobs[task["id"]]
        assert jobs[task["id"]]["interaction_requires_ui"] is True
    else:
        assert jobs[task["id"]]["approval_cards"] == [
            {"header": "操作确认: cua_agent", "message": "cua_agent 将操作桌面：打开计算器"}]
    assert "approval_cards" not in jobs[asking["id"]], "Information questions are not approval cards"
    message = query["message"]
    assert "answered by voice with jiuwen_task_answer" in message
    assert "同意, 允许, 可以 or 批准, choose the allow option" in message
    assert "拒绝, 不要 or 取消这个操作, choose the reject option" in message
    assert "only when the user says 本会话保持同意" in message
    assert "permanent option (e.g. 永久记住) only when the user explicitly says 永久同意" in message
    assert "ask one short question and do not submit" in message
    assert "several cards are pending, confirm which card" in message
    assert "Never use a new request as an answer to a card" in message
    assert "Cards left by typed chat also appear in jiuwen_task_query" in message
    assert "已批准 only after the answer receipt or a later query confirms the task resumed" in message
    assert "permission_interrupt" in message and "manual click" in message


@pytest.mark.parametrize("version_kind", ["quoted", "invalid", "omitted"])
async def test_voice_reorder_with_quoted_version_pushes_queue_and_changes_execution_order(queue, version_kind):
    q = queue
    await q.start("A")
    await until(lambda: q.executed == ["A"])
    desktop, weather = await q.start("B"), await q.start("C")
    before = q.manager.snapshot("alice", "scope")
    q.events.clear()
    await q.manager.handle_qwen_tool(None, "voice-move", {
        "name": "jiuwen_task_reorder", "call_id": "move-weather",
        "search_session_id": "scope", "question": "把天气任务调到最前面",
        "arguments": json.dumps({
            "job_id": weather, "action": "next",
            **({"queue_version": str(before["queue_version"])} if version_kind == "quoted" else
               {"queue_version": "invalid"} if version_kind == "invalid" else {}),
        }),
    }, None)
    assert q.responses[-1]["ok"]
    receipt = q.responses[-1]["payload"]["tool_result"]
    assert receipt["operation"]["state"] == "reordered"
    assert receipt["queue_version"] > before["queue_version"]
    await until(lambda: any(name == "video.search.queue" for name, _ in q.events))
    pushed = next(payload for name, payload in q.events if name == "video.search.queue")
    positions = {job["job_id"]: job["queue_position"] for job in pushed["jobs"]}
    assert positions[weather] == 1 and positions[desktop] == 2
    assert q.executed == ["A"], "Reordering must not interrupt current work"
    q.gates["A"].set()
    await until(lambda: len(q.executed) == 2)
    assert q.executed == ["A", "C"]
    q.gates["C"].set()
    await until(lambda: len(q.executed) == 3)
    assert q.executed == ["A", "C", "B"]
    q.gates["B"].set()
    await until(lambda: q.record(desktop)["status"] == "completed")


def test_qwen_exposes_queue_manager_and_preserves_voice_approval_answers():
    names = {t["function"]["name"] for t in qwen_omni_tools(queue_manager=True)}
    assert names == {"jiuwen_delegate", "jiuwen_queue_manage", "jiuwen_task_answer"}
    for args in ({}, {"instruction": " "}, {"instruction": "move", "job_id": "invented"}):
        with pytest.raises(ValueError):
            parse_qwen_omni_tool_call({"name": "jiuwen_queue_manage", "call_id": "m", "arguments": args})


@pytest.mark.asyncio
async def test_reorder_queries_then_executes_and_returns_real_receipts(monkeypatch):
    arguments = {"job_id": "feishu", "action": "before", "before_job_id": "wechat"}
    result, operate, _ = await run_queue(monkeypatch, [
        tool_proposal("jiuwen_task_reorder", arguments), '{"response":"已将飞书排在微信前面。"}',
    ], [{"jobs": [{"job_id": "wechat", "revision": 1}, {"job_id": "feishu", "revision": 1}]},
        {"operation": {"state": "reordered"}}])
    calls = operate.call_args_list
    assert calls[0].args[2].name == "jiuwen_task_query"
    assert calls[1].args[:2] == ("alice", "voice")
    assert calls[1].args[2].arguments == arguments
    assert result["receipts"][1]["receipt"]["operation"]["state"] == "reordered"


@pytest.mark.asyncio
async def test_unknown_ids_and_new_delegations_cannot_execute(monkeypatch):
    for name, arguments in [("jiuwen_task_cancel", {"job_id": "guessed"}), ("jiuwen_delegate", {"task": "new"})]:
        result, operate, _ = await run_queue(monkeypatch, tool_proposal(name, arguments), {"jobs": []})
        assert result["state"] == "incomplete"
        assert operate.await_count == 1


@pytest.mark.asyncio
async def test_joyai_manager_cannot_submit_information_answers(monkeypatch):
    result, operate, step = await run_queue(monkeypatch, tool_proposal("jiuwen_task_answer", {
        "job_id": "form", "interaction_id": "question", "answers": ["yes"],
    }), {"jobs": [{"job_id": "form", "revision": 1}]}, allow_answers=False, reply_language="en")
    assert result["state"] == "incomplete"
    assert operate.await_count == 1
    context = json.loads(step.call_args.args[0][1]["content"])
    assert context["reply_language"] == "en"
    assert "jiuwen_task_answer" not in [t["function"]["name"] for t in context["allowed_tools"]]


@pytest.mark.asyncio
async def test_failure_after_write_preserves_receipt_without_repeating(monkeypatch):
    result, operate, _ = await run_queue(monkeypatch, [
        tool_proposal("jiuwen_task_cancel", {"job_id": "feishu"}), TimeoutError(),
    ], [{"jobs": [{"job_id": "feishu", "revision": 1}]}, {"state": "cancelling"}])
    assert result["state"] == "incomplete"
    assert result["receipts"][-1]["receipt"]["state"] == "cancelling"
    assert operate.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", "not json", '{"tool":{"name":[],"arguments":{}}}',
    '{"tool":{"name":"jiuwen_task_modify","arguments":{"job_id":"a","revision":0,"instruction":"change"}}}'])
async def test_invalid_proposal_is_corrected_without_user_error(monkeypatch, invalid):
    result, operate, step = await run_queue(monkeypatch, [invalid, '{"response":"已查询当前任务。"}'],
                                          {"jobs": [{"job_id": "a", "revision": 1}]})
    assert "error" not in result
    assert result["response"] == "已查询当前任务。"
    assert operate.await_count == 1
    assert json.loads(step.call_args.args[0][-1]["content"])["applied"] is False



@pytest.mark.asyncio
async def test_duplicate_write_reuses_receipt_without_dispatch(monkeypatch):
    write = tool_proposal("jiuwen_task_cancel", {"job_id": "a"})
    result, operate, _ = await run_queue(monkeypatch, [write, write, '{"response":"正在停止。"}'],
                                        [{"jobs": [{"job_id": "a", "revision": 1}]}, {"state": "cancelling"}])
    assert "error" not in result
    assert operate.await_count == 2
    assert result["receipts"][1]["receipt"] == result["receipts"][2]["receipt"]



@pytest.mark.asyncio
async def test_operational_exception_is_not_replayed_as_validation_error(monkeypatch):
    result, operate, step = await run_queue(monkeypatch, tool_proposal("jiuwen_task_cancel", {"job_id": "a"}),
        [{"jobs": [{"job_id": "a", "revision": 1}]}, RuntimeError("connection lost after dispatch")])
    assert result["state"] == "incomplete"
    assert result["error"] == "connection lost after dispatch"
    assert operate.await_count == 2
    assert step.await_count == 1



@pytest.mark.asyncio
async def test_revision_conflict_can_query_then_correct_without_replaying_old_write(monkeypatch):
    def modification(revision):
        return tool_proposal("jiuwen_task_modify", {"job_id": "a", "revision": revision, "instruction": "change"})

    result, operate, _ = await run_queue(monkeypatch, [
        modification(1), tool_proposal("jiuwen_task_query", {}), modification(2), '{"response":"修改已受理。"}',
    ], [
        {"jobs": [{"job_id": "a", "revision": 1}]},
        {"state": "rejected", "applied": False, "error": "revision changed"},
        {"jobs": [{"job_id": "a", "revision": 2}]}, {"state": "accepted"},
    ])
    assert "error" not in result
    assert operate.await_count == 4
    assert operate.await_args_list[1].args[2].arguments["revision"] == 1
    assert operate.await_args_list[3].args[2].arguments["revision"] == 2



@pytest.mark.asyncio
async def test_gateway_duplicate_call_shares_execution(monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager

    channel = SimpleNamespace(send_response=AsyncMock())
    manager = VideoSearchManager(channel, None, log_event=lambda *a, **k: None, qwen_active=lambda: True)
    manager.scope = AsyncMock(return_value=("alice", "voice"))
    gate = asyncio.Event()

    async def run(*args, **kwargs):
        await gate.wait()
        return {"managed_by": "text_queue_manager", "receipts": []}

    mocked = AsyncMock(side_effect=run)
    monkeypatch.setattr(queue_manager, "manage_queue", mocked)
    params = {"name": "jiuwen_queue_manage", "call_id": "m", "arguments": {"instruction": "调整顺序"}}
    tasks = [asyncio.create_task(manager.handle_qwen_tool(None, str(i), params, "")) for i in range(2)]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert mocked.await_count == 1
    assert channel.send_response.await_count == 2
    await manager.close()



@pytest.mark.asyncio
async def test_text_manager_changes_real_queue_order(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager
    from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore

    service = TaskService(TaskStore(tmp_path / "tasks.sqlite"), None)
    service.started = True
    service.kick = Mock()
    wechat = service.submit("alice", "voice", "create-wechat", "打开微信")
    feishu = service.submit("alice", "voice", "create-feishu", "打开飞书")
    manager = VideoSearchManager(None, None, log_event=lambda *a, **k: None, qwen_active=lambda: True)
    manager._service = service
    monkeypatch.setattr(queue_manager, "request_step", AsyncMock(side_effect=[
        json.dumps({"tool": {"name": "jiuwen_task_reorder", "arguments": {
            "job_id": feishu["id"], "action": "before", "before_job_id": wechat["id"]}}}),
        json.dumps({"response": "飞书已排在微信前面"}),
    ]))
    try:
        result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "先飞书再微信")
        assert "state" not in result
        tasks, _ = service.snapshot("alice", "voice")
        assert [t["id"] for t in sorted(tasks, key=lambda t: t["position"])] == [feishu["id"], wechat["id"]]
        assert result["receipts"][-1]["receipt"]["operation"]["state"] == "reordered"
    finally:
        await manager.close()



@pytest.mark.asyncio
async def test_pending_management_is_cancellable_and_does_not_repeat_operations(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending_step(messages):
        entered.set()
        await release.wait()
        return json.dumps({"response": "队列为空"})

    monkeypatch.setattr(queue_manager, "request_step", pending_step)
    pending = asyncio.create_task(queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "查询队列",
    ))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert manager.operate.await_count == 1



@pytest.mark.asyncio
async def test_model_request_preserves_configured_network_timeout(monkeypatch):
    from jiuwenswarm.common import config
    from openjiuwen.core.foundation import llm

    monkeypatch.delenv("DUPLEX_QUEUE_MANAGER_MODEL", raising=False)
    monkeypatch.setattr(config, "get_default_models", lambda: [{
        "model_client_config": {
            "model_name": "test-model", "client_provider": "OpenAI",
            "api_key": "test-only", "api_base": "https://example.invalid", "timeout": 77,
        },
    }])
    received = []
    invoke = AsyncMock(return_value=SimpleNamespace(content='{"response":"done"}'))

    def model(**kwargs):
        received.append(kwargs["model_client_config"])
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(llm, "Model", model)
    assert await queue_manager.request_step([]) == '{"response":"done"}'
    assert received[0].timeout == 77
    invoke.assert_awaited_once_with([])



@pytest.mark.asyncio
@pytest.mark.parametrize("proposal, decisions, queries, error", [
    ("invalid", 4, 1, ""),
    (json.dumps({"tool": {"name": "jiuwen_task_query", "arguments": {}}}), 6, 7, "operation limit"),
])
async def test_management_budgets_are_bounded(monkeypatch, proposal, decisions, queries, error):
    result, operate, step = await run_queue(monkeypatch, proposal, {"jobs": []})
    assert result["state"] == "incomplete"
    assert error in result["error"]
    assert step.await_count == decisions
    assert operate.await_count == queries


@pytest.mark.parametrize(
    "model_name,login",
    [("glm-5#3", "login-session"), ("glm-5#3", None),
     ("configured", "login-session"), ("glm-5#3", "expired-session"),
     ("glm-5#3", "revoked-session")],
)
async def test_duplex_login_auth_is_bound_to_caller_and_sent_on_each_model_request(
    monkeypatch, tmp_path, model_name, login,
):
    from agent_gateway.auth import apig, login_credentials, model_catalog, passthrough, service
    from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager
    from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import RecordingChannel

    monkeypatch.setattr(login_credentials, "_ref_key_cache", b"k" * 32)
    monkeypatch.setattr(model_catalog, "get_models", lambda *args, **kwargs: [
        model_catalog.LoginModel("glm-5", "GLM-5"),
    ])
    monkeypatch.setattr(apig, "resolve_apig_config", lambda **kwargs: SimpleNamespace(
        invoke_base_url="https://apig.example/v1",
    ))
    live_session = Mock(return_value=SimpleNamespace(
        user_id="account-alice", credential=SimpleNamespace(id_token="id-initial"),
    ))
    monkeypatch.setattr(service, "live_session", live_session)
    expired, refresh = object(), Mock()
    monkeypatch.setattr(passthrough, "expired_login_session", lambda params, session: (
        expired if session == "expired-session" else None
    ))
    monkeypatch.setattr(service, "get_auth_service", lambda: SimpleNamespace(try_refresh=refresh))
    monkeypatch.setattr(passthrough, "refreshed_credential_for_ref", lambda ref: (
        {"credential_ref": ref, "revoked": True} if login == "revoked-session" else
        {"credential_ref": ref, "api_key": "id-fresh"}
    ))
    sent = []

    async def send_request(envelope):
        payload = {}
        if envelope.method == ReqMethod.SESSION_GET_METADATA.value:
            payload = {
                "user_id": "alice", "mode": "agent", "model_name": model_name,
                "model_selection": {"type": "model", "id": "selected-login-model"},
            }
        elif envelope.method == ReqMethod.CHAT_SEND.value:
            sent.append(envelope)
            payload = {"content": "任务已完成"}
        return SimpleNamespace(ok=True, payload=payload)

    manager = VideoSearchManager(
        RecordingChannel(), SimpleNamespace(send_request=send_request),
        log_event=lambda event: None, qwen_active=lambda: True, path=tmp_path / "tasks.sqlite",
    )
    try:
        public = await manager.start(
            SimpleNamespace(_web_connection_user_id="alice", _jiuwen_auth_session=login),
            question="执行任务", query="执行任务", search_session_id="task-duplex:saved",
            command_id="task-call", scheduling={"login_credential_ref": "forged"},
        )
        await until(lambda: manager.service.store.read(public["id"])["status"] == "completed")
        task = manager.service.store.read(public["id"])
        bound = model_name.startswith("glm-5") and login is not None
        authenticated = bound and login != "revoked-session"
        ref = login_credentials.credential_ref_for_user("account-alice")
        assert task["request"].get("login_credential_ref") == (ref if bound else None)
        persisted = json.dumps(task)
        assert all(secret not in persisted for secret in ("id-initial", "id-fresh", "login-session", "expired-session"))
        assert "login_credential_ref" not in json.dumps(public)

        task["interaction"] = {
            "request_id": "question", "operation_id": "answer-op", "source": "ask_user_interrupt",
            "answers": [{"question": "预算？", "answer": "100"}],
        }
        await manager.service.executor.answer(task)
        task["resume_answer"] = True
        await manager.service.executor._run(task, AsyncMock(), [])
        assert len(sent) == 3
        for envelope in sent:
            auth = envelope.params.get(E2A_MODEL_AUTH_PARAM_KEY)
            if authenticated:
                assert auth == {
                    "api_base": "https://apig.example/v1", "api_key": "id-fresh", "credential_ref": ref,
                }
                assert envelope.params["model_name"] == model_name
            else:
                assert auth is None
            assert envelope.params.get("model_selection") == {
                "type": "model", "id": "selected-login-model",
            }
        if bound:
            live_session.assert_called_once_with(login, allow_refresh=False)
        else:
            live_session.assert_not_called()
        if login == "expired-session":
            refresh.assert_called_once_with(expired)
        else:
            refresh.assert_not_called()
    finally:
        await manager.close()
