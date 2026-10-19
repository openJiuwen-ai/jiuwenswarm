from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.common.utils import get_agent_skills_dir


@dataclass(frozen=True)
class SkillInventoryItem:
    worker_id: str
    name: str
    description: str
    skill_dir: str
    skill_file: str
    source: str
    version: str
    author: str
    content_hash: str


@dataclass(frozen=True)
class SkillInventory:
    skills_dir: str
    items: tuple[SkillInventoryItem, ...]
    fingerprint: str

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def item_paths(self) -> list[str]:
        return [item.skill_dir for item in self.items]

    def to_state_payload(self) -> dict[str, Any]:
        return {
            "skills_dir": self.skills_dir,
            "count": self.count,
            "fingerprint": self.fingerprint,
            "items": [asdict(item) for item in self.items],
        }


def scan_skill_inventory(manager: Any) -> SkillInventory:
    skills_dir = _manager_skills_dir(manager)
    source_by_name = _source_by_name(manager)
    items: list[SkillInventoryItem] = []
    if skills_dir.exists():
        for child in sorted(skills_dir.iterdir(), key=lambda path: path.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or child.name.startswith("_"):
                continue
            skill_file = _find_skill_file(child)
            if skill_file is None:
                continue
            meta, body = _parse_skill_md(skill_file)
            name = str(meta.get("name") or child.name).strip() or child.name
            description = str(meta.get("description") or "").strip() or _first_paragraph(body)
            items.append(
                SkillInventoryItem(
                    worker_id=child.name,
                    name=name,
                    description=description,
                    skill_dir=str(child.resolve()),
                    skill_file=str(skill_file.resolve()),
                    source=source_by_name.get(name, "local"),
                    version=str(meta.get("version") or "").strip(),
                    author=str(meta.get("author") or "").strip(),
                    content_hash=_sha256_file(skill_file),
                )
            )
    ordered = tuple(sorted(items, key=lambda item: (item.name.lower(), item.worker_id.lower())))
    return SkillInventory(
        skills_dir=str(skills_dir.resolve()),
        items=ordered,
        fingerprint=_inventory_fingerprint(ordered),
    )


def _manager_skills_dir(manager: Any) -> Path:
    raw = getattr(manager, "_skills_dir", None)
    if raw:
        return Path(raw)
    return get_agent_skills_dir()


def _source_by_name(manager: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _call_list(manager, "get_local_skills"):
        name = str(item.get("name") or "").strip()
        source = str(item.get("source") or "").strip()
        if name and source:
            out[name] = source
    for item in _call_list(manager, "get_installed_plugins"):
        name = str(item.get("name") or "").strip()
        source = str(item.get("source") or item.get("marketplace") or "").strip()
        if name and source:
            out.setdefault(name, source)
    return out


def _call_list(manager: Any, method_name: str) -> list[dict[str, Any]]:
    method = getattr(manager, method_name, None)
    if not callable(method):
        return []
    try:
        value = method()
    except Exception:
        return []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _find_skill_file(skill_dir: Path) -> Path | None:
    for filename in ("SKILL.md", "skill.md", "Skill.md"):
        candidate = skill_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _parse_skill_md(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if match:
        body = match.group(2).strip()
        loaded = yaml.safe_load(match.group(1)) or {}
        if isinstance(loaded, dict):
            meta = {str(key): value for key, value in loaded.items()}
    return meta, body


def _first_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n", str(text or "").strip()):
        cleaned = " ".join(line.strip() for line in block.splitlines()).strip()
        if cleaned:
            return cleaned[:500]
    return ""


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _inventory_fingerprint(items: tuple[SkillInventoryItem, ...]) -> str:
    payload = [
        {
            "worker_id": item.worker_id,
            "name": item.name,
            "skill_dir": item.skill_dir,
            "skill_file": item.skill_file,
            "version": item.version,
            "content_hash": item.content_hash,
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
