# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm Skill session policy (v2): XOR with bound teams, open-library visibility.

Product rules (需求 v2 §0.1):
- Bound ``team_name`` + identifiable Swarm Skill → reject.
- Bound team + ordinary skills → allow; do **not** write team visibility.
- Unbound + at most one Swarm → auto-bind/generated; keep **empty allow** (full
  library); emphasize SOP via Leader hint — intentionally **not** develop's
  "selection writes allow=[skill]" narrowing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill.skill_type import (
    SKILL_TYPE_SWARM,
    is_swarm_kind,
    is_swarm_skill_type,
    read_frontmatter_kind,
)

logger = logging.getLogger(__name__)

SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND = "SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND"
SWARM_SKILL_MULTIPLE_NOT_ALLOWED = "SWARM_SKILL_MULTIPLE_NOT_ALLOWED"

# Deprecated M1 temporary-entry names; bind must not accept these as openers.
DEPRECATED_DYNAMIC_ENTRIES = frozenset({"dynamic_skill", "hybrid_seed", "dynamic_open"})

TEAM_ORIGIN_SESSION_GENERATED = "session_generated"

_SOP_HINT_PREFIX = "[Team collaboration SOP]"
_SOP_HINT_SUFFIX = "[/Team collaboration SOP]"


class SwarmSkillSessionError(Exception):
    """Reject chat/bind when Swarm Skill policy is violated."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        session_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = str(code or "BAD_REQUEST")
        self.session_id = str(session_id or "").strip()

    @property
    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": str(self),
            "code": self.code,
        }
        if self.session_id:
            body["session_id"] = self.session_id
        return body


def parse_request_skill_ids(params: dict[str, Any] | None) -> list[str]:
    """Normalize ``params.skills`` to a de-duplicated list of non-empty ids."""
    if not isinstance(params, dict) or "skills" not in params:
        return []
    raw = params.get("skills")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _is_unsafe_skill_id(name: str) -> bool:
    """Reject empty / traversal / path-separator skill ids."""
    if not name:
        return True
    if "/" in name or "\\" in name:
        return True
    return name in {".", ".."}


def resolve_skill_package(skill_id: str) -> Path | None:
    """Resolve ``skill_id`` under the primary team Skill library with path containment.

    Uses :func:`primary_team_skill_library_dir` (same pin as Hub / evolution /
    ``configure_global_skills_dir``) so Ace SHARED tips resolve Swarm packages
    from the writable user library rather than the agent workspace tree alone.
    """
    from jiuwenswarm.common.utils import primary_team_skill_library_dir

    name = str(skill_id or "").strip()
    if _is_unsafe_skill_id(name):
        return None
    try:
        skills_root = Path(primary_team_skill_library_dir()).expanduser().resolve()
    except OSError:
        return None
    if not skills_root.is_dir():
        return None
    try:
        skill_dir = (skills_root / name).resolve(strict=False)
        skill_dir.relative_to(skills_root)
    except (OSError, ValueError):
        return None
    if not skill_dir.is_dir():
        return None
    if not any((skill_dir / md).is_file() for md in ("SKILL.md", "skill.md", "Skill.md")):
        return None
    return skill_dir


def is_identifiable_swarm_skill(skill_id: str) -> bool:
    """True only when disk/frontmatter clearly marks swarm/team-skill.

    Unresolvable or kind-less packages are treated as ordinary skills so Office
    Ace ordinary picks are not false-rejected (需求 v2 §0.1).
    """
    package = resolve_skill_package(skill_id)
    if package is None:
        return False
    kind = read_frontmatter_kind(package)
    if is_swarm_kind(kind):
        return True
    # Reuse already-parsed kind so detect_skill_type does not re-read SKILL.md.
    from jiuwenswarm.server.runtime.skill.skill_type import detect_skill_type

    return detect_skill_type(package, kind=kind) == SKILL_TYPE_SWARM


def classify_skill_ids(skill_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split into (swarm_ids, ordinary_ids); order preserved."""
    swarm: list[str] = []
    ordinary: list[str] = []
    for skill_id in skill_ids:
        if is_identifiable_swarm_skill(skill_id):
            swarm.append(skill_id)
        else:
            ordinary.append(skill_id)
    return swarm, ordinary


def assert_chat_swarm_skill_policy(
    *,
    session_id: str,
    team_name: str,
    skill_ids: list[str],
    metadata: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Enforce Swarm XOR / single-swarm rules for ``chat.send``.

    Bound catalog/OA teams reject Swarm. Session-generated teams opened by a
    Swarm Skill may keep sending that same Swarm on later turns (idempotent
    continuation). Plain auto-bind without a Swarm still forbids later Swarm
    picks — dissolve first (产品边角).

    Returns:
        ``(swarm_ids, ordinary_ids)`` after classification.

    Raises:
        SwarmSkillSessionError: bound+swarm (non-continuable) or multiple swarm.
    """
    swarm_ids, ordinary_ids = classify_skill_ids(skill_ids)
    if len(swarm_ids) > 1:
        raise SwarmSkillSessionError(
            "At most one Swarm Skill may be selected per message.",
            code=SWARM_SKILL_MULTIPLE_NOT_ALLOWED,
            session_id=session_id,
        )
    bound = str(team_name or "").strip()
    if bound and swarm_ids:
        if _allows_bound_swarm_continuation(metadata, swarm_ids[0]):
            return swarm_ids, ordinary_ids
        raise SwarmSkillSessionError(
            "Swarm / team skills cannot be selected while a team is bound; "
            "unbind the expert team first or use ordinary skills.",
            code=SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND,
            session_id=session_id,
        )
    return swarm_ids, ordinary_ids


def _allows_bound_swarm_continuation(
    metadata: dict[str, Any] | None,
    swarm_skill_id: str,
) -> bool:
    """True when this bound session was opened by the same Swarm Skill."""
    if not is_session_generated_team(metadata):
        return False
    opened = str((metadata or {}).get("skill_id") or "").strip()
    wanted = str(swarm_skill_id or "").strip()
    return bool(opened) and bool(wanted) and opened == wanted


def clear_member_skill_allow_lists(snapshot: dict[str, Any]) -> None:
    """Force empty ``agents.*.skills`` / member skills so rail seeds open library.

    Always writes ``skills: []`` on dict agent/member blocks (even when the key
    was absent) so later template reconcile cannot "preserve" a missing key and
    leave bootstrap_allow ambiguous.

    Used only on the unbound + Swarm generated path. Must never run against an
    OfficeAce preset snapshot in place (需求: do not wipe OA experts' skills[]).
    """
    agents = snapshot.get("agents")
    if isinstance(agents, dict):
        for key, block in list(agents.items()):
            if not isinstance(block, dict):
                continue
            block = dict(block)
            block["skills"] = []
            agents[key] = block
        snapshot["agents"] = agents

    members = snapshot.get("predefined_members")
    if isinstance(members, list):
        cleaned: list[Any] = []
        for item in members:
            if isinstance(item, dict):
                member = dict(item)
                member["skills"] = []
                cleaned.append(member)
            else:
                cleaned.append(item)
        snapshot["predefined_members"] = cleaned


def should_skip_live_template_reconcile(metadata: dict[str, Any] | None) -> bool:
    """Session-generated open-library teams must not rehydrate preset skills[].

    ``reconcile_session_team_snapshot`` compares frozen entity/session snapshots
    to the live ``modes.team`` template. For Swarm / session_generated teams we
    intentionally cleared ``agents.*.skills``; refreshing from live would restore
    the whitelist and break empty-allow / T-J-08.
    """
    return is_session_generated_team(metadata)


def build_swarm_sop_hint(swarm_skill_id: str) -> str:
    """Short Leader-facing reminder; does not narrow visibility."""
    skill_id = str(swarm_skill_id or "").strip()
    return (
        f"{_SOP_HINT_PREFIX}\n"
        f"This session follows Swarm Skill `{skill_id}` as the collaboration SOP. "
        f"Before spawning teammates, read its SKILL.md, roles/, and workflow "
        f"(via skill tools). Prefer that playbook over ad-hoc team design.\n"
        f"{_SOP_HINT_SUFFIX}"
    )


def inject_swarm_sop_into_params(params: dict[str, Any], swarm_skill_id: str) -> None:
    """Prepend SOP hint to chat query/content (idempotent on re-entry).

    Why user-content (not a separate system channel): the Team Leader turn
    currently builds model inputs from ``query``/``content`` only; there is no
    Host rail that reliably appends a one-shot Swarm SOP to the system prompt
    for both fresh and resumed team runs. Tagged injection
    (``[Team collaboration SOP]…[/Team collaboration SOP]``) is therefore the
    deliberate M2 delivery path so the Leader sees the playbook reminder on
    the same turn that opens / continues the Swarm session.

    Mitigations already in place:
    - Idempotent: re-entry does not stack duplicate hints.
    - Title / generated-team naming capture the user text *before* inject
      (``chat._ensure_auto_team_binding_for_chat``).
    - Session metadata keeps ``skill_id`` / ``team_origin`` as structured
      identity; consumers must not treat the tagged block as user intent.

    Follow-up (not this MR): move the hint to a dedicated system-prompt /
    metadata channel once the team adapter exposes a stable injection point.
    Adapter ``_build_inputs`` prefers ``query`` over ``content`` when both are
    set, so inject into ``query`` first and also stamp ``content`` when present
    (REST / long-horizon dual-field sends).
    """
    hint = build_swarm_sop_hint(swarm_skill_id)
    if not hint:
        return
    wrote = False
    for key in ("query", "content"):
        raw = params.get(key)
        if not isinstance(raw, str):
            continue
        if _SOP_HINT_PREFIX in raw:
            wrote = True
            continue
        params[key] = f"{hint}\n\n{raw}" if raw.strip() else hint
        wrote = True
    if not wrote:
        params["query"] = hint


def is_session_generated_team(metadata: dict[str, Any] | None) -> bool:
    """Whether this session's team should be cleaned on dissolve (not a shelf asset)."""
    if not isinstance(metadata, dict):
        return False
    origin = str(metadata.get("team_origin") or "").strip()
    if origin == TEAM_ORIGIN_SESSION_GENERATED:
        return True
    # Legacy M1 temporary dyn sessions still need cleanup on dissolve.
    if bool(metadata.get("temporary_team_session") or metadata.get("team_temporary")):
        return True
    lifecycle = str(metadata.get("team_lifecycle") or "").strip().lower()
    return lifecycle == "temporary"


def reject_deprecated_dynamic_entry(params: dict[str, Any] | None) -> None:
    """Refuse M1 ``entry=dynamic_*`` openers on ``team.session.bind``."""
    if not isinstance(params, dict):
        return
    entry = str(params.get("entry") or "").strip()
    if entry in DEPRECATED_DYNAMIC_ENTRIES:
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

        raise TeamBindingStoreError(
            "dynamic entry openers are removed; bind with team_name or send a "
            "Swarm Skill on chat.send without a bound team",
            code="BAD_REQUEST",
        )


def meta_looks_like_swarm_skill(
    *,
    kind: Any = None,
    skill_type: Any = None,
) -> bool:
    """Frontend/DTO dual-read helper (no disk I/O)."""
    return is_swarm_kind(kind) or is_swarm_skill_type(skill_type)
