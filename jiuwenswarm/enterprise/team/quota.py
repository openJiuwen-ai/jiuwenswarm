# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team quota: policy selection, atomic reserve/commit/release, usage views.

Occupancy lives in ``team_quota_lease``. ``team_quota_account`` is only the
mutex row for per-user team-create (X). ``team_quota_org_account`` is the mutex
row for organization team-create (Z). Y locks the existing ``team_entity`` row.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_gateway.enterprise.team.quota_policy import (
    TeamQuotaPolicyStore,
    DEFAULT_MAX_TEAMMATES_PER_TEAM,
    DEFAULT_MAX_TEAMS_PER_USER,
    KIND_TEAMMATE_SPAWN,
    KIND_TEAM_CREATE,
    QuotaLimits,
    STATUS_COMMITTED,
    STATUS_RESERVED,
    _clean,
    _lease_still_held,
    _now,
    leader_spawn_names,
)
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.enterprise.team.control_store import TEAM_ACTIVE_STATUSES, OwnerIdentity

logger = logging.getLogger(__name__)

KIND_ORG_TEAM_CREATE = "org_team_create"
KIND_REQUESTED_TEAM_SIZE = "requested_team_size"

STATUS_RELEASED = "released"

LEASE_TTL_SEC = 60.0
# A held reservation has no expiry. Gateway and AgentServer both release it
# when it is still reserved this long after the last hold stamp.
RESERVED_HOLD_MAX_SEC = 30 * 60
HOLD_REFRESH_INTERVAL_SEC = 5 * 60
FEATURE_ENABLED_ENV = "TEAM_QUOTA_ENABLED"

QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class SessionAlreadyHasTeam(Exception):
    """The session already has a team entity that is not deleted."""


class TeamQuotaExceeded(Exception):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        used: int,
        limit: int,
        default: int,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.used = used
        self.limit = limit
        self.default = default

    def as_quota(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "used": self.used,
            "limit": self.limit,
            "default": self.default,
        }


@dataclass
class QuotaLease:
    lease_id: str
    kind: str
    idempotency_key: str
    status: str
    noop: bool = False


def team_quota_enabled() -> bool:
    raw = str(os.environ.get(FEATURE_ENABLED_ENV, "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _same_owner(row: dict[str, Any], identity: OwnerIdentity) -> bool:
    if _clean(row.get("user_id")) != identity.user_id:
        return False
    if _clean(row.get("group_id")) != identity.group_id:
        return False
    return _clean(row.get("bot_id")) == identity.bot_id


def team_create_key(session_id: str) -> str:
    return f"session:{_clean(session_id)}"


def _leased_session_ids(rows: list[dict[str, Any]]) -> set[str]:
    prefix = "session:"
    found: set[str] = set()
    for row in rows:
        key = _clean(row.get("idempotency_key"))
        if key.startswith(prefix):
            session_id = key[len(prefix):].strip()
            if session_id:
                found.add(session_id)
    return found


def _read_live_team_session(metadata_path: Path) -> tuple[str, str] | None:
    """Return ``(session_id, team_name)`` when the session is still a team."""
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    mode = str(payload.get("mode") or "").strip().lower()
    if mode != "team" and not mode.startswith("team."):
        return None
    team_name = str(payload.get("team_name") or "").strip()
    session_id = str(payload.get("session_id") or metadata_path.parent.name).strip()
    if not team_name or not session_id or session_id in {".", ".."}:
        return None
    return session_id, team_name


def iter_unleased_team_sessions(sessions_dir: Path, leased_session_ids: set[str]) -> list[tuple[str, str]]:
    """Session teams under ``sessions_dir`` that do not already hold a lease."""
    if not sessions_dir.is_dir():
        return []
    found: list[tuple[str, str]] = []
    try:
        children = list(sessions_dir.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or child.name in {".", ".."}:
            continue
        hit = _read_live_team_session(child / "metadata.json")
        if hit is None or hit[0] in leased_session_ids:
            continue
        found.append(hit)
    return found


def _unleased_user_session_teams(
    identity: OwnerIdentity,
    leased_session_ids: set[str],
) -> list[tuple[str, str]]:
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return []
    if not identity.user_id or not identity.group_id or not identity.bot_id:
        return []
    try:
        from jiuwenswarm.common.utils import build_tenant_key, get_agent_root_dir

        sessions_dir = get_agent_root_dir(
            build_tenant_key(identity.group_id, identity.bot_id, identity.user_id)
        ) / "sessions"
    except (OSError, ValueError):
        logger.debug("[TeamQuota] user session team scan skipped", exc_info=True)
        return []
    return iter_unleased_team_sessions(sessions_dir, leased_session_ids)


def _reserve_session_id(kind: str, key: str) -> str:
    """Session a ``team_create`` reservation is for, if the key encodes one."""
    if kind != KIND_TEAM_CREATE:
        return ""
    prefix = "session:"
    text = _clean(key)
    if not text.startswith(prefix):
        return ""
    return text[len(prefix):].strip()


def _live_control_teams(
    *,
    identity: OwnerIdentity | None = None,
    group_id: str = "",
) -> list[tuple[str, str, str]]:
    """Active control-plane teams as ``(team_id, session_id, label)``.

    Stopped and deleted teams do not occupy quota. A missing store is treated
    as no extra teams so a quota read never fails closed on the catalog.
    """
    from jiuwenswarm.enterprise.edition import is_enterprise

    gid = _clean(group_id)
    if not is_enterprise():
        return []
    if identity is not None and not identity.user_id:
        return []
    if identity is None and not gid:
        return []
    try:
        from jiuwenswarm.enterprise.team.control_store import get_team_control_store

        entities = get_team_control_store().list_entities(
            owner=identity,
            include_deleted=False,
        )
    except Exception:
        logger.debug("[TeamQuota] control-plane team scan skipped", exc_info=True)
        return []
    found: list[tuple[str, str, str]] = []
    for entity in entities:
        if str(getattr(entity, "status", "") or "") not in TEAM_ACTIVE_STATUSES:
            continue
        owner = getattr(entity, "owner", None)
        if gid and _clean(getattr(owner, "group_id", "")) != gid:
            continue
        team_id = _clean(getattr(entity, "team_id", ""))
        session_id = _clean(getattr(entity, "team_session_id", ""))
        label = _clean(getattr(entity, "team_name", "")) or team_id or session_id
        if not label:
            continue
        found.append((team_id, session_id, label))
    return found


def _labels_beyond_leases(
    *,
    entities: list[tuple[str, str, str]],
    sessions: list[tuple[str, str]],
    leased_team_ids: set[str],
    leased_session_ids: set[str],
) -> list[str]:
    """Teams that occupy a slot but have no occupying ``team_create`` lease."""
    labels: list[str] = []
    seen_ids = set(leased_team_ids)
    seen_sessions = set(leased_session_ids)
    for team_id, session_id, label in entities:
        if team_id and team_id in seen_ids:
            continue
        if session_id and session_id in seen_sessions:
            continue
        labels.append(label)
        if team_id:
            seen_ids.add(team_id)
        if session_id:
            seen_sessions.add(session_id)
    for session_id, team_name in sessions:
        if not session_id or session_id in seen_sessions or not team_name:
            continue
        labels.append(team_name)
        seen_sessions.add(session_id)
    return labels


def _unleased_org_session_teams(
    identity: OwnerIdentity,
    leased_session_ids: set[str],
) -> list[tuple[str, str]]:
    """Unleased session teams of this identity that also occupy the org quota.

    Tenant directories are ``domain/workspace_{md5}``. The hash cannot be
    reversed to a group id, so other members' session directories are not
    scanned here. Their live teams are counted from the control plane.
    """
    if not _clean(identity.group_id):
        return []
    return _unleased_user_session_teams(identity, leased_session_ids)


def teammate_spawn_key(team_id: str, member_name: str) -> str:
    return f"team:{_clean(team_id)}:member:{_clean(member_name)}"


def team_create_quota_user_message(limit: int) -> str:
    """User-facing X-quota failure. ``limit`` is max teams per user."""
    return f"创建失败，一个用户最多有{max(int(limit), 0)}个团队"


def org_team_create_quota_user_message(limit: int) -> str:
    """User-facing Z-quota failure. ``limit`` is max teams in the organization."""
    return f"创建失败，一个组织最多有{max(int(limit), 0)}个团队"


def team_quota_user_message(exc: TeamQuotaExceeded) -> str:
    """Chinese sentence for a create-time quota block. Other kinds stay internal."""
    if exc.kind == KIND_ORG_TEAM_CREATE:
        return org_team_create_quota_user_message(exc.limit)
    if exc.kind == KIND_TEAM_CREATE:
        return team_create_quota_user_message(exc.limit)
    return str(exc)


def is_leader_member_name(member_name: str, leader_member_name: str = "") -> bool:
    return _clean(member_name) in set(leader_spawn_names(leader_member_name))


def teammate_spawn_slots(team_limit_including_leader: int) -> int:
    """``max_teammates_per_team`` is whole-team size N including the leader.

    Spawn leases only cover non-leader members, so the slot cap is ``N - 1``.
    N=3 → two ``spawn_teammate`` calls.
    """
    return max(int(team_limit_including_leader) - 1, 0)


def count_predefined_non_leaders(config_base: dict[str, Any] | None, template_id: str | None) -> int:
    """How many template members would occupy Y (role != leader)."""
    cfg = config_base if isinstance(config_base, dict) else {}
    modes = cfg.get("modes") if isinstance(cfg.get("modes"), dict) else {}
    teams = modes.get("team") if isinstance(modes.get("team"), dict) else {}
    team_raw: dict[str, Any] = {}
    tid = _clean(template_id)
    if tid and isinstance(teams.get(tid), dict):
        team_raw = teams[tid]
    elif isinstance(cfg.get("team"), dict):
        team_raw = cfg["team"]
    else:
        for item in teams.values():
            if isinstance(item, dict):
                team_raw = item
                break
    members = team_raw.get("predefined_members") or []
    if not isinstance(members, list):
        return 0
    n = 0
    for item in members:
        if not isinstance(item, dict):
            continue
        if not _clean(item.get("member_name")):
            continue
        role = _clean(item.get("role_type") or item.get("role") or "teammate").lower()
        if role != "leader":
            n += 1
    return n


_OCCUPY_SQL = (
    "(status = :committed OR (status = :reserved AND (expires_at IS NULL OR expires_at > :now)))"
)

_ABANDONED_HOLD_SQL = (
    "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
    "WHERE kind=:kind AND status=:res AND expires_at IS NULL "
    "AND updated_at<=:cutoff"
)


def _abandoned_hold_params(now: float) -> dict[str, Any]:
    return {
        "rel": STATUS_RELEASED,
        "now": now,
        "kind": KIND_TEAM_CREATE,
        "res": STATUS_RESERVED,
        "cutoff": now - RESERVED_HOLD_MAX_SEC,
    }

_SQLITE_OCCUPY = (
    "(status=? OR (status=? AND (expires_at IS NULL OR expires_at>?)))"
)


def _postgres_team_store() -> bool:
    from jiuwenswarm.infrastructure.db.team_store_actor import team_store_db_type

    return team_store_db_type() == "postgres"


def _insert_ignore_sql(mysql_sql: str, postgres_sql: str) -> str:
    if _postgres_team_store():
        return postgres_sql
    return mysql_sql


class TeamQuotaService(TeamQuotaPolicyStore):
    """Process-wide quota operations. Backed by Gateway MySQL or local SQLite."""

    @staticmethod
    def _default_sqlite_path() -> Path:
        return get_user_workspace_dir() / "agent" / "teams" / "team_control.db"

    @staticmethod
    def _quota_config() -> dict[str, Any]:
        from jiuwenswarm.common.config import get_config

        return get_config()

    def reserve_team_create(
        self,
        identity: OwnerIdentity,
        session_id: str,
        *,
        pending_non_leaders: int | None = None,
        on_existing: str = "ignore",
    ) -> QuotaLease:
        if not team_quota_enabled():
            return QuotaLease(lease_id="", kind=KIND_TEAM_CREATE, idempotency_key="", status=STATUS_RESERVED, noop=True)
        key = team_create_key(session_id)
        if self._use_mysql():
            # Policies, live entities, and the reservation share one actor call.
            lease = self._reserve_mysql(
                kind=KIND_TEAM_CREATE,
                key=key,
                identity=identity,
                team_id="",
                member_name="",
                limit=0,
                default=0,
                lock="account",
                pending_non_leaders=pending_non_leaders,
                on_existing=on_existing,
            )
            if lease is None:
                return QuotaLease(
                    lease_id="",
                    kind=KIND_TEAM_CREATE,
                    idempotency_key=key,
                    status=STATUS_RESERVED,
                    noop=True,
                )
            return lease
        limits = self.resolve_limits(identity)
        return self._reserve(
            kind=KIND_TEAM_CREATE,
            key=key,
            identity=identity,
            team_id="",
            member_name="",
            limit=limits.max_teams_per_user,
            default=limits.default_teams,
            lock="account",
            org_limit=limits.max_teams_per_org,
            org_default=limits.default_org_teams,
        )

    def reserve_teammate_spawn(
        self,
        *,
        team_id: str,
        member_name: str,
        owner: OwnerIdentity,
        leader_member_name: str = "",
    ) -> QuotaLease:
        if not team_quota_enabled():
            return QuotaLease(
                lease_id="",
                kind=KIND_TEAMMATE_SPAWN,
                idempotency_key="",
                status=STATUS_RESERVED,
                noop=True,
            )
        tid = _clean(team_id)
        name = _clean(member_name)
        if not tid or not name:
            raise ValueError("team_id and member_name are required")
        if is_leader_member_name(name, leader_member_name):
            # build_team registers the leader via spawn_member; that is not a Y slot.
            return QuotaLease(
                lease_id="",
                kind=KIND_TEAMMATE_SPAWN,
                idempotency_key=teammate_spawn_key(tid, name),
                status=STATUS_RESERVED,
                noop=True,
            )
        if self._use_mysql():
            return self._reserve_mysql(
                kind=KIND_TEAMMATE_SPAWN,
                key=teammate_spawn_key(tid, name),
                identity=owner,
                team_id=tid,
                member_name=name,
                limit=0,
                default=0,
                lock="team",
                exclude_member_names=leader_spawn_names(leader_member_name),
            )
        limits = self.resolve_limits(owner)
        n = int(limits.max_teammates_per_team)
        try:
            return self._reserve(
                kind=KIND_TEAMMATE_SPAWN,
                key=teammate_spawn_key(tid, name),
                identity=owner,
                team_id=tid,
                member_name=name,
                limit=teammate_spawn_slots(n),
                default=teammate_spawn_slots(limits.default_teammates),
                lock="team",
                exclude_member_names=leader_spawn_names(leader_member_name),
            )
        except TeamQuotaExceeded as exc:
            raise TeamQuotaExceeded(
                f"team quota exceeded: {int(exc.used) + 1}/{n} members including leader",
                kind=exc.kind,
                used=int(exc.used) + 1,
                limit=n,
                default=int(limits.default_teammates),
            ) from exc

    def commit_lease(
        self,
        lease: QuotaLease | str,
        *,
        team_id: str = "",
        member_name: str = "",
    ) -> None:
        if isinstance(lease, QuotaLease) and lease.noop:
            return
        lid = lease.lease_id if isinstance(lease, QuotaLease) else _clean(lease)
        if not lid:
            return
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:st, "
                            "team_id=CASE WHEN :tid='' THEN team_id ELSE :tid END, "
                            "member_name=CASE WHEN :mn='' THEN member_name ELSE :mn END, "
                            "expires_at=NULL, updated_at=:now WHERE lease_id=:lid AND status<>:rel"
                        ),
                        {
                            "st": STATUS_COMMITTED,
                            "tid": _clean(team_id),
                            "mn": _clean(member_name),
                            "now": now,
                            "lid": lid,
                            "rel": STATUS_RELEASED,
                        },
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            self._sqlite().execute(
                "UPDATE team_quota_lease SET status=?, team_id=CASE WHEN ?='' THEN team_id ELSE ? END, "
                "member_name=CASE WHEN ?='' THEN member_name ELSE ? END, expires_at=NULL, updated_at=? "
                "WHERE lease_id=? AND status<>?",
                (
                    STATUS_COMMITTED,
                    _clean(team_id),
                    _clean(team_id),
                    _clean(member_name),
                    _clean(member_name),
                    now,
                    lid,
                    STATUS_RELEASED,
                ),
            )

    def hold_reserved_create(self, session_id: str) -> None:
        """Keep a chat reservation occupying until the resident stream ends.

        Only a still-reserved team_create row is touched. Clearing ``expires_at``
        stops the 60s TTL from dropping it before ``team.runtime_ready``.
        A committed lease is left alone, and no team entity is inserted.
        """
        sid = _clean(session_id)
        if not sid or not self._use_mysql():
            return
        from sqlalchemy import text

        from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

        key = team_create_key(sid)
        now = _now()
        actor = self._mysql()

        async def _op() -> None:
            async def _tx(conn: Any) -> None:
                await conn.execute(
                    text(
                        "UPDATE team_quota_lease SET expires_at=NULL, updated_at=:now "
                        "WHERE kind=:kind AND idempotency_key=:key AND status=:res"
                    ),
                    {
                        "now": now,
                        "kind": KIND_TEAM_CREATE,
                        "key": key,
                        "res": STATUS_RESERVED,
                    },
                )

            await run_tx(actor.handler, _tx)

        actor.call(_op)

    def release_reserved_create(self, session_id: str) -> None:
        """Release a chat reservation only while it is still reserved. One MySQL call."""
        sid = _clean(session_id)
        if not sid or not self._use_mysql():
            return
        from sqlalchemy import text

        from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

        key = team_create_key(sid)
        now = _now()
        actor = self._mysql()

        async def _op() -> None:
            async def _tx(conn: Any) -> None:
                row = (
                    await conn.execute(
                        text(
                            "SELECT status FROM team_quota_lease "
                            "WHERE kind=:kind AND idempotency_key=:key"
                        ),
                        {"kind": KIND_TEAM_CREATE, "key": key},
                    )
                ).mappings().first()
                if row is None or str(row["status"]) != STATUS_RESERVED:
                    return
                await conn.execute(
                    text(
                        "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                        "WHERE kind=:kind AND idempotency_key=:key AND status=:res"
                    ),
                    {
                        "rel": STATUS_RELEASED,
                        "res": STATUS_RESERVED,
                        "now": now,
                        "kind": KIND_TEAM_CREATE,
                        "key": key,
                    },
                )

            await run_tx(actor.handler, _tx)

        actor.call(_op)

    def release_lease(self, lease: QuotaLease | str) -> None:
        if isinstance(lease, QuotaLease) and lease.noop:
            return
        lid = lease.lease_id if isinstance(lease, QuotaLease) else _clean(lease)
        if not lid:
            return
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                            "WHERE lease_id=:lid AND status<>:rel"
                        ),
                        {"rel": STATUS_RELEASED, "now": now, "lid": lid},
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            self._sqlite().execute(
                "UPDATE team_quota_lease SET status=?, updated_at=? WHERE lease_id=? AND status<>?",
                (STATUS_RELEASED, now, lid, STATUS_RELEASED),
            )

    def release_team(self, team_id: str) -> None:
        tid = _clean(team_id)
        if not tid:
            return
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                            "WHERE team_id=:tid AND status<>:rel"
                        ),
                        {"rel": STATUS_RELEASED, "now": now, "tid": tid},
                    )
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                            "WHERE kind=:kind AND status<>:rel AND team_id=:tid"
                        ),
                        {"rel": STATUS_RELEASED, "now": now, "kind": KIND_TEAM_CREATE, "tid": tid},
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            conn = self._sqlite()
            conn.execute(
                "UPDATE team_quota_lease SET status=?, updated_at=? WHERE team_id=? AND status<>?",
                (STATUS_RELEASED, now, tid, STATUS_RELEASED),
            )

    def peek_team_create_lease(self, session_id: str) -> dict[str, str] | None:
        """Return the team_create lease for ``session:{session_id}``, if any."""
        sid = _clean(session_id)
        if not sid:
            return None
        key = team_create_key(sid)
        if self._use_mysql():
            from jiuwenswarm.infrastructure.db.team_store_actor import record_to_dict

            actor = self._mysql()

            async def _op() -> dict[str, Any] | None:
                record = await actor.handler.get(
                    "team_quota_lease",
                    {"kind": KIND_TEAM_CREATE, "idempotency_key": key},
                )
                return record_to_dict(record) if record is not None else None

            row = actor.call(_op)
            if not row:
                return None
            return {
                "user_id": _clean(row.get("user_id")),
                "group_id": _clean(row.get("group_id")),
                "bot_id": _clean(row.get("bot_id")),
                "team_id": _clean(row.get("team_id")),
                "status": _clean(row.get("status")),
            }
        with self._lock:
            found = self._sqlite().execute(
                "SELECT user_id, group_id, bot_id, team_id, status "
                "FROM team_quota_lease WHERE kind=? AND idempotency_key=?",
                (KIND_TEAM_CREATE, key),
            ).fetchone()
        if found is None:
            return None
        return {
            "user_id": _clean(found["user_id"]),
            "group_id": _clean(found["group_id"]),
            "bot_id": _clean(found["bot_id"]),
            "team_id": _clean(found["team_id"]),
            "status": _clean(found["status"]),
        }

    def drop_occupancy(self, *, session_id: str = "", team_id: str = "") -> None:
        """Physically delete X/Y lease rows for a deleted session or team.

        ``team.stop`` keeps rows as ``released``. Session delete follows develop
        (the team is gone) and removes occupancy so the lease table is empty.

        When only ``session_id`` is known, the create-lease ``team_id`` is
        resolved so teammate (Y) rows are dropped with the X row.
        """
        sid = _clean(session_id)
        tid = _clean(team_id)
        if not tid and sid:
            try:
                peeked = self.peek_team_create_lease(sid)
            except Exception:  # noqa: BLE001
                peeked = None
            if peeked:
                tid = _clean(peeked.get("team_id"))
        if not sid and not tid:
            return
        key = team_create_key(sid) if sid else ""
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    if tid:
                        await conn.execute(
                            text("DELETE FROM team_quota_lease WHERE team_id=:tid"),
                            {"tid": tid},
                        )
                    if key:
                        await conn.execute(
                            text(
                                "DELETE FROM team_quota_lease WHERE kind=:kind "
                                "AND idempotency_key=:key"
                            ),
                            {"kind": KIND_TEAM_CREATE, "key": key},
                        )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            conn = self._sqlite()
            if tid:
                conn.execute("DELETE FROM team_quota_lease WHERE team_id=?", (tid,))
            if key:
                conn.execute(
                    "DELETE FROM team_quota_lease WHERE kind=? AND idempotency_key=?",
                    (KIND_TEAM_CREATE, key),
                )

    def release_teammate(self, team_id: str, member_name: str) -> None:
        key = teammate_spawn_key(team_id, member_name)
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                            "WHERE kind=:kind AND idempotency_key=:key AND status<>:rel"
                        ),
                        {
                            "rel": STATUS_RELEASED,
                            "now": now,
                            "kind": KIND_TEAMMATE_SPAWN,
                            "key": key,
                        },
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            self._sqlite().execute(
                "UPDATE team_quota_lease SET status=?, updated_at=? "
                "WHERE kind=? AND idempotency_key=? AND status<>?",
                (STATUS_RELEASED, now, KIND_TEAMMATE_SPAWN, key, STATUS_RELEASED),
            )

    def snapshot_user(self, identity: OwnerIdentity) -> dict[str, Any]:
        if self._use_mysql():
            return self._snapshot_user_mysql(identity)
        limits = self.resolve_limits(identity)
        used, ids = self._count_user_teams(identity)
        payload: dict[str, Any] = {
            "teams": {
                "used": used,
                "limit": limits.max_teams_per_user,
                "default": limits.default_teams,
                "occupying_ids": ids,
            },
            "teammates_default_limit": limits.default_teammates,
        }
        if identity.group_id and limits.max_teams_per_org > 0:
            payload["org"] = {
                "group_id": identity.group_id,
                "used": self._count_org_teams(identity),
                "limit": limits.max_teams_per_org,
            }
        return payload

    def snapshot_team(self, team_id: str, owner: OwnerIdentity) -> dict[str, Any]:
        limits = self.resolve_limits(owner)
        return {
            "used": self._count_team_members_including_leader(team_id),
            "limit": limits.max_teammates_per_team,
            "default": limits.default_teammates,
        }

    def reconcile_mysql(self) -> None:
        """Pass A: SQL-only, safe on any AgentServer."""
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                            "WHERE status=:res AND expires_at IS NOT NULL AND expires_at<=:now"
                        ),
                        {"rel": STATUS_RELEASED, "now": now, "res": STATUS_RESERVED},
                    )
                    await conn.execute(text(_ABANDONED_HOLD_SQL), _abandoned_hold_params(now))
                    active = ",".join(f"'{s}'" for s in sorted(TEAM_ACTIVE_STATUSES))
                    result = await conn.execute(
                        text(
                            f"SELECT team_id, owner_user_id, owner_group_id, owner_bot_id, team_session_id "
                            f"FROM team_entity WHERE status IN ({active})"
                        )
                    )
                    entities = [dict(r) for r in result.mappings().all()]
                    for ent in entities:
                        tid = _clean(ent.get("team_id"))
                        existing = await conn.execute(
                            text(
                                "SELECT lease_id FROM team_quota_lease WHERE kind=:kind AND team_id=:tid "
                                "AND status IN (:res, :com)"
                            ),
                            {
                                "kind": KIND_TEAM_CREATE,
                                "tid": tid,
                                "res": STATUS_RESERVED,
                                "com": STATUS_COMMITTED,
                            },
                        )
                        if existing.first() is not None:
                            continue
                        sid = _clean(ent.get("team_session_id"))
                        key = team_create_key(sid or tid)
                        await conn.execute(
                            text(
                                _insert_ignore_sql(
                                    "INSERT IGNORE INTO team_quota_lease ("
                                    "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
                                    "team_id, member_name, status, expires_at, created_at, updated_at"
                                    ") VALUES ("
                                    ":lid, :kind, :key, :uid, :gid, :bid, :tid, '', :st, NULL, :now, :now)",
                                    "INSERT INTO team_quota_lease ("
                                    "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
                                    "team_id, member_name, status, expires_at, created_at, updated_at"
                                    ") VALUES ("
                                    ":lid, :kind, :key, :uid, :gid, :bid, :tid, '', :st, NULL, :now, :now) "
                                    "ON CONFLICT (kind, idempotency_key) DO NOTHING",
                                )
                            ),
                            {
                                "lid": uuid.uuid4().hex,
                                "kind": KIND_TEAM_CREATE,
                                "key": key,
                                "uid": _clean(ent.get("owner_user_id")),
                                "gid": _clean(ent.get("owner_group_id")),
                                "bid": _clean(ent.get("owner_bot_id")),
                                "tid": tid,
                                "st": STATUS_COMMITTED,
                                "now": now,
                            },
                        )
                    stopped = await conn.execute(
                        text(
                            "SELECT team_id FROM team_entity WHERE status NOT IN "
                            f"({active})"
                        )
                    )
                    for row in stopped.mappings().all():
                        tid = _clean(row.get("team_id"))
                        await conn.execute(
                            text(
                                "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                                "WHERE team_id=:tid AND status<>:rel"
                            ),
                            {"rel": STATUS_RELEASED, "now": now, "tid": tid},
                        )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            conn = self._sqlite()
            conn.execute(
                "UPDATE team_quota_lease SET status=?, updated_at=? "
                "WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
                (STATUS_RELEASED, now, STATUS_RESERVED, now),
            )

    def reconcile_local_roster(self, team_id: str, member_names: list[str]) -> None:
        """Pass B: only for teams hosted in this process."""
        tid = _clean(team_id)
        if not tid:
            return
        live = {_clean(n) for n in member_names if _clean(n)}
        now = _now()
        occupying = self._list_occupying(KIND_TEAMMATE_SPAWN, team_id=tid)
        held = {_clean(r.get("member_name")) for r in occupying}
        for name in held - live:
            self.release_teammate(tid, name)
        missing = live - held
        if not missing:
            return
        owner = self._owner_of_team(tid)
        if owner is None:
            return
        for name in missing:
            try:
                lease = self.reserve_teammate_spawn(team_id=tid, member_name=name, owner=owner)
                self.commit_lease(lease, team_id=tid, member_name=name)
            except TeamQuotaExceeded:
                logger.info("[TeamQuota] roster backfill over limit team=%s member=%s", tid, name)
            except Exception:  # noqa: BLE001
                logger.debug("[TeamQuota] roster backfill failed team=%s member=%s", tid, name, exc_info=True)
        _ = now

    @staticmethod
    def _owner_of_team(team_id: str) -> OwnerIdentity | None:
        from jiuwenswarm.enterprise.team.control_store import get_team_control_store

        entity = get_team_control_store().get_entity(team_id)
        return None if entity is None else entity.owner

    def _reserve(
        self,
        *,
        kind: str,
        key: str,
        identity: OwnerIdentity,
        team_id: str,
        member_name: str,
        limit: int,
        default: int,
        lock: str,
        exclude_member_names: tuple[str, ...] = (),
        org_limit: int = 0,
        org_default: int = 0,
    ) -> QuotaLease:
        if self._use_mysql():
            return self._reserve_mysql(
                kind=kind, key=key, identity=identity, team_id=team_id,
                member_name=member_name, limit=limit, default=default, lock=lock,
                exclude_member_names=exclude_member_names,
                org_limit=org_limit, org_default=org_default,
            )
        return self._reserve_sqlite(
            kind=kind, key=key, identity=identity, team_id=team_id,
            member_name=member_name, limit=limit, default=default, lock=lock,
            exclude_member_names=exclude_member_names,
            org_limit=org_limit, org_default=org_default,
        )

    def _reserve_sqlite(
        self,
        *,
        kind: str,
        key: str,
        identity: OwnerIdentity,
        team_id: str,
        member_name: str,
        limit: int,
        default: int,
        lock: str,
        exclude_member_names: tuple[str, ...] = (),
        org_limit: int = 0,
        org_default: int = 0,
    ) -> QuotaLease:
        now = _now()
        with self._lock:
            conn = self._sqlite()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE team_quota_lease SET status=?, updated_at=? "
                    "WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
                    (STATUS_RELEASED, now, STATUS_RESERVED, now),
                )
                existing = conn.execute(
                    "SELECT * FROM team_quota_lease WHERE kind=? AND idempotency_key=?",
                    (kind, key),
                ).fetchone()
                if existing is not None:
                    row = dict(existing)
                    if _lease_still_held(row["status"], row["expires_at"], now):
                        conn.execute("COMMIT")
                        return QuotaLease(
                            lease_id=row["lease_id"],
                            kind=kind,
                            idempotency_key=key,
                            status=row["status"],
                        )
                if lock == "account":
                    user_beyond, org_beyond = self._teams_beyond_leases(
                        identity, _reserve_session_id(kind, key)
                    )
                    gid = identity.group_id
                    if gid and int(org_limit) > 0:
                        conn.execute(
                            "INSERT OR IGNORE INTO team_quota_org_account "
                            "(group_id, created_at, updated_at) VALUES (?,?,?)",
                            (gid, now, now),
                        )
                        org_used = conn.execute(
                            "SELECT COUNT(*) AS n FROM team_quota_lease WHERE kind=? AND group_id=? AND "
                            + _SQLITE_OCCUPY,
                            (
                                KIND_TEAM_CREATE, gid,
                                STATUS_COMMITTED, STATUS_RESERVED, now,
                            ),
                        ).fetchone()["n"]
                        org_used = int(org_used) + org_beyond
                        if int(org_used) >= int(org_limit):
                            conn.execute("COMMIT")
                            raise TeamQuotaExceeded(
                                f"team quota exceeded: {org_used}/{org_limit}",
                                kind=KIND_ORG_TEAM_CREATE,
                                used=int(org_used),
                                limit=int(org_limit),
                                default=int(org_default),
                            )
                    conn.execute(
                        "INSERT OR IGNORE INTO team_quota_account "
                        "(user_id, group_id, bot_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (identity.user_id, identity.group_id, identity.bot_id, now, now),
                    )
                    used = conn.execute(
                        "SELECT COUNT(*) AS n FROM team_quota_lease WHERE kind=? AND user_id=? "
                        "AND group_id=? AND bot_id=? AND "
                        + _SQLITE_OCCUPY,
                        (
                            KIND_TEAM_CREATE, identity.user_id, identity.group_id, identity.bot_id,
                            STATUS_COMMITTED, STATUS_RESERVED, now,
                        ),
                    ).fetchone()["n"]
                    used = int(used) + user_beyond
                else:
                    conn.execute("SELECT team_id FROM team_entity WHERE team_id=?", (team_id,))
                    excluded = tuple(n for n in exclude_member_names if _clean(n))
                    sql = (
                        "SELECT COUNT(*) AS n FROM team_quota_lease WHERE kind=? AND team_id=? AND "
                        "(status=? OR (status=? AND (expires_at IS NULL OR expires_at>?)))"
                    )
                    args: tuple[Any, ...] = (
                        KIND_TEAMMATE_SPAWN, team_id, STATUS_COMMITTED, STATUS_RESERVED, now,
                    )
                    if excluded:
                        sql += " AND member_name NOT IN ({})".format(
                            ",".join("?" * len(excluded))
                        )
                        args = args + excluded
                    used = conn.execute(sql, args).fetchone()["n"]
                if int(used) >= int(limit):
                    conn.execute("COMMIT")
                    raise TeamQuotaExceeded(
                        f"team quota exceeded: {used}/{limit}",
                        kind=kind,
                        used=int(used),
                        limit=int(limit),
                        default=int(default),
                    )
                lid = uuid.uuid4().hex
                if existing is not None:
                    conn.execute(
                        "UPDATE team_quota_lease SET lease_id=?, user_id=?, group_id=?, bot_id=?, "
                        "team_id=?, member_name=?, status=?, expires_at=?, updated_at=? "
                        "WHERE kind=? AND idempotency_key=?",
                        (
                            lid, identity.user_id, identity.group_id, identity.bot_id,
                            team_id, member_name, STATUS_RESERVED, now + LEASE_TTL_SEC, now,
                            kind, key,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO team_quota_lease ("
                        "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
                        "team_id, member_name, status, expires_at, created_at, updated_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            lid, kind, key, identity.user_id, identity.group_id, identity.bot_id,
                            team_id, member_name, STATUS_RESERVED, now + LEASE_TTL_SEC, now, now,
                        ),
                    )
                conn.execute("COMMIT")
                return QuotaLease(lease_id=lid, kind=kind, idempotency_key=key, status=STATUS_RESERVED)
            except TeamQuotaExceeded:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def reserve_mysql(
        self,
        *,
        kind: str,
        key: str,
        identity: OwnerIdentity,
        team_id: str,
        member_name: str,
        limit: int,
        default: int,
        lock: str,
        exclude_member_names: tuple[str, ...] = (),
        org_limit: int = 0,
        org_default: int = 0,
        pending_non_leaders: int | None = None,
        on_existing: str = "ignore",
        record_entity: dict[str, Any] | None = None,
    ) -> QuotaLease | None:
        return self._reserve_mysql(
            kind=kind,
            key=key,
            identity=identity,
            team_id=team_id,
            member_name=member_name,
            limit=limit,
            default=default,
            lock=lock,
            exclude_member_names=exclude_member_names,
            org_limit=org_limit,
            org_default=org_default,
            pending_non_leaders=pending_non_leaders,
            on_existing=on_existing,
            record_entity=record_entity,
        )

    def _reserve_mysql(
        self,
        *,
        kind: str,
        key: str,
        identity: OwnerIdentity,
        team_id: str,
        member_name: str,
        limit: int,
        default: int,
        lock: str,
        exclude_member_names: tuple[str, ...] = (),
        org_limit: int = 0,
        org_default: int = 0,
        pending_non_leaders: int | None = None,
        on_existing: str = "ignore",
        record_entity: dict[str, Any] | None = None,
    ) -> QuotaLease | None:
        from sqlalchemy import text

        from jiuwenswarm.infrastructure.db.team_store_actor import (
            is_unique_violation,
            json_value_expr,
            run_tx,
        )
        from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_DELETED

        actor = self._mysql()
        now = _now()
        session_id = _reserve_session_id(kind, key)
        # Session files stay on this thread. The SQL that used to be several
        # actor.call calls runs inside _tx, before any FOR UPDATE.
        user_sessions: list[tuple[str, str]] = []
        org_sessions: list[tuple[str, str]] = []
        if lock == "account":
            user_sessions, org_sessions = self._local_session_pairs(identity, session_id)

        async def _op() -> QuotaLease | None:
            async def _tx(conn: Any) -> QuotaLease | None:
                async def _with_entity(lease: QuotaLease) -> QuotaLease:
                    if not record_entity:
                        return lease
                    await conn.execute(
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
                        record_entity,
                    )
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:st, team_id=:tid, "
                            "expires_at=NULL, updated_at=:now "
                            "WHERE lease_id=:lid AND status<>:rel"
                        ),
                        {
                            "st": STATUS_COMMITTED,
                            "tid": record_entity["team_id"],
                            "now": now,
                            "lid": lease.lease_id,
                            "rel": STATUS_RELEASED,
                        },
                    )
                    return lease

                await conn.execute(
                    text(
                        "UPDATE team_quota_lease SET status=:rel, updated_at=:now "
                        "WHERE status=:res AND expires_at IS NOT NULL AND expires_at<=:now"
                    ),
                    {"rel": STATUS_RELEASED, "now": now, "res": STATUS_RESERVED},
                )
                await conn.execute(text(_ABANDONED_HOLD_SQL), _abandoned_hold_params(now))
                if on_existing != "ignore" and session_id:
                    entity_row = (
                        await conn.execute(
                            text(
                                "SELECT status FROM team_entity "
                                "WHERE team_session_id=:sid ORDER BY updated_at DESC LIMIT 1"
                            ),
                            {"sid": session_id},
                        )
                    ).mappings().first()
                    if entity_row is not None and str(entity_row["status"]) != TEAM_STATUS_DELETED:
                        if on_existing == "reject":
                            raise SessionAlreadyHasTeam(session_id)
                        return None
                policy_result = await conn.execute(text("SELECT * FROM team_quota_policy"))
                policy_rows = [dict(row) for row in policy_result.mappings().all()]
                limits_now = self._limits_for_rows(policy_rows, identity)
                if pending_non_leaders is not None:
                    whole = int(limits_now.max_teammates_per_team)
                    if int(pending_non_leaders) > teammate_spawn_slots(whole):
                        raise TeamQuotaExceeded(
                            f"team quota exceeded: {int(pending_non_leaders) + 1}/{whole} "
                            "members including leader",
                            kind=KIND_TEAMMATE_SPAWN,
                            used=int(pending_non_leaders) + 1,
                            limit=whole,
                            default=int(limits_now.default_teammates),
                        )
                if lock == "account":
                    user_beyond, org_beyond = await self._beyond_on_connection(
                        conn,
                        identity,
                        session_id,
                        user_sessions,
                        org_sessions,
                    )
                    limit = int(limits_now.max_teams_per_user)
                    default = int(limits_now.default_teams)
                    org_limit = int(limits_now.max_teams_per_org)
                    org_default = int(limits_now.default_org_teams)
                else:
                    user_beyond = 0
                    org_beyond = 0
                    whole = int(limits_now.max_teammates_per_team)
                    limit = teammate_spawn_slots(whole)
                    default = teammate_spawn_slots(int(limits_now.default_teammates))
                    org_limit = 0
                    org_default = 0
                existing = (
                    await conn.execute(
                        text(
                            "SELECT * FROM team_quota_lease WHERE kind=:kind AND idempotency_key=:key"
                        ),
                        {"kind": kind, "key": key},
                    )
                ).mappings().first()
                if existing is not None:
                    if _lease_still_held(existing["status"], existing["expires_at"], now):
                        return await _with_entity(QuotaLease(
                            lease_id=str(existing["lease_id"]),
                            kind=kind,
                            idempotency_key=key,
                            status=str(existing["status"]),
                        ))
                if lock == "account":
                    gid = identity.group_id
                    if gid and int(org_limit) > 0:
                        await conn.execute(
                            text(
                                _insert_ignore_sql(
                                    "INSERT IGNORE INTO team_quota_org_account "
                                    "(group_id, created_at, updated_at) "
                                    "VALUES (:gid, :now, :now)",
                                    "INSERT INTO team_quota_org_account "
                                    "(group_id, created_at, updated_at) "
                                    "VALUES (:gid, :now, :now) "
                                    "ON CONFLICT (group_id) DO NOTHING",
                                )
                            ),
                            {"gid": gid, "now": now},
                        )
                        await conn.execute(
                            text(
                                "SELECT group_id FROM team_quota_org_account "
                                "WHERE group_id=:gid FOR UPDATE"
                            ),
                            {"gid": gid},
                        )
                        org_row = (
                            await conn.execute(
                                text(
                                    "SELECT COUNT(*) AS n FROM team_quota_lease "
                                    "WHERE kind=:kind AND group_id=:gid "
                                    f"AND {_OCCUPY_SQL}"
                                ),
                                {
                                    "kind": KIND_TEAM_CREATE,
                                    "gid": gid,
                                    "committed": STATUS_COMMITTED,
                                    "reserved": STATUS_RESERVED,
                                    "now": now,
                                },
                            )
                        ).mappings().first()
                        org_used = int((org_row or {}).get("n") or 0) + org_beyond
                        if org_used >= int(org_limit):
                            raise TeamQuotaExceeded(
                                f"team quota exceeded: {org_used}/{org_limit}",
                                kind=KIND_ORG_TEAM_CREATE,
                                used=org_used,
                                limit=int(org_limit),
                                default=int(org_default),
                            )
                    await conn.execute(
                        text(
                            _insert_ignore_sql(
                                "INSERT IGNORE INTO team_quota_account "
                                "(user_id, group_id, bot_id, created_at, updated_at) "
                                "VALUES (:uid, :gid, :bid, :now, :now)",
                                "INSERT INTO team_quota_account "
                                "(user_id, group_id, bot_id, created_at, updated_at) "
                                "VALUES (:uid, :gid, :bid, :now, :now) "
                                "ON CONFLICT (user_id, group_id, bot_id) DO NOTHING",
                            )
                        ),
                        {
                            "uid": identity.user_id,
                            "gid": identity.group_id,
                            "bid": identity.bot_id,
                            "now": now,
                        },
                    )
                    await conn.execute(
                        text(
                            "SELECT id FROM team_quota_account "
                            "WHERE user_id=:uid AND group_id=:gid AND bot_id=:bid FOR UPDATE"
                        ),
                        {
                            "uid": identity.user_id,
                            "gid": identity.group_id,
                            "bid": identity.bot_id,
                        },
                    )
                    used_row = (
                        await conn.execute(
                            text(
                                "SELECT COUNT(*) AS n FROM team_quota_lease "
                                "WHERE kind=:kind AND user_id=:uid AND group_id=:gid AND bot_id=:bid "
                                f"AND {_OCCUPY_SQL}"
                            ),
                            {
                                "kind": KIND_TEAM_CREATE,
                                "uid": identity.user_id,
                                "gid": identity.group_id,
                                "bid": identity.bot_id,
                                "committed": STATUS_COMMITTED,
                                "reserved": STATUS_RESERVED,
                                "now": now,
                            },
                        )
                    ).mappings().first()
                else:
                    await conn.execute(
                        text("SELECT team_id FROM team_entity WHERE team_id=:tid FOR UPDATE"),
                        {"tid": team_id},
                    )
                    excluded = tuple(n for n in exclude_member_names if _clean(n))
                    exclude_sql = ""
                    exclude_params: dict[str, Any] = {}
                    if excluded:
                        keys = []
                        for index, name in enumerate(excluded):
                            key_name = f"ex{index}"
                            keys.append(f":{key_name}")
                            exclude_params[key_name] = name
                        exclude_sql = f" AND member_name NOT IN ({', '.join(keys)})"
                    used_row = (
                        await conn.execute(
                            text(
                                "SELECT COUNT(*) AS n FROM team_quota_lease "
                                f"WHERE kind=:kind AND team_id=:tid AND {_OCCUPY_SQL}"
                                f"{exclude_sql}"
                            ),
                            {
                                "kind": KIND_TEAMMATE_SPAWN,
                                "tid": team_id,
                                "committed": STATUS_COMMITTED,
                                "reserved": STATUS_RESERVED,
                                "now": now,
                                **exclude_params,
                            },
                        )
                    ).mappings().first()
                used = int((used_row or {}).get("n") or 0)
                if lock == "account":
                    used += user_beyond
                if used >= int(limit):
                    if lock == "team":
                        shown_limit = int(limits_now.max_teammates_per_team)
                        raise TeamQuotaExceeded(
                            f"team quota exceeded: {used + 1}/{shown_limit} members including leader",
                            kind=kind,
                            used=used + 1,
                            limit=shown_limit,
                            default=int(limits_now.default_teammates),
                        )
                    raise TeamQuotaExceeded(
                        f"team quota exceeded: {used}/{limit}",
                        kind=kind,
                        used=used,
                        limit=int(limit),
                        default=int(default),
                    )
                lid = uuid.uuid4().hex
                params = {
                    "lid": lid,
                    "kind": kind,
                    "key": key,
                    "uid": identity.user_id,
                    "gid": identity.group_id,
                    "bid": identity.bot_id,
                    "tid": team_id,
                    "mn": member_name,
                    "st": STATUS_RESERVED,
                    "exp": now + LEASE_TTL_SEC,
                    "now": now,
                }
                if existing is not None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET lease_id=:lid, user_id=:uid, group_id=:gid, "
                            "bot_id=:bid, team_id=:tid, member_name=:mn, status=:st, expires_at=:exp, "
                            "updated_at=:now WHERE kind=:kind AND idempotency_key=:key"
                        ),
                        params,
                    )
                else:
                    try:
                        await conn.execute(
                            text(
                                "INSERT INTO team_quota_lease ("
                                "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
                                "team_id, member_name, status, expires_at, created_at, updated_at"
                                ") VALUES ("
                                ":lid, :kind, :key, :uid, :gid, :bid, :tid, :mn, :st, :exp, :now, :now)"
                            ),
                            params,
                        )
                    except Exception as exc:
                        if is_unique_violation(exc):
                            row = (
                                await conn.execute(
                                    text(
                                        "SELECT * FROM team_quota_lease WHERE kind=:kind AND idempotency_key=:key"
                                    ),
                                    {"kind": kind, "key": key},
                                )
                            ).mappings().first()
                            if row:
                                return await _with_entity(QuotaLease(
                                    lease_id=str(row["lease_id"]),
                                    kind=kind,
                                    idempotency_key=key,
                                    status=str(row["status"]),
                                ))
                        raise
                return await _with_entity(
                    QuotaLease(lease_id=lid, kind=kind, idempotency_key=key, status=STATUS_RESERVED)
                )

            return await run_tx(actor.handler, _tx)

        return actor.call(_op)

    def _user_team_rows(self, identity: OwnerIdentity) -> list[dict[str, Any]]:
        occupying = self._list_occupying(KIND_TEAM_CREATE)
        return [row for row in occupying if _same_owner(row, identity)]

    def _org_team_rows(self, group_id: str) -> list[dict[str, Any]]:
        gid = _clean(group_id)
        if not gid:
            return []
        return [
            row
            for row in self._list_occupying(KIND_TEAM_CREATE)
            if _clean(row.get("group_id")) == gid
        ]

    def _count_user_teams(
        self,
        identity: OwnerIdentity,
        *,
        skip_session_id: str = "",
    ) -> tuple[int, list[str]]:
        """Teams of this user: leases, plus live teams that never took a lease.

        ``skip_session_id`` is the session being reserved. It is not already
        used, so a team recording its own occupancy is not counted twice.
        """
        rows = self._user_team_rows(identity)
        ids = [_clean(row.get("team_id")) for row in rows if _clean(row.get("team_id"))]
        leased_sessions = _leased_session_ids(rows)
        if skip_session_id:
            leased_sessions.add(skip_session_id)
        extras = _labels_beyond_leases(
            entities=_live_control_teams(identity=identity),
            sessions=_unleased_user_session_teams(identity, leased_sessions),
            leased_team_ids=set(ids),
            leased_session_ids=leased_sessions,
        )
        ids.extend(extras)
        return len(rows) + len(extras), ids

    def _count_org_teams(self, identity: OwnerIdentity, *, skip_session_id: str = "") -> int:
        gid = _clean(identity.group_id)
        if not gid:
            return 0
        rows = self._org_team_rows(gid)
        leased_ids = {_clean(row.get("team_id")) for row in rows if _clean(row.get("team_id"))}
        leased_sessions = _leased_session_ids(rows)
        if skip_session_id:
            leased_sessions.add(skip_session_id)
        extras = _labels_beyond_leases(
            entities=_live_control_teams(group_id=gid),
            sessions=_unleased_org_session_teams(identity, leased_sessions),
            leased_team_ids=leased_ids,
            leased_session_ids=leased_sessions,
        )
        return len(rows) + len(extras)

    @staticmethod
    def _local_session_pairs(
        identity: OwnerIdentity,
        session_id: str,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Team sessions on disk that have no lease yet.

        Runs on the caller thread. The session being reserved is excluded.
        ``_labels_beyond_leases`` drops sessions that a lease already covers.
        """
        claimed = {session_id} if session_id else set()
        user_sessions = _unleased_user_session_teams(identity, set(claimed))
        org_sessions = (
            _unleased_org_session_teams(identity, set(claimed))
            if identity.group_id
            else []
        )
        return user_sessions, org_sessions

    async def _select_occupying(
        self,
        conn: Any,
        kind: str,
        *,
        identity: OwnerIdentity | None = None,
        group_id: str = "",
    ) -> list[dict[str, Any]]:
        from sqlalchemy import text

        sql = (
            "SELECT * FROM team_quota_lease WHERE kind=:kind AND "
            + _OCCUPY_SQL
        )
        params: dict[str, Any] = {
            "kind": kind,
            "committed": STATUS_COMMITTED,
            "reserved": STATUS_RESERVED,
            "now": _now(),
        }
        if identity is not None:
            sql += " AND user_id=:uid AND group_id=:gid AND bot_id=:bid"
            params.update({
                "uid": identity.user_id,
                "gid": identity.group_id,
                "bid": identity.bot_id,
            })
        elif group_id:
            sql += " AND group_id=:gid"
            params["gid"] = group_id
        result = await conn.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]

    async def _select_live_entities(
        self,
        conn: Any,
        *,
        identity: OwnerIdentity | None = None,
        group_id: str = "",
    ) -> list[tuple[str, str, str]]:
        from sqlalchemy import text

        from jiuwenswarm.enterprise.edition import is_enterprise

        gid = _clean(group_id)
        if not is_enterprise():
            return []
        if identity is not None and not identity.user_id:
            return []
        if identity is None and not gid:
            return []
        clauses = ["status IN (" + ", ".join(f":st{i}" for i, _ in enumerate(sorted(TEAM_ACTIVE_STATUSES))) + ")"]
        params: dict[str, Any] = {
            f"st{i}": status for i, status in enumerate(sorted(TEAM_ACTIVE_STATUSES))
        }
        if identity is not None:
            clauses.append(
                "owner_user_id=:uid AND owner_group_id=:gid AND owner_bot_id=:bid"
            )
            params.update({
                "uid": identity.user_id,
                "gid": identity.group_id,
                "bid": identity.bot_id,
            })
        elif gid:
            clauses.append("owner_group_id=:gid")
            params["gid"] = gid
        result = await conn.execute(
            text(
                "SELECT team_id, team_session_id, team_name FROM team_entity WHERE "
                + " AND ".join(clauses)
            ),
            params,
        )
        found: list[tuple[str, str, str]] = []
        for row in result.mappings().all():
            team_id = _clean(row.get("team_id"))
            sid = _clean(row.get("team_session_id"))
            label = _clean(row.get("team_name")) or team_id or sid
            if label:
                found.append((team_id, sid, label))
        return found

    async def _beyond_on_connection(
        self,
        conn: Any,
        identity: OwnerIdentity,
        session_id: str,
        user_sessions: list[tuple[str, str]],
        org_sessions: list[tuple[str, str]],
    ) -> tuple[int, int]:
        """Lease and entity rows on ``conn``. Each list is read once."""
        user_rows = await self._select_occupying(conn, KIND_TEAM_CREATE, identity=identity)
        user_ids = {_clean(row.get("team_id")) for row in user_rows if _clean(row.get("team_id"))}
        user_leased = _leased_session_ids(user_rows)
        if session_id:
            user_leased.add(session_id)
        user_beyond = len(_labels_beyond_leases(
            entities=await self._select_live_entities(conn, identity=identity),
            sessions=user_sessions,
            leased_team_ids=user_ids,
            leased_session_ids=user_leased,
        ))
        org_beyond = 0
        if identity.group_id:
            org_rows = await self._select_occupying(
                conn, KIND_TEAM_CREATE, group_id=identity.group_id,
            )
            org_ids = {_clean(row.get("team_id")) for row in org_rows if _clean(row.get("team_id"))}
            org_leased = _leased_session_ids(org_rows)
            if session_id:
                org_leased.add(session_id)
            org_beyond = len(_labels_beyond_leases(
                entities=await self._select_live_entities(conn, group_id=identity.group_id),
                sessions=org_sessions,
                leased_team_ids=org_ids,
                leased_session_ids=org_leased,
            ))
        return user_beyond, org_beyond

    def _snapshot_user_mysql(self, identity: OwnerIdentity) -> dict[str, Any]:
        """One actor call: policies, leases, and live entities on the same connection."""
        from sqlalchemy import text

        from jiuwenswarm.infrastructure.db.team_store_actor import run_tx

        user_sessions, org_sessions = self._local_session_pairs(identity, "")
        actor = self._mysql()

        async def _op() -> dict[str, Any]:
            async def _tx(conn: Any) -> dict[str, Any]:
                policy_result = await conn.execute(text("SELECT * FROM team_quota_policy"))
                policy_rows = [dict(row) for row in policy_result.mappings().all()]
                limits = self._limits_for_rows(policy_rows, identity)
                rows = await self._select_occupying(conn, KIND_TEAM_CREATE, identity=identity)
                ids = [_clean(row.get("team_id")) for row in rows if _clean(row.get("team_id"))]
                leased_sessions = _leased_session_ids(rows)
                extras = _labels_beyond_leases(
                    entities=await self._select_live_entities(conn, identity=identity),
                    sessions=user_sessions,
                    leased_team_ids=set(ids),
                    leased_session_ids=leased_sessions,
                )
                ids.extend(extras)
                payload: dict[str, Any] = {
                    "teams": {
                        "used": len(rows) + len(extras),
                        "limit": limits.max_teams_per_user,
                        "default": limits.default_teams,
                        "occupying_ids": ids,
                    },
                    "teammates_default_limit": limits.default_teammates,
                }
                if identity.group_id and limits.max_teams_per_org > 0:
                    org_rows = await self._select_occupying(
                        conn, KIND_TEAM_CREATE, group_id=identity.group_id,
                    )
                    org_ids = {
                        _clean(row.get("team_id"))
                        for row in org_rows
                        if _clean(row.get("team_id"))
                    }
                    org_extras = _labels_beyond_leases(
                        entities=await self._select_live_entities(
                            conn, group_id=identity.group_id,
                        ),
                        sessions=org_sessions,
                        leased_team_ids=org_ids,
                        leased_session_ids=_leased_session_ids(org_rows),
                    )
                    payload["org"] = {
                        "group_id": identity.group_id,
                        "used": len(org_rows) + len(org_extras),
                        "limit": limits.max_teams_per_org,
                    }
                return payload

            return await run_tx(actor.handler, _tx)

        return actor.call(_op)

    def _teams_beyond_leases(self, identity: OwnerIdentity, session_id: str) -> tuple[int, int]:
        """User and org teams that occupy a slot without a lease row.

        The session being reserved is excluded. Its lease is the row this
        call is about to insert.
        """
        user_total, _ids = self._count_user_teams(identity, skip_session_id=session_id)
        user_beyond = user_total - len(self._user_team_rows(identity))
        org_beyond = 0
        if identity.group_id:
            org_total = self._count_org_teams(identity, skip_session_id=session_id)
            org_beyond = org_total - len(self._org_team_rows(identity.group_id))
        return max(user_beyond, 0), max(org_beyond, 0)

    def list_occupying_team_create_sessions(
        self, identity: OwnerIdentity | None = None,
    ) -> list[tuple[str, OwnerIdentity]]:
        """Occupying team_create leases as ``(session_id, owner)``.

        ``identity`` limits the result to that owner. ``None`` returns every
        occupying team_create session.
        """
        rows = self._list_occupying(KIND_TEAM_CREATE)
        if identity is not None:
            rows = [row for row in rows if _same_owner(row, identity)]
        found: dict[str, OwnerIdentity] = {}
        prefix = "session:"
        for row in rows:
            key = _clean(row.get("idempotency_key"))
            if not key.startswith(prefix):
                continue
            session_id = key[len(prefix):].strip()
            if not session_id:
                continue
            found[session_id] = OwnerIdentity(
                user_id=_clean(row.get("user_id")),
                group_id=_clean(row.get("group_id")),
                bot_id=_clean(row.get("bot_id")),
            )
        return sorted(found.items())

_SERVICE: TeamQuotaService | None = None
_SERVICE_LOCK = threading.Lock()


def get_team_quota_service() -> TeamQuotaService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = TeamQuotaService()
    return _SERVICE


def set_team_quota_service(service: TeamQuotaService | None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


__all__ = [
    "DEFAULT_MAX_TEAMS_PER_USER",
    "DEFAULT_MAX_TEAMMATES_PER_TEAM",
    "KIND_ORG_TEAM_CREATE",
    "KIND_REQUESTED_TEAM_SIZE",
    "KIND_TEAM_CREATE",
    "KIND_TEAMMATE_SPAWN",
    "QUOTA_EXCEEDED",
    "QuotaLease",
    "QuotaLimits",
    "TeamQuotaExceeded",
    "TeamQuotaService",
    "count_predefined_non_leaders",
    "is_leader_member_name",
    "leader_spawn_names",
    "org_team_create_quota_user_message",
    "team_create_quota_user_message",
    "team_quota_user_message",
    "teammate_spawn_slots",
    "get_team_quota_service",
    "set_team_quota_service",
    "team_quota_enabled",
    "team_create_key",
]
