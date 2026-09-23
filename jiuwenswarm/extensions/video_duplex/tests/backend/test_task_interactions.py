"""Task identity, queue retries and actual answer transport boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import LocalCheckpoint

from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import InteractionPending
from jiuwenswarm.extensions.video_duplex.backend.tasks.interactions import information_question
from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import bind_task_execution
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.video_search import execute_core_agent
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import (
    parse_qwen_omni_tool_call,
)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def token(service, task):
    return service.store.read(task["id"])["interaction"]["id"]


def reply(text="5000"):
    return [{"question": "预算是多少？", "answer": text}]


def test_voice_answers_bind_original_text_and_reject_missing_or_mismatched_answers():
    from jiuwenswarm.extensions.video_duplex.backend.tasks.interactions import validate_answers

    question = information_question(dict(
        request_id="q", source="ask_user_interrupt",
        questions=[{"question": "请问总预算是多少（人民币）？"}, {"question": "同行人数？"}],
    ))
    answers = validate_answers(question, ["5000", "2"])
    assert [a["question"] for a in answers] == [q["question"] for q in question["questions"]]
    assert [a["answer"] for a in answers] == ["5000", "2"]
    for invalid in [True, None, 5000, {}, {"question": "其他问题", "answer": "5000"}, ""]:
        with pytest.raises(ValueError):
            validate_answers(question, [invalid, "2"])
    with pytest.raises(ValueError):
        validate_answers(question, ["5000"])


async def test_voice_answer_replay_preserves_answer_dto_without_duplicate_execution(system):
    service, executor = system
    task = submit(service, "index")
    await until(lambda: service.store.read(task["id"])["output_closed"])
    args = ["230"]
    receipt = await service.answer("user", "voice", task["id"], "indexed", token(service, task), answers=args)
    assert await service.answer("user", "voice", task["id"], "indexed", token(service, task), answers=args) == receipt
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    assert service.store.read(task["id"])["result"]["answer"] == "230"


async def test_cancel_closes_only_the_exact_output_lease_without_claiming_settlement(system):
    from unittest.mock import AsyncMock
    from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import (
        TaskExecutionBinding, bind_task_output, close_task_output,
    )

    service, executor = system
    task = submit(service, "lease")
    checkpoint = TaskExecutionBinding(LocalCheckpoint(service.store, task), task, None)
    rail = SimpleNamespace(managed_tasks={task["core_session_id"]: checkpoint})
    stream = SimpleNamespace(close=AsyncMock())
    request = SimpleNamespace(session_id=task["core_session_id"], request_id=task["request_id"])
    await bind_task_output(rail, request, stream)
    request.params = {"managed_task_request": "stale"}
    service.store.update(task["id"], lambda t: t.update(status="cancelling"))
    await close_task_output(rail, request)
    stream.close.assert_not_awaited()
    request.params["managed_task_request"] = task["request_id"]
    await close_task_output(rail, request)
    stream.close.assert_awaited_once_with(abort_active_round=False)
    assert not service.store.read(task["id"])["execution_settled"]
    assert service.store.read(task["id"])["status"] == "cancelling"


async def test_cancel_wakes_native_output_waiter_after_round_already_stopped(system):
    from openjiuwen.harness.schema.interaction import OutputLeaseManager, InteractionOutputStream
    from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import (
        TaskExecutionBinding,
        bind_task_output,
        close_task_output,
    )

    service, _ = system
    task = submit(service, "idle-output")
    outputs = OutputLeaseManager()

    class Agent:
        async def next_output(self, lease):
            return await outputs.next_item(lease)

        async def detach_output(self, lease, *, abort_active_round):
            assert not abort_active_round
            await outputs.detach(lease)

    stream = InteractionOutputStream(Agent(), await outputs.attach())
    binding = TaskExecutionBinding(LocalCheckpoint(service.store, task), task, None)
    rail = SimpleNamespace(managed_tasks={task["core_session_id"]: binding})
    request = SimpleNamespace(session_id=task["core_session_id"], request_id=task["request_id"],
                              params={"managed_task_request": task["request_id"]})
    await bind_task_output(rail, request, stream)
    service.store.update(task["id"], lambda t: t.update(status="cancelling"))
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await close_task_output(rail, request)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 1)
    assert not service.store.read(task["id"])["execution_settled"]


class QuestionExecutor:
    def __init__(self, store):
        self.store, self.calls, self.stops = store, [], []

    async def run(self, task, progress):
        self.calls.append(task)
        if task.get("resume_answer"):
            answer = AgentTaskExecutor.answer_input(task).to_agent_request()
            assert answer.params["request_id"] == "question-" + task["id"]
            self.store.update(task["id"], lambda t: t.update(execution_settled=True))
            return {"answer": answer.params["answers"][0]["custom_input"]}
        if task["instruction"] == "weather":
            self.store.update(task["id"], lambda t: t.update(execution_settled=True))
            return {"answer": "weather result"}
        interaction = information_question(
            dict(
                request_id="question-" + task["id"],
                source="ask_user_interrupt",
                questions=[{"question": "预算是多少？"}],
            )
        )
        await progress(dict(stage="interaction", interaction=interaction))
        self.store.update(task["id"], lambda t: t.update(execution_settled=True))
        raise InteractionPending()

    async def cancel(self, task):
        self.stops.append(task["id"])


@pytest.fixture
async def system(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    executor = QuestionExecutor(store)
    service = TaskService(store, executor, concurrency=1)
    yield service, executor
    await service.close()


def submit(service, key, text="trip"):
    return service.submit(
        "user", "voice", key, text, {"independent": True, "resources": []}
    )


async def test_answer_resumes_same_task_and_waiting_releases_only_settled_capacity(
    system,
):
    service, executor = system
    settling = asyncio.Event()
    original_run = executor.run

    async def held_question(task, progress):
        async def observe(entry):
            await progress(entry)
            if entry.get("stage") == "interaction":
                await settling.wait()
        return await original_run(task, observe)

    executor.run = held_question
    trip = submit(service, "trip")
    await until(lambda: service.store.read(trip["id"])["status"] == "waiting_user")
    weather = submit(service, "weather", "weather")
    await asyncio.sleep(0.02)
    assert len(executor.calls) == 1
    assert service.store.read(weather["id"])["status"] == "queued"
    assert not service.store.read(trip["id"])["execution_settled"]
    settling.set()
    await until(lambda: service.store.read(weather["id"])["status"] == "completed")
    receipt = await service.answer(
        "user", "voice", trip["id"], "reply", token(service, trip), answers=reply()
    )
    assert (
        await service.answer(
            "user", "voice", trip["id"], "reply", token(service, trip), answers=reply()
        )
        == receipt
    )
    await until(lambda: service.store.read(trip["id"])["status"] == "completed")
    final = service.store.read(trip["id"])
    assert final["result"]["answer"] == "5000"
    assert final["core_session_id"] == trip["core_session_id"]
    assert final["request_id"] != trip["request_id"]
    assert len(service.snapshot("user", "voice")[0]) == 2
    assert len(executor.calls) == 3
    with pytest.raises(ValueError):
        await service.answer(
            "user", "voice", trip["id"], "late", token(service, trip), answers=reply()
        )


async def test_questions_do_not_cross_tasks_and_cancel_prevents_resume(system):
    service, executor = system
    a, b = submit(service, "a"), submit(service, "b")
    await until(lambda: service.store.read(b["id"])["output_closed"])
    before = service.snapshot("user", "voice")
    calls = len(executor.calls)
    for owner, scope, interaction_id in [
        ("other", "voice", token(service, a)),
        ("user", "other", token(service, a)),
        ("user", "voice", token(service, b)),
    ]:
        with pytest.raises(ValueError):
            await service.answer(
                owner, scope, a["id"], "bad", interaction_id, answers=reply()
            )
    assert service.snapshot("user", "voice") == before
    assert len(executor.calls) == calls
    assert executor.stops == []
    await service.cancel("user", "voice", a["id"], "cancel")
    await until(lambda: service.store.read(a["id"])["status"] == "cancelled")
    with pytest.raises(ValueError):
        await service.answer(
            "user", "voice", a["id"], "late", token(service, a), answers=reply()
        )
    await service.answer(
        "user", "voice", b["id"], "b-reply", token(service, b), answers=reply("230")
    )
    await until(lambda: service.store.read(b["id"])["status"] == "completed")
    assert service.store.read(b["id"])["result"]["answer"] == "230"
    assert len(executor.calls) == 3


async def test_reorder_receipt_replays_before_version_check(system):
    service, executor = system
    a, b = submit(service, "a"), submit(service, "b")
    version = service.snapshot("user", "voice")[1]
    receipt = service.reorder(
        "user", "voice", b["id"], version, a["id"], command_id="order"
    )
    assert (
        service.reorder("user", "voice", b["id"], version, a["id"], command_id="order")
        == receipt
    )
    with pytest.raises(ValueError):
        service.reorder("user", "voice", a["id"], version, b["id"], command_id="order")
    assert executor.calls == executor.stops == []


@pytest.mark.parametrize(
    "source", ["permission_interrupt", "confirm_interrupt", "evolution_interrupt"]
)
def test_voice_never_accepts_approval(source):
    with pytest.raises(ValueError):
        information_question(
            dict(
                request_id="approval",
                source=source,
                questions=[{"question": "允许吗？"}],
            )
        )


@pytest.mark.parametrize(
    "args",
    [
        {"job_id": "a", "queue_version": 1, "action": "before"},
        {"job_id": "a", "queue_version": 1, "action": "next", "before_job_id": "b"},
        {"job_id": "a", "queue_version": 1, "action": "preempt"},
    ],
)
def test_ambiguous_or_destructive_queue_arguments_are_rejected(args):
    with pytest.raises(ValueError):
        parse_qwen_omni_tool_call(
            dict(name="jiuwen_task_reorder", call_id="call", arguments=args)
        )


async def test_agent_question_is_not_a_completed_result():
    class Client:
        async def send_request_stream(self, env):
            yield SimpleNamespace(
                payload=dict(
                    event_type="chat.ask_user_question",
                    request_id="q",
                    source="ask_user_interrupt",
                    questions=[{"question": "预算是多少？"}],
                )
            )
            yield SimpleNamespace(
                payload=dict(event_type="chat.final", content="等待预算")
            )

    progress = []

    async def observe(entry):
        progress.append(entry)

    with pytest.raises(InteractionPending):
        await execute_core_agent(
            Client(),
            question="trip",
            query="trip",
            visual_context="",
            search_session_id="voice",
            on_progress=observe,
        )
    assert progress[0]["interaction"]["request_id"] == "q"


async def test_answer_ack_does_not_finish_the_original_live_stream(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    gate = asyncio.Event()

    class LiveExecutor:
        async def run(self, task, progress):
            await progress(
                dict(
                    stage="interaction",
                    interaction=information_question(
                        dict(
                            request_id="live",
                            source="ask_user",
                            questions=[{"question": "预算是多少？"}],
                        )
                    ),
                )
            )
            await gate.wait()
            store.update(task["id"], lambda t: t.update(execution_settled=True))
            return {"answer": "5000"}

        async def answer(self, task):
            assert task["interaction"]["answers"] == reply()

    service = TaskService(store, LiveExecutor())
    try:
        task = submit(service, "live")
        await until(lambda: store.read(task["id"])["status"] == "waiting_user")
        await service.answer(
            "user", "voice", task["id"], "answer", token(service, task), answers=reply()
        )
        assert store.read(task["id"])["status"] == "running"
        assert store.read(task["id"])["result"] is None
        gate.set()
        await until(lambda: store.read(task["id"])["status"] == "completed")
    finally:
        await service.close()


async def test_repeated_question_never_replays_an_accepted_answer(system):
    service, executor = system
    original_run = executor.run
    callbacks = []

    async def repeat(task, progress):
        callbacks.append(progress)
        if not task.get("resume_answer"):
            return await original_run(task, progress)
        executor.calls.append(task)
        assert task["interaction"]["state"] == "submitted"
        await progress(
            dict(
                stage="interaction",
                interaction=information_question(
                    dict(
                        request_id="question-" + task["id"],
                        source="ask_user_interrupt",
                        questions=[{"question": "预算是多少？"}],
                    )
                ),
            )
        )
        service.store.update(task["id"], lambda t: t.update(execution_settled=True))
        raise InteractionPending()

    executor.run = repeat
    task = submit(service, "repeat")
    await until(lambda: service.store.read(task["id"])["output_closed"])
    old_token = token(service, task)
    await service.answer(
        "user", "voice", task["id"], "answer", old_token, answers=reply()
    )
    await until(
        lambda: (
            len(executor.calls) == 2 and service.store.read(task["id"])["output_closed"]
        )
    )
    await asyncio.sleep(0.03)
    assert len(executor.calls) == 2
    assert service.store.read(task["id"])["interaction"]["state"] == "pending"
    current = service.store.read(task["id"])
    assert executor.calls[0]["request_id"] != executor.calls[1]["request_id"]
    await callbacks[0]({"stage": "tool_result", "content": "late result from before resume"})
    assert service.store.read(task["id"]) == current
    with pytest.raises(ValueError):
        await service.answer(
            "user", "voice", task["id"], "stale-answer", old_token, answers=reply()
        )


async def test_cancel_accepted_answer_before_dispatch_does_not_resume(system):
    service, executor = system
    original = executor.run
    gate = asyncio.Event()

    async def run(task, progress):
        if task["instruction"] == "hold":
            executor.calls.append(task)
            await gate.wait()
            return {"answer": "held"}
        return await original(task, progress)

    executor.run = run
    task = submit(service, "question")
    await until(lambda: service.store.read(task["id"])["output_closed"])
    submit(service, "hold", "hold")
    await until(lambda: len(executor.calls) == 2)
    old_request = service.store.read(task["id"])["request_id"]
    await service.answer(
        "user", "voice", task["id"], "answer", token(service, task), answers=reply()
    )
    assert service.store.read(task["id"])["status"] == "queued"
    await service.cancel("user", "voice", task["id"], "cancel")
    await until(lambda: service.store.read(task["id"])["status"] == "cancelled")
    assert service.store.read(task["id"])["request_id"] == old_request
    assert executor.stops == [task["id"]]
    assert len(executor.calls) == 2


async def test_restart_expires_questions_without_replaying_answers(system):
    service, executor = system
    task = submit(service, "restart")
    await until(lambda: service.store.read(task["id"])["output_closed"])
    await service.close()
    restarted = TaskService(service.store, executor)
    try:
        current = restarted.get("user", "voice", task["id"])
        assert current["status"] == "unknown"
        assert current["interaction"]["state"] == "expired"
        with pytest.raises(ValueError):
            await restarted.answer(
                "user",
                "voice",
                task["id"],
                "late",
                token(service, task),
                answers=reply(),
            )
        assert len(executor.calls) == 1
    finally:
        await restarted.close()


async def test_managed_execution_without_authoritative_record_is_rejected(tmp_path):
    path = tmp_path / "missing.sqlite"
    request = SimpleNamespace(
        channel_id="video_tool", session_id="managed-task-missing", request_id="stale", params={}, user_id="user"
    )
    adapter = SimpleNamespace(_is_session_scoped_adapter=True)
    for create_database in (False, True):
        if create_database:
            TaskStore(path)
        with pytest.raises(RuntimeError, match="Managed task"):
            async with bind_task_execution(request, adapter, {}):
                pytest.fail("No Agent may run without its task record")


async def test_completed_resumed_task_survives_restart(system):
    service, executor = system
    task = submit(service, "completed-resume")
    await until(lambda: service.store.read(task["id"])["output_closed"])
    await service.answer(
        "user", "voice", task["id"], "answer", token(service, task), answers=reply()
    )
    await until(lambda: service.store.read(task["id"])["status"] == "completed")
    await service.close()
    restarted = TaskService(service.store, executor)
    try:
        assert restarted.get("user", "voice", task["id"])["result"]["answer"] == "5000"
        assert restarted.get("user", "voice", task["id"])["status"] == "completed"
        assert len(executor.calls) == 2
    finally:
        await restarted.close()


async def test_slow_notification_cannot_hide_durable_cancel_receipt(system):
    service, executor = system
    task = submit(service, "cancel-before-start")

    async def blocked(_task):
        await asyncio.Event().wait()

    service.on_change = blocked
    async with asyncio.timeout(3):
        receipt = await service.cancel("user", "voice", task["id"], "stop")
    assert receipt["state"] == "cancelled"
    assert executor.calls == []


async def test_confirmed_native_cancellation_closes_stuck_output_observer(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    entered = asyncio.Event()

    class Executor:
        async def run(self, task, progress):
            entered.set()
            await asyncio.Event().wait()

        async def cancel(self, task):
            store.update(
                task["id"],
                lambda t: t.update(execution_settled=True, execution_cancelled=True),
            )

    service = TaskService(store, Executor())
    try:
        task = submit(service, "stuck-output")
        await entered.wait()
        receipt = await service.cancel("user", "voice", task["id"], "stop")
        assert receipt["state"] == "accepted"
        await until(lambda: store.read(task["id"])["status"] == "cancelled")
        assert store.read(task["id"])["output_closed"]
    finally:
        await service.close()
