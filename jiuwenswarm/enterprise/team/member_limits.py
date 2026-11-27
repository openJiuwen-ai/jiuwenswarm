# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-team member count guard (``max_teammates``) for the enterprise edition.

``max_teammates`` / Y is the whole-team size including the leader.
Admission uses Team quota leases (MySQL/SQLite). Roster count is the fallback.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import uuid
import types
from typing import Any

from agent_gateway.enterprise.team.quota_policy import DEFAULT_MAX_TEAMMATES, resolve_max_teammates

logger = logging.getLogger(__name__)

TEAM_MEMBER_QUOTA_EXCEEDED = "TEAM_MEMBER_QUOTA_EXCEEDED"
TEAM_MEMBER_QUOTA_UNAVAILABLE = "TEAM_MEMBER_QUOTA_UNAVAILABLE"


def _t(message_id: str, *, language: str | None = None, **params: Any) -> str:
    from jiuwenswarm.common.i18n import t
    from jiuwenswarm.enterprise.config.preferred_language import (
        resolve_preferred_language,
    )

    return t(
        message_id,
        language=language if language is not None else resolve_preferred_language(),
        **params,
    )


def teammate_quota_user_message(limit: int, *, language: str | None = None) -> str:
    """User-facing spawn failure. ``limit`` is whole-team size including the leader."""
    return _t(
        "enterprise.team.teammate_full",
        language=language,
        limit=max(int(limit), 0),
    )


def teammate_quota_fail_reason(limit: int, *, language: str | None = None) -> str:
    return (
        f"{TEAM_MEMBER_QUOTA_EXCEEDED}: "
        f"{teammate_quota_user_message(limit, language=language)}"
    )


def teammate_quota_unavailable_reason(*, language: str | None = None) -> str:
    """Failure when admission cannot be evaluated (deadlock/timeout/outage).

    A transient DB failure is not evidence that the team is full; claiming
    "人数已满" would be false. Tell the user to retry instead.
    """
    return (
        f"{TEAM_MEMBER_QUOTA_UNAVAILABLE}: "
        f"{_t('enterprise.team.teammate_quota_unavailable', language=language)}"
    )


def requested_team_size_user_message(
    requested: int, limit: int, *, language: str | None = None
) -> str:
    """User-facing refusal when the original sentence already exceeds the cap."""
    return _t(
        "enterprise.team.requested_size_over",
        language=language,
        requested=int(requested),
        limit=max(int(limit), 0),
    )


_CN_UNITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_COUNT = r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})"
_COUNT_EDGE = r"(?<![0-9零一二两三四五六七八九十A-Za-z])"
_AMBIGUOUS_SIZE = re.compile(
    r"(?:至少|不少于|至多|不多于|最多|超过|多于)"
    r"|(?:\bat least\b|\bno more than\b|\bmore than\b|\bup to\b)"
    r"|(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?:到|至|-|~|—)\s*"
    r"(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*人"
    r"|(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*人以上"
    r"|\b[0-9]{1,2}\s*(?:to|-|~)\s*[0-9]{1,2}\b",
    re.IGNORECASE,
)


def _pattern(*parts: str) -> str:
    return "".join(parts)


_INCLUDING_LEADER = (
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*人(?:的)?[^，。；;、\s]{0,12}团队")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*个人(?:的)?[^，。；;、\s]{0,12}团队")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*人团队")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*个人的团队")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*个成员")),
    re.compile(_pattern(r"包含\s*", _COUNT, r"\s*个成员")),
    re.compile(r"\bteam of\s+([0-9]{1,2})\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,2})\s*[- ]person team\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,2})\s+members\b", re.IGNORECASE),
)
_EXCLUDING_LEADER = (
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*个队员")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*名队员")),
    re.compile(_pattern(_COUNT_EDGE, _COUNT, r"\s*个专家")),
)
# Enterprise-only readings. Personal keeps the patterns above.
_EXCLUDING_LEADER_ENTERPRISE = (
    re.compile(_pattern(r"再加\s*", _COUNT, r"\s*人")),
    re.compile(_pattern(r"除我以外\s*", _COUNT, r"\s*人")),
    re.compile(_pattern(r"除我之外\s*", _COUNT, r"\s*人")),
)


def _parse_count_token(token: str) -> int | None:
    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 99 else None
    if any(ch not in _CN_UNITS and ch != "十" for ch in text):
        return None
    if "十" not in text:
        if len(text) != 1:
            return None
        value = _CN_UNITS.get(text, 0)
        return value if value > 0 else None
    left, _, right = text.partition("十")
    if len(left) > 1 or len(right) > 1:
        return None
    if left and left not in _CN_UNITS:
        return None
    if right and right not in _CN_UNITS:
        return None
    tens = _CN_UNITS[left] if left else 1
    if left and tens == 0:
        return None
    ones = _CN_UNITS[right] if right else 0
    value = tens * 10 + ones
    return value if 1 <= value <= 99 else None


def _collect_sizes(text: str, patterns: tuple[re.Pattern[str], ...], *, plus_leader: bool) -> set[int]:
    found: set[int] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            count = _parse_count_token(match.group(1))
            if count is None:
                continue
            found.add(count + 1 if plus_leader else count)
    return found


def explicit_whole_team_size(query: str, *, enterprise: bool = False) -> int | None:
    """One unambiguous whole-team size from the user's own words, or None.

    「N 人团队 / N 个成员」is N including the leader. 「N 个队员 / N 个专家」is
    N teammates, so the whole team is N+1. Enterprise also reads 「再加 N 人」
    and 「除我以外 N 人」as N teammates. A range, a qualifier, or two different
    readings returns None and leaves the existing spawn guard in place.
    """
    text = str(query or "").strip()
    if not text or _AMBIGUOUS_SIZE.search(text):
        return None
    sizes = _collect_sizes(text, _INCLUDING_LEADER, plus_leader=False)
    sizes.update(_collect_sizes(text, _EXCLUDING_LEADER, plus_leader=True))
    if enterprise:
        sizes.update(_collect_sizes(text, _EXCLUDING_LEADER_ENTERPRISE, plus_leader=True))
    if len(sizes) != 1:
        return None
    return next(iter(sizes))


def enterprise_recruit_constraint(query: str) -> str:
    """Sentence appended to an enterprise build so the sidebar cannot add one more.

    Including-leader phrases recruit ``N - 1``. Excluding-leader phrases recruit
    ``N``. The sidebar list omits the leader and is not a headcount.
    """
    size = explicit_whole_team_size(query, enterprise=True)
    if size is None:
        return ""
    recruit = max(size - 1, 0)
    return _t("enterprise.team.recruit_constraint", size=size, recruit=recruit)


def requested_team_size_over_limit(
    query: str,
    limit: int,
    *,
    enterprise: bool = False,
) -> int | None:
    """Whole-team size from the original sentence when it is strictly over ``limit``."""
    size = explicit_whole_team_size(query, enterprise=enterprise)
    if size is None:
        return None
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        return None
    if size <= cap:
        return None
    return size


def whole_team_member_limit(request: Any, config_base: dict[str, Any] | None = None) -> tuple[int, int]:
    """``(limit, default)`` using the same per-user cap as teammate spawn.

    When the caller identity cannot be resolved, enterprise must not let a
    template's legacy ``max_teammates`` widen the platform default; the quota
    policy is the only source of truth there.
    """
    from jiuwenswarm.enterprise.edition import is_enterprise
    from jiuwenswarm.enterprise.team.handlers import request_identity
    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

    identity = request_identity(request)
    if str(getattr(identity, "user_id", "") or "").strip():
        limits = get_team_quota_service().resolve_limits(identity)
        limit = int(limits.max_teammates_per_team)
        if is_enterprise() and limit <= 0:
            # A malformed policy row must not disable admission.
            limit = DEFAULT_MAX_TEAMMATES
        return limit, int(limits.default_teammates)
    if is_enterprise():
        return DEFAULT_MAX_TEAMMATES, DEFAULT_MAX_TEAMMATES
    cap = int(resolve_max_teammates(config_base if isinstance(config_base, dict) else None, None))
    return cap, cap


def predefined_team_size_user_message(
    team_size: int, limit: int, *, language: str | None = None
) -> str:
    """User-facing refusal when a team template/package exceeds the cap.

    Unlike ``requested_team_size_user_message`` the size comes from the
    materialized roster (an AgentGroup or a Team skill), not from the user's
    sentence, so the wording must not claim the user asked for it.
    """
    return _t(
        "enterprise.team.predefined_size_over",
        language=language,
        team_size=int(team_size),
        limit=max(int(limit), 0),
    )


def count_predefined_quota_members(members: Any) -> int:
    """Count predefined members that occupy a teammate quota slot.

    The Leader is registered separately by ``build_team`` and must not be
    counted twice. Dicts from raw team configs and spec objects from an
    AgentGroup assembly are both accepted.
    """
    total = 0
    for member in members or []:
        if isinstance(member, dict):
            role = member.get("role_type") or member.get("role")
            name = member.get("member_name")
            if name is None:
                name = member.get("name")
        else:
            role = getattr(member, "role_type", None)
            name = getattr(member, "member_name", None)
        if name is not None and not str(name).strip():
            continue
        role_value = getattr(role, "value", role)
        if str(role_value or "").strip().lower() == "leader":
            continue
        total += 1
    return total


def session_owner_identity(session_id: str) -> Any | None:
    """Resolve the owning user from session metadata for background team builds.

    Expert/Skill teams materialize outside a chat round, so there is no
    request contextvar to read. The session metadata written when the user
    started the Team keeps the owner identity.
    """
    from jiuwenswarm.enterprise.team.control_store import OwnerIdentity

    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(sid, cache_bust=True) or {}
    except Exception:  # noqa: BLE001
        logger.debug("[MemberLimits] session metadata read failed: %s", sid, exc_info=True)
        return None
    channel_metadata = metadata.get("channel_metadata") or {}
    routing = (
        channel_metadata.get("routing") if isinstance(channel_metadata, dict) else {}
    ) or {}
    candidate = OwnerIdentity(
        user_id=str(metadata.get("user_id") or "").strip(),
        group_id=str(routing.get("group_id") or "").strip(),
        bot_id=str(routing.get("bot_id") or "").strip(),
    )
    return candidate if candidate.user_id else None


async def resolve_session_member_limit(session_id: str) -> tuple[int, Any | None]:
    """``(whole-team member cap, owner)`` for a team materialized off the chat path.

    Falls back to the platform default cap when the owner cannot be resolved
    in enterprise so the guard never silently disappears; personal edition
    keeps the config-derived cap through ``whole_team_member_limit`` semantics.
    """
    from jiuwenswarm.enterprise.edition import is_enterprise
    from jiuwenswarm.enterprise.team.quota import (
        get_team_quota_service,
        team_quota_enabled,
    )

    if not is_enterprise() or not team_quota_enabled():
        return 0, None
    owner = session_owner_identity(session_id)
    if owner is None:
        logger.warning(
            "[MemberLimits] owner unresolved for team build; using default cap: session=%s",
            session_id,
        )
        return DEFAULT_MAX_TEAMMATES, None
    try:
        from jiuwenswarm.infrastructure.db.team_store_actor import run_team_store

        limits = await run_team_store(get_team_quota_service().resolve_limits, owner)
        limit = int(limits.max_teammates_per_team)
        if limit <= 0:
            # Enterprise quota always has a platform default; a zero/malformed
            # policy must not silently turn the guard off.
            limit = DEFAULT_MAX_TEAMMATES
        return limit, owner
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MemberLimits] member limit resolve failed; using default cap: session=%s",
            session_id,
            exc_info=True,
        )
        return DEFAULT_MAX_TEAMMATES, owner


_GUARD_ATTR = "_jiuwen_teammate_limit_attached"
_ORIGINAL_ATTR = "_jiuwen_original_spawn_member"
_SHUTDOWN_GUARD_ATTR = "_jiuwen_teammate_quota_shutdown_attached"
_ORIGINAL_SHUTDOWN_ATTR = "_jiuwen_original_shutdown_member"


async def quota_member_names(backend: Any) -> list[str]:
    """Return roster identities that occupy ordinary team runtime slots."""
    members = await backend.db.member.get_team_members(backend.team_name)
    return [member.member_name for member in members if member.role != "host_managed"]


_SWARMFLOW_QUOTA_LOCK = threading.RLock()
_SWARMFLOW_QUOTA: dict[str, dict[str, Any]] = {}
_SWARMFLOW_GUARD_ATTR = "_jiuwen_swarmflow_quota_guard"


def register_swarmflow_worker_quota(
    *,
    session_id: str,
    team_name: str,
    max_teammates: int,
    on_quota_exceeded: Any = None,
) -> None:
    """Bind the team's whole-team cap to swarmflow worker admission.

    Swarmflow workers are ephemeral executors created by ``TeamWorkerBackend``;
    they never call ``TeamBackend.spawn_member``, so the normal teammate guard
    cannot see them. Registering the cap here lets the backend guard reject
    workers beyond ``max_teammates - 1`` (the leader is not a worker).
    """
    from jiuwenswarm.enterprise.team.quota import teammate_spawn_slots

    worker_limit = teammate_spawn_slots(max_teammates)
    entry = {
        "worker_limit": max(int(worker_limit), 0),
        "team_limit": max(int(max_teammates), 0),
        "on_quota_exceeded": on_quota_exceeded,
    }
    keys = [str(k or "").strip() for k in (session_id, team_name)]
    with _SWARMFLOW_QUOTA_LOCK:
        for key in keys:
            if key:
                _SWARMFLOW_QUOTA[key] = entry


def clear_swarmflow_worker_quota(*, session_id: str = "", team_name: str = "") -> None:
    keys = [str(k or "").strip() for k in (session_id, team_name)]
    with _SWARMFLOW_QUOTA_LOCK:
        for key in keys:
            if key:
                _SWARMFLOW_QUOTA.pop(key, None)


def _swarmflow_quota_for(session_id: str, team_name: str) -> dict[str, Any] | None:
    with _SWARMFLOW_QUOTA_LOCK:
        for key in (str(session_id or "").strip(), str(team_name or "").strip()):
            if key and key in _SWARMFLOW_QUOTA:
                return _SWARMFLOW_QUOTA[key]
    return None


def claim_swarmflow_worker_slot(backend: Any, call_key: str = "") -> tuple[bool, int, int]:
    """Claim one worker slot for a swarmflow run.

    Returns ``(allowed, used, team_limit)``. A repeated ``call_key`` (engine
    retry of the same call) is idempotent and does not consume another slot.
    """
    from jiuwenswarm.enterprise.edition import is_enterprise
    from jiuwenswarm.enterprise.team.quota import team_quota_enabled

    if not is_enterprise() or not team_quota_enabled():
        return True, 0, 0
    entry = _swarmflow_quota_for(
        str(getattr(backend, "_session_id", "") or ""),
        str(getattr(backend, "_team_name", "") or ""),
    )
    if entry is None:
        return True, 0, 0
    seen = getattr(backend, "_jiuwen_swarmflow_quota_seen", None)
    if seen is None:
        seen = set()
        setattr(backend, "_jiuwen_swarmflow_quota_seen", seen)
    key = str(call_key or "").strip()
    if key and key in seen:
        return True, len(seen), int(entry["team_limit"])
    limit = int(entry["worker_limit"])
    if len(seen) >= limit:
        callback = entry.get("on_quota_exceeded")
        if callable(callback):
            try:
                callback(
                    used=len(seen) + 1,
                    limit=int(entry["team_limit"]),
                    member_name=key,
                )
            except Exception:  # noqa: BLE001
                logger.debug("[MemberLimits] swarmflow quota notice failed", exc_info=True)
        return False, len(seen) + 1, int(entry["team_limit"])
    seen.add(key or f"worker-{len(seen)}")
    return True, len(seen), int(entry["team_limit"])


def install_swarmflow_worker_quota_guard() -> bool:
    """Wrap ``TeamWorkerBackend.run`` once so swarmflow workers hit the quota.

    The current openjiuwen backend has no public worker-admission hook, so the
    shared ``run`` choke point is wrapped once per process. Revisit this shim
    if agent-core exposes a first-class quota callback.
    """
    try:
        from openjiuwen.agent_teams.workflow.backends.team_worker_backend import (
            TeamWorkerBackend,
        )
        from openjiuwen.agent_teams.workflow.engine.errors import BackendError
    except Exception:  # noqa: BLE001 - older openjiuwen has no swarmflow backend
        return False
    original = getattr(TeamWorkerBackend, "run", None)
    if not callable(original):
        return False
    if getattr(original, _SWARMFLOW_GUARD_ATTR, False):
        return True

    async def _guarded_run(
        self: Any,
        prompt: str,
        opts: dict,
        schema_json: Any,
        *,
        call_key: str | None = None,
    ):
        # Each invocation owns one active slot, including concurrent retries.
        slot_key = uuid.uuid4().hex
        allowed, _used, team_limit = claim_swarmflow_worker_slot(self, slot_key)
        if not allowed:
            raise BackendError(teammate_quota_fail_reason(team_limit), retryable=False)
        try:
            return await original(self, prompt, opts, schema_json, call_key=call_key)
        finally:
            seen = getattr(self, "_jiuwen_swarmflow_quota_seen", None)
            if seen is not None:
                seen.discard(slot_key)

    setattr(_guarded_run, _SWARMFLOW_GUARD_ATTR, True)
    TeamWorkerBackend.run = _guarded_run
    logger.info("[MemberLimits] swarmflow worker quota guard installed")
    return True


async def count_non_leader_members(backend: Any) -> int:
    """Count runtime slots excluding the leader and host-managed identities."""
    leader_name = str(getattr(backend, "leader_member_name", "") or "")
    names = await quota_member_names(backend)
    return sum(1 for name in names if name != leader_name)


async def count_team_members(backend: Any) -> int:
    """Count runtime slots including the leader, excluding host-managed identities."""
    return len(await quota_member_names(backend))


_RETRYABLE_DB_CODES = frozenset({1205, 1213})
_RESERVE_RETRY_ATTEMPTS = 3
_RESERVE_RETRY_BASE_DELAY = 0.05
_SPAWN_LOCKS: dict[str, asyncio.Lock] = {}


def _db_error_codes(exc: BaseException) -> set[int]:
    """Integer error codes carried by an exception and its causes.

    SQLAlchemy wraps the driver error, so 1213/1205 may live on ``orig``,
    ``__cause__`` or ``__context__`` rather than on ``exc.args``.
    """
    codes: set[int] = set()
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        args = getattr(current, "args", ())
        if args and isinstance(args[0], int):
            codes.add(args[0])
        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                stack.append(nested)
    return codes


def _is_retryable_db_error(exc: BaseException) -> bool:
    """InnoDB deadlock (1213) / lock-wait timeout (1205) are safe to retry."""
    return bool(_db_error_codes(exc) & _RETRYABLE_DB_CODES)


def _team_spawn_lock(team_id: str) -> asyncio.Lock:
    """Per-team admission lock for this process.

    It only removes same-process contention on the lease row; the shared DB
    lease remains the cross-process source of truth.
    """
    lock = _SPAWN_LOCKS.get(team_id)
    if lock is None:
        lock = asyncio.Lock()
        _SPAWN_LOCKS[team_id] = lock
    return lock


def _teammate_quota_fails_closed(svc: Any) -> bool:
    """Whether an unevaluable admission must be refused instead of guessed.

    Personal edition and the SQLite single-process store keep the historical
    roster fallback. Enterprise shared-store quota fails closed so a deadlock
    cannot turn into an over-quota team.
    """
    try:
        from jiuwenswarm.enterprise.edition import is_enterprise
        from jiuwenswarm.enterprise.team.quota import team_quota_enabled

        if not is_enterprise() or not team_quota_enabled():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        use_shared = getattr(svc, "use_shared_store", None)
        return bool(use_shared()) if callable(use_shared) else True
    except Exception:  # noqa: BLE001
        # Enterprise quota is enabled but the store could not be classified;
        # refusing is safer than guessing a member count.
        return True


async def _reserve_teammate_spawn_with_retry(svc: Any, **kwargs: Any) -> Any:
    """Reserve a spawn slot, retrying transient InnoDB lock conflicts."""
    from jiuwenswarm.infrastructure.db.team_store_actor import run_team_store

    for attempt in range(_RESERVE_RETRY_ATTEMPTS):
        try:
            return await run_team_store(svc.reserve_teammate_spawn, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if (
                not _is_retryable_db_error(exc)
                or attempt + 1 >= _RESERVE_RETRY_ATTEMPTS
            ):
                raise
            delay = _RESERVE_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(
                0, _RESERVE_RETRY_BASE_DELAY
            )
            logger.warning(
                "[MemberLimits] quota reserve lock conflict; retry %s/%s in %.3fs",
                attempt + 1,
                _RESERVE_RETRY_ATTEMPTS - 1,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("reserve retry loop exhausted without a result")


def attach_teammate_limit_guard(
    team_agent: Any,
    *,
    max_teammates: int = 0,
    team_id: str = "",
    owner: Any = None,
    on_quota_exceeded: Any = None,
) -> bool:
    """Wrap ``team_backend.spawn_member`` with quota admission.

    Prefer lease admission when ``team_id`` is set. ``max_teammates`` is the
    whole-team cap including the leader (roster fallback and tests).
    """
    from openjiuwen.agent_teams.schema.team import MemberOpResult, TeamRole

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return False
    backend = getattr(team_agent, "team_backend", None)
    if backend is None:
        logger.debug("[MemberLimits] no team_backend on leader; skip guard")
        return False
    if getattr(backend, "conversation_mode", None) == "group_chat":
        detach_teammate_limit_guard(team_agent)
        return False
    tid = str(team_id or "").strip()
    roster_limit = int(max_teammates or 0)
    if not tid and roster_limit <= 0:
        return False
    if getattr(backend, _GUARD_ATTR, False):
        return True

    original = getattr(backend, "spawn_member", None)
    if not callable(original):
        logger.warning("[MemberLimits] team_backend has no spawn_member; skip guard")
        return False

    async def _guarded_spawn_member(self: Any, member_name: str, *args: Any, **kwargs: Any):
        role = kwargs.get("role", TeamRole.TEAMMATE)
        role_value = getattr(role, "value", role)
        leader_name = str(getattr(self, "leader_member_name", "") or "").strip()
        # build_team registers the leader via spawn_member without role=LEADER
        # (openjiuwen default is TEAMMATE). Host-managed identities run outside
        # this team and must not reserve an ordinary runtime slot either.
        if role_value in {TeamRole.LEADER.value, "host_managed"} or (
            leader_name and str(member_name or "").strip() == leader_name
        ):
            return await original(member_name, *args, **kwargs)
        if tid:
            from jiuwenswarm.enterprise.team.quota import (
                TeamQuotaExceeded,
                get_team_quota_service,
            )

            from jiuwenswarm.infrastructure.db.team_store_actor import run_team_store

            svc = get_team_quota_service()
            # Same-process spawns for one team hit the lease transaction one at
            # a time; the shared lease still enforces the cap across processes.
            async with _team_spawn_lock(tid):
                try:
                    lease = await _reserve_teammate_spawn_with_retry(
                        svc,
                        team_id=tid,
                        member_name=str(member_name),
                        owner=owner,
                        leader_member_name=leader_name,
                    )
                except TeamQuotaExceeded as exc:
                    reason = teammate_quota_fail_reason(exc.limit)
                    logger.warning("[MemberLimits] %s", reason)
                    if callable(on_quota_exceeded):
                        try:
                            on_quota_exceeded(
                                used=exc.used,
                                limit=exc.limit,
                                member_name=str(member_name),
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug("[MemberLimits] quota notice callback failed", exc_info=True)
                    return MemberOpResult.fail(reason)
                except Exception:  # noqa: BLE001
                    if _teammate_quota_fails_closed(svc):
                        logger.warning(
                            "[MemberLimits] quota reserve unavailable; refusing spawn: "
                            "team=%s member=%s",
                            tid,
                            member_name,
                            exc_info=True,
                        )
                        return MemberOpResult.fail(teammate_quota_unavailable_reason())
                    logger.warning("[MemberLimits] quota reserve failed; falling back to roster count", exc_info=True)
                    lease = None
                else:
                    try:
                        result = await original(member_name, *args, **kwargs)
                    except Exception:
                        await run_team_store(svc.release_lease, lease)
                        raise
                    if not getattr(result, "ok", True):
                        await run_team_store(svc.release_lease, lease)
                        return result
                    await run_team_store(
                        svc.commit_lease,
                        lease,
                        team_id=tid,
                        member_name=str(member_name),
                    )
                    return result
        limit = roster_limit
        if limit <= 0:
            try:
                from jiuwenswarm.enterprise.team.quota import get_team_quota_service

                if owner is not None:
                    from jiuwenswarm.infrastructure.db.team_store_actor import run_team_store

                    limits = await run_team_store(get_team_quota_service().resolve_limits, owner)
                    limit = limits.max_teammates_per_team
            except Exception:  # noqa: BLE001
                limit = DEFAULT_MAX_TEAMMATES
        if limit <= 0:
            limit = DEFAULT_MAX_TEAMMATES
        try:
            current = await count_team_members(self)
        except Exception:  # noqa: BLE001
            logger.warning("[MemberLimits] roster count failed; allowing spawn", exc_info=True)
            current = -1
        if current >= limit:
            reason = teammate_quota_fail_reason(limit)
            logger.warning("[MemberLimits] %s", reason)
            if callable(on_quota_exceeded):
                try:
                    on_quota_exceeded(
                        used=current,
                        limit=limit,
                        member_name=str(member_name),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("[MemberLimits] quota notice callback failed", exc_info=True)
            return MemberOpResult.fail(reason)
        return await original(member_name, *args, **kwargs)

    setattr(backend, _ORIGINAL_ATTR, original)
    backend.spawn_member = types.MethodType(_guarded_spawn_member, backend)
    setattr(backend, _GUARD_ATTR, True)

    original_shutdown = getattr(backend, "shutdown_member", None)
    if callable(original_shutdown) and tid and not getattr(backend, _SHUTDOWN_GUARD_ATTR, False):
        async def _guarded_shutdown(self: Any, member_name: str, *args: Any, **kwargs: Any):
            result = await original_shutdown(member_name, *args, **kwargs)
            if getattr(result, "ok", True):
                try:
                    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

                    from jiuwenswarm.infrastructure.db.team_store_actor import run_team_store

                    await run_team_store(
                        get_team_quota_service().release_teammate,
                        tid,
                        str(member_name),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("[MemberLimits] teammate quota release failed", exc_info=True)
            return result

        setattr(backend, _ORIGINAL_SHUTDOWN_ATTR, original_shutdown)
        backend.shutdown_member = types.MethodType(_guarded_shutdown, backend)
        setattr(backend, _SHUTDOWN_GUARD_ATTR, True)

    logger.info(
        "[MemberLimits] teammate limit guard attached: team=%s team_id=%s roster_limit=%s",
        getattr(backend, "team_name", "?"), tid or "-", roster_limit,
    )
    return True


def detach_teammate_limit_guard(team_agent: Any) -> None:
    backend = getattr(team_agent, "team_backend", None)
    if backend is None:
        return
    if getattr(backend, _GUARD_ATTR, False):
        original = getattr(backend, _ORIGINAL_ATTR, None)
        if callable(original):
            backend.spawn_member = original
        for attr in (_GUARD_ATTR, _ORIGINAL_ATTR):
            try:
                delattr(backend, attr)
            except AttributeError:
                pass
    if getattr(backend, _SHUTDOWN_GUARD_ATTR, False):
        original = getattr(backend, _ORIGINAL_SHUTDOWN_ATTR, None)
        if callable(original):
            backend.shutdown_member = original
        for attr in (_SHUTDOWN_GUARD_ATTR, _ORIGINAL_SHUTDOWN_ATTR):
            try:
                delattr(backend, attr)
            except AttributeError:
                pass


__all__ = [
    "DEFAULT_MAX_TEAMMATES",
    "TEAM_MEMBER_QUOTA_EXCEEDED",
    "teammate_quota_fail_reason",
    "teammate_quota_user_message",
    "requested_team_size_user_message",
    "explicit_whole_team_size",
    "requested_team_size_over_limit",
    "whole_team_member_limit",
    "attach_teammate_limit_guard",
    "quota_member_names",
    "count_non_leader_members",
    "count_team_members",
    "detach_teammate_limit_guard",
    "resolve_max_teammates",
]
