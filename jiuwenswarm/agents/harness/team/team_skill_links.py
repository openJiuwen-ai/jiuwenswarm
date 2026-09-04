# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Utilities for linking team skill directories."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import winerror
except ImportError:  # pragma: no cover - unavailable outside Windows
    ERROR_PRIVILEGE_NOT_HELD = 1314
else:
    ERROR_PRIVILEGE_NOT_HELD = winerror.ERROR_PRIVILEGE_NOT_HELD


def is_valid_skill_dir(path: Path) -> bool:
    """Return whether the path points to a valid skill directory."""
    return path.is_dir() and (path / "SKILL.md").is_file()


def path_exists_or_link(path: Path) -> bool:
    """Return whether a path entry exists, including broken links."""
    return os.path.lexists(path)


def _is_windows_reparse_point(path: Path) -> bool:
    """Return whether a path entry is a Windows reparse point."""
    if sys.platform != "win32":
        return False
    try:
        file_attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_skill_dir_link(path: Path) -> bool:
    """Return whether the path entry is a skill directory link."""
    return path.is_symlink() or _is_windows_reparse_point(path)


def ensure_skill_dir_links(source: Path, target: Path) -> None:
    """Link every valid skill directory from *source* into *target*.

    A valid skill is a sub-directory containing a ``SKILL.md`` file.
    Existing target entries are left untouched.
    """
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    linked = 0
    for skill_dir in source.iterdir():
        if not is_valid_skill_dir(skill_dir):
            continue
        dest = target / skill_dir.name
        if path_exists_or_link(dest):
            continue
        link_skill_dir(skill_dir, dest)
        linked += 1
    if linked:
        logger.info("[TeamSkillLinks] linked %d skills: %s -> %s", linked, source, target)


def prune_skill_dir_links(source: Path, target: Path, selected_skill_names: set[str] | None = None) -> None:
    """Remove stale skill directory links from *target* without touching ordinary directories."""
    if not target.is_dir():
        return

    removed = 0
    for entry in target.iterdir():
        if not _is_skill_dir_link(entry):
            continue
        source_skill_dir = source / entry.name
        if selected_skill_names is not None and entry.name not in selected_skill_names:
            remove_skill_dir_link(entry)
            removed += 1
            continue
        if not is_valid_skill_dir(source_skill_dir):
            remove_skill_dir_link(entry)
            removed += 1
    if removed:
        logger.info("[TeamSkillLinks] pruned %d stale skill links: %s", removed, target)


def sync_skill_dir_links(source: Path, target: Path) -> None:
    """Synchronize valid skill links from *source* into *target*."""
    prune_skill_dir_links(source, target)
    ensure_skill_dir_links(source, target)


def link_skill_dir(source: Path, target: Path) -> None:
    """Create a directory link for a single skill directory."""
    if path_exists_or_link(target):
        return
    try:
        _create_directory_link(source.resolve(), target)
    except Exception:
        if path_exists_or_link(target):
            logger.debug("[TeamSkillLinks] skill dir link already exists after create race: %s", target)
            return
        logger.exception("[TeamSkillLinks] failed to link skill dir: %s -> %s", target, source)
        raise


def remove_skill_dir_link(target: Path) -> None:
    """Remove a skill directory link without deleting ordinary directories."""
    if target.is_symlink():
        target.unlink()
        return
    if _is_windows_reparse_point(target):
        os.rmdir(target)


def _create_directory_link(target_path: Path, link_path: Path) -> None:
    """Create a directory link, falling back to a junction on Windows or a copy on sandboxed runtimes."""
    try:
        os.symlink(str(target_path), str(link_path), target_is_directory=True)
        return
    except OSError as exc:
        errno = getattr(exc, "errno", None)
        is_privilege_error = errno in (13, 1)  # EACCES/EPERM
        if sys.platform == "win32" and getattr(exc, "winerror", None) == ERROR_PRIVILEGE_NOT_HELD:
            _create_windows_junction(target_path, link_path)
            return
        if not is_privilege_error:
            raise
        # Sandboxed runtimes (e.g. HarmonyOS app sandbox) forbid symlink(2)
        # even inside the app's own filesDir. Fall back to a real copy so
        # team skill directories remain usable without kernel privileges.
        logger.info(
            "[TeamSkillLinks] symlink not permitted (%s); copying %s -> %s",
            exc, target_path, link_path,
        )
        _copy_skill_directory(target_path, link_path)


def _copy_skill_directory(target_path: Path, link_path: Path) -> None:
    """Copy a skill directory as a fallback when symlinks are unavailable."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or os.path.lexists(link_path):
        return
    shutil.copytree(
        str(target_path),
        str(link_path),
        symlinks=False,
        copy_function=shutil.copy2,
        dirs_exist_ok=False,
    )


def _create_windows_junction(target_path: Path, link_path: Path) -> None:
    """Create a directory junction using ``mklink /J`` on Windows."""
    cmd_path = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "cmd.exe",
    )
    result = subprocess.run(
        [cmd_path, "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
        encoding="gbk",
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        error_output = result.stderr.strip() or result.stdout.strip()
        raise OSError(f"Failed to create junction {link_path} -> {target_path}: {error_output}")
