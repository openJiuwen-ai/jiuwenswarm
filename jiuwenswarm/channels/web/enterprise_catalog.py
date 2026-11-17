# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the user-facing projection of the Manager database."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, MetaData, String, Table, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from e2a.match_expressions import matches


def _active(row: Any) -> bool:
    if not row.enabled:
        return False
    expires = row.expires_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def _matches_agent_rule(rule: Any, user_id: str, group_id: str) -> bool:
    try:
        return matches(rule, {"user_id": user_id, "group_id": group_id, "bot_id": ""})
    except ValueError:
        return False


@dataclass(frozen=True)
class User:
    user_id: str
    orgs: dict[str, str]

    @property
    def groups(self) -> dict[str, str]:
        return self.orgs or {"__none__": "无组织"}


class Catalog:
    """Read-only access to three database views. The database grants enforce the boundary."""

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

    async def contexts(self, db: AsyncConnection, user: User) -> list[dict[str, str]]:
        admitted = await self._admitted(db, user)
        if not admitted:
            return []
        instances = self.instances.c
        agents = self.agents.c
        query = select(agents, instances.jiuwenclaw_name).join(
            self.instances, agents.jiuwenclaw_id == instances.jiuwenclaw_id,
        ).where(agents.jiuwenclaw_id.in_(admitted), agents.enabled.is_(True))
        result = []
        for row in await db.execute(query):
            for group, name in user.groups.items():
                if self._matches(row, user, group):
                    result.append({
                        "bot_id": row.resource_id, "group_id": group, "user_id": user.user_id,
                        "jiuwenclaw_id": row.jiuwenclaw_id,
                        "agent_name": row.resource_name or row.resource_id,
                        "group_name": name, "jiuwenclaw_name": row.jiuwenclaw_name or row.jiuwenclaw_id,
                    })
        return sorted(result, key=lambda item: (
            item["agent_name"], item["group_name"], item["jiuwenclaw_name"],
            item["bot_id"], item["group_id"], item["jiuwenclaw_id"],
        ))

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
        return (self.instances, self.grants, self.agents)
