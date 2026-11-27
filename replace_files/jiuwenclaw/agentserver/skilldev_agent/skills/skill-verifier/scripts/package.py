#!/usr/bin/env python3
"""Package a skill under <workspace>/skill/ to <workspace>/output/.

Merges functionality from skill-creator/package_skill.py (dependency-reference
copying) and skill-standardizer/package.py (workspace-based CLI).

Does NOT run validation — the gate script handles ordering.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shutil
import sys
import zipfile
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".trash"}
EXCLUDE_GLOBS = {"*.pyc", "*.swp", "*.bak-*"}
EXCLUDE_FILES = {".DS_Store"}
ROOT_EXCLUDE_DIRS = {"evals", "output"}


def find_skill_root(skill_dir: Path) -> Path | None:
    """Locate the skill root directory containing SKILL.md under skill/."""
    if not skill_dir.is_dir():
        return None
    if (skill_dir / "SKILL.md").is_file():
        return skill_dir

    subdirs = [
        child
        for child in skill_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    ]
    if len(subdirs) == 1:
        return subdirs[0]
    if len(subdirs) > 1:
        names = ", ".join(sorted(d.name for d in subdirs))
        logger.warning("multiple skill roots under %s: %s", skill_dir, names)
        return None

    for skill_md in skill_dir.rglob("SKILL.md"):
        return skill_md.parent
    return None


def _should_exclude(rel_path: Path) -> bool:
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def _workspace_for_skill(skill_path: Path) -> Path:
    if skill_path.parent.name == "skill":
        return skill_path.parent.parent
    return skill_path.parent


def _load_frontmatter(skill_path: Path) -> dict:
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = yaml.safe_load(match.group(1))
    return fm if isinstance(fm, dict) else {}


def _has_dependency(metadata: dict, keys: tuple[str, ...]) -> bool:
    return any(metadata.get(k) for k in keys)


def _collect_tool_sources(
    metadata: dict, workspace_path: Path
) -> tuple[list[tuple[Path, Path]], list[str]]:
    ref = Path("references")
    pairs: list[tuple[Path, Path]] = []
    errors: list[str] = []

    tools = metadata.get("tools") or []
    if tools:
        if not isinstance(tools, list):
            errors.append("metadata.tools must be a list")
        else:
            for i, tool in enumerate(tools, start=1):
                if not isinstance(tool, dict):
                    errors.append(f"metadata.tools[{i}] must be a mapping")
                    continue
                bundle = str(
                    tool.get("bundleName") or tool.get("bundle_name") or ""
                ).strip()
                tname = str(
                    tool.get("toolName") or tool.get("tool_name") or ""
                ).strip()
                if not bundle or not tname:
                    errors.append(
                        f"metadata.tools[{i}] must include bundleName and toolName"
                    )
                    continue
                fname = f"{bundle}__{tname}.json"
                pairs.append((
                    workspace_path / "resources" / "available-tools" / fname,
                    ref / "tools" / fname,
                ))

    if _has_dependency(metadata, ("agents", "agent_tools", "agentTools")):
        pairs.append((
            workspace_path / "resources" / "agents" / "available_agents.json",
            ref / "agents" / "available_agents.json",
        ))

    if _has_dependency(metadata, ("clis", "cli_tools", "cliTools")):
        pairs.append((
            workspace_path / "resources" / "clis" / "available_clis.json",
            ref / "clis" / "available_clis.json",
        ))

    return pairs, errors


def copy_dependency_references(skill_path: Path) -> bool:
    """Copy declared external dependency JSON files into the skill before zipping."""
    metadata = _load_frontmatter(skill_path).get("metadata") or {}
    if not isinstance(metadata, dict):
        logger.error("metadata in SKILL.md frontmatter must be a mapping")
        return False

    workspace_path = _workspace_for_skill(skill_path)
    pairs, errors = _collect_tool_sources(metadata, workspace_path)
    if errors:
        for e in errors:
            logger.error(e)
        return False
    if not pairs:
        return True

    truly_missing: list[Path] = []
    for source, rel_dest in pairs:
        dest = skill_path / rel_dest
        if not source.exists():
            if dest.exists():
                logger.info(
                    "Source not in workspace but already in skill: %s", rel_dest
                )
            else:
                truly_missing.append(rel_dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        logger.info("Copied dependency reference: %s", dest.relative_to(skill_path))

    if truly_missing:
        for src in truly_missing:
            logger.error("Dependency reference not found: %s", src)
        return False

    return True

def clean_empty_dirs(skill_path: Path) -> None:
    """Remove empty optional directories (scripts/, assets/, references/) from skill folder."""
    optional_dirs = ["scripts", "assets", "references"]
    for dir_name in optional_dirs:
        dir_path = skill_path / dir_name
        if dir_path.exists() and dir_path.is_dir():
            _remove_empty_recursive(dir_path, skill_path)


def _remove_empty_recursive(directory: Path, skill_path: Path) -> None:
    """Recursively remove empty directories from bottom up."""
    try:
        for child in directory.iterdir():
            if not child.is_symlink() and child.is_dir():
                _remove_empty_recursive(child, skill_path)
    except OSError:
        return  # skip if we can't even list the directory

    try:
        if not any(directory.iterdir()):
            directory.rmdir()
            logger.info(
                "Removed empty directory: %s",
                directory.relative_to(skill_path)
            )
    except OSError:
        pass  # non-critical cleanup, skip on failure

def package_skill(skill_root: Path, output_dir: Path) -> Path | None:
    """Package a skill directory into a zip under *output_dir*."""
    skill_root = skill_root.resolve()
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        logger.error("package failed: SKILL.md missing in %s", skill_root)
        return None

    logger.info("Copying declared dependency references...")
    if not copy_dependency_references(skill_root):
        logger.error("Please fix dependency reference errors before packaging.")
        return None

    logger.info("Cleaning empty optional directories...")
    try:
        clean_empty_dirs(skill_root)
    except OSError:
        logger.warning("Failed to clean empty directories, continuing...")

    output_dir.mkdir(parents=True, exist_ok=True)
    for existing in output_dir.iterdir():
        if existing.is_file():
            existing.unlink()
            logger.info("removed stale output file: %s", existing.name)

    skill_name = skill_root.name
    skill_filename = output_dir / f"{skill_name}.zip"

    files_to_package: list[tuple[Path, Path]] = []
    for file_path in skill_root.rglob("*"):
        if not file_path.is_file():
            continue
        arcname = Path(skill_name) / file_path.relative_to(skill_root)
        if _should_exclude(arcname):
            continue
        files_to_package.append((file_path, arcname))

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path, arcname in files_to_package:
                zipf.write(file_path, arcname)
        logger.info("packaged skill to %s", skill_filename)
        return skill_filename
    except Exception as exc:
        logger.exception("package failed: %s", exc)
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 -m scripts.package <workspace>")
        return 2

    workspace = Path(argv[1]).resolve()
    skill_root = find_skill_root(workspace / "skill")
    if skill_root is None:
        print("Package failed: cannot find skill root under <workspace>/skill/")
        return 1

    output_dir = workspace / "output"
    packaged = package_skill(skill_root, output_dir)
    if packaged is None:
        print("Package failed.")
        return 1

    print(f"Packaged: {packaged}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main(sys.argv))
