"""Pure-voice approval: new voice tasks queue behind a live card, never answer it (#5486)."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.interactions import native_approval_question
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import InteractionPending

SCOPE = "task-duplex:chat"
OPTIONS = [{"label": "允许", "value": "allow_once"}, {"label": "拒绝", "value": "reject"}]


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class CoreSession:
    """One Core session shared by typed chat and voice tasks."""

    def __init__(self, store):
        self.store, self.card_live, self.runs, self.probes = store, False, [], 0

    async def pending_interrupt(self, task):
        self.probes += 1
        return self.card_live

    async def run(self, task, progress):
        # Core refuses a managed task while a card waits (interface_deep guard).
        assert not self.card_live or task.get("resume_answer"), "task was fed to a live card"
        self.runs.append((task["instruction"], bool(task.get("resume_answer"))))
        if task["instruction"] == "删除文件" and not task.get("resume_answer"):
            await progress(dict(stage="interaction", interaction=native_approval_question({
                "request_id": "core-approval", "source": "permission_interrupt",
                "questions": [{"question": "允许删除？", "options": OPTIONS, "card_id": "card-1"}],
            })))
            self.card_live = True
            self.store.update(task["id"], lambda t: t.update(execution_settled=True))
            raise InteractionPending()
        if task.get("resume_answer"):
            self.card_live = False
            picked = task["interaction"]["answers"][0]["selected_options"]
            return {"answer": "deleted" if picked == ["允许"] else "kept"}
        return {"answer": "done " + task["instruction"]}


def submit(service, key, text):
    return service.submit("alice", SCOPE, key, text, {"independent": True, "resources": []})


@pytest.fixture
async def system(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    core = CoreSession(store)
    service = TaskService(store, core)
    yield service, core, store
    await service.close()


@pytest.mark.parametrize("spoken, outcome", [("允许", "deleted"), ("拒绝", "kept")])
async def test_voice_answer_on_managed_card_then_queued_task_runs(system, spoken, outcome):
    service, core, store = system
    first = submit(service, "first", "删除文件")
    await until(lambda: store.read(first["id"])["status"] == "waiting_user"
                and store.read(first["id"])["output_closed"])
    second = submit(service, "second", "打开微信")
    await asyncio.sleep(0.05)
    assert store.read(second["id"])["status"] == "queued"
    assert store.read(first["id"])["interaction"]["state"] == "pending", "the card stays valid"

    with pytest.raises(ValueError, match="card options"):
        await service.answer("alice", SCOPE, first["id"], "vague", store.read(first["id"])["interaction"]["id"],
                             answers=["好的"])
    await service.answer("alice", SCOPE, first["id"], "voice", store.read(first["id"])["interaction"]["id"],
                         answers=[spoken])
    await until(lambda: store.read(second["id"])["status"] == "completed")
    assert store.read(first["id"])["result"]["answer"] == outcome
    assert core.runs == [("删除文件", False), ("删除文件", True), ("打开微信", False)]


async def test_typed_card_holds_voice_task_until_turn_ends(system):
    service, core, store = system
    service.start()
    core.card_live = True  # A typed chat turn left a card on screen.
    task = submit(service, "voice", "打开微信")
    await until(lambda: core.probes >= 1)
    await asyncio.sleep(0.05)
    assert store.read(task["id"])["status"] == "queued"
    assert core.probes == 1, "a held task is not re-probed until the card settles"
    tasks, _ = service.snapshot("alice", SCOPE)
    assert tasks[0]["wait_reason"] == "pending_interrupt"

    # The user clicks on screen: the resumed typed turn ends.
    core.card_live = False
    service.interrupt_settled("chat")
    await until(lambda: store.read(task["id"])["status"] == "completed")
    assert core.runs == [("打开微信", False)]


class FakeChannel:
    def __init__(self):
        self.listeners, self.dispatched, self.events = [], [], []
        self.resumes = True

    def add_session_event_listener(self, listener):
        self.listeners.append(listener)

    async def dispatch_client_request(self, ws, method, params):
        self.dispatched.append((method, params))
        if self.resumes:
            # Core resumes the answered turn.
            self.push("chat.processing_status", {"is_processing": True})

    async def send_event(self, ws, event, payload):
        self.events.append((event, payload))

    def push(self, event, payload):
        for listener in self.listeners:
            listener(event, "chat", payload)


def manager_for(service, core, channel):
    manager = video_search.VideoSearchManager(
        channel, None, log_event=lambda _: None, qwen_active=lambda: True,
        authorize=lambda ws, scope: ("alice", scope),
    )
    manager._service = service

    async def query(client, method, params, session, owner):
        assert session == "chat" and owner == "alice"
        return {"pending": core.card_live}

    return manager, query


@pytest.mark.parametrize("spoken", ["允许", "拒绝"])
async def test_voice_answers_typed_card_through_click_path(system, monkeypatch, spoken):
    from jiuwenswarm.extensions.video_duplex.backend import task_adapter

    service, core, store = system
    channel = FakeChannel()
    manager, query = manager_for(service, core, channel)
    monkeypatch.setattr(task_adapter, "task_agent_query", query)
    ws = object()
    manager.subscribers[("alice", SCOPE, id(ws))] = ws
    core.card_live = True
    channel.push("chat.ask_user_question", {
        "request_id": "typed-approval", "source": "permission_interrupt",
        "questions": [{"question": "允许执行 rm？", "header": "权限审批: bash", "options": OPTIONS,
                       "card_id": "card-9"}],
    })
    channel.push("chat.processing_status", {"is_processing": False})
    task = submit(service, "voice", "打开微信")
    await until(lambda: core.probes >= 1)

    receipt = await manager.operate("alice", SCOPE, SimpleNamespace(
        name="jiuwen_task_query", arguments={}, call_id="q"))
    card = receipt["chat_approval"]
    assert card["job_id"] == "" and card["interaction_id"] == "typed-approval"
    assert [o["label"] for o in card["interaction"]["questions"][0]["options"]] == ["允许", "拒绝"]

    answer = SimpleNamespace(name="jiuwen_task_answer", call_id="a", arguments={
        "job_id": "", "interaction_id": "typed-approval", "answers": ["可以吧"]})
    rejected = await manager.operate("alice", SCOPE, answer, ws=ws)
    assert rejected["state"] == "rejected" and not channel.dispatched

    answer.arguments["answers"] = [spoken]
    accepted = await manager.operate("alice", SCOPE, answer, ws=ws)
    assert accepted["state"] == "accepted"
    method, params = channel.dispatched[-1]
    assert method == "chat.send" and params["session_id"] == "chat" and params["query"] == ""
    assert params["request_id"] == "typed-approval" and params["source"] == "permission_interrupt"
    assert params["answers"] == [{"custom_input": "", "question": "允许执行 rm？",
                                  "selected_options": [spoken], "card_id": "card-9"}]
    assert ("video.search.confirmation_closed", {"session_id": "chat", "request_id": "typed-approval"}) \
        in channel.events, "the on-screen card closes"
    assert store.read(task["id"])["status"] == "queued"

    # The resumed typed turn runs, then ends: the queued voice task dispatches.
    core.card_live = False
    channel.push("chat.processing_status", {"is_processing": False})
    await until(lambda: store.read(task["id"])["status"] == "completed")
    assert (await manager.chat_card("alice", SCOPE)) is None


async def test_typed_card_answer_is_not_reported_applied_until_core_resumes(system, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend import task_adapter

    service, core, _store = system
    channel = FakeChannel()
    channel.resumes = False
    manager, query = manager_for(service, core, channel)
    monkeypatch.setattr(task_adapter, "task_agent_query", query)
    monkeypatch.setattr(task_adapter, "CHAT_ANSWER_CONFIRM_TIMEOUT", 0.05)
    core.card_live = True
    channel.push("chat.ask_user_question", {
        "request_id": "typed-approval", "source": "permission_interrupt",
        "questions": [{"question": "允许执行 rm？", "header": "权限审批: bash", "options": OPTIONS}],
    })
    receipt = await manager.answer_chat_card(object(), "alice", SCOPE, "typed-approval", ["允许"])
    assert receipt["state"] == "submitted" and receipt["applied"] is False
    assert channel.dispatched and "query again" in receipt["message"]


async def test_joyai_may_answer_approval_cards_but_not_information_forms(system):
    service, core, store = system
    manager = object.__new__(video_search.VideoSearchManager)
    manager._service = service
    service.start()
    task = submit(service, "trip", "规划行程")
    service.store.update(task["id"], lambda t: t.update(status="waiting_user", interaction={
        "id": "question-a", "request_id": "q", "state": "pending", "source": "ask_user_interrupt",
        "questions": [{"question": "预算是多少？"}]}))
    call = SimpleNamespace(name="jiuwen_task_answer", call_id="a", arguments={
        "job_id": task["id"], "interaction_id": "question-a", "answers": ["5000"]})
    with pytest.raises(ValueError, match="JoyAI voice form answers are disabled"):
        await manager.operate("alice", SCOPE, call, approvals_only=True)


def test_web_channel_reports_card_and_turn_pushes_to_listeners():
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    channel = WebChannel.__new__(WebChannel)
    channel._session_busy, channel._session_event_listeners = {}, []
    channel._expert_processing_requests = {}
    seen = []
    channel.add_session_event_listener(lambda *event: seen.append(event))
    for name, payload in [("chat.ask_user_question", {"request_id": "r"}),
                          ("chat.processing_status", {"is_processing": False}),
                          ("chat.delta", {"content": "x"})]:
        channel._track_session_busy(SimpleNamespace(
            type="event", event_type=None, session_id="chat", payload={"event_type": name, **payload}))
    assert [(name, sid) for name, sid, _ in seen] == [
        ("chat.ask_user_question", "chat"), ("chat.processing_status", "chat")]
    assert channel.is_session_busy("chat") is False


async def test_web_channel_replays_request_through_its_own_dispatch():
    import json
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    channel = WebChannel.__new__(WebChannel)
    frames = []

    async def handle(ws, raw, query):
        frames.append((json.loads(raw), query))

    channel._handle_raw_message = handle
    ws = SimpleNamespace(_jiuwen_query={"app_id": ["default"]})
    await channel.dispatch_client_request(ws, "chat.send", {"session_id": "chat"})
    frame, query = frames[0]
    assert frame["type"] == "req" and frame["method"] == "chat.send"
    assert frame["params"] == {"session_id": "chat"} and query == {"app_id": ["default"]}
