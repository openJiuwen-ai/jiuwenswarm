"""Persistent group management, separate from WebSocket transport and scheduling."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakValueDictionary

from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.group_chat.handler import sync_history
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from openjiuwen.agent_teams.schema.team import ExternalCliMemberSpec
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write
from openjiuwen.agent_teams.skill.file_lock import cross_process_file_lock
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwenswarm.common.protocol_ids import validate_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput
from jiuwenswarm.server.runtime import group_files
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

logger = logging.getLogger(__name__)
GROUP_METHODS = frozenset(
    {
        "group.create",
        "group.delete",
        "group.list",
        "group.get",
        "group.update",
        "group.member.add",
        "group.history.get",
        "group.execution.list",
        "group.execution.get",
        "group.file.rename",
        "group.file.delete",
        "group.history.clear",
    }
)
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def group_lock(group_id: str):
    """Keep writer ownership across processes, outside the deletable session directory."""
    validate_session_id(group_id)
    async with _locks.setdefault(group_id, asyncio.Lock()):
        guard = cross_process_file_lock(
            get_agent_sessions_dir().parent / "group-locks" / group_id
        )
        acquisition = asyncio.create_task(asyncio.to_thread(guard.__enter__))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            await acquisition
            await asyncio.to_thread(guard.__exit__, None, None, None)
            raise
        try:
            yield
        finally:
            await asyncio.to_thread(guard.__exit__, None, None, None)


class GroupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def group_path(group_id: str) -> Path:
    return get_agent_sessions_dir() / validate_session_id(group_id) / "group.json"


def normalize_group_member(member: dict) -> dict:
    """Read legacy roster fields without changing the stored or requested object."""
    if not isinstance(member, dict):
        raise GroupError("BAD_REQUEST", "invalid group member fields")
    result = copy.deepcopy(member)
    result["kind"] = {"expert": "agent_template", "expert_team": "agent_group"}.get(
        result.get("kind"), result.get("kind")
    )
    if "definition_id" in result:
        result.setdefault("id", result.pop("definition_id"))
    return result


def read_group(group_id: str) -> dict | None:
    path = group_path(group_id)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GroupError("GROUP_CORRUPT", "invalid persisted group") from exc
    required = {
        "group_id",
        "session_id",
        "team_name",
        "name",
        "description",
        "members",
        "created_at",
        "updated_at",
        "status",
        "user_id",
        "channel_id",
        "compiled",
        "receipts",
        "mode",
    }
    if (
        not isinstance(state, dict)
        or state.get("group_id") != group_id
        or not required <= state.keys()
    ):
        raise GroupError("GROUP_CORRUPT", "invalid persisted group")
    state.setdefault("configs", {})
    state.setdefault("avatar", "")
    state["members"] = [normalize_group_member(member) for member in state["members"]]
    state["receipts"] = {
        key: normalize_group_member(member) for key, member in state["receipts"].items()
    }
    retired_tools = {"swarm.group_adjust_task", "swarm.group_submit_task"}
    for member in state["compiled"].values():
        template = member.get("template")
        if template is not None:
            template["tools"] = [
                tool for tool in template.get("tools", []) if tool.get("type") not in retired_tools
            ]
    return state


def group_organization_teams(group_id: str) -> list[dict]:
    """Expert-team instances recorded for a swarm group, or an empty list."""
    try:
        state = read_group(group_id)
    except GroupError:
        return []
    if not isinstance(state, dict):
        return []
    teams = state.get("organization_teams")
    if not isinstance(teams, list):
        return []
    return [
        item
        for item in teams
        if isinstance(item, dict) and str(item.get("team_id") or "").strip()
    ]


def clear_group_organization_binding(group_id: str) -> None:
    """Forget the live organization registration. Plans and messages stay."""
    state = read_group(group_id)
    if state is None:
        return
    state.pop("organization_id", None)
    state["organization_teams"] = []
    state["updated_at"] = time.time()
    _save(state)


def group_organization_owner_team_ids(group_id: str) -> set[str]:
    """Team ids that own the organization created for this group chat."""
    return {
        str(item.get("team_id")).strip()
        for item in group_organization_teams(group_id)
        if item.get("role") == "owner"
    }


def mark_group_organization(
    group_id: str,
    organization_id: str,
    teams: list[dict],
) -> None:
    """Remember the organization created for this group. Extra keys stay readable."""
    state = read_group(group_id)
    if state is None:
        return
    state["organization_id"] = organization_id
    state["organization_teams"] = teams
    state["updated_at"] = time.time()
    _save(state)


def upsert_group_organization_team(
    group_id: str,
    organization_id: str,
    entry: dict,
) -> None:
    """Persist one proxy-to-instance binding without replacing the others."""
    state = read_group(group_id)
    if state is None:
        return
    previous = str(state.get("organization_id") or "")
    # A different organization must not keep the previous round's members.
    # Those rows would be invited into the new organization.
    if previous and previous != organization_id:
        stored = []
    else:
        stored = state.get("organization_teams") or []
    teams = [
        item
        for item in stored
        if isinstance(item, dict)
        and item.get("member_name") != entry.get("member_name")
    ]
    teams.append(copy.deepcopy(entry))
    state["organization_id"] = organization_id
    state["organization_teams"] = teams
    state["updated_at"] = time.time()
    _save(state)


def group_organization_plans(group_id: str) -> list[dict]:
    """Queued multi-expert runs for this group, oldest first."""
    try:
        state = read_group(group_id)
    except GroupError:
        return []
    plans = (state or {}).get("organization_plans")
    if not isinstance(plans, list):
        return []
    return [
        item
        for item in plans
        if isinstance(item, dict) and str(item.get("plan_id") or "").strip()
    ]


def add_group_organization_plan(group_id: str, plan: dict) -> bool:
    """Queue a run idempotently by the group message id."""
    state = read_group(group_id)
    if state is None:
        return False
    plans = group_organization_plans(group_id)
    if any(item["plan_id"] == plan.get("plan_id") for item in plans):
        return False
    state["organization_plans"] = [*plans, copy.deepcopy(plan)]
    state["updated_at"] = time.time()
    _save(state)
    return True


def group_direct_routes(group_id: str) -> list[dict]:
    """Messages that must stay off every organization, oldest first."""
    try:
        state = read_group(group_id)
    except GroupError:
        return []
    routes = (state or {}).get("direct_routes")
    if not isinstance(routes, list):
        return []
    return [
        item
        for item in routes
        if isinstance(item, dict) and str(item.get("plan_id") or "").strip()
    ]


def add_group_direct_route(group_id: str, route: dict) -> bool:
    """Remember one direct-reply message. A repeated message id is ignored."""
    state = read_group(group_id)
    if state is None:
        return False
    routes = group_direct_routes(group_id)
    if any(item.get("plan_id") == route.get("plan_id") for item in routes):
        return False
    routes = [*routes, copy.deepcopy(route)]
    if len(routes) > 100:
        routes = routes[-100:]
    state["direct_routes"] = routes
    state["updated_at"] = time.time()
    _save(state)
    return True


def update_group_organization_plan(
    group_id: str, plan_id: str, **fields
) -> None:
    state = read_group(group_id)
    if state is None:
        return
    plans = group_organization_plans(group_id)
    for item in plans:
        if item["plan_id"] == plan_id:
            item.update(copy.deepcopy(fields))
            break
    state["organization_plans"] = plans
    state["updated_at"] = time.time()
    _save(state)


def _save(state: dict) -> None:
    # Share the session deletion fence so a late writer cannot recreate its directory.
    with lifecycle.resource_lock("session", state["group_id"]):
        lifecycle.guard(state["group_id"])
        atomic_write(
            group_path(state["group_id"]), json.dumps(state, ensure_ascii=False)
        )


def _text(value, name: str, *, empty=False, maximum=2000) -> str:
    if not isinstance(value, str):
        raise GroupError("BAD_REQUEST", f"invalid {name}")
    if len(value) > maximum:
        raise GroupError("BAD_REQUEST", f"invalid {name}")
    value = value.strip()
    if not empty and not value:
        raise GroupError("BAD_REQUEST", f"invalid {name}")
    return value


def _page(params: dict) -> tuple[int, int]:
    limit, offset = params.get("limit", 50), params.get("offset", 0)
    valid_limit = type(limit) is int and 1 <= limit <= 200
    valid_offset = type(offset) is int and offset >= 0
    if not valid_limit or not valid_offset:
        raise GroupError(
            "BAD_REQUEST", "limit must be 1..200 and offset must be nonnegative"
        )
    return limit, offset


def _public(state: dict) -> dict:
    keys = (
        "group_id",
        "session_id",
        "team_name",
        "name",
        "description",
        "members",
        "configs",
        "avatar",
        "created_at",
        "updated_at",
        "status",
    )
    result = {}
    for key in keys:
        result[key] = copy.deepcopy(state[key])
    return result


_FINISHED_EXECUTION_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "timed_out", "stopped"}
)


def _seconds(timestamp) -> float:
    """History timestamps are milliseconds; tolerate second-based values in older records."""
    value = float(timestamp or 0)
    return value / 1000 if value > 1e12 else value


def _cleared(at: float, cleared_at) -> bool:
    return isinstance(cleared_at, (int, float)) and at <= cleared_at


def _execution_cleared(record: dict, state: dict) -> bool:
    return record.get("status") in _FINISHED_EXECUTION_STATUSES and _cleared(
        _seconds(record.get("created_at")), state.get("history_cleared_at")
    )


def _with_file_overrides(execution: dict, state: dict) -> dict:
    overrides = state.get("file_overrides") or {}
    if not overrides or not execution.get("attachments"):
        return execution
    return {**execution, "attachments": group_files.apply_file_overrides(execution["attachments"], overrides)}


async def resolve_group_backend(state: dict):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.agents.harness.team.team_manager import (
        _runner_team_runtime_manager,
    )

    runtime = _runner_team_runtime_manager(GLOBAL_RUNNER)
    entry = await runtime.pool.get(state["team_name"])
    if entry is None or entry.current_session_id != state["session_id"]:
        return None
    return entry.agent.team_backend


class GroupService:
    def __init__(
        self, runtime, *, manager_factory=None, backend_resolver=resolve_group_backend
    ):
        if manager_factory is None:
            from jiuwenswarm.agents.harness.team import get_team_manager

            manager_factory = get_team_manager
        self.runtime = runtime
        self.manager_factory = manager_factory
        self.backend_resolver = backend_resolver

    @staticmethod
    def _authorized(group_id: str, user_id: str, channel_id: str) -> dict:
        state = read_group(group_id)
        if (
            state is None
            or state["user_id"] != user_id
            or state["channel_id"] != channel_id
        ):
            raise GroupError("NOT_FOUND", "group not found")
        return state

    def owned(self, group_id: str, user_id: str, channel_id: str) -> dict:
        state = self._authorized(group_id, user_id, channel_id)
        lifecycle.guard(group_id)
        if state["status"] == "deleting":
            raise GroupError("GROUP_BUSY", "group is being deleted")
        return state

    async def _compile(self, state: dict, members: list[dict]):
        for member in members:
            if not isinstance(member, dict) or set(member) - {
                "kind",
                "id",
                "member_name",
                "display_name",
            }:
                raise GroupError("BAD_REQUEST", "invalid group member fields")
        manager = self.manager_factory(state["channel_id"])
        spec = await manager.get_swarm_enriched_team_spec(
            session_id=state["session_id"],
            mode=state["mode"],
            project_dir=state.get("project_dir") or None,
            user_id=state["user_id"],
            channel_id=state["channel_id"],
            request_metadata={"mode": state["mode"], "group_members": members},
        )
        from jiuwenswarm.agents.swarm.group_chat import RUN_TEAM
        from jiuwenswarm.agents.swarm.group_execution import START_TASK

        compiled = {}
        for member in spec.predefined_members:
            if member.member_name not in {item["member_name"] for item in members}:
                continue
            agent = spec.agents[member.member_name]
            template = copy.deepcopy(agent.agent_template_spec)
            if template is not None:
                template["tools"] = [
                    *template.get("tools", []),
                    *[
                        tool.model_dump(mode="json")
                        for tool in (agent.tools or [])
                        if getattr(tool, "type", None) in {RUN_TEAM, START_TASK}
                    ],
                ]
            compiled[member.member_name] = {
                "member": member.model_dump(mode="json"),
                "template": template,
            }
        return spec, compiled

    async def prepare_create(
        self, params: dict, *, user_id: str, channel_id: str, request_id: str
    ):
        name = _text(params.get("name"), "name", maximum=200)
        description = _text(params.get("description", ""), "description", empty=True)
        create_token = _text(
            params.get("create_token") or request_id, "create_token", maximum=256
        )
        avatar = _text(params.get("avatar", ""), "avatar", maximum=4096, empty=True)
        configs = params.get("configs", {})
        if not isinstance(configs, dict):
            raise GroupError("BAD_REQUEST", "configs must be an object")
        configs = copy.deepcopy(configs)
        members = params.get("members", [])
        if not isinstance(members, list):
            raise GroupError(
                "BAD_REQUEST", "members must be an array"
            )
        await self.runtime.start()
        prepared = await self.runtime.prepare_session_create(
            SessionCreateInput(
                channel_id=channel_id,
                create_token=create_token,
                persist_session=True,
                persist_session_supplied=True,
                mode="team",
                team_hint=True,
                title=name,
                user_id=user_id,
            )
        )
        group_id = prepared.result.session_id
        try:
            existing = read_group(group_id)
            if existing is not None:
                self.owned(group_id, user_id, channel_id)
                existing_config = (
                    existing["name"],
                    existing["description"],
                    existing["members"],
                    existing["configs"],
                    existing["avatar"],
                )
                if existing_config != (
                    name,
                    description,
                    members,
                    configs,
                    avatar,
                ):
                    raise GroupError(
                        "CONFLICT", "create_token was used for a different group"
                    )
                return _public(existing), prepared, False
            now = time.time()
            state = dict(
                group_id=group_id,
                session_id=group_id,
                team_name="",
                name=name,
                description=description,
                members=members,
                configs=configs,
                avatar=avatar,
                user_id=user_id,
                channel_id=channel_id,
                mode=prepared.result.canonical_mode,
                project_dir=prepared.result.project_dir,
                created_at=now,
                updated_at=now,
                status="ready",
                receipts={},
            )
            spec, compiled = await self._compile(state, members)
            state.update(
                team_name=spec.team_name,
                compiled=compiled,
                workspace_path=spec.workspace.root_path if spec.workspace else None,
            )
            update_session_metadata(
                session_id=group_id,
                group_members=members,
                conversation_type="group_chat",
                team_name=spec.team_name,
                touch_last_message_at=False,
                sync_write=True,
            )
            _save(state)
            return _public(state), prepared, True
        except BaseException:
            await self.runtime.abort_session_provision(prepared)
            raise

    def get(self, group_id, *, user_id, channel_id):
        from jiuwenswarm.server.runtime.group_questions import GroupQuestions

        return {**_public(self.owned(group_id, user_id, channel_id)),
                "pending_questions": GroupQuestions(group_id).pending()}

    def list(self, params, *, user_id, channel_id):
        limit, offset = _page(params)
        groups = []
        for path in get_agent_sessions_dir().glob("*/group.json"):
            try:
                state = self.owned(path.parent.name, user_id, channel_id)
            except (GroupError, lifecycle.LifecycleError):
                continue
            groups.append(_public(state))
        groups.sort(
            key=lambda state: (state["updated_at"], state["group_id"]), reverse=True
        )
        page_end = offset + limit
        return dict(
            groups=groups[offset:page_end],
            total=len(groups),
            limit=limit,
            offset=offset,
        )

    async def update(self, group_id, params, *, user_id, channel_id):
        async with group_lock(group_id):
            state = self.owned(group_id, user_id, channel_id)
            if set(params) - {"group_id", "session_id", "name", "description", "avatar"}:
                raise GroupError(
                    "BAD_REQUEST", "group.update only accepts name, description and avatar"
                )
            if "name" in params:
                state["name"] = _text(params["name"], "name", maximum=200)
            if "description" in params:
                state["description"] = _text(
                    params["description"], "description", empty=True
                )
            if "avatar" in params:
                state["avatar"] = _text(params["avatar"], "avatar", maximum=4096, empty=True)
            state["updated_at"] = time.time()
            _save(state)
            update_session_metadata(
                session_id=group_id,
                title=state["name"],
                touch_last_message_at=False,
                sync_write=True,
            )
            return _public(state)

    async def add_member(self, group_id, params, *, user_id, channel_id, request_id):
        manager = self.manager_factory(channel_id)
        async with manager.get_startup_lock(group_id):
            async with group_lock(group_id):
                state = self.owned(group_id, user_id, channel_id)
                member = params.get("member")
                receipt_id = _text(
                    params.get("client_request_id", request_id),
                    "client_request_id",
                    maximum=256,
                )
                previous = state["receipts"].get(receipt_id)
                if previous is not None and previous != member:
                    raise GroupError(
                        "CONFLICT", "client_request_id was used for a different member"
                    )
                names = [item["member_name"] for item in state["members"]]
                if previous is None:
                    if isinstance(member, dict) and member.get("member_name") in names:
                        raise GroupError("ALREADY_EXISTS", "member_name already exists")
                    proposed = [*state["members"], member]
                    _, compiled = await self._compile(state, proposed)
                    backend = await self.backend_resolver(state)
                    if backend is not None:
                        from jiuwenswarm.agents.swarm.group_chat import (
                            GROUP_MEMBER_TEMPLATE,
                        )

                        role = backend.group_chat_spec.agents.get("teammate")
                        if not any(
                            rail.type == GROUP_MEMBER_TEMPLATE
                            for rail in (role.rails or [])
                        ):
                            raise GroupError(
                                "GROUP_RESTART_REQUIRED",
                                "resume this group with the current runtime before adding members",
                            )
                        external = compiled[member["member_name"]]["member"].get(
                            "external_cli"
                        )
                        if external is not None:
                            requested = ExternalCliMemberSpec.model_validate(
                                compiled[member["member_name"]]["member"]
                            ).external_cli
                            current = backend.external_cli_config(requested.cli_agent)
                            if current is None or current.model_dump(
                                mode="json"
                            ) != requested.model_dump(mode="json"):
                                raise GroupError(
                                    "GROUP_RESTART_REQUIRED",
                                    "the new CLI configuration requires a cold runtime recovery; "
                                    "it cannot replace a running CLI configuration",
                                )
                    # Adding a member must not silently upgrade existing experts.
                    compiled = {**compiled, **state["compiled"]}
                    state.update(
                        members=proposed, compiled=compiled, updated_at=time.time()
                    )
                    state["receipts"][receipt_id] = member
                    # Persist capabilities before exposing a routable DB identity.
                    _save(state)
                update_session_metadata(
                    session_id=group_id,
                    group_members=state["members"],
                    touch_last_message_at=False,
                    sync_write=True,
                )
                backend = await self.backend_resolver(state)
                if backend is not None:
                    await sync_group_roster(state, backend)
                return dict(
                    group_id=group_id,
                    member=member,
                    status="registered",
                    event_type="group.member.added",
                )

    async def history(self, group_id, params, *, user_id, channel_id):
        state = self.owned(group_id, user_id, channel_id)
        limit, offset = _page(params)
        cleared_at = state.get("history_cleared_at")
        messages = [
            message for message in await self._messages(state)
            if not _cleared(_seconds(message.timestamp), cleared_at)
        ]
        overrides = state.get("file_overrides") or {}
        page = []
        for message in messages[offset:offset + limit]:
            item = message.model_dump(mode="json")
            item["attachments"] = group_files.apply_file_overrides(item.get("attachments"), overrides)
            page.append(item)
        page_end = offset + limit
        return dict(
            group_id=group_id,
            messages=page,
            total=len(messages),
            limit=limit,
            offset=offset,
            next_offset=page_end if page_end < len(messages) else None,
        )

    async def _messages(self, state):
        group_id = state["group_id"]
        backend = await self.backend_resolver(state)
        if backend is not None:
            token = set_session_id(state["session_id"])
            try:
                messages = await sync_history(
                    await backend.group_conversation(), backend.message_manager
                )
            finally:
                reset_session_id(token)
        else:
            path = GroupConversationLog(
                state["team_name"], group_id, workspace_path=state.get("workspace_path")
            ).history_path
            lines = (
                path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            )
            messages = [
                ConversationMessage.model_validate_json(line)
                for line in lines
                if line.strip()
            ]
        if any(
            m.team_name != state["team_name"] or m.session_id != group_id
            for m in messages
        ):
            raise GroupError("GROUP_CORRUPT", "history scope does not match group")
        messages.sort(key=lambda message: (message.timestamp, message.message_id))
        return messages

    async def clear_history(self, group_id, *, user_id, channel_id):
        """Hide everything published so far; active task cards stay visible until they finish."""
        async with group_lock(group_id):
            state = self.owned(group_id, user_id, channel_id)
            state["history_cleared_at"] = time.time()
            _save(state)
        return dict(group_id=group_id, cleared=True, history_cleared_at=state["history_cleared_at"])

    async def _attachment_paths(self, state) -> set[str]:
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions

        messages = [message.model_dump(mode="json") for message in await self._messages(state)]
        executions = list(GroupExecutions(state["group_id"]).read().values())
        return group_files.group_attachment_paths(messages, executions)

    async def _owned_file(self, group_id, params, *, user_id, channel_id):
        state = self.owned(group_id, user_id, channel_id)
        path = group_files.validate_file_path(params.get("path"))
        if path not in await self._attachment_paths(state):
            raise GroupError("NOT_FOUND", "file not found in this group")
        return state, path

    async def file_rename(self, group_id, params, *, user_id, channel_id):
        name = group_files.validate_file_name(params.get("name"))
        async with group_lock(group_id):
            state, path = await self._owned_file(group_id, params, user_id=user_id, channel_id=channel_id)
            overrides = group_files.record_rename(state, path, name)
            _save(state)
        return dict(group_id=group_id, path=path, name=name, file_overrides=overrides)

    async def file_delete(self, group_id, params, *, user_id, channel_id):
        async with group_lock(group_id):
            state, path = await self._owned_file(group_id, params, user_id=user_id, channel_id=channel_id)
            overrides = group_files.record_delete(state, path)
            # Persist the hidden state first so a failed unlink never resurfaces the file.
            _save(state)
            removed = group_files.remove_workspace_file(state["team_name"], path)
        return dict(group_id=group_id, path=path, deleted=True, file_removed=removed, file_overrides=overrides)

    async def prepare_task(self, group_id, params, *, user_id, channel_id, request_id):
        from jiuwenswarm.agents.swarm.group_execution import TASK_POLICY
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions

        group = self.owned(group_id, user_id, channel_id)
        members = params.get("mentions", [])
        if not isinstance(members, list):
            raise GroupError("BAD_REQUEST", "task mentions must be an array")
        backend = await self.backend_resolver(group)
        if backend is not None:
            agents = backend.group_chat_spec.agents
            for name in members:
                if not isinstance(name, str):
                    raise GroupError("BAD_REQUEST", "task members must be strings")
                agent = agents.get(name) or agents.get("teammate")
                rails = agent.rails if agent is not None else []
                if not any(getattr(rail, "type", None) == TASK_POLICY for rail in (rails or [])):
                    raise GroupError("GROUP_RESTART_REQUIRED", "task lifecycle policy requires cold recovery")
        return await GroupExecutions(group_id).create(
            params.get("mentions", []), params.get("query"), params.get("client_message_id") or request_id,
            defer_card=True,
        )

    async def execution_list(self, group_id, params, *, user_id, channel_id):
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions, public_execution

        state = self.owned(group_id, user_id, channel_id)
        limit, offset = _page(params)
        store = GroupExecutions(group_id)
        await store.reconcile()
        records = []
        for record in store.read().values():
            if record.get("presentation", "card") == "card" and not _execution_cleared(record, state):
                records.append(record)
        records.sort(key=lambda item: (item["created_at"], item["execution_id"]))
        page_end = offset + limit
        return {
            "group_id": group_id,
            "executions": [
                _with_file_overrides(public_execution(item), state) for item in records[offset:page_end]
            ],
            "total": len(records), "limit": limit, "offset": offset,
        }

    async def execution_get(self, group_id, params, *, user_id, channel_id):
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions, public_execution

        state = self.owned(group_id, user_id, channel_id)
        execution_id = _text(params.get("execution_id"), "execution_id", maximum=256)
        store = GroupExecutions(group_id)
        await store.reconcile()
        record = store.get(execution_id)
        if record.get("presentation", "card") != "card" or _execution_cleared(record, state):
            raise GroupError("NOT_FOUND", "task card not found")
        return {"group_id": group_id, **_with_file_overrides(public_execution(record), state)}

    async def delete(self, group_id, *, user_id, channel_id):
        async with group_lock(group_id):
            state = self._authorized(group_id, user_id, channel_id)
            if state["status"] != "deleting":
                lifecycle.guard(group_id)
                state["status"] = "deleting"
                _save(state)
            try:
                from jiuwenswarm.server.runtime.group_execution import GroupExecutions

                for execution in GroupExecutions(group_id).read().values():
                    child_session = execution.get("team_session_id")
                    if child_session:
                        await self.manager_factory(channel_id).stop_session_runtime(
                            child_session, reason="group.delete.private",
                        )
                if state.get("organization_id"):
                    from jiuwenswarm.agents.swarm.group_org_bootstrap import (
                        dissolve_group_organization,
                    )

                    await dissolve_group_organization(group_id)
                await self.manager_factory(channel_id).stop_session_runtime(
                    group_id, reason="group.delete"
                )
                log = GroupConversationLog(
                    state["team_name"],
                    group_id,
                    workspace_path=state.get("workspace_path"),
                )
                removed_teams = set()
                for path in (log.history_path.parent / "team-runs").glob("*.json"):
                    run = json.loads(path.read_text(encoding="utf-8"))
                    identity = (run.get("team_name"), run.get("session_id"))
                    if identity in removed_teams:
                        continue
                    if run.get("team_name") and run.get("session_id"):
                        await self.manager_factory(channel_id).stop_session_runtime(
                            run["session_id"], reason="group.delete.private",
                        )
                        removed = await Runner.delete_agent_team(
                            team_name=run["team_name"],
                            session_ids=[run["session_id"]],
                            force=True,
                        )
                        if removed:
                            removed_teams.add(identity)
                        if not removed:
                            raise GroupError(
                                "DELETE_FAILED",
                                "private expert team could not be deleted",
                            )
                result = await self.runtime.delete_session(
                    channel_id=channel_id, session_id=group_id
                )
                if not result.ok:
                    raise GroupError(
                        result.error_code or "DELETE_FAILED",
                        result.error_message or "group deletion failed",
                    )
                group_path(group_id).unlink(missing_ok=True)
                return dict(group_id=group_id, deleted=True)
            except BaseException:
                if group_path(group_id).exists():
                    state["status"] = "ready"
                    try:
                        _save(state)
                    except lifecycle.LifecycleError:
                        # A pending Runtime deletion owns the fence; retain its recovery marker.
                        logger.info(
                            "group deletion remains fenced for recovery: %s", group_id
                        )
                raise


async def sync_group_roster(state: dict, backend) -> None:
    """Register persisted members without modifying the running immutable spec."""
    if backend.conversation_mode != "group_chat":
        raise GroupError("MODE_MISMATCH", "group requires a group_chat runtime")
    token = set_session_id(state["session_id"])
    try:
        await backend.db.initialize()
        if await backend.db.team.get_team(backend.team_name) is None:
            await backend.build_team(
                display_name=state["name"],
                desc=state["description"],
                leader_display_name=backend.leader_member_name,
                leader_desc="",
            )
        for name, definition in state["compiled"].items():
            if await backend.db.member.get_member(name, backend.team_name) is not None:
                continue
            raw = definition["member"]
            common = dict(
                member_name=name,
                display_name=raw["display_name"],
                desc=raw["desc"],
                prompt=raw["prompt"],
                allocation=backend.allocate_model(raw.get("model_name")),
            )
            if "external_cli" in raw:
                member = ExternalCliMemberSpec.model_validate(raw)
                predefined = any(
                    m.member_name == name and isinstance(m, ExternalCliMemberSpec)
                    for m in backend.group_chat_spec.predefined_members
                )
                if predefined:
                    result = await backend.spawn_member(
                        **common,
                        role=TeamRole.EXTERNAL_CLI,
                        mode=backend.teammate_mode,
                        cli_agent=member.external_cli.cli_agent,
                        agent_card=AgentCard(
                            id=f"{backend.team_name}_{name}",
                            name=raw["display_name"],
                            description=raw["desc"],
                        ),
                    )
                    await backend.restore_external_cli_specs_from_db()
                else:
                    result = await backend.spawn_external_cli_agent(
                        **common, cli_agent=member.external_cli.cli_agent
                    )
            else:
                result = await backend.spawn_member(
                    **common,
                    mode=backend.teammate_mode,
                    agent_card=AgentCard(
                        id=f"{backend.team_name}_{name}",
                        name=raw["display_name"],
                        description=raw["desc"],
                    ),
                )
            if not result.ok:
                raise GroupError("MEMBER_REGISTRATION_FAILED", result.reason)
            await backend.message_manager.send_message(
                content=f"Group member joined: {raw['display_name']} ({name}).",
                to_member_name=backend.leader_member_name,
                from_member_name="user",
            )
    finally:
        reset_session_id(token)
