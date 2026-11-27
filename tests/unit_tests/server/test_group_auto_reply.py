"""Unmentioned user messages are judged once and reach members without rewriting mentions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.agent_teams.schema.conversation import ConversationMessage

from jiuwenswarm.server.runtime import group_auto_reply as auto
from jiuwenswarm.server.runtime import group_service as gs

pytestmark = pytest.mark.asyncio
GROUP = "group-one"


def _message(message_id: str, timestamp: int, *, sender: str = "user", mentions=()):
    return ConversationMessage(
        message_id=message_id, team_name="team", session_id=GROUP, client_message_id=f"client-{message_id}",
        sender=sender, sender_name=sender, content=f"text {message_id}", timestamp=timestamp,
        mentions=list(mentions), attachments=[],
    )


def _calls(*names):
    return SimpleNamespace(tool_calls=[
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments=json.dumps({"member_name": name, "reason": "relevant"}))
        for name in names
    ])


@pytest.fixture
def group(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(gs.lifecycle, "guard", Mock())
    state = {
        "group_id": GROUP, "session_id": GROUP, "team_name": "team", "name": "Group", "description": "",
        "user_id": "owner", "channel_id": "web", "status": "ready", "created_at": 0, "updated_at": 0,
        "mode": "team", "receipts": {},
        "members": [
            {"kind": "agent_template", "id": "writer", "member_name": "writer", "display_name": "Writer"},
            {"kind": "agent_group", "id": "lab", "member_name": "lab", "display_name": "Lab"},
            {"kind": "colleague", "id": "mate", "member_name": "mate", "display_name": "Mate"},
        ],
        "compiled": {"writer": {"member": {"desc": "Writes papers"}}},
    }
    path = gs.group_path(GROUP)
    path.parent.mkdir(parents=True)

    def save():
        path.write_text(json.dumps(state), encoding="utf-8")

    save()
    history = [_message("m0", 1_000, sender="writer"), _message("m1", 2_000)]
    monkeypatch.setattr(gs.GroupService, "public_messages", AsyncMock(side_effect=lambda *a, **k: list(history)))
    model = SimpleNamespace(invoke=AsyncMock(return_value=_calls()))
    monkeypatch.setattr(auto, "build_model", lambda _: model)
    backend = SimpleNamespace(notify_group_member=AsyncMock())
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=backend))
    events = []

    async def publish(group_id, message_id, record):
        events.append((message_id, record["status"]))

    monkeypatch.setattr(auto, "_publish", publish)
    colleague = AsyncMock(return_value=True)
    auto.register_colleague_notifier(colleague)
    try:
        yield SimpleNamespace(state=state, save=save, history=history, model=model, backend=backend,
                              colleague=colleague, events=events)
    finally:
        auto.register_colleague_notifier(None)
        auto._tasks.clear()


async def test_selected_members_are_notified_through_their_own_paths(group):
    group.model.invoke.return_value = _calls("writer", "lab", "mate")

    record = await auto.judge(GROUP, "client-m1")

    assert record["status"] == "notified"
    assert [item["member"] for item in record["notified"]] == ["writer", "lab", "mate"]
    assert all(item["delivered"] for item in record["notified"])
    # Experts and expert-team proxies go through the core; nothing rewrites the message itself.
    assert [call.args for call in group.backend.notify_group_member.await_args_list] == [
        ("writer", "m1"), ("lab", "m1"),
    ]
    assert group.backend.notify_group_member.await_args_list[0].kwargs == {
        "notification_id": "auto-reply:m1:writer",
    }
    group.colleague.assert_awaited_once()
    assert group.colleague.await_args.args[:2] == (GROUP, "mate")
    assert group.colleague.await_args.args[2].mentions == []
    assert group.events == [("m1", "pending"), ("m1", "notified")]
    assert auto.is_notified(GROUP, "m1", "writer") and not auto.is_notified(GROUP, "m1", "quiet")


async def test_model_sees_group_members_and_earlier_messages_only(group):
    await auto.judge(GROUP, "client-m1")

    messages = group.model.invoke.await_args.args[0]
    tool = group.model.invoke.await_args.kwargs["tools"][0]
    assert tool.parameters["properties"]["member_name"]["enum"] == ["writer", "lab", "mate"]
    assert "Writes papers" in messages[0]["content"] and "quiet" not in messages[0]["content"]
    assert "text m0" in messages[1]["content"] and messages[1]["content"].count("text m1") == 1


async def test_invalid_and_repeated_notifications_are_ignored(group):
    group.model.invoke.return_value = SimpleNamespace(tool_calls=[
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments='{"member_name": "quiet"}'),
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments='{"member_name": "stranger"}'),
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments={"member_name": ["writer"]}),
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments="not json"),
        SimpleNamespace(name="other_tool", arguments='{"member_name": "writer"}'),
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments={"member_name": "lab"}),
        SimpleNamespace(name=auto.NOTIFY_TOOL, arguments='{"member_name": "lab"}'),
    ])

    record = await auto.judge(GROUP, "client-m1")

    assert [item["member"] for item in record["notified"]] == ["lab"]
    group.backend.notify_group_member.assert_awaited_once()


@pytest.mark.parametrize("failure, status", [(None, "none"), (RuntimeError("down"), "failed"),
                                             (asyncio.TimeoutError(), "failed")])
async def test_no_notification_without_a_valid_decision(group, failure, status):
    if failure is not None:
        group.model.invoke.side_effect = failure

    record = await auto.judge(GROUP, "client-m1")

    assert record["status"] == status and record["notified"] == []
    group.backend.notify_group_member.assert_not_awaited()
    group.colleague.assert_not_awaited()
    assert auto.public_records(group.state)["m1"] == {
        "status": status, "client_message_id": "client-m1", "notified": [],
    }


async def test_one_decision_per_message_even_when_repeated(group):
    group.model.invoke.return_value = _calls("writer")
    first = await auto.judge(GROUP, "client-m1")
    group.model.invoke.return_value = _calls("lab")

    second, third = await asyncio.gather(auto.judge(GROUP, "client-m1"), auto.judge(GROUP, "client-m1"))

    assert group.model.invoke.await_count == 1
    assert first["notified"] == second["notified"] == third["notified"]
    group.backend.notify_group_member.assert_awaited_once()


@pytest.mark.parametrize("change", ["mentioned", "member message", "disabled", "no members", "unknown"])
async def test_ineligible_messages_never_reach_the_model(group, change):
    client_id = "client-m1"
    if change == "mentioned":
        group.history[1] = _message("m1", 2_000, mentions=["writer"])
    elif change == "member message":
        client_id = "client-m0"
    elif change == "disabled":
        group.state["auto_reply"] = {"enabled": False}
    elif change == "no members":
        group.state["members"] = []
    else:
        client_id = "client-missing"
    group.save()

    assert await auto.judge(GROUP, client_id) is None
    auto.schedule(GROUP, client_id)
    await asyncio.gather(*auto._tasks.get(GROUP, ()))

    group.model.invoke.assert_not_awaited()
    assert auto.records(GROUP) == {}


async def test_undelivered_notifications_wait_for_their_path_and_are_recovered(group, monkeypatch):
    group.model.invoke.return_value = _calls("writer", "mate")
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    auto.register_colleague_notifier(None)

    record = await auto.judge(GROUP, "client-m1")
    assert [item.get("delivered") for item in record["notified"]] == [False, False]

    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=group.backend))
    auto.register_colleague_notifier(group.colleague)
    await auto.recover_group(GROUP)
    await auto.recover_group(GROUP)

    assert all(item["delivered"] for item in auto.records(GROUP)["m1"]["notified"])
    group.backend.notify_group_member.assert_awaited_once()
    group.colleague.assert_awaited_once()
    assert group.model.invoke.await_count == 1


async def test_delivery_rechecks_the_roster_and_drops_rejected_members(group, monkeypatch):
    group.model.invoke.return_value = _calls("writer", "lab", "mate")
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    auto.register_colleague_notifier(None)
    await auto.judge(GROUP, "client-m1")

    group.state["members"] = [item for item in group.state["members"] if item["member_name"] != "writer"]
    group.save()
    group.backend.notify_group_member.side_effect = ValueError("departed")
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=group.backend))
    auto.register_colleague_notifier(group.colleague)
    await auto.recover_group(GROUP)
    await auto.recover_group(GROUP)

    entries = {item["member"]: item for item in auto.records(GROUP)["m1"]["notified"]}
    assert entries["writer"].get("dropped") and entries["lab"].get("dropped") and entries["mate"]["delivered"]
    # The rejected expert-team proxy is not retried forever.
    group.backend.notify_group_member.assert_awaited_once()


async def test_interrupted_judgement_is_retried_only_while_recent(group, monkeypatch):
    group.model.invoke.return_value = _calls("writer")
    for message_id, started in (("m1", auto.time.time()), ("old", auto.time.time() - 3600)):
        await auto._update(GROUP, message_id, lambda _, m=message_id, s=started: {
            "status": "pending", "client_message_id": f"client-{m}", "notified": [], "message_at": 2.0,
            "started_at": s,
        })

    await auto.recover_group(GROUP)

    records = auto.records(GROUP)
    assert records["m1"]["status"] == "notified"
    assert (records["old"]["status"], records["old"]["error"]) == ("failed", "interrupted")
    assert group.model.invoke.await_count == 1


async def test_stop_cancels_judgement_and_drops_unsent_notifications(group, monkeypatch):
    group.model.invoke.return_value = _calls("writer")
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    await auto.judge(GROUP, "client-m1")
    group.history.append(_message("m2", 3_000))
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)

    group.model.invoke.side_effect = slow
    auto.schedule(GROUP, "client-m2")
    await started.wait()
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=group.backend))

    await auto.stop(group.state)
    await asyncio.gather(*auto._tasks.get(GROUP, ()), return_exceptions=True)

    records = auto.records(GROUP)
    assert records["m1"]["notified"][0].get("dropped") and not records["m1"]["notified"][0].get("delivered")
    assert records["m2"]["status"] == "stopped" and "error" not in records["m2"]
    assert auto.public_records(group.state)["m2"]["status"] == "stopped"
    # A stopped judgement is final: neither recovery nor a repeated request judges it again.
    group.model.invoke.side_effect = None
    calls = group.model.invoke.await_count
    await auto.recover_group(GROUP)
    assert (await auto.judge(GROUP, "client-m2"))["status"] == "stopped"
    assert group.model.invoke.await_count == calls
    await auto.recover_group(GROUP)
    group.backend.notify_group_member.assert_not_awaited()


async def test_cleared_history_hides_earlier_decisions(group):
    await auto.judge(GROUP, "client-m1")
    assert "m1" in auto.public_records(group.state)
    assert auto.public_records({**group.state, "history_cleared_at": 2.0}) == {}


async def test_stop_discards_a_judgement_already_running_in_recovery(group):
    started = asyncio.Event()
    release = asyncio.Event()

    async def decide(*args, **kwargs):
        started.set()
        await release.wait()
        return _calls("writer")

    group.model.invoke.side_effect = decide
    await auto._update(GROUP, "m1", lambda _: {
        "status": "pending", "client_message_id": "client-m1", "notified": [],
        "started_at": auto.time.time(),
    })
    recovery = asyncio.create_task(auto.recover_group(GROUP))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await auto.stop(group.state)
    finally:
        release.set()
        await recovery

    assert auto.records(GROUP)["m1"]["status"] == "stopped"
    group.backend.notify_group_member.assert_not_awaited()


def _coordinate(owner, members):
    return SimpleNamespace(name=auto.COORDINATE_TOOL, arguments=json.dumps({
        "owner": owner, "members": members, "reason": "joint report",
    }))


@pytest.fixture
def teams(group, monkeypatch):
    """A second expert team makes a joint decision possible; candidates use the real route store."""
    from jiuwenswarm.agents.swarm import group_route

    group.state["members"].append(
        {"kind": "agent_group", "id": "studio", "member_name": "studio", "display_name": "Studio"},
    )
    group.save()
    monkeypatch.setattr(group_route, "publish", AsyncMock())
    return group_route


async def test_joint_decision_registers_one_candidate_and_wakes_only_its_owner(group, teams):
    group.model.invoke.return_value = SimpleNamespace(tool_calls=[
        *_calls("writer").tool_calls, _coordinate("studio", ["lab", "studio", "lab"]),
        _coordinate("lab", ["lab", "studio"]),
    ])

    record = await auto.judge(GROUP, "client-m1")
    await auto.deliver(GROUP, "m1")
    await auto.recover_group(GROUP)

    # The joint decision replaces separate replies, and the first valid one wins; the Owner comes first.
    assert record["organization"] == {"owner": "studio", "members": ["studio", "lab"]}
    assert [(item["member"], item["delivered"]) for item in record["notified"]] == [("studio", True)]
    group.backend.notify_group_member.assert_awaited_once_with("studio", "m1", notification_id="auto-reply:m1:studio")
    group.colleague.assert_not_awaited()
    candidate = teams.routes(GROUP)["client-m1"]
    assert (candidate["owner_member"], candidate["members"], candidate["body"], candidate["status"]) == (
        "studio", ["studio", "lab"], "text m1", "pending",
    )
    assert auto.organization_members(GROUP, "m1") == ["studio", "lab"]
    assert auto.public_records(group.state)["m1"] == {
        "status": "notified", "client_message_id": "client-m1", "notified": ["studio"],
        "organization": {"owner": "studio", "members": ["studio", "lab"]},
    }
    tools = group.model.invoke.await_args.kwargs["tools"]
    assert [tool.name for tool in tools] == [auto.NOTIFY_TOOL, auto.COORDINATE_TOOL]
    assert tools[1].parameters["properties"]["owner"]["enum"] == ["lab", "studio"]
    assert auto.COORDINATE_TOOL in group.model.invoke.await_args.args[0][0]["content"]


@pytest.mark.parametrize("owner, members", [
    ("lab", ["lab"]), ("lab", ["lab", "writer"]), ("lab", ["lab", "mate"]), ("lab", ["studio", "stranger"]),
    ("writer", ["writer", "lab"]), ("lab", "studio"), ("lab", ["lab", "lab"]),
    ("lab", ["lab", "studio", {"team": "lab"}]), ("lab", [["lab"], "studio"]),
    (["lab"], ["lab", "studio"]),
])
async def test_invalid_joint_decisions_leave_separate_notifications_alone(group, teams, owner, members):
    group.model.invoke.return_value = SimpleNamespace(tool_calls=[
        _coordinate(owner, members), *_calls("lab", "studio").tool_calls,
    ])

    record = await auto.judge(GROUP, "client-m1")

    assert "organization" not in record and auto.organization_members(GROUP, "m1") == []
    assert [item["member"] for item in record["notified"]] == ["lab", "studio"]
    assert teams.routes(GROUP) == {}
    assert group.backend.notify_group_member.await_count == 2


async def test_joint_decision_is_not_offered_to_a_group_with_one_expert_team(group):
    group.model.invoke.return_value = SimpleNamespace(tool_calls=[_coordinate("lab", ["lab", "writer"])])

    record = await auto.judge(GROUP, "client-m1")

    assert record["status"] == "none"
    assert [tool.name for tool in group.model.invoke.await_args.kwargs["tools"]] == [auto.NOTIFY_TOOL]
    assert auto.COORDINATE_TOOL not in group.model.invoke.await_args.args[0][0]["content"]


@pytest.mark.parametrize("change", ["team left", "candidate conflict"])
async def test_joint_decision_that_can_no_longer_start_wakes_nobody(group, teams, monkeypatch, change):
    group.model.invoke.return_value = SimpleNamespace(tool_calls=[_coordinate("studio", ["studio", "lab"])])
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    await auto.judge(GROUP, "client-m1")
    # Nothing is registered before the Owner can actually be woken.
    assert teams.routes(GROUP) == {}

    if change == "team left":
        group.state["members"] = [item for item in group.state["members"] if item["member_name"] != "lab"]
        group.save()
    else:
        await teams.record_candidate(GROUP, mentions=["studio", "lab"], body="another request", plan_id="client-m1")
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=group.backend))
    await auto.recover_group(GROUP)
    await auto.recover_group(GROUP)

    entry = auto.records(GROUP)["m1"]["notified"][0]
    assert entry.get("dropped") and not entry.get("delivered")
    # Never degraded into separate replies of the remaining teams.
    group.backend.notify_group_member.assert_not_awaited()
    if change == "team left":
        assert teams.routes(GROUP) == {}


async def test_prompt_marks_departed_member_history_and_excludes_from_candidates(group):
    from openjiuwen.agent_teams.schema.conversation import ConversationMessage

    history = [
        ConversationMessage(
            message_id="m0", team_name="team-one", session_id=GROUP,
            client_message_id="client-m0", sender="former-mate", sender_name="小九",
            content="你要选方案 A 还是方案 B？", timestamp=900, mentions=[], attachments=[],
        ),
        ConversationMessage(
            message_id="m1", team_name="team-one", session_id=GROUP,
            client_message_id="client-m1", sender="user", sender_name="user",
            content="选 A", timestamp=1000, mentions=[], attachments=[],
        ),
    ]
    candidates = auto._candidates(group.state)
    prompt = auto._prompt(candidates, history[:-1], history[-1])
    assert "departed=true" in prompt[0]["content"]
    assert '"departed": true' in prompt[1]["content"]
    assert '"from": "小九"' in prompt[1]["content"]
    assert all(item["member_name"] != "former-mate" for item in candidates)
