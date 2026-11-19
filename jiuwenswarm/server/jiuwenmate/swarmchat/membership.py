"""Resolve live colleague membership from the authoritative group roster."""

from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.runtime.group_service import read_group


def colleague_members(group: dict) -> list[dict]:
    return [item for item in group.get("members", []) if item.get("kind") == "colleague"]


def groups_for(agent_id: str, *, owner: str | None = None) -> list[dict]:
    groups = []
    for path in get_agent_sessions_dir().glob("*/group.json"):
        group = read_group(path.parent.name)
        if group is None or group.get("status") == "deleting":
            continue
        if owner is not None and group.get("user_id") != owner:
            continue
        if any(item["id"] == agent_id for item in colleague_members(group)):
            groups.append(group)
    return groups


def require_membership(group_id: str, agent_id: str) -> tuple[dict, dict]:
    group = read_group(group_id)
    if group is not None and group.get("status") != "deleting":
        for item in colleague_members(group):
            if item["id"] == agent_id:
                return group, item
    raise PersistentAgentError("SwarmChat membership is unavailable", code="FORBIDDEN")


class ColleagueGroupDirectory:
    """Metadata-only view across the same colleague's IM identities and SwarmChat memberships."""

    def __init__(self, repository: Any, service: Any, *, owner: str | None = None) -> None:
        self._repository = repository
        self._service = service
        self._owner = owner

    def directory(self, namespace_id: str, agent_id: str) -> tuple[dict, ...]:
        colleague = self._service.get_config(namespace_id, agent_id)
        if not colleague.enabled:
            return ()
        rows = {}
        if self._repository is not None:
            for identity in self._service.list_agents(namespace_id):
                if identity.config_id != colleague.agent_id or not identity.enabled:
                    continue
                for row in self._repository.directory(namespace_id, identity.agent_id):
                    key = row["platform"], row["group_id"]
                    if key not in rows or row["status"] == "active":
                        rows[key] = row
        for group in groups_for(colleague.agent_id, owner=self._owner):
            rows["swarmchat", group["group_id"]] = {
                "platform": "swarmchat", "group_id": group["group_id"], "name": group["name"], "status": "active",
            }
        return tuple(rows[key] for key in sorted(rows))


ROSTER_GUIDANCE_LIMIT = 30
_DESCRIPTION_LIMIT = 80
_KIND_LABELS = {"agent_template": "expert", "agent_group": "expert team", "colleague": "AI colleague"}


def roster(
    group: dict, *, self_agent_id: str | None = None, description_limit: int | None = _DESCRIPTION_LIMIT,
) -> list[dict]:
    """Every member of the group, read from the authoritative group.json roster."""
    members = []
    for item in group.get("members", []):
        compiled = group.get("compiled", {}).get(item["member_name"], {}).get("member", {})
        description = str(item.get("description") or compiled.get("desc") or "").strip()
        is_colleague = item.get("kind") == "colleague"
        members.append({
            "member_name": item["member_name"],
            "display_name": item.get("display_name") or compiled.get("display_name") or item["member_name"],
            "kind": _KIND_LABELS.get(item.get("kind"), str(item.get("kind") or "member")),
            "description": description if description_limit is None or len(description) <= description_limit
            else description[:description_limit] + "…",
            "mention": f"[[agent:{item['member_name']}]]",
            "is_self": is_colleague and self_agent_id is not None and item.get("id") == self_agent_id,
        })
    return members
