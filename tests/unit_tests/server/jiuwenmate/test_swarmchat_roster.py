"""Colleague awareness of SwarmChat group rosters: per-turn guidance and the roster tool."""

from types import SimpleNamespace

import pytest


def _group(group_id: str, name: str, owner: str = "owner", *, extra: int = 0) -> dict:
    members = [
        {"kind": "agent_group", "id": "growth", "member_name": "member-team", "display_name": "Growth team",
         "description": "Plans content growth"},
        {"kind": "agent_template", "id": "writer", "member_name": "member-writer", "display_name": "Writer"},
        {"kind": "colleague", "id": "mate", "member_name": "member-mate", "display_name": "Xiaojiu",
         "description": "Office assistant"},
        {"kind": "colleague", "id": "coach", "member_name": "member-coach", "display_name": "Coach"},
    ]
    members += [{"kind": "agent_template", "id": f"expert-{index}", "member_name": f"member-{index}",
                 "display_name": f"Expert {index}"} for index in range(extra)]
    return {
        "group_id": group_id, "session_id": group_id, "name": name, "user_id": owner, "channel_id": "web",
        "status": "ready", "created_at": 0, "team_name": "team", "members": members,
        "compiled": {"member-writer": {"member": {"desc": "Writes long-form articles"}}},
    }


def test_guidance_lists_every_member_with_kind_mention_and_self(monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import files

    monkeypatch.setattr(files, "published_files", lambda group: [])
    guidance = files.execution_guidance(_group("g1", "D8"), "mate")
    assert "D8 (g1), 4 AI members plus the human owner" in guidance
    assert "- Growth team (expert team): [[agent:member-team]] - Plans content growth" in guidance
    assert "- Writer (expert): [[agent:member-writer]] - Writes long-form articles" in guidance
    assert "- Xiaojiu (AI colleague, you): [[agent:member-mate]]" in guidance
    assert "- Coach (AI colleague): [[agent:member-coach]]" in guidance
    assert "never infer who is in a group from who has spoken" in guidance


def test_guidance_caps_large_groups_and_points_to_the_tool(monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import files, membership

    monkeypatch.setattr(files, "published_files", lambda group: [])
    guidance = files.execution_guidance(_group("g1", "Big", extra=40), "mate")
    listed = [line for line in guidance.splitlines() if line.startswith("- ") and "[[agent:" in line]
    assert len(listed) == membership.ROSTER_GUIDANCE_LIMIT
    assert "14 more; call list_group_members" in guidance


def _tool(monkeypatch, groups: dict, *, session_kind: str, session_key: str = "g1"):
    from jiuwenswarm.server.jiuwenmate.swarmchat import membership, roster_tool

    def require(group_id, agent_id):
        group = groups.get(group_id)
        if group is not None:
            for item in membership.colleague_members(group):
                if item["id"] == agent_id:
                    return group, item
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
        raise PersistentAgentError("SwarmChat membership is unavailable", code="FORBIDDEN")

    def groups_for(agent_id, *, owner=None):
        return [group for group in groups.values()
                if (owner is None or group["user_id"] == owner)
                and any(item["id"] == agent_id for item in membership.colleague_members(group))]

    monkeypatch.setattr(roster_tool, "require_membership", require)
    monkeypatch.setattr(roster_tool, "groups_for", groups_for)
    port = SimpleNamespace(validate_execution=lambda execution: "mate")
    session = SimpleNamespace(conversation=SimpleNamespace(kind=session_kind, key=session_key))
    sessions = SimpleNamespace(get_session=lambda namespace_id, session_id: session)
    execution = SimpleNamespace(namespace_id="local", session_id="session")
    return roster_tool.GroupRosterTool(port, sessions, execution)


async def test_tool_lists_current_and_other_groups_including_silent_members(monkeypatch):
    groups = {"g1": _group("g1", "Here"), "g2": _group("g2", "D8")}
    tool = _tool(monkeypatch, groups, session_kind="swarm_group")
    current = await tool.invoke({})
    assert current["status"] == "found" and current["group_id"] == "g1"
    assert current["complete"] is True and current["member_count"] == 4
    other = await tool.invoke({"group_name": "D8"})
    assert other["group_id"] == "g2"
    assert {item["display_name"] for item in other["members"]} == {"Growth team", "Writer", "Xiaojiu", "Coach"}
    writer = next(item for item in other["members"] if item["display_name"] == "Writer")
    assert writer == {"display_name": "Writer", "kind": "expert", "description": "Writes long-form articles",
                      "mention": "[[agent:member-writer]]", "is_self": False}
    assert next(item for item in other["members"] if item["is_self"])["display_name"] == "Xiaojiu"


@pytest.mark.parametrize("arguments", [
    {"group_id": "not-a-member"},     # the colleague is not in this group
    {"group_id": "other-owner"},      # same colleague, but another owner's group
    {"group_name": "Missing"},
])
async def test_tool_refuses_groups_outside_the_authorized_scope(monkeypatch, arguments):
    outsider = _group("not-a-member", "Outside")
    outsider["members"] = [item for item in outsider["members"] if item.get("id") != "mate"]
    groups = {"g1": _group("g1", "Here"), "not-a-member": outsider,
              "other-owner": _group("other-owner", "Other", owner="someone-else")}
    tool = _tool(monkeypatch, groups, session_kind="swarm_group")
    result = await tool.invoke(arguments)
    assert result["status"] == "unavailable"
    assert "members" not in result


async def test_tool_requires_a_target_outside_swarmchat_and_rejects_ambiguous_names(monkeypatch):
    groups = {"g1": _group("g1", "Same"), "g2": _group("g2", "Same")}
    tool = _tool(monkeypatch, groups, session_kind="web_direct")
    assert (await tool.invoke({}))["status"] == "invalid"
    assert (await tool.invoke({"group_name": "Same"}))["status"] == "unavailable"
    assert (await tool.invoke({"group_id": "g2"}))["group_id"] == "g2"
    assert (await tool.invoke({"group_id": ""}))["status"] == "invalid"


async def test_tool_is_injected_only_for_a_local_colleague_in_some_group(monkeypatch):
    from jiuwenswarm.server.jiuwenmate.swarmchat import port, roster_tool

    class Spec:
        """Only the parts of the published spec that runtime tool injection touches."""

        def __init__(self, tools=()):
            self.tools = list(tools)

        def model_copy(self, *, update):
            return Spec(update["tools"])

        def fingerprint(self):
            return f"fingerprint-{len(self.tools)}"

    monkeypatch.setattr(port.SwarmChatGroupChatPort, "validate_execution", lambda self, execution: "mate")
    sessions = SimpleNamespace()
    service = SimpleNamespace()
    spec = Spec()

    def context():
        return SimpleNamespace(extras={}, execution=SimpleNamespace(namespace_id="local", agent_id="mate"))

    monkeypatch.setattr(roster_tool, "groups_for", lambda agent_id: [])
    unchanged = await roster_tool.prepare_group_roster_invocation(
        spec, context(), SimpleNamespace(namespace_id="local"), sessions, service)
    assert unchanged is spec

    monkeypatch.setattr(roster_tool, "groups_for", lambda agent_id: [_group("g1", "Here")])
    remote = await roster_tool.prepare_group_roster_invocation(
        spec, context(), SimpleNamespace(namespace_id="tenant"), sessions, service)
    assert remote is spec

    ctx = context()
    monkeypatch.setattr(roster_tool, "replace", lambda execution, **changes: SimpleNamespace(
        **{**vars(execution), **changes}))
    injected = await roster_tool.prepare_group_roster_invocation(
        spec, ctx, SimpleNamespace(namespace_id="local"), sessions, service)
    assert [tool.type for tool in injected.tools] == [roster_tool.PROVIDER]
    assert ctx.execution.config_fingerprint == "fingerprint-1"
    with pytest.raises(Exception):
        await roster_tool.prepare_group_roster_invocation(
            injected, context(), SimpleNamespace(namespace_id="local"), sessions, service)
    built = roster_tool.build_group_roster_tool({}, ctx)
    assert built.card.name == "list_group_members"


@pytest.mark.parametrize("access", ["active", "denied", "revoked", "incomplete", "ambiguous"])
async def test_feishu_tool_only_releases_complete_roster_for_an_authorized_group(monkeypatch, access):
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.server.jiuwenmate import feishu_group_fanout
    from jiuwenswarm.server.jiuwenmate.swarmchat.roster_tool import GroupRosterTool

    source = {"group_id": "grp_project", "platform_chat_id": "oc_project", "app_id": "app",
              "name": "Project", "platform": "feishu"}
    before = () if access == "denied" else (source,)
    if access == "ambiguous":
        before = (source, {**source, "group_id": "grp_other"})
    after = () if access == "revoked" else (source,)
    im = SimpleNamespace(sharing_sources=AsyncMock(side_effect=[before, after]))
    lookup = Mock(return_value=None if access == "incomplete" else {"ou_member": "Silent member"})
    monkeypatch.setattr(feishu_group_fanout, "feishu_group_roster", lookup)
    port = SimpleNamespace(validate_execution=Mock(return_value="mate"))
    tool = GroupRosterTool(port, None, object(), im)
    result = await tool.invoke({"platform": "feishu"})
    if access == "active":
        assert result["complete"] is True and result["members"] == [{"display_name": "Silent member"}]
        assert im.sharing_sources.await_args.args[1].group_ids == ("grp_project",)
    else:
        assert result["status"] == "unavailable" and "members" not in result
    if access in {"denied", "ambiguous"}:
        lookup.assert_not_called()
    else:
        lookup.assert_called_once_with("oc_project", "app")


@pytest.mark.parametrize("second_page_ok", [True, False])
def test_feishu_roster_requires_every_sdk_page(monkeypatch, second_page_ok):
    from unittest.mock import Mock

    from jiuwenswarm.common import config
    from jiuwenswarm.server.jiuwenmate import feishu_group_fanout as feishu

    def response(ok, member_id, name, *, token=""):
        data = SimpleNamespace(items=[SimpleNamespace(member_id=member_id, name=name)],
                               has_more=bool(token), page_token=token)
        return SimpleNamespace(success=lambda: ok, data=data)

    get = Mock(side_effect=[response(True, "ou_1", "First", token="next"),
                            response(second_page_ok, "ou_2", "Second")])
    api = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat_members=SimpleNamespace(get=get))))
    monkeypatch.setitem(feishu._MEMBER_CLIENTS, "roster-test-app", api)
    monkeypatch.setattr(config, "get_config", lambda: {"channels": {"feishu": {"apps": [
        {"app_id": "roster-test-app", "app_secret": "test-only"},
    ]}}})
    result = feishu.feishu_group_roster("oc_project", "roster-test-app")
    assert result == ({"ou_1": "First", "ou_2": "Second"} if second_page_ok else None)
    assert get.call_count == 2
    assert get.call_args.args[0].page_token == "next"


@pytest.mark.parametrize(("command", "kind", "redirected"), [
    ("lark-cli im +chat-members-list --chat-id oc_project --as bot", "swarm_group", True),
    ('lark-cli auth login --scope "im:chat.members:read" --no-wait --json', "swarm_group", True),
    ("lark-cli im +chat-members-list --chat-id oc_project --as bot", "web_direct", False),
    ("lark-cli im +chat-list --as bot", "swarm_group", False),
])
async def test_swarmchat_preflight_routes_roster_cli_attempts_to_the_read_only_tool(
    monkeypatch, command, kind, redirected,
):
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.server.jiuwenmate.swarmchat import port

    sessions = SimpleNamespace(get_session=lambda *_: SimpleNamespace(
        conversation=SimpleNamespace(kind=kind, key="group")))
    colleague = SimpleNamespace(enabled=True) if kind == "swarm_group" else None
    service = SimpleNamespace(get_colleague=Mock(return_value=colleague), connection_of=lambda *_: None)
    swarm = port.SwarmChatGroupChatPort(sessions, service, None).bind("local", "mate")
    im = SimpleNamespace(prepare_tool_call=AsyncMock(return_value=None))
    monkeypatch.setattr(port, "require_membership", lambda *_: ({"user_id": "owner"}, {}))
    composite = port.CompositeGroupChatPort(im, swarm, sessions, None)
    result = await composite.prepare_tool_call(
        SimpleNamespace(namespace_id="local", agent_id="mate", session_id="session", principal_ref="owner"),
        "bash", {"command": command},
    )
    if redirected:
        assert result.data["status"] == "use_roster_tool"
        im.prepare_tool_call.assert_not_awaited()
    else:
        assert result is None
        im.prepare_tool_call.assert_awaited_once()
    if kind != "swarm_group":
        service.get_colleague.assert_not_called()


async def test_employee_roster_redirect_runs_before_shell_approval(monkeypatch):
    from unittest.mock import AsyncMock

    from jiuwenswarm.server.jiuwenmate.employee_permission_rail import EmployeeAutoPermissionRail
    from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail

    policy = AsyncMock()
    monkeypatch.setattr(AutoPermissionInterruptRail, "before_tool_call", policy)
    monkeypatch.setattr(AutoPermissionInterruptRail, "__init__", lambda self, **kwargs: None)
    call = SimpleNamespace(id="call", arguments='{"command":"lark-cli im +chat-members-list --chat-id oc_group"}')
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="bash", tool_call=call), extra={})
    await EmployeeAutoPermissionRail(swarmchat=True).before_tool_call(ctx)
    assert ctx.extra["_skip_tool_calls"] == {"call": True}
    assert ctx.inputs.tool_result.data["status"] == "use_roster_tool"
    policy.assert_not_awaited()
    await EmployeeAutoPermissionRail().before_tool_call(ctx)
    policy.assert_awaited_once_with(ctx)
