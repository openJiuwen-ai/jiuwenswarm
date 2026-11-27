# pylint: disable=line-too-long,too-many-boolean-expressions,too-many-return-values
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseScanner, ScannedItem, console
from .common import clean_first_paragraph, parse_frontmatter, read_text_if_exists


class PluginScanner(BaseScanner):
    item_type = "plugin"

    @classmethod
    def _plugin_metadata_candidates(cls, path: Path) -> tuple[Path, ...]:
        return (
            path / ".codex-plugin" / "plugin.yaml",
            path / ".codex-plugin" / "plugin.yml",
            path / "plugin.yaml",
            path / "plugin.yml",
            path / ".codex-plugin" / "plugin.json",
            path / "plugin.json",
        )

    @classmethod
    def detect_item_root(cls, path: Path) -> Path | None:
        for candidate in cls._plugin_metadata_candidates(path):
            if candidate.exists():
                return path
        for filename in ("SKILL.md", "skill.md", "Skill.md"):
            if (path / filename).exists():
                return path
        return None

    def scan_item_dir(self, item_dir: Path) -> ScannedItem | None:
        item_root = self.detect_item_root(item_dir)
        if item_root is None:
            return None

        plugin_file = next((candidate for candidate in self._plugin_metadata_candidates(item_root) if candidate.exists()), None)
        if plugin_file is not None:
            payload = self._load_plugin_payload(plugin_file)
            if payload is None:
                return None
            readme_text = read_text_if_exists(item_root / "README.md")
            description = str(payload.get("description") or "").strip() or clean_first_paragraph(readme_text)
            content = readme_text.strip()
            metadata = payload.get("metadata")
            author = ""
            if isinstance(metadata, dict):
                author = str(metadata.get("author") or "").strip()
            if not author:
                author = str(payload.get("author") or "").strip()
            display_name = str(payload.get("display_name") or "").strip()
            plugin_name = str(payload.get("name") or "").strip()
            return ScannedItem(
                id=item_root.name,
                name=display_name or plugin_name or item_root.name,
                description=description,
                item_path=str(plugin_file.resolve()),
                content=content,
                author=author,
            )

        skill_file = next((item_root / name for name in ("SKILL.md", "skill.md", "Skill.md") if (item_root / name).exists()), None)
        if skill_file is None:
            return None
        try:
            content = skill_file.read_text(encoding="utf-8")
        except Exception as exc:
            console.print(f"[yellow]Failed to read {skill_file}: {exc}[/yellow]")
            return None
        frontmatter, body = parse_frontmatter(content)
        description = str(frontmatter.get("description") or "").strip() or clean_first_paragraph(body)
        return ScannedItem(
            id=item_root.name,
            name=str(frontmatter.get("name") or item_root.name).strip() or item_root.name,
            description=description,
            item_path=str(skill_file.resolve()),
            content=body.strip(),
        )

    @staticmethod
    def _load_plugin_payload(plugin_file: Path) -> dict[str, Any] | None:
        try:
            if plugin_file.suffix.lower() == ".json":
                payload = json.loads(plugin_file.read_text(encoding="utf-8"))
            else:
                text = plugin_file.read_text(encoding="utf-8")
                try:
                    import yaml  # type: ignore
                except ModuleNotFoundError:
                    payload = PluginScanner._parse_simple_yaml_payload(text)
                else:
                    payload = yaml.safe_load(text) or {}
        except Exception as exc:
            console.print(f"[yellow]Failed to read {plugin_file}: {exc}[/yellow]")
            return None
        if not isinstance(payload, dict):
            console.print(f"[yellow]Failed to read {plugin_file}: expected a mapping payload[/yellow]")
            return None
        return payload

    @staticmethod
    def _parse_simple_yaml_payload(text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        active_section: str | None = None
        for raw_line in text.splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            line = raw_line.strip()
            if indent == 0:
                active_section = None
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                clean_key = key.strip()
                clean_value = value.strip()
                if clean_value:
                    payload[clean_key] = PluginScanner._parse_yaml_scalar(clean_value)
                else:
                    payload[clean_key] = {}
                    active_section = clean_key
                continue
            if active_section and indent >= 2 and ":" in line and not line.startswith("- "):
                section = payload.get(active_section)
                if not isinstance(section, dict):
                    section = {}
                    payload[active_section] = section
                key, value = line.split(":", 1)
                clean_value = value.strip()
                section[key.strip()] = PluginScanner._parse_yaml_scalar(clean_value) if clean_value else {}
        return payload

    @staticmethod
    def _parse_yaml_scalar(value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1]
        return text
