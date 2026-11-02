# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-team member count guard (``max_teammates``) for the enterprise edition.

``max_teammates`` / Y is the whole-team size including the leader.
Admission uses Team quota leases (MySQL/SQLite). Roster count is the fallback.
"""

from __future__ import annotations

import logging
import re
import types
from typing import Any

logger = logging.getLogger(__name__)

TEAM_MEMBER_QUOTA_EXCEEDED = "TEAM_MEMBER_QUOTA_EXCEEDED"
DEFAULT_MAX_TEAMMATES = 5


def teammate_quota_user_message(limit: int) -> str:
    """User-facing spawn failure. ``limit`` is whole-team size including the leader."""
    return (
        f"人数已满：该团队最多{max(int(limit), 0)}人（含 Leader），无法再添加队员"
    )


def teammate_quota_fail_reason(limit: int) -> str:
    return f"{TEAM_MEMBER_QUOTA_EXCEEDED}: {teammate_quota_user_message(limit)}"


def requested_team_size_user_message(requested: int, limit: int) -> str:
    """User-facing refusal when the original sentence already exceeds the cap."""
    return (
        f"无法创建：你要求的是 {int(requested)} 人团队，"
        f"该团队最多 {max(int(limit), 0)} 人（含 Leader）。"
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
_INCLUDING_LEADER = (
    re.compile(_COUNT_EDGE + _COUNT + r"\s*人团队"),
    re.compile(_COUNT_EDGE + _COUNT + r"\s*个人的团队"),
    re.compile(_COUNT_EDGE + _COUNT + r"\s*个成员"),
    re.compile(r"包含\s*" + _COUNT + r"\s*个成员"),
    re.compile(r"\bteam of\s+([0-9]{1,2})\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,2})\s*[- ]person team\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,2})\s+members\b", re.IGNORECASE),
)
_EXCLUDING_LEADER = (
    re.compile(_COUNT_EDGE + _COUNT + r"\s*个队员"),
    re.compile(_COUNT_EDGE + _COUNT + r"\s*名队员"),
    re.compile(_COUNT_EDGE + _COUNT + r"\s*个专家"),
)
# Enterprise-only readings. Personal keeps the patterns above.
_EXCLUDING_LEADER_ENTERPRISE = (
    re.compile(r"再加\s*" + _COUNT + r"\s*人"),
    re.compile(r"除我以外\s*" + _COUNT + r"\s*人"),
    re.compile(r"除我之外\s*" + _COUNT + r"\s*人"),
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
    return (
        f"【团队人数约束】本次含组长共 {size} 人，再招募 {recruit} 名队员。"
        "侧边成员列表不含组长，不要因为列表里没有组长而再多招募一人。"
    )


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
    """``(limit, default)`` using the same per-user cap as teammate spawn."""
    from jiuwenswarm.enterprise.team.handlers import request_identity
    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

    identity = request_identity(request)
    if str(getattr(identity, "user_id", "") or "").strip():
        limits = get_team_quota_service().resolve_limits(identity)
        return int(limits.max_teammates_per_team), int(limits.default_teammates)
    cap = int(resolve_max_teammates(config_base if isinstance(config_base, dict) else None, None))
    return cap, cap


_GUARD_ATTR = "_jiuwen_teammate_limit_attached"
_ORIGINAL_ATTR = "_jiuwen_original_spawn_member"
_SHUTDOWN_GUARD_ATTR = "_jiuwen_teammate_quota_shutdown_attached"
_ORIGINAL_SHUTDOWN_ATTR = "_jiuwen_original_shutdown_member"


def resolve_max_teammates(config_base: dict[str, Any] | None, template_id: str | None = None) -> int:
    """Read ``modes.team.<template>.max_teammates`` (fallback: any team / default)."""
    cfg = config_base if isinstance(config_base, dict) else {}
    modes = cfg.get("modes") if isinstance(cfg.get("modes"), dict) else {}
    teams = modes.get("team") if isinstance(modes.get("team"), dict) else {}
    candidates: list[dict[str, Any]] = []
    if template_id and isinstance(teams.get(template_id), dict):
        candidates.append(teams[template_id])
    candidates.extend(v for v in teams.values() if isinstance(v, dict))
    legacy = cfg.get("team")
    if isinstance(legacy, dict):
        candidates.append(legacy)
    for item in candidates:
        raw = item.get("max_teammates")
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_MAX_TEAMMATES


async def count_non_leader_members(backend: Any) -> int:
    """Count members on the roster excluding the leader row."""
    leader_name = str(getattr(backend, "leader_member_name", "") or "")
    rows = await backend.db.member.get_member_roster(backend.team_name)
    return sum(1 for name, _display, _status in rows if name != leader_name)


async def count_team_members(backend: Any) -> int:
    """Count the whole roster including the leader."""
    rows = await backend.db.member.get_member_roster(backend.team_name)
    return len(rows)


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
    tid = str(team_id or "").strip()
    roster_limit = int(max_teammates or 0)
    if not tid and roster_limit <= 0:
        return False
    backend = getattr(team_agent, "team_backend", None)
    if backend is None:
        logger.debug("[MemberLimits] no team_backend on leader; skip guard")
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
        # (openjiuwen default is TEAMMATE). That must not consume a Y slot.
        if role_value == TeamRole.LEADER.value or (
            leader_name and str(member_name or "").strip() == leader_name
        ):
            return await original(member_name, *args, **kwargs)
        if tid:
            from jiuwenswarm.enterprise.team.quota import (
                TeamQuotaExceeded,
                get_team_quota_service,
            )

            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

            svc = get_team_quota_service()
            try:
                lease = await run_team_store(
                    svc.reserve_teammate_spawn,
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
                    from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

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

                    from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

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
    "count_non_leader_members",
    "count_team_members",
    "detach_teammate_limit_guard",
    "resolve_max_teammates",
]
