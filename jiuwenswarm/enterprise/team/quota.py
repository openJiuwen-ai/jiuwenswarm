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
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway.config.enterprise.expressions import matches, validate_match_expr
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.enterprise.team.control_store import TEAM_ACTIVE_STATUSES, OwnerIdentity

logger = logging.getLogger(__name__)

KIND_TEAM_CREATE = "team_create"
KIND_ORG_TEAM_CREATE = "org_team_create"
KIND_TEAMMATE_SPAWN = "teammate_spawn"
KIND_REQUESTED_TEAM_SIZE = "requested_team_size"

STATUS_RESERVED = "reserved"
STATUS_COMMITTED = "committed"
STATUS_RELEASED = "released"

DEFAULT_MAX_TEAMS_PER_USER = 5
DEFAULT_MAX_TEAMMATES_PER_TEAM = 5
LEASE_TTL_SEC = 60.0
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
class QuotaLimits:
    max_teams_per_user: int
    max_teammates_per_team: int
    default_teams: int = DEFAULT_MAX_TEAMS_PER_USER
    default_teammates: int = DEFAULT_MAX_TEAMMATES_PER_TEAM
    policy_id: str = ""
    # 0 means the organization has no team cap.
    max_teams_per_org: int = 0
    default_org_teams: int = 0


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


def _now() -> float:
    return time.time()


def _clean(value: Any) -> str:
    return str(value or "").strip()


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


def _unleased_org_session_teams(group_id: str, leased_session_ids: set[str]) -> list[tuple[str, str]]:
    from jiuwenswarm.enterprise.edition import is_enterprise

    gid = _clean(group_id)
    if not is_enterprise() or not gid:
        return []
    try:
        root = get_user_workspace_dir()
        children = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        logger.debug("[TeamQuota] org session team scan skipped", exc_info=True)
        return []
    prefix = f"workspace_{gid}::"
    found: list[tuple[str, str]] = []
    claimed = set(leased_session_ids)
    for child in children:
        if not child.is_dir() or not child.name.startswith(prefix):
            continue
        for session_id, team_name in iter_unleased_team_sessions(child / "agent" / "sessions", claimed):
            claimed.add(session_id)
            found.append((session_id, team_name))
    return found


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


DEFAULT_LEADER_MEMBER_NAME = "team-leader"


def leader_spawn_names(leader_member_name: str = "") -> tuple[str, ...]:
    """Member names that must not occupy a Y (teammate_spawn) slot."""
    names = {_clean(leader_member_name), DEFAULT_LEADER_MEMBER_NAME}
    return tuple(n for n in names if n)


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


def _parse_match_expr(raw: Any) -> Any:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, dict)):
        return raw
    text = str(raw).strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _updated_at_rank(raw: Any) -> str:
    """Normalize ``updated_at`` so a later save sorts after an earlier one."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d %H:%M:%S.%f")
    return str(raw).strip().replace("T", " ").replace("Z", "")


def _policy_rank(row: dict[str, Any]) -> tuple[int, str]:
    """Higher priority wins. The same priority keeps the later ``updated_at``."""
    return (int(row.get("priority") or 0), _updated_at_rank(row.get("updated_at")))


def _is_unconditional(expr: Any) -> bool:
    canon = _parse_match_expr(expr)
    if canon in (None, [], ""):
        return True
    if isinstance(canon, str):
        return not any(marker in canon for marker in ("==", "!=", " in "))
    return False


def _select_limits(policies: list[dict[str, Any]], identity: OwnerIdentity) -> QuotaLimits:
    ident = {
        "user_id": identity.user_id,
        "group_id": identity.group_id,
        "bot_id": identity.bot_id,
    }
    hits: list[dict[str, Any]] = []
    for row in policies:
        if not bool(row.get("enabled", True)):
            continue
        expr = _parse_match_expr(row.get("match_expr"))
        try:
            if matches(expr, ident):
                hits.append(row)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamQuota] skip invalid match_expr policy=%s", row.get("policy_id"))
    default_teams = DEFAULT_MAX_TEAMS_PER_USER
    default_teammates = DEFAULT_MAX_TEAMMATES_PER_TEAM
    for row in policies:
        if bool(row.get("enabled", True)) and _is_unconditional(row.get("match_expr")):
            default_teams = max(1, int(row.get("max_teams_per_user") or default_teams))
            default_teammates = max(1, int(row.get("max_teammates_per_team") or default_teammates))
            break
    if not hits:
        return QuotaLimits(
            max_teams_per_user=default_teams,
            max_teammates_per_team=default_teammates,
            default_teams=default_teams,
            default_teammates=default_teammates,
        )
    hits.sort(key=_policy_rank, reverse=True)
    top = hits[0]
    return QuotaLimits(
        max_teams_per_user=max(1, int(top.get("max_teams_per_user") or default_teams)),
        max_teammates_per_team=max(1, int(top.get("max_teammates_per_team") or default_teammates)),
        default_teams=default_teams,
        default_teammates=default_teammates,
        policy_id=_clean(top.get("policy_id")),
    )


def _expr_blob(expr: Any) -> str:
    canon = _parse_match_expr(expr)
    if canon in (None, [], ""):
        return ""
    if isinstance(canon, list):
        return "\n".join(str(item) for item in canon)
    return str(canon)


def _is_org_policy(expr: Any) -> bool:
    """Z ignores policies that name a user or a bot."""
    blob = _expr_blob(expr)
    return "user_id" not in blob and "bot_id" not in blob


def _org_limit_value(row: dict[str, Any]) -> int:
    raw = row.get("max_teams_per_org")
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    return min(value, 1000)


def _select_org_limit(policies: list[dict[str, Any]], group_id: str) -> tuple[int, int]:
    """Return ``(limit, default)``. 0 means unlimited.

    Only unconditional policies and policies that do not mention ``user_id`` or
    ``bot_id`` participate. Highest priority among those that match this org wins.
    """
    default_org = 0
    for row in policies:
        if bool(row.get("enabled", True)) and _is_unconditional(row.get("match_expr")) and _is_org_policy(row.get("match_expr")):
            default_org = _org_limit_value(row)
            break
    ident = {"user_id": "", "group_id": group_id, "bot_id": ""}
    hits: list[dict[str, Any]] = []
    for row in policies:
        if not bool(row.get("enabled", True)) or not _is_org_policy(row.get("match_expr")):
            continue
        expr = _parse_match_expr(row.get("match_expr"))
        try:
            if matches(expr, ident):
                hits.append(row)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamQuota] skip invalid org match_expr policy=%s", row.get("policy_id"))
    if not hits:
        return default_org, default_org
    hits.sort(key=_policy_rank, reverse=True)
    return _org_limit_value(hits[0]), default_org


def _coerce_org_limit(body: dict[str, Any]) -> int:
    if "max_teams_per_org" not in body or body.get("max_teams_per_org") is None:
        return 0
    value = int(body.get("max_teams_per_org"))
    if value < 0 or value > 1000:
        raise ValueError("max_teams_per_org must be between 0 and 1000")
    return value


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_quota_policy (
    policy_id TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL DEFAULT '',
    policy_desc TEXT,
    match_expr TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    max_teams_per_user INTEGER NOT NULL,
    max_teammates_per_team INTEGER NOT NULL,
    max_teams_per_org INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'manual',
    data TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS team_quota_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (user_id, group_id, bot_id)
);
CREATE TABLE IF NOT EXISTS team_quota_lease (
    lease_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    member_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_team_quota_lease_owner
    ON team_quota_lease (kind, user_id, group_id, bot_id, status);
CREATE INDEX IF NOT EXISTS idx_team_quota_lease_team
    ON team_quota_lease (kind, team_id, status);
CREATE INDEX IF NOT EXISTS idx_team_quota_lease_org
    ON team_quota_lease (kind, group_id, status);
CREATE TABLE IF NOT EXISTS team_quota_org_account (
    group_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_entity (
    team_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL DEFAULT '',
    owner_group_id TEXT NOT NULL DEFAULT '',
    owner_bot_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL
);
"""

_OCCUPY_SQL = (
    "(status = :committed OR (status = :reserved AND (expires_at IS NULL OR expires_at > :now)))"
)

_SQLITE_OCCUPY = (
    "(status=? OR (status=? AND (expires_at IS NULL OR expires_at>?)))"
)


def _ensure_sqlite_org_column(conn: sqlite3.Connection) -> None:
    """Existing files keep the old policy table. CREATE IF NOT EXISTS will not add a column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(team_quota_policy)")}
    if "max_teams_per_org" not in cols:
        conn.execute(
            "ALTER TABLE team_quota_policy ADD COLUMN max_teams_per_org INTEGER NOT NULL DEFAULT 0"
        )


class TeamQuotaService:
    """Process-wide quota operations. Backed by Gateway MySQL or local SQLite."""

    def __init__(self, sqlite_path: Path | None = None) -> None:
        self._sqlite_path = sqlite_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def _use_mysql(self) -> bool:
        if self._sqlite_path is not None:
            return False
        from jiuwenswarm.infrastructure.db.team_mysql_actor import use_team_mysql

        return use_team_mysql()

    def _sqlite(self) -> sqlite3.Connection:
        if self._conn is None:
            path = self._sqlite_path
            if path is None:
                path = get_user_workspace_dir() / "agent" / "teams" / "team_control.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SQLITE_SCHEMA)
            _ensure_sqlite_org_column(conn)
            self._conn = conn
        return self._conn

    def _mysql(self):
        from jiuwenswarm.infrastructure.db.team_mysql_actor import get_team_mysql_actor

        return get_team_mysql_actor()

    def resolve_limits(self, identity: OwnerIdentity) -> QuotaLimits:
        return self._limits_for_rows(self.list_policies(), identity)

    def _limits_for_rows(self, rows: list[dict[str, Any]], identity: OwnerIdentity) -> QuotaLimits:
        limits = _select_limits(rows, identity)
        if not rows:
            y = DEFAULT_MAX_TEAMMATES_PER_TEAM
            try:
                from jiuwenswarm.enterprise.team.member_limits import resolve_max_teammates
                from jiuwenswarm.common.config import get_config

                y = max(1, int(resolve_max_teammates(get_config(), None)))
            except Exception:  # noqa: BLE001
                pass
            limits = QuotaLimits(
                max_teams_per_user=DEFAULT_MAX_TEAMS_PER_USER,
                max_teammates_per_team=y,
                default_teams=DEFAULT_MAX_TEAMS_PER_USER,
                default_teammates=DEFAULT_MAX_TEAMMATES_PER_TEAM,
            )
        org_limit, org_default = _select_org_limit(rows, identity.group_id)
        limits.max_teams_per_org = org_limit
        limits.default_org_teams = org_default
        return limits

    def list_policies(self) -> list[dict[str, Any]]:
        if self._use_mysql():
            from jiuwenswarm.infrastructure.db.team_mysql_actor import list_record_dicts

            actor = self._mysql()
            return actor.call(
                lambda: list_record_dicts(actor.handler, "team_quota_policy")
            )
        with self._lock:
            rows = self._sqlite().execute("SELECT * FROM team_quota_policy").fetchall()
        return [dict(r) for r in rows]

    def upsert_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        pid = _clean(body.get("policy_id")) or uuid.uuid4().hex
        name = _clean(body.get("policy_name")) or "team-quota"
        expr = body.get("match_expr")
        if expr is None:
            expr = []
        validate_match_expr(expr)
        x = max(1, min(1000, int(body.get("max_teams_per_user") or DEFAULT_MAX_TEAMS_PER_USER)))
        y = max(1, min(1000, int(body.get("max_teammates_per_team") or DEFAULT_MAX_TEAMMATES_PER_TEAM)))
        z = _coerce_org_limit(body)
        enabled = bool(body.get("enabled", True))
        priority = int(body.get("priority") or 0)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # MySQL DATETIME rejects ISO-8601 with T/Z under strict mode.
        now_mysql = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        row = {
            "policy_id": pid,
            "policy_name": name,
            "policy_desc": _clean(body.get("policy_desc")) or None,
            "match_expr": expr,
            "priority": priority,
            "max_teams_per_user": x,
            "max_teammates_per_team": y,
            "max_teams_per_org": z,
            "enabled": enabled,
            "source": _clean(body.get("source")) or "manual",
            "data": body.get("data"),
            "created_at": now_iso,
            "created_by": _clean(body.get("created_by")) or None,
            "updated_at": now_iso,
            "updated_by": _clean(body.get("updated_by")) or None,
        }
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                payload = dict(row)
                payload["match_expr"] = json.dumps(expr, ensure_ascii=False)
                payload["data"] = json.dumps(payload["data"], ensure_ascii=False) if payload["data"] is not None else None
                payload["enabled"] = 1 if enabled else 0
                payload["created_at"] = now_mysql
                payload["updated_at"] = now_mysql

                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "INSERT INTO team_quota_policy ("
                            "policy_id, policy_name, policy_desc, match_expr, priority, "
                            "max_teams_per_user, max_teammates_per_team, max_teams_per_org, enabled, source, data, "
                            "created_at, created_by, updated_at, updated_by"
                            ") VALUES ("
                            ":policy_id, :policy_name, :policy_desc, :match_expr, :priority, "
                            ":max_teams_per_user, :max_teammates_per_team, :max_teams_per_org, :enabled, :source, :data, "
                            ":created_at, :created_by, :updated_at, :updated_by"
                            ") ON DUPLICATE KEY UPDATE "
                            "policy_name=VALUES(policy_name), policy_desc=VALUES(policy_desc), "
                            "match_expr=VALUES(match_expr), priority=VALUES(priority), "
                            "max_teams_per_user=VALUES(max_teams_per_user), "
                            "max_teammates_per_team=VALUES(max_teammates_per_team), "
                            "max_teams_per_org=VALUES(max_teams_per_org), "
                            "enabled=VALUES(enabled), source=VALUES(source), data=VALUES(data), "
                            "updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)"
                        ),
                        payload,
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return row
        with self._lock:
            conn = self._sqlite()
            conn.execute(
                "INSERT INTO team_quota_policy ("
                "policy_id, policy_name, policy_desc, match_expr, priority, "
                "max_teams_per_user, max_teammates_per_team, max_teams_per_org, enabled, source, data, "
                "created_at, created_by, updated_at, updated_by"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(policy_id) DO UPDATE SET "
                "policy_name=excluded.policy_name, policy_desc=excluded.policy_desc, "
                "match_expr=excluded.match_expr, priority=excluded.priority, "
                "max_teams_per_user=excluded.max_teams_per_user, "
                "max_teammates_per_team=excluded.max_teammates_per_team, "
                "max_teams_per_org=excluded.max_teams_per_org, "
                "enabled=excluded.enabled, source=excluded.source, data=excluded.data, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (
                    pid, name, row["policy_desc"], json.dumps(expr, ensure_ascii=False),
                    priority, x, y, z, 1 if enabled else 0, row["source"],
                    json.dumps(row["data"], ensure_ascii=False) if row["data"] is not None else None,
                    now_iso, row["created_by"], now_iso, row["updated_by"],
                ),
            )
        return row

    def delete_policy(self, policy_id: str) -> None:
        pid = _clean(policy_id)
        if not pid:
            return
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text("DELETE FROM team_quota_policy WHERE policy_id = :pid"),
                        {"pid": pid},
                    )

                await run_tx(actor.handler, _tx)

            actor.call(_op)
            return
        with self._lock:
            self._sqlite().execute("DELETE FROM team_quota_policy WHERE policy_id = ?", (pid,))

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
            return QuotaLease(lease_id="", kind=KIND_TEAMMATE_SPAWN, idempotency_key="", status=STATUS_RESERVED, noop=True)
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

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

            actor = self._mysql()

            async def _op() -> None:
                async def _tx(conn: Any) -> None:
                    await conn.execute(
                        text(
                            "UPDATE team_quota_lease SET status=:st, team_id=CASE WHEN :tid='' THEN team_id ELSE :tid END, "
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
                (STATUS_COMMITTED, _clean(team_id), _clean(team_id), _clean(member_name), _clean(member_name), now, lid, STATUS_RELEASED),
            )

    def release_reserved_create(self, session_id: str) -> None:
        """Release a chat reservation only while it is still reserved. One MySQL call."""
        sid = _clean(session_id)
        if not sid or not self._use_mysql():
            return
        from sqlalchemy import text

        from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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
            from jiuwenswarm.infrastructure.db.team_mysql_actor import record_to_dict

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

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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
                "used": self._count_org_teams(identity.group_id),
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

    def list_usage(self) -> dict[str, Any]:
        rows = self._list_occupying(KIND_TEAM_CREATE)
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (_clean(row.get("user_id")), _clean(row.get("group_id")), _clean(row.get("bot_id")))
            grouped.setdefault(key, []).append(row)
        items = []
        for (uid, gid, bid), leases in grouped.items():
            ident = OwnerIdentity(user_id=uid, group_id=gid, bot_id=bid)
            limits = self.resolve_limits(ident)
            team_ids = [_clean(r.get("team_id")) for r in leases if _clean(r.get("team_id"))]
            teams = []
            for tid in team_ids:
                teams.append(
                    {
                        "team_id": tid,
                        "used": self._count_team_members_including_leader(tid),
                        "limit": limits.max_teammates_per_team,
                    }
                )
            items.append(
                {
                    "user_id": uid,
                    "group_id": gid,
                    "bot_id": bid,
                    "used": len(leases),
                    "limit": limits.max_teams_per_user,
                    "default": limits.default_teams,
                    "teams": teams,
                }
            )
        by_org: dict[str, int] = {}
        for row in rows:
            gid = _clean(row.get("group_id"))
            if gid:
                by_org[gid] = by_org.get(gid, 0) + 1
        orgs = []
        for gid, used_n in sorted(by_org.items()):
            limits = self.resolve_limits(OwnerIdentity(user_id="", group_id=gid, bot_id=""))
            orgs.append({
                "group_id": gid,
                "used": used_n,
                "limit": limits.max_teams_per_org,
            })
        return {"items": items, "count": len(items), "orgs": orgs}

    def reconcile_mysql(self) -> None:
        """Pass A: SQL-only, safe on any AgentServer."""
        now = _now()
        if self._use_mysql():
            from sqlalchemy import text

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

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
                                "INSERT IGNORE INTO team_quota_lease ("
                                "lease_id, kind, idempotency_key, user_id, group_id, bot_id, "
                                "team_id, member_name, status, expires_at, created_at, updated_at"
                                ") VALUES ("
                                ":lid, :kind, :key, :uid, :gid, :bid, :tid, '', :st, NULL, :now, :now)"
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

    def _owner_of_team(self, team_id: str) -> OwnerIdentity | None:
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
                    if row["status"] == STATUS_COMMITTED or (
                        row["status"] == STATUS_RESERVED
                        and (row["expires_at"] is None or float(row["expires_at"]) > now)
                    ):
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

        from jiuwenswarm.infrastructure.db.team_mysql_actor import is_unique_violation, run_tx
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
                            ":agent_service_id, :gateway_id, :created_at, :updated_at, CAST(:extra AS json)"
                            ")"
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
                policy_rows = [
                    dict(row)
                    for row in (
                        await conn.execute(text("SELECT * FROM team_quota_policy"))
                    ).mappings().all()
                ]
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
                    if existing["status"] == STATUS_COMMITTED or (
                        existing["status"] == STATUS_RESERVED
                        and (existing["expires_at"] is None or float(existing["expires_at"]) > now)
                    ):
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
                                "INSERT IGNORE INTO team_quota_org_account "
                                "(group_id, created_at, updated_at) "
                                "VALUES (:gid, :now, :now)"
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
                            "INSERT IGNORE INTO team_quota_account "
                            "(user_id, group_id, bot_id, created_at, updated_at) "
                            "VALUES (:uid, :gid, :bid, :now, :now)"
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
        return [
            row
            for row in self._list_occupying(KIND_TEAM_CREATE)
            if _clean(row.get("user_id")) == identity.user_id
            and _clean(row.get("group_id")) == identity.group_id
            and _clean(row.get("bot_id")) == identity.bot_id
        ]

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

    def _count_org_teams(self, group_id: str, *, skip_session_id: str = "") -> int:
        gid = _clean(group_id)
        if not gid:
            return 0
        rows = self._org_team_rows(gid)
        leased_ids = {_clean(row.get("team_id")) for row in rows if _clean(row.get("team_id"))}
        leased_sessions = _leased_session_ids(rows)
        if skip_session_id:
            leased_sessions.add(skip_session_id)
        extras = _labels_beyond_leases(
            entities=_live_control_teams(group_id=gid),
            sessions=_unleased_org_session_teams(gid, leased_sessions),
            leased_team_ids=leased_ids,
            leased_session_ids=leased_sessions,
        )
        return len(rows) + len(extras)

    def _local_session_pairs(
        self,
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
            _unleased_org_session_teams(identity.group_id, set(claimed))
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

        from jiuwenswarm.infrastructure.db.team_mysql_actor import run_tx

        user_sessions, org_sessions = self._local_session_pairs(identity, "")
        actor = self._mysql()

        async def _op() -> dict[str, Any]:
            async def _tx(conn: Any) -> dict[str, Any]:
                policy_rows = [
                    dict(row)
                    for row in (
                        await conn.execute(text("SELECT * FROM team_quota_policy"))
                    ).mappings().all()
                ]
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
            org_total = self._count_org_teams(identity.group_id, skip_session_id=session_id)
            org_beyond = org_total - len(self._org_team_rows(identity.group_id))
        return max(user_beyond, 0), max(org_beyond, 0)

    def _count_team_teammates(self, team_id: str) -> int:
        skip = set(leader_spawn_names())
        return len(
            [
                row
                for row in self._list_occupying(KIND_TEAMMATE_SPAWN, team_id=team_id)
                if _clean(row.get("member_name")) not in skip
            ]
        )

    def _count_team_members_including_leader(self, team_id: str) -> int:
        tid = _clean(team_id)
        used_teammates = self._count_team_teammates(tid)
        if not tid:
            return used_teammates
        team_alive = bool(self._list_occupying(KIND_TEAM_CREATE, team_id=tid))
        return used_teammates + (1 if team_alive else 0)

    def list_occupying_team_create_sessions(
        self, identity: OwnerIdentity | None = None,
    ) -> list[tuple[str, OwnerIdentity]]:
        """Occupying team_create leases as ``(session_id, owner)``.

        ``identity`` limits the result to that owner. ``None`` returns every
        occupying team_create session.
        """
        rows = self._list_occupying(KIND_TEAM_CREATE)
        if identity is not None:
            rows = [
                row
                for row in rows
                if _clean(row.get("user_id")) == identity.user_id
                and _clean(row.get("group_id")) == identity.group_id
                and _clean(row.get("bot_id")) == identity.bot_id
            ]
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

    def _list_occupying(self, kind: str, team_id: str = "") -> list[dict[str, Any]]:
        now = _now()
        if self._use_mysql():
            from jiuwenswarm.infrastructure.db.team_mysql_actor import list_record_dicts

            actor = self._mysql()
            filters: dict[str, Any] = {
                "kind": kind,
                "status": [STATUS_COMMITTED, STATUS_RESERVED],
            }
            if team_id:
                filters["team_id"] = team_id
            rows = actor.call(
                lambda: list_record_dicts(
                    actor.handler,
                    "team_quota_lease",
                    filters=filters,
                )
            )
            return [
                row
                for row in rows
                if row.get("status") == STATUS_COMMITTED
                or row.get("expires_at") is None
                or float(row["expires_at"]) > now
            ]
        with self._lock:
            conn = self._sqlite()
            if team_id:
                rows = conn.execute(
                    "SELECT * FROM team_quota_lease WHERE kind=? AND team_id=? AND "
                    "(status=? OR (status=? AND (expires_at IS NULL OR expires_at>?)))",
                    (kind, team_id, STATUS_COMMITTED, STATUS_RESERVED, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM team_quota_lease WHERE kind=? AND "
                    "(status=? OR (status=? AND (expires_at IS NULL OR expires_at>?)))",
                    (kind, STATUS_COMMITTED, STATUS_RESERVED, now),
                ).fetchall()
        return [dict(r) for r in rows]


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
