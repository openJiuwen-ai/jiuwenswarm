from __future__ import annotations

import datetime
import secrets
from typing import Any


def _vet_section(state: dict[str, Any]) -> dict[str, Any]:
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        section = {}
        state["skill_vet"] = section
    return section


def remove_skill_hash(state: dict[str, Any], skill_name: str) -> bool:
    """Drop a skill's recorded content baseline and any outstanding token.

    Returns True if anything was removed.

    ``skill_hashes`` drives the enabled-skill update gate: on reinstall of a
    same-named skill with different bytes, a stale entry would make the fresh
    install look "changed while enabled" and auto-disable it. Prune on uninstall.
    The approval token is also dropped so it cannot outlive the skill it was
    minted for. Defensive against a missing or non-dict ``skill_vet`` section.
    """
    if not skill_name:
        return False
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        return False
    removed = False
    hashes = section.get("skill_hashes")
    if isinstance(hashes, dict) and skill_name in hashes:
        del hashes[skill_name]
        removed = True
    tokens = section.get("tokens")
    if isinstance(tokens, dict) and skill_name in tokens:
        del tokens[skill_name]
        removed = True
    return removed


def get_vet_report(state: dict[str, Any], content_hash: str) -> dict[str, Any] | None:
    reports = _vet_section(state).get("reports")
    if not isinstance(reports, dict):
        return None
    value = reports.get(content_hash)
    return value if isinstance(value, dict) else None


def set_vet_report(state: dict[str, Any], report_dict: dict[str, Any]) -> None:
    section = _vet_section(state)
    reports = section.get("reports")
    if not isinstance(reports, dict):
        reports = {}
        section["reports"] = reports
    content_hash = str(report_dict.get("content_hash") or "").strip()
    if content_hash:
        reports[content_hash] = report_dict


def get_vet_approval(state: dict[str, Any], content_hash: str) -> dict[str, Any] | None:
    approvals = _vet_section(state).get("approvals")
    if not isinstance(approvals, dict):
        return None
    value = approvals.get(content_hash)
    return value if isinstance(value, dict) else None


def set_vet_approval(state: dict[str, Any], content_hash: str, approved_by: str) -> None:
    section = _vet_section(state)
    approvals = section.get("approvals")
    if not isinstance(approvals, dict):
        approvals = {}
        section["approvals"] = approvals
    approvals[content_hash] = {
        "approved_by": approved_by,
        "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def issue_vet_token(state: dict[str, Any], skill_name: str, content_hash: str) -> str:
    """Mint a single-use approval token bound to ``(skill_name, content_hash)``.

    At most one live token is kept per skill: issuing replaces any prior entry.
    The token is evidence-of-review binding, not role-based authorization.
    """
    token = secrets.token_urlsafe(32)
    if not skill_name:
        return token
    section = _vet_section(state)
    tokens = section.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
        section["tokens"] = tokens
    tokens[skill_name] = {
        "content_hash": content_hash,
        "token": token,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return token


def consume_vet_token(
    state: dict[str, Any], skill_name: str, content_hash: str, token: str
) -> bool:
    """Consume the token for *skill_name* iff it matches name + hash + token.

    Single-use: a successful match deletes the entry. A missing entry, a wrong
    token, or a hash mismatch all return False and leave the entry intact, so
    every failure mode is fail-closed.
    """
    if not skill_name or not token:
        return False
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        return False
    tokens = section.get("tokens")
    if not isinstance(tokens, dict):
        return False
    entry = tokens.get(skill_name)
    if not isinstance(entry, dict):
        return False
    if entry.get("content_hash") != content_hash:
        return False
    stored = entry.get("token")
    if not isinstance(stored, str) or not secrets.compare_digest(stored, token):
        return False
    del tokens[skill_name]
    return True


def invalidate_vet_tokens(state: dict[str, Any], skill_name: str) -> None:
    """Drop any outstanding approval token for *skill_name* (defensive)."""
    if not skill_name:
        return
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        return
    tokens = section.get("tokens")
    if isinstance(tokens, dict):
        tokens.pop(skill_name, None)
