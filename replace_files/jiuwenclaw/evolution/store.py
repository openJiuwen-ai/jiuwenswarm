# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""EvolutionStore - Pure IO layer for skill evolution data."""
from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from jiuwenclaw.evolution.schema import (
    EvolutionChange,
    EvolutionEntry,
    EvolutionFile,
    ExperienceTarget,
)

logger = logging.getLogger(__name__)

_EVOLUTION_FILENAME = "evolutions.json"


class EvolutionStore:
    """Handles all file-system IO for the evolution system.

    Responsibilities:
      - Skill directory scanning and SKILL.md reading
      - evolutions.json read/write (single unified file; target distinguished by entry.change.target)
      - Solidification (writing pending body entries into SKILL.md)
      - Experience text formatting for system prompts
    """

    def __init__(self, skills_base_dir: str) -> None:
        self._base = Path(skills_base_dir)

    @property
    def base_dir(self) -> Path:
        return self._base

    # ------------------------------------------------------------------
    # File-system queries
    # ------------------------------------------------------------------

    def list_skill_names(self) -> List[str]:
        """List all skill directory names under base dir."""
        if not self._base.exists():
            return []
        return [
            d.name
            for d in self._base.iterdir()
            if d.is_dir() and not d.name.startswith("_")
        ]

    def skill_exists(self, name: str) -> bool:
        return (self._base / name).is_dir()

    def read_skill_content(self, name: str) -> str:
        """Read SKILL.md raw content for a skill."""
        skill_dir = self._base / name
        md_path = self._find_skill_md(skill_dir)
        if md_path is None:
            return ""
        try:
            return md_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("[EvolutionStore] failed to read %s: %s", md_path, exc)
            return ""

    # ------------------------------------------------------------------
    # Evolution file read/write (unified file, target distinguished by field)
    # ------------------------------------------------------------------

    def load_evolution_file(
        self, name: str, target: Optional[ExperienceTarget] = None,
    ) -> EvolutionFile:
        """Load the unified evolution file for the given skill.

        Args:
            name: Skill name.
            target: If provided, returns a view with only entries matching that
                target (entries list is filtered; the returned object should be
                treated as read-only for save purposes).  Pass ``None`` to get
                all entries.
        """
        skill_dir = self._base / name
        unified_path = skill_dir / _EVOLUTION_FILENAME

        if unified_path.exists():
            evo_file = self._read_evo_json(unified_path, name)
        else:
            evo_file = EvolutionFile.empty(skill_id=name)

        if target is not None:
            evo_file = EvolutionFile(
                skill_id=evo_file.skill_id,
                version=evo_file.version,
                updated_at=evo_file.updated_at,
                entries=[e for e in evo_file.entries if e.change.target == target],
            )
        return evo_file

    def append_entry(self, name: str, entry: EvolutionEntry) -> None:
        """Append an evolution entry to the unified evolutions.json.

        If ``entry.change.merge_target`` is set, replaces the matching entry
        instead of appending.
        """
        evo_file = self._load_full_evolution_file(name)
        merge_target = getattr(entry.change, "merge_target", None)

        if merge_target:
            replaced = False
            for i, existing in enumerate(evo_file.entries):
                if existing.id == merge_target:
                    evo_file.entries[i] = entry
                    replaced = True
                    logger.info(
                        "[EvolutionStore] merged entry %s replacing %s",
                        entry.id,
                        merge_target,
                    )
                    break
            if not replaced:
                evo_file.entries.append(entry)
        else:
            evo_file.entries.append(entry)

        evo_file.updated_at = datetime.now(tz=timezone.utc).isoformat()
        self._save_evolution_file(name, evo_file)
        logger.info(
            "[EvolutionStore] wrote %s/%s (id=%s, target=%s)",
            name, _EVOLUTION_FILENAME, entry.id, entry.change.target.value,
        )

    # ------------------------------------------------------------------
    # Typed pending-entry loaders
    # ------------------------------------------------------------------

    def load_desc_pending_entries(self, name: str) -> List[EvolutionEntry]:
        """Load pending description-experience entries for a skill."""
        return self.load_evolution_file(name, ExperienceTarget.DESCRIPTION).pending_entries

    def load_body_pending_entries(self, name: str) -> List[EvolutionEntry]:
        """Load pending body-experience entries for a skill."""
        return self.load_evolution_file(name, ExperienceTarget.BODY).pending_entries

    def get_pending_entries(
        self, name: str, target: Optional[ExperienceTarget] = None,
    ) -> List[EvolutionEntry]:
        """Load pending entries, optionally filtered by target.

        If *target* is ``None``, returns all pending entries from the unified file.
        """
        return self.load_evolution_file(name, target).pending_entries

    # ------------------------------------------------------------------
    # Solidification (body experiences only)
    # ------------------------------------------------------------------

    def solidify(self, name: str) -> int:
        """Write pending **body** entries into SKILL.md, mark them as applied.

        Description experiences are not solidified into SKILL.md; they live
        in evolutions.json and are injected at prompt-build time.

        Returns:
            Number of entries solidified.
        """
        skill_dir = self._base / name
        evo_file = self._load_full_evolution_file(name)
        pending = [e for e in evo_file.pending_entries if e.change.target == ExperienceTarget.BODY]
        if not pending:
            return 0

        skill_md_path = self._find_skill_md(skill_dir)
        if skill_md_path is None:
            logger.warning("[EvolutionStore] solidify: SKILL.md not found (skill=%s)", name)
            return 0

        content = skill_md_path.read_text(encoding="utf-8")
        for entry in pending:
            content = self._inject_section(content, entry.change)
            entry.applied = True

        skill_md_path.write_text(content, encoding="utf-8")
        evo_file.updated_at = datetime.now(tz=timezone.utc).isoformat()
        self._save_evolution_file(name, evo_file)
        logger.info("[EvolutionStore] solidified %d body entries (skill=%s)", len(pending), name)
        return len(pending)

    # ------------------------------------------------------------------
    # Experience text formatting
    # ------------------------------------------------------------------

    def format_desc_experience_text(self, name: str) -> str:
        """Format pending description experiences for a single skill.

        Returns a compact Markdown string suitable for appending after the
        skill's description line in the system prompt.
        """
        pending = self.load_desc_pending_entries(name)
        if not pending:
            return ""
        lines: List[str] = []
        for entry in pending:
            lines.append(f"- {entry.change.content}")
        return "\n".join(lines)

    def format_all_desc_experiences(self, names: List[str]) -> Dict[str, str]:
        """Batch-format description experiences for multiple skills.

        Returns:
            ``{skill_name: formatted_text}`` for skills that have pending
            description experiences.
        """
        result: Dict[str, str] = {}
        for name in names:
            text = self.format_desc_experience_text(name)
            if text:
                result[name] = text
        return result

    def format_body_experience_text(self, name: str) -> str:
        """Format pending body experiences for a single skill.

        Useful for ``/evolve list`` display and for passing to the evolver
        as existing-entries context.
        """
        pending = self.load_body_pending_entries(name)
        if not pending:
            return ""
        lines = [f"\n\n# Skill '{name}' body 演进经验\n"]
        for index, entry in enumerate(pending):
            lines.append(f"{index + 1}. **[{entry.change.section}]** {entry.change.content}")
        return "\n".join(lines)

    def list_pending_summary(self, names: List[str]) -> str:
        """Return a human-readable pending summary for multiple skills."""
        lines: List[str] = []
        count = 0
        for name in names:
            desc_pending = self.load_desc_pending_entries(name)
            body_pending = self.load_body_pending_entries(name)
            all_pending = desc_pending + body_pending
            if not all_pending:
                continue

            count += 1
            lines.append(
                f"{count}. **{name}** - 共 {len(all_pending)} 条 pending 经验"
                f"（description: {len(desc_pending)}, body: {len(body_pending)}）"
            )
            for e in all_pending:
                tag = "description" if e.change.target == ExperienceTarget.DESCRIPTION else "body"
                content = e.change.content
                title = content.split("\n")[0] if "\n" in content else content[:50]
                lines.append(f"   - [{tag}] **{title}**: ")
                if "\n" in content:
                    body_lines = content.split("\n")[1:]
                    if body_lines:
                        summary = " ".join(
                            ln.strip().lstrip("- ") for ln in body_lines if ln.strip()
                        )
                        lines.append(f"    {summary[:100].replace('**', '')}")
            lines.append("")

        if not lines:
            return "当前所有 Skill 暂无演进信息。"
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_full_evolution_file(self, name: str) -> EvolutionFile:
        """Load the full (unfiltered) unified evolution file for *name*."""
        skill_dir = self._base / name
        unified_path = skill_dir / _EVOLUTION_FILENAME
        if unified_path.exists():
            return self._read_evo_json(unified_path, name)
        return EvolutionFile.empty(skill_id=name)

    def _save_evolution_file(self, name: str, evo_file: EvolutionFile) -> None:
        skill_dir = self._base / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        evo_path = skill_dir / _EVOLUTION_FILENAME
        try:
            evo_path.write_text(
                json.dumps(evo_file.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("[EvolutionStore] write %s failed: %s", _EVOLUTION_FILENAME, exc)

    @staticmethod
    def _read_evo_json(path: Path, skill_id: str) -> EvolutionFile:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return EvolutionFile.from_dict(data)
        except Exception as exc:
            logger.warning("[EvolutionStore] read %s failed: %s", path.name, exc)
        return EvolutionFile.empty(skill_id=skill_id)

    @staticmethod
    def _find_skill_md(skill_dir: Path) -> Optional[Path]:
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            return skill_md
        md_files = list(skill_dir.glob("*.md"))
        return md_files[0] if md_files else None

    @staticmethod
    def _inject_section(content: str, change: EvolutionChange) -> str:
        """Append change.content to the corresponding section in SKILL.md."""
        section = change.section
        addition = f"\n{change.content}\n"
        header_pattern = re.compile(
            rf"(## {re.escape(section)}.*?)(\n## |\Z)", re.DOTALL
        )
        m = header_pattern.search(content)
        if m:
            insert_pos = m.start(2)
            content = content[:insert_pos] + addition + content[insert_pos:]
        else:
            content = content.rstrip() + f"\n\n## {section}\n{change.content}\n"
        return content
