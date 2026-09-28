"""Video adapter regressions over the persisted task service (no private queue mirrors)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import task_identity
from jiuwenswarm.extensions.video_duplex.backend.tasks.errors import QueueVersionConflict, TaskRevisionConflict


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


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


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
        executed.append(name)
        execution_users.append(kwargs.get("user_id"))
        await gates[name].wait()
        with manager.service.store.transaction() as db:
            task = next(
                t
                for t in manager.service.store.rows(db)
                if t["request_id"] == kwargs["request_id"]
            )
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
        "owner": owner, "core_session_id": "execution-a",
        "interaction": {
            "operation_id": "answer-a", "request_id": "question-a", "source": "ask_user_interrupt",
            "answers": [{"question": "Budget?", "answer": "5000"}],
        },
    })
    assert len(sent) == 1
    assert sent[0].user_id == (owner or None)
    assert sent[0].session_id == "execution-a"


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
    with pytest.raises(ValueError, match="not found"):
        await manager.operate("mallory", "scope", call)


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
    from jiuwenswarm.extensions.video_duplex.backend.tasks.server_adapter import VoiceTaskServerAdapter
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
