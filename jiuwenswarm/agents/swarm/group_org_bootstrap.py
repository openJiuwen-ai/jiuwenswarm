# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coordinate expert-team proxies into one resident Team Organization.

The group message keeps its mentions, so agent-core wakes the proxies through
the normal group-chat path. Each proxy calls run_team and launches its own
expert-team instance. The first mentioned instance creates and owns the
organization; the outer group team never joins it.

run_team itself returns as soon as that instance is ready. Tool calls are cut
off at 300 seconds, so the Owner's wait and the group post run in a background
task. Another run_team for that same live plan returns the in-progress result.
A message that also mentions a single expert is marked as a direct reply and
does not open an organization. Each proxy turn stays bound to the message that
woke it, so a later message cannot steal an organization that has not posted
its result. Expert teams named on that later message wait, then answer on the
ordinary single-team path. A single expert who is not in the live organization
may reply immediately.
A later multi-@ message keeps its own plan. When that plan shares a member
with an older round that is still pending or running, recording it schedules
the Owner. The background task starts the new plan only after the older round
finishes and releases the shared instances. A proxy that is not in the older
plan queues too; it does not bind those instances early. If this proxy has not
joined that older plan yet, run_team joins it first. An early attempt that
fails leaves the new plan queued.
The join wait does not fail a proxy whose group turn for this message is still
running. A run_team for that same multi-@ message never opens a private team,
including after the plan has already ended.

After the Owner instance is bound, one organization execution card is written.
Each row is an organization child task the Owner published, labeled with that
task's title. The Owner background updates the card from the task pool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMBER_JOIN_TIMEOUT_SECONDS = 120.0
# Age at which a plan with nobody waiting on it becomes expired.
# A running Owner background or a live queue for that plan is not subject to this clock.
PLAN_STALE_SECONDS = 3720.0
RESULT_GRACE_SECONDS = 180.0
OWNER_START_WATCHDOG_SECONDS = 90.0
POLL_SECONDS = 3.0
_JOIN_POLL_SECONDS = 1.0
_ACTIVE_PROXY_STATUSES = frozenset({"busy", "starting"})

_locks: dict[str, asyncio.Lock] = {}
_plan_events: dict[tuple[str, str], asyncio.Event] = {}
_member_runs: dict[tuple[str, str, str], asyncio.Task] = {}
_accepted_plans: set[tuple[str, str, str]] = set()
_queued_plans: dict[tuple[str, str, str], asyncio.Task] = {}
_proxy_backends: dict[tuple[str, str], Any] = {}
_proxy_tools: dict[tuple[str, str], Any] = {}
_owner_runs: dict[tuple[str, str], asyncio.Task] = {}
_followup_runs: dict[tuple[str, str], asyncio.Task] = {}
_member_turns: dict[tuple[str, str], list[str]] = {}
_deferred_direct: dict[tuple[str, str, str], dict] = {}

_QUEUED_NOTE = (
    "This multi-expert request is queued behind an older Team Organization. "
    "Do not call run_team again and do not post a group message. It starts "
    "when the older run finishes."
)
_CLOSED_PLAN_NOTE = (
    "This multi-expert round has already ended. Do not open a private team "
    "and do not post a group message."
)
_IN_PROGRESS_NOTE = (
    "The Team Organization is already running. Do not call run_team again "
    "and do not post a group message. The Owner posts the final report."
)
_DIRECT_WAIT_NOTE = (
    "An older Team Organization has not posted its result yet. Do not call "
    "run_team again and do not post a group message. This request does not "
    "open an organization. It runs on the ordinary single-team path after "
    "that result is posted."
)

_REPLY_BLOCK = (
    "A multi-expert Team Organization is in progress. Only the Owner posts "
    "the group reply. Do not call group_send_message or send_file. Call "
    "run_team so this expert team joins the organization, then wait."
)

_MEMBER_SILENCE = (
    "\n\n[SWARM GROUP ORGANIZATION MEMBER — DO NOT REPLY]\n"
    "This Team is a member of a Team Organization, not the Owner. Do not call "
    "build_team and do not research or draft before an organization child is "
    "claimed. If no OPEN child requires your group-member capability, end this "
    "turn now; a later wake brings the task. When that child is present, claim "
    "it with org_claim_task and do only that child. Do not post to the group, "
    "do not call group_send_message, and do not send a file to the group. The "
    "Owner posts the only group reply."
)

_OWNER_PROMPT_SUFFIX = (
    "\n\n[SWARM GROUP ORGANIZATION OWNER — MANDATORY]\n"
    "This Team is the Owner of an already-created Team Organization. The other "
    "mentioned expert Teams are organization members, not teammates to spawn. "
    "If the Team bootstrap requires build_team, call it once only to initialize "
    "this Team; after it returns, do not use swarmflow or ordinary create_task "
    "for the user's cross-Team request. You MUST coordinate the request through "
    "the org_* tools: inspect the organization and claim the client-originated root "
    "with org_claim_task. The aggregation mode is not selected for you. Before "
    "creating any child, select it yourself with "
    "org_update_task(action='set_aggregation_mode'). If the user named "
    "SUMMARY_TEAM or HIERARCHICAL, use that mode. If the user named neither, "
    "choose one from the task. Then start the root if it is still CLAIMED. "
    "In that same turn, create one organization child for every mentioned "
    "expert team, including this Owner team, and leave each child OPEN. "
    "Do not set delegated_to_team_id and do not call org_delegate_task unless "
    "the user asks to delegate. Set each child's required_capabilities to "
    "exactly [\"group-member:{that proxy's member_name}\"] so the matching "
    "team is woken and claims it with org_claim_task. Create every child "
    "before any of them finishes, and do not start this Owner team's own "
    "writing until the other children exist. Then review their submissions. "
    "When you selected SUMMARY_TEAM, call "
    "org_create_summary_execution after every source task is accepted and wait "
    "until its Summary Task completes the root. When you selected HIERARCHICAL, "
    "do not create a Summary Team: after every source task is accepted, "
    "integrate the results and complete the root yourself with the final report. "
    "Only then call complete_team_run with that report. Never answer the request "
    "directly, substitute an internal teammate workflow, or submit partial work. "
    "A normal prose response before complete_team_run is invalid. "
    "Do not call org_create_and_invite_expert_team or build a team from an "
    "expert-group template. Each mentioned expert team joins only when its own "
    "proxy calls run_team, which resumes that member's existing instance. "
    "The text you pass to complete_team_run is the only group reply the user sees. "
    "Write only the requested deliverable. Do not mention instance pauses, "
    "templates, rebuilding a team, or reusing an earlier draft. "
    "When the final deliverable is a file, pass only that file in "
    "complete_team_run attachments as {name, path}; do not attach child-task "
    "drafts or intermediate files."
)

_FOLLOWUP_PROMPT_SUFFIX = (
    "\n\n[SWARM GROUP FOLLOW-UP]\n"
    "The Team Organization round is finished. This turn continues the same team. "
    "Use the conversation and outputs already in this team, including the child "
    "task you handled. Do not start another team and do not discard that context. "
    "Finish with complete_team_run. A normal prose ending without that call is invalid."
)

_OWNER_START_REMINDER = (
    "[SWARM GROUP ORGANIZATION OWNER — CONTINUE NOW]\n"
    "The group request is still waiting and its claimed root has no organization "
    "child tasks. Use org_view_tasks first and do not duplicate the root. If the "
    "aggregation mode is not selected yet, select it now with "
    "org_update_task(action='set_aggregation_mode'). Follow a mode the user "
    "named. If the user named neither SUMMARY_TEAM nor HIERARCHICAL, choose one "
    "from the task. Then start the root if it is still CLAIMED and, in this "
    "turn, create one OPEN child per mentioned expert. Leave them claimable: "
    "set required_capabilities to exactly [\"group-member:{member_name}\"] and "
    "do not delegate unless the user asks. Do not wait for one child to finish "
    "before creating the next. Do not "
    "use swarmflow, ordinary create_task, or org_create_and_invite_expert_team, "
    "and do not answer in prose. The mentioned teams are already members. For "
    "SUMMARY_TEAM, finish through org review, org_create_summary_execution, and "
    "complete_team_run. For HIERARCHICAL, accept the children, complete the root "
    "with the final report, and then call complete_team_run. That report is only "
    "the requested deliverable."
)

def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _plan_event(session_id: str, plan_id: str) -> asyncio.Event:
    key = (session_id, plan_id)
    event = _plan_events.get(key)
    if event is None:
        event = asyncio.Event()
        _plan_events[key] = event
    return event


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(*parts: str) -> str:
    return hashlib.sha256(
        json.dumps(list(parts), ensure_ascii=False).encode()
    ).hexdigest()


def expert_team_roster_entry(member: dict) -> dict | None:
    """Return one expert-team proxy in the current group roster shape.

    Current groups store ``agent_group`` and ``id``. Rows written before that
    rename used ``expert_team`` and ``definition_id``; accept both.
    """
    if not isinstance(member, dict) or member.get("kind") not in {
        "agent_group",
        "expert_team",
    }:
        return None
    package_id = str(member.get("id") or member.get("definition_id") or "").strip()
    member_name = str(member.get("member_name") or "").strip()
    if not package_id or not member_name:
        return None
    entry = dict(member)
    entry.pop("definition_id", None)
    entry["kind"] = "agent_group"
    entry["id"] = package_id
    entry["member_name"] = member_name
    return entry


def mentioned_expert_teams(mentions: list[str], members: list[dict]) -> list[dict]:
    """Return mentioned expert-team roster entries in mention order."""
    by_name = {}
    for item in members:
        entry = expert_team_roster_entry(item)
        if entry is not None:
            by_name[entry["member_name"]] = entry
    result: list[dict] = []
    seen: set[str] = set()
    for name in mentions:
        member = by_name.get(name)
        if member is None or name in seen:
            continue
        seen.add(name)
        result.append(member)
    return result


def mentioned_single_experts(mentions: list[str], members: list[dict]) -> list[str]:
    """Return mentioned single-expert member names in mention order.

    A single expert is ``agent_template``. Rows written before that rename used
    ``expert``. Expert teams are not included.
    """
    by_name: dict[str, str] = {}
    for item in members:
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in {"agent_template", "expert"}:
            continue
        name = str(item.get("member_name") or "").strip()
        if name:
            by_name[name] = name
    result: list[str] = []
    seen: set[str] = set()
    for name in mentions:
        if name not in by_name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def organization_id_for_session(session_id: str) -> str:
    """Legacy organization id used before each round chose its own."""
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in session_id)
    return f"gorg-{safe}"[:96]


def new_organization_id(session_id: str, plan_id: str) -> str:
    """A fresh organization id for one multi-expert round. At most 96 characters."""
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in session_id)
    suffix = _digest(plan_id)[:8]
    prefix = f"gorg-{safe}-"
    room = 96 - len(suffix)
    if len(prefix) > room:
        prefix = prefix[:room]
    return f"{prefix}{suffix}"


def resolve_organization_id(
    session_id: str,
    members: list[str],
    plans: list[dict],
    *,
    plan_id: str,
) -> str:
    """Every SwarmChat multi-expert round gets a fresh organization id.

    A finished round dissolves the organization while keeping each expert-team
    instance. Reusing an earlier id would reattach to erased org rows, so the
    member set no longer selects the organization. ``members`` / ``plans`` stay
    on the signature for call-site compatibility.
    """
    del members, plans
    return new_organization_id(session_id, plan_id)


def organization_id_for_plan(plan: dict, session_id: str) -> str:
    """Organization chosen for this plan, with the legacy session id as fallback."""
    saved = str(plan.get("organization_id") or "").strip()
    if saved:
        return saved
    return organization_id_for_session(session_id)


def instance_team_id(session_id: str, member_name: str) -> str:
    """Stable identity used to recover the same proxy-bound instance."""
    return f"org-expert-{_digest(session_id, member_name)[:12]}"


def result_key(session_id: str, owner_member: str) -> str:
    return _digest("group-organization", session_id, owner_member)


def organization_execution_id(session_id: str, plan_id: str) -> str:
    """Stable card id for one multi-expert request."""
    return hashlib.sha256(
        json.dumps(
            [session_id, "swarm-group-organization", plan_id],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _root_task_id(plan_id: str) -> str:
    return f"groot-{_digest(plan_id)[:16]}"


def owner_member_from_teams(teams: list[dict] | None) -> str:
    for item in teams or []:
        if (
            isinstance(item, dict)
            and item.get("role") == "owner"
            and item.get("member_name")
        ):
            return str(item["member_name"])
    return ""


def owner_team_id_from_teams(teams: list[dict] | None) -> str:
    for item in teams or []:
        if (
            isinstance(item, dict)
            and item.get("role") == "owner"
            and item.get("team_id")
        ):
            return str(item["team_id"])
    return ""


def _owner_for_organization(
    names: list[str],
    organization_id: str,
    plans: list[dict],
    *,
    legacy_id: str,
) -> str:
    """Owner is always the first @ for this round.

    Rounds no longer rejoin a prior organization id, so there is no retained
    Owner to prefer. Unused parameters remain for call-site compatibility.
    """
    del organization_id, plans, legacy_id
    return names[0]


def record_organization_plan(
    session_id: str,
    *,
    mentions: list[str],
    body: str,
    plan_id: str,
) -> dict | None:
    """Queue a multi-expert run without consuming or rewriting its mentions."""
    from jiuwenswarm.server.runtime.group_service import (
        add_group_organization_plan,
        group_organization_plans,
        read_group,
    )

    state = read_group(session_id)
    if state is None:
        return None
    members = list(state.get("members") or [])
    selected = mentioned_expert_teams(mentions, members)
    names = [str(item["member_name"]) for item in selected]
    singles = mentioned_single_experts(mentions, members)
    if names and singles:
        logger.info(
            "group organization plan skipped; message also mentions a single expert "
            "session=%s experts=%s singles=%s",
            session_id,
            names,
            singles,
        )
        remember_direct_route(session_id, plan_id, names)
        _defer_direct_members(session_id, plan_id, names, body)
        return None
    if len(selected) < 2:
        return None
    existing = next(
        (
            item
            for item in group_organization_plans(session_id)
            if item.get("plan_id") == plan_id
        ),
        None,
    )
    if existing is not None:
        return existing
    # Each multi-@ round creates a new organization; Owner is the first @.
    # Expert-team instances are still reused. Overlapping live rounds wait in
    # _blocking_earlier_plans.
    plans = group_organization_plans(session_id)
    organization_id = resolve_organization_id(
        session_id,
        names,
        plans,
        plan_id=plan_id,
    )
    owner = _owner_for_organization(
        names,
        organization_id,
        plans,
        legacy_id=organization_id_for_session(session_id),
    )
    plan = {
        "plan_id": plan_id,
        "members": names,
        "owner_member": owner,
        "organization_id": organization_id,
        "body": str(body or ""),
        "status": "pending",
        "created_at": _now_ms(),
    }
    if add_group_organization_plan(session_id, plan):
        for name in names:
            bind_member_turn(session_id, name, plan_id)
        logger.info(
            "group organization plan recorded session=%s plan=%s members=%s organization=%s",
            session_id,
            plan_id,
            names,
            organization_id,
        )
    if _blocking_earlier_plans(session_id, plan):
        _start_owner_queue(session_id, plan)
    return plan


def _owner_run_active(session_id: str, plan_id: str) -> bool:
    task = _owner_runs.get((session_id, plan_id))
    return task is not None and not task.done()


def _queue_run_active(session_id: str, plan_id: str) -> bool:
    """Whether this plan is waiting behind an older round."""
    for (queued_session, queued_plan, _owner), task in _queued_plans.items():
        if queued_session == session_id and queued_plan == plan_id and not task.done():
            return True
    return False


def _collect_live_plans(session_id: str, member_name: str) -> list[dict]:
    """Live plans that include this proxy, oldest first.

    A running Owner background stays live for as long as that task runs.
    A plan queued behind an older round stays live until that queue ends.
    Expiring a live Owner run here made the proxy's next run_team open a
    private team execution and post that result to the group.
    """
    from jiuwenswarm.server.runtime.group_service import (
        group_organization_plans,
        update_group_organization_plan,
    )

    stale_after_ms = int(PLAN_STALE_SECONDS * 1000)
    now = _now_ms()
    live: list[dict] = []
    for plan in group_organization_plans(session_id):
        if plan.get("status") not in {"pending", "running"}:
            continue
        plan_id = str(plan.get("plan_id") or "")
        if (
            not _owner_run_active(session_id, plan_id)
            and not _queue_run_active(session_id, plan_id)
            and now - int(plan.get("created_at") or 0) > stale_after_ms
        ):
            update_group_organization_plan(
                session_id, plan["plan_id"], status="expired"
            )
            _plan_event(session_id, plan_id).set()
            release_deferred_direct(session_id, plan_id)
            continue
        if member_name in (plan.get("members") or []):
            live.append(plan)
    return live


def pending_organization_plan(session_id: str, member_name: str) -> dict | None:
    """Oldest live organization run that includes this proxy."""
    plans = _collect_live_plans(session_id, member_name)
    return plans[0] if plans else None


def remember_direct_route(
    session_id: str, plan_id: str, members: list[str]
) -> None:
    """Mark one message as a direct reply that must not enter an organization."""
    target = str(plan_id or "").strip()
    named = [str(name) for name in members if str(name).strip()]
    if not session_id or not target or not named:
        return
    from jiuwenswarm.server.runtime.group_service import add_group_direct_route

    add_group_direct_route(
        session_id, {"plan_id": target, "members": named}
    )


def direct_route_for_member(
    session_id: str, member_name: str, plan_id: str | None
) -> bool:
    """Whether this message told this proxy to stay off the organization."""
    target = str(plan_id or "").strip()
    member = str(member_name or "").strip()
    if not target or not member:
        return False
    from jiuwenswarm.server.runtime.group_service import group_direct_routes

    for item in group_direct_routes(session_id):
        if str(item.get("plan_id") or "") != target:
            continue
        return member in {str(name) for name in (item.get("members") or [])}
    return False


def bind_member_turn(session_id: str, member_name: str, message_id: str) -> None:
    """Remember that this proxy still owes a turn for this message, in arrival order."""
    target = str(message_id or "").strip()
    member = str(member_name or "").strip()
    if not session_id or not member or not target:
        return
    queue = _member_turns.setdefault((session_id, member), [])
    if target not in queue:
        queue.append(target)


def bind_member_turn_front(session_id: str, member_name: str, message_id: str) -> None:
    """Put a message back at the front after a call was spent on an older organization."""
    target = str(message_id or "").strip()
    member = str(member_name or "").strip()
    if not session_id or not member or not target:
        return
    queue = _member_turns.setdefault((session_id, member), [])
    queue[:] = [item for item in queue if item != target]
    queue.insert(0, target)


def peek_member_turn(session_id: str, member_name: str) -> str | None:
    """The oldest message this proxy has not handled yet."""
    queue = _member_turns.get((session_id, str(member_name or "")))
    if not queue:
        return None
    return queue[0]


def consume_member_turn(session_id: str, member_name: str) -> str | None:
    """Drop the oldest bound message once this proxy has handled it."""
    key = (session_id, str(member_name or ""))
    queue = _member_turns.get(key)
    if not queue:
        return None
    message_id = queue.pop(0)
    if not queue:
        _member_turns.pop(key, None)
    return message_id


def release_member_turn(session_id: str, member_name: str, message_id: str) -> None:
    """Drop one bound message without affecting messages that arrived around it."""
    key = (session_id, str(member_name or ""))
    queue = _member_turns.get(key)
    if not queue:
        return
    target = str(message_id or "")
    remaining = [item for item in queue if item != target]
    if remaining:
        _member_turns[key] = remaining
    else:
        _member_turns.pop(key, None)


def _defer_direct_members(
    session_id: str, message_id: str, members: list[str], body: str
) -> None:
    """Bind a direct reply's turn; the proxy handles it now, beside any live organization."""
    del body
    for name in members:
        bind_member_turn(session_id, name, message_id)


def live_organizations_for_member(session_id: str, member_name: str) -> list[dict]:
    """Running organizations this proxy has joined, as the proxy sees them."""
    return [
        {
            "execution_id": organization_execution_id(session_id, str(plan["plan_id"])),
            "task": str(plan.get("body") or ""),
            "role": "owner" if str(plan.get("owner_member") or "") == member_name else "member",
        }
        for plan in _collect_live_plans(session_id, member_name)
        if organization_plan_accepted(session_id, str(plan.get("plan_id") or ""), member_name)
    ]


async def deliver_organization_instruction(
    session_id: str, member_name: str, execution_id: str, instruction: str,
) -> dict:
    """Send a related request into this proxy's own instance of a running organization."""
    from openjiuwen.agent_teams.interaction.payload import GodViewMessage
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.server.runtime.group_service import GroupError

    joined = {item["execution_id"] for item in live_organizations_for_member(session_id, member_name)}
    if execution_id not in joined:
        raise GroupError("NOT_FOUND", "this expert team is not in that running organization")
    team_id = instance_team_id(session_id, member_name)
    delivered = await Runner.interact_agent_team(
        GodViewMessage(body=instruction), team_name=team_id, session_id=session_id,
    )
    if not delivered.ok:
        raise GroupError("DELIVERY_FAILED", delivered.reason or "instruction was not accepted")
    return {"execution_id": execution_id, "team_id": team_id, "instruction_delivered": True}


def queued_direct_result(plan: dict) -> Any:
    """Tell the proxy to wait; the direct reply starts after the organization posts."""
    from openjiuwen.core.foundation.tool import ToolOutput

    return ToolOutput(
        success=True,
        data={
            "content": _DIRECT_WAIT_NOTE,
            "plan_id": str(plan.get("plan_id") or ""),
            "accepted": True,
            "status": "queued",
            "posted_to_group": False,
        },
    )


def release_deferred_direct(session_id: str, plan_id: str) -> None:
    """After an organization posts, start direct replies that were waiting on it."""
    plan = _plan_record(session_id, plan_id) or {}
    for name in plan.get("members") or []:
        release_member_turn(session_id, str(name), plan_id)
    for key, job in list(_deferred_direct.items()):
        if key[0] != session_id or job.get("started"):
            continue
        member = str(job.get("member") or "")
        if pending_organization_plan(session_id, member) is not None:
            continue
        job["started"] = True
        _schedule_direct_member(session_id, job)


def _schedule_direct_member(session_id: str, job: dict) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        job["started"] = False
        return
    member = str(job.get("member") or "")
    message_id = str(job.get("message_id") or "")
    task = asyncio.create_task(
        _launch_direct_member(
            session_id=session_id,
            member_name=member,
            message_id=message_id,
            body=str(job.get("body") or ""),
        ),
        name=f"group-direct-{message_id}-{member}",
    )

    def _finished(done: asyncio.Task) -> None:
        if done.cancelled() or done.exception() is not None:
            current = _deferred_direct.get((session_id, message_id, member))
            if current is not None:
                current["started"] = False

    task.add_done_callback(_finished)


async def _launch_direct_member(
    session_id: str, member_name: str, message_id: str, body: str
) -> None:
    """Run one direct reply on the ordinary single-team path.

    Reuse the proxy's own run_team tool. A freshly built tool would not have
    the private-team spec or the session context that path needs.
    """
    tool = None
    for _ in range(10):
        tool = _proxy_tools.get((session_id, member_name))
        if tool is not None:
            break
        await asyncio.sleep(POLL_SECONDS)
    if tool is None:
        logger.warning(
            "direct reply had no proxy session=%s member=%s message=%s",
            session_id,
            member_name,
            message_id,
        )
        return
    if peek_member_turn(session_id, member_name) != message_id:
        bind_member_turn_front(session_id, member_name, message_id)
    await tool.invoke(
        {
            "action": "start",
            "run_id": f"direct-{message_id}",
            "content": body or message_id,
        }
    )


def organization_plan_for_member(
    session_id: str, member_name: str, plan_id: str | None
) -> dict | None:
    """Plan for one run_team call.

    A call caused by a recorded multi-@ message uses that message's plan, even
    when an older plan is still live. A call that is not itself a new multi-@
    plan stays on the oldest live plan, so it does not open a private team
    while an organization is running. A direct-reply message never reaches
    this lookup.
    """
    plans = _collect_live_plans(session_id, member_name)
    if plan_id:
        matched = next(
            (plan for plan in plans if str(plan.get("plan_id") or "") == plan_id),
            None,
        )
        if matched is not None:
            return matched
    return plans[0] if plans else None


def recorded_organization_plan(
    session_id: str, member_name: str, plan_id: str | None
) -> dict | None:
    """The stored multi-@ plan for this message, including a finished one.

    Live routing ignores failed, expired, and done plans. This lookup is how a
    late run_team for the same message stays off the private-team path.
    """
    from jiuwenswarm.server.runtime.group_service import group_organization_plans

    target = str(plan_id or "").strip()
    if not target:
        return None
    for plan in group_organization_plans(session_id):
        if str(plan.get("plan_id") or "") != target:
            continue
        if member_name not in (plan.get("members") or []):
            return None
        return plan
    return None


def closed_organization_plan_result(plan: dict) -> Any:
    """Stop a run_team whose multi-@ plan has already ended."""
    from openjiuwen.core.foundation.tool import ToolOutput

    return ToolOutput(
        success=True,
        data={
            "content": _CLOSED_PLAN_NOTE,
            "plan_id": str(plan.get("plan_id") or ""),
            "status": str(plan.get("status") or ""),
            "accepted": False,
            "posted_to_group": False,
        },
    )


def _plan_sort_key(plan: dict) -> tuple[int, str]:
    return (int(plan.get("created_at") or 0), str(plan.get("plan_id") or ""))


def earlier_organization_plans(
    session_id: str, member_name: str, plan: dict
) -> list[dict]:
    """Live plans for this proxy that were recorded before ``plan``."""
    return _earlier_live_plans(session_id, member_name, plan)


def _earlier_live_plans(session_id: str, member_name: str, plan: dict) -> list[dict]:
    target = _plan_sort_key(plan)
    earlier = [
        item
        for item in _collect_live_plans(session_id, member_name)
        if str(item.get("plan_id") or "") != str(plan.get("plan_id") or "")
        and _plan_sort_key(item) < target
    ]
    earlier.sort(key=_plan_sort_key)
    return earlier


def _acceptance_key(session_id: str, plan_id: str, member_name: str) -> tuple[str, str, str]:
    return (session_id, str(plan_id), str(member_name))


def organization_plan_accepted(session_id: str, plan_id: str, member_name: str) -> bool:
    """Whether this proxy's run_team has already taken this live plan."""
    return _acceptance_key(session_id, plan_id, member_name) in _accepted_plans


def claim_organization_plan(session_id: str, plan_id: str, member_name: str) -> bool:
    """Record that this proxy's run_team is taking this plan. False if it already did."""
    key = _acceptance_key(session_id, plan_id, member_name)
    if key in _accepted_plans:
        return False
    _accepted_plans.add(key)
    return True


def release_organization_plan_claim(session_id: str, plan_id: str, member_name: str) -> None:
    """Drop a claim when joining that plan did not succeed."""
    _accepted_plans.discard(_acceptance_key(session_id, plan_id, member_name))


def _organization_attempt_started(session_id: str, plan_id: str) -> bool:
    """Whether this plan's member run or Owner background is already going."""
    if _owner_run_active(session_id, plan_id):
        return True
    for (run_session, run_plan, _member), task in _member_runs.items():
        if run_session == session_id and run_plan == plan_id and not task.done():
            return True
    return False


def _close_queued_organization_plan(
    session_id: str, plan_id: str, member_name: str, error: str
) -> None:
    """Fail a queue that will not start, and wake whoever is waiting on it."""
    release_organization_plan_claim(session_id, plan_id, member_name)
    _fail_open_organization_plan(session_id, plan_id, error)
    _plan_event(session_id, plan_id).set()
    release_deferred_direct(session_id, plan_id)


def _fail_open_organization_plan(session_id: str, plan_id: str, error: str) -> None:
    """Mark a still-open plan failed. A finished plan is left as it is."""
    from jiuwenswarm.server.runtime.group_service import update_group_organization_plan

    current = _plan_record(session_id, plan_id)
    if current is None or str(current.get("status") or "") not in {"pending", "running"}:
        return
    update_group_organization_plan(
        session_id, plan_id, status="failed", error=error
    )


def _plan_record(session_id: str, plan_id: str) -> dict | None:
    from jiuwenswarm.server.runtime.group_service import group_organization_plans

    target = str(plan_id or "")
    for plan in group_organization_plans(session_id):
        if str(plan.get("plan_id") or "") == target:
            return plan
    return None


def _blocking_earlier_plans(session_id: str, plan: dict) -> list[dict]:
    """Older pending or running plans that still use one of this plan's members."""
    from jiuwenswarm.server.runtime.group_service import group_organization_plans

    members = {str(name) for name in (plan.get("members") or [])}
    target = _plan_sort_key(plan)
    blocking = [
        item
        for item in group_organization_plans(session_id)
        if str(item.get("status") or "") in {"pending", "running"}
        and str(item.get("plan_id") or "") != str(plan.get("plan_id") or "")
        and _plan_sort_key(item) < target
        and members.intersection(str(name) for name in (item.get("members") or []))
    ]
    blocking.sort(key=_plan_sort_key)
    return blocking


def remember_proxy_backend(session_id: str, backend: Any) -> None:
    """Keep the group proxy that can start this member's organization round."""
    member_name = str(getattr(backend, "member_name", "") or "")
    if session_id and member_name and backend is not None:
        _proxy_backends[(session_id, member_name)] = backend


def remember_proxy_tool(session_id: str, tool: Any) -> None:
    """Keep the run_team tool that can start this member's later single-team reply."""
    backend = getattr(tool, "backend", None)
    member_name = str(getattr(backend, "member_name", "") or "")
    if session_id and member_name and tool is not None:
        _proxy_tools[(session_id, member_name)] = tool


def route_live_organization_plan(
    session_id: str, member_name: str, plan: dict
) -> tuple[str, dict]:
    """Choose the plan this run_team call should join or queue.

    The oldest live plan this proxy has not accepted is joined now, even when
    a newer mention is already in the history. A plan that shares a member with
    an older live round queues instead of binding those instances. That includes
    a proxy that is not itself a member of the older round.
    """
    earlier = earlier_organization_plans(session_id, member_name, plan)
    if earlier:
        oldest = earlier[0]
        oldest_id = str(oldest.get("plan_id") or "")
        if not organization_plan_accepted(session_id, oldest_id, member_name):
            if claim_organization_plan(session_id, oldest_id, member_name):
                return ("join", oldest)
    plan_id = str(plan.get("plan_id") or "")
    if _blocking_earlier_plans(session_id, plan):
        claim_organization_plan(session_id, plan_id, member_name)
        return ("queue", plan)
    if not earlier:
        claim_organization_plan(session_id, plan_id, member_name)
        return ("join", plan)
    live = sorted(
        _collect_live_plans(session_id, member_name),
        key=_plan_sort_key,
    )
    pending = next(
        (
            item
            for item in live
            if not organization_plan_accepted(
                session_id, str(item.get("plan_id") or ""), member_name
            )
        ),
        plan,
    )
    claim_organization_plan(session_id, str(pending.get("plan_id") or ""), member_name)
    return ("queue", pending)


async def wait_for_earlier_organization_plans(
    session_id: str, member_name: str, plan: dict
) -> None:
    """Hold a later multi-@ request until every overlapping older plan has finished."""
    while True:
        earlier = _earlier_live_plans(session_id, member_name, plan)
        seen = {str(item.get("plan_id") or "") for item in earlier}
        blocking = [
            item
            for item in _blocking_earlier_plans(session_id, plan)
            if str(item.get("plan_id") or "") not in seen
        ]
        pending = [*earlier, *blocking]
        if not pending:
            return
        event = _plan_event(session_id, str(pending[0].get("plan_id") or ""))
        try:
            await asyncio.wait_for(event.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            continue


def queue_organization_plan(*, backend: Any, session_id: str, plan: dict) -> Any:
    """Accept a later multi-@ request without holding its run_team call open.

    The proxy tool returns before the older organization finishes. A background
    task starts this plan once those earlier plans are done. The Owner task
    launches or resumes every mentioned instance, including a proxy whose turn
    already ended without another run_team call.
    """
    from openjiuwen.core.foundation.tool import ToolOutput

    remember_proxy_backend(session_id, backend)
    _start_owner_queue(session_id, plan, backend)
    return ToolOutput(
        success=True,
        data={
            "content": _QUEUED_NOTE,
            "plan_id": str(plan.get("plan_id") or ""),
            "accepted": True,
            "status": "queued",
            "posted_to_group": False,
        },
    )


def _start_owner_queue(session_id: str, plan: dict, backend: Any | None = None) -> None:
    """Queue the Owner once. A member call does not start its own join."""
    owner = str(plan.get("owner_member") or "")
    plan_id = str(plan.get("plan_id") or "")
    if not owner or not plan_id:
        return
    if backend is not None:
        remember_proxy_backend(session_id, backend)
    key = (session_id, plan_id, owner)
    current = _queued_plans.get(key)
    if current is not None and not current.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    task = asyncio.create_task(
        _owner_queue_entry(session_id, plan_id, owner),
        name=f"group-org-queue-{plan_id}-{owner}",
    )
    _queued_plans[key] = task

    def _finished(done: asyncio.Task) -> None:
        if _queued_plans.get(key) is done:
            _queued_plans.pop(key, None)

    task.add_done_callback(_finished)


async def _owner_queue_entry(session_id: str, plan_id: str, owner: str) -> None:
    """Wait for the Owner proxy, then start this plan after older rounds finish."""
    try:
        while _proxy_backends.get((session_id, owner)) is None:
            plan = _plan_record(session_id, plan_id)
            if plan is None or str(plan.get("status") or "") not in {"pending", "running"}:
                return
            await asyncio.sleep(POLL_SECONDS)
        await _join_queued_organization_plan(
            backend=_proxy_backends[(session_id, owner)],
            session_id=session_id,
            member_name=owner,
            plan_id=plan_id,
        )
    except asyncio.CancelledError:
        # The round has not started. Leaving the plan pending blocks every
        # later message that shares a member until the 62-minute expiry.
        if not _organization_attempt_started(session_id, plan_id):
            _close_queued_organization_plan(
                session_id,
                plan_id,
                owner,
                "queued organization wait ended before the round started",
            )
        raise


async def _join_queued_organization_plan(
    *,
    backend: Any,
    session_id: str,
    member_name: str,
    plan_id: str,
) -> None:
    try:
        while True:
            plan = organization_plan_for_member(session_id, member_name, plan_id)
            if plan is None:
                release_organization_plan_claim(session_id, plan_id, member_name)
                return
            await wait_for_earlier_organization_plans(session_id, member_name, plan)
            plan = organization_plan_for_member(session_id, member_name, plan_id)
            if plan is None:
                release_organization_plan_claim(session_id, plan_id, member_name)
                return
            result = await run_organization_member(
                backend=backend,
                session_id=session_id,
                plan=plan,
                ensure_members=member_name == str(plan.get("owner_member") or ""),
            )
            if getattr(result, "success", True) is False:
                error = str(getattr(result, "error", "") or "")
                current = _plan_record(session_id, plan_id)
                if (
                    "live organization" in error
                    and current is not None
                    and str(current.get("status") or "") in {"pending", "running"}
                ):
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                release_organization_plan_claim(session_id, plan_id, member_name)
                return
            return
    except Exception as exc:
        logger.exception(
            "queued group organization did not start session=%s plan=%s member=%s",
            session_id,
            plan_id,
            member_name,
        )
        _close_queued_organization_plan(
            session_id,
            plan_id,
            member_name,
            str(exc) or "queued organization did not start",
        )


async def triggering_plan_id(backend: Any, member_name: str) -> str | None:
    """Client message id of the turn this proxy is still handling.

    A message bound when it arrived stays ahead of anything written into the
    history later. The history scan is only the fallback for a turn that was
    not bound.
    """
    session_id = str(getattr(getattr(backend, "context", None), "session_id", "") or "")
    if not session_id:
        session_id = str(getattr(backend, "session_id", "") or "")
    bound = peek_member_turn(session_id, member_name)
    if bound:
        return bound
    getter = getattr(backend, "group_conversation", None)
    if not callable(getter):
        return None
    try:
        conversation = await getter()
        text = Path(conversation.history_path).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError, AttributeError):
        return None
    found: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        mentions = row.get("mentions") or []
        if member_name not in mentions or str(row.get("sender") or "") != "user":
            continue
        identity = str(row.get("client_message_id") or "").strip()
        if identity:
            found = identity
    return found


def group_reply_block_reason(
    session_id: str,
    member_name: str,
    team_id: str = "",
    message_id: str | None = None,
) -> str | None:
    """Block a non-owner reply to a live multi-@ organization message.

    The Owner background posts the one group reply. A mentioned teammate must
    not answer that message, and its expert-team instance must not post at all.
    Without a known message the proxy is held while any organization is live.
    """
    if message_id:
        # Only the reply to the organization's own message is held for the Owner.
        # The proxy answers any other message normally while the organization runs.
        plan = None
        if not direct_route_for_member(session_id, member_name, message_id):
            plan = recorded_organization_plan(session_id, member_name, message_id)
        if plan is not None and str(plan.get("status") or "") not in {"pending", "running"}:
            plan = None
    else:
        plan = pending_organization_plan(session_id, member_name)
    if plan is not None and str(plan.get("owner_member") or "") != str(member_name):
        return _REPLY_BLOCK
    team_id = str(team_id or "").strip()
    if not team_id:
        return None
    from jiuwenswarm.server.runtime.group_service import group_organization_teams

    for item in group_organization_teams(session_id):
        if str(item.get("team_id") or "") != team_id or item.get("role") == "owner":
            continue
        member_plan = pending_organization_plan(
            session_id, str(item.get("member_name") or "")
        )
        if member_plan is None:
            continue
        if str(member_plan.get("owner_member") or "") != str(item.get("member_name") or ""):
            return _REPLY_BLOCK
    return None


async def _set_member_instance_silence(
    runtime: Any, team_id: str, session_id: str, *, silent: bool
) -> None:
    """Keep a non-owner expert instance from posting into the group."""
    pool = getattr(runtime, "pool", None)
    if pool is None:
        return
    entry = await pool.get(team_id)
    if entry is None or entry.current_session_id != session_id:
        return
    harness = getattr(entry.agent, "harness", None)
    builder = getattr(harness, "system_prompt_builder", None)
    if not silent:
        if builder is not None:
            builder.remove_section("swarm_org_member_silence")
        return
    remove_tool = getattr(harness, "remove_tool", None)
    if callable(remove_tool):
        remove_tool("group_send_message")
        remove_tool("send_file")
    if builder is None:
        return
    from openjiuwen.harness.prompts import PromptSection

    builder.remove_section("swarm_org_member_silence")
    builder.add_section(
        PromptSection(
            name="swarm_org_member_silence",
            content={"en": _MEMBER_SILENCE, "cn": _MEMBER_SILENCE},
            priority=98,
        )
    )


def _organization_runtime() -> tuple[Any, Any, Any]:
    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        install_expert_org_adapters,
    )
    from jiuwenswarm.agents.harness.team.team_manager import (
        _runner_team_runtime_manager,
    )
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    runtime = _runner_team_runtime_manager(GLOBAL_RUNNER)
    org_runtime = getattr(runtime, "organization_runtime_manager", None)
    if org_runtime is None:
        raise RuntimeError("organization runtime is unavailable")
    install_expert_org_adapters(org_runtime)
    launcher = getattr(org_runtime, "_expert_team_launcher", None)
    if launcher is None:
        raise RuntimeError("expert team launcher is unavailable")
    return runtime, org_runtime, launcher


async def _live_backend(runtime: Any, team_id: str, session_id: str) -> Any | None:
    entry = await runtime.pool.get(team_id)
    if entry is None or entry.current_session_id != session_id:
        return None
    return getattr(entry.agent, "team_backend", None)


def _bound_organization(backend: Any) -> str:
    manager = getattr(backend, "org_task_manager", None)
    return str(getattr(manager, "organization_id", "") or "")


async def _invoke_owner_tool(tool: Any, inputs: dict[str, Any]) -> None:
    result = await tool.invoke(inputs)
    if getattr(result, "success", False):
        return
    error = getattr(result, "error", None) or (
        f"{getattr(tool.card, 'name', 'organization tool')} failed"
    )
    raise RuntimeError(str(error))


async def _owner_creates_organization(
    *,
    org_runtime: Any,
    owner_team_id: str,
    session_id: str,
    organization_id: str,
    display_name: str,
) -> None:
    from openjiuwen.agent_teams.organization.tools import (
        OrgCreateOrganizationTool,
    )

    await _invoke_owner_tool(
        OrgCreateOrganizationTool(org_runtime, owner_team_id, session_id),
        {
            "organization_id": organization_id,
            "display_name": display_name,
            "description": f"Group organization owned by {display_name}",
        },
    )


async def _owner_invites_team(
    *,
    org_runtime: Any,
    owner_team_id: str,
    session_id: str,
    organization_id: str,
    target_team_id: str,
) -> None:
    from openjiuwen.agent_teams.organization.tools import OrgInviteTeamTool

    await _invoke_owner_tool(
        OrgInviteTeamTool(org_runtime, owner_team_id, session_id),
        {
            "organization_id": organization_id,
            "team_id": target_team_id,
        },
    )


async def _owner_primes_root_task(
    *,
    task_pool: Any,
    team_id: str,
    session_id: str,
    plan: dict,
) -> None:
    """Claim the root and register each expert team.

    The Owner model selects SUMMARY_TEAM or HIERARCHICAL after the claim, then
    starts the root and publishes the child tasks. This function does not choose
    the aggregation mode and does not invent a placeholder child per team.
    """
    from openjiuwen.agent_teams.organization.schema import OrgTaskStatus
    from openjiuwen.agent_teams.organization.tools import (
        OrgClaimTaskTool,
        OrgCreateTaskTool,
    )

    task_id = _root_task_id(str(plan["plan_id"]))
    await task_pool.register_leader(
        team_id=team_id,
        leader_id="team-leader",
        leader_member_name="team-leader",
        capabilities=["group-coordination"],
    )
    task = await task_pool.get_task(task_id)
    if task is None:
        await _invoke_owner_tool(
            OrgCreateTaskTool(task_pool, team_id, "team-leader"),
            {
                "task_id": task_id,
                "title": "Swarm group request",
                "description": str(plan["body"]),
                "required_capabilities": ["group-coordination"],
                "metadata": {
                    "swarm_group_plan_id": str(plan["plan_id"]),
                    "client_originated": True,
                },
            },
        )
        task = await task_pool.get_task(task_id)
    if task is not None and task.status == OrgTaskStatus.OPEN:
        await _invoke_owner_tool(
            OrgClaimTaskTool(task_pool, team_id, "team-leader"),
            {"task_id": task_id},
        )
    for member_name in plan["members"]:
        member_team_id = instance_team_id(session_id, member_name)
        capability = f"group-member:{member_name}"
        capabilities = [capability]
        if member_team_id == team_id:
            capabilities.append("group-coordination")
        await task_pool.register_leader(
            team_id=member_team_id,
            leader_id="team-leader",
            leader_member_name="team-leader",
            capabilities=capabilities,
        )


async def ensure_member_instance(
    *,
    runtime: Any,
    launcher: Any,
    session_id: str,
    outer_team_id: str,
    member: dict,
    is_owner: bool,
    enable_organization: bool = True,
    leader_tools: list[Any] | None = None,
    leader_prompt_suffix: str | None = None,
    organization_id: str | None = None,
) -> str:
    """Reuse or launch the instance owned by one group proxy."""
    member_name = str(member["member_name"])
    team_id = instance_team_id(session_id, member_name)
    entry = await runtime.pool.get(team_id)
    if entry is not None and getattr(entry, "current_session_id", None) == session_id:
        await _sync_reused_member_role(
            getattr(entry, "agent", None),
            session_id=session_id,
            member_name=member_name,
            is_owner=is_owner,
        )
        return team_id

    tools = leader_tools
    if is_owner and tools is None:
        from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec

        from jiuwenswarm.agents.swarm.group_chat import COMPLETE_RUN

        tools = [
            BuiltinToolSpec(
                type=COMPLETE_RUN,
                params={"key": result_key(session_id, member_name)},
            )
        ]
    suffix = _OWNER_PROMPT_SUFFIX if is_owner else _MEMBER_SILENCE
    if leader_prompt_suffix is not None:
        suffix = leader_prompt_suffix
    await launcher.launch(
        organization_id=organization_id or organization_id_for_session(session_id),
        agent_group_name=str(member.get("id") or member.get("definition_id") or ""),
        session_id=session_id,
        display_name=member.get("display_name") or member_name,
        share_db_from_team_id=outer_team_id,
        team_id=team_id,
        enable_organization=enable_organization,
        leader_tools=tools,
        leader_prompt_suffix=suffix,
        extra_capabilities=[
            f"group-member:{member_name}",
            *(["group-coordination"] if is_owner else []),
        ],
    )
    return team_id


async def _sync_reused_member_role(
    agent: Any,
    *,
    session_id: str,
    member_name: str,
    is_owner: bool,
) -> None:
    """A live instance still has the previous round's role until this runs."""
    if agent is None:
        return
    spec = getattr(agent, "spec", None)
    metadata = getattr(spec, "metadata", None)
    if isinstance(metadata, dict):
        capabilities = [
            str(item)
            for item in (metadata.get("capabilities") or [])
            if str(item) != "group-coordination"
        ]
        member_capability = f"group-member:{member_name}"
        if member_capability not in capabilities:
            capabilities.append(member_capability)
        if is_owner:
            capabilities.append("group-coordination")
        spec.metadata = {**metadata, "capabilities": capabilities}
    harness = getattr(agent, "harness", None)
    remove_tool = getattr(harness, "remove_tool", None)
    if callable(remove_tool):
        remove_tool("complete_team_run")
    builder = getattr(harness, "system_prompt_builder", None)
    if builder is not None:
        builder.remove_section("swarm_org_owner")
        builder.remove_section("swarm_instance_followup")
    if not is_owner:
        return
    add_tool = getattr(harness, "add_tool", None)
    if callable(add_tool):
        from jiuwenswarm.agents.swarm.group_chat import CompleteTeamRun

        add_tool(CompleteTeamRun(result_key(session_id, member_name)))
    if builder is None:
        return
    from openjiuwen.harness.prompts import PromptSection

    builder.add_section(
        PromptSection(
            name="swarm_org_owner",
            content={"en": _OWNER_PROMPT_SUFFIX, "cn": _OWNER_PROMPT_SUFFIX},
            priority=99,
        )
    )


def _organization_record_paths(backend: Any, conversation: Any, member_name: str) -> list[Path]:
    """The instance record's current path, then the older long-name path still read."""
    from jiuwenswarm.agents.swarm.group_chat import RUN_PATH_HASH_LENGTH, group_runs_dir

    legacy = Path(conversation.history_path).parent / "team-runs" / f"organization-{member_name}.json"
    team_name = getattr(backend, "team_name", None)
    if not team_name:
        return [legacy]
    digest = hashlib.sha256(member_name.encode("utf-8")).hexdigest()[:RUN_PATH_HASH_LENGTH]
    return [group_runs_dir(team_name) / f"org-{digest}.json", legacy]


async def saved_organization_instance(
    backend: Any, member_name: str, session_id: str
) -> dict | None:
    """Return the instance record written when this proxy joined an organization."""
    conversation_getter = getattr(backend, "group_conversation", None)
    if not callable(conversation_getter):
        return None
    try:
        conversation = await conversation_getter()
        path = next(
            (item for item in _organization_record_paths(backend, conversation, member_name) if item.is_file()),
            None,
        )
        if path is None:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("kind") != "organization":
        return None
    if str(data.get("session_id") or "") != session_id:
        return None
    if data.get("team_name") != instance_team_id(session_id, member_name):
        return None
    return data


def _followup_query(task: str) -> str:
    return (
        "[SWARM GROUP FOLLOW-UP]\n"
        "The organization round is finished. Continue from this team's existing "
        "conversation and the child task you already handled. Do not start another "
        "team and do not discard that context.\n"
        f"User follow-up:\n{task}\n"
        "When the answer is ready, call complete_team_run with the full result."
    )


def _ability_manager(agent: Any) -> Any:
    harness = getattr(agent, "harness", None)
    manager = getattr(harness, "ability_manager", None)
    if manager is not None:
        return manager
    native = getattr(harness, "_native", None)
    return getattr(native, "ability_manager", None)


def _has_complete_team_run(agent: Any) -> bool:
    manager = _ability_manager(agent)
    listed = getattr(manager, "list", None)
    if not callable(listed):
        return False
    return any(getattr(card, "name", None) == "complete_team_run" for card in listed())


def _mount_followup_tool(agent: Any, key: str) -> None:
    from jiuwenswarm.agents.swarm.group_chat import CompleteTeamRun

    harness = getattr(agent, "harness", None)
    add_tool = getattr(harness, "add_tool", None)
    if callable(add_tool):
        add_tool(CompleteTeamRun(key))
    builder = getattr(harness, "system_prompt_builder", None)
    if builder is None:
        return
    from openjiuwen.harness.prompts import PromptSection

    builder.remove_section("swarm_instance_followup")
    builder.add_section(
        PromptSection(
            name="swarm_instance_followup",
            content={"en": _FOLLOWUP_PROMPT_SUFFIX, "cn": _FOLLOWUP_PROMPT_SUFFIX},
            priority=98,
        )
    )


def _restore_followup_tool(agent: Any, *, session_id: str, member_name: str, had_tool: bool) -> None:
    from jiuwenswarm.agents.swarm.group_chat import CompleteTeamRun

    harness = getattr(agent, "harness", None)
    builder = getattr(harness, "system_prompt_builder", None)
    if builder is not None:
        builder.remove_section("swarm_instance_followup")
    if had_tool:
        add_tool = getattr(harness, "add_tool", None)
        if callable(add_tool):
            add_tool(CompleteTeamRun(result_key(session_id, member_name)))
        return
    remove_tool = getattr(harness, "remove_tool", None)
    if callable(remove_tool):
        remove_tool("complete_team_run")


async def _open_followup_card(
    *,
    session_id: str,
    member_name: str,
    run_id: str,
    task: str,
    team_id: str,
) -> str | None:
    """Publish the same team card a first private run shows once work starts.

    A proxy that already joined an organization still resumes that instance.
    The card is what makes this later single-@ visible like a first run.
    """
    from jiuwenswarm.server.runtime.group_execution import TERMINAL, GroupExecutions

    store = GroupExecutions(session_id)
    token = _digest("group-instance-followup-card", session_id, member_name, run_id)
    try:
        created = await store.create([member_name], task, token, defer_card=True)
        record = created[0]
        if record.get("status") in TERMINAL:
            return None
        execution_id = str(record["execution_id"])
        await store.update(
            execution_id,
            execution_kind="team",
            team_id=team_id,
            background=True,
        )
        opened = await store.begin_task(execution_id, member_name)
    except Exception:
        logger.warning(
            "group follow-up card was not opened session=%s member=%s",
            session_id,
            member_name,
            exc_info=True,
        )
        return None
    return str(opened["execution_id"])


async def _start_followup_task_monitor(
    session_id: str,
    execution_id: str | None,
    team_id: str,
):
    """Copy this instance's child tasks onto the follow-up card.

    create_task and claim_task update the team board. The card only grows a
    row from a team.task event. A first private run gets those events from
    the team monitor; this follow-up attaches the same monitor once the
    resumed instance is active. The leader stream does not carry them.
    """
    if not execution_id:
        return None, None
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler

    token = set_session_id(session_id)
    try:
        monitor = await Runner.get_agent_team_monitor(
            team_name=team_id,
            session_id=session_id,
            hide_dm=True,
        )
    finally:
        reset_session_id(token)
    if monitor is None:
        return None, None
    handler = TeamMonitorHandler(monitor, session_id)
    await handler.start()

    async def _watch() -> None:
        async for event in handler.events():
            await _record_followup_task(session_id, execution_id, event)

    return handler, asyncio.create_task(_watch())


async def _stop_followup_task_monitor(handler, worker) -> None:
    if handler is not None:
        try:
            await handler.stop()
        except Exception:
            logger.warning("group follow-up task monitor did not stop", exc_info=True)
    if worker is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=2)
    except asyncio.TimeoutError:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    except Exception:
        logger.warning("group follow-up task monitor did not drain", exc_info=True)


async def _record_followup_task(session_id: str, execution_id: str | None, payload: dict) -> None:
    """Add one private-team child task to the follow-up card."""
    if not execution_id or payload.get("event_type") != "team.task":
        return
    from jiuwenswarm.server.runtime.group_execution import GroupExecutions

    try:
        await GroupExecutions(session_id).task_event(execution_id, payload)
    except Exception:
        logger.warning(
            "group follow-up card missed a child task session=%s execution=%s",
            session_id,
            execution_id,
            exc_info=True,
        )


async def _fail_followup_card(session_id: str, execution_id: str | None) -> None:
    if not execution_id:
        return
    from jiuwenswarm.server.runtime.group_execution import TERMINAL, GroupExecutions

    store = GroupExecutions(session_id)
    try:
        record = store.get(execution_id)
    except Exception:
        return
    if record.get("status") in TERMINAL:
        return
    try:
        await store.update(execution_id, status="failed", phase="failed")
    except Exception:
        logger.warning(
            "group follow-up card was not marked failed session=%s execution=%s",
            session_id,
            execution_id,
            exc_info=True,
        )


async def run_instance_followup(
    *,
    backend: Any,
    session_id: str,
    run_id: str,
    task: str,
) -> Any:
    """Resume the proxy's organization instance for one later single-team task.

    The model chooses this path by calling run_team. The same team id and
    session keep the child-task conversation. This does not open a blank
    private team and does not rejoin the organization. It does publish a
    team execution card, with one row per child task from that instance.
    """
    from openjiuwen.core.foundation.tool import ToolOutput

    member_name = str(backend.member_name)
    key = (session_id, member_name)
    current = _followup_runs.get(key)
    if current is not None and not current.done():
        return ToolOutput(
            success=True,
            data={
                "accepted": True,
                "posted_to_group": False,
                "resumed_instance": True,
                "instance_team_id": instance_team_id(session_id, member_name),
            },
        )
    worker = asyncio.create_task(
        _run_instance_followup(
            backend=backend,
            session_id=session_id,
            member_name=member_name,
            run_id=run_id,
            task=task,
        )
    )
    _followup_runs[key] = worker
    return ToolOutput(
        success=True,
        data={
            "accepted": True,
            "posted_to_group": False,
            "resumed_instance": True,
            "instance_team_id": instance_team_id(session_id, member_name),
        },
    )


async def _run_instance_followup(
    *,
    backend: Any,
    session_id: str,
    member_name: str,
    run_id: str,
    task: str,
) -> None:
    from jiuwenswarm.agents.swarm.group_chat import _results
    from jiuwenswarm.server.runtime.group_service import read_group

    follow_key = _digest("group-instance-followup", session_id, member_name, run_id)
    future = asyncio.get_running_loop().create_future()
    _results[follow_key] = future
    agent = None
    had_tool = False
    runtime = None
    execution_id: str | None = None
    team_id = instance_team_id(session_id, member_name)
    task_monitor = None
    task_monitor_worker = None
    try:
        state = read_group(session_id) or {}
        member = None
        for item in state.get("members") or []:
            entry = expert_team_roster_entry(item)
            if entry is not None and entry["member_name"] == member_name:
                member = entry
                break
        if member is None:
            raise RuntimeError(f"{member_name} is not an expert-team proxy")
        runtime, _org_runtime, launcher = _organization_runtime()
        existing = await runtime.pool.get(instance_team_id(session_id, member_name))
        existing_agent = getattr(existing, "agent", None)
        had_tool = existing_agent is not None and _has_complete_team_run(existing_agent)
        from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec

        from jiuwenswarm.agents.swarm.group_chat import COMPLETE_RUN

        team_id = await ensure_member_instance(
            runtime=runtime,
            launcher=launcher,
            session_id=session_id,
            outer_team_id=str(state.get("team_name") or ""),
            member=member,
            is_owner=False,
            enable_organization=False,
            leader_tools=[
                BuiltinToolSpec(type=COMPLETE_RUN, params={"key": follow_key})
            ],
            leader_prompt_suffix=_FOLLOWUP_PROMPT_SUFFIX,
        )
        entry = await runtime.pool.get(team_id)
        agent = getattr(entry, "agent", None)
        if agent is not None:
            _mount_followup_tool(agent, follow_key)
        execution_id = await _open_followup_card(
            session_id=session_id,
            member_name=member_name,
            run_id=run_id,
            task=task,
            team_id=team_id,
        )

        async def _on_payload(payload: dict) -> None:
            await _record_followup_task(session_id, execution_id, payload)

        async def _on_activated() -> None:
            nonlocal task_monitor, task_monitor_worker
            if task_monitor is not None:
                return
            try:
                task_monitor, task_monitor_worker = await _start_followup_task_monitor(
                    session_id, execution_id, team_id,
                )
            except Exception:
                logger.warning(
                    "group follow-up task monitor was not attached session=%s team=%s",
                    session_id,
                    team_id,
                    exc_info=True,
                )

        ran = await runtime.run_organization_turn(
            team_name=team_id,
            session_id=session_id,
            inputs={"query": _followup_query(task)},
            on_payload=_on_payload,
            on_activated=_on_activated,
        )
        await _stop_followup_task_monitor(task_monitor, task_monitor_worker)
        task_monitor = None
        task_monitor_worker = None
        if not ran or not future.done():
            raise RuntimeError("organization instance ended without a follow-up result")
        submission = future.result()
        if isinstance(submission, dict):
            result = submission["result"]
            attachments = list(submission.get("attachments") or [])
        else:
            result = submission
            attachments = []
        if execution_id:
            from jiuwenswarm.server.runtime.group_execution import GroupExecutions

            await GroupExecutions(session_id).finish(
                execution_id, backend, result, attachments
            )
        else:
            await backend.append_group_message(
                member_name,
                result,
                attachments=attachments,
                client_message_id=f"expert-followup-{follow_key[:24]}",
                mentions=[],
            )
    except Exception as exc:
        await _fail_followup_card(session_id, execution_id)
        logger.exception(
            "group instance follow-up failed session=%s member=%s",
            session_id,
            member_name,
        )
        from jiuwenswarm.server.runtime.group_execution import post_member_error

        await post_member_error(backend, member_name, str(exc), f"expert-followup-error-{follow_key[:24]}")
    finally:
        await _stop_followup_task_monitor(task_monitor, task_monitor_worker)
        _results.pop(follow_key, None)
        if runtime is not None:
            try:
                entry = await runtime.pool.get(team_id)
            except Exception:
                entry = None
            agent = getattr(entry, "agent", None) or agent
        if agent is not None:
            _restore_followup_tool(
                agent,
                session_id=session_id,
                member_name=member_name,
                had_tool=had_tool,
            )
        current = _followup_runs.get((session_id, member_name))
        if current is asyncio.current_task():
            _followup_runs.pop((session_id, member_name), None)


def _organization_has_live_plan(session_id: str, organization_id: str) -> bool:
    """Whether this organization has a round whose owner has already started.

    A later multi-@ message is recorded as pending before its owner runs.
    That record reuses an organization id but has not taken the instances, so
    it must not stop an older round from leaving this organization.
    """
    from jiuwenswarm.server.runtime.group_service import group_organization_plans

    for plan in group_organization_plans(session_id):
        if organization_id_for_plan(plan, session_id) != organization_id:
            continue
        status = str(plan.get("status") or "")
        if status == "running":
            return True
        plan_id = str(plan.get("plan_id") or "")
        if status == "pending" and _owner_run_active(session_id, plan_id):
            return True
    return False


def _owner_team_id_for_organization(session_id: str, organization_id: str) -> str:
    """Owner instance of the organization recorded on a plan, not the new round."""
    from jiuwenswarm.server.runtime.group_service import group_organization_plans

    for plan in reversed(group_organization_plans(session_id)):
        if organization_id_for_plan(plan, session_id) != organization_id:
            continue
        owner = str(plan.get("owner_member") or "")
        if owner:
            return instance_team_id(session_id, owner)
    return ""


async def _detach_other_organization(
    runtime: Any,
    org_runtime: Any,
    *,
    session_id: str,
    team_id: str,
    organization_id: str,
) -> None:
    """Leave a finished organization without deleting its tasks or context.

    Core refuses to create or invite into a new organization while the team
    still points at the old one. A round whose owner is running, or whose
    owner task has already started, is left untouched. A plan that is only
    recorded is not a reason to stay.
    """
    backend = await _live_backend(runtime, team_id, session_id)
    current = _bound_organization(backend)
    if not current or current == organization_id:
        return
    if _organization_has_live_plan(session_id, current):
        raise RuntimeError(f"team already belongs to a live organization: {current}")
    owner_team_id = _owner_team_id_for_organization(session_id, current) or team_id
    release = getattr(org_runtime, "release_membership", None)
    if callable(release):
        await release(
            organization_id=current,
            owner_team_id=owner_team_id,
            session_id=session_id,
        )
    if _bound_organization(backend) != current:
        return
    backend.org_task_manager = None
    if hasattr(backend, "org_message_service"):
        backend.org_message_service = None


async def sync_organization_membership(
    *,
    runtime: Any,
    org_runtime: Any,
    session_id: str,
    organization_id: str,
    owner_member: str,
    owner_display_name: str,
    teams: list[dict],
) -> None:
    """Bind the Owner, then invite every launched instance idempotently."""
    owner_team_id = instance_team_id(session_id, owner_member)
    owner_backend = await _live_backend(runtime, owner_team_id, session_id)
    if owner_backend is None:
        return
    await _detach_other_organization(
        runtime,
        org_runtime,
        session_id=session_id,
        team_id=owner_team_id,
        organization_id=organization_id,
    )
    if _bound_organization(owner_backend) != organization_id:
        await _owner_creates_organization(
            org_runtime=org_runtime,
            owner_team_id=owner_team_id,
            session_id=session_id,
            organization_id=organization_id,
            display_name=owner_display_name,
        )
    for item in teams:
        team_id = str(item.get("team_id") or "")
        if not team_id or team_id == owner_team_id:
            continue
        backend = await _live_backend(runtime, team_id, session_id)
        if backend is None or _bound_organization(backend) == organization_id:
            continue
        await _detach_other_organization(
            runtime,
            org_runtime,
            session_id=session_id,
            team_id=team_id,
            organization_id=organization_id,
        )
        if _bound_organization(backend) == organization_id:
            continue
        await _owner_invites_team(
            org_runtime=org_runtime,
            owner_team_id=owner_team_id,
            session_id=session_id,
            organization_id=organization_id,
            target_team_id=team_id,
        )


async def _record_team_run(
    backend: Any,
    *,
    member_name: str,
    team_id: str,
    session_id: str,
    organization_id: str,
) -> None:
    """Make group.delete remove organization instances like private runs."""
    from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write

    conversation = await backend.group_conversation()
    path = _organization_record_paths(backend, conversation, member_name)[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps(
            {
                "kind": "organization",
                "member_name": member_name,
                "team_name": team_id,
                "session_id": session_id,
                "organization_id": organization_id,
            },
            ensure_ascii=False,
        ),
    )


async def _proxy_turn_active(session_id: str, member_name: str) -> bool | None:
    """Whether this group proxy is still inside its current turn.

    ``True`` keeps the join wait open past 120 seconds. ``False`` means the
    turn has ended. ``None`` means the live team could not be read, so the
    wait keeps the original 120-second limit.
    """
    try:
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager
        from jiuwenswarm.server.runtime.group_service import read_group

        state = read_group(session_id) or {}
        channel_id = str(state.get("channel_id") or "web").strip() or "web"
        agent = get_team_manager(channel_id).get_team_agent(session_id)
        backend = getattr(agent, "team_backend", None) if agent is not None else None
        getter = getattr(backend, "get_member_status", None)
        if not callable(getter):
            return None
        status = await getter(member_name)
    except Exception:
        logger.debug(
            "group proxy turn status unavailable session=%s member=%s",
            session_id,
            member_name,
            exc_info=True,
        )
        return None
    if not isinstance(status, str) or not status.strip():
        return None
    return status.strip().lower() in _ACTIVE_PROXY_STATUSES


async def _wait_for_members(
    runtime: Any,
    *,
    session_id: str,
    organization_id: str,
    members: list[str],
) -> list[str]:
    """Members that never bound. A still-running proxy turn is not missing yet.

    The 120-second limit applies once that proxy's group turn has ended
    without ``run_team``, or when its status cannot be read. While the turn
    is still running, the wait continues with no time limit.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    extended = False
    while True:
        missing: list[str] = []
        for name in members:
            backend = await _live_backend(
                runtime, instance_team_id(session_id, name), session_id
            )
            if backend is None or _bound_organization(backend) != organization_id:
                missing.append(name)
        if not missing:
            return []
        elapsed = loop.time() - started
        idle: list[str] = []
        active: list[str] = []
        for name in missing:
            if await _proxy_turn_active(session_id, name) is True:
                active.append(name)
            else:
                idle.append(name)
        if idle and elapsed >= MEMBER_JOIN_TIMEOUT_SECONDS:
            return missing
        if active and not extended and elapsed >= MEMBER_JOIN_TIMEOUT_SECONDS:
            extended = True
            logger.info(
                "group organization join wait extended while proxies are still "
                "in turn session=%s members=%s",
                session_id,
                active,
            )
        await asyncio.sleep(_JOIN_POLL_SECONDS)


async def _block_template_expert_creation(
    runtime: Any, team_id: str, session_id: str
) -> None:
    """Swarm members join through run_team. The owner must not mint a template team."""
    pool = getattr(runtime, "pool", None)
    if pool is None:
        return
    entry = await pool.get(team_id)
    if entry is None or entry.current_session_id != session_id:
        return
    remove_tool = getattr(getattr(entry.agent, "harness", None), "remove_tool", None)
    if callable(remove_tool):
        remove_tool("org_create_and_invite_expert_team")


async def _plan_root(task_pool: Any, plan_id: str) -> Any:
    """The root created for this plan. Its id is stable across a restarted wait."""
    root_id = _root_task_id(plan_id)
    getter = getattr(task_pool, "get_task", None)
    if callable(getter):
        return await getter(root_id)
    tasks = await task_pool.list_tasks(limit=200)
    for task in tasks or []:
        if str(getattr(task, "task_id", "") or "") == root_id:
            return task
    return None


async def _round_state(task_pool: Any, plan_id: str) -> tuple[str, list[Any]]:
    """Return running/completed/failed for this plan's root.

    The root id comes from the plan, not from when this background wait
    started. A recovered wait therefore still sees a root created earlier.
    Only a summary execution of that same root can keep the round open.
    """
    from openjiuwen.agent_teams.organization.schema import OrgTaskStatus

    root = await _plan_root(task_pool, plan_id)
    if root is None:
        return "running", []
    roots = [root]
    if root.status == OrgTaskStatus.FAILED:
        return "failed", roots
    if root.status != OrgTaskStatus.COMPLETED:
        return "running", roots
    root_id = str(getattr(root, "task_id", "") or "")
    incomplete = await task_pool.list_incomplete_summary_executions()
    if any(str(getattr(item, "root_task_id", "") or "") == root_id for item in incomplete):
        return "running", roots
    return "completed", roots


async def _queue_owner_start_reminder(
    *,
    org_runtime: Any,
    task_pool: Any,
    team_id: str,
    session_id: str,
    plan_id: str,
) -> None:
    """Queue one idempotent correction if this plan's root has no children."""
    await asyncio.sleep(OWNER_START_WATCHDOG_SECONDS)
    root_id = _root_task_id(plan_id)
    tasks = await task_pool.list_tasks(limit=200)
    children = [
        task
        for task in tasks
        if str(getattr(task, "parent_task_id", "") or "") == root_id
    ]
    if children:
        return
    logger.warning(
        "group organization owner created no children; queueing reminder "
        "session=%s team=%s",
        session_id,
        team_id,
    )
    org_runtime._schedule_leader_turn(
        team_id=team_id,
        session_id=session_id,
        prompt=_OWNER_START_REMINDER,
    )


def _root_output(roots: list[Any]) -> str:
    parts: list[str] = []
    for task in roots:
        abstract = str(getattr(task, "output_abstract", "") or "").strip()
        if abstract:
            parts.append(abstract)
        context = getattr(task, "output_context", None)
        uri = str(getattr(context, "result_uri", "") or "").strip()
        if not uri:
            continue
        path = Path(uri.removeprefix("file://"))
        try:
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8")[:20000])
        except OSError:
            logger.debug("organization output unreadable: %s", uri, exc_info=True)
    return "\n\n".join(parts)


def _final_file_path(raw: Any, *, session_id: str, organization_id: str) -> Path | None:
    """Resolve one Owner-delivered file, or ``None`` when it is not a readable team file."""
    from openjiuwen.agent_teams.paths import (
        get_agent_teams_home,
        get_openjiuwen_home,
        organization_workspace_dir,
    )

    text = str(raw or "").strip().removeprefix("file://")
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        prefix = f".organization/{organization_id}/"
        normalized = text.replace("\\", "/")
        if not organization_id or not normalized.startswith(prefix):
            return None
        candidate = organization_workspace_dir(organization_id, session_id) / normalized[len(prefix):]
    try:
        resolved = candidate.resolve(strict=True)
        roots = [
            get_agent_teams_home().resolve(),
            (get_openjiuwen_home() / ".organizations").resolve(),
        ]
    except OSError:
        return None
    if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in roots):
        return None
    return resolved


def _owner_final_files(
    submitted: list, roots: list[Any], *, session_id: str, organization_id: str
) -> list[dict]:
    """Files the Owner delivered: complete_team_run attachments and the root result_uri.

    Child-task outputs are never scanned, so drafts stay out of the group post.
    """
    candidates = [item.get("path") for item in submitted if isinstance(item, dict)]
    for task in roots:
        context = getattr(task, "output_context", None)
        candidates.append(getattr(context, "result_uri", None))
    files: list[dict] = []
    seen: set[str] = set()
    for raw in candidates:
        path = _final_file_path(raw, session_id=session_id, organization_id=organization_id)
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        files.append({"name": path.name, "path": str(path)})
    return files


def _split_submission(value: Any) -> tuple[str, list]:
    """complete_team_run stores plain text, or a dict when it carries attachments."""
    if isinstance(value, dict):
        return str(value.get("result") or ""), list(value.get("attachments") or [])
    return str(value), []


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _team_labels(session_id: str) -> dict[str, str]:
    """Map a resident instance id to the expert team's display name.

    The live organization roster is replaced when the next round starts. A card
    that is still refreshing must keep resolving the previous round's instances
    from the group member list, whose instance ids do not change.
    """
    from jiuwenswarm.server.runtime.group_service import (
        group_organization_teams,
        read_group,
    )

    state = read_group(session_id) or {}
    roster = {
        item.get("member_name"): item
        for item in state.get("members") or []
        if isinstance(item, dict)
    }
    labels: dict[str, str] = {}
    for item in roster.values():
        entry = expert_team_roster_entry(item)
        if entry is None:
            continue
        member_name = entry["member_name"]
        labels[instance_team_id(session_id, member_name)] = str(
            entry.get("display_name") or member_name
        )
    for item in group_organization_teams(session_id):
        team_id = str(item.get("team_id") or "")
        member_name = str(item.get("member_name") or "")
        member = roster.get(member_name) or {}
        if team_id:
            labels[team_id] = str(member.get("display_name") or member_name)
    return labels


def _assigned_team_id(task: Any) -> str:
    direct = getattr(task, "assigned_team_id", None)
    if direct:
        return str(direct)
    assignment = getattr(task, "assignment", None)
    return str(getattr(assignment, "team_id", "") or "")


def _task_metadata(task: Any) -> dict:
    raw = getattr(task, "metadata", None)
    if isinstance(raw, dict):
        return raw
    encoded = getattr(task, "metadata_json", None)
    if isinstance(encoded, str) and encoded.strip():
        try:
            loaded = json.loads(encoded)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _repairs_task_id(task: Any) -> str:
    return str(_task_metadata(task).get("repairs_task_id") or "")


def _is_summary_task(task: Any) -> bool:
    task_type = str(getattr(task, "task_type", "") or "")
    task_id = str(getattr(task, "task_id", "") or "")
    return task_type == "organization.summary" or task_id.startswith("org-summary")


def _aggregation_mode(task: Any) -> str:
    aggregation = getattr(task, "aggregation", None)
    if aggregation is None:
        return ""
    if isinstance(aggregation, dict):
        mode = aggregation.get("mode")
    else:
        mode = getattr(aggregation, "mode", None)
    return str(getattr(mode, "value", mode) or "")


def _controller_team_id(task: Any) -> str:
    aggregation = getattr(task, "aggregation", None)
    if isinstance(aggregation, dict):
        return str(aggregation.get("controller_team_id") or "")
    return str(getattr(aggregation, "controller_team_id", "") or "")


def _outline_line(text: str) -> str:
    """First line of a task outline, without a markdown heading mark."""
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line.lstrip("#").strip()


def _hierarchical_row_title(task: Any, *, outline: str = "") -> str:
    """Name the Owner's summary when it is handed over, and keep that name.

    The primed root title is not an outline. The finished report is not either:
    its first line must not replace the row after the summary completes.
    """
    title = str(getattr(task, "title", "") or "").strip()
    if not title or title == "Swarm group request":
        title = str(getattr(task, "description", "") or "").strip() or outline
    line = _outline_line(title)
    if not line:
        return "汇总"
    if line.startswith("汇总"):
        return line
    return f"汇总：{line}"


async def _plan_child_tasks(task_pool: Any, plan: dict) -> tuple[list[Any], Any]:
    """Root children, then this root's Summary Team task. Nested work stays out."""
    if task_pool is None:
        return [], None
    lister = getattr(task_pool, "list_tasks", None)
    if not callable(lister):
        return [], None
    root_id = _root_task_id(str(plan["plan_id"]))
    tasks = await lister(limit=200)
    children = []
    summary: Any = None
    root: Any = None
    for task in tasks or []:
        if str(getattr(task, "task_id", "") or "") == root_id:
            root = task
        if _is_summary_task(task):
            if str(getattr(task, "root_task_id", "") or "") == root_id:
                summary = task
            continue
        if str(getattr(task, "parent_task_id", "") or "") != root_id:
            continue
        children.append(task)
    children.sort(key=lambda item: int(getattr(item, "created_at", 0) or 0))
    if summary is not None:
        children.append(summary)
    return children, root


async def _organization_rows(
    session_id: str, plan: dict, task_pool: Any, *, fail_unfinished: bool
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """One card row per organization child. The title is that subtask."""
    labels = _team_labels(session_id)
    tasks: dict[str, str] = {}
    details: dict[str, dict[str, str]] = {}
    children, root = await _plan_child_tasks(task_pool, plan)
    for task in children:
        task_id = str(task.task_id)
        status = await _expert_row_status(task_pool, task)
        if fail_unfinished and status not in {"completed", "failed"}:
            status = "failed"
        title = str(getattr(task, "title", "") or "").strip() or task_id
        member = labels.get(_assigned_team_id(task), "")
        if not member and _is_summary_task(task):
            member = "Summary Team"
        tasks[task_id] = status
        details[task_id] = {
            "title": title,
            "status": status,
            "member_id": member,
        }
    accepted_repairs = {
        _repairs_task_id(task)
        for task in children
        if _repairs_task_id(task) and tasks.get(str(task.task_id)) == "completed"
    }
    source_done = [
        tasks[str(task.task_id)] == "completed" or str(task.task_id) in accepted_repairs
        for task in children
        if not _is_summary_task(task)
    ]
    # The root is in progress as soon as the Owner starts it. That is not the
    # summary. Show this row only after every chapter is accepted. A child sent
    # back for revision stays failed on its own row; its accepted repair closes
    # that chapter.
    if (
        root is not None
        and _aggregation_mode(root) == "HIERARCHICAL"
        and not any(_is_summary_task(task) for task in children)
        and source_done
        and all(source_done)
    ):
        task_id = str(root.task_id)
        status = await _expert_row_status(task_pool, root)
        if fail_unfinished and status not in {"completed", "failed"}:
            status = "failed"
        controller = _controller_team_id(root) or _assigned_team_id(root)
        tasks[task_id] = status
        details[task_id] = {
            "title": _hierarchical_row_title(root, outline=str(plan.get("body") or "")),
            "status": status,
            "member_id": labels.get(controller, ""),
        }
    return tasks, details


async def _expert_row_status(task_pool: Any, task: Any) -> str:
    """Map one organization task onto a card row. Acceptance completes a child."""
    text = _status_text(getattr(task, "status", ""))
    if _is_summary_task(task) or _aggregation_mode(task) == "HIERARCHICAL":
        if text == "FAILED":
            return "failed"
        if text == "COMPLETED":
            return "completed"
        if text == "OPEN":
            return "pending"
        return "in_progress"
    if text == "FAILED":
        return "failed"
    if text == "COMPLETED":
        review_text = ""
        getter = getattr(task_pool, "get_task_review", None)
        if callable(getter):
            review = await getter(task.task_id)
            review_text = _status_text(getattr(review, "review_status", ""))
        if review_text == "ACCEPTED":
            return "completed"
        if review_text in {"REJECTED", "NEEDS_REVISION"}:
            return "failed"
        return "in_progress"
    if text in {"CLAIMED", "IN_PROGRESS", "DELEGATED", "WAITING_SOURCES"}:
        return "in_progress"
    return "pending"


async def publish_organization_card(
    *,
    session_id: str,
    plan: dict,
    organization_id: str,
    team_id: str,
    task_pool: Any = None,
    status: str = "running",
    message_id: str | None = None,
    fail_unfinished: bool = False,
    create: bool = True,
) -> None:
    """Upsert the one organization card. A write failure does not stop the run."""
    plan_id = str(plan.get("plan_id") or "")
    try:
        from jiuwenswarm.server.runtime.group_execution import GroupExecutions

        execution_id = organization_execution_id(session_id, plan_id)
        store = GroupExecutions(session_id)
        if not create and store.read().get(execution_id) is None:
            return
        tasks, details = await _organization_rows(
            session_id, plan, task_pool, fail_unfinished=fail_unfinished
        )
        if status == "completed":
            phase = "completed"
        elif status == "failed":
            phase = "failed"
        else:
            phase = "executing"
        body = str(plan.get("body") or "")
        await store.upsert_organization_card(
            execution_id,
            title=body,
            task=body,
            organization_id=organization_id,
            owner_member=str(plan.get("owner_member") or ""),
            team_id=team_id,
            root_task_id=_root_task_id(plan_id),
            tasks=tasks,
            task_details=details,
            status=status,
            phase=phase,
            message_id=message_id,
        )
    except Exception:
        logger.exception(
            "group organization card update failed session=%s plan=%s",
            session_id,
            plan_id,
        )


async def _await_completed_result(
    future: asyncio.Future,
    task_pool: Any,
    plan_id: str,
    on_tick: Any = None,
    *,
    session_id: str = "",
    organization_id: str = "",
) -> tuple[str, list[dict]]:
    """Wait for both the Owner submission and durable root/summary completion.

    The wait has no time limit. It ends when the root fails, or when the
    round is complete and the Owner has submitted (or the grace fallback
    applies). Returns the report and the Owner's final files.
    """
    loop = asyncio.get_running_loop()
    completed_at: float | None = None
    submitted: str | None = None
    submitted_files: list = []
    while True:
        if on_tick is not None:
            await on_tick()
        if future.done() and not future.cancelled():
            submitted, submitted_files = _split_submission(future.result())
        state, roots = await _round_state(task_pool, plan_id)
        if state == "failed":
            raise RuntimeError("organization root task failed")
        if state == "completed":
            completed_at = completed_at or loop.time()
            if submitted:
                return submitted, _owner_final_files(
                    submitted_files,
                    roots,
                    session_id=session_id,
                    organization_id=organization_id,
                )
            if loop.time() - completed_at >= RESULT_GRACE_SECONDS:
                fallback = _root_output(roots)
                if fallback:
                    logger.warning(
                        "owner did not submit the final report; using root output"
                    )
                    return fallback, _owner_final_files(
                        [],
                        roots,
                        session_id=session_id,
                        organization_id=organization_id,
                    )
                raise RuntimeError("organization completed without a final report")
        await asyncio.sleep(POLL_SECONDS)


async def pause_organization_instances(
    *,
    session_id: str,
    task_pool: Any,
    team_ids: list[str],
    include_summary: bool,
) -> None:
    """Pause resident instances; never interrupt an unfinished Summary Team."""
    from openjiuwen.core.runner import Runner

    names = list(dict.fromkeys(team_ids))
    if include_summary:
        summary = await task_pool.get_summary_team()
        if summary is not None and summary.summary_team_id:
            names.append(summary.summary_team_id)
    for team_id in names:
        try:
            await Runner.pause_agent_team(team_name=team_id, session_id=session_id)
        except Exception:
            logger.warning(
                "failed to pause organization team=%s session=%s",
                team_id,
                session_id,
                exc_info=True,
            )


async def team_contribution(
    task_pool: Any, team_id: str, started_at: int
) -> list[dict[str, Any]]:
    """Completed subtasks returned privately to a non-Owner proxy."""
    from openjiuwen.agent_teams.organization.schema import OrgTaskStatus

    tasks = await task_pool.list_tasks(
        status=OrgTaskStatus.COMPLETED,
        assigned_team_id=team_id,
        limit=100,
    )
    result: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: item.created_at):
        if int(task.created_at) < started_at - 1000:
            continue
        context = getattr(task, "output_context", None)
        result.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "output_abstract": task.output_abstract or "",
                "result_uri": getattr(context, "result_uri", None),
            }
        )
    return result


async def run_organization_member(
    *,
    backend: Any,
    session_id: str,
    plan: dict,
    ensure_members: bool = False,
) -> Any:
    """Join duplicate proxy tool calls to one durable organization run."""
    key = (session_id, str(plan["plan_id"]), str(backend.member_name))
    task = _member_runs.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _run_organization_member_once(
                backend=backend,
                session_id=session_id,
                plan=plan,
                ensure_members=ensure_members,
            )
        )
        _member_runs[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _member_runs.get(key) is task:
            _member_runs.pop(key, None)


async def _run_organization_member_once(
    *,
    backend: Any,
    session_id: str,
    plan: dict,
    ensure_members: bool = False,
) -> Any:
    """Organization-mode implementation of one proxy's run_team call."""
    from openjiuwen.core.foundation.tool import ToolOutput

    from jiuwenswarm.server.runtime.group_service import (
        group_organization_teams,
        read_group,
        update_group_organization_plan,
        upsert_group_organization_team,
    )

    member_name = str(backend.member_name)
    owner_member = str(plan["owner_member"])
    is_owner = member_name == owner_member
    plan_id = str(plan["plan_id"])
    organization_id = organization_id_for_plan(plan, session_id)
    try:
        runtime, org_runtime, launcher = _organization_runtime()
        state = read_group(session_id) or {}
        roster = {
            item.get("member_name"): item
            for item in state.get("members") or []
            if isinstance(item, dict)
        }
        member = expert_team_roster_entry(roster.get(member_name) or {})
        if member is None:
            raise RuntimeError(f"{member_name} is not an expert-team proxy")
        async with _lock_for(session_id):
            team_id = await ensure_member_instance(
                runtime=runtime,
                launcher=launcher,
                session_id=session_id,
                outer_team_id=str(state.get("team_name") or ""),
                member=member,
                is_owner=is_owner,
                organization_id=organization_id,
            )
            upsert_group_organization_team(
                session_id,
                organization_id,
                {
                    "member_name": member_name,
                    "team_id": team_id,
                    "role": "owner" if is_owner else "member",
                    "id": member["id"],
                },
            )
            await _record_team_run(
                backend,
                member_name=member_name,
                team_id=team_id,
                session_id=session_id,
                organization_id=organization_id,
            )
            owner = roster.get(owner_member) or {}
            await sync_organization_membership(
                runtime=runtime,
                org_runtime=org_runtime,
                session_id=session_id,
                organization_id=organization_id,
                owner_member=owner_member,
                owner_display_name=(owner.get("display_name") or owner_member),
                teams=group_organization_teams(session_id),
            )
            await _set_member_instance_silence(
                runtime, team_id, session_id, silent=not is_owner
            )
            await _block_template_expert_creation(runtime, team_id, session_id)
        logger.info(
            "group organization instance ready session=%s member=%s team=%s",
            session_id,
            member_name,
            team_id,
        )
        if is_owner and runtime is not None:
            try:
                bound = await _live_backend(runtime, team_id, session_id)
            except Exception:
                logger.warning(
                    "group organization card skipped; owner binding unreadable session=%s",
                    session_id,
                    exc_info=True,
                )
                bound = None
            if _bound_organization(bound) == organization_id:
                await publish_organization_card(
                    session_id=session_id,
                    plan=plan,
                    organization_id=organization_id,
                    team_id=team_id,
                )
        if is_owner:
            _start_owner_run(
                backend=backend,
                runtime=runtime,
                org_runtime=org_runtime,
                session_id=session_id,
                plan=plan,
                organization_id=organization_id,
                team_id=team_id,
                ensure_members=ensure_members,
            )
        return _accepted_organization_output(
            organization_id=organization_id,
            team_id=team_id,
            plan_id=plan_id,
            role="owner" if is_owner else "member",
        )
    except Exception as exc:
        logger.exception(
            "group organization run failed session=%s member=%s",
            session_id,
            member_name,
        )
        # An early bind hit a round that is still live. Leave this plan pending
        # so the Owner queue can start it after that round releases.
        if _blocking_earlier_plans(session_id, plan) and "live organization" in str(exc):
            return ToolOutput(success=False, error=str(exc))
        if is_owner:
            update_group_organization_plan(
                session_id, plan_id, status="failed", error=str(exc)
            )
            _plan_event(session_id, plan_id).set()
            release_deferred_direct(session_id, plan_id)
        return ToolOutput(success=False, error=str(exc))


def _accepted_organization_output(
    *,
    organization_id: str,
    team_id: str,
    plan_id: str,
    role: str,
) -> Any:
    from openjiuwen.core.foundation.tool import ToolOutput

    return ToolOutput(
        success=True,
        data={
            "content": _IN_PROGRESS_NOTE,
            "organization_id": organization_id,
            "team_id": team_id,
            "plan_id": plan_id,
            "role": role,
            "accepted": True,
            "status": "running",
            "posted_to_group": False,
        },
    )


def _start_owner_run(**kwargs: Any) -> None:
    """Keep the Owner wait outside the proxy's run_team call."""
    session_id = str(kwargs["session_id"])
    plan_id = str(kwargs["plan"]["plan_id"])
    key = (session_id, plan_id)
    current = _owner_runs.get(key)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _owner_background(**kwargs),
        name=f"group-org-owner-{plan_id}",
    )
    _owner_runs[key] = task

    def _finished(done: asyncio.Task) -> None:
        if _owner_runs.get(key) is done:
            _owner_runs.pop(key, None)

    task.add_done_callback(_finished)


async def _owner_background(**kwargs: Any) -> None:
    session_id = str(kwargs["session_id"])
    plan_id = str(kwargs["plan"]["plan_id"])
    try:
        await _run_owner_proxy(**kwargs)
    except Exception:
        logger.exception(
            "group organization owner background failed session=%s plan=%s",
            session_id,
            plan_id,
        )
        try:
            _fail_open_organization_plan(
                session_id, plan_id, "organization owner background failed"
            )
        except Exception:
            logger.exception(
                "group organization status update failed session=%s plan=%s",
                session_id,
                plan_id,
            )


async def _ensure_mentioned_member_instances(
    *,
    runtime: Any,
    org_runtime: Any,
    launcher: Any,
    session_id: str,
    plan: dict,
    organization_id: str,
    record_backend: Any | None = None,
) -> None:
    """Launch or resume every mentioned instance and invite it into this plan.

    A queued follow-up starts after the previous organization has released
    membership. A proxy that already ended its turn cannot call run_team
    again, so the Owner brings that instance back itself.
    """
    from jiuwenswarm.server.runtime.group_service import (
        group_organization_teams,
        read_group,
        upsert_group_organization_team,
    )

    state = read_group(session_id) or {}
    roster = {
        item.get("member_name"): item
        for item in state.get("members") or []
        if isinstance(item, dict)
    }
    owner_member = str(plan.get("owner_member") or "")
    async with _lock_for(session_id):
        for name in list(plan.get("members") or []):
            member_name = str(name)
            member = expert_team_roster_entry(roster.get(member_name) or {})
            if member is None:
                raise RuntimeError(f"{member_name} is not an expert-team proxy")
            is_owner = member_name == owner_member
            team_id = await ensure_member_instance(
                runtime=runtime,
                launcher=launcher,
                session_id=session_id,
                outer_team_id=str(state.get("team_name") or ""),
                member=member,
                is_owner=is_owner,
                organization_id=organization_id,
            )
            upsert_group_organization_team(
                session_id,
                organization_id,
                {
                    "member_name": member_name,
                    "team_id": team_id,
                    "role": "owner" if is_owner else "member",
                    "id": member["id"],
                },
            )
            if record_backend is not None and callable(
                getattr(record_backend, "group_conversation", None)
            ):
                await _record_team_run(
                    record_backend,
                    member_name=member_name,
                    team_id=team_id,
                    session_id=session_id,
                    organization_id=organization_id,
                )
            await _set_member_instance_silence(
                runtime, team_id, session_id, silent=not is_owner
            )
            await _block_template_expert_creation(runtime, team_id, session_id)
        owner = roster.get(owner_member) or {}
        await sync_organization_membership(
            runtime=runtime,
            org_runtime=org_runtime,
            session_id=session_id,
            organization_id=organization_id,
            owner_member=owner_member,
            owner_display_name=(owner.get("display_name") or owner_member),
            teams=group_organization_teams(session_id),
        )


async def _run_owner_proxy(
    *,
    backend: Any,
    runtime: Any,
    org_runtime: Any,
    session_id: str,
    plan: dict,
    organization_id: str,
    team_id: str,
    ensure_members: bool = False,
) -> Any:
    from openjiuwen.core.foundation.tool import ToolOutput

    from jiuwenswarm.agents.swarm.group_chat import _results
    from jiuwenswarm.server.runtime.group_service import (
        group_organization_teams,
        update_group_organization_plan,
    )

    plan_id = str(plan["plan_id"])
    key = result_key(session_id, str(plan["owner_member"]))
    future = asyncio.get_running_loop().create_future()
    _results[key] = future
    started_at = _now_ms()
    owner_backend = await _live_backend(runtime, team_id, session_id)
    task_pool = getattr(owner_backend, "org_task_manager", None)

    async def _refresh_card(
        *,
        status: str = "running",
        message_id: str | None = None,
        fail_unfinished: bool = False,
        create: bool = True,
    ) -> None:
        await publish_organization_card(
            session_id=session_id,
            plan=plan,
            organization_id=organization_id,
            team_id=team_id,
            task_pool=task_pool,
            status=status,
            message_id=message_id,
            fail_unfinished=fail_unfinished,
            create=create,
        )

    if task_pool is None:
        _results.pop(key, None)
        try:
            await _refresh_card(status="failed", fail_unfinished=True, create=False)
        except Exception:
            logger.exception(
                "group organization card update failed session=%s", session_id
            )
        _fail_open_organization_plan(
            session_id,
            plan_id,
            "owner instance is not bound to the organization",
        )
        raise RuntimeError("owner instance is not bound to the organization")
    completed = False
    failed_error: str | None = None
    card_marked_failed = False
    result = ""
    posted = None
    start_watchdog: asyncio.Task | None = None

    async def _refresh_failed_card() -> None:
        """Mark the card failed even when this wait is already cancelled.

        ``CancelledError`` is not an ``Exception``, so the handler above never
        runs. A cancelled task also raises again at the next await. Clear that
        count for this write, then put it back so the cancellation still
        propagates.
        """
        current = asyncio.current_task()
        pending = 0
        if current is not None:
            while current.cancelling():
                current.uncancel()
                pending += 1
        try:
            await _refresh_card(status="failed", fail_unfinished=True)
        except Exception:
            logger.exception(
                "group organization card update failed session=%s", session_id
            )
        finally:
            if current is not None:
                for _ in range(pending):
                    current.cancel()
    try:
        if ensure_members:
            launcher = getattr(org_runtime, "_expert_team_launcher", None)
            bound_runtime, bound_org = runtime, org_runtime
            if launcher is None:
                bound_runtime, bound_org, launcher = _organization_runtime()
            await _ensure_mentioned_member_instances(
                runtime=bound_runtime,
                org_runtime=bound_org,
                launcher=launcher,
                session_id=session_id,
                plan=plan,
                organization_id=organization_id,
                record_backend=backend,
            )
        missing = await _wait_for_members(
            runtime,
            session_id=session_id,
            organization_id=organization_id,
            members=list(plan["members"]),
        )
        if missing:
            raise RuntimeError(
                "organization members did not call run_team: " + ", ".join(missing)
            )
        update_group_organization_plan(
            session_id,
            plan_id,
            status="running",
            started_at=started_at,
        )
        await _owner_primes_root_task(
            task_pool=task_pool,
            team_id=team_id,
            session_id=session_id,
            plan=plan,
        )
        await _refresh_card()
        start_watchdog = asyncio.create_task(
            _queue_owner_start_reminder(
                org_runtime=org_runtime,
                task_pool=task_pool,
                team_id=team_id,
                session_id=session_id,
                plan_id=plan_id,
            )
        )
        result, attachments = await _await_completed_result(
            future,
            task_pool,
            plan_id,
            on_tick=_refresh_card,
            session_id=session_id,
            organization_id=organization_id,
        )
        completed = True
        posted = await backend.append_group_message(
            backend.member_name,
            result,
            attachments=attachments,
            client_message_id=f"expert-result-{plan_id}",
            mentions=[],
        )
        await _refresh_card(
            status="completed", message_id=posted.message.message_id
        )
    except Exception as exc:
        failed_error = str(exc)
        await _refresh_card(status="failed", fail_unfinished=True)
        card_marked_failed = True
        from jiuwenswarm.server.runtime.group_execution import post_member_error

        await post_member_error(backend, backend.member_name, failed_error, f"expert-error-{plan_id}")
        raise
    finally:
        try:
            if start_watchdog is not None:
                start_watchdog.cancel()
            _results.pop(key, None)
            try:
                await pause_organization_instances(
                    session_id=session_id,
                    task_pool=task_pool,
                    team_ids=[
                        str(item["team_id"])
                        for item in group_organization_teams(session_id)
                        if item.get("team_id")
                    ],
                    include_summary=completed,
                )
            except Exception:
                logger.exception(
                    "group organization pause failed session=%s", session_id
                )
            # Release while this plan is still the live one, then mark it done
            # or failed. Marking it earlier let the next queued round bind, and
            # this finally then released that new binding. A failed round
            # releases too, but only while the instance still points at this
            # organization.
            try:
                bound_now = await _live_backend(runtime, team_id, session_id)
                current = _bound_organization(bound_now)
                if current and current != organization_id:
                    logger.info(
                        "group organization release skipped; instance moved "
                        "session=%s plan=%s bound=%s",
                        session_id,
                        plan_id,
                        current,
                    )
                else:
                    await dissolve_completed_swarm_organization(
                        org_runtime=org_runtime,
                        task_pool=task_pool,
                        session_id=session_id,
                        organization_id=organization_id,
                        owner_team_id=team_id,
                    )
            except Exception:
                logger.exception(
                    "group organization dissolve failed session=%s",
                    session_id,
                )
        finally:
            # Cancellation is not an Exception, so the status write lives in
            # this inner finally and still runs when the wait is cancelled.
            try:
                if failed_error is not None:
                    update_group_organization_plan(
                        session_id, plan_id, status="failed", error=failed_error
                    )
                elif completed and posted is not None:
                    update_group_organization_plan(
                        session_id,
                        plan_id,
                        status="done",
                        message_id=posted.message.message_id,
                    )
                else:
                    _fail_open_organization_plan(
                        session_id,
                        plan_id,
                        "organization wait ended before the round finished",
                    )
                    if not card_marked_failed:
                        await _refresh_failed_card()
            except Exception:
                logger.exception(
                    "group organization status update failed session=%s plan=%s",
                    session_id,
                    plan_id,
                )
            _plan_event(session_id, plan_id).set()
            release_deferred_direct(session_id, plan_id)
    return ToolOutput(
        success=True,
        data={
            "result": result,
            "message_id": posted.message.message_id,
            "posted_to_group": True,
            "organization_id": organization_id,
            "role": "owner",
        },
    )


async def _stop_swarm_summary_team(
    org_runtime: Any, *, team_id: str, session_id: str
) -> None:
    """Stop the Summary Team for a finished SwarmChat organization round."""
    name = str(team_id or "").strip()
    if not name:
        return
    launcher = getattr(org_runtime, "_summary_team_launcher", None)
    stop = getattr(launcher, "stop", None)
    if not callable(stop):
        return
    try:
        await stop(team_id=name, session_id=session_id)
    except Exception:
        logger.warning(
            "swarm summary team stop failed session=%s team=%s",
            session_id,
            name,
            exc_info=True,
        )


async def dissolve_completed_swarm_organization(
    *,
    org_runtime: Any,
    task_pool: Any,
    session_id: str,
    organization_id: str,
    owner_team_id: str,
) -> None:
    """Erase SwarmChat organization rows; keep expert-team instances alive.

    Used after success and after failure/timeout. Callers still propagate the
    original exception / ``plan.error``. Unlike agent-core
    ``dissolve_organization``, this path never stops ``org-expert-*`` teams, so
    each instance keeps its own conversation context. Summary Team is stopped
    when a launcher is present.
    """
    from jiuwenswarm.server.runtime.group_service import (
        clear_group_organization_binding,
    )

    summary_team_id = ""
    try:
        getter = getattr(task_pool, "get_summary_team", None)
        if callable(getter):
            summary = await getter()
            if summary is not None:
                summary_team_id = str(
                    getattr(summary, "summary_team_id", "") or ""
                ).strip()
    except Exception:
        logger.exception(
            "swarm organization summary lookup failed session=%s org=%s",
            session_id,
            organization_id,
        )

    try:
        release = getattr(org_runtime, "release_membership", None)
        if callable(release):
            try:
                await release(
                    organization_id=organization_id,
                    owner_team_id=owner_team_id,
                    session_id=session_id,
                )
            except Exception:
                logger.exception(
                    "swarm organization membership release failed session=%s org=%s",
                    session_id,
                    organization_id,
                )
        # release_membership only drops leaders; erase the remaining org rows.
        eraser = getattr(task_pool, "dissolve_organization", None)
        if callable(eraser):
            try:
                await eraser()
            except Exception:
                logger.exception(
                    "swarm organization row erase failed session=%s org=%s",
                    session_id,
                    organization_id,
                )
        else:
            releaser = getattr(task_pool, "release_membership", None)
            if callable(releaser):
                try:
                    await releaser()
                except Exception:
                    logger.exception(
                        "swarm organization pool release failed session=%s org=%s",
                        session_id,
                        organization_id,
                    )
        if summary_team_id:
            await _stop_swarm_summary_team(
                org_runtime, team_id=summary_team_id, session_id=session_id
            )
    finally:
        clear_group_organization_binding(session_id)


async def unregister_completed_organization(
    *,
    org_runtime: Any,
    task_pool: Any,
    session_id: str,
    organization_id: str,
    owner_team_id: str,
) -> None:
    """Backward-compatible alias for :func:`dissolve_completed_swarm_organization`."""
    await dissolve_completed_swarm_organization(
        org_runtime=org_runtime,
        task_pool=task_pool,
        session_id=session_id,
        organization_id=organization_id,
        owner_team_id=owner_team_id,
    )


async def dissolve_group_organization(group_id: str) -> None:
    """Rebind after a restart if needed, then dissolve before group deletion."""
    from jiuwenswarm.server.runtime.group_service import (
        group_organization_teams,
        read_group,
    )

    state = read_group(group_id) or {}
    organization_id = str(state.get("organization_id") or "")
    teams = group_organization_teams(group_id)
    owner_member = owner_member_from_teams(teams)
    owner_team_id = owner_team_id_from_teams(teams)
    if not organization_id or not owner_member or not owner_team_id:
        return
    runtime, org_runtime, launcher = _organization_runtime()
    roster = {
        item.get("member_name"): item
        for item in state.get("members") or []
        if isinstance(item, dict)
    }
    # A restarted worker has durable DB rows but no pool entries. Relaunch the
    # fixed instances so agent-core can resolve the Owner and dissolve cleanly.
    async with _lock_for(group_id):
        for item in teams:
            member_name = str(item.get("member_name") or "")
            member = roster.get(member_name)
            if member is None:
                continue
            await ensure_member_instance(
                runtime=runtime,
                launcher=launcher,
                session_id=group_id,
                outer_team_id=str(state.get("team_name") or ""),
                member=member,
                is_owner=member_name == owner_member,
            )
        owner = roster.get(owner_member) or {}
        await sync_organization_membership(
            runtime=runtime,
            org_runtime=org_runtime,
            session_id=group_id,
            organization_id=organization_id,
            owner_member=owner_member,
            owner_display_name=(owner.get("display_name") or owner_member),
            teams=teams,
        )
    await org_runtime.dissolve_organization(
        organization_id=organization_id,
        owner_team_id=owner_team_id,
        session_id=group_id,
    )
