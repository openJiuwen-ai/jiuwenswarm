"""Group roster assembly and on-demand expert-team execution.

This is host logic, not another team scheduler. A proxy remains a teammate in
the public group; its private team runs with its own leader and session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from weakref import WeakValueDictionary

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element
from openjiuwen.agent_teams.rails.team_context import get_team_backend
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.team import (
    ExternalCliAgentSpec,
    ExternalCliMemberSpec,
    TeamMemberSpec,
    TeamRole,
)
from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput
from openjiuwen.core.runner import Runner
from openjiuwen.harness.resources import load_agent_template_package
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, RailSpec
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

RUN_TEAM = "swarm.group_run_team"
COMPLETE_RUN = "swarm.group_complete_team_run"
GROUP_MEMBER_TEMPLATE = "swarm.group_member_template"
# CLI kinds an expert template can name (runtime.provider_name codex / claudecode).
GROUP_CLI_KINDS = ("claude", "codex")
# Per-template fields that a kind-level launch config cannot carry.
CLI_MEMBER_ONLY_FIELDS = frozenset({"skills", "skill_conflict"})
# Hash length in team-runs file names; full 64-character hashes exceed Windows MAX_PATH.
RUN_PATH_HASH_LENGTH = 12
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_results: dict[str, asyncio.Future] = {}
_private_runs: dict[str, dict] = {}
# Unfinished background private-team runs one expert-team proxy may hold, queued runs included.
MAX_PRIVATE_RUNS_PER_PROXY = 5
# execution_id -> its background worker, running or waiting for its private session.
_execution_workers: dict[str, asyncio.Task] = {}
# Executions whose worker still waits for an earlier run on the same private session.
_waiting_runs: set[str] = set()
# private session -> latest scheduled worker; a continuation queues behind it.
_session_tails: dict[str, asyncio.Task] = {}
PRIVATE_TASK_POLICY = "swarm.group_private_task_policy"
BEGIN_TEAM_TASK = "swarm.group_begin_team_task"


def resolve_group_members(session_id: str, requested, *, is_first_request: bool):
    """Restore the saved roster; chat messages cannot silently replace it."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    from jiuwenswarm.server.runtime.group_service import normalize_group_member, read_group

    group = read_group(session_id)
    stored = (
        group["members"]
        if group is not None
        else (get_session_metadata(session_id, enable_writeback=False) or {}).get(
            "group_members"
        )
    )
    if stored is not None:
        stored = [normalize_group_member(member) for member in stored]
    if requested is not None:
        if not isinstance(requested, list):
            raise ValueError("group_members must be an array")
        requested = [normalize_group_member(member) for member in requested]
        if stored is not None and requested != stored:
            raise ValueError("group roster cannot be replaced by a chat message")
        if stored is None and not is_first_request:
            raise ValueError("initial group roster requires a new group runtime")
    return stored if stored is not None else requested


def assemble_group_members(spec, members: list[dict]) -> None:
    """Prepare a roster before build; serialized specs also retain proxy tools."""
    from jiuwenswarm.agents.swarm.assembly import (
        _apply_agent_group,
        _merge_team_prompt,
        _template_team_prompt,
        _with_agent_template,
    )
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package_bundle
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_dir,
        resolve_agent_template_dir,
    )
    from openjiuwen.agent_teams.tools.tool_permissions import _MEMBER_NAME_PATTERN
    from openjiuwen.agent_teams.constants import RESERVED_MEMBER_NAMES

    if not isinstance(members, list):
        raise ValueError("group_members must be an array")
    names = {
        *RESERVED_MEMBER_NAMES,
        spec.leader.member_name,
        *(m.member_name for m in spec.predefined_members),
    }
    prepared = []
    # Validate and load every member before modifying the spec.
    for item in members:
        name = item.get("member_name") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not _MEMBER_NAME_PATTERN.fullmatch(name)
            or name in names
        ):
            raise ValueError("group member_name must be valid and unique")
        names.add(name)
        kind, definition = item.get("kind"), item.get("id")
        if kind == "agent_template":
            template = load_agent_template_package(
                resolve_agent_template_dir(definition) / "manifest.json"
            )
            child = None
        elif kind == "agent_group":
            package = load_agent_group_package_bundle(
                resolve_agent_group_dir(definition)
            )
            template = package.templates["leader"]
            child = TeamAgentSpec.model_validate(
                spec.model_dump(mode="json", exclude={"build_context"})
            )
            child.metadata = {}
            child.predefined_members = []
            child.spawn_mode = "inprocess"
            child.lifecycle = "persistent"
            if "enable_organization" in type(child).model_fields:
                child.enable_organization = False
            _apply_agent_group(child, definition, package)
        else:
            raise ValueError("group member kind must be agent_template or agent_group")
        base = spec.agents["teammate"]
        agent = _with_agent_template(base.model_copy(deep=True), template)
        prompt = _template_team_prompt(template, base)
        if child is not None:
            agent.tools = [
                *(agent.tools or []),
                BuiltinToolSpec(
                    type=RUN_TEAM,
                    params={
                        "child_spec": child.model_dump(
                            mode="json", exclude={"build_context"}
                        ),
                    },
                ),
            ]
            from jiuwenswarm.agents.swarm.group_execution import GROUP_MEMBER_RULES, EXPERT_TEAM_RULES

            prompt = _merge_team_prompt(
                prompt,
                GROUP_MEMBER_RULES + EXPERT_TEAM_RULES
                + "For private-team work, call run_team with a stable run_id and the task. "
                "The tool starts background work and returns its execution_id; remain available for new messages. "
                "Use run_team action=update or cancel to change that execution. "
                "For a follow-up to a private-team execution, use run_team action=continue with a new run_id and the previous execution_id. "
                "After a Team Organization, a message that mentions only you is a follow-up. "
                "Answer it yourself with group_send_message when one agent can do it. "
                "Call run_team when the expert team should do it. That call resumes the same instance "
                "and loads the context from its earlier child task. "
                "Results are posted automatically; do not post or submit them twice. "
                "If the same message mentions multiple expert teams, call run_team and do not reply. "
                "That call joins your bound instance to one shared Team Organization. "
                "Only the Owner posts the group reply. A teammate proxy and its expert-team instance "
                "must not call group_send_message or send_file for that message. "
                "Use group_send_message only when this message mentions you alone. "
                "Do not build the outer group.",
            )
        member_params = dict(
            member_name=name,
            display_name=item.get("display_name") or template.agent_card.name or name,
            desc=template.agent_card.description or "",
            prompt=prompt,
        )
        external = external_cli_agent_spec_from_template(template)
        member = (
            ExternalCliMemberSpec(**member_params, external_cli=external)
            if external is not None
            else TeamMemberSpec(**member_params, role_type=TeamRole.TEAMMATE)
        )
        if external is not None:
            agent = base.model_copy(deep=True)
        if external is None:
            from jiuwenswarm.agents.swarm.group_execution import install_execution_policy

            install_execution_policy(agent)
        prepared.append((name, agent, member))
    for name, agent, member in prepared:
        spec.agents[name] = agent
        spec.predefined_members = [*spec.predefined_members, member]
    spec.metadata = {**spec.metadata, "group_members": members}
    spec.team_mode = "hybrid"
    declare_group_cli_kinds(spec)


def enabled_group_cli_kinds() -> set[str]:
    """Read the same live switches used by the external-agent settings panel."""
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.config_panel.config_set_handlers import flatten_external_cli_agents_for_config_panel

    switches = flatten_external_cli_agents_for_config_panel(get_config())
    return {kind for kind in GROUP_CLI_KINDS if switches.get(f"external_cli_agent_{kind}_enabled") == "true"}


def declare_group_cli_kinds(spec) -> None:
    """Declare enabled, installed CLI kinds so experts of those kinds can join a running group.

    core fixes its CLI launch configs when the runtime starts. Predefined CLI
    members use their own config; a member added later can only use the kind
    config declared here. Configs the host already declares are kept.
    """
    from openjiuwen.agent_teams.external.cli_agent.backends import missing_sdk_requirement

    enabled = enabled_group_cli_kinds()
    declared = {config.cli_agent for config in spec.external_cli_agents}
    additions = [
        ExternalCliAgentSpec(cli_agent=kind, skill_conflict="append")
        for kind in GROUP_CLI_KINDS
        if kind in enabled and kind not in declared and missing_sdk_requirement(kind) is None
    ]
    if additions:
        spec.external_cli_agents = [*spec.external_cli_agents, *additions]


def cli_launch_config(config) -> dict:
    """Return the launch fields that a kind-level config shares with its members."""
    return config.model_dump(mode="json", exclude=set(CLI_MEMBER_ONLY_FIELDS))


def run_ticket_path(runs_dir: Path, key: str) -> Path:
    """Return the run ticket for ``key``, keeping a legacy full-hash ticket in place."""
    legacy = runs_dir / f"{key}.json"
    if legacy.is_file():
        return legacy
    return runs_dir / f"{key[:RUN_PATH_HASH_LENGTH]}.json"


def run_workspace_path(runs_dir: Path, team_key: str) -> Path:
    """Return a private team's workspace, keeping a legacy full-hash directory in place.

    A recovered private team keeps the workspace root saved in its first spec,
    so an existing directory must not move.
    """
    legacy = runs_dir / team_key
    if legacy.is_dir():
        return legacy / "workspace"
    return runs_dir / team_key[:RUN_PATH_HASH_LENGTH] / "workspace"


def track_leader_reply(chunk, reply: list[str]) -> None:
    """Keep the private leader's latest model text; a tool call starts a new reply."""
    role = getattr(chunk, "role", None)
    if getattr(role, "value", role) != TeamRole.LEADER.value:
        return
    chunk_type, payload = getattr(chunk, "type", None), chunk.payload
    if chunk_type in {"tool_call", "tool_result"}:
        reply.clear()
    elif chunk_type == "llm_output" and not payload.get("retrying"):
        content = payload.get("content")
        if isinstance(content, str):
            reply.append(content)


async def cancel_waiting_runs(execution_ids) -> None:
    """Cancel runs still waiting for their private session, before a group is deleted."""
    workers = [
        _execution_workers[execution_id] for execution_id in execution_ids
        if execution_id in _waiting_runs and execution_id in _execution_workers
    ]
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.wait(workers)


class _GroupTool(Tool):
    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class CompleteTeamRun(_GroupTool):
    def __init__(self, key: str):
        super().__init__(
            ToolCard(
                id="group.complete_team_run",
                name="complete_team_run",
                description=(
                    "Complete this private-team run with its final result; the host publishes it to the group. "
                    "Direct answers need no task card; call start_group_task before doing substantive work."
                    " Final files in the team's outputs directory are attached automatically. "
                    "Include the complete answer in result when no file was produced."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "result": {"type": "string"},
                        "execution_id": {"type": "string"},
                        "attachments": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": (
                        ["result", "execution_id"] if _private_runs.get(key, {}).get("execution_id") else ["result"]
                    ),
                },
            )
        )
        self.key = key

    async def invoke(self, inputs, **kwargs):
        result = inputs.get("result")
        future = _results.get(self.key)
        if not isinstance(result, str) or not result.strip():
            return ToolOutput(success=False, error="result must be nonempty")
        if future is None or future.done():
            return ToolOutput(
                success=False, error="expert-team execution is not awaiting a result"
            )
        attachments = inputs.get("attachments", [])
        if not isinstance(attachments, list):
            return ToolOutput(success=False, error="attachments must be a list")
        if any(not isinstance(item, dict) for item in attachments):
            return ToolOutput(success=False, error="attachments must be JSON objects")
        current = _private_runs.get(self.key)
        if current is not None and current.get("execution_id"):
            if inputs.get("execution_id") != current["execution_id"]:
                return ToolOutput(success=False, error="execution_id must match the active private-team task")
        attachments = list(attachments)
        outputs_dir = current.get("outputs_dir") if current is not None else None
        if outputs_dir:
            root = Path(outputs_dir).resolve()
            paths = {item.get("path") for item in attachments if isinstance(item.get("path"), str)}
            for file in sorted(root.rglob("*")):
                resolved = file.resolve()
                if not file.is_file() or not resolved.is_relative_to(root):
                    continue
                path = str(resolved)
                if path not in paths:
                    attachments.append({"name": file.name, "path": path})
                    paths.add(path)
        submission = {"result": result, "attachments": attachments} if attachments else result
        future.set_result(submission)
        return ToolOutput(success=True, data={"submitted": True})


class RunTeam(_GroupTool):
    def __init__(self, backend, context, child_spec: dict):
        super().__init__(
            ToolCard(
                id="group.run_team",
                name="run_team",
                description=(
                    "Manage private-team work: start a new context, continue a completed context, "
                    "update or cancel active work. Reuse run_id for retries. "
                    "A direct answer is posted as chat; a card appears only when actual work starts."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["start", "continue", "update", "cancel"]},
                        "run_id": {"type": "string"},
                        "content": {"type": "string"},
                        "execution_id": {"type": "string"},
                        "reason": {"type": "string"},
                        "task": {"type": "string", "description": "Legacy alias for content."},
                        "continue_from_execution_id": {"type": "string", "description": "Legacy continuation alias."},
                    },
                    "required": [],
                },
            )
        )
        self.backend, self.context, self.child_spec = backend, context, child_spec

    async def invoke(self, inputs, **kwargs):
        action = inputs.get("action")
        if action is None:
            action = "continue" if inputs.get("continue_from_execution_id") else "start"
        if not isinstance(action, str) or action not in {"start", "continue", "update", "cancel"}:
            return ToolOutput(success=False, error="invalid run_team action")
        if self.backend.conversation_mode != "group_chat":
            return ToolOutput(success=False, error="run_team requires a group_chat runtime")
        if action in {"update", "cancel"}:
            return await self._adjust({**inputs, "action": action})
        run_id, task = inputs.get("run_id"), inputs.get("content") or inputs.get("task")
        if not all(isinstance(v, str) and v.strip() for v in (run_id, task)):
            return ToolOutput(success=False, error="run_id and task must be nonempty")
        session = self.context.session_id
        from jiuwenswarm.agents.swarm.group_org_bootstrap import (
            closed_organization_plan_result,
            organization_plan_for_member,
            pending_organization_plan,
            queue_organization_plan,
            direct_route_for_member,
            recorded_organization_plan,
            release_organization_plan_claim,
            remember_proxy_backend,
            remember_proxy_tool,
            route_live_organization_plan,
            organization_plan_accepted,
            peek_member_turn,
            consume_member_turn,
            bind_member_turn_front,
            run_instance_followup,
            run_organization_member,
            saved_organization_instance,
            triggering_plan_id,
        )

        remember_proxy_backend(session, self.backend)
        remember_proxy_tool(session, self)

        bound = peek_member_turn(session, self.backend.member_name)
        message_plan_id = bound or await triggering_plan_id(
            self.backend, self.backend.member_name
        )
        if bound and bound == message_plan_id:
            consume_member_turn(session, self.backend.member_name)
        direct = direct_route_for_member(
            session, self.backend.member_name, message_plan_id
        )
        live = pending_organization_plan(session, self.backend.member_name)
        recorded = (
            recorded_organization_plan(session, self.backend.member_name, message_plan_id)
            if message_plan_id and not direct
            else None
        )
        plan = None
        if recorded is not None:
            # This call answers the multi-@ message that opened an organization.
            if str(recorded.get("status") or "") not in {"pending", "running"}:
                return closed_organization_plan_result(recorded)
            plan = organization_plan_for_member(
                session, self.backend.member_name, message_plan_id
            )
        elif live is not None and not organization_plan_accepted(
            session, str(live.get("plan_id") or ""), self.backend.member_name
        ):
            # The proxy still owes its join to an older organization message.
            if message_plan_id:
                bind_member_turn_front(
                    session, self.backend.member_name, message_plan_id
                )
            plan = live
        # Any other message is outside the organization: the proxy chose this run by
        # meaning. An organization-related request is run_team update on its card.
        if plan is not None:
            action, plan = route_live_organization_plan(
                session, self.backend.member_name, plan
            )
            if action == "queue":
                return queue_organization_plan(
                    backend=self.backend,
                    session_id=session,
                    plan=plan,
                )
            result = await run_organization_member(
                backend=self.backend,
                session_id=session,
                plan=plan,
            )
            if getattr(result, "success", True) is False:
                release_organization_plan_claim(
                    session, str(plan.get("plan_id") or ""), self.backend.member_name
                )
            return result
        # While its organization runs, the proxy's instance is busy; a new task opens a private team.
        if not direct and live is None:
            instance = await saved_organization_instance(
                self.backend, self.backend.member_name, session
            )
            if instance is not None:
                return await run_instance_followup(
                    backend=self.backend,
                    session_id=session,
                    run_id=run_id,
                    task=task,
                )
        previous_id = inputs.get("execution_id") or inputs.get("continue_from_execution_id")
        if action == "continue" and not previous_id:
            return ToolOutput(success=False, error="continue requires a source execution_id")
        if action == "start" and previous_id:
            return ToolOutput(success=False, error="use action=continue to reuse an execution context")
        key = hashlib.sha256(
            json.dumps(
                [session, self.backend.team_name, self.backend.member_name, run_id]
            ).encode()
        ).hexdigest()
        from jiuwenswarm.server.runtime.group_service import read_group

        managed = read_group(session) is not None
        lock_key = key
        if managed:
            # Serializes only binding a run to an execution; the runs execute concurrently.
            lock_key = json.dumps([session, self.backend.team_name, self.backend.member_name])
        lock = _locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            from jiuwenswarm.server.runtime.group_execution import GroupExecutions

            execution = None
            store = GroupExecutions(session)
            if managed:
                from jiuwenswarm.server.runtime.group_execution import TERMINAL
                from jiuwenswarm.agents.harness.team import get_team_manager

                await store.reconcile()
                previous = None
                if previous_id is not None:
                    try:
                        previous = store.get(previous_id)
                    except ValueError as exc:
                        return ToolOutput(success=False, error=str(exc))
                    if previous["member_name"] != self.backend.member_name:
                        return ToolOutput(success=False, error="continuation must belong to this member")
                    if previous["status"] != "completed" or not previous.get("team_session_id"):
                        return ToolOutput(
                            success=False, error="continuation requires a completed private-team execution",
                        )
                manager = get_team_manager(self.context.channel_id)
                for record in store.read().values():
                    if record.get("run_key") == key:
                        execution = record
                        break
                if execution is None:
                    # Bind a task the user assigned to this member, if this run carries it out.
                    assigned = store.active(self.backend.member_name)
                    if assigned is not None and not assigned.get("run_key") and assigned["task"] == task:
                        execution = assigned
                if execution is None:
                    running = len(store.background_runs(self.backend.member_name))
                    if running >= MAX_PRIVATE_RUNS_PER_PROXY:
                        return ToolOutput(success=False, error=(
                            f"this expert team already has {running} unfinished tasks, the limit; "
                            "wait for one to finish or cancel one before starting another"
                        ))
                    # Each new run gets its own private team, so runs of one proxy proceed concurrently.
                    execution = (await store.create(
                        [self.backend.member_name], task, key, defer_card=True, concurrent=True,
                    ))[0]
                if execution["task"] != task:
                    return ToolOutput(success=False, error="run_id was already used for a different task")
                if execution.get("run_key") and execution.get("previous_execution_id") != previous_id:
                    return ToolOutput(success=False, error="run_id was used with another continuation")
                if execution["status"] in TERMINAL:
                    if execution["status"] == "completed":
                        return ToolOutput(success=True, data=execution)
                    return ToolOutput(success=False, error="execution ended; use a new run_id")
                team_key = execution.get("private_team_key") or key
                if previous is not None:
                    team_key = previous.get("private_team_key") or previous.get("run_key")
                child_session = f"group-run-{team_key[:32]}"
                from jiuwenswarm.server.runtime.group_service import group_lock

                async with group_lock(f"group-adjust-{team_key}"):
                    scheduled = _execution_workers.get(execution["execution_id"])
                    if scheduled is not None and not scheduled.done():
                        return ToolOutput(success=True, data={
                            "execution_id": execution["execution_id"], "accepted": True,
                            "queued": execution["execution_id"] in _waiting_runs,
                        })
                    # A continuation reuses its private session, so it waits for the run already there.
                    predecessor = _session_tails.get(child_session)
                    if predecessor is None or predecessor.done():
                        predecessor = manager.get_stream_task(child_session)
                    if predecessor is not None and predecessor.done():
                        predecessor = None
                    waiting = {"phase": "waiting_for_team"} if predecessor is not None else {}
                    execution = await store.update(
                        execution["execution_id"], run_key=key, execution_kind="team", background=True,
                        private_team_key=team_key, team_session_id=child_session,
                        previous_execution_id=previous_id,
                        team_id=f"group-expert-{team_key[:24]}", **waiting,
                    )
                    from jiuwenswarm.server.runtime.session import lifecycle

                    async with group_lock(session):
                        current_group = read_group(session)
                        if current_group is None or current_group["status"] == "deleting":
                            return ToolOutput(success=False, error="group is unavailable")
                        lifecycle.guard(session)
                        worker = asyncio.create_task(
                            self._execute_background(key, task, execution, manager, after=predecessor),
                        )
                        _execution_workers[execution["execution_id"]] = worker
                        _session_tails[child_session] = worker
                        if predecessor is None:
                            manager.register_stream_task(child_session, worker)
                        else:
                            _waiting_runs.add(execution["execution_id"])
                    return ToolOutput(success=True, data={
                        "execution_id": execution["execution_id"], "accepted": True,
                        "queued": predecessor is not None, "posted_to_group": False,
                    })
            try:
                return await self._run(key, task, execution=execution)
            except asyncio.CancelledError:
                if execution is not None:
                    await store.update(execution["execution_id"], status="cancelled", phase="cancelled")
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                if execution is not None:
                    current = store.get(execution["execution_id"])
                    if current["phase"] != "publishing":
                        await store.update(execution["execution_id"], status="failed", phase="failed")
                return ToolOutput(success=False, error=str(exc))

    async def _adjust(self, inputs):
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions
        from jiuwenswarm.server.runtime.group_service import GroupError
        from openjiuwen.core.runner import Runner
        from openjiuwen.agent_teams.interaction.payload import GodViewMessage
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.server.runtime.group_execution import TERMINAL
        from jiuwenswarm.server.runtime.group_service import group_lock

        store = GroupExecutions(self.context.session_id)
        try:
            action = inputs.get("action")
            if not isinstance(action, str):
                raise GroupError("BAD_REQUEST", "action must be update or cancel")
            if action not in {"update", "cancel"}:
                raise GroupError("BAD_REQUEST", "action must be update or cancel")
            record = store.get(inputs.get("execution_id"))
            if record.get("execution_kind") == "organization":
                return await self._adjust_organization(record, action, inputs)
            if record["member_name"] != self.backend.member_name:
                raise GroupError("NOT_FOUND", "execution not found")
            if record["status"] in TERMINAL:
                raise GroupError("CONFLICT", "execution has ended; use run_team continuation for follow-up")
            child_session = record.get("team_session_id")
            if not child_session:
                raise GroupError("UNSUPPORTED_RUNTIME", "execution has no private team to adjust")
            # Serialize cancellation with a continuation that reuses the same team.
            async with group_lock(f"group-adjust-{record['private_team_key']}"):
                record = store.get(record["execution_id"])
                if record["status"] in TERMINAL:
                    raise GroupError("CONFLICT", "execution has already ended")
                manager = get_team_manager(self.context.channel_id)
                worker = _execution_workers.get(record["execution_id"])
                if worker is None or worker.done():
                    raise GroupError("NOT_RUNNING", "private-team worker is not running on this server")
                waiting = record["execution_id"] in _waiting_runs
                if action == "cancel":
                    reason = inputs.get("reason", "user_cancelled")
                    if not isinstance(reason, str):
                        raise GroupError("BAD_REQUEST", "reason must be text")
                    await store.update(record["execution_id"], phase="cancelling", cancellation_reason=reason)
                    if waiting:
                        # Not started yet: the private session keeps serving the run ahead of it.
                        worker.cancel()
                        await asyncio.wait({worker})
                        result = await store.update(record["execution_id"], status="cancelled", phase="cancelled")
                        return ToolOutput(success=True, data=result)
                    await manager.stop_session_runtime(child_session, reason="group.execution.cancel")
                    current = store.get(record["execution_id"])
                    if current["status"] in TERMINAL and current["status"] != "cancelled":
                        raise GroupError("CONFLICT", "execution ended with status " + current["status"])
                    result = await store.update(record["execution_id"], status="cancelled", phase="cancelled")
                    return ToolOutput(success=True, data=result)
                instruction = inputs.get("content") or inputs.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    raise GroupError("BAD_REQUEST", "instruction must be nonempty text")
                if len(instruction) > 20000:
                    raise GroupError("BAD_REQUEST", "instruction exceeds 20000 characters")
                if waiting:
                    # Delivered with the task once the private session is free.
                    adjustments = [*record.get("adjustments", []), {"instruction": instruction, "delivered": False}]
                    result = await store.update(record["execution_id"], adjustments=adjustments)
                    return ToolOutput(success=True, data={**result, "instruction_delivered": False, "queued": True})
                delivered = await Runner.interact_agent_team(
                    GodViewMessage(body=instruction), team_name=record["team_id"], session_id=child_session,
                )
                if not delivered.ok:
                    raise GroupError("DELIVERY_FAILED", delivered.reason or "instruction was not accepted")
                current = store.get(record["execution_id"])
                adjustments = [*current.get("adjustments", []), {"instruction": instruction, "delivered": True}]
                result = await store.update(record["execution_id"], adjustments=adjustments)
                return ToolOutput(success=True, data={**result, "instruction_delivered": True})
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolOutput(success=False, error=str(exc))

    async def _adjust_organization(self, record: dict, action: str, inputs) -> ToolOutput:
        """Pass a related request to this proxy's instance in a running organization."""
        from jiuwenswarm.agents.swarm.group_org_bootstrap import deliver_organization_instruction
        from jiuwenswarm.server.runtime.group_service import GroupError

        if action == "cancel":
            raise GroupError("UNSUPPORTED_RUNTIME", "one expert team cannot cancel a Team Organization")
        instruction = inputs.get("content") or inputs.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise GroupError("BAD_REQUEST", "instruction must be nonempty text")
        if len(instruction) > 20000:
            raise GroupError("BAD_REQUEST", "instruction exceeds 20000 characters")
        delivered = await deliver_organization_instruction(
            self.context.session_id, self.backend.member_name, record["execution_id"], instruction,
        )
        return ToolOutput(success=True, data=delivered)

    async def _execute_background(
        self, key: str, task: str, execution: dict, manager, *, after: asyncio.Task | None = None,
    ) -> None:
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions
        from jiuwenswarm.server.runtime.group_service import read_group

        store = GroupExecutions(self.context.session_id)
        execution_id = execution["execution_id"]
        child_session = execution["team_session_id"]
        try:
            query = task
            if after is not None:
                # asyncio.wait, unlike gather, does not cancel the earlier run when this one is cancelled.
                await asyncio.wait({after})
                manager.register_stream_task(child_session, asyncio.current_task())
                _waiting_runs.discard(execution_id)
                current = store.get(execution_id)
                pending = [item["instruction"] for item in current.get("adjustments", []) if not item["delivered"]]
                if pending:
                    query = task + "\n\nAdditional instructions:\n" + "\n".join(f"- {item}" for item in pending)
                    await store.update(execution_id, adjustments=[
                        {**item, "delivered": True} for item in current["adjustments"]
                    ])
            await self._run(key, task, execution=execution, query=query)
        except asyncio.CancelledError:
            group = read_group(self.context.session_id)
            if group is not None and group["status"] != "deleting":
                await store.update(execution["execution_id"], status="cancelled", phase="cancelled")
            raise
        except Exception:
            logger.exception("Private group execution failed execution=%s", execution["execution_id"])
            group = read_group(self.context.session_id)
            if group is not None and group["status"] != "deleting":
                current = store.get(execution["execution_id"])
                if current["phase"] != "publishing":
                    await store.update(execution["execution_id"], status="failed", phase="failed")
        finally:
            current_task = asyncio.current_task()
            if manager.get_stream_task(child_session) is current_task:
                manager.pop_stream_task(child_session)
            if _execution_workers.get(execution_id) is current_task:
                del _execution_workers[execution_id]
            if _session_tails.get(child_session) is current_task:
                del _session_tails[child_session]
            _waiting_runs.discard(execution_id)

    async def _run(self, key: str, task: str, *, execution=None, query: str | None = None):
        from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write
        from jiuwenswarm.agents.swarm.assembly import enrich_team_spec_for_swarm

        conversation = await self.backend.group_conversation()
        state_path = run_ticket_path(conversation.history_path.parent / "team-runs", key)
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {}
        )
        if state and state["task"] != task:
            raise ValueError("run_id was already used for a different task")
        state_path.parent.mkdir(parents=True, exist_ok=True)

        def save():
            atomic_write(state_path, json.dumps(state, ensure_ascii=False))

        if not state.get("result"):
            child = TeamAgentSpec.model_validate(self.child_spec)
            team_key = execution.get("private_team_key", key) if execution is not None else key
            child.team_name = f"group-expert-{team_key[:24]}"
            child_session = f"group-run-{team_key[:32]}"
            child.external_transport = None
            if child.workspace is not None:
                child.workspace = child.workspace.model_copy(
                    update={
                        "root_path": str(run_workspace_path(state_path.parent, team_key)),
                    }
                )
            # Build fresh host context for the private team, not the proxy's workspace.
            enrich_team_spec_for_swarm(
                child,
                session_id=child_session,
                mode=self.context.mode,
                project_dir=self.context.project_dir,
                trusted_dirs=self.context.trusted_dirs,
                user_id=self.context.user_id,
                channel_id=self.context.channel_id,
                request_metadata={"mode": self.context.mode},
            )
            child.agents["leader"].tools = [
                *(child.agents["leader"].tools or []),
                BuiltinToolSpec(type=COMPLETE_RUN, params={"key": team_key}),
            ]
            if execution is not None:
                child.agents["leader"].tools.append(BuiltinToolSpec(type=BEGIN_TEAM_TASK, params={"key": team_key}))
                from jiuwenswarm.agents.swarm.group_execution import GROUP_ASK_USER

                leader_agent = child.agents["leader"]
                leader_agent.rails = [
                    rail for rail in (leader_agent.rails or []) if rail.type != "swarm.structured_ask_user"
                ]
                leader_agent.rails.append(RailSpec(type=GROUP_ASK_USER, params={
                    "group_id": self.context.session_id, "member_name": self.backend.member_name,
                    "execution_id": execution["execution_id"],
                }))
                leader_agent.rails = [*(leader_agent.rails or []), RailSpec(
                    type=PRIVATE_TASK_POLICY, params={"key": team_key},
                )]
            _private_runs[team_key] = {
                "member_name": self.backend.member_name,
                "execution_id": execution["execution_id"] if execution is not None else None,
                "group_id": self.context.session_id if execution is not None else None,
                "outputs_dir": getattr(child.build_context, "team_outputs_dir", None),
            }
            child.leader = child.leader.model_copy(
                update={
                    "prompt": (child.leader.prompt or "")
                    + "\nSubmit the final deliverable with complete_team_run. "
                    "Do not finish with only a plain-text reply or clean_team."
                }
            )
            state.update(
                task=task,
                team_name=child.team_name,
                session_id=child_session,
                status="running",
            )
            save()
            if execution is not None:
                from jiuwenswarm.server.runtime.group_execution import GroupExecutions

                await GroupExecutions(self.context.session_id).update(
                    execution["execution_id"], status="running", phase="executing", team_id=child.team_name,
                )
            future = asyncio.get_running_loop().create_future()
            _results[team_key] = future

            async def consume():
                from openjiuwen.agent_teams.context import set_session_id, reset_session_id
                from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler

                handler = None
                progress_worker = None
                leader_reply: list[str] = []

                def settle_with_leader_reply() -> bool:
                    # A leader that answered in plain text has answered; its words are the result.
                    text = "".join(leader_reply).strip()
                    if not text or future.done():
                        return False
                    logger.info("Private team finished without complete_team_run; using its last reply")
                    future.set_result(text)
                    return True

                async def record_progress(event):
                    await GroupExecutions(self.context.session_id).task_event(execution["execution_id"], event)

                async def watch_progress():
                    async for event in handler.events():
                        await record_progress(event)

                try:
                    async for chunk in Runner.run_agent_team_streaming(
                        child, {"query": query or task}, session=child_session,
                    ):
                        payload = getattr(chunk, "payload", None)
                        if not isinstance(payload, dict):
                            continue
                        track_leader_reply(chunk, leader_reply)
                        event_type = payload.get("event_type")
                        if execution is not None:
                            await record_progress(payload)
                            if event_type == "team.runtime_ready" and handler is None:
                                token = set_session_id(child_session)
                                try:
                                    monitor = await Runner.get_agent_team_monitor(
                                        team_name=child.team_name, session_id=child_session, hide_dm=True,
                                    )
                                finally:
                                    reset_session_id(token)
                                if monitor is not None:
                                    handler = TeamMonitorHandler(monitor, child_session)
                                    await handler.start()
                                    progress_worker = asyncio.create_task(watch_progress())
                        if event_type in {"team.interact.failed", "team.error"}:
                            raise RuntimeError("expert-team execution failed")
                        if event_type in {"team.idle", "team.completed"} and not future.done():
                            from jiuwenswarm.server.runtime.group_questions import GroupQuestions

                            if execution is not None:
                                pending = GroupQuestions(self.context.session_id).pending(self.backend.member_name)
                                waiting = any(item["execution_id"] == execution["execution_id"] for item in pending)
                                if waiting:
                                    continue
                            if settle_with_leader_reply():
                                return
                            raise RuntimeError("expert team became idle without submitting a result")
                    settle_with_leader_reply()
                finally:
                    if progress_worker is not None:
                        progress_worker.cancel()
                        await asyncio.gather(progress_worker, return_exceptions=True)
                    if handler is not None:
                        await handler.stop()

            worker = asyncio.create_task(consume())
            try:
                done, _ = await asyncio.wait(
                    {worker, future}, return_when=asyncio.FIRST_COMPLETED
                )
                if future not in done:
                    await worker
                    raise RuntimeError("expert team ended without submitting a result")
                submission = future.result()
                if isinstance(submission, dict):
                    state.update(submission)
                else:
                    state["result"] = submission
                state["status"] = "completed"
                save()
            finally:
                try:
                    paused = await Runner.pause_agent_team(
                        team_name=child.team_name, session_id=child_session
                    )
                    if paused is False:
                        raise RuntimeError("private team could not be paused")
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    _results.pop(team_key, None)
                    _private_runs.pop(team_key, None)
                    if not state.get("result"):
                        state["status"] = "interrupted"
                        save()
        if execution is not None:
            from jiuwenswarm.server.runtime.group_execution import GroupExecutions

            record = await GroupExecutions(self.context.session_id).finish(
                execution["execution_id"], self.backend, state["result"], state.get("attachments", []),
            )
            state.update(status="delivered", message_id=record["message_id"])
            save()
            return ToolOutput(success=True, data={
                "execution_id": execution["execution_id"], "message_id": record["message_id"],
                "result": state["result"], "posted_to_group": True,
            })
        # Stable client ID repairs a crash between result storage and publication.
        posted = await self.backend.append_group_message(
            self.backend.member_name,
            state["result"],
            client_message_id=f"expert-result-{key}",
            mentions=[],
        )
        state.update(status="delivered", message_id=posted.message.message_id)
        save()
        return ToolOutput(
            success=True,
            data={
                "result": state["result"],
                "message_id": state["message_id"],
                "posted_to_group": True,
            },
        )


class BeginTeamTask(_GroupTool):
    def __init__(self, key: str):
        super().__init__(ToolCard(
            id="group.begin_private_task", name="start_group_task",
            description="Start substantive work for the current execution_id and create its public task card.",
            input_params={
                "type": "object", "required": ["execution_id"],
                "properties": {"execution_id": {"type": "string"}}, "additionalProperties": False,
            },
        ))
        self.key = key

    async def invoke(self, inputs, **kwargs):
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions

        current = _private_runs.get(self.key)
        if current is None or inputs.get("execution_id") != current["execution_id"]:
            return ToolOutput(success=False, error="execution_id must match the current private invocation")
        future = _results.get(self.key)
        if future is None or future.done():
            return ToolOutput(success=False, error="result submission has begun")
        try:
            record = await GroupExecutions(current["group_id"]).begin_task(
                current["execution_id"], current["member_name"],
            )
            return ToolOutput(success=True, data={"execution_id": record["execution_id"], "task_started": True})
        except (ValueError, OSError) as exc:
            return ToolOutput(success=False, error=str(exc))


class PrivateTaskPolicy(DeepAgentRail):
    """Keep direct answers separate from work that needs a public task card."""

    def __init__(self, context, key: str):
        super().__init__()
        self.context = context
        self.key = key

    async def before_invoke(self, ctx):
        from openjiuwen.harness.prompts import PromptSection

        current = _private_runs.get(self.key)
        if current is None:
            return
        content = (
            "Current execution_id: " + current["execution_id"]
            + ". If you can answer directly, call complete_team_run with this ID; do not start a task. "
            "Before substantive work or using other tools, call start_group_task with this ID. "
            "Follow the normal team collaboration rules for planning, delegation and task execution. "
            "Submit the final result with complete_team_run. "
            "Ask the user with ask_user if important requirements are unclear, before substantive work. "
            "Never reuse an earlier execution_id."
        )
        builder = ctx.agent.system_prompt_builder
        builder.remove_section("group_private_execution")
        builder.add_section(PromptSection(
            name="group_private_execution", content={"en": content, "cn": content}, priority=95,
        ))

    async def before_tool_call(self, ctx):
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions
        from openjiuwen.core.foundation.llm import ToolMessage

        if ctx.inputs.tool_name in {"start_group_task", "complete_team_run", "ask_user"}:
            return
        current = _private_runs.get(self.key)
        if current is None:
            return
        record = GroupExecutions(current["group_id"]).get(current["execution_id"])
        if record["presentation"] == "card":
            return
        error = "Call start_group_task for the current execution_id before using work tools."
        call = ctx.inputs.tool_call
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = {"error": error}
        ctx.inputs.tool_msg = ToolMessage(content=error, tool_call_id=call.id if call else "")


@harness_element(kind=ElementKind.TOOL, name=BEGIN_TEAM_TASK, description="Confirm private-team substantive work.")
def build_begin_team_task(params, context):
    backend = get_team_backend(context)
    if backend is None or not backend.is_leader:
        raise ValueError("only the private leader may start its task card")
    return BeginTeamTask(params["key"])


@harness_element(kind=ElementKind.RAIL, name=PRIVATE_TASK_POLICY, description="Private response or task policy.")
def build_private_task_policy(params, context):
    return PrivateTaskPolicy(context, params["key"])


@harness_element(
    kind=ElementKind.TOOL,
    name=RUN_TEAM,
    description="On-demand expert team bound to one public-group proxy.",
)
def build_group_run_team(params, context):
    backend = get_team_backend(context)
    if backend is None or backend.conversation_mode != "group_chat":
        raise ValueError("group proxy requires a group_chat backend")
    return RunTeam(backend, context, params["child_spec"])


@harness_element(
    kind=ElementKind.TOOL,
    name=COMPLETE_RUN,
    description="Complete a private expert-team run and return its final result.",
)
def build_group_complete_run(params, context):
    backend = get_team_backend(context)
    if backend is None or not backend.is_leader:
        raise ValueError("only the private expert-team leader may submit results")
    return CompleteTeamRun(params["key"])


class GroupMemberTemplateRail(DeepAgentRail):
    """Load a newly registered member's capabilities before its first invocation."""

    def __init__(self, context, initial_names):
        super().__init__()
        self.context = context
        self.initial_names = initial_names
        self.loaded = False

    async def before_invoke(self, ctx):
        if self.loaded or self.context.member_name in self.initial_names:
            return
        from jiuwenswarm.server.runtime.group_service import read_group
        from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec

        group = read_group(self.context.session_id)
        if group is None:
            return
        definition = group["compiled"].get(self.context.member_name)
        if definition is not None and definition["template"] is not None:
            template = AgentTemplateSpec.model_validate(definition["template"])
            from jiuwenswarm.agents.swarm.group_execution import START_TASK

            abilities = getattr(ctx.agent, "ability_manager", None)
            shared_tools = {START_TASK: "start_group_task", RUN_TEAM: "run_team"}
            tools = []
            for tool in template.tools:
                name = shared_tools.get(tool.type)
                # Recovery may already bind group tools; duplicate binding rolls back the entire template.
                if name and abilities is not None and abilities.get(name) is not None:
                    continue
                tools.append(tool)
            template.tools = tools
            await ctx.agent.load_agent_template_spec(template, context=self.context)
        self.loaded = True


@harness_element(
    kind=ElementKind.RAIL,
    name=GROUP_MEMBER_TEMPLATE,
    description="Capabilities of experts dynamically registered in an existing group.",
)
def build_group_member_template(params, context):
    return GroupMemberTemplateRail(context, frozenset(params["initial_names"]))
