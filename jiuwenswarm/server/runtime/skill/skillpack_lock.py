# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exact, ordered member identities used to distribute a thin SkillPack."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.server.runtime.skill.skillpack import (
    SkillPackDefinition,
    SkillPackValidationError,
    load_skillpack,
)

LOCK_FILENAME = "skillpack.lock.json"
LOCK_SCHEMA = "openjiuwen.skillpack.lock/v1"
SKILLPACK_HUB_TAG = "openjiuwen-skillpack-v1"


def has_skillpack_wrapper_tag(extracted: Path) -> bool:
    """Use wrapper tags only to route into strict root/lock validation."""
    for path in extracted.rglob("plugin.yaml"):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
            continue
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        tags = metadata.get("tags") if isinstance(metadata, dict) else None
        if isinstance(tags, list) and SKILLPACK_HUB_TAG in tags:
            return True
    return False


@dataclass(frozen=True)
class SkillPackLockedMember:
    name: str
    asset_id: str
    version: str
    artifact_sha256: str


@dataclass(frozen=True)
class SkillPackLock:
    name: str
    version: str
    members: tuple[SkillPackLockedMember, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LOCK_SCHEMA,
            "pack": {"name": self.name, "version": self.version},
            "members": [asdict(member) for member in self.members],
        }


def _identity(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or any(char in value for char in "/\\\x00")
        or any(ord(char) < 32 for char in value)
        or len(value) > 256
    ):
        raise SkillPackValidationError(f"Invalid SkillPack lock {field}")
    return value


def parse_skillpack_lock(
    payload: Any,
    definition: SkillPackDefinition,
    expected_version: str | None = None,
) -> SkillPackLock:
    """Validate identities, exact digests and root/member agreement."""
    if not isinstance(payload, dict) or payload.get("schema_version") != LOCK_SCHEMA:
        raise SkillPackValidationError("Unsupported SkillPack lock schema")
    pack = payload.get("pack")
    if not isinstance(pack, dict) or pack.get("name") != definition.name:
        raise SkillPackValidationError("SkillPack lock name differs from SKILL.md")
    version = _identity(pack.get("version"), "pack.version")
    if version.lower() == "latest" or any(char in version for char in "^~<>=|*, "):
        raise SkillPackValidationError("SkillPack version must be exact")
    if expected_version is not None and version != expected_version:
        raise SkillPackValidationError("SkillPack lock version differs from Hub version")
    raw_members = payload.get("members")
    if not isinstance(raw_members, list) or len(raw_members) != len(definition.members):
        raise SkillPackValidationError("SkillPack lock members differ from SKILL.md")
    members = []
    asset_ids: set[str] = set()
    for name, raw in zip(definition.members, raw_members):
        if not isinstance(raw, dict) or raw.get("name") != name:
            raise SkillPackValidationError("SkillPack lock must preserve SKILL.md member order")
        asset_id = _identity(raw.get("asset_id"), "asset_id")
        member_version = _identity(raw.get("version"), "version")
        if member_version.lower() == "latest" or any(
            char in member_version for char in "^~<>=|*, "
        ):
            raise SkillPackValidationError("SkillPack member version must be exact")
        digest = raw.get("artifact_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SkillPackValidationError("SkillPack member requires a SHA-256 artifact digest")
        if asset_id in asset_ids:
            raise SkillPackValidationError("SkillPack member asset IDs must be unique")
        asset_ids.add(asset_id)
        members.append(SkillPackLockedMember(name, asset_id, member_version, digest))
    return SkillPackLock(definition.name, version, tuple(members))


def load_skillpack_lock(
    skill_dir: Path,
    expected_version: str | None = None,
) -> SkillPackLock:
    try:
        payload = json.loads((skill_dir / LOCK_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillPackValidationError("SkillPack requires a valid skillpack.lock.json") from exc
    return parse_skillpack_lock(payload, load_skillpack(skill_dir), expected_version)
