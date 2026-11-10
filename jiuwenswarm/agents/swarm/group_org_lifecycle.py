"""Keep private-team work attached to its live SwarmChat organization task."""

import json
import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element
from openjiuwen.agent_teams.rails.team_context import get_team_backend
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.deep_agent_spec import RailSpec

logger = logging.getLogger(__name__)
SECTION = "swarm_org_lifecycle"
POLICY = "swarm.group_org_lifecycle"
WORK_TOOLS = frozenset({"create_task", "claim_task", "swarmflow"})
CHECKED_TOOLS = WORK_TOOLS | {"org_create_task", "org_review_task", "complete_team_run"}


def _accepted(row: dict) -> bool:
    return row["status"] == "COMPLETED" and (row.get("review") or {}).get("review_status") == "ACCEPTED"


def _swarmflow_control(name: str, resume_id: Any, action: Any) -> bool:
    """Allow pause or stop cleanup without reading organization state."""
    if name != "swarmflow" or not isinstance(resume_id, str):
        return False
    if not resume_id.strip() or not isinstance(action, str):
        return False
    return action.strip() in {"pause", "stop"}


def _needs_repair(row: dict, repaired: set) -> bool:
    """An original child still needs a linked repair."""
    if row.get("repairs_task_id") or row["task_id"] in repaired:
        return False
    if row["status"] == "FAILED":
        return True
    review = (row.get("review") or {}).get("review_status")
    return review in {"REJECTED", "NEEDS_REVISION"}


def _covered(rows: list[dict]) -> bool:
    repaired = {row["repairs_task_id"] for row in rows if row.get("repairs_task_id") and _accepted(row)}
    return bool(rows) and all(
        _accepted(row) or row["task_id"] in repaired
        or (row.get("repairs_task_id") in repaired and (
            row["status"] == "FAILED"
            or (row.get("review") or {}).get("review_status") in {"REJECTED", "NEEDS_REVISION"}
        ))
        for row in rows
    )


class GroupOrganizationLifecycleRail(DeepAgentRail):
    """Apply only to an instance bound to a live multi-mention group plan."""

    def __init__(self, backend: Any, session_id: str, member_name: str):
        super().__init__()
        self.backend = backend
        self.session_id = session_id
        self.member_name = member_name

    async def _round(self) -> tuple[dict, Any, Any, list[dict]] | None:
        from jiuwenswarm.agents.swarm.group_org_bootstrap import (
            _root_task_id, organization_id_for_plan,
        )
        from jiuwenswarm.server.runtime.group_service import group_organization_plans

        pool = getattr(self.backend, "org_task_manager", None)
        if pool is None:
            return None
        plan = None
        for item in group_organization_plans(self.session_id):
            if item.get("status") not in {"pending", "running"}:
                continue
            if self.member_name not in item.get("members", []):
                continue
            if organization_id_for_plan(item, self.session_id) != pool.organization_id:
                continue
            plan = item
            break
        if plan is None:
            return None
        root = await pool.get_task(_root_task_id(plan["plan_id"]))
        if root is None:
            return None
        rows = await pool.list_child_task_views(parent_task_id=root.task_id)
        return plan, pool, root, rows

    async def before_model_call(self, ctx: Any) -> None:
        builder = getattr(ctx.agent, "system_prompt_builder", None)
        if builder is None:
            return
        builder.remove_section(SECTION)
        # A transient status-read failure must not disable the normal org tools.
        try:
            state = await self._round()
        except Exception:
            logger.warning("organization lifecycle snapshot unavailable session=%s", self.session_id, exc_info=True)
            return
        if state is None:
            return
        plan, _pool, root, rows = state
        fields = (
            "task_id", "status", "required_capabilities", "assignment", "review", "repairs_task_id",
        )
        snapshot = []
        for row in rows:
            snapshot.append({key: row.get(key) for key in fields})
        content = (
            "Current organization task state (not the private team's task board): "
            + json.dumps({"root_task_id": root.task_id, "root_status": str(root.status), "children": snapshot},
                         ensure_ascii=False)
            + f". Your child requires group-member:{self.member_name}. "
            "Before private-team work, call org_claim_task on your OPEN child, then "
            "org_update_task(action='start'). Creating the child does not claim it; "
            "the creator team is not woken by its own task-created event. "
            "When your private work is done, submit its output with org_update_task(action='complete') "
            "using the organization child ID. Private create_task/claim_task IDs are never org_* IDs. "
        )
        if plan.get("owner_member") == self.member_name:
            content += (
                "Create expert tasks in dependency order; downstream tasks may be created after upstream outputs "
                "are ready. Before final aggregation, check the full request and every mentioned expert's work, "
                "including tasks not yet created. Accepted existing children alone do not mean the "
                "request is complete. "
                "Review COMPLETED children whose review is PENDING using org_review_task and their "
                "organization IDs. Read their outputs with org_view_child_tasks and explicitly choose "
                "ACCEPTED, NEEDS_REVISION, or REJECTED based on the requirements; private-team review does not count. "
                "For a FAILED, REJECTED, or NEEDS_REVISION child, use "
                "org_create_task(repairs_task_id=<original child ID>) "
                "and keep its required_capabilities. A v2/v3 name alone never repairs the original. "
                "Claim and start your own repair before resuming private work. Do not repeatedly expand "
                "verification work. When all sources are accepted (including accepted repairs), complete "
                "the selected aggregation workflow and then call complete_team_run. "
            )
        builder.add_section(PromptSection(name=SECTION, content={"en": content, "cn": content}, priority=99))

    async def before_tool_call(self, ctx: Any) -> None:
        name = ctx.inputs.tool_name
        if name not in CHECKED_TOOLS:
            return
        from jiuwenswarm.agents.swarm.group_execution import _reject_tool

        arguments = getattr(ctx.inputs.tool_call, "arguments", None)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return  # Let the tool report malformed arguments.
        if not isinstance(arguments, dict):
            return
        resume_id = arguments.get("resume_id")
        action = arguments.get("action")
        # Match SwarmflowTool's control branch. Cleanup must remain available
        # after task failure, even when the organization database cannot be read.
        if _swarmflow_control(name, resume_id, action):
            return
        try:
            state = await self._round()
            if state is None:
                return
            plan, pool, root, rows = state
            error = None
            if name in WORK_TOOLS:
                error = await self._work_error(plan, pool, root, rows)
            elif name == "org_create_task":
                error = self._create_error(arguments, plan, root, rows)
            elif name == "org_review_task":
                task = await pool.get_task(str(arguments.get("task_id") or ""))
                if task is None or task.root_task_id != root.task_id or not task.parent_task_id:
                    error = (
                        "org_review_task requires an organization child ID from org_view_child_tasks. "
                        "A private-team task ID cannot be reviewed here."
                    )
            elif name == "complete_team_run" and str(root.status) != "COMPLETED":
                error = (
                    "The organization root is not complete. Submit your organization child, review all sources, "
                    "and finish the selected HIERARCHICAL or SUMMARY_TEAM workflow before complete_team_run."
                )
        except Exception:
            logger.warning("organization lifecycle validation unavailable session=%s", self.session_id, exc_info=True)
            _reject_tool(ctx, "Organization state could not be read. Retry org_view_tasks before this operation.")
            return
        if error:
            _reject_tool(ctx, error)

    async def _work_error(self, plan: dict, pool: Any, root: Any, rows: list[dict]) -> str | None:
        owner = plan.get("owner_member") == self.member_name
        if owner and _covered(rows) and all(
            any(
                row.get("required_capabilities") == [f"group-member:{member}"] for row in rows
            ) for member in plan["members"]
        ):
            return None  # The Owner may delegate final integration to its own team.
        capability = f"group-member:{self.member_name}"
        own = [row for row in rows if row.get("required_capabilities") == [capability]]
        team_id = self.backend.team_name
        if any(row["status"] == "IN_PROGRESS" and (row.get("assignment") or {}).get("team_id") == team_id
               for row in own):
            return None
        # Direct children still define the final-integration exception above.
        # Only work authorization falls back to descendants, using Core's uncapped
        # child query so deeper or larger trees are not silently truncated.
        own_ids = [row["task_id"] for row in own]
        pending = [row["task_id"] for row in rows]
        visited = {root.task_id}
        while pending:
            parent_id = pending.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            for task in await pool.list_child_tasks(parent_task_id=parent_id):
                if task.root_task_id != root.task_id:
                    continue
                pending.append(task.task_id)
                if task.required_capabilities != [capability]:
                    continue
                own_ids.append(task.task_id)
                if str(task.status) == "IN_PROGRESS" and task.assignment.team_id == team_id:
                    return None
        ids = ", ".join(own_ids) or "not created yet"
        return (
            f"Private-team work requires your own IN_PROGRESS organization child under {root.task_id}; "
            f"your child IDs: {ids}. Create the child if missing, call org_claim_task if OPEN, then "
            "org_update_task(action='start'). Claiming the root does not claim your child. "
            "If the child failed or needs revision, have the Owner create a linked repair with "
            "repairs_task_id=<original child ID>, then claim and start that repair."
        )

    @staticmethod
    def _create_error(arguments: dict, plan: dict, root: Any, rows: list[dict]) -> str | None:
        # SwarmChat owns the direct per-expert decomposition; Core still owns
        # nested task, repair eligibility, authorization and atomic transitions.
        if not arguments.get("parent_task_id"):
            return (
                f"This request already has root {root.task_id}. "
                "Create expert children under that root, not another root."
            )
        capabilities = arguments.get("required_capabilities")
        allowed = [f"group-member:{member}" for member in plan["members"]]
        if not isinstance(capabilities, list) or len(capabilities) != 1 or capabilities[0] not in allowed:
            return (
                "Each expert child must require exactly one capability from this organization: "
                + json.dumps(allowed)
                + ". A team merely present in the public group has not joined this organization. "
                "Use the appropriate participating expert; do not create an unclaimable task."
            )
        if arguments.get("parent_task_id") != root.task_id:
            return None
        siblings = [row for row in rows if row.get("required_capabilities") == capabilities]
        if any(row["task_id"] == arguments.get("task_id") for row in siblings):
            return None  # Preserve Core's idempotent create semantics.
        repaired = {row["repairs_task_id"] for row in siblings if row.get("repairs_task_id") and _accepted(row)}
        unresolved = [row["task_id"] for row in siblings if _needs_repair(row, repaired)]
        if unresolved and arguments.get("repairs_task_id") not in unresolved:
            return (
                "This expert has unresolved failed/rejected organization children: " + ", ".join(unresolved)
                + ". Do not create an unrelated v2/v3 sibling. "
                + f"Create a repair with repairs_task_id='{unresolved[0]}' (or another listed original ID) "
                "and the same capability."
            )
        return None


async def install_organization_lifecycle(agent: Any, *, session_id: str, member_name: str) -> None:
    """Install once on this native instance, including a resumed/rebuilt harness."""
    # Persist the declaration for cold recovery of an instance created before
    # this policy was installed; live handles stay out of the serialized spec.
    agents = getattr(getattr(agent, "spec", None), "agents", {})
    leader = agents.get("leader")
    if leader is not None and not any(rail.type == POLICY for rail in leader.rails or []):
        leader.rails = [*(leader.rails or []), organization_lifecycle_spec(member_name)]
    harness = getattr(agent, "harness", None)
    register = getattr(harness, "register_rail", None)
    find = getattr(harness, "find_rails", None)
    if not callable(register) or not callable(find):
        return
    if not find(GroupOrganizationLifecycleRail):
        await register(GroupOrganizationLifecycleRail(getattr(agent, "team_backend", None), session_id, member_name))


def organization_lifecycle_spec(member_name: str) -> RailSpec:
    return RailSpec(type=POLICY, params={"member_name": member_name})


@harness_element(
    kind=ElementKind.RAIL,
    name=POLICY,
    description="Keep SwarmChat organization task lifecycle consistent.",
)
def build_organization_lifecycle(params: dict, context: Any) -> GroupOrganizationLifecycleRail:
    return GroupOrganizationLifecycleRail(get_team_backend(context), context.session_id, params["member_name"])
