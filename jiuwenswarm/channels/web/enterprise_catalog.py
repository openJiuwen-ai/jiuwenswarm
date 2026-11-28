# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the user-facing projection of the Manager database."""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, and_, or_, select,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from e2a.match_expressions import matches

_SCOPE_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def _active(row: Any) -> bool:
    if not row.enabled:
        return False
    expires = row.expires_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def _matches_agent_rule(rule: Any, user_id: str, group_id: str, bot_id: str = "") -> bool:
    try:
        return matches(rule, {"user_id": user_id, "group_id": group_id, "bot_id": bot_id})
    except ValueError:
        return False


def _scope_id(resource_id: str) -> str:
    """Mirror Manager's runtime scope ordering key."""
    safe = _SCOPE_SAFE.sub("-", resource_id).strip("-")
    if not safe:
        safe = uuid.uuid5(uuid.NAMESPACE_URL, resource_id).hex
    return f"service-{safe}"[:128]


@dataclass(frozen=True)
class User:
    user_id: str
    orgs: dict[str, str]

    @property
    def groups(self) -> dict[str, str]:
        return self.orgs or {"__none__": "无组织"}


class Catalog:
    """Read-only access to database views. The database grants enforce the boundary."""

    def __init__(self, schema: str | None):
        metadata = MetaData(schema=schema)
        self.instances = Table(
            "user_access_instance", metadata,
            Column("jiuwenclaw_id", String), Column("jiuwenclaw_name", String),
            Column("gateway_http", String), Column("gateway_ws", String),
        )
        self.grants = Table(
            "user_access_instance_grant", metadata,
            Column("jiuwenclaw_id", String), Column("subject_type", String),
            Column("subject_id", String), Column("login_policy", String),
            Column("enabled", Boolean), Column("expires_at", DateTime),
        )
        self.agents = Table(
            "user_access_agent_resource", metadata,
            Column("jiuwenclaw_id", String), Column("resource_id", String),
            Column("resource_name", String), Column("match_expr", JSON),
            Column("enabled", Boolean), Column("expires_at", DateTime),
            Column("ref_template_id", String),
            Column("upload_local_skill", Boolean),
        )
        self.agent_definitions = Table(
            "user_access_agent_definition", metadata,
            Column("agent_config_template_id", String),
            Column("runtime_template_id", String), Column("agent_name", String),
        )
        self.service_resources = Table(
            "user_access_instance_service_resource", metadata,
            Column("jiuwenclaw_id", String), Column("resource_id", String),
            Column("ref_template_id", String), Column("priority", Integer),
            Column("match_expr", JSON), Column("enabled", Boolean),
            Column("expires_at", DateTime),
        )

    async def _admitted(
        self, db: AsyncConnection, user: User, jid: str | None = None,
    ) -> set[str]:
        grants = self.grants.c
        query = select(self.grants).where(or_(
            and_(grants.subject_type == "user", grants.subject_id == user.user_id),
            and_(grants.subject_type == "org", grants.subject_id.in_(user.orgs)),
        ))
        if jid:
            query = query.where(grants.jiuwenclaw_id == jid)
        allowed: set[str] = set()
        denied: set[str] = set()
        for row in await db.execute(query):
            if _active(row):
                policy = (row.login_policy or "allow").strip().lower()
                targets = denied if policy == "deny" else allowed
                targets.add(row.jiuwenclaw_id)
        return allowed - denied

    @staticmethod
    def _matches(row: Any, user: User, group: str) -> bool:
        return (
            _active(row) and _matches_agent_rule(row.match_expr, user.user_id, group)
        )

    async def contexts(
        self, db: AsyncConnection, user: User,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(contexts, matched)`` for the user.

        ``matched`` counts the admitted agent/group combinations before the Agent
        template gate, so the caller can distinguish ``no_agent_template`` from
        ``no_grant``. Agent names come from the configured Agent template; pairs
        missing there are not exposed to the user plane.
        """
        admitted = await self._admitted(db, user)
        if not admitted:
            return [], 0
        instances = self.instances.c
        agents = self.agents.c
        query = select(agents, instances.jiuwenclaw_name).join(
            self.instances, agents.jiuwenclaw_id == instances.jiuwenclaw_id,
        ).where(agents.jiuwenclaw_id.in_(admitted), agents.enabled.is_(True))
        names = await self._configured_names(db)
        scopes = await self._runtime_scopes(db, admitted)
        result = []
        matched = 0
        for row in await db.execute(query):
            for group, name in user.groups.items():
                if not self._matches(row, user, group):
                    continue
                matched += 1
                runtime_template_id = self._resolve_runtime(
                    scopes.get(row.jiuwenclaw_id, []), user, group, row.resource_id,
                )
                agent_template_id = (row.ref_template_id or "").strip()
                configured_name = names.get((agent_template_id, runtime_template_id))
                if not configured_name:
                    continue
                result.append({
                    "bot_id": row.resource_id, "group_id": group, "user_id": user.user_id,
                    "jiuwenclaw_id": row.jiuwenclaw_id,
                    "runtime_template_id": runtime_template_id,
                    "agent_template_id": agent_template_id,
                    "agent_name": configured_name,
                    "allow_local_skill_upload": bool(row.upload_local_skill),
                    "group_name": name, "jiuwenclaw_name": row.jiuwenclaw_name or row.jiuwenclaw_id,
                })
        return sorted(result, key=lambda item: (
            item["agent_name"], item["group_name"], item["jiuwenclaw_name"],
            item["bot_id"], item["group_id"], item["jiuwenclaw_id"],
        )), matched

    async def _configured_names(
        self, db: AsyncConnection,
    ) -> dict[tuple[str, str], str]:
        """``(Agent 配置模板 id, 运行时模板 id)`` → 管理面配置的 Agent 名称。"""
        names: dict[tuple[str, str], str] = {}
        for row in await db.execute(select(self.agent_definitions)):
            name = (row.agent_name or "").strip()
            if name:
                names[(row.agent_config_template_id, row.runtime_template_id or "")] = name
        return names

    async def _runtime_scopes(
        self, db: AsyncConnection, jids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Per-instance runtime scope entries sorted by ``(priority, scope_id)``."""
        out: dict[str, list[dict[str, Any]]] = {}
        if not jids:
            return out
        resources = self.service_resources.c
        query = select(resources).where(resources.jiuwenclaw_id.in_(jids))
        for row in await db.execute(query):
            resource_id = (row.resource_id or "").strip()
            template_id = (row.ref_template_id or "").strip()
            if not _active(row) or not resource_id or not template_id:
                continue
            out.setdefault(row.jiuwenclaw_id, []).append({
                "scope_id": _scope_id(resource_id),
                "priority": int(row.priority or 0),
                "ref_template_id": template_id,
                "match_expr": row.match_expr,
            })
        for entries in out.values():
            entries.sort(key=lambda item: (item["priority"], item["scope_id"]))
        return out

    @staticmethod
    def _resolve_runtime(entries: list[dict[str, Any]], user: User, group: str,
                         bot_id: str) -> str:
        for entry in entries:
            if _matches_agent_rule(entry["match_expr"], user.user_id, group, bot_id):
                return entry["ref_template_id"]
        return ""

    async def resolve(
        self, db: AsyncConnection, user: User, jid: str, bot: str = "", group: str = "",
    ) -> Any | None:
        if not jid or not await self._admitted(db, user, jid):
            return None
        if group and group not in user.groups:
            return None
        if bot:
            if not group:
                return None
            agent = (await db.execute(select(self.agents).where(
                self.agents.c.jiuwenclaw_id == jid, self.agents.c.resource_id == bot,
            ))).one_or_none()
            if agent is None or not self._matches(agent, user, group):
                return None
        return (await db.execute(select(self.instances).where(
            self.instances.c.jiuwenclaw_id == jid,
        ))).one_or_none()

    def tables(self) -> Iterable[Table]:
        return (
            self.instances, self.grants, self.agents,
            self.agent_definitions, self.service_resources,
        )
