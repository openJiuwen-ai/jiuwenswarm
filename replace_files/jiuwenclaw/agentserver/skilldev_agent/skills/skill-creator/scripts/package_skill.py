#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .zip file of a skill folder

Usage:
    python utils/package_skill.py <path/to/skill-folder> [output-directory]

Example:
    python utils/package_skill.py ./skill/my-skill
    python utils/package_skill.py ./skill/my-skill ./output
"""

import fnmatch
import logging
import re
import shutil
import sys
import zipfile
from pathlib import Path

import yaml

from scripts.quick_validate import validate_skill


logger = logging.getLogger(__name__)

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".trash"}
EXCLUDE_GLOBS = {"*.pyc", "*.swp"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals", "output"}


def load_frontmatter(skill_path: Path) -> dict:
    """Load SKILL.md YAML frontmatter."""
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    frontmatter = yaml.safe_load(match.group(1))
    return frontmatter if isinstance(frontmatter, dict) else {}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path is relative to skill_path.parent, so parts[0] is the skill
    # folder name and parts[1] (if present) is the first subdir.
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def workspace_for_skill(skill_path: Path) -> Path:
    """Return <workspace> for <workspace>/skill/<skill-name>, else use parent."""
    if skill_path.parent.name == "skill":
        return skill_path.parent.parent
    return skill_path.parent


def has_dependency(metadata: dict, keys: tuple[str, ...]) -> bool:
    """Return whether any metadata key declares a non-empty dependency list."""
    for key in keys:
        value = metadata.get(key)
        if value:
            return True
    return False


def collect_tool_sources(metadata: dict, workspace_path: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Build source/destination pairs for declared external dependencies."""
    reference_path = Path("references")
    source_pairs = []
    errors = []

    tools = metadata.get("tools") or []
    if tools:
        if not isinstance(tools, list):
            errors.append("metadata.tools must be a list")
        else:
            for index, tool in enumerate(tools, start=1):
                if not isinstance(tool, dict):
                    errors.append(f"metadata.tools[{index}] must be a mapping")
                    continue
                bundle_name = str(tool.get("bundleName") or tool.get("bundle_name") or "").strip()
                tool_name = str(tool.get("toolName") or tool.get("tool_name") or "").strip()
                if not bundle_name or not tool_name:
                    errors.append(
                        f"metadata.tools[{index}] must include bundleName and toolName"
                    )
                    continue
                filename = f"{bundle_name}__{tool_name}.json"
                source_pairs.append((
                    workspace_path / "resources" / "available-tools" / filename,
                    reference_path / "tools" / filename,
                ))

    if has_dependency(metadata, ("agents", "agent_tools", "agentTools")):
        source_pairs.append((
            workspace_path / "resources" / "agents" / "available_agents.json",
            reference_path / "agents" / "available_agents.json",
        ))

    if has_dependency(metadata, ("clis", "cli_tools", "cliTools")):
        source_pairs.append((
            workspace_path / "resources" / "clis" / "available_clis.json",
            reference_path / "clis" / "available_clis.json",
        ))

    return source_pairs, errors


def copy_dependency_references(skill_path: Path) -> bool:
    """Copy declared external dependency JSON files into the skill before zipping."""
    metadata = load_frontmatter(skill_path).get("metadata") or {}
    if not isinstance(metadata, dict):
        logger.error("metadata in SKILL.md frontmatter must be a mapping")
        return False

    workspace_path = workspace_for_skill(skill_path)
    source_pairs, errors = collect_tool_sources(metadata, workspace_path)
    if errors:
        for error in errors:
            logger.error(error)
        return False
    if not source_pairs:
        return True

    missing = [source for source, _ in source_pairs if not source.exists()]
    if missing:
        for source in missing:
            logger.error("Dependency reference not found: %s", source)
        return False

    for source, rel_dest in source_pairs:
        dest = skill_path / rel_dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        logger.info("Copied dependency reference: %s", dest.relative_to(skill_path))

    return True


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .zip file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .zip file
            (defaults to <workspace>/output when skill_path is <workspace>/skill/<name>,
            otherwise falls back to <current working directory>/output)

    Returns:
        Path to the created .zip file, or None if error
    """
    skill_path = Path(skill_path).resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        logger.error("Skill folder not found: %s", skill_path)
        return None

    if not skill_path.is_dir():
        logger.error("Path is not a directory: %s", skill_path)
        return None

    # Validate SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        logger.error("SKILL.md not found in %s", skill_path)
        return None

    # Run validation before packaging
    logger.info("Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        logger.error("Validation failed: %s", message)
        logger.error("Please fix the validation errors before packaging.")
        return None
    logger.info("%s", message)

    logger.info("Copying declared dependency references...")
    if not copy_dependency_references(skill_path):
        logger.error("Please fix dependency reference errors before packaging.")
        return None

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
    else:
        if skill_path.parent.name == "skill":
            output_path = (skill_path.parent.parent / "output").resolve()
        else:
            output_path = (Path.cwd() / "output").resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    skill_filename = output_path / f"{skill_name}.zip"

    files_to_package = []
    for file_path in skill_path.rglob('*'):
        if not file_path.is_file():
            continue
        # Zip layout must be:
        #   <skill-name>.zip
        #     └── <skill-name>/
        #         ├── SKILL.md
        #         └── ...
        arcname = Path(skill_name) / file_path.relative_to(skill_path)
        if should_exclude(arcname):
            logger.info("Skipped: %s", arcname)
            continue
        files_to_package.append((file_path, arcname))

    # Create the .zip file (zip format)
    try:
        with zipfile.ZipFile(skill_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path, arcname in files_to_package:
                zipf.write(file_path, arcname)
                logger.info("Added: %s", arcname)

        logger.info("Successfully packaged skill to: %s", skill_filename)
        return skill_filename

    except Exception as e:
        logger.exception("Error creating .zip file: %s", e)
        return None


def main():
    if len(sys.argv) < 2:
        logger.error("Usage: python utils/package_skill.py <path/to/skill-folder> [output-directory]")
        logger.error("Example:")
        logger.error("  python utils/package_skill.py ./skill/my-skill")
        logger.error("  python utils/package_skill.py ./skill/my-skill ./output")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    logger.info("Packaging skill: %s", skill_path)
    if output_dir:
        logger.info("Output directory: %s", output_dir)

    result = package_skill(skill_path, output_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
