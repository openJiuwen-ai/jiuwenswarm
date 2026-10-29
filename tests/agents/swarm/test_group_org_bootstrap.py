"""Swarm group expert-team Organization coordination."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.swarm import group_chat
from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from jiuwenswarm.server.runtime import group_execution
from jiuwenswarm.server.runtime import group_service
from openjiuwen.agent_teams.organization.schema import OrgTaskStatus

SESSION = "web_group"
ORG = bootstrap.organization_id_for_session(SESSION)


@pytest.fixture(autouse=True)
def _clear_bound_turns():
    yield
    bootstrap._member_turns.clear()
    bootstrap._deferred_direct.clear()
    bootstrap._proxy_tools.clear()
    bootstrap._accepted_plans.clear()


MEMBERS = [
    {"kind": "agent_template", "id": "writer", "member_name": "writer"},
    {
        "kind": "agent_group",
        "id": "business-planning",
        "member_name": "business",
        "display_name": "商业策划专家团",
    },
    {
        "kind": "agent_group",
        "id": "technology-insight",
        "member_name": "insight",
        "display_name": "技术洞察专家团",
    },
]


class _MemoryGroup:
    def __init__(self, monkeypatch, **extra):
        self.state = {
            "team_name": "outer-team",
            "members": MEMBERS,
            **extra,
        }
        monkeypatch.setattr(group_service, "read_group", lambda _group_id: self.state)
        monkeypatch.setattr(group_service, "_save", lambda _state: None)


class _Pool:
    def __init__(self):
        self.entries = {}

    async def get(self, team_id):
        return self.entries.get(team_id)

    def add(self, team_id, *, organization_id=""):
        task_pool = (
            SimpleNamespace(organization_id=organization_id)
            if organization_id
            else None
        )
        backend = SimpleNamespace(org_task_manager=task_pool)
        self.entries[team_id] = SimpleNamespace(
            current_session_id=SESSION,
            agent=SimpleNamespace(team_backend=backend),
        )
        return backend


class _Launcher:
    def __init__(self, pool):
        self.pool = pool
        self.calls = []

    async def launch(self, **kwargs):
        self.calls.append(kwargs)
        self.pool.add(kwargs["team_id"])
        return SimpleNamespace(team_id=kwargs["team_id"])


class _OrganizationRecord:
    def model_dump(self, **_kwargs):
        return {"organization_id": ORG}


class _OrgRuntime:
    def __init__(self, pool):
        self.pool = pool
        self.created = []
        self.invites = []

    async def create_organization(self, **kwargs):
        self.created.append(kwargs)
        backend = self.pool.entries[kwargs["owner_team_id"]].agent.team_backend
        backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs["organization_id"]
        )
        return _OrganizationRecord()

    async def invite_team(self, **kwargs):
        self.invites.append(kwargs)
        backend = self.pool.entries[kwargs["target_team_id"]].agent.team_backend
        backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs["organization_id"]
        )
        return _OrganizationRecord()


class _TaskPool:
    def __init__(self, *, tasks=(), incomplete=(), summary_team_id=None):
        self.tasks = list(tasks)
        self.incomplete = list(incomplete)
        self.summary_team_id = summary_team_id

    async def list_tasks(self, *, status=None, assigned_team_id=None, limit=50):
        return [
            item
            for item in self.tasks
            if (status is None or item.status == status)
            and (assigned_team_id is None or item.assigned_team_id == assigned_team_id)
        ]

    async def list_incomplete_summary_executions(self):
        return self.incomplete

    async def get_summary_team(self):
        if self.summary_team_id is None:
            return None
        return SimpleNamespace(summary_team_id=self.summary_team_id)


def _task(
    task_id,
    *,
    root_id=None,
    status=OrgTaskStatus.COMPLETED,
    team_id=None,
    created_at=2000,
):
    return SimpleNamespace(
        task_id=task_id,
        root_task_id=root_id or task_id,
        parent_task_id=None if root_id is None else root_id,
        status=status,
        assigned_team_id=team_id,
        created_at=created_at,
        title=f"title {task_id}",
        output_abstract=f"output {task_id}",
        output_context=None,
    )


def test_owner_prompt_lets_the_model_choose_an_unnamed_aggregation_mode():
    prompt = bootstrap._OWNER_PROMPT_SUFFIX
    reminder = bootstrap._OWNER_START_REMINDER
    assert "The aggregation mode is not selected for you" in prompt
    assert "If the user named neither, choose one from the task" in prompt
    assert "choose one from the task" in reminder
    assert "org_create_and_invite_expert_team" in prompt
    assert "Write only the requested deliverable" in prompt
    assert "org_create_and_invite_expert_team" in reminder
    assert "requested deliverable" in reminder


@pytest.mark.asyncio
async def test_owner_prime_does_not_select_or_start_the_root(monkeypatch):
    calls = []

    async def _invoke(tool, inputs):
        calls.append((type(tool).__name__, dict(inputs)))
        return SimpleNamespace(success=True)

    monkeypatch.setattr(bootstrap, "_invoke_owner_tool", _invoke)

    class _Pool:
        async def register_leader(self, **_kwargs):
            return None

        async def get_task(self, _task_id):
            return SimpleNamespace(status=OrgTaskStatus.OPEN, aggregation=None)

    await bootstrap._owner_primes_root_task(
        task_pool=_Pool(),
        team_id="org-expert-owner",
        session_id=SESSION,
        plan={
            "plan_id": "plan-1",
            "body": "最终完成500字左右的报告返回给我",
            "members": ["business", "insight"],
        },
    )

    actions = [inputs.get("action") for _name, inputs in calls]
    assert calls[0][0] == "OrgClaimTaskTool"
    assert "set_aggregation_mode" not in actions
    assert "start" not in actions


def test_mentions_keep_order_and_instance_identity_is_stable():
    selected = bootstrap.mentioned_expert_teams(
        ["insight", "writer", "insight", "business"], MEMBERS
    )
    assert [item["member_name"] for item in selected] == [
        "insight",
        "business",
    ]
    assert bootstrap.instance_team_id(
        SESSION, "business"
    ) == bootstrap.instance_team_id(SESSION, "business")
    assert bootstrap.instance_team_id(
        SESSION, "business"
    ) != bootstrap.instance_team_id(SESSION, "insight")
    legacy = bootstrap.mentioned_expert_teams(
        ["insight", "business"],
        [
            {
                "kind": "expert_team",
                "definition_id": "technology-insight",
                "member_name": "insight",
            },
            {
                "kind": "expert_team",
                "definition_id": "business-planning",
                "member_name": "business",
            },
        ],
    )
    assert [(item["kind"], item["id"]) for item in legacy] == [
        ("agent_group", "technology-insight"),
        ("agent_group", "business-planning"),
    ]


def test_plan_keeps_mentions_and_first_expert_is_owner(monkeypatch):
    group = _MemoryGroup(monkeypatch)
    mentions = ["business", "insight"]
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=mentions,
        body="写可行性报告",
        plan_id="message-1",
    )
    assert mentions == ["business", "insight"]
    assert plan["members"] == ["business", "insight"]
    assert plan["owner_member"] == "business"
    assert plan["organization_id"] == bootstrap.new_organization_id(SESSION, "message-1")
    assert group.state["organization_plans"] == [plan]

    # Message retries do not duplicate a run.
    bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "insight"],
        body="写可行性报告",
        plan_id="message-1",
    )
    assert len(group.state["organization_plans"]) == 1


def test_expert_teams_with_a_single_expert_stay_off_the_organization(monkeypatch):
    group = _MemoryGroup(monkeypatch)
    mentions = ["business", "writer", "insight"]
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=mentions,
        body="专家团和单个专家一起看",
        plan_id="message-mixed",
    )
    assert plan is None
    assert mentions == ["business", "writer", "insight"]
    assert group.state.get("organization_plans", []) == []
    assert group.state["direct_routes"] == [
        {"plan_id": "message-mixed", "members": ["business", "insight"]}
    ]
    assert bootstrap.direct_route_for_member(SESSION, "business", "message-mixed")
    assert not bootstrap.direct_route_for_member(SESSION, "writer", "message-mixed")
    bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "writer", "insight"],
        body="专家团和单个专家一起看",
        plan_id="message-mixed",
    )
    assert len(group.state["direct_routes"]) == 1
    assert (
        bootstrap.record_organization_plan(
            SESSION,
            mentions=["business", "writer"],
            body="一个团加一个专家",
            plan_id="message-one",
        )
        is None
    )
    assert group.state["direct_routes"][-1] == {
        "plan_id": "message-one",
        "members": ["business"],
    }
    legacy = _MemoryGroup(
        monkeypatch,
        members=[
            {"kind": "expert", "id": "writer", "member_name": "writer"},
            {
                "kind": "agent_group",
                "id": "business-planning",
                "member_name": "business",
            },
            {
                "kind": "agent_group",
                "id": "technology-insight",
                "member_name": "insight",
            },
        ],
    )
    assert (
        bootstrap.record_organization_plan(
            SESSION,
            mentions=["insight", "writer", "business"],
            body="旧花名册里的单个专家",
            plan_id="message-legacy-mixed",
        )
        is None
    )
    assert legacy.state.get("organization_plans", []) == []
    assert legacy.state["direct_routes"] == [
        {"plan_id": "message-legacy-mixed", "members": ["insight", "business"]}
    ]


@pytest.mark.asyncio
async def test_an_older_organization_finishes_before_the_direct_reply(monkeypatch):
    calls = []
    group = _MemoryGroup(monkeypatch)
    bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "insight"],
        body="一起做上线前检查",
        plan_id="message-org",
    )
    bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "writer", "insight"],
        body="这条上线说明写得清不清楚",
        plan_id="message-mixed",
    )
    assert [item["plan_id"] for item in group.state["organization_plans"]] == ["message-org"]
    assert bootstrap._deferred_direct[(SESSION, "message-mixed", "business")]["body"] == (
        "这条上线说明写得清不清楚"
    )

    async def _organization(**kwargs):
        calls.append(("organization", kwargs["plan"]["plan_id"]))
        return "organization-result"

    async def _saved(*_args, **_kwargs):
        return {"kind": "organization"}

    async def _follow(**_kwargs):
        calls.append(("followup",))
        return "followup-result"

    async def _private(_key, task, *, execution=None):
        calls.append(("private", task))
        return "private-result"

    monkeypatch.setattr(bootstrap, "run_organization_member", _organization)
    monkeypatch.setattr(bootstrap, "saved_organization_instance", _saved)
    monkeypatch.setattr(bootstrap, "run_instance_followup", _follow)
    tool = group_chat.RunTeam(
        SimpleNamespace(
            conversation_mode="group_chat",
            team_name="outer-team",
            member_name="business",
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    monkeypatch.setattr(tool, "_run", _private)

    first = await tool.invoke({"run_id": "r-org", "task": "上线前检查"})
    assert first == "organization-result"
    assert calls == [("organization", "message-org")]

    second = await tool.invoke({"run_id": "r-mixed", "task": "说明写得清不清楚"})
    assert second.data["status"] == "queued"
    assert calls == [("organization", "message-org")]

    scheduled = []
    monkeypatch.setattr(
        bootstrap,
        "_schedule_direct_member",
        lambda session_id, job: scheduled.append(
            (session_id, job["message_id"], job["member"], job["body"])
        ),
    )
    group.state["organization_plans"][0]["status"] = "done"
    bootstrap.release_deferred_direct(SESSION, "message-org")
    assert scheduled == [
        (SESSION, "message-mixed", "business", "这条上线说明写得清不清楚"),
        (SESSION, "message-mixed", "insight", "这条上线说明写得清不清楚"),
    ]


@pytest.mark.asyncio
async def test_deferred_direct_reply_reuses_the_proxy_tool():
    calls = []

    class _Tool:
        def __init__(self):
            self.backend = SimpleNamespace(member_name="business")

        async def invoke(self, inputs, **_kwargs):
            calls.append(inputs)
            return "ran"

    bootstrap.remember_proxy_tool(SESSION, _Tool())
    await bootstrap._launch_direct_member(
        SESSION, "business", "message-mixed", "这条上线说明写得清不清楚"
    )
    assert calls == [
        {
            "action": "start",
            "run_id": "direct-message-mixed",
            "content": "这条上线说明写得清不清楚",
        }
    ]


def test_direct_reply_waits_until_the_older_organization_posts(monkeypatch):
    now = bootstrap._now_ms()
    _MemoryGroup(
        monkeypatch,
        organization_plans=[
            {
                "plan_id": "round-2",
                "status": "running",
                "members": ["business", "insight"],
                "owner_member": "business",
                "created_at": now,
            }
        ],
        direct_routes=[
            {"plan_id": "message-mixed", "members": ["business", "insight"]}
        ],
        organization_teams=[
            {
                "team_id": "org-expert-insight",
                "role": "member",
                "member_name": "insight",
            }
        ],
    )
    assert bootstrap.group_reply_block_reason(SESSION, "insight")
    assert bootstrap.group_reply_block_reason(
        SESSION, "insight", message_id="message-mixed"
    )
    assert bootstrap.group_reply_block_reason(
        SESSION, "insight", message_id="round-2"
    )
    assert bootstrap.group_reply_block_reason(
        SESSION,
        "team-leader",
        team_id="org-expert-insight",
        message_id="message-mixed",
    )
    plans = group_service.read_group(SESSION)["organization_plans"]
    plans[0]["status"] = "done"
    assert (
        bootstrap.group_reply_block_reason(
            SESSION, "insight", message_id="message-mixed"
        )
        is None
    )


def test_public_agent_group_roster_records_an_organization_plan(monkeypatch):
    _MemoryGroup(
        monkeypatch,
        members=[
            {
                "kind": "agent_group",
                "id": "business-planning",
                "member_name": "business",
                "display_name": "商业策划专家团",
            },
            {
                "kind": "agent_group",
                "id": "software-architecture-design",
                "member_name": "architecture",
                "display_name": "代码架构设计专家团",
            },
        ],
    )
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "architecture"],
        body="写可行性报告",
        plan_id="message-public",
    )
    assert plan["members"] == ["business", "architecture"]
    assert plan["owner_member"] == "business"


def test_follow_up_forms_an_organization_owned_by_its_first_team(monkeypatch):
    _MemoryGroup(
        monkeypatch,
        members=[
            {
                "kind": "agent_group",
                "id": "business-planning",
                "member_name": "business",
            },
            {
                "kind": "agent_group",
                "id": "technology-insight",
                "member_name": "insight",
            },
            {
                "kind": "agent_group",
                "id": "contract-review",
                "member_name": "review",
            },
        ],
        organization_teams=[
            {
                "member_name": "business",
                "team_id": "owner-team",
                "role": "owner",
            }
        ],
        organization_plans=[
            {
                "plan_id": "round-1",
                "members": ["business", "insight"],
                "owner_member": "business",
                "status": "running",
                "created_at": 1,
            }
        ],
    )
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=["insight", "review"],
        body="下一轮",
        plan_id="round-2",
    )
    assert plan["members"] == ["insight", "review"]
    assert plan["owner_member"] == "insight"
    assert plan["status"] == "pending"
    assert bootstrap._blocking_earlier_plans(SESSION, plan)


def test_identical_expert_set_rejoins_and_any_change_creates_an_organization(monkeypatch):
    _MemoryGroup(
        monkeypatch,
        members=[
            {
                "kind": "agent_group",
                "id": "business-planning",
                "member_name": "business",
            },
            {
                "kind": "agent_group",
                "id": "technology-insight",
                "member_name": "insight",
            },
            {
                "kind": "agent_group",
                "id": "contract-review",
                "member_name": "review",
            },
        ],
    )
    first = bootstrap.record_organization_plan(
        SESSION,
        mentions=["insight", "business"],
        body="第一轮",
        plan_id="round-1",
    )
    same = bootstrap.record_organization_plan(
        SESSION,
        mentions=["business", "insight"],
        body="同一批再来",
        plan_id="round-2",
    )
    changed = bootstrap.record_organization_plan(
        SESSION,
        mentions=["insight", "review"],
        body="换一个",
        plan_id="round-3",
    )
    assert set(same["members"]) == set(first["members"])
    assert same["organization_id"] == first["organization_id"]
    assert changed["organization_id"] == bootstrap.new_organization_id(SESSION, "round-3")
    assert changed["organization_id"] != first["organization_id"]
    assert bootstrap.instance_team_id(SESSION, "insight") == bootstrap.instance_team_id(
        SESSION, "insight"
    )


def test_legacy_plan_rejoins_the_session_organization(monkeypatch):
    _MemoryGroup(
        monkeypatch,
        organization_plans=[
            {
                "plan_id": "old-round",
                "members": ["business", "insight"],
                "owner_member": "business",
                "status": "done",
            }
        ],
    )
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=["insight", "business"],
        body="回到旧组织",
        plan_id="round-again",
    )
    assert plan["organization_id"] == ORG


@pytest.mark.asyncio
async def test_each_proxy_launches_its_fixed_org_enabled_instance():
    pool = _Pool()
    launcher = _Launcher(pool)
    runtime = SimpleNamespace(pool=pool)

    owner_id = await bootstrap.ensure_member_instance(
        runtime=runtime,
        launcher=launcher,
        session_id=SESSION,
        outer_team_id="outer-team",
        member=MEMBERS[1],
        is_owner=True,
    )
    member_id = await bootstrap.ensure_member_instance(
        runtime=runtime,
        launcher=launcher,
        session_id=SESSION,
        outer_team_id="outer-team",
        member=MEMBERS[2],
        is_owner=False,
    )

    owner_call, member_call = launcher.calls
    assert owner_id == bootstrap.instance_team_id(SESSION, "business")
    assert member_id == bootstrap.instance_team_id(SESSION, "insight")
    assert owner_call["session_id"] == SESSION
    assert owner_call["share_db_from_team_id"] == "outer-team"
    assert owner_call["enable_organization"] is True
    assert owner_call["leader_tools"][0].type == group_chat.COMPLETE_RUN
    assert owner_call["extra_capabilities"] == [
        "group-member:business",
        "group-coordination",
    ]
    assert "org_claim_task" in owner_call["leader_prompt_suffix"]
    assert "do not use swarmflow or ordinary create_task" in owner_call[
        "leader_prompt_suffix"
    ]
    assert member_call["leader_tools"] is None
    assert member_call["extra_capabilities"] == ["group-member:insight"]

    # A second call reuses the live proxy-bound instance.
    await bootstrap.ensure_member_instance(
        runtime=runtime,
        launcher=launcher,
        session_id=SESSION,
        outer_team_id="outer-team",
        member=MEMBERS[1],
        is_owner=True,
    )
    assert len(launcher.calls) == 2


@pytest.mark.asyncio
async def test_owner_start_watchdog_queues_idempotent_org_reminder(monkeypatch):
    class _TaskPool:
        async def list_tasks(self, **_kwargs):
            return []

        async def list_incomplete_summary_executions(self):
            return []

    scheduled = []
    org_runtime = SimpleNamespace(
        _schedule_leader_turn=lambda **kwargs: scheduled.append(kwargs)
    )
    monkeypatch.setattr(bootstrap, "OWNER_START_WATCHDOG_SECONDS", 0)

    await bootstrap._queue_owner_start_reminder(
        org_runtime=org_runtime,
        task_pool=_TaskPool(),
        team_id="owner-team",
        session_id=SESSION,
        plan_id="plan-1",
    )

    assert scheduled[0]["team_id"] == "owner-team"
    assert "org_view_tasks first" in scheduled[0]["prompt"]
    assert "do not duplicate the root" in scheduled[0]["prompt"]

    root_id = bootstrap._root_task_id("plan-1")

    class _ChildPool:
        async def list_tasks(self, **_kwargs):
            return [_task("child", root_id=root_id, created_at=1)]

    child_pool = _ChildPool()
    scheduled.clear()
    await bootstrap._queue_owner_start_reminder(
        org_runtime=org_runtime,
        task_pool=child_pool,
        team_id="owner-team",
        session_id=SESSION,
        plan_id="plan-1",
    )
    assert scheduled == []


@pytest.mark.asyncio
async def test_reused_instance_takes_the_current_owner_role():
    team_id = bootstrap.instance_team_id(SESSION, "insight")

    class _Builder:
        def __init__(self):
            self.names = []

        def remove_section(self, name):
            self.names = [item for item in self.names if item != name]

        def add_section(self, section):
            self.names.append(section.name)

    class _Harness:
        def __init__(self):
            self.added = []
            self.removed = []
            self.system_prompt_builder = _Builder()

        def add_tool(self, tool):
            self.added.append(tool.key)

        def remove_tool(self, name):
            self.removed.append(name)

    harness = _Harness()
    agent = SimpleNamespace(
        spec=SimpleNamespace(metadata={"capabilities": ["group-member:insight"]}),
        harness=harness,
    )
    pool = _Pool()
    pool.entries[team_id] = SimpleNamespace(
        current_session_id=SESSION,
        agent=agent,
    )
    launched = _Launcher(pool)

    owner_id = await bootstrap.ensure_member_instance(
        runtime=SimpleNamespace(pool=pool),
        launcher=launched,
        session_id=SESSION,
        outer_team_id="outer-team",
        member=MEMBERS[2],
        is_owner=True,
    )
    assert owner_id == team_id
    assert launched.calls == []
    assert harness.removed == ["complete_team_run"]
    assert harness.added == [bootstrap.result_key(SESSION, "insight")]
    assert "group-coordination" in agent.spec.metadata["capabilities"]
    assert harness.system_prompt_builder.names == ["swarm_org_owner"]

    await bootstrap.ensure_member_instance(
        runtime=SimpleNamespace(pool=pool),
        launcher=launched,
        session_id=SESSION,
        outer_team_id="outer-team",
        member=MEMBERS[2],
        is_owner=False,
    )
    assert launched.calls == []
    assert "group-coordination" not in agent.spec.metadata["capabilities"]
    assert harness.system_prompt_builder.names == []


def test_a_new_organization_drops_the_previous_member_list(monkeypatch):
    group = _MemoryGroup(
        monkeypatch,
        organization_id="gorg-old",
        organization_teams=[
            {"member_name": "insight", "team_id": "old-insight", "role": "member"},
            {"member_name": "business", "team_id": "old-business", "role": "owner"},
        ],
    )
    group_service.upsert_group_organization_team(
        SESSION,
        "gorg-new",
        {
            "member_name": "business",
            "team_id": "old-business",
            "role": "owner",
            "id": "business-planning",
        },
    )
    assert [item["member_name"] for item in group.state["organization_teams"]] == [
        "business"
    ]
    assert group.state["organization_id"] == "gorg-new"


@pytest.mark.asyncio
async def test_failed_organization_is_released_before_a_different_set(monkeypatch):
    old = "gorg-old"
    new = "gorg-new"
    owner_team = bootstrap.instance_team_id(SESSION, "business")
    group = _MemoryGroup(
        monkeypatch,
        organization_id=old,
        organization_teams=[
            {
                "member_name": "business",
                "team_id": owner_team,
                "role": "owner",
                "id": "business-planning",
            }
        ],
        organization_plans=[
            {
                "plan_id": "old-plan",
                "members": ["business", "insight"],
                "owner_member": "business",
                "organization_id": old,
                "status": "running",
                "created_at": 1,
            }
        ],
    )
    pool = _Pool()
    backend = pool.add(owner_team, organization_id=old)
    released = []
    created = []

    class _Runtime:
        async def release_membership(self, **kwargs):
            released.append(kwargs)
            backend.org_task_manager = None

        async def create_organization(self, **kwargs):
            created.append(kwargs)
            backend.org_task_manager = SimpleNamespace(
                organization_id=kwargs["organization_id"]
            )
            return _OrganizationRecord()

    runtime = SimpleNamespace(pool=pool)
    with pytest.raises(RuntimeError, match="live organization"):
        await bootstrap.sync_organization_membership(
            runtime=runtime,
            org_runtime=_Runtime(),
            session_id=SESSION,
            organization_id=new,
            owner_member="business",
            owner_display_name="商业策划专家团",
            teams=[{"member_name": "business", "team_id": owner_team, "role": "owner"}],
        )
    assert released == []
    assert created == []

    group.state["organization_plans"][0]["status"] = "failed"
    await bootstrap.sync_organization_membership(
        runtime=runtime,
        org_runtime=_Runtime(),
        session_id=SESSION,
        organization_id=new,
        owner_member="business",
        owner_display_name="商业策划专家团",
        teams=[{"member_name": "business", "team_id": owner_team, "role": "owner"}],
    )
    assert released[0]["organization_id"] == old
    assert released[0]["owner_team_id"] == owner_team
    assert created[0]["organization_id"] == new
    assert backend.org_task_manager.organization_id == new


@pytest.mark.asyncio
async def test_a_recorded_plan_does_not_pin_the_previous_organization(monkeypatch):
    old = "gorg-old"
    new = "gorg-new"
    owner_team = bootstrap.instance_team_id(SESSION, "business")
    _MemoryGroup(
        monkeypatch,
        organization_id=old,
        organization_teams=[
            {
                "member_name": "business",
                "team_id": owner_team,
                "role": "owner",
                "id": "business-planning",
            }
        ],
        organization_plans=[
            {
                "plan_id": "later-plan",
                "members": ["business", "insight", "architecture"],
                "owner_member": "business",
                "organization_id": old,
                "status": "pending",
                "created_at": 2,
            }
        ],
    )
    pool = _Pool()
    backend = pool.add(owner_team, organization_id=old)
    released = []
    created = []

    class _Runtime:
        async def release_membership(self, **kwargs):
            released.append(kwargs)
            backend.org_task_manager = None

        async def create_organization(self, **kwargs):
            created.append(kwargs)
            backend.org_task_manager = SimpleNamespace(
                organization_id=kwargs["organization_id"]
            )
            return _OrganizationRecord()

    await bootstrap.sync_organization_membership(
        runtime=SimpleNamespace(pool=pool),
        org_runtime=_Runtime(),
        session_id=SESSION,
        organization_id=new,
        owner_member="business",
        owner_display_name="商业策划专家团",
        teams=[{"member_name": "business", "team_id": owner_team, "role": "owner"}],
    )
    assert released[0]["organization_id"] == old
    assert created[0]["organization_id"] == new
    assert backend.org_task_manager.organization_id == new


@pytest.mark.asyncio
async def test_a_started_owner_keeps_a_pending_organization(monkeypatch):
    old = "gorg-old"
    new = "gorg-new"
    owner_team = bootstrap.instance_team_id(SESSION, "business")
    _MemoryGroup(
        monkeypatch,
        organization_plans=[
            {
                "plan_id": "started-plan",
                "members": ["business", "insight"],
                "owner_member": "business",
                "organization_id": old,
                "status": "pending",
                "created_at": 1,
            }
        ],
    )
    pool = _Pool()
    pool.add(owner_team, organization_id=old)
    hold = asyncio.Event()
    owner = asyncio.create_task(hold.wait())
    bootstrap._owner_runs[(SESSION, "started-plan")] = owner
    released = []

    class _Runtime:
        async def release_membership(self, **kwargs):
            released.append(kwargs)

        async def create_organization(self, **_kwargs):
            raise AssertionError("a started round must keep its organization")

    try:
        with pytest.raises(RuntimeError, match="live organization"):
            await bootstrap.sync_organization_membership(
                runtime=SimpleNamespace(pool=pool),
                org_runtime=_Runtime(),
                session_id=SESSION,
                organization_id=new,
                owner_member="business",
                owner_display_name="商业策划专家团",
                teams=[
                    {"member_name": "business", "team_id": owner_team, "role": "owner"}
                ],
            )
        assert released == []
    finally:
        hold.set()
        await owner
        bootstrap._owner_runs.pop((SESSION, "started-plan"), None)


@pytest.mark.asyncio
async def test_recovered_root_is_complete_without_the_new_wait_clock():
    plan_id = "plan-1"
    root_id = bootstrap._root_task_id(plan_id)
    root = _task(root_id, status=OrgTaskStatus.COMPLETED, created_at=1)

    class _Pool:
        def __init__(self, incomplete):
            self.incomplete = incomplete

        async def get_task(self, task_id):
            return root if task_id == root_id else None

        async def list_incomplete_summary_executions(self):
            return self.incomplete

    state, roots = await bootstrap._round_state(
        _Pool([SimpleNamespace(root_task_id="other-root")]),
        plan_id,
    )
    assert state == "completed"
    assert roots == [root]

    state, _roots = await bootstrap._round_state(
        _Pool([SimpleNamespace(root_task_id=root_id)]),
        plan_id,
    )
    assert state == "running"

    class _Empty:
        async def get_task(self, _task_id):
            return None

        async def list_incomplete_summary_executions(self):
            return []

    assert (await bootstrap._round_state(_Empty(), plan_id))[0] == "running"


def _clear_queued_plan_claims() -> None:
    for plan_id in ("plan-1", "plan-2", "plan-next"):
        for member_name in ("business", "insight", "architecture"):
            bootstrap.release_organization_plan_claim(SESSION, plan_id, member_name)
            bootstrap._proxy_backends.pop((SESSION, member_name), None)
            task = bootstrap._queued_plans.pop((SESSION, plan_id, member_name), None)
            if task is not None and not task.done():
                task.cancel()


@pytest.mark.asyncio
async def test_a_later_plan_waits_instead_of_joining_the_older_run(monkeypatch, tmp_path):
    _clear_queued_plan_claims()
    now = bootstrap._now_ms()
    older = {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "running",
        "created_at": now,
        "organization_id": ORG,
        "body": "任务一",
    }
    newer = {
        "plan_id": "plan-2",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "pending",
        "created_at": now + 1,
        "organization_id": "gorg-new",
        "body": "任务二",
    }
    group = _MemoryGroup(monkeypatch, organization_plans=[older, newer])
    history = tmp_path / "history.jsonl"

    class _Conversation:
        history_path = history

    async def _conversation():
        return _Conversation()

    calls = []

    async def _organization(**kwargs):
        calls.append(kwargs["plan"]["plan_id"])
        return "joined"

    monkeypatch.setattr(bootstrap, "run_organization_member", _organization)
    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name="business",
            conversation_mode="group_chat",
            team_name="outer",
            group_conversation=_conversation,
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    history.write_text(
        json.dumps(
            {
                "client_message_id": "plan-1",
                "sender": "user",
                "mentions": ["business", "insight"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    await tool.invoke({"run_id": "retry", "task": "任务一"})
    assert calls == ["plan-1"]

    history.write_text(
        history.read_text(encoding="utf-8")
        + json.dumps(
            {
                "client_message_id": "plan-2",
                "sender": "user",
                "mentions": ["business", "insight"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    queued = await tool.invoke({"run_id": "next", "task": "任务二"})
    assert queued.data["status"] == "queued"
    assert queued.data["plan_id"] == "plan-2"
    assert calls == ["plan-1"]
    group.state["organization_plans"][0]["status"] = "done"
    bootstrap._plan_event(SESSION, "plan-1").set()
    waiter = bootstrap._queued_plans[(SESSION, "plan-2", "business")]
    await waiter
    assert calls == ["plan-1", "plan-2"]


@pytest.mark.asyncio
async def test_a_late_run_team_joins_the_older_plan_before_queueing(monkeypatch, tmp_path):
    _clear_queued_plan_claims()
    now = bootstrap._now_ms()
    older = {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "pending",
        "created_at": now,
        "organization_id": ORG,
        "body": "任务一",
    }
    newer = {
        "plan_id": "plan-2",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "pending",
        "created_at": now + 1,
        "organization_id": ORG,
        "body": "任务二",
    }
    _MemoryGroup(monkeypatch, organization_plans=[older, newer])
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join(
            json.dumps(
                {
                    "client_message_id": plan_id,
                    "sender": "user",
                    "mentions": ["business", "insight"],
                }
            )
            for plan_id in ("plan-1", "plan-2")
        )
        + "\n",
        encoding="utf-8",
    )

    class _Conversation:
        history_path = history

    async def _conversation():
        return _Conversation()

    calls = []

    async def _organization(**kwargs):
        calls.append(kwargs["plan"]["plan_id"])
        return "joined"

    monkeypatch.setattr(bootstrap, "run_organization_member", _organization)
    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name="business",
            conversation_mode="group_chat",
            team_name="outer",
            group_conversation=_conversation,
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    first = await tool.invoke({"run_id": "late", "task": "任务一"})
    assert calls == ["plan-1"]
    assert getattr(first, "data", {}).get("status") != "queued"

    queued = await tool.invoke({"run_id": "next", "task": "任务二"})
    assert queued.data["status"] == "queued"
    assert queued.data["plan_id"] == "plan-2"
    assert calls == ["plan-1"]


def _roster_with_architecture() -> list[dict]:
    return [
        *MEMBERS,
        {
            "kind": "agent_group",
            "id": "software-architecture-design",
            "member_name": "architecture",
            "display_name": "代码架构设计专家团",
        },
    ]


@pytest.mark.asyncio
async def test_overlapping_plan_queues_the_owner_when_it_is_recorded(monkeypatch, tmp_path):
    _clear_queued_plan_claims()
    now = bootstrap._now_ms()
    older = {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "running",
        "created_at": now,
        "organization_id": ORG,
        "body": "上一问",
    }
    group = _MemoryGroup(
        monkeypatch,
        members=_roster_with_architecture(),
        organization_plans=[older],
    )
    calls = []

    async def _run(**kwargs):
        calls.append((kwargs["plan"]["plan_id"], kwargs["ensure_members"]))
        return SimpleNamespace(success=True)

    monkeypatch.setattr(bootstrap, "run_organization_member", _run)
    owner = SimpleNamespace(member_name="business")
    bootstrap.remember_proxy_backend(SESSION, owner)
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=["architecture", "business", "insight"],
        body="后一问",
        plan_id="plan-next",
    )
    assert plan["status"] == "pending"
    assert plan["owner_member"] == "business"
    waiter = bootstrap._queued_plans[(SESSION, "plan-next", "business")]
    await asyncio.sleep(0)
    assert calls == []

    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "client_message_id": "plan-next",
                "sender": "user",
                "mentions": ["architecture", "business", "insight"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def _conversation():
        return SimpleNamespace(history_path=history)

    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name="architecture",
            conversation_mode="group_chat",
            team_name="outer",
            group_conversation=_conversation,
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    queued = await tool.invoke({"run_id": "next", "task": "后一问"})
    assert queued.data["status"] == "queued"
    assert queued.data["plan_id"] == "plan-next"
    assert calls == []
    assert bootstrap._queued_plans[(SESSION, "plan-next", "business")] is waiter

    group.state["organization_plans"][0]["status"] = "done"
    bootstrap._plan_event(SESSION, "plan-1").set()
    await waiter
    assert calls == [("plan-next", True)]
    assert group.state["organization_plans"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_early_bind_failure_keeps_the_overlapping_plan_pending(monkeypatch):
    now = bootstrap._now_ms()
    older = {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "running",
        "created_at": now,
        "organization_id": ORG,
        "body": "上一问",
    }
    newer = {
        "plan_id": "plan-next",
        "members": ["architecture", "business", "insight"],
        "owner_member": "business",
        "status": "pending",
        "created_at": now + 1,
        "organization_id": "gorg-next",
        "body": "后一问",
    }
    group = _MemoryGroup(
        monkeypatch,
        members=_roster_with_architecture(),
        organization_plans=[older, newer],
    )

    async def _ensure(**_kwargs):
        return "org-expert-business"

    async def _sync(**_kwargs):
        raise RuntimeError("team already belongs to a live organization: " + ORG)

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        bootstrap, "_organization_runtime", lambda: (object(), object(), object())
    )
    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ensure)
    monkeypatch.setattr(bootstrap, "sync_organization_membership", _sync)
    monkeypatch.setattr(bootstrap, "_record_team_run", _quiet)
    monkeypatch.setattr(bootstrap, "_set_member_instance_silence", _quiet)
    monkeypatch.setattr(bootstrap, "_block_template_expert_creation", _quiet)
    monkeypatch.setattr(group_service, "upsert_group_organization_team", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(group_service, "group_organization_teams", lambda _session_id: [])

    result = await bootstrap._run_organization_member_once(
        backend=SimpleNamespace(member_name="business"),
        session_id=SESSION,
        plan=newer,
    )
    assert result.success is False
    assert "live organization" in result.error
    assert group.state["organization_plans"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_duplicate_proxy_tool_calls_join_one_member_run(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _once(**_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "done"

    monkeypatch.setattr(bootstrap, "_run_organization_member_once", _once)
    backend = SimpleNamespace(member_name="business")
    plan = {"plan_id": "same-plan"}

    first = asyncio.create_task(
        bootstrap.run_organization_member(
            backend=backend, session_id=SESSION, plan=plan
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        bootstrap.run_organization_member(
            backend=backend, session_id=SESSION, plan=plan
        )
    )
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == ["done", "done"]
    assert calls == 1


@pytest.mark.asyncio
async def test_first_instance_creates_org_and_outer_team_is_not_invited():
    pool = _Pool()
    runtime = SimpleNamespace(pool=pool)
    org_runtime = _OrgRuntime(pool)
    owner_id = bootstrap.instance_team_id(SESSION, "business")
    member_id = bootstrap.instance_team_id(SESSION, "insight")
    pool.add("outer-team")
    pool.add(owner_id)
    pool.add(member_id)
    teams = [
        {
            "member_name": "business",
            "team_id": owner_id,
            "role": "owner",
        },
        {
            "member_name": "insight",
            "team_id": member_id,
            "role": "member",
        },
    ]

    await bootstrap.sync_organization_membership(
        runtime=runtime,
        org_runtime=org_runtime,
        session_id=SESSION,
        organization_id=ORG,
        owner_member="business",
        owner_display_name="商业策划专家团",
        teams=teams,
    )
    assert [item["owner_team_id"] for item in org_runtime.created] == [owner_id]
    assert [
        (item["inviter_team_id"], item["target_team_id"])
        for item in org_runtime.invites
    ] == [(owner_id, member_id)]
    assert pool.entries["outer-team"].agent.team_backend.org_task_manager is None

    # Already-bound teams are not invited twice.
    await bootstrap.sync_organization_membership(
        runtime=runtime,
        org_runtime=org_runtime,
        session_id=SESSION,
        organization_id=ORG,
        owner_member="business",
        owner_display_name="商业策划专家团",
        teams=teams,
    )
    assert len(org_runtime.created) == 1
    assert len(org_runtime.invites) == 1


@pytest.mark.asyncio
async def test_summary_is_paused_only_when_completion_is_confirmed(
    monkeypatch,
):
    paused = []

    async def _pause(**kwargs):
        paused.append(kwargs["team_name"])
        return True

    monkeypatch.setattr("openjiuwen.core.runner.Runner.pause_agent_team", _pause)
    pool = _TaskPool(
        tasks=[_task("root")],
        incomplete=["running-summary"],
        summary_team_id="summary-team",
    )
    await bootstrap.pause_organization_instances(
        session_id=SESSION,
        task_pool=pool,
        team_ids=["owner", "member"],
        include_summary=False,
    )
    assert paused == ["owner", "member"]

    paused.clear()
    pool.incomplete = []
    await bootstrap.pause_organization_instances(
        session_id=SESSION,
        task_pool=pool,
        team_ids=["owner", "member"],
        include_summary=True,
    )
    assert paused == ["owner", "member", "summary-team"]


@pytest.mark.asyncio
async def test_non_owner_receives_only_its_completed_subtasks():
    pool = _TaskPool(
        tasks=[
            _task("mine", root_id="root", team_id="insight"),
            _task(
                "old",
                root_id="old-root",
                team_id="insight",
                created_at=1,
            ),
            _task("other", root_id="root", team_id="architecture"),
            _task(
                "open",
                root_id="root",
                team_id="insight",
                status=OrgTaskStatus.IN_PROGRESS,
            ),
        ]
    )
    result = await bootstrap.team_contribution(pool, "insight", 2000)
    assert [item["task_id"] for item in result] == ["mine"]


@pytest.mark.asyncio
async def test_single_team_run_team_path_is_unchanged(monkeypatch):
    calls = []
    tool = group_chat.RunTeam(
        SimpleNamespace(
            conversation_mode="group_chat",
            team_name="outer-team",
            member_name="insight",
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )

    async def _private(key, task, *, execution=None):
        calls.append(("private", task))
        return "private-result"

    async def _organization(**kwargs):
        calls.append(("organization", kwargs["plan"]["plan_id"]))
        return "organization-result"

    monkeypatch.setattr(tool, "_run", _private)
    monkeypatch.setattr(bootstrap, "run_organization_member", _organization)
    monkeypatch.setattr(bootstrap, "pending_organization_plan", lambda *_args: None)
    result = await tool.invoke({"run_id": "r1", "task": "调研"})
    assert result == "private-result"

    monkeypatch.setattr(
        bootstrap,
        "pending_organization_plan",
        lambda *_args: {"plan_id": "message-1"},
    )
    result = await tool.invoke({"run_id": "r2", "task": "联合调研"})
    assert result == "organization-result"
    assert calls == [
        ("private", "调研"),
        ("organization", "message-1"),
    ]


def _stale_plan() -> dict:
    return {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "running",
        "created_at": 0,
        "body": "联合调研",
    }


@pytest.mark.asyncio
async def test_live_owner_run_is_not_expired_into_a_private_team(monkeypatch):
    plan = _stale_plan()
    _MemoryGroup(monkeypatch, organization_plans=[plan])
    hold = asyncio.Event()
    task = asyncio.create_task(hold.wait())
    bootstrap._owner_runs[(SESSION, plan["plan_id"])] = task
    calls = []
    tool = group_chat.RunTeam(
        SimpleNamespace(
            conversation_mode="group_chat",
            team_name="outer-team",
            member_name="insight",
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )

    async def _private(key, task_text, *, execution=None):
        calls.append(("private", task_text))
        return "private-result"

    async def _organization(**kwargs):
        calls.append(("organization", kwargs["plan"]["plan_id"]))
        return "organization-result"

    monkeypatch.setattr(tool, "_run", _private)
    monkeypatch.setattr(bootstrap, "run_organization_member", _organization)
    try:
        found = bootstrap.pending_organization_plan(SESSION, "insight")
        assert found["plan_id"] == "plan-1"
        assert plan["status"] == "running"
        result = await tool.invoke({"run_id": "retry", "task": "再次挂接"})
        assert result == "organization-result"
        assert calls == [("organization", "plan-1")]
    finally:
        hold.set()
        task.cancel()
        bootstrap._owner_runs.pop((SESSION, plan["plan_id"]), None)


@pytest.mark.asyncio
async def test_stale_plan_without_a_live_owner_run_expires(monkeypatch):
    plan = _stale_plan()
    _MemoryGroup(monkeypatch, organization_plans=[plan])
    bootstrap._owner_runs.pop((SESSION, plan["plan_id"]), None)

    assert bootstrap.pending_organization_plan(SESSION, "insight") is None
    assert plan["status"] == "expired"


@pytest.mark.asyncio
async def test_run_team_returns_before_the_owner_finishes(monkeypatch):
    _MemoryGroup(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    owner_calls = 0

    async def _owner(**_kwargs):
        nonlocal owner_calls
        owner_calls += 1
        started.set()
        await release.wait()

    async def _ready(**_kwargs):
        return "org-expert-business"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (None, None, None))
    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ready)
    monkeypatch.setattr(bootstrap, "_record_team_run", _noop)
    monkeypatch.setattr(bootstrap, "sync_organization_membership", _noop)
    monkeypatch.setattr(bootstrap, "_run_owner_proxy", _owner)
    plan = _stale_plan()
    plan["created_at"] = int(time.time() * 1000)
    backend = SimpleNamespace(member_name="business")
    try:
        result = await bootstrap._run_organization_member_once(
            backend=backend, session_id=SESSION, plan=plan
        )
        assert result.success is True
        assert result.data["posted_to_group"] is False
        assert result.data["accepted"] is True
        assert result.data["role"] == "owner"
        assert "Do not call run_team again" in result.data["content"]
        await asyncio.wait_for(started.wait(), timeout=1)
        member = await bootstrap._run_organization_member_once(
            backend=SimpleNamespace(member_name="insight"),
            session_id=SESSION,
            plan=plan,
        )
        assert member.data["role"] == "member"
        assert member.data["posted_to_group"] is False
        again = await bootstrap._run_organization_member_once(
            backend=backend, session_id=SESSION, plan=plan
        )
        assert again.data["status"] == "running"
        assert owner_calls == 1
    finally:
        release.set()
        task = bootstrap._owner_runs.get((SESSION, plan["plan_id"]))
        if task is not None and not task.done():
            await task
        bootstrap._owner_runs.pop((SESSION, plan["plan_id"]), None)
        bootstrap._member_runs.clear()


class _CardStore:
    def __init__(self, _group_id):
        self.records = {}
        self.calls = []

    def read(self):
        return self.records

    async def upsert_organization_card(self, execution_id, **kwargs):
        self.calls.append((execution_id, kwargs))
        self.records[execution_id] = kwargs
        return kwargs


def _install_card_store(monkeypatch) -> _CardStore:
    store = _CardStore(SESSION)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.group_execution.GroupExecutions",
        lambda group_id: store,
    )
    return store


@pytest.mark.asyncio
async def test_organization_card_rows_follow_expert_children_only(monkeypatch):
    plan = _stale_plan()
    root_id = bootstrap._root_task_id(plan["plan_id"])
    accepted = SimpleNamespace(
        task_id="child-business",
        parent_task_id=root_id,
        title="用户价值及第一版功能范围",
        status="COMPLETED",
        task_type=None,
        created_at=2,
        assignment=SimpleNamespace(team_id="org-expert-business"),
    )
    claimed = SimpleNamespace(
        task_id="child-insight",
        parent_task_id=root_id,
        title="技术路线及主要风险",
        status="CLAIMED",
        task_type=None,
        created_at=3,
        assignment=SimpleNamespace(team_id="org-expert-insight"),
    )
    listed = [
        SimpleNamespace(
            task_id=root_id,
            parent_task_id=None,
            title="Swarm group request",
            status="IN_PROGRESS",
            task_type=None,
            created_at=1,
            assignment=SimpleNamespace(team_id="org-expert-business"),
        ),
        accepted,
        claimed,
        SimpleNamespace(
            task_id="internal-teammate",
            parent_task_id="child-business",
            title="团内拆分",
            status="IN_PROGRESS",
            task_type=None,
            created_at=4,
            assignment=SimpleNamespace(team_id="org-expert-business"),
        ),
        SimpleNamespace(
            task_id="org-summary-1",
            parent_task_id=None,
            root_task_id=root_id,
            title="可行性报告汇总",
            status="IN_PROGRESS",
            task_type="organization.summary",
            created_at=5,
            assignment=SimpleNamespace(team_id="org-summary"),
        ),
    ]
    reviews = {"child-business": SimpleNamespace(review_status="ACCEPTED")}

    class _Pool:
        async def list_tasks(self, **_kwargs):
            return listed

        async def get_task_review(self, task_id):
            return reviews.get(task_id)

    _MemoryGroup(
        monkeypatch,
        organization_teams=[
            {"member_name": "business", "team_id": "org-expert-business", "role": "owner"},
            {"member_name": "insight", "team_id": "org-expert-insight", "role": "member"},
        ],
    )
    rows, details = await bootstrap._organization_rows(
        SESSION, plan, _Pool(), fail_unfinished=False
    )
    assert list(rows) == ["child-business", "child-insight", "org-summary-1"]
    assert rows["child-business"] == "completed"
    assert rows["child-insight"] == "in_progress"
    assert rows["org-summary-1"] == "in_progress"
    assert details["child-business"]["title"] == "用户价值及第一版功能范围"
    assert details["child-business"]["member_id"] == "商业策划专家团"
    assert details["child-insight"]["title"] == "技术路线及主要风险"
    assert details["child-insight"]["member_id"] == "技术洞察专家团"
    assert details["org-summary-1"]["title"] == "可行性报告汇总"
    assert details["org-summary-1"]["member_id"] == "Summary Team"
    failed_rows, _failed_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool(), fail_unfinished=True
    )
    assert failed_rows["child-business"] == "completed"
    assert failed_rows["child-insight"] == "failed"

    store = _install_card_store(monkeypatch)
    await bootstrap.publish_organization_card(
        session_id=SESSION,
        plan=plan,
        organization_id=ORG,
        team_id="org-expert-business",
        task_pool=_Pool(),
    )
    execution_id, fields = store.calls[0]
    assert execution_id == bootstrap.organization_execution_id(SESSION, plan["plan_id"])
    assert fields["title"] == "联合调研"
    assert fields["status"] == "running"
    assert fields["owner_member"] == "business"
    assert fields["root_task_id"] == root_id
    assert fields["task_details"]["child-business"]["title"] == "用户价值及第一版功能范围"
    await bootstrap.publish_organization_card(
        session_id=SESSION,
        plan=plan,
        organization_id=ORG,
        team_id="org-expert-business",
        task_pool=_Pool(),
        status="failed",
        fail_unfinished=True,
        create=False,
    )
    assert store.calls[-1][1]["status"] == "failed"
    assert store.calls[-1][1]["tasks"]["child-insight"] == "failed"
    assert store.calls[-1][1]["tasks"]["child-business"] == "completed"
    store.records.clear()
    await bootstrap.publish_organization_card(
        session_id=SESSION,
        plan=plan,
        organization_id=ORG,
        team_id="org-expert-business",
        status="failed",
        fail_unfinished=True,
        create=False,
    )
    assert len(store.calls) == 2


@pytest.mark.asyncio
async def test_card_rows_keep_names_after_another_round_replaces_the_roster(monkeypatch):
    plan = _stale_plan()
    root_id = bootstrap._root_task_id(plan["plan_id"])
    business = bootstrap.instance_team_id(SESSION, "business")
    insight = bootstrap.instance_team_id(SESSION, "insight")
    listed = [
        SimpleNamespace(
            task_id="child-business",
            parent_task_id=root_id,
            title="第一批内测使用对象",
            status="COMPLETED",
            task_type=None,
            created_at=2,
            assignment=SimpleNamespace(team_id=business),
        ),
        SimpleNamespace(
            task_id="child-insight",
            parent_task_id=root_id,
            title="上线首日最关键环节",
            status="CLAIMED",
            task_type=None,
            created_at=3,
            assignment=SimpleNamespace(team_id=insight),
        ),
    ]

    class _Pool:
        async def list_tasks(self, **_kwargs):
            return listed

        async def get_task_review(self, task_id):
            if task_id == "child-business":
                return SimpleNamespace(review_status="ACCEPTED")
            return None

    _MemoryGroup(
        monkeypatch,
        organization_id="gorg-next",
        organization_teams=[
            {"member_name": "writer", "team_id": "org-expert-other", "role": "member"},
        ],
    )
    _rows, details = await bootstrap._organization_rows(
        SESSION, plan, _Pool(), fail_unfinished=False
    )
    assert details["child-business"]["member_id"] == "商业策划专家团"
    assert details["child-insight"]["member_id"] == "技术洞察专家团"


@pytest.mark.asyncio
async def test_hierarchical_summary_row_names_the_owner_team(monkeypatch):
    plan = _stale_plan()
    root_id = bootstrap._root_task_id(plan["plan_id"])
    root = SimpleNamespace(
        task_id=root_id,
        parent_task_id=None,
        root_task_id=root_id,
        title="Swarm group request",
        status="IN_PROGRESS",
        task_type=None,
        created_at=1,
        assignment=SimpleNamespace(team_id="org-expert-business"),
        aggregation=SimpleNamespace(
            mode="HIERARCHICAL",
            controller_team_id="org-expert-business",
        ),
        output_context=None,
    )
    child = SimpleNamespace(
        task_id="child-business",
        parent_task_id=root_id,
        title="用户价值及第一版功能范围",
        status="CLAIMED",
        task_type=None,
        created_at=2,
        assignment=SimpleNamespace(team_id="org-expert-business"),
    )

    class _Pool:
        def __init__(self, tasks):
            self._tasks = tasks

        async def list_tasks(self, **_kwargs):
            return self._tasks

        async def get_task_review(self, task_id):
            if task_id == "child-business":
                return SimpleNamespace(review_status="ACCEPTED")
            return None

    _MemoryGroup(
        monkeypatch,
        organization_teams=[
            {
                "member_name": "business",
                "team_id": "org-expert-business",
                "role": "owner",
            }
        ],
    )
    early_rows, _early_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert root_id not in early_rows

    child.status = "COMPLETED"
    waiting_rows, waiting_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert list(waiting_rows) == ["child-business", root_id]
    assert waiting_rows[root_id] == "in_progress"
    assert waiting_details[root_id]["title"] == "汇总：联合调研"
    assert waiting_details[root_id]["member_id"] == "商业策划专家团"

    root.status = "COMPLETED"
    root.output_context = SimpleNamespace(
        description="# 《10人团队内部知识问答助手》可行性报告（约500字）\n\n正文"
    )
    rows, details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert list(rows) == ["child-business", root_id]
    assert details[root_id]["title"] == "汇总：联合调研"
    assert details[root_id]["member_id"] == "商业策划专家团"
    assert rows[root_id] == "completed"

    root.aggregation.mode = "SUMMARY_TEAM"
    plain_rows, _plain_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert root_id not in plain_rows


@pytest.mark.asyncio
async def test_accepted_repair_opens_the_hierarchical_summary_row(monkeypatch):
    plan = _stale_plan()
    root_id = bootstrap._root_task_id(plan["plan_id"])
    root = SimpleNamespace(
        task_id=root_id,
        parent_task_id=None,
        root_task_id=root_id,
        title="Swarm group request",
        status="COMPLETED",
        task_type=None,
        created_at=1,
        assignment=SimpleNamespace(team_id="org-expert-business"),
        aggregation=SimpleNamespace(
            mode="HIERARCHICAL",
            controller_team_id="org-expert-business",
        ),
        output_context=SimpleNamespace(description="10人团队内部知识问答助手 · 可行性报告（简）"),
    )
    original = SimpleNamespace(
        task_id="child-tech",
        parent_task_id=root_id,
        title="技术洞察章节",
        status="COMPLETED",
        task_type=None,
        created_at=2,
        assignment=SimpleNamespace(team_id="org-expert-insight"),
        metadata={},
    )
    repair = SimpleNamespace(
        task_id="repair-tech",
        parent_task_id=root_id,
        title="修复：技术洞察章节",
        status="CLAIMED",
        task_type=None,
        created_at=3,
        assignment=SimpleNamespace(team_id="org-expert-insight"),
        metadata={"repairs_task_id": "child-tech"},
    )
    reviews = {"child-tech": SimpleNamespace(review_status="NEEDS_REVISION")}

    class _Pool:
        async def list_tasks(self, **_kwargs):
            return [root, original, repair]

        async def get_task_review(self, task_id):
            return reviews.get(task_id)

    _MemoryGroup(
        monkeypatch,
        organization_teams=[
            {"member_name": "business", "team_id": "org-expert-business", "role": "owner"},
            {"member_name": "insight", "team_id": "org-expert-insight", "role": "member"},
        ],
    )
    hidden, _hidden_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool(), fail_unfinished=False
    )
    assert root_id not in hidden
    assert hidden["child-tech"] == "failed"

    repair.status = "COMPLETED"
    reviews["repair-tech"] = SimpleNamespace(review_status="ACCEPTED")
    rows, details = await bootstrap._organization_rows(
        SESSION, plan, _Pool(), fail_unfinished=False
    )
    assert rows["child-tech"] == "failed"
    assert rows["repair-tech"] == "completed"
    assert rows[root_id] == "completed"
    assert details[root_id]["title"] == "汇总：联合调研"
    assert details[root_id]["member_id"] == "商业策划专家团"


@pytest.mark.asyncio
async def test_card_appears_only_after_the_owner_organization_is_bound(monkeypatch):
    plan = _stale_plan()
    plan["created_at"] = int(time.time() * 1000)
    _MemoryGroup(monkeypatch, organization_plans=[plan])
    published = []

    async def _publish(**kwargs):
        published.append(kwargs)

    async def _ready(**_kwargs):
        return "org-expert-business"

    async def _noop(*_args, **_kwargs):
        return None

    async def _owner(**_kwargs):
        return None

    async def _bound(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id=ORG))

    async def _unbound(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id="other"))

    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (object(), None, None))
    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ready)
    monkeypatch.setattr(bootstrap, "_record_team_run", _noop)
    monkeypatch.setattr(bootstrap, "sync_organization_membership", _noop)
    monkeypatch.setattr(bootstrap, "_run_owner_proxy", _owner)
    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_live_backend", _unbound)
    try:
        await bootstrap._run_organization_member_once(
            backend=SimpleNamespace(member_name="business"),
            session_id=SESSION,
            plan=plan,
        )
        assert published == []
        monkeypatch.setattr(bootstrap, "_live_backend", _bound)
        await bootstrap._run_organization_member_once(
            backend=SimpleNamespace(member_name="business"),
            session_id=SESSION,
            plan=plan,
        )
        await bootstrap._run_organization_member_once(
            backend=SimpleNamespace(member_name="insight"),
            session_id=SESSION,
            plan=plan,
        )
        assert len(published) == 1
        assert published[0]["organization_id"] == ORG
        assert published[0]["team_id"] == "org-expert-business"
    finally:
        await asyncio.sleep(0)
        task = bootstrap._owner_runs.get((SESSION, plan["plan_id"]))
        if task is not None and not task.done():
            await task
        bootstrap._owner_runs.pop((SESSION, plan["plan_id"]), None)
        bootstrap._member_runs.clear()


@pytest.mark.asyncio
async def test_unregister_drops_membership_without_dissolve(monkeypatch):
    plan = {"plan_id": "plan-1", "status": "done"}
    group = _MemoryGroup(
        monkeypatch,
        organization_id=ORG,
        organization_teams=[
            {"team_id": "org-expert-business", "role": "owner", "member_name": "business"}
        ],
        organization_plans=[plan],
    )
    calls = []

    class _Runtime:
        async def dissolve_organization(self, **kwargs):
            calls.append(("dissolve", kwargs))

        async def release_membership(self, **kwargs):
            calls.append(("release", kwargs))

    class _Pool:
        async def release_membership(self):
            calls.append(("pool", None))

    await bootstrap.unregister_completed_organization(
        org_runtime=_Runtime(),
        task_pool=_Pool(),
        session_id=SESSION,
        organization_id=ORG,
        owner_team_id="org-expert-business",
    )

    assert [name for name, _ in calls] == ["release", "pool"]
    assert calls[0][1]["organization_id"] == ORG
    assert calls[0][1]["owner_team_id"] == "org-expert-business"
    assert "organization_id" not in group.state
    assert group.state["organization_teams"] == []
    assert group.state["organization_plans"] == [plan]


@pytest.mark.asyncio
async def test_unregister_still_clears_the_group_when_live_release_fails(monkeypatch):
    group = _MemoryGroup(
        monkeypatch,
        organization_id=ORG,
        organization_teams=[{"team_id": "org-expert-business"}],
        organization_plans=[{"plan_id": "plan-1"}],
    )
    calls = []

    class _Runtime:
        async def release_membership(self, **_kwargs):
            raise RuntimeError("owner runtime is gone")

    class _Pool:
        async def release_membership(self):
            calls.append("pool")

    await bootstrap.unregister_completed_organization(
        org_runtime=_Runtime(),
        task_pool=_Pool(),
        session_id=SESSION,
        organization_id=ORG,
        owner_team_id="org-expert-business",
    )

    assert calls == ["pool"]
    assert "organization_id" not in group.state
    assert group.state["organization_plans"][0]["plan_id"] == "plan-1"


@pytest.mark.asyncio
async def test_owner_completion_updates_the_card_without_finish(monkeypatch):
    plan = _stale_plan()
    group = _MemoryGroup(
        monkeypatch,
        organization_id=ORG,
        organization_teams=[
            {"team_id": "org-expert-business", "role": "owner", "member_name": "business"}
        ],
        organization_plans=[plan],
    )
    calls = []
    posts = []
    released = []

    async def _publish(**kwargs):
        calls.append(kwargs)
        future = group_chat._results.get(bootstrap.result_key(SESSION, "business"))
        if future is not None and not future.done():
            future.set_result("final report")

    async def _round(*_args, **_kwargs):
        return "completed", []

    async def _wait(*_args, **_kwargs):
        return []

    async def _prime(**_kwargs):
        return None

    async def _quiet(**_kwargs):
        return None

    class _Pool:
        async def release_membership(self):
            released.append("pool")

    class _Runtime:
        async def dissolve_organization(self, **_kwargs):
            raise AssertionError("a finished round must not dissolve the organization")

        async def release_membership(self, **kwargs):
            released.append(kwargs)

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=_Pool())

    async def _post(*_args, **kwargs):
        posts.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(message_id="msg-1"))

    async def _forbid_finish(*_args, **_kwargs):
        raise AssertionError("organization completion must not call finish")

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_round_state", _round)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _wait)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _prime)
    monkeypatch.setattr(bootstrap, "_queue_owner_start_reminder", _quiet)
    monkeypatch.setattr(bootstrap, "pause_organization_instances", _quiet)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.group_execution.GroupExecutions.finish",
        _forbid_finish,
    )
    result = await bootstrap._run_owner_proxy(
        backend=SimpleNamespace(member_name="business", append_group_message=_post),
        runtime=object(),
        org_runtime=_Runtime(),
        session_id=SESSION,
        plan=plan,
        organization_id=ORG,
        team_id="org-expert-business",
    )
    assert result.data["posted_to_group"] is True
    assert result.data["message_id"] == "msg-1"
    assert posts[0]["client_message_id"] == "expert-result-plan-1"
    assert calls[-1]["status"] == "completed"
    assert calls[-1]["message_id"] == "msg-1"
    assert plan["status"] == "done"
    assert released[0]["organization_id"] == ORG
    assert released[1] == "pool"
    assert "organization_id" not in group.state
    assert group.state["organization_teams"] == []
    assert group.state["organization_plans"] == [plan]


@pytest.mark.asyncio
async def test_next_plan_joins_only_after_the_finished_round_unregisters(monkeypatch):
    now = bootstrap._now_ms()
    older = {
        "plan_id": "plan-1",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "running",
        "created_at": now,
        "organization_id": ORG,
        "body": "任务一",
    }
    newer = {
        "plan_id": "plan-2",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "pending",
        "created_at": now + 1,
        "organization_id": ORG,
        "body": "任务二",
    }
    _MemoryGroup(
        monkeypatch,
        organization_id=ORG,
        organization_teams=[
            {"team_id": "org-expert-business", "role": "owner", "member_name": "business"}
        ],
        organization_plans=[older, newer],
    )
    bootstrap._plan_events.pop((SESSION, "plan-1"), None)
    bootstrap._plan_events.pop((SESSION, "plan-2"), None)
    order = []

    async def _publish(**_kwargs):
        future = group_chat._results.get(bootstrap.result_key(SESSION, "business"))
        if future is not None and not future.done():
            future.set_result("final report")

    async def _round(*_args, **_kwargs):
        return "completed", []

    async def _members(*_args, **_kwargs):
        return []

    async def _noop(**_kwargs):
        return None

    async def _pause(**_kwargs):
        order.append("pause")

    async def _release(**_kwargs):
        order.append("release")
        await asyncio.sleep(0)

    class _Pool:
        async def release_membership(self):
            order.append("pool")

    class _Runtime:
        async def release_membership(self, **kwargs):
            await _release(**kwargs)

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=_Pool())

    async def _post(*_args, **_kwargs):
        return SimpleNamespace(message=SimpleNamespace(message_id="msg-1"))

    async def _join(**kwargs):
        order.append("join")
        assert older["status"] == "done"
        assert kwargs["plan"]["plan_id"] == "plan-2"
        return "joined"

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_round_state", _round)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _members)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _noop)
    monkeypatch.setattr(bootstrap, "_queue_owner_start_reminder", _noop)
    monkeypatch.setattr(bootstrap, "pause_organization_instances", _pause)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    monkeypatch.setattr(bootstrap, "run_organization_member", _join)

    async def _queued():
        await bootstrap.wait_for_earlier_organization_plans(SESSION, "business", newer)
        await bootstrap.run_organization_member(
            backend=SimpleNamespace(member_name="business"),
            session_id=SESSION,
            plan=newer,
        )

    waiter = asyncio.create_task(_queued())
    await asyncio.sleep(0)
    await bootstrap._run_owner_proxy(
        backend=SimpleNamespace(member_name="business", append_group_message=_post),
        runtime=object(),
        org_runtime=_Runtime(),
        session_id=SESSION,
        plan=older,
        organization_id=ORG,
        team_id="org-expert-business",
    )
    await waiter
    assert order.index("join") > order.index("release")
    assert order.index("release") > order.index("pause")


@pytest.mark.asyncio
async def test_missing_run_team_fails_before_the_root_is_created(monkeypatch):
    plan = _stale_plan()
    _MemoryGroup(monkeypatch, organization_id=ORG, organization_plans=[plan])
    primed = []

    async def _publish(**_kwargs):
        return None

    async def _wait(*_args, **_kwargs):
        return ["insight"]

    async def _prime(**_kwargs):
        primed.append(True)

    class _Pool:
        pass

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=_Pool())

    async def _post(*_args, **_kwargs):
        raise AssertionError("a round missing run_team must not post")

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _wait)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _prime)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    with pytest.raises(RuntimeError, match="did not call run_team: insight"):
        await bootstrap._run_owner_proxy(
            backend=SimpleNamespace(member_name="business", append_group_message=_post),
            runtime=object(),
            org_runtime=object(),
            session_id=SESSION,
            plan=plan,
            organization_id=ORG,
            team_id="org-expert-business",
        )
    assert primed == []
    assert plan["status"] == "failed"
    assert "insight" in plan["error"]


@pytest.mark.asyncio
async def test_queued_owner_launches_members_before_the_join_wait(monkeypatch):
    plan = _stale_plan()
    plan["status"] = "pending"
    _MemoryGroup(monkeypatch, organization_id=ORG, organization_plans=[plan])
    order = []

    async def _publish(**_kwargs):
        return None

    async def _ensure(**kwargs):
        order.append("ensure")
        assert kwargs["plan"]["members"] == ["business", "insight"]
        assert kwargs["organization_id"] == ORG

    async def _wait(*_args, **_kwargs):
        order.append("wait")
        return ["insight"]

    async def _prime(**_kwargs):
        raise AssertionError("missing members must not prime the root")

    class _Pool:
        pass

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=_Pool())

    async def _post(*_args, **_kwargs):
        raise AssertionError("a round missing run_team must not post")

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_ensure_mentioned_member_instances", _ensure)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _wait)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _prime)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    with pytest.raises(RuntimeError, match="did not call run_team: insight"):
        await bootstrap._run_owner_proxy(
            backend=SimpleNamespace(member_name="business", append_group_message=_post),
            runtime=object(),
            org_runtime=SimpleNamespace(_expert_team_launcher=object()),
            session_id=SESSION,
            plan=plan,
            organization_id=ORG,
            team_id="org-expert-business",
            ensure_members=True,
        )
    assert order == ["ensure", "wait"]


@pytest.mark.asyncio
async def test_queued_plan_ensures_a_member_that_skipped_run_team(monkeypatch):
    plan = _stale_plan()
    group = _MemoryGroup(monkeypatch, organization_id=ORG, organization_plans=[plan])
    launched = []
    synced = []

    async def _ensure(**kwargs):
        launched.append(kwargs["member"]["member_name"])
        return bootstrap.instance_team_id(SESSION, kwargs["member"]["member_name"])

    async def _sync(**kwargs):
        synced.append([item["member_name"] for item in kwargs["teams"]])

    async def _record(*_args, **_kwargs):
        return None

    async def _silence(*_args, **_kwargs):
        return None

    async def _block(*_args, **_kwargs):
        return None

    teams = []

    def _upsert(_session_id, _organization_id, team):
        teams.append(team)
        group.state["organization_teams"] = teams

    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ensure)
    monkeypatch.setattr(bootstrap, "sync_organization_membership", _sync)
    monkeypatch.setattr(bootstrap, "_record_team_run", _record)
    monkeypatch.setattr(bootstrap, "_set_member_instance_silence", _silence)
    monkeypatch.setattr(bootstrap, "_block_template_expert_creation", _block)
    monkeypatch.setattr(group_service, "upsert_group_organization_team", _upsert)
    monkeypatch.setattr(
        group_service, "group_organization_teams", lambda _session_id: list(teams)
    )

    await bootstrap._ensure_mentioned_member_instances(
        runtime=object(),
        org_runtime=object(),
        launcher=object(),
        session_id=SESSION,
        plan=plan,
        organization_id=ORG,
        record_backend=SimpleNamespace(member_name="business"),
    )

    assert launched == ["business", "insight"]
    assert [item["role"] for item in teams] == ["owner", "member"]
    assert synced == [["business", "insight"]]


@pytest.mark.asyncio
async def test_queued_owner_join_ensures_members_after_the_older_plan(monkeypatch):
    plan = _stale_plan()
    plan["owner_member"] = "business"
    seen = []

    async def _wait(*_args, **_kwargs):
        return None

    async def _run(**kwargs):
        seen.append(kwargs["ensure_members"])
        return SimpleNamespace(success=True)

    monkeypatch.setattr(bootstrap, "wait_for_earlier_organization_plans", _wait)
    monkeypatch.setattr(
        bootstrap, "organization_plan_for_member", lambda *_args, **_kwargs: plan
    )
    monkeypatch.setattr(bootstrap, "run_organization_member", _run)

    await bootstrap._join_queued_organization_plan(
        backend=SimpleNamespace(member_name="business"),
        session_id=SESSION,
        member_name="business",
        plan_id=plan["plan_id"],
    )
    await bootstrap._join_queued_organization_plan(
        backend=SimpleNamespace(member_name="insight"),
        session_id=SESSION,
        member_name="insight",
        plan_id=plan["plan_id"],
    )
    assert seen == [True, False]


@pytest.mark.asyncio
async def test_swarm_owner_loses_template_team_creation(monkeypatch):
    removed = []

    class _Harness:
        def remove_tool(self, name):
            removed.append(name)

    class _Pool:
        async def get(self, _team_id):
            return SimpleNamespace(
                current_session_id=SESSION,
                agent=SimpleNamespace(harness=_Harness()),
            )

    await bootstrap._block_template_expert_creation(
        SimpleNamespace(pool=_Pool()), "org-expert-business", SESSION
    )
    assert removed == ["org_create_and_invite_expert_team"]


@pytest.mark.asyncio
async def test_owner_failure_marks_unfinished_card_rows_failed(monkeypatch):
    plan = _stale_plan()
    group = _MemoryGroup(
        monkeypatch,
        organization_id=ORG,
        organization_teams=[
            {"team_id": "org-expert-business", "role": "owner", "member_name": "business"}
        ],
        organization_plans=[plan],
    )
    calls = []
    released = []

    async def _publish(**kwargs):
        calls.append(kwargs)

    async def _round(*_args, **_kwargs):
        return "failed", []

    async def _wait(*_args, **_kwargs):
        return []

    async def _prime(**_kwargs):
        return None

    async def _quiet(**_kwargs):
        return None

    class _Pool:
        async def release_membership(self):
            released.append("pool")

    class _Runtime:
        async def dissolve_organization(self, **_kwargs):
            raise AssertionError("a failed round must not dissolve the organization")

        async def release_membership(self, **_kwargs):
            released.append(plan["status"])

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(org_task_manager=_Pool())

    async def _post(*_args, **_kwargs):
        raise AssertionError("a failed organization must not post")

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_round_state", _round)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _wait)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _prime)
    monkeypatch.setattr(bootstrap, "_queue_owner_start_reminder", _quiet)
    monkeypatch.setattr(bootstrap, "pause_organization_instances", _quiet)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    with pytest.raises(RuntimeError, match="organization root task failed"):
        await bootstrap._run_owner_proxy(
            backend=SimpleNamespace(member_name="business", append_group_message=_post),
            runtime=object(),
            org_runtime=_Runtime(),
            session_id=SESSION,
            plan=plan,
            organization_id=ORG,
            team_id="org-expert-business",
        )
    assert calls[-1]["status"] == "failed"
    assert calls[-1]["fail_unfinished"] is True
    assert plan["status"] == "failed"
    assert released == ["running", "pool"]
    assert "organization_id" not in group.state
    assert group.state["organization_teams"] == []


@pytest.mark.asyncio
async def test_failed_round_keeps_a_binding_that_already_moved(monkeypatch):
    plan = _stale_plan()
    group = _MemoryGroup(
        monkeypatch,
        organization_id="gorg-next",
        organization_teams=[
            {"team_id": "org-expert-next", "role": "owner", "member_name": "business"}
        ],
        organization_plans=[plan],
    )
    released = []

    async def _publish(**_kwargs):
        return None

    async def _round(*_args, **_kwargs):
        return "failed", []

    async def _wait(*_args, **_kwargs):
        return []

    async def _quiet(**_kwargs):
        return None

    class _Runtime:
        async def release_membership(self, **_kwargs):
            released.append("release")

    async def _backend(*_args, **_kwargs):
        return SimpleNamespace(
            org_task_manager=SimpleNamespace(organization_id="gorg-next")
        )

    async def _post(*_args, **_kwargs):
        raise AssertionError("a failed organization must not post")

    monkeypatch.setattr(bootstrap, "publish_organization_card", _publish)
    monkeypatch.setattr(bootstrap, "_round_state", _round)
    monkeypatch.setattr(bootstrap, "_wait_for_members", _wait)
    monkeypatch.setattr(bootstrap, "_owner_primes_root_task", _quiet)
    monkeypatch.setattr(bootstrap, "_queue_owner_start_reminder", _quiet)
    monkeypatch.setattr(bootstrap, "pause_organization_instances", _quiet)
    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    with pytest.raises(RuntimeError, match="organization root task failed"):
        await bootstrap._run_owner_proxy(
            backend=SimpleNamespace(member_name="business", append_group_message=_post),
            runtime=object(),
            org_runtime=_Runtime(),
            session_id=SESSION,
            plan=plan,
            organization_id=ORG,
            team_id="org-expert-business",
        )
    assert released == []
    assert group.state["organization_id"] == "gorg-next"
    assert group.state["organization_teams"][0]["team_id"] == "org-expert-next"


def test_followup_policy_lets_the_model_choose_proxy_or_instance():
    from jiuwenswarm.agents.swarm.group_execution import TASK_RULES

    assert "when one agent can do it" in TASK_RULES
    assert "loads the context from its earlier child task" in TASK_RULES
    assert "already in this team" in bootstrap._FOLLOWUP_PROMPT_SUFFIX


@pytest.mark.asyncio
async def test_saved_organization_instance_matches_the_stable_team(tmp_path):
    member = "insight"
    team_id = bootstrap.instance_team_id(SESSION, member)
    path = tmp_path / "team-runs" / f"organization-{member}.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "kind": "organization",
                "team_name": team_id,
                "session_id": SESSION,
                "member_name": member,
            }
        ),
        encoding="utf-8",
    )

    class _Conversation:
        history_path = tmp_path / "history.jsonl"

    async def _conversation():
        return _Conversation()

    backend = SimpleNamespace(group_conversation=_conversation)
    found = await bootstrap.saved_organization_instance(backend, member, SESSION)
    assert found["team_name"] == team_id
    assert await bootstrap.saved_organization_instance(backend, member, "other-session") is None


@pytest.mark.asyncio
async def test_run_team_resumes_the_organization_instance(monkeypatch):
    calls = []

    def _pending(*_args, **_kwargs):
        return None

    async def _saved(*_args, **_kwargs):
        return {"kind": "organization"}

    async def _follow(**kwargs):
        calls.append(kwargs)
        from openjiuwen.core.foundation.tool import ToolOutput

        return ToolOutput(success=True, data={"resumed_instance": True})

    async def _private(*_args, **_kwargs):
        raise AssertionError("follow-up must not open a private team")

    monkeypatch.setattr(bootstrap, "pending_organization_plan", _pending)
    monkeypatch.setattr(bootstrap, "saved_organization_instance", _saved)
    monkeypatch.setattr(bootstrap, "run_instance_followup", _follow)
    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name="insight",
            conversation_mode="group_chat",
            team_name="outer",
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    monkeypatch.setattr(tool, "_run", _private)
    result = await tool.invoke({"action": "continue", "run_id": "follow-1", "content": "举个例子"})
    assert result.data["resumed_instance"] is True
    assert calls[0]["task"] == "举个例子"
    assert calls[0]["session_id"] == SESSION


@pytest.mark.asyncio
async def test_run_team_without_an_organization_instance_stays_private(monkeypatch):
    calls = []

    def _pending(*_args, **_kwargs):
        return None

    async def _saved(*_args, **_kwargs):
        return None

    async def _follow(**_kwargs):
        raise AssertionError("a first private run must not resume an organization instance")

    async def _private(*_args, **_kwargs):
        calls.append(_args[-1] if _args else _kwargs.get("task"))
        from openjiuwen.core.foundation.tool import ToolOutput

        return ToolOutput(success=True, data={"private": True})

    monkeypatch.setattr(bootstrap, "pending_organization_plan", _pending)
    monkeypatch.setattr(bootstrap, "saved_organization_instance", _saved)
    monkeypatch.setattr(bootstrap, "run_instance_followup", _follow)
    monkeypatch.setattr(group_service, "read_group", lambda _group_id: None)
    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name="insight",
            conversation_mode="group_chat",
            team_name="outer",
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    monkeypatch.setattr(tool, "_run", _private)
    result = await tool.invoke({"run_id": "solo-1", "task": "单独做一份研究"})
    assert result.data["private"] is True
    assert calls == ["单独做一份研究"]


def _run_team(tmp_path, member_name="insight"):
    history = tmp_path / "history.jsonl"

    class _Conversation:
        history_path = history

    async def _conversation():
        return _Conversation()

    tool = group_chat.RunTeam(
        SimpleNamespace(
            member_name=member_name,
            conversation_mode="group_chat",
            team_name="outer",
            group_conversation=_conversation,
        ),
        SimpleNamespace(session_id=SESSION),
        {},
    )
    return tool, history


@pytest.mark.asyncio
async def test_a_finished_multi_mention_does_not_open_a_private_team(
    monkeypatch, tmp_path
):
    _MemoryGroup(
        monkeypatch,
        organization_plans=[
            {
                "plan_id": "plan-closed",
                "members": ["business", "insight"],
                "owner_member": "business",
                "status": "failed",
                "created_at": bootstrap._now_ms(),
                "organization_id": ORG,
                "body": "联合调研",
            }
        ],
    )

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("a finished multi-@ must not join, resume, or go private")

    monkeypatch.setattr(bootstrap, "run_organization_member", _forbidden)
    monkeypatch.setattr(bootstrap, "saved_organization_instance", _forbidden)
    monkeypatch.setattr(bootstrap, "run_instance_followup", _forbidden)
    tool, history = _run_team(tmp_path)
    monkeypatch.setattr(tool, "_run", _forbidden)
    history.write_text(
        json.dumps(
            {
                "client_message_id": "plan-closed",
                "sender": "user",
                "mentions": ["business", "insight"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = await tool.invoke({"run_id": "late", "task": "联合调研"})
    assert result.success is True
    assert result.data["posted_to_group"] is False
    assert result.data["plan_id"] == "plan-closed"
    assert result.data["status"] == "failed"
    assert "already ended" in result.data["content"]


@pytest.mark.asyncio
async def test_a_later_message_still_uses_the_private_team(monkeypatch, tmp_path):
    failed = {
        "plan_id": "plan-closed",
        "members": ["business", "insight"],
        "owner_member": "business",
        "status": "failed",
        "created_at": bootstrap._now_ms(),
        "organization_id": ORG,
    }
    calls = []

    async def _saved(*_args, **_kwargs):
        return None

    async def _private(*args, **_kwargs):
        calls.append(args[1])
        from openjiuwen.core.foundation.tool import ToolOutput

        return ToolOutput(success=True, data={"private": True})

    monkeypatch.setattr(bootstrap, "saved_organization_instance", _saved)
    monkeypatch.setattr(group_service, "group_organization_plans", lambda _group_id: [failed])
    monkeypatch.setattr(group_service, "read_group", lambda _group_id: None)
    tool, history = _run_team(tmp_path)
    monkeypatch.setattr(tool, "_run", _private)
    history.write_text(
        json.dumps(
            {
                "client_message_id": "solo-later",
                "sender": "user",
                "mentions": ["insight"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = await tool.invoke({"run_id": "solo-later", "task": "单独做一份研究"})
    assert result.data["private"] is True
    assert calls == ["单独做一份研究"]


@pytest.mark.asyncio
async def test_join_wait_holds_while_the_proxy_turn_is_active(monkeypatch):
    monkeypatch.setattr(bootstrap, "MEMBER_JOIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(bootstrap, "RESULT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(bootstrap, "_JOIN_POLL_SECONDS", 0.01)
    state = {"active": True, "bound": False}

    async def _backend(*_args, **_kwargs):
        if state["bound"]:
            return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id=ORG))
        return None

    async def _active(_session_id, _member_name):
        return state["active"]

    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    monkeypatch.setattr(bootstrap, "_proxy_turn_active", _active)

    async def _finish_turn():
        await asyncio.sleep(0.12)
        state["active"] = False

    started = time.monotonic()
    waiter = asyncio.create_task(_finish_turn())
    missing = await bootstrap._wait_for_members(
        SimpleNamespace(),
        session_id=SESSION,
        organization_id=ORG,
        members=["insight"],
    )
    await waiter
    assert missing == ["insight"]
    assert time.monotonic() - started >= 0.12

    state["active"] = True

    async def _bind():
        await asyncio.sleep(0.12)
        state["bound"] = True

    waiter = asyncio.create_task(_bind())
    missing = await bootstrap._wait_for_members(
        SimpleNamespace(),
        session_id=SESSION,
        organization_id=ORG,
        members=["insight"],
    )
    await waiter
    assert missing == []


@pytest.mark.asyncio
async def test_join_wait_fails_after_the_proxy_turn_ends(monkeypatch):
    monkeypatch.setattr(bootstrap, "MEMBER_JOIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(bootstrap, "RESULT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(bootstrap, "_JOIN_POLL_SECONDS", 0.01)

    async def _backend(*_args, **_kwargs):
        return None

    async def _active(_session_id, _member_name):
        return False

    monkeypatch.setattr(bootstrap, "_live_backend", _backend)
    monkeypatch.setattr(bootstrap, "_proxy_turn_active", _active)
    started = time.monotonic()
    missing = await bootstrap._wait_for_members(
        SimpleNamespace(),
        session_id=SESSION,
        organization_id=ORG,
        members=["insight"],
    )
    elapsed = time.monotonic() - started
    assert missing == ["insight"]
    assert elapsed < 0.4


@pytest.mark.asyncio
async def test_instance_followup_reuses_the_organization_session(monkeypatch):
    _MemoryGroup(monkeypatch)
    launched = []
    turns = []
    posts = []
    team_id = bootstrap.instance_team_id(SESSION, "insight")

    class _Builder:
        def __init__(self):
            self.names = []

        def remove_section(self, name):
            self.names = [item for item in self.names if item != name]

        def add_section(self, section):
            self.names.append(section.name)

    class _Harness:
        def __init__(self):
            self.tools = []
            self.removed = []
            self.system_prompt_builder = _Builder()

        def add_tool(self, tool):
            self.tools.append(tool.key)

        def remove_tool(self, name):
            self.removed.append(name)

    harness = _Harness()
    agent = SimpleNamespace(harness=harness)

    class _Pool:
        async def get(self, _team_id):
            return SimpleNamespace(agent=agent)

    class _Runtime:
        def __init__(self):
            self.pool = _Pool()

        async def run_organization_turn(self, **kwargs):
            turns.append(kwargs)
            key = bootstrap._digest(
                "group-instance-followup", SESSION, "insight", "follow-1"
            )
            group_chat._results[key].set_result("沿用上一轮子任务")
            return True

    async def _ensure(**kwargs):
        launched.append(kwargs)
        return team_id

    async def _post(*args, **kwargs):
        posts.append((args, kwargs))
        return SimpleNamespace(message=SimpleNamespace(message_id="msg-follow"))

    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ensure)
    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (_Runtime(), object(), object()))
    output = await bootstrap.run_instance_followup(
        backend=SimpleNamespace(member_name="insight", append_group_message=_post),
        session_id=SESSION,
        run_id="follow-1",
        task="举个出域例子",
    )
    worker = bootstrap._followup_runs[(SESSION, "insight")]
    await worker

    assert output.data["resumed_instance"] is True
    assert output.data["instance_team_id"] == team_id
    assert launched[0]["enable_organization"] is False
    assert launched[0]["session_id"] == SESSION
    assert "already in this team" in launched[0]["leader_prompt_suffix"]
    assert turns[0]["team_name"] == team_id
    assert turns[0]["session_id"] == SESSION
    assert "举个出域例子" in turns[0]["inputs"]["query"]
    assert "existing conversation" in turns[0]["inputs"]["query"]
    assert posts[0][0][1] == "沿用上一轮子任务"
    assert harness.removed == ["complete_team_run"]


@pytest.mark.asyncio
async def test_followup_after_an_organization_opens_a_team_card(monkeypatch):
    _MemoryGroup(monkeypatch)
    team_id = bootstrap.instance_team_id(SESSION, "insight")
    seen = []

    class _Store:
        def __init__(self, session_id):
            self.session_id = session_id

        async def create(self, members, task, token, *, defer_card=False):
            seen.append(("create", members, task, defer_card))
            return [{
                "execution_id": "exec-follow",
                "status": "queued",
                "presentation": "message",
                "member_name": members[0],
            }]

        async def update(self, execution_id, **changes):
            seen.append(("update", execution_id, changes))
            return {
                "execution_id": execution_id,
                "status": changes.get("status", "running"),
                "presentation": "message",
                "member_name": "insight",
                **changes,
            }

        async def begin_task(self, execution_id, member_name):
            seen.append(("begin", execution_id, member_name))
            return {
                "execution_id": execution_id,
                "status": "running",
                "presentation": "card",
                "member_name": member_name,
            }

        async def task_event(self, execution_id, payload):
            seen.append(("task", execution_id, payload["event"]["member_id"]))

        async def finish(self, execution_id, backend, result, attachments):
            seen.append(("finish", execution_id, result, attachments))
            return {"execution_id": execution_id, "status": "completed"}

        def get(self, execution_id):
            return {"execution_id": execution_id, "status": "running"}

    class _Runtime:
        def __init__(self):
            self.pool = SimpleNamespace(get=self._get)

        async def _get(self, _team_id):
            return None

        async def run_organization_turn(self, **kwargs):
            await kwargs["on_payload"]({
                "event_type": "team.task",
                "event": {
                    "task_id": "child-1",
                    "status": "in_progress",
                    "title": "检索质量",
                    "member_id": "trend_analyst",
                },
            })
            key = bootstrap._digest(
                "group-instance-followup", SESSION, "insight", "follow-card"
            )
            group_chat._results[key].set_result({
                "result": "团队结论",
                "attachments": [],
            })
            return True

    async def _ensure(**_kwargs):
        return team_id

    async def _post(*_args, **_kwargs):
        raise AssertionError("the card finish publishes the group result")

    monkeypatch.setattr(group_execution, "GroupExecutions", _Store)
    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ensure)
    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (_Runtime(), object(), object()))
    output = await bootstrap.run_instance_followup(
        backend=SimpleNamespace(member_name="insight", append_group_message=_post),
        session_id=SESSION,
        run_id="follow-card",
        task="交给专家团评估检索质量",
    )
    await bootstrap._followup_runs[(SESSION, "insight")]

    assert output.data["resumed_instance"] is True
    assert seen[0][0] == "create"
    assert seen[0][3] is True
    assert ("update", "exec-follow", {
        "execution_kind": "team",
        "team_id": team_id,
        "background": True,
    }) in seen
    assert ("begin", "exec-follow", "insight") in seen
    assert ("task", "exec-follow", "trend_analyst") in seen
    assert ("finish", "exec-follow", "团队结论", []) in seen


@pytest.mark.asyncio
async def test_followup_card_copies_child_tasks_from_the_team_monitor(monkeypatch):
    _MemoryGroup(monkeypatch)
    team_id = bootstrap.instance_team_id(SESSION, "insight")
    seen = []

    class _Store:
        def __init__(self, session_id):
            self.session_id = session_id

        async def create(self, members, task, token, *, defer_card=False):
            return [{
                "execution_id": "exec-monitor",
                "status": "queued",
                "presentation": "message",
                "member_name": members[0],
            }]

        async def update(self, execution_id, **changes):
            return {"execution_id": execution_id, "status": "running", "member_name": "insight", **changes}

        async def begin_task(self, execution_id, member_name):
            return {"execution_id": execution_id, "status": "running", "presentation": "card", "member_name": member_name}

        async def task_event(self, execution_id, payload):
            seen.append((execution_id, payload["event"]["member_id"], payload["event"]["task_id"]))

        async def finish(self, execution_id, backend, result, attachments):
            seen.append(("finish", execution_id, result))
            return {"execution_id": execution_id, "status": "completed"}

        def get(self, execution_id):
            return {"execution_id": execution_id, "status": "running"}

    class _Monitor:
        def __init__(self):
            self.stopped = False

        async def events(self):
            yield {
                "event_type": "team.member",
                "event": {"member_id": "trend_analyst", "type": "team.member.status_changed"},
            }
            yield {
                "event_type": "team.task",
                "event": {
                    "task_id": "child-1",
                    "status": "in_progress",
                    "title": "检索质量从哪来",
                    "member_id": "trend_analyst",
                },
            }
            while not self.stopped:
                await asyncio.sleep(0.01)

        async def stop(self):
            self.stopped = True

    class _Runtime:
        def __init__(self):
            self.pool = SimpleNamespace(get=self._get)

        async def _get(self, _team_id):
            return None

        async def run_organization_turn(self, **kwargs):
            await kwargs["on_activated"]()
            await asyncio.sleep(0.05)
            key = bootstrap._digest(
                "group-instance-followup", SESSION, "insight", "follow-monitor"
            )
            group_chat._results[key].set_result({"result": "团队结论", "attachments": []})
            return True

    async def _ensure(**_kwargs):
        return team_id

    async def _post(*_args, **_kwargs):
        raise AssertionError("the card finish publishes the group result")

    async def _start(session_id, execution_id, watched_team_id):
        assert session_id == SESSION
        assert execution_id == "exec-monitor"
        assert watched_team_id == team_id
        monitor = _Monitor()

        async def _watch():
            async for event in monitor.events():
                await bootstrap._record_followup_task(session_id, execution_id, event)

        return monitor, asyncio.create_task(_watch())

    monkeypatch.setattr(group_execution, "GroupExecutions", _Store)
    monkeypatch.setattr(bootstrap, "ensure_member_instance", _ensure)
    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (_Runtime(), object(), object()))
    monkeypatch.setattr(bootstrap, "_start_followup_task_monitor", _start)
    output = await bootstrap.run_instance_followup(
        backend=SimpleNamespace(member_name="insight", append_group_message=_post),
        session_id=SESSION,
        run_id="follow-monitor",
        task="请团里分工评估检索质量",
    )
    await bootstrap._followup_runs[(SESSION, "insight")]

    assert output.data["resumed_instance"] is True
    assert ("exec-monitor", "trend_analyst", "child-1") in seen
    assert seen[-1] == ("finish", "exec-monitor", "团队结论")


def test_only_the_owner_can_reply_during_a_multi_mention(monkeypatch):
    now = bootstrap._now_ms()
    plan = {
        "plan_id": "round-2",
        "status": "running",
        "members": ["business", "insight"],
        "owner_member": "business",
        "created_at": now,
    }
    monkeypatch.setattr(group_service, "group_organization_plans", lambda _sid: [plan])
    monkeypatch.setattr(group_service, "update_group_organization_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(
        group_service,
        "group_organization_teams",
        lambda _sid: [
            {"team_id": "org-expert-business", "role": "owner", "member_name": "business"},
            {"team_id": "org-expert-insight", "role": "member", "member_name": "insight"},
        ],
    )
    assert bootstrap.group_reply_block_reason(SESSION, "insight")
    assert bootstrap.group_reply_block_reason(SESSION, "business") is None
    assert bootstrap.group_reply_block_reason(
        SESSION, "team-leader", team_id="org-expert-insight"
    )
    assert (
        bootstrap.group_reply_block_reason(
            SESSION, "team-leader", team_id="org-expert-business"
        )
        is None
    )
    plan["status"] = "done"
    assert bootstrap.group_reply_block_reason(SESSION, "insight") is None


@pytest.mark.asyncio
async def test_member_instance_loses_group_reply_tools(monkeypatch):
    removed = []
    sections = []

    class _Builder:
        def remove_section(self, name):
            sections.append(("remove", name))

        def add_section(self, section):
            sections.append(("add", section.name))

    class _Harness:
        system_prompt_builder = _Builder()

        def remove_tool(self, name):
            removed.append(name)

    class _Pool:
        async def get(self, _team_id):
            return SimpleNamespace(
                current_session_id=SESSION, agent=SimpleNamespace(harness=_Harness())
            )

    runtime = SimpleNamespace(pool=_Pool())
    await bootstrap._set_member_instance_silence(
        runtime, "org-expert-insight", SESSION, silent=True
    )
    assert removed == ["group_send_message", "send_file"]
    assert ("add", "swarm_org_member_silence") in sections

    removed.clear()
    await bootstrap._set_member_instance_silence(
        runtime, "org-expert-insight", SESSION, silent=False
    )
    assert removed == []
    assert ("remove", "swarm_org_member_silence") in sections
