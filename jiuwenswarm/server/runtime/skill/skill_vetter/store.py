from __future__ import annotations

import datetime
from typing import Any


def _vet_section(state: dict[str, Any]) -> dict[str, Any]:
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        section = {}
        state["skill_vet"] = section
    return section


def remove_skill_hash(state: dict[str, Any], skill_name: str) -> bool:
    """Drop a skill's recorded content baseline. Returns True if anything was removed.

    ``skill_hashes`` drives the enabled-skill update gate: on reinstall of a
    same-named skill with different bytes, a stale entry would make the fresh
    install look "changed while enabled" and auto-disable it. Prune on uninstall.
    """
    if not skill_name:
        return False
    section = state.get("skill_vet")
    if not isinstance(section, dict):
        return False
    hashes = section.get("skill_hashes")
    if not isinstance(hashes, dict) or skill_name not in hashes:
        return False
    del hashes[skill_name]
    return True


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
