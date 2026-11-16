"""Invocation-local routing identity for a consumed public group message."""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TURN_KEY = "swarm_group_turn"
TOKEN_KEY = "swarm_group_turn_token"
STATE_KEY = "swarm_group_turn_admission"
_NOTICE = re.compile(
    r"\A(?:群聊消息通知。\n本次群聊时间范围（毫秒）：[^\n]+\n"
    r"同时包含本次 @ 的触发消息：([\w-]+)。\n群聊历史文件："
    r"|Group message notice\.\nGroup history time range \(milliseconds\):[^\n]+\n"
    r"The triggering mention is also included: ([\w-]+)\.\nGroup history file: )"
)


@dataclass(frozen=True)
class GroupTurn:
    session_id: str
    member_name: str
    client_message_id: str | None
    route: str
    error: str | None = None
    mixed_mentions: bool = False


_CURRENT_TURN: ContextVar[GroupTurn | None] = ContextVar("swarm_current_group_turn", default=None)


def current_group_turn(session_id: str, member_name: str) -> GroupTurn | None:
    turn = _CURRENT_TURN.get()
    if turn is not None and (turn.session_id, turn.member_name) == (session_id, member_name):
        return turn
    return None


def bind_group_turn(turn: GroupTurn | None) -> Token:
    return _CURRENT_TURN.set(turn)


def reset_group_turn(token: Token) -> None:
    _CURRENT_TURN.reset(token)


def _verified_public_broadcast(message: Any, meta: Any, trigger_id: str, member_name: str, team_name: str) -> bool:
    """Accept only the persisted broadcast that this member was actually mentioned in."""
    if message is None or not isinstance(meta, dict):
        return False
    if meta.get("type") != "group_chat" or message.team_name != team_name:
        return False
    if message.from_member_name != "user" or not message.broadcast:
        return False
    if message.message_id != trigger_id:
        return False
    client_message_id = meta.get("client_message_id")
    if not isinstance(client_message_id, str) or not client_message_id.strip():
        return False
    mentions = meta.get("mentions")
    return isinstance(mentions, list) and member_name in mentions


def is_group_notice(part: str) -> bool:
    return _NOTICE.match(str(part)) is not None


async def expand_public_group_inputs(parts: list[str], backend: Any, member_name: str) -> None:
    """Normalize public broadcasts delivered by the ordinary mailbox handler.

    Only the outer envelope identifies a message; its body and archived
    excerpts are never searched for routing identity. Persisted metadata is
    authoritative, and unverified public input must not inherit an old turn.
    """
    from openjiuwen.agent_teams.group_chat.handler import context_for

    for index, part in enumerate(parts):
        header = re.match(r'\A<team-inbound\b[^>]*>', str(part))
        if header is None:
            continue
        try:
            attrs = ET.fromstring(header.group(0)[:-1] + " />").attrib
        except ET.ParseError:
            parts[index] = "Group message notice.\nInvalid inbound envelope."
            continue
        if attrs.get("from") != "user" or attrs.get("type") != "broadcast":
            continue
        # A failed lookup is public input with unknown identity, not an
        # internal reminder that may keep the previous message binding.
        parts[index] = "Group message notice.\nUnverified public broadcast."
        trigger_id = attrs.get("message_id")
        if not trigger_id:
            continue
        try:
            message = await backend.db.message.get_message(trigger_id)
            raw = getattr(message, "meta", None)
            meta = json.loads(raw) if isinstance(raw, str) else raw
            if not _verified_public_broadcast(
                message, meta, trigger_id, member_name, backend.team_name,
            ):
                continue
            parts[index] = await context_for(backend, member_name, message)
        except (OSError, ValueError, AttributeError, TypeError):
            continue


def _prepend_steering(ctx: Any, surplus: list[str]) -> None:
    """Restore consumed input before the backlog without losing runtime contexts."""
    from openjiuwen.core.single_agent.rail.base import SteeringInput

    if ctx.steering_queue is None:
        ctx.bind_steering_queue(asyncio.Queue())
    missing = object()
    original_context = ctx.extra.get("run_context", missing)
    if original_context is not missing:
        surplus = [part if isinstance(part, SteeringInput) else SteeringInput(part, run_context=original_context)
                   for part in surplus]
    remaining = []
    try:
        while ctx.has_pending_steering():
            ctx.extra.pop("run_context", None)
            batch = ctx.drain_steering()
            if "run_context" in ctx.extra:
                batch = [SteeringInput(part, run_context=ctx.extra["run_context"]) for part in batch]
            remaining.extend(batch)
    finally:
        if original_context is missing:
            ctx.extra.pop("run_context", None)
        else:
            ctx.extra["run_context"] = original_context
    for part in [*surplus, *remaining]:
        ctx.push_steering(part)


def _unaccepted_organization(turn: GroupTurn | None) -> bool:
    if turn is None or turn.route != "organization" or not turn.client_message_id:
        return False
    from jiuwenswarm.agents.swarm.group_org_bootstrap import (
        organization_plan_accepted, recorded_organization_plan,
    )

    from jiuwenswarm.agents.swarm.group_route import candidate

    item = candidate(turn.session_id, turn.client_message_id)
    if item is not None and item["owner_member"] == turn.member_name:
        if item["status"] in {"pending", "chat", "clarification"}:
            return True
    plan = recorded_organization_plan(turn.session_id, turn.member_name, turn.client_message_id)
    return (
        plan is not None and plan.get("status") in {"pending", "running"}
        and not organization_plan_accepted(turn.session_id, turn.client_message_id, turn.member_name)
    )


def _new_organization_must_wait(
    first: GroupTurn | None, current: GroupTurn | None, suspended: list,
) -> bool:
    """Hold a new organization while an admitted turn is still unfinished."""
    if first is None or first.route != "organization":
        return False
    if _unaccepted_organization(current) and first.client_message_id != current.client_message_id:
        return True
    if current is None or current.route != "direct":
        return False
    return bool(suspended)


def restore_suspended_organization(ctx: Any) -> None:
    """Resume an unaccepted organization only after the intervening direct turn yields."""
    turn = getattr(ctx, "extra", {}).get(TURN_KEY)
    state = getattr(ctx, "extra", {}).get(STATE_KEY)
    if turn is None or turn.route != "direct" or not state:
        return
    suspended = state.get("suspended", [])
    pending = [notice for previous, notice in suspended if _unaccepted_organization(previous)]
    suspended.clear()
    if pending:
        _prepend_steering(ctx, pending)


async def bind_consumed_group_turn(ctx: Any, backend: Any, session_id: str, member_name: str, group: dict) -> None:
    """Keep organization notices separate without reordering the input backlog."""
    parts = ctx.inputs.parts
    if getattr(ctx.inputs, "source", None) == "resume":
        return
    from jiuwenswarm.agents.swarm.group_route import expand_route_notices

    await expand_route_notices(parts, backend, session_id, member_name)
    await expand_public_group_inputs(parts, backend, member_name)
    notice_parts = [part for part in parts if str(part).startswith(("群聊消息通知。", "Group message notice."))]
    if not notice_parts:
        # Internal reminders and interrupt answers do not introduce another
        # public message. Keep the consumed message identity, if one exists.
        return
    if any(not is_group_notice(part) for part in notice_parts):
        ctx.extra[TURN_KEY] = GroupTurn(
            session_id, member_name, None, "unknown", "The group notice has no verifiable trigger header.",
        )
        return
    notices = []
    for index, part in enumerate(parts):
        if is_group_notice(part):
            turn = await resolve_group_turn(backend, session_id, member_name, group, [part])
            notices.append((index, turn))
    state = ctx.extra.setdefault(STATE_KEY, {"notice": None, "suspended": []})
    state["suspended"][:] = [item for item in state["suspended"] if _unaccepted_organization(item[0])]
    current = ctx.extra.get(TURN_KEY)
    first = notices[0][1] if notices else None
    if _new_organization_must_wait(first, current, state["suspended"]):
        # Keep the model working on its admitted request until its proxy
        # handoff completes. A new Organization must not strand its predecessor.
        direct = next(((index, turn) for index, turn in notices if turn.route == "direct"), None)
        if direct is None:
            _prepend_steering(ctx, list(parts))
            parts.clear()
            return
        # A direct request in the same consumed batch must not be held behind
        # the organization admission barrier either.
        index, first = direct
        current_input = parts[index]
        _prepend_steering(ctx, [*parts[:index], *parts[index + 1:]])
        parts[:] = [current_input]
        notices = [(0, first)]
    if first is not None and first.route == "direct" and _unaccepted_organization(current):
        previous_notice = state.get("notice")
        if previous_notice is not None and not any(previous == current for previous, _ in state["suspended"]):
            state["suspended"].append((current, previous_notice))
    if len(notices) > 1 and any(turn.route == "organization" for _, turn in notices):
        # Nothing may await between draining and restoring the queue: new
        # arrivals must remain behind the already-consumed surplus input.
        split = notices[1][0]
        surplus = list(parts[split:])
        del parts[split:]
        _prepend_steering(ctx, surplus)
        ctx.extra[TURN_KEY] = notices[0][1]
    elif len(notices) > 1 and all(turn.route == "direct" for _, turn in notices):
        # Direct-message batching is existing behavior; only organization
        # requests require one invocation-local plan identity.
        ctx.extra[TURN_KEY] = GroupTurn(
            session_id, member_name, None, "direct",
            mixed_mentions=any(turn.mixed_mentions for _, turn in notices),
        )
    else:
        ctx.extra[TURN_KEY] = await resolve_group_turn(backend, session_id, member_name, group, parts)
    if notices:
        from openjiuwen.core.single_agent.rail.base import SteeringInput

        notice = parts[notices[0][0]]
        if "run_context" in ctx.extra:
            notice = SteeringInput(notice, run_context=ctx.extra["run_context"])
        state["notice"] = notice


async def resolve_group_turn(
    backend: Any, session_id: str, member_name: str, group: dict, parts: list[str],
) -> GroupTurn:
    """Validate the notice consumed now against its exact persisted message."""
    def unknown(error: str) -> GroupTurn:
        return GroupTurn(session_id, member_name, None, "unknown", error)

    triggers = set()
    for part in parts:
        match = _NOTICE.match(str(part))
        if match:
            triggers.add(match.group(1) or match.group(2))
    if len(triggers) != 1:
        return unknown("A single triggering group message is required; no message was guessed.")
    trigger = next(iter(triggers))
    try:
        conversation = await backend.group_conversation()
        rows = Path(conversation.history_path).read_text(encoding="utf-8").splitlines()
        message = None
        for line in rows:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("message_id") == trigger:
                message = row
                break
    except (OSError, ValueError, AttributeError, TypeError):
        return unknown("The triggering group message could not be verified.")
    if message is None or message.get("sender") != "user":
        return unknown("The trigger is not a persisted user message.")
    mentions = message.get("mentions") or []
    client_id = message.get("client_message_id")
    if member_name not in mentions or not isinstance(client_id, str) or not client_id:
        return unknown("The trigger does not address this member or lacks its stable identity.")
    members = {item["member_name"]: item for item in group.get("members", [])}
    selected = [members.get(name) for name in dict.fromkeys(mentions)]
    organization = len(selected) >= 2 and all(
        member is not None and member.get("kind") == "agent_group" for member in selected
    )
    kinds = {member.get("kind") for member in selected if member is not None}
    return GroupTurn(
        session_id, member_name, client_id, "organization" if organization else "direct",
        mixed_mentions={"agent_group", "agent_template"}.issubset(kinds),
    )
