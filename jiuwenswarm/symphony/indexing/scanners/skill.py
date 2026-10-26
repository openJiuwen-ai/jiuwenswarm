from __future__ import annotations

import json
from pathlib import Path

from shared.tags import normalize_tags

from .base import BaseScanner, ScannedItem, console
from .common import clean_first_paragraph, parse_frontmatter


class SkillScanner(BaseScanner):
    item_type = "skill"

    def __init__(self, items_dir: Path | str, *, display_items_dir: Path | str | None = None) -> None:
        super().__init__(items_dir, display_items_dir=display_items_dir)
        self._metadata: dict[str, dict[str, object]] = {}
        self._load_metadata()

    def _load_metadata(self) -> None:
        metadata_path = self.items_dir / "skills.json"
        if not metadata_path.exists():
            return
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            console.print(f"[yellow]Warning: Failed to load skills.json: {exc}[/yellow]")
            return
        for item in payload.get("skills", []):
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            self._metadata[item_id] = {
                "github_url": item.get("github_url", ""),
                "stars": item.get("stars", 0),
                "is_official": item.get("is_official", False),
                "author": item.get("author", ""),
                "tags": normalize_tags(item.get("tags")),
            }

    @classmethod
    def detect_item_root(cls, path: Path) -> Path | None:
        for filename in ("SKILL.md", "skill.md", "Skill.md"):
            candidate = path / filename
            if candidate.exists():
                return path
        return None

    def scan_item_dir(self, item_dir: Path) -> ScannedItem | None:
        item_root = self.detect_item_root(item_dir)
        if item_root is None:
            return None

        skill_file = next(
            (item_root / name for name in ("SKILL.md", "skill.md", "Skill.md") if (item_root / name).exists()), None
        )
        if skill_file is None:
            return None
        try:
            content = skill_file.read_text(encoding="utf-8")
        except Exception as exc:
            console.print(f"[yellow]Failed to read {skill_file}: {exc}[/yellow]")
            return None

        frontmatter, body = parse_frontmatter(content)
        item_id = item_root.name
        meta = self._metadata.get(item_id, {})
        name = str(frontmatter.get("name") or item_root.name).strip() or item_root.name
        description = str(frontmatter.get("description") or "").strip()
        if not description:
            description = clean_first_paragraph(body)

        return ScannedItem(
            id=item_id,
            name=name,
            description=description,
            item_path=str(skill_file.resolve()),
            content=body.strip(),
            github_url=str(meta.get("github_url") or ""),
            stars=int(meta.get("stars") or 0),
            is_official=bool(meta.get("is_official")),
            author=str(meta.get("author") or ""),
            tags=normalize_tags(
                frontmatter.get("tags"),
                meta.get("tags"),
            ),
        )
