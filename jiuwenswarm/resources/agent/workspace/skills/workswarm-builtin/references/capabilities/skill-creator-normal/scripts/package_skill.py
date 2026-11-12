# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Skill Packager - Creates a distributable .skill file of a skill folder.

Usage:
    python utils/package_skill.py <path/to/skill-folder> [output-directory]

Example:
    python utils/package_skill.py skills/public/my-skill
    python utils/package_skill.py skills/public/my-skill ./dist
"""

import fnmatch
import logging
import sys
import zipfile
from pathlib import Path

from scripts.quick_validate import validate_skill

# Configure logging
logger = logging.getLogger(__name__)


# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging.

    Args:
        rel_path: Path relative to skill_path.parent.

    Returns:
        True if path should be excluded, False otherwise.
    """
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


def package_skill(
    skill_path: Path | str,
    output_dir: Path | str | None = None,
) -> Path | None:
    """Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder.
        output_dir: Optional output directory for the .skill file
            (defaults to current directory).

    Returns:
        Path to the created .skill file, or None if error.
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

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"

    # Create the .skill file (zip format)
    try:
        with zipfile.ZipFile(
            skill_filename, "w", zipfile.ZIP_DEFLATED
        ) as zipf:
            # Walk through the skill directory, excluding build artifacts
            for file_path in skill_path.rglob("*"):
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    logger.info("  Skipped: %s", arcname)
                    continue
                zipf.write(file_path, arcname)
                logger.info("  Added: %s", arcname)

        logger.info("Successfully packaged skill to: %s", skill_filename)
        return skill_filename

    except Exception as e:
        logger.error("Error creating .skill file: %s", e)
        return None


def main() -> None:
    """Main entry point for CLI usage."""
    if len(sys.argv) < 2:
        logger.info(
            "Usage: python utils/package_skill.py "
            "<path/to/skill-folder> [output-directory]"
        )
        logger.info("")
        logger.info("Example:")
        logger.info("  python utils/package_skill.py skills/public/my-skill")
        logger.info(
            "  python utils/package_skill.py skills/public/my-skill ./dist"
        )
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
    main()
