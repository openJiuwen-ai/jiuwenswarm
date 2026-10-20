"""Message-mediated collaboration helpers for Feishu group conversations."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MENTION_TOKEN = re.compile(r"\[\[agent:([A-Za-z0-9_-]{1,128})\]\]")
_INVALID_MENTION_TOKEN = re.compile(r"\[\[agent:.*?\]\]", re.DOTALL)


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
        "\n\n群内协作规则：用户点名的第一位同事先完成自己的部分。"
        "如果还要名单中的另一位同事接着做，在最终回复里说明交接内容，"
        "并在目标名称处使用名单对应的交接标记。下一位会在看到这条公开消息后再继续，"
        "不要替对方完成后一步。"
        "接力只能通过当前飞书群的公开消息完成；不要声称已经在后台调用了其他 Agent，"
        "也不要使用名单以外的员工。每条回复最多交接给一位同事。\n"
        f"当前群内可用同事：\n{roster_lines}"
    )


def earliest_named_colleague(text: str, roster: Iterable[Mapping[str, Any]]) -> str | None:
    """Return the colleague whose name occurs first. A longer name wins the same span."""
    if not isinstance(text, str) or not text:
        return None
    folded = text.casefold()
    best: tuple[int, int, str] | None = None
    for member in _unique_members(roster):
        name = member["name"].casefold()
        start = 0
        while True:
            index = folded.find(name, start)
            if index < 0:
                break
            candidate = (index, -len(name), member["agent_id"])
            if best is None or candidate < best:
                best = candidate
            start = index + 1
    return None if best is None else best[2]


def waits_for_named_colleague(
    text: str,
    roster: Iterable[Mapping[str, Any]],
    self_agent_id: str,
) -> bool:
    """A later-named colleague waits; an unnamed broadcast still lets everyone judge."""
    primary = earliest_named_colleague(text, roster)
    return primary is not None and primary != self_agent_id


def resolve_group_mentions(
    text: str,
    roster: Iterable[Mapping[str, Any]],
    *,
    sender_agent_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Keep tokens only for active, unambiguous group members other than the sender."""
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
