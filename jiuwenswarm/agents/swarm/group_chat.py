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
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_results: dict[str, asyncio.Future] = {}
_private_runs: dict[str, dict] = {}
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
                "For a follow-up use run_team action=continue with a new run_id and the previous execution_id. "
                "Results are posted automatically; do not post or submit them twice. "
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
        previous_id = inputs.get("execution_id") or inputs.get("continue_from_execution_id")
        if action == "continue" and not previous_id:
            return ToolOutput(success=False, error="continue requires a source execution_id")
        if action == "start" and previous_id:
            return ToolOutput(success=False, error="use action=continue to reuse an execution context")
        run_id, task = inputs.get("run_id"), inputs.get("content") or inputs.get("task")
        if not all(isinstance(v, str) and v.strip() for v in (run_id, task)):
            return ToolOutput(success=False, error="run_id and task must be nonempty")
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
                    execution = store.active(self.backend.member_name)
                if execution is None:
                    if previous is not None:
                        previous_worker = manager.get_stream_task(previous["team_session_id"])
                        if previous_worker is not None and not previous_worker.done():
                            return ToolOutput(success=False, error=
                            "private team is busy or still finishing its last run")
                    execution = (await store.create([self.backend.member_name], task, key, defer_card=True))[0]
                if execution["task"] != task:
                    return ToolOutput(success=False, error="run_team task must match the assigned execution")
                if execution.get("run_key") and execution["run_key"] != key:
                    return ToolOutput(
                        success=False, error="member has another run; use its run_id to retry or adjust it",
                    )
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
                    existing_worker = manager.get_stream_task(child_session)
                    if existing_worker is not None and not existing_worker.done():
                        if execution.get("run_key") == key:
                            return ToolOutput(success=True, data={
                                "execution_id": execution["execution_id"], "accepted": True,
                            })
                        return ToolOutput(success=False, error="private team is busy")
                    execution = await store.update(
                        execution["execution_id"], run_key=key, execution_kind="team", background=True,
                        private_team_key=team_key, team_session_id=child_session,
                        previous_execution_id=previous_id,
                        team_id=f"group-expert-{team_key[:24]}",
                    )
                    from jiuwenswarm.server.runtime.session import lifecycle

                    async with group_lock(session):
                        current_group = read_group(session)
                        if current_group is None or current_group["status"] == "deleting":
                            return ToolOutput(success=False, error="group is unavailable")
                        lifecycle.guard(session)
                        worker = asyncio.create_task(self._execute_background(key, task, execution, manager))
                        manager.register_stream_task(child_session, worker)
                    return ToolOutput(success=True, data={
                        "execution_id": execution["execution_id"], "accepted": True,
                        "posted_to_group": False,
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
                worker = manager.get_stream_task(child_session)
                if worker is None or worker.done():
                    raise GroupError("NOT_RUNNING", "private-team worker is not running on this server")
                if action == "cancel":
                    reason = inputs.get("reason", "user_cancelled")
                    if not isinstance(reason, str):
                        raise GroupError("BAD_REQUEST", "reason must be text")
                    await store.update(record["execution_id"], phase="cancelling", cancellation_reason=reason)
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

    async def _execute_background(self, key: str, task: str, execution: dict, manager) -> None:
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions
        from jiuwenswarm.server.runtime.group_service import read_group

        store = GroupExecutions(self.context.session_id)
        try:
            await self._run(key, task, execution=execution)
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
            child_session = execution["team_session_id"]
            if manager.get_stream_task(child_session) is asyncio.current_task():
                manager.pop_stream_task(child_session)

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
            team_key = execution.get("private_team_key", key) if execution is not None else key
            child.team_name = f"group-expert-{team_key[:24]}"
            child_session = f"group-run-{team_key[:32]}"
            child.external_transport = None
            if child.workspace is not None:
                child.workspace = child.workspace.model_copy(
                    update={
                        "root_path": str(state_path.parent / team_key / "workspace"),
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

                async def record_progress(event):
                    await GroupExecutions(self.context.session_id).task_event(execution["execution_id"], event)

                async def watch_progress():
                    async for event in handler.events():
                        await record_progress(event)

                try:
                    async for chunk in Runner.run_agent_team_streaming(
                        child, {"query": task}, session=child_session,
                    ):
                        payload = getattr(chunk, "payload", None)
                        if not isinstance(payload, dict):
                            continue
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
