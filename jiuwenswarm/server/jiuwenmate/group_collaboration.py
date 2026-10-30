"""Message-mediated collaboration helpers for group conversations."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MENTION_TOKEN = re.compile(r"\[\[agent:([A-Za-z0-9_-]{1,128})\]\]")
_INVALID_MENTION_TOKEN = re.compile(r"\[\[agent:.*?\]\]", re.DOTALL)
_DUTY_BOUNDARY = (
    "职责边界：参与判断时，具体任务与你的职责完全无关，decision 就必须是 ignore，不要回答。"
    "不要因为还没有同事回复，或者觉得具体任务容易、属于通用生活问题，就替职责相符的同事补位。"
    "只有具体任务属于你的职责，或与你的职责直接相关时，才可以 respond 或 intervene。"
    "这条不改变已经被明确点名时的回复。"
    "面向群成员的轻量日常交流不按具体任务处理，范围从宽判断。"
    "它包括寒暄、近况、随口闲聊，以及询问各位自身情况的问题。"
    "遇到这类消息，decision 用 respond，用一两句简短回应。"
    "问题在问大家各自的情况时，只介绍自己，不要替其他同事作答。"
    "不要借轻量日常交流去完成职责以外的具体任务。\n"
)
_GREETING = re.compile(
    r"^(?:大家好|各位好|你好|您好|嗨喽|早上好|下午好|晚上好|早安|晚安|在不在|在吗|在么|hello|hey|hi|嗨)"
    r"[呀啊哦哟吧呢了！!。.~～?？,，\s]*$",
    re.IGNORECASE,
)


def is_pure_greeting(text: str) -> bool:
    """A greeting with no task. A request that merely starts with a greeting is not one."""
    if not isinstance(text, str):
        return False
    compact = re.sub(r"\s+", "", text).strip()
    return bool(compact) and _GREETING.fullmatch(compact) is not None


def greeting_respondent(roster: Iterable[Mapping[str, Any]]) -> str | None:
    """Pick one adaptive colleague to answer a pure greeting.

    A colleague whose name or description concerns daily life is preferred.
    Passive colleagues are left out. The choice is stable for one roster.
    """
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in roster:
        agent_id = item.get("agent_id")
        name = item.get("name")
        if not isinstance(agent_id, str) or not isinstance(name, str):
            continue
        agent_id = agent_id.strip()
        if agent_id in seen or not _SAFE_AGENT_ID.fullmatch(agent_id):
            continue
        if item.get("group_reply_mode") == "passive":
            continue
        seen.add(agent_id)
        description = item.get("description")
        candidates.append({
            "agent_id": agent_id,
            "name": name.strip(),
            "description": description if isinstance(description, str) else "",
        })
    if not candidates:
        return None

    def sort_key(member: dict[str, str]) -> tuple[int, str, str]:
        daily = 0 if "生活" in f"{member['name']}{member['description']}" else 1
        return (daily, member["name"], member["agent_id"])

    return min(candidates, key=sort_key)["agent_id"]


def build_group_execution_guidance(roster: Iterable[Mapping[str, Any]]) -> str:
    """Return the duty boundary plus collaboration guidance for one group execution.

    Participation reads this addendum before it decides. An empty roster still
    keeps the duty boundary, so a colleague does not answer outside its role.
    """
    collaboration = build_group_collaboration_guidance(roster)
    if not collaboration:
        return _DUTY_BOUNDARY
    return _DUTY_BOUNDARY + collaboration


def build_group_collaboration_guidance(roster: Iterable[Mapping[str, Any]]) -> str:
    """Return per-execution guidance listing only currently available group colleagues."""
    members = _unique_members(roster)
    if not members:
        return ""
    roster_lines = "\n".join(
        f"- {json.dumps(member['name'], ensure_ascii=False)}（交接标记：[[agent:{member['agent_id']}]]）"
        for member in members
    )
    return (
        "\n\n群内协作规则：用户让大家各自回复、分别说明或做同一件事时，只完成自己的部分，"
        "不要点名其他同事，不要写交接说明，也不要使用交接标记。"
        "只有用户明确安排了一位同事先完成自己的部分，再让另一位同事继续一项进一步的工作时，"
        "才在最终回复里说明要交给对方的事项，并在对方名称处使用名单对应的交接标记。"
        "用户对同事的称呼可以和名单上的登记名不同。由你判断用户指的是不是名单中的某一位："
        "是，就使用该同事的交接标记；名单中没有这个人，就不要编造标记。"
        "下一位会在看到这条公开消息后再继续，不要替对方完成后一步。"
        "没有这种先后安排时，最终回复里不得出现交接标记。"
        "接力只能通过当前群的公开消息完成；不要声称已经在后台调用了其他 Agent，"
        "也不要使用名单以外的员工。每条回复最多交接给一位同事，"
        "接到交接的同事不要再交接回去。\n"
        f"当前群内可用同事：\n{roster_lines}"
    )


def resolve_group_mentions(
    text: str,
    roster: Iterable[Mapping[str, Any]],
    *,
    sender_agent_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Keep one token for a roster colleague the model selected. The model decides who the user meant."""
    members = _unique_members(roster)
    by_id: dict[str, dict[str, Any]] = {}
    for member in members:
        if member["agent_id"] != sender_agent_id:
            by_id[member["agent_id"]] = member

    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        agent_id = match.group(1)
        member = by_id.get(agent_id)
        if member is None:
            return ""
        if agent_id in seen or seen:
            return ""
        targets.append(member)
        seen.add(agent_id)
        return match.group(0)

    cleaned = _MENTION_TOKEN.sub(replace, text)
    cleaned = _INVALID_MENTION_TOKEN.sub(
        lambda match: match.group(0) if _MENTION_TOKEN.fullmatch(match.group(0)) else "",
        cleaned,
    )
    return cleaned, targets


def _unique_members(roster: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    members: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    for item in roster:
        agent_id = item.get("agent_id")
        name = item.get("name")
        if not isinstance(agent_id, str) or not _SAFE_AGENT_ID.fullmatch(agent_id.strip()):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        name = re.sub(r"[\x00-\x1f\x7f]", " ", name).strip()[:256]
        if not name:
            continue
        app_ids = item.get("app_ids", [])
        if not isinstance(app_ids, list):
            app_ids = []
        member: dict[str, Any] = {"agent_id": agent_id.strip(), "name": name.strip()}
        if app_ids:
            member["app_ids"] = sorted({value for value in app_ids if isinstance(value, str) and value.strip()})
        if member["agent_id"] in conflicts:
            continue
        existing = members.get(member["agent_id"])
        if existing is None:
            members[member["agent_id"]] = member
        elif existing["name"] != member["name"]:
            members.pop(member["agent_id"], None)
            conflicts.add(member["agent_id"])
        elif member.get("app_ids"):
            existing["app_ids"] = sorted(set(existing.get("app_ids", [])) | set(member["app_ids"]))
    return [members[key] for key in sorted(members)]
