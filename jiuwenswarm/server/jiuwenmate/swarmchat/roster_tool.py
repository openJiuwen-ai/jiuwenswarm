"""List members of an authorized SwarmChat or Feishu group."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, register_tool_provider

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.swarmchat.membership import groups_for, require_membership, roster

PROVIDER = "jiuwenmate.swarmchat_roster"
TOOL_NAME = "list_group_members"
_CONTEXT_KEY = PROVIDER
_UNAVAILABLE = "The requested group is unavailable. Only SwarmChat groups this colleague belongs to can be listed."


def _tool_inputs(inputs: Any) -> dict[str, Any]:
    if isinstance(inputs, dict):
        return inputs
    dump = getattr(inputs, "model_dump", None)
    value = dump() if callable(dump) else None
    return value if isinstance(value, dict) else {}


def _unavailable(reason: str = _UNAVAILABLE) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


class GroupRosterTool(Tool):
    """Read-only roster lookup; authorization matches cross-group SwarmChat message search."""

    def __init__(self, port: Any, sessions: Any, execution: Any, im_port: Any = None) -> None:
        super().__init__(ToolCard(
            id="jiuwenmate-swarmchat-roster",
            name=TOOL_NAME,
            description=(
                "List every member of a SwarmChat group from its authoritative roster, including members who "
                "never spoke: display name, kind (expert, expert team or AI colleague), description and the exact "
                "[[agent:...]] mention token. Omit both arguments for the current group; pass group_id or "
                "group_name for another SwarmChat group you belong to. Use this instead of inferring membership "
                "from message senders. For Feishu, set platform=feishu and use the ID or name in the group "
                "directory. If there is exactly one authorized Feishu group, platform alone is sufficient. "
                "This is read-only; do not fall back to bash or CLI if Feishu cannot supply the roster."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["swarmchat", "feishu"]},
                    "group_id": {"type": "string", "maxLength": 512},
                    "group_name": {"type": "string", "maxLength": 512},
                },
                "additionalProperties": False,
            },
        ))
        self._port = port
        self._sessions = sessions
        self._execution = execution
        self._im_port = im_port

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        data = _tool_inputs(inputs)
        group_id, group_name = data.get("group_id"), data.get("group_name")
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (group_id, group_name)):
            return {"status": "invalid", "reason": "group_id and group_name must be non-empty strings."}
        platform = data.get("platform", "swarmchat")
        if platform not in ("swarmchat", "feishu"):
            return {"status": "invalid", "reason": "Supported platforms: swarmchat, feishu."}
        try:
            if platform == "feishu":
                return await self._feishu_roster(group_id, group_name)
            return await asyncio.to_thread(self._lookup, group_id, group_name)
        except PersistentAgentError:
            return _unavailable()

    async def _feishu_roster(self, group_id: str | None, group_name: str | None) -> dict[str, Any]:
        from openjiuwen.harness.persistent_agent import GroupMessageQuery

        from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import feishu_group_roster

        denied = "Feishu membership could not be verified for this colleague."
        if self._im_port is None:
            return _unavailable(denied)
        try:
            await asyncio.to_thread(self._port.validate_execution, self._execution)
            query = GroupMessageQuery(
                platforms=("feishu",), group_ids=(group_id,) if group_id else (),
                group_names=(group_name,) if group_name else (),
            )
            sources = await self._im_port.sharing_sources(self._execution, query)
        except Exception:
            return _unavailable(denied)
        if len(sources) != 1:
            return _unavailable("Specify one authorized Feishu group by its group ID.")
        source = sources[0]
        try:
            names = await asyncio.to_thread(feishu_group_roster, source["platform_chat_id"], source["app_id"])
            if names is None:
                return _unavailable("Feishu did not provide a complete roster. Check the bot's member-list access.")
            await asyncio.to_thread(self._port.validate_execution, self._execution)
            exact = GroupMessageQuery(platforms=("feishu",), group_ids=(source["group_id"],))
            current = await self._im_port.sharing_sources(self._execution, exact)
        except Exception:
            return _unavailable(denied)
        if len(current) != 1 or any(current[0][key] != source[key] for key in ("app_id", "platform_chat_id")):
            return _unavailable(denied)
        return {
            "status": "found", "platform": "feishu", "group_id": source["group_id"],
            "group_name": source["name"], "complete": True, "member_count": len(names),
            "members": [{"display_name": name} for name in names.values()],
        }

    def _lookup(self, group_id: str | None, group_name: str | None) -> dict[str, Any]:
        execution = self._execution
        agent_id = self._port.validate_execution(execution)
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        current, owner = None, None
        if session.conversation.kind == "swarm_group":
            current, _ = require_membership(session.conversation.key, agent_id)
            owner = current["user_id"]
        if group_id is None and group_name is None:
            if current is None:
                return {"status": "invalid", "reason": "Specify group_id or group_name outside a SwarmChat group."}
            group = current
        else:
            matches = []
            for item in groups_for(agent_id, owner=owner):
                if group_id is not None and item["group_id"] != group_id:
                    continue
                if group_name is not None and item["name"] != group_name:
                    continue
                matches.append(item)
            if len(matches) > 1:
                return _unavailable("The group name is ambiguous; specify group_id.")
            if not matches:
                return _unavailable()
            group = matches[0]
        # Membership may change while reading; release the roster only if it still holds.
        group, _ = require_membership(group["group_id"], agent_id)
        members = roster(group, self_agent_id=agent_id, description_limit=None)
        return {
            "status": "found", "platform": "swarmchat", "group_id": group["group_id"], "group_name": group["name"],
            "owner": "the user (human group owner; not listed as an AI member)",
            "complete": True, "member_count": len(members),
            "members": [
                {key: item[key] for key in ("display_name", "kind", "description", "mention", "is_self")}
                for item in members
            ],
        }

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield await self.invoke(inputs, **kwargs)


async def prepare_group_roster_invocation(spec, context, record, sessions, service):
    """Expose the roster tool only to a local colleague that belongs to at least one SwarmChat group."""
    if service is None or sessions is None or record.namespace_id != "local":
        return spec
    from jiuwenswarm.server.jiuwenmate.swarmchat.port import SwarmChatGroupChatPort

    execution = context.execution
    port = SwarmChatGroupChatPort(sessions, service, None).bind(execution.namespace_id, execution.agent_id)
    try:
        agent_id = await asyncio.to_thread(port.validate_execution, execution)
    except PersistentAgentError:
        return spec
    if not await asyncio.to_thread(groups_for, agent_id):
        return spec
    tools = list(spec.tools or [])
    if any((item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == PROVIDER
           for item in tools):
        raise PersistentAgentError("duplicate SwarmChat roster tool declaration", code="INVALID_CONFIG")
    im_port = getattr(getattr(context, "ports", None), "group_chat", None)
    context.extras[_CONTEXT_KEY] = (port, sessions, im_port)
    runtime_spec = spec.model_copy(update={"tools": [*tools, BuiltinToolSpec(type=PROVIDER)]})
    # Same as other runtime-only host tools: keep the execution identity aligned with the effective spec.
    context.execution = replace(context.execution, config_fingerprint=runtime_spec.fingerprint())
    return runtime_spec


def build_group_roster_tool(params: dict[str, Any], context):
    if params or _CONTEXT_KEY not in context.extras:
        raise ValueError("The SwarmChat roster tool requires a colleague runtime")
    port, sessions, im_port = context.extras[_CONTEXT_KEY]
    return GroupRosterTool(port, sessions, context.execution, im_port)


register_tool_provider(PROVIDER, build_group_roster_tool)
