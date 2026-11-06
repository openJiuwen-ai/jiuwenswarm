"""Exercise organization lifecycle guards against the real Core task database."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openjiuwen.agent_teams.organization.schema import OrgTaskCreator
from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager
from openjiuwen.agent_teams.organization.tools import (
    OrgClaimTaskTool, OrgCreateTaskTool, OrgReviewTaskTool, OrgUpdateTaskTool,
)
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.workflow.tool_swarmflow import SwarmflowTool
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from jiuwenswarm.agents.swarm.group_org_lifecycle import GroupOrganizationLifecycleRail
from jiuwenswarm.server.runtime import group_service


def _context(name, arguments):
    return SimpleNamespace(
        inputs=SimpleNamespace(tool_name=name, tool_call=SimpleNamespace(id="call-1", arguments=json.dumps(arguments))),
        extra={},
    )


async def _call(rail, tool, arguments):
    ctx = _context(tool.card.name, arguments)
    await rail.before_tool_call(ctx)
    if ctx.extra.get("_skip_tool"):
        return SimpleNamespace(success=False, error=ctx.inputs.tool_msg.content)
    return await tool.invoke(arguments)


@pytest_asyncio.fixture
async def team_db(tmp_path):
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def organization(team_db, monkeypatch):
    plan = {
        "plan_id": "request-1", "organization_id": "org-1", "status": "running",
        "owner_member": "finance", "members": ["finance", "ops", "brand"],
    }
    monkeypatch.setattr(group_service, "group_organization_plans", lambda _: [plan])
    pool = OrgTaskManager(db=team_db, organization_id="org-1", session_id="group-1")
    await pool.ensure_organization(metadata={"owner_team_id": "finance"})
    for member in plan["members"]:
        await pool.register_leader(team_id=member, leader_id="leader", capabilities=[f"group-member:{member}", "group-coordination"])
    root_id = bootstrap._root_task_id(plan["plan_id"])
    result = await pool.create_task(
        task_id=root_id, title="Opening plan", description="Coordinate three experts",
        required_capabilities=["group-coordination"],
        created_by=OrgTaskCreator(creator_id="user", organization_id="org-1"),
    )
    assert result.ok
    assert (await pool.claim_task(task_id=root_id, team_id="finance")).ok
    assert (await pool.set_root_aggregation_mode(task_id=root_id, team_id="finance", leader_id="leader", aggregation_mode="HIERARCHICAL")).ok
    assert (await pool.start_task(task_id=root_id, team_id="finance")).ok
    agent = SimpleNamespace(team_backend=SimpleNamespace(team_name="finance", org_task_manager=pool))
    rail = GroupOrganizationLifecycleRail(agent.team_backend, "group-1", "finance")
    return SimpleNamespace(pool=pool, rail=rail, plan=plan, root_id=root_id, agent=agent)


def _child(state, member, task_id=None, **extra):
    return {
        "task_id": task_id or f"ot-{member}", "parent_task_id": state.root_id,
        "title": member, "description": f"Deliver {member} analysis",
        "required_capabilities": [f"group-member:{member}"], **extra,
    }


async def _create_children(state):
    tool = OrgCreateTaskTool(state.pool, "finance", "leader")
    for member in state.plan["members"]:
        result = await _call(state.rail, tool, _child(state, member))
        assert result.success, result.error


async def _complete(state, member, task_id=None):
    task_id = task_id or f"ot-{member}"
    claim = await OrgClaimTaskTool(state.pool, member, "leader").invoke({"task_id": task_id})
    assert claim.success, claim.error
    update = OrgUpdateTaskTool(state.pool, member, "leader")
    assert (await update.invoke({"task_id": task_id, "action": "start"})).success
    assert (await update.invoke({"task_id": task_id, "action": "complete", "output_abstract": "Prepared deliverable"})).success


@pytest.mark.asyncio
async def test_owner_cannot_skip_claim_and_start_before_private_work(organization):
    state = organization
    await _create_children(state)
    private_tool = SimpleNamespace(card=SimpleNamespace(name="create_task"), invoke=AsyncMock(return_value=SimpleNamespace(success=True)))
    result = await _call(state.rail, private_tool, {"tasks": [{"task_id": "fin-budget"}]})
    assert not result.success and "org_claim_task" in result.error
    private_tool.invoke.assert_not_awaited()
    assert (await state.pool.get_task("ot-finance")).assignment.team_id is None

    claim = await _call(state.rail, OrgClaimTaskTool(state.pool, "finance", "leader"), {"task_id": "ot-finance"})
    assert claim.success
    assert not (await _call(state.rail, private_tool, {"tasks": []})).success
    start = await _call(state.rail, OrgUpdateTaskTool(state.pool, "finance", "leader"), {"task_id": "ot-finance", "action": "start"})
    assert start.success
    assert (await _call(state.rail, private_tool, {"tasks": []})).success
    private_tool.invoke.assert_awaited_once()
    task = await state.pool.get_task("ot-finance")
    assert str(task.status) == "IN_PROGRESS" and task.assignment.team_id == "finance"
    await state.pool.advance_unclaimed_tasks(now=task.created_at + 600_000)
    assert str((await state.pool.get_task("ot-finance")).status) == "IN_PROGRESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["create_task", "claim_task", "swarmflow"])
async def test_owner_can_start_before_downstream_expert_tasks_exist(organization, tool_name):
    state = organization
    assert (await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"), _child(state, "finance"))).success
    ctx = _context(tool_name, {})
    await state.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "org_claim_task" in ctx.inputs.tool_msg.content
    assert (await state.pool.claim_task(task_id="ot-finance", team_id="finance")).ok
    ctx = _context(tool_name, {})
    await state.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    assert (await state.pool.start_task(task_id="ot-finance", team_id="finance")).ok
    ctx = _context(tool_name, {})
    await state.rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")
    assert await state.pool.get_task("ot-ops") is None
    assert await state.pool.get_task("ot-brand") is None
    assert (await OrgUpdateTaskTool(state.pool, "finance", "leader").invoke(
        {"task_id": "ot-finance", "action": "complete", "output_abstract": "Upstream analysis"},
    )).success
    result = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"), _child(state, "brand"))
    assert result.success, result.error
    assert (await state.pool.get_task("ot-brand")).parent_task_id == state.root_id


@pytest.mark.asyncio
async def test_accepted_current_stage_does_not_enable_final_integration_with_missing_experts(organization):
    state = organization
    assert (await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"), _child(state, "finance"))).success
    await _complete(state, "finance")
    assert (await OrgReviewTaskTool(state.pool, "finance", "leader").invoke(
        {"task_id": "ot-finance", "review_status": "ACCEPTED"},
    )).success
    ctx = _context("create_task", {})
    await state.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    builder = SystemPromptBuilder()
    await state.rail.before_model_call(SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder)))
    assert "including tasks not yet created" in builder.build()


@pytest.mark.asyncio
async def test_pending_reviews_are_visible_and_private_ids_cannot_be_reviewed(organization):
    state = organization
    await _create_children(state)
    await _complete(state, "ops")
    builder = SystemPromptBuilder()
    await state.rail.before_model_call(SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder)))
    prompt = builder.build()
    assert "ot-ops" in prompt and "PENDING" in prompt and "org_review_task" in prompt
    assert (await state.pool.get_task_review("ot-ops")).review_status == "PENDING"
    review = OrgReviewTaskTool(state.pool, "finance", "leader")
    result = await _call(state.rail, review, {"task_id": "so-1", "review_status": "ACCEPTED"})
    assert not result.success and "private-team task ID" in result.error
    result = await _call(state.rail, review, {"task_id": "ot-ops", "review_status": "ACCEPTED"})
    assert result.success, result.error
    assert (await state.pool.get_task_review("ot-ops")).review_status == "ACCEPTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [["商业策划"], ["group-member:business"], [], ["group-member:finance", "budget"]])
async def test_unclaimable_child_never_reaches_database(organization, capabilities):
    state = organization
    result = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"),
                         _child(state, "business", required_capabilities=capabilities))
    assert not result.success and "this organization" in result.error
    assert await state.pool.get_task("ot-business") is None


@pytest.mark.asyncio
async def test_failed_child_requires_linked_repair_and_can_finish_root(organization):
    state = organization
    await _create_children(state)
    original = await state.pool.get_task("ot-finance")
    # Reproduce the exported log's five-minute notification plus three-minute timeout.
    await state.pool.advance_unclaimed_tasks(now=original.created_at + 301_000)
    await state.pool.advance_unclaimed_tasks(now=original.created_at + 482_000)
    assert (await state.pool.get_task("ot-finance")).failure_reason == "description_update_timeout"
    create = OrgCreateTaskTool(state.pool, "finance", "leader")
    result = await _call(state.rail, create, _child(state, "finance", "ot-finance-v2"))
    assert not result.success and "repairs_task_id='ot-finance'" in result.error
    assert await state.pool.get_task("ot-finance-v2") is None
    # Other unclaimed children expired too; repair each with the same explicit relation.
    for member in state.plan["members"]:
        repair_id = f"ot-{member}-repair"
        result = await _call(state.rail, create, _child(state, member, repair_id, repairs_task_id=f"ot-{member}"))
        assert result.success, result.error
        await _complete(state, member, repair_id)
        result = await _call(state.rail, OrgReviewTaskTool(state.pool, "finance", "leader"),
                             {"task_id": repair_id, "review_status": "ACCEPTED"})
        assert result.success, result.error
    ctx = _context("create_task", {"tasks": []})
    await state.rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")  # Final integration is still allowed.
    result = await _call(state.rail, OrgUpdateTaskTool(state.pool, "finance", "leader"),
                         {"task_id": state.root_id, "action": "complete", "output_abstract": "Final opening plan"})
    assert result.success, result.error
    assert str((await state.pool.get_task(state.root_id)).status) == "COMPLETED"


@pytest.mark.asyncio
async def test_premature_final_submission_returns_actionable_error(organization):
    ctx = _context("complete_team_run", {"result": "premature result"})
    await organization.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "root is not complete" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_member_claims_only_its_child_without_waiting_for_other_experts(organization):
    state = organization
    create = OrgCreateTaskTool(state.pool, "finance", "leader")
    assert (await _call(state.rail, create, _child(state, "ops"))).success
    member_agent = SimpleNamespace(team_backend=SimpleNamespace(team_name="ops", org_task_manager=state.pool))
    rail = GroupOrganizationLifecycleRail(member_agent.team_backend, "group-1", "ops")
    ctx = _context("create_task", {"tasks": []})
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    assert (await _call(rail, OrgClaimTaskTool(state.pool, "ops", "leader"), {"task_id": "ot-ops"})).success
    assert (await _call(rail, OrgUpdateTaskTool(state.pool, "ops", "leader"), {"task_id": "ot-ops", "action": "start"})).success
    ctx = _context("create_task", {"tasks": []})
    await rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["create_task", "claim_task", "swarmflow"])
@pytest.mark.parametrize("depth", [1, 2])
async def test_started_descendant_allows_private_work_after_direct_child_finishes(organization, tool_name, depth):
    state = organization
    await _create_children(state)
    assert (await state.pool.claim_task(task_id="ot-finance", team_id="finance")).ok
    assert (await state.pool.start_task(task_id="ot-finance", team_id="finance")).ok
    await _complete(state, "ops")
    assert (await OrgReviewTaskTool(state.pool, "finance", "leader").invoke(
        {"task_id": "ot-ops", "review_status": "ACCEPTED"},
    )).success
    rail = GroupOrganizationLifecycleRail(SimpleNamespace(team_name="ops", org_task_manager=state.pool), "group-1", "ops")
    parent_id = "ot-finance"
    for level in range(depth):
        task_id = f"ops-check-{level}"
        member = "ops" if level == depth - 1 else "finance"
        created = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"),
                              _child(state, member, task_id, parent_task_id=parent_id))
        assert created.success, created.error
        parent_id = task_id
        if level < depth - 1:
            assert (await state.pool.claim_task(task_id=task_id, team_id="finance")).ok
            assert (await state.pool.start_task(task_id=task_id, team_id="finance")).ok

    # Only the leaf belongs to ops, so the deeper case must traverse the whole path.
    # An OPEN/CLAIMED descendant is insufficient.
    private_tool = SimpleNamespace(card=SimpleNamespace(name=tool_name),
                                   invoke=AsyncMock(return_value=SimpleNamespace(success=True)))
    assert not (await _call(rail, private_tool, {})).success
    assert (await state.pool.claim_task(task_id=task_id, team_id="ops")).ok
    assert not (await _call(rail, private_tool, {})).success
    assert (await state.pool.start_task(task_id=task_id, team_id="ops")).ok
    result = await _call(rail, private_tool, {})
    assert result.success, result.error
    private_tool.invoke.assert_awaited_once()
    task = await state.pool.get_task(task_id)
    assert task.root_task_id == state.root_id and task.assignment.team_id == "ops"
    assert str(task.status) == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_owner_work_does_not_require_other_experts_to_have_direct_children(organization):
    state = organization
    create = OrgCreateTaskTool(state.pool, "finance", "leader")
    assert (await _call(state.rail, create, _child(state, "finance"))).success
    assert (await state.pool.claim_task(task_id="ot-finance", team_id="finance")).ok
    assert (await state.pool.start_task(task_id="ot-finance", team_id="finance")).ok
    for member in ("ops", "brand"):
        result = await _call(state.rail, create, _child(state, member, parent_task_id="ot-finance"))
        assert result.success, result.error
    ctx = _context("create_task", {})
    await state.rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
async def test_descendant_assigned_to_another_team_does_not_allow_private_work(organization):
    state = organization
    await _create_children(state)
    assert (await state.pool.claim_task(task_id="ot-finance", team_id="finance")).ok
    assert (await state.pool.start_task(task_id="ot-finance", team_id="finance")).ok
    await state.pool.register_leader(team_id="other-ops", leader_id="leader", capabilities=["group-member:ops"])
    created = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"),
                          _child(state, "ops", "ops-check", parent_task_id="ot-finance"))
    assert created.success, created.error
    assert (await state.pool.claim_task(task_id="ops-check", team_id="other-ops")).ok
    assert (await state.pool.start_task(task_id="ops-check", team_id="other-ops")).ok
    rail = GroupOrganizationLifecycleRail(SimpleNamespace(team_name="ops", org_task_manager=state.pool), "group-1", "ops")
    ctx = _context("create_task", {})
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]


@pytest.mark.asyncio
async def test_started_task_under_another_root_does_not_allow_private_work(organization):
    state = organization
    await _create_children(state)
    assert (await state.pool.create_task(
        task_id="other-root", title="Other request", description="Separate request",
        required_capabilities=["group-coordination"],
        created_by=OrgTaskCreator(creator_id="user", organization_id="org-1"),
    )).ok
    assert (await state.pool.claim_task(task_id="other-root", team_id="finance")).ok
    assert (await state.pool.set_root_aggregation_mode(task_id="other-root", team_id="finance", leader_id="leader",
                                                       aggregation_mode="HIERARCHICAL")).ok
    assert (await state.pool.start_task(task_id="other-root", team_id="finance")).ok
    created = await OrgCreateTaskTool(state.pool, "finance", "leader").invoke(
        _child(state, "ops", "other-ops-task", parent_task_id="other-root"),
    )
    assert created.success, created.error
    assert (await state.pool.claim_task(task_id="other-ops-task", team_id="ops")).ok
    assert (await state.pool.start_task(task_id="other-ops-task", team_id="ops")).ok
    rail = GroupOrganizationLifecycleRail(SimpleNamespace(team_name="ops", org_task_manager=state.pool), "group-1", "ops")
    ctx = _context("create_task", {})
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "stop"])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_workflow_cleanup_reaches_controller_after_child_failure(organization, monkeypatch, action, unavailable):
    state = organization
    await _create_children(state)
    assert (await state.pool.claim_task(task_id="ot-finance", team_id="finance")).ok
    assert (await state.pool.start_task(task_id="ot-finance", team_id="finance")).ok
    assert (await state.pool.fail_task(task_id="ot-finance", team_id="finance", failure_code="EXECUTION_FAILED",
                                      failure_reason="Stop remaining private work")).ok
    if unavailable:
        monkeypatch.setattr(state.pool, "get_task", AsyncMock(side_effect=OSError("database unavailable")))
    active_runs = {"running-workflow"}

    async def control(run_id):
        active_runs.remove(run_id)
        return True

    controller = SimpleNamespace(is_paused=lambda _: False, pause=AsyncMock(side_effect=control),
                                 stop=AsyncMock(side_effect=control))
    tool = SwarmflowTool(parent_agent=SimpleNamespace(background_task_controller=controller), messager=None,
                        team_name="finance", model_resolver=None)
    # Normalize controls the same way as the real tool, including surrounding whitespace.
    result = await _call(state.rail, tool, {"resume_id": " running-workflow ", "action": f" {action} "})
    assert result.success, result.error
    getattr(controller, action).assert_awaited_once_with("running-workflow")
    assert not active_runs
    if unavailable:
        state.pool.get_task.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"script": "start private work"},
    {"resume_id": "existing-workflow", "action": "resume"},
    {"resume_id": "existing-workflow"},
    {"action": "stop"},
    {"resume_id": " ", "action": "stop"},
])
async def test_workflow_launch_and_resume_still_require_started_child(organization, arguments):
    await _create_children(organization)
    ctx = _context("swarmflow", arguments)
    await organization.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "org_claim_task" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_descendant_read_failure_returns_retry_instructions(organization, monkeypatch):
    await _create_children(organization)
    monkeypatch.setattr(organization.pool, "list_child_tasks", AsyncMock(side_effect=OSError("database unavailable")))
    ctx = _context("create_task", {})
    await organization.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "Retry org_view_tasks" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
@pytest.mark.parametrize("review_status", ["REJECTED", "NEEDS_REVISION"])
async def test_review_revision_requires_link_but_retains_core_repair_guards(organization, review_status):
    state = organization
    await _create_children(state)
    await _complete(state, "finance")
    assert (await _call(state.rail, OrgReviewTaskTool(state.pool, "finance", "leader"),
                        {"task_id": "ot-finance", "review_status": review_status, "required_changes": ["Resolve budget conflict"]})).success
    create = OrgCreateTaskTool(state.pool, "finance", "leader")
    args = _child(state, "finance", "ot-finance-v2")
    assert not (await _call(state.rail, create, args)).success
    args["repairs_task_id"] = "ot-finance"
    assert (await _call(state.rail, create, args)).success
    duplicate = await _call(state.rail, create, {**args, "task_id": "ot-finance-v3"})
    assert not duplicate.success and "active repair" in duplicate.error
    assert await state.pool.get_task("ot-finance-v3") is None


@pytest.mark.asyncio
async def test_accepted_work_does_not_block_a_legitimate_additional_task(organization):
    state = organization
    await _create_children(state)
    await _complete(state, "finance")
    assert (await _call(state.rail, OrgReviewTaskTool(state.pool, "finance", "leader"),
                        {"task_id": "ot-finance", "review_status": "ACCEPTED"})).success
    result = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"),
                         _child(state, "finance", "additional-finance", description="Additional requested analysis"))
    assert result.success, result.error


@pytest.mark.asyncio
async def test_no_extra_root_is_created(organization):
    state = organization
    args = _child(state, "finance", "unrelated-root")
    args.pop("parent_task_id")
    result = await _call(state.rail, OrgCreateTaskTool(state.pool, "finance", "leader"), args)
    assert not result.success and state.root_id in result.error
    assert await state.pool.get_task("unrelated-root") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["no_plan", "finished", "different_org", "not_member", "detached"])
async def test_other_routes_and_finished_instance_followups_are_unaffected(organization, monkeypatch, scope):
    state = organization
    builder = SystemPromptBuilder()
    model_ctx = SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder))
    await state.rail.before_model_call(model_ctx)
    assert "Current organization task state" in builder.build()
    if scope == "no_plan":
        monkeypatch.setattr(group_service, "group_organization_plans", lambda _: [])
    elif scope == "finished":
        state.plan["status"] = "completed"
    elif scope == "different_org":
        state.plan["organization_id"] = "older-org"
    elif scope == "not_member":
        state.plan["members"] = ["ops", "brand"]
    else:
        state.agent.team_backend.org_task_manager = None
    for name in ["create_task", "org_create_task", "org_review_task", "complete_team_run"]:
        ctx = _context(name, {})
        await state.rail.before_tool_call(ctx)
        assert not ctx.extra.get("_skip_tool")
    await state.rail.before_model_call(model_ctx)
    assert "Current organization task state" not in builder.build()


@pytest.mark.asyncio
async def test_status_read_failure_does_not_block_recovery_tools(organization, monkeypatch):
    state = organization
    monkeypatch.setattr(state.pool, "list_child_task_views", AsyncMock(side_effect=OSError("database temporarily unavailable")))
    ctx = _context("create_task", {})
    await state.rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "Retry org_view_tasks" in ctx.inputs.tool_msg.content
    for name in ["org_view_tasks", "org_claim_task", "org_update_task", "org_view_child_tasks"]:
        ctx = _context(name, {})
        await state.rail.before_tool_call(ctx)
        assert not ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
async def test_proxy_launch_and_reuse_install_one_guard(monkeypatch):
    rails = []
    harness = SimpleNamespace(find_rails=lambda cls: [rail for rail in rails if isinstance(rail, cls)],
                              register_rail=AsyncMock(side_effect=lambda rail: rails.append(rail)))
    agent = SimpleNamespace(harness=harness)
    entry = SimpleNamespace(agent=agent, current_session_id="group-1")
    pool = SimpleNamespace(get=AsyncMock(side_effect=[None, entry, entry]))
    launcher = SimpleNamespace(launch=AsyncMock())
    arguments = dict(runtime=SimpleNamespace(pool=pool), launcher=launcher, session_id="group-1",
                     outer_team_id="outer", member={"member_name": "finance", "id": "finance"}, is_owner=True)
    await bootstrap.ensure_member_instance(**arguments)
    await bootstrap.ensure_member_instance(**arguments)
    launcher.launch.assert_awaited_once()
    harness.register_rail.assert_awaited_once()
    assert rails[0].member_name == "finance"


@pytest.mark.asyncio
async def test_direct_instance_launch_does_not_install_org_guard():
    harness = SimpleNamespace(find_rails=lambda _: [], register_rail=AsyncMock())
    entry = SimpleNamespace(agent=SimpleNamespace(harness=harness), current_session_id="group-1")
    pool = SimpleNamespace(get=AsyncMock(return_value=entry))
    await bootstrap.ensure_member_instance(
        runtime=SimpleNamespace(pool=pool), launcher=SimpleNamespace(launch=AsyncMock()),
        session_id="group-1", outer_team_id="outer", member={"member_name": "finance", "id": "finance"},
        is_owner=True, enable_organization=False,
    )
    harness.register_rail.assert_not_awaited()


@pytest.mark.asyncio
async def test_serialized_policy_rebuild_uses_current_backend_and_still_guards(organization):
    from openjiuwen.agent_teams.rails.team_context import TeamHandleKey
    from openjiuwen.harness.schema.deep_agent_spec import RailSpec
    from jiuwenswarm.agents.swarm.group_org_lifecycle import organization_lifecycle_spec
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    state = organization
    await _create_children(state)
    register_swarm_providers()
    saved = organization_lifecycle_spec("finance").model_dump_json()
    context = SimpleNamespace(session_id="group-1", extras={TeamHandleKey.TEAM_BACKEND: state.agent.team_backend})
    rebuilt = RailSpec.model_validate_json(saved).build(language="cn", context=context)
    assert isinstance(rebuilt, GroupOrganizationLifecycleRail)
    ctx = _context("create_task", {"tasks": []})
    await rebuilt.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "org_claim_task" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_existing_instance_records_serializable_policy_for_recovery():
    from jiuwenswarm.agents.swarm.group_org_lifecycle import install_organization_lifecycle, POLICY

    leader = SimpleNamespace(rails=[])
    agent = SimpleNamespace(spec=SimpleNamespace(agents={"leader": leader}))
    await install_organization_lifecycle(agent, session_id="group-1", member_name="finance")
    await install_organization_lifecycle(agent, session_id="group-1", member_name="finance")
    assert [rail.type for rail in leader.rails] == [POLICY]
    assert json.loads(leader.rails[0].model_dump_json())["params"] == {"member_name": "finance"}
