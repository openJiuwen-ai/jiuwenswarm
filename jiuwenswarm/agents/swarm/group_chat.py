"""Group roster assembly and on-demand expert-team execution.

This is host logic, not another team scheduler. A proxy remains a teammate in
the public group; its private team runs with its own leader and session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from weakref import WeakValueDictionary

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element
from openjiuwen.agent_teams.rails.team_context import get_team_backend
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.team import (
    ExternalCliMemberSpec,
    TeamMemberSpec,
    TeamRole,
)
from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput
from openjiuwen.core.runner import Runner
from openjiuwen.harness.resources import load_agent_template_package
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec
from openjiuwen.harness.rails.base import DeepAgentRail

RUN_TEAM = "swarm.group_run_team"
SUBMIT_RESULT = "swarm.group_submit_team_result"
GROUP_MEMBER_TEMPLATE = "swarm.group_member_template"
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_results: dict[str, asyncio.Future] = {}


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
            prompt = _merge_team_prompt(
                prompt,
                "You represent an expert team in this public group. Answer directly when appropriate. "
                "For work requiring your team, call run_team with a stable run_id and the task. "
                "The tool posts the submitted result to the group as you; do not post it twice. "
                "Use group_send_message for your own discussion. Do not build the outer group.",
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


class _GroupTool(Tool):
    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class SubmitTeamResult(_GroupTool):
    def __init__(self, key: str):
        super().__init__(
            ToolCard(
                id="group.submit_team_result",
                name="submit_team_result",
                description="Submit the final expert-team result to the group proxy. Only submit a final deliverable.",
                input_params={
                    "type": "object",
                    "properties": {
                        "result": {"type": "string"},
                        "attachments": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["result"],
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
        submission = {"result": result, "attachments": attachments} if attachments else result
        future.set_result(submission)
        return ToolOutput(success=True, data={"submitted": True})


class RunTeam(_GroupTool):
    def __init__(self, backend, context, child_spec: dict):
        super().__init__(
            ToolCard(
                id="group.run_team",
                name="run_team",
                description="Run your bound expert team on demand. Reuse run_id when retrying the same task. "
                "The submitted final result is posted to the public group under your identity.",
                input_params={
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    "required": ["run_id", "task"],
                },
            )
        )
        self.backend, self.context, self.child_spec = backend, context, child_spec

    async def invoke(self, inputs, **kwargs):
        run_id, task = inputs.get("run_id"), inputs.get("task")
        if not all(isinstance(v, str) and v.strip() for v in (run_id, task)):
            return ToolOutput(success=False, error="run_id and task must be nonempty")
        if self.backend.conversation_mode != "group_chat":
            return ToolOutput(
                success=False, error="run_team requires a group_chat runtime"
            )
        session = self.context.session_id
        key = hashlib.sha256(
            json.dumps(
                [session, self.backend.team_name, self.backend.member_name, run_id]
            ).encode()
        ).hexdigest()
        from jiuwenswarm.server.runtime.group_service import read_group

        managed = read_group(session) is not None
        lock_key = key
        if managed:
            lock_key = json.dumps([session, self.backend.team_name, self.backend.member_name])
        lock = _locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            from jiuwenswarm.server.runtime.group_execution import GroupExecutions

            execution = None
            store = GroupExecutions(session)
            if managed:
                await store.reconcile()
                for record in store.read().values():
                    if record.get("run_key") == key:
                        execution = record
                        break
                if execution is None:
                    execution = store.active(self.backend.member_name)
                if execution is None:
                    execution = (await store.create([self.backend.member_name], task, run_id))[0]
                if execution["task"] != task:
                    return ToolOutput(success=False, error="run_team task must match the assigned execution")
                if execution["status"] in {"failed", "cancelled", "interrupted"}:
                    return ToolOutput(success=False, error="execution ended; use a new run_id to start another task")
                execution = await store.update(
                    execution["execution_id"], run_key=key, execution_kind="team", presentation="card",
                )
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

    async def _run(self, key: str, task: str, *, execution=None):
        from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write
        from jiuwenswarm.agents.swarm.assembly import enrich_team_spec_for_swarm

        conversation = await self.backend.group_conversation()
        state_path = conversation.history_path.parent / "team-runs" / f"{key}.json"
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
            child.team_name = f"group-expert-{key[:24]}"
            child_session = f"group-run-{key[:32]}"
            child.external_transport = None
            if child.workspace is not None:
                child.workspace = child.workspace.model_copy(
                    update={
                        "root_path": str(state_path.parent / key / "workspace"),
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
                BuiltinToolSpec(type=SUBMIT_RESULT, params={"key": key}),
            ]
            child.leader = child.leader.model_copy(
                update={
                    "prompt": (child.leader.prompt or "")
                    + "\nSubmit the final deliverable with submit_team_result. "
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
            _results[key] = future

            async def consume():
                from openjiuwen.agent_teams.context import set_session_id, reset_session_id
                from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler

                handler = None
                progress_worker = None

                async def watch_progress():
                    async for event in handler.events():
                        if event.get("event_type") == "team.task":
                            await GroupExecutions(self.context.session_id).task_event(execution["execution_id"], event)

                try:
                    async for chunk in Runner.run_agent_team_streaming(
                        child, {"query": task}, session=child_session,
                    ):
                        payload = getattr(chunk, "payload", None)
                        if not isinstance(payload, dict):
                            continue
                        event_type = payload.get("event_type")
                        if execution is not None:
                            await GroupExecutions(self.context.session_id).task_event(execution["execution_id"], payload)
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
                            raise RuntimeError("expert team became idle without submitting a result")
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
                    await Runner.pause_agent_team(
                        team_name=child.team_name, session_id=child_session
                    )
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    _results.pop(key, None)
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
    name=SUBMIT_RESULT,
    description="Final-result submission from a private expert-team leader.",
)
def build_group_submit_result(params, context):
    backend = get_team_backend(context)
    if backend is None or not backend.is_leader:
        raise ValueError("only the private expert-team leader may submit results")
    return SubmitTeamResult(params["key"])


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
            await ctx.agent.load_agent_template_spec(
                AgentTemplateSpec.model_validate(definition["template"]),
                context=self.context,
            )
        self.loaded = True


@harness_element(
    kind=ElementKind.RAIL,
    name=GROUP_MEMBER_TEMPLATE,
    description="Capabilities of experts dynamically registered in an existing group.",
)
def build_group_member_template(params, context):
    return GroupMemberTemplateRail(context, frozenset(params["initial_names"]))
