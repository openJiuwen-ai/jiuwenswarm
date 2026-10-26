"""Skill folder discovery."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional, Union

from jiuwenswarm.symphony.fingerprint.models import SkillFolder


class SkillFolderScanner:
    """Find folders that contain a SKILL.md entrypoint."""

    @staticmethod
    def scan(
        skills_root: Union[Path, str],
        max_depth: Optional[int] = None,
    ) -> List[SkillFolder]:
        root = Path(skills_root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"skills_root does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"skills_root is not a directory: {root}")

        folders: List[SkillFolder] = []
        for entry in sorted(root.rglob("SKILL.md"), key=lambda path: path.as_posix().lower()):
            folder_path = entry.parent
            relative_path = folder_path.relative_to(root).as_posix()
            if max_depth is not None and max_depth >= 0:
                depth = 0 if relative_path == "." else len(folder_path.relative_to(root).parts)
                if depth > max_depth:
                    continue
            folders.append(
                SkillFolder(
                    id_hint=folder_path.name,
                    path=folder_path,
                    entry=entry,
                    relative_path=relative_path,
                )
            )
        return folders

    def snapshot(
        self,
        skills_root: Union[Path, str],
        max_depth: Optional[int] = None,
    ) -> tuple[List[SkillFolder], dict[str, str]]:
        folders = self.scan(skills_root, max_depth=max_depth)
        return folders, {
            folder.relative_path: _file_sha256(folder.entry)
            for folder in folders
        }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
