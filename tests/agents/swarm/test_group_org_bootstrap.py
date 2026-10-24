"""Swarm group expert-team Organization coordination."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.swarm import group_chat
from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from jiuwenswarm.server.runtime import group_service
from openjiuwen.agent_teams.organization.schema import OrgTaskStatus

SESSION = "web_group"
ORG = bootstrap.organization_id_for_session(SESSION)
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
    mentions = ["business", "writer", "insight"]
    plan = bootstrap.record_organization_plan(
        SESSION,
        mentions=mentions,
        body="写可行性报告",
        plan_id="message-1",
    )
    assert mentions == ["business", "writer", "insight"]
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


def test_existing_owner_must_be_mentioned(monkeypatch):
    _MemoryGroup(
        monkeypatch,
        organization_teams=[
            {
                "member_name": "business",
                "team_id": "owner-team",
                "role": "owner",
            }
        ],
    )
    assert (
        bootstrap.record_organization_plan(
            SESSION,
            mentions=["insight", "writer"],
            body="x",
            plan_id="message-2",
        )
        is None
    )


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
        started_at=123,
    )

    assert scheduled[0]["team_id"] == "owner-team"
    assert "org_view_tasks first" in scheduled[0]["prompt"]
    assert "do not duplicate the root" in scheduled[0]["prompt"]


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
async def test_hierarchical_summary_row_names_the_owner_team(monkeypatch):
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
    )
    child = SimpleNamespace(
        task_id="child-business",
        parent_task_id=root_id,
        title="用户价值及第一版功能范围",
        status="COMPLETED",
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
    rows, details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert list(rows) == ["child-business", root_id]
    assert details[root_id]["title"] == "汇总"
    assert details[root_id]["member_id"] == "商业策划专家团"
    assert rows[root_id] == "completed"

    root.aggregation.mode = "SUMMARY_TEAM"
    plain_rows, _plain_details = await bootstrap._organization_rows(
        SESSION, plan, _Pool([root, child]), fail_unfinished=False
    )
    assert root_id not in plain_rows


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
            released.append("release")

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
    assert released == []
    assert group.state["organization_id"] == ORG
    assert group.state["organization_teams"][0]["team_id"] == "org-expert-business"


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
