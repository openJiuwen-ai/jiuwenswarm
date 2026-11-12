# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""主库上的 TeamEntity / TeamMembership。方法语义与 SQLite 实现一致。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jiuwenswarm.infrastructure.db.team_store_actor import (
    fetch_all,
    fetch_one,
    get_team_store_actor,
    is_unique_violation,
    json_value_expr,
    run_tx,
)
from jiuwenswarm.enterprise.team.control_store import (
    DEFAULT_HUMAN_PERMISSIONS,
    MEMBERSHIP_STATUSES,
    MEMBER_TYPE_HUMAN,
    TEAM_STATUS_CREATING,
    TEAM_STATUS_DELETED,
    TEAM_STATUSES,
    OwnerIdentity,
    TeamControlStoreError,
    TeamEntityRecord,
    TeamMembershipRecord,
    _ENTITY_UPDATABLE,
    _clean,
)


def _json_obj(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return fallback
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return loaded


def _entity_from_row(row: dict[str, Any]) -> TeamEntityRecord:
    extra = _json_obj(row.get("extra"), {})
    return TeamEntityRecord(
        team_id=row["team_id"],
        team_name=row["team_name"],
        owner=OwnerIdentity(
            user_id=row["owner_user_id"] or "",
            group_id=row["owner_group_id"] or "",
            bot_id=row["owner_bot_id"] or "",
        ),
        status=row["status"],
        runtime_mode=row["runtime_mode"] or "local",
        template_id=row["template_id"] or "",
        team_session_id=row["team_session_id"] or "",
        runtime_team_name=row["runtime_team_name"] or "",
        agent_service_id=row["agent_service_id"] or "",
        gateway_id=row["gateway_id"] or "",
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        extra=extra if isinstance(extra, dict) else {},
    )


def _membership_from_row(row: dict[str, Any]) -> TeamMembershipRecord:
    perms = _json_obj(row.get("permissions"), [])
    expires = row.get("expires_at")
    return TeamMembershipRecord(
        membership_id=row["membership_id"],
        team_id=row["team_id"],
        identity=OwnerIdentity(
            user_id=row["user_id"] or "",
            group_id=row["group_id"] or "",
            bot_id=row["bot_id"] or "",
        ),
        member_name=row["member_name"],
        member_type=row["member_type"] or MEMBER_TYPE_HUMAN,
        display_name=row["display_name"] or "",
        permissions=tuple(str(p) for p in perms) if isinstance(perms, list) else (),
        status=row["status"],
        expires_at=None if expires is None else float(expires),
        joined_at=float(row["joined_at"]),
        updated_at=float(row["updated_at"]),
    )


class SharedTeamControlStore:
    """``TeamControlStore`` backed by the shared team store."""

    def __init__(self) -> None:
        get_team_store_actor()

    @staticmethod
    def _handler() -> Any:
        return get_team_store_actor().handler

    @staticmethod
    def _run(coro_fn: Any) -> Any:
        return get_team_store_actor().call(coro_fn)

    def create_entity(
        self,
        *,
        team_name: str,
        owner: OwnerIdentity,
        template_id: str = "",
        team_session_id: str = "",
        runtime_team_name: str = "",
        status: str = TEAM_STATUS_CREATING,
        team_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TeamEntityRecord:
        name = _clean(team_name)
        if not name:
            raise TeamControlStoreError("team_name is required", code="BAD_REQUEST")
        if status not in TEAM_STATUSES:
            raise TeamControlStoreError(f"invalid team status: {status}", code="BAD_REQUEST")
        now = time.time()
        record = TeamEntityRecord(
            team_id=_clean(team_id) or uuid.uuid4().hex,
            team_name=name,
            owner=owner,
            status=status,
            template_id=_clean(template_id),
            team_session_id=_clean(team_session_id),
            runtime_team_name=_clean(runtime_team_name),
            created_at=now,
            updated_at=now,
            extra=dict(extra or {}),
        )

        async def _op() -> None:
            handler = self._handler()
            try:
                await run_tx(
                    handler,
                    lambda conn: conn.execute(
                        text(
                            "INSERT INTO team_entity ("
                            "team_id, team_name, owner_user_id, owner_group_id, owner_bot_id, "
                            "status, runtime_mode, template_id, team_session_id, runtime_team_name, "
                            "agent_service_id, gateway_id, created_at, updated_at, extra"
                            ") VALUES ("
                            ":team_id, :team_name, :owner_user_id, :owner_group_id, :owner_bot_id, "
                            ":status, :runtime_mode, :template_id, :team_session_id, :runtime_team_name, "
                            ":agent_service_id, :gateway_id, :created_at, :updated_at, "
                            + json_value_expr("extra")
                            + ")"
                        ),
                        {
                            "team_id": record.team_id,
                            "team_name": record.team_name,
                            "owner_user_id": owner.user_id,
                            "owner_group_id": owner.group_id,
                            "owner_bot_id": owner.bot_id,
                            "status": record.status,
                            "runtime_mode": record.runtime_mode,
                            "template_id": record.template_id,
                            "team_session_id": record.team_session_id,
                            "runtime_team_name": record.runtime_team_name,
                            "agent_service_id": record.agent_service_id,
                            "gateway_id": record.gateway_id,
                            "created_at": record.created_at,
                            "updated_at": record.updated_at,
                            "extra": json.dumps(record.extra, ensure_ascii=False),
                        },
                    ),
                )
            except IntegrityError as exc:
                if is_unique_violation(exc):
                    raise TeamControlStoreError(
                        "team_name or team_id already exists", code="CONFLICT"
                    ) from exc
                raise

        self._run(_op)
        return record

    def get_entity(self, team_id: str) -> TeamEntityRecord | None:
        tid = _clean(team_id)
        if not tid:
            return None

        async def _op() -> dict[str, Any] | None:
            return await fetch_one(
                self._handler(),
                "SELECT * FROM team_entity WHERE team_id = :team_id",
                {"team_id": tid},
            )

        row = self._run(_op)
        return _entity_from_row(row) if row else None

    def get_entity_by_name(self, team_name: str) -> TeamEntityRecord | None:
        name = _clean(team_name)
        if not name:
            return None

        async def _op() -> dict[str, Any] | None:
            return await fetch_one(
                self._handler(),
                "SELECT * FROM team_entity WHERE team_name = :team_name",
                {"team_name": name},
            )

        row = self._run(_op)
        return _entity_from_row(row) if row else None

    def get_entity_by_session(self, session_id: str) -> TeamEntityRecord | None:
        sid = _clean(session_id)
        if not sid:
            return None

        async def _op() -> dict[str, Any] | None:
            return await fetch_one(
                self._handler(),
                "SELECT * FROM team_entity WHERE team_session_id = :sid "
                "ORDER BY updated_at DESC LIMIT 1",
                {"sid": sid},
            )

        row = self._run(_op)
        return _entity_from_row(row) if row else None

    def list_entities(
        self,
        *,
        owner: OwnerIdentity | None = None,
        include_deleted: bool = False,
    ) -> list[TeamEntityRecord]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if owner is not None:
            clauses.append(
                "owner_user_id = :uid AND owner_group_id = :gid AND owner_bot_id = :bid"
            )
            params.update(
                {"uid": owner.user_id, "gid": owner.group_id, "bid": owner.bot_id}
            )
        if not include_deleted:
            clauses.append("status <> :deleted")
            params["deleted"] = TEAM_STATUS_DELETED
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        async def _op() -> list[dict[str, Any]]:
            return await fetch_all(
                self._handler(),
                f"SELECT * FROM team_entity {where} ORDER BY updated_at DESC",
                params,
            )

        return [_entity_from_row(row) for row in self._run(_op)]

    def update_entity(self, team_id: str, **fields: Any) -> TeamEntityRecord | None:
        tid = _clean(team_id)
        if not tid:
            return None
        unknown = set(fields) - _ENTITY_UPDATABLE
        if unknown:
            raise TeamControlStoreError(f"unknown entity fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in TEAM_STATUSES:
            raise TeamControlStoreError(f"invalid team status: {fields['status']}")
        if not fields:
            return self.get_entity(tid)
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "extra":
                values[key] = json.dumps(dict(value or {}), ensure_ascii=False)
            else:
                values[key] = _clean(value)
        values["updated_at"] = time.time()
        assignments = []
        params: dict[str, Any] = {"team_id": tid}
        for key, value in values.items():
            if key == "extra":
                assignments.append(f"extra = {json_value_expr('extra')}")
            else:
                assignments.append(f"{key} = :{key}")
            params[key] = value

        async def _op() -> int:
            handler = self._handler()
            try:
                result = await run_tx(
                    handler,
                    lambda conn: conn.execute(
                        text(
                            f"UPDATE team_entity SET {', '.join(assignments)} "
                            "WHERE team_id = :team_id"
                        ),
                        params,
                    ),
                )
                return int(result.rowcount or 0)
            except IntegrityError as exc:
                if is_unique_violation(exc):
                    raise TeamControlStoreError(
                        "team_name already exists", code="CONFLICT"
                    ) from exc
                raise

        if self._run(_op) == 0:
            return None
        return self.get_entity(tid)

    def set_status(self, team_id: str, status: str) -> TeamEntityRecord | None:
        return self.update_entity(team_id, status=status)

    def set_status_by_name(self, team_name: str, status: str) -> TeamEntityRecord | None:
        entity = self.get_entity_by_name(team_name)
        if entity is None:
            return None
        return self.update_entity(entity.team_id, status=status)

    def delete_entity(self, team_id: str) -> bool:
        tid = _clean(team_id)
        if not tid:
            return False

        async def _op() -> bool:
            async def _tx(conn: Any) -> bool:
                await conn.execute(
                    text("DELETE FROM team_membership WHERE team_id = :tid"),
                    {"tid": tid},
                )
                result = await conn.execute(
                    text("DELETE FROM team_entity WHERE team_id = :tid"),
                    {"tid": tid},
                )
                return int(result.rowcount or 0) > 0

            return await run_tx(self._handler(), _tx)

        return bool(self._run(_op))

    def upsert_membership(
        self,
        *,
        team_id: str,
        identity: OwnerIdentity,
        member_name: str,
        display_name: str = "",
        member_type: str = MEMBER_TYPE_HUMAN,
        permissions: Iterable[str] | None = None,
        status: str = "pending",
        expires_at: float | None = None,
    ) -> TeamMembershipRecord:
        tid = _clean(team_id)
        name = _clean(member_name)
        if not tid or not name:
            raise TeamControlStoreError("team_id and member_name are required")
        if not _clean(identity.user_id):
            raise TeamControlStoreError("user_id is required")
        if status not in MEMBERSHIP_STATUSES:
            raise TeamControlStoreError(f"invalid membership status: {status}")
        perms = tuple(permissions) if permissions is not None else DEFAULT_HUMAN_PERMISSIONS
        now = time.time()

        async def _op() -> TeamMembershipRecord:
            async def _tx(conn: Any) -> TeamMembershipRecord:
                result = await conn.execute(
                    text(
                        "SELECT * FROM team_membership "
                        "WHERE team_id = :tid AND user_id = :uid FOR UPDATE"
                    ),
                    {"tid": tid, "uid": identity.user_id},
                )
                existing_row = result.mappings().first()
                payload_perms = json.dumps(list(perms), ensure_ascii=False)
                if existing_row is not None:
                    existing = _membership_from_row(dict(existing_row))
                    record = replace(
                        existing,
                        identity=identity,
                        member_name=name,
                        display_name=_clean(display_name) or existing.display_name,
                        member_type=member_type,
                        permissions=perms,
                        status=status,
                        expires_at=expires_at,
                        updated_at=now,
                    )
                    await conn.execute(
                        text(
                            "UPDATE team_membership SET group_id=:group_id, bot_id=:bot_id, "
                            "member_name=:member_name, member_type=:member_type, "
                            "display_name=:display_name, "
                            f"permissions={json_value_expr('permissions')}, "
                            "status=:status, expires_at=:expires_at, updated_at=:updated_at "
                            "WHERE membership_id=:membership_id"
                        ),
                        {
                            "group_id": identity.group_id,
                            "bot_id": identity.bot_id,
                            "member_name": record.member_name,
                            "member_type": record.member_type,
                            "display_name": record.display_name,
                            "permissions": payload_perms,
                            "status": record.status,
                            "expires_at": record.expires_at,
                            "updated_at": record.updated_at,
                            "membership_id": record.membership_id,
                        },
                    )
                    return record
                record = TeamMembershipRecord(
                    membership_id=uuid.uuid4().hex,
                    team_id=tid,
                    identity=identity,
                    member_name=name,
                    member_type=member_type,
                    display_name=_clean(display_name),
                    permissions=perms,
                    status=status,
                    expires_at=expires_at,
                    joined_at=now,
                    updated_at=now,
                )
                await conn.execute(
                    text(
                        "INSERT INTO team_membership ("
                        "membership_id, team_id, user_id, group_id, bot_id, member_name, "
                        "member_type, display_name, permissions, status, expires_at, "
                        "joined_at, updated_at"
                        ") VALUES ("
                        ":membership_id, :team_id, :user_id, :group_id, :bot_id, :member_name, "
                        ":member_type, :display_name, "
                        + json_value_expr("permissions")
                        + ", :status, :expires_at, :joined_at, :updated_at)"
                    ),
                    {
                        "membership_id": record.membership_id,
                        "team_id": record.team_id,
                        "user_id": identity.user_id,
                        "group_id": identity.group_id,
                        "bot_id": identity.bot_id,
                        "member_name": record.member_name,
                        "member_type": record.member_type,
                        "display_name": record.display_name,
                        "permissions": payload_perms,
                        "status": record.status,
                        "expires_at": record.expires_at,
                        "joined_at": record.joined_at,
                        "updated_at": record.updated_at,
                    },
                )
                return record

            try:
                return await run_tx(self._handler(), _tx)
            except IntegrityError as exc:
                if is_unique_violation(exc):
                    raise TeamControlStoreError(
                        f"member_name '{name}' is already taken", code="CONFLICT"
                    ) from exc
                raise

        return self._run(_op)

    def get_membership(self, team_id: str, user_id: str) -> TeamMembershipRecord | None:
        tid, uid = _clean(team_id), _clean(user_id)
        if not tid or not uid:
            return None

        async def _op() -> dict[str, Any] | None:
            return await fetch_one(
                self._handler(),
                "SELECT * FROM team_membership WHERE team_id = :tid AND user_id = :uid",
                {"tid": tid, "uid": uid},
            )

        row = self._run(_op)
        return _membership_from_row(row) if row else None

    def get_membership_by_member_name(
        self, team_id: str, member_name: str
    ) -> TeamMembershipRecord | None:
        tid, name = _clean(team_id), _clean(member_name)
        if not tid or not name:
            return None

        async def _op() -> dict[str, Any] | None:
            return await fetch_one(
                self._handler(),
                "SELECT * FROM team_membership WHERE team_id = :tid AND member_name = :name",
                {"tid": tid, "name": name},
            )

        row = self._run(_op)
        return _membership_from_row(row) if row else None

    def list_memberships(
        self, team_id: str, *, statuses: Iterable[str] | None = None
    ) -> list[TeamMembershipRecord]:
        tid = _clean(team_id)
        if not tid:
            return []
        params: dict[str, Any] = {"tid": tid}
        where = "team_id = :tid"
        if statuses is not None:
            wanted = [s for s in statuses if s]
            if not wanted:
                return []
            keys = []
            for index, status in enumerate(wanted):
                key = f"s{index}"
                keys.append(f":{key}")
                params[key] = status
            where += f" AND status IN ({', '.join(keys)})"

        async def _op() -> list[dict[str, Any]]:
            return await fetch_all(
                self._handler(),
                f"SELECT * FROM team_membership WHERE {where} ORDER BY joined_at ASC",
                params,
            )

        return [_membership_from_row(row) for row in self._run(_op)]

    def set_membership_status(
        self, membership_id: str, status: str
    ) -> TeamMembershipRecord | None:
        mid = _clean(membership_id)
        if not mid:
            return None
        if status not in MEMBERSHIP_STATUSES:
            raise TeamControlStoreError(f"invalid membership status: {status}")
        now = time.time()

        async def _op() -> dict[str, Any] | None:
            async def _tx(conn: Any) -> dict[str, Any] | None:
                result = await conn.execute(
                    text(
                        "UPDATE team_membership SET status = :status, updated_at = :now "
                        "WHERE membership_id = :mid"
                    ),
                    {"status": status, "now": now, "mid": mid},
                )
                if int(result.rowcount or 0) == 0:
                    return None
                loaded = await conn.execute(
                    text("SELECT * FROM team_membership WHERE membership_id = :mid"),
                    {"mid": mid},
                )
                row = loaded.mappings().first()
                return dict(row) if row is not None else None

            return await run_tx(self._handler(), _tx)

        row = self._run(_op)
        return _membership_from_row(row) if row else None
