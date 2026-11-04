# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fresh typed local state for routing; retrieval scores are never health evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill.archive_store import compute_content_checksum
from jiuwenswarm.server.runtime.skill.skillpack import (
    SkillPackValidationError,
    compute_skillpack_status,
    is_skillpack,
    load_skillpack,
    read_skillpack_section,
)
from jiuwenswarm.server.runtime.skill.skill_type import detect_skill_type

_ROUTING_SECTIONS = {
    "when_to_use": "When to use",
    "do_not_use": "Do not use",
    "required_inputs": "Required inputs",
    "side_effects": "Side effects and confirmation",
    "final_output": "Final output",
}


def skillpack_inventory_snapshot(
    skills_dir: Path,
    enabled_for: Callable[[str], bool],
) -> dict[str, Any]:
    """Hash live content and enabled state, including ordinary members."""
    records: list[dict[str, Any]] = []
    for child in sorted(skills_dir.iterdir() if skills_dir.is_dir() else []):
        if child.name.startswith((".", "_")) or not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            continue
        record: dict[str, Any] = {
            "id": child.name,
            "name": child.name,
            "skill_type": detect_skill_type(child),
            "enabled": enabled_for(child.name),
            "content_sha256": compute_content_checksum(child),
            "members": [],
            "blocked_members": [],
            "routing": {},
        }
        if is_skillpack(child):
            try:
                definition = load_skillpack(child, expected_name=child.name)
                status = compute_skillpack_status(
                    definition, skills_dir=skills_dir, enabled_for=enabled_for,
                )
                record.update(
                    description=definition.description,
                    enabled=status.enabled,
                    members=list(definition.members),
                    blocked_members=list(status.blocked_members),
                    routing={
                        key: read_skillpack_section(child, section)
                        for key, section in _ROUTING_SECTIONS.items()
                    },
                )
            except (SkillPackValidationError, OSError):
                record.update(enabled=False, blocked_members=[{"name": child.name, "reason": "invalid"}])
        records.append(record)
    revision = hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"inventory_revision": revision, "skills": records}
