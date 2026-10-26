"""Host-owned profile version disk helpers (WEB-03).

Scans ``im/profiles/versions`` and reads version files. Activate/resolve
delegate to Core ``activate_profile_version`` / ``resolve_current_profile``.
No classes — closed-class inventory allows only ``PersonalContextHostAPI``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openjiuwen.harness.personal_context.distill import (
    activate_profile_version,
    resolve_current_profile,
)
from openjiuwen.harness.personal_context.distill.profile import (
    META_FILENAME,
    PERSONA_FILENAME,
    WORK_FILENAME,
    current_json_path,
    load_text,
    profiles_root,
)

# Align preview truncation with Core profile clip limits.
_PREVIEW_CLIP_CHARS = 12000


def _clip(text: str, limit: int = _PREVIEW_CLIP_CHARS) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _safe_job_id(job_id: str) -> str:
    """Reject empty / multi-segment / traversal job_id before any disk join.

    ``version_dir(home, job_id)`` is a plain Path join; values like ``../x``
    escape ``im/profiles/versions``. Host RPC must only accept a single
    directory name under that root.
    """

    cleaned = (job_id or "").strip()
    if not cleaned:
        raise ValueError("job_id must be a non-empty string")
    if cleaned in {".", ".."}:
        raise ValueError("job_id is invalid")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise ValueError("job_id must be a single path segment")
    candidate = Path(cleaned)
    if candidate.is_absolute() or candidate.anchor:
        raise ValueError("job_id must be a single path segment")
    if candidate.name != cleaned:
        # Catches oddities like trailing separators on some platforms.
        raise ValueError("job_id must be a single path segment")
    return cleaned


def _resolved_version_dir(home: str, job_id: str) -> Path:
    """Return the version directory only when it stays under ``versions/``."""

    cleaned = _safe_job_id(job_id)
    versions_root = (profiles_root(home) / "versions").resolve()
    root = (versions_root / cleaned).resolve()
    try:
        root.relative_to(versions_root)
    except ValueError as exc:
        raise ValueError("job_id escapes versions directory") from exc
    if root.parent != versions_root:
        raise ValueError("job_id escapes versions directory")
    return root


def _version_files_complete(root: Path) -> bool:
    return (
        (root / PERSONA_FILENAME).is_file()
        and (root / WORK_FILENAME).is_file()
        and (root / META_FILENAME).is_file()
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_current_pointer(home: str) -> dict[str, Any] | None:
    return _read_json(current_json_path(home))


def list_profile_versions(home: str) -> list[dict[str, Any]]:
    """Scan ``versions/*`` directories; mark current via ``current.json``."""

    pointer = _read_current_pointer(home)
    current_job = str((pointer or {}).get("job_id") or "").strip()
    versions_root = profiles_root(home) / "versions"
    items: list[dict[str, Any]] = []
    if not versions_root.is_dir():
        return items
    for child in versions_root.iterdir():
        if not child.is_dir():
            continue
        job_id = child.name.strip()
        try:
            job_id = _safe_job_id(job_id)
        except ValueError:
            continue
        if not _version_files_complete(child):
            continue
        meta = _read_json(child / META_FILENAME) or {}
        published_at_ms = meta.get("published_at_ms")
        if published_at_ms is None and pointer and job_id == current_job:
            published_at_ms = pointer.get("published_at_ms")
        if published_at_ms is None:
            try:
                published_at_ms = int(child.stat().st_mtime * 1000)
            except OSError:
                published_at_ms = 0
        source = meta.get("source")
        if source is None and pointer and job_id == current_job:
            source = pointer.get("source")
        meta_summary = {
            "message_count": meta.get("message_count"),
            "analyzer": meta.get("analyzer"),
        }
        items.append(
            {
                "job_id": job_id,
                "is_current": job_id == current_job,
                "published_at_ms": int(published_at_ms or 0),
                "source": source or "distill",
                "meta_summary": meta_summary,
            }
        )
    items.sort(
        key=lambda item: (
            0 if item.get("is_current") else 1,
            -int(item.get("published_at_ms") or 0),
        )
    )
    return items


def get_current_profile(home: str) -> dict[str, Any] | None:
    """Delegate to Core resolve; empty when missing or incomplete."""

    return resolve_current_profile(home)


def get_profile_version(
    home: str,
    job_id: str,
    *,
    clip_chars: int = _PREVIEW_CLIP_CHARS,
) -> dict[str, Any]:
    """Load one version's persona/work/meta; clip long markdown."""

    cleaned = _safe_job_id(job_id)
    root = _resolved_version_dir(home, cleaned)
    if not _version_files_complete(root):
        raise FileNotFoundError(f"profile version not found or incomplete: {cleaned}")
    meta = _read_json(root / META_FILENAME) or {}
    persona_md, persona_truncated = _clip(load_text(root / PERSONA_FILENAME), clip_chars)
    work_md, work_truncated = _clip(load_text(root / WORK_FILENAME), clip_chars)
    return {
        "job_id": cleaned,
        "persona_md": persona_md,
        "work_md": work_md,
        "meta": meta,
        "truncated": persona_truncated or work_truncated,
    }


def activate_profile(home: str, job_id: str) -> dict[str, Any]:
    """Atomically switch ``current.json`` via Core (source=manual_switch)."""

    cleaned = _safe_job_id(job_id)
    # Validate containment before Core join; Core still receives only the safe id.
    _resolved_version_dir(home, cleaned)
    return activate_profile_version(home, cleaned, source="manual_switch")


__all__ = [
    "activate_profile",
    "get_current_profile",
    "get_profile_version",
    "list_profile_versions",
]
