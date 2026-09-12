#!/usr/bin/env python3
"""Publish one UUID package as the final Skill using SKILL.md frontmatter name.

The LLM writes package/SKILL.md exactly once. This script never invents or edits
that name: it parses, validates kebab-case, makes folder == name, overwrites any
existing target Skill, then removes this run and the empty runtime root.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
from pathlib import Path

import common

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _read_frontmatter_name(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter")

    match = re.match(
        r"^---\s*\n(.*?)\n---(?:\s*\n|$)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("SKILL.md frontmatter is not closed correctly")

    name_match = re.search(
        r"(?m)^name:\s*['\"]?([^'\"\r\n]+)['\"]?\s*$",
        match.group(1),
    )
    if not name_match:
        raise ValueError("SKILL.md frontmatter has no name")

    return common.validate_skill_name(name_match.group(1).strip())


def _remove_existing_target(target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return

    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish package/SKILL.md using its kebab-case name as the final "
            "folder; existing target is overwritten."
        )
    )
    parser.add_argument("run_id")
    args = parser.parse_args()

    run_dir = common.run_dir(args.run_id).resolve()
    package_dir = common.package_dir(args.run_id).resolve()
    skill_md = package_dir / "SKILL.md"

    if not skill_md.is_file():
        logger.error(
            "[finalize_skill] REFUSED: package/SKILL.md does not exist: %s",
            skill_md,
        )
        return 2

    try:
        final_name = _read_frontmatter_name(skill_md)
    except (OSError, ValueError) as exc:
        logger.error("[finalize_skill] REFUSED: %s", exc)
        return 2

    target = common.skill_dir(final_name).resolve()
    skills_root = common.SKILLS_ROOT.resolve()

    if target.parent != skills_root:
        logger.error(
            "[finalize_skill] REFUSED: target escapes skills root: %s",
            target,
        )
        return 2

    # User-selected policy: existing generated Skill is not versioned or preserved.
    # The two infrastructure directories are protected by validate_skill_name().
    _remove_existing_target(target)
    os.replace(package_dir, target)

    published_name = _read_frontmatter_name(target / "SKILL.md")
    if target.name != published_name:
        logger.error(
            "[finalize_skill] invariant failed after publish: "
            "folder=%r frontmatter.name=%r",
            target.name,
            published_name,
        )
        return 3

    # Only this run is removed. Concurrent UUID workspaces remain untouched.
    if run_dir.exists():
        shutil.rmtree(run_dir)

    runtime_root = common.RUNTIME_ROOT
    if runtime_root.exists():
        try:
            runtime_root.rmdir()  # removes only when empty
        except OSError:
            pass

    logger.info("[finalize_skill] FINAL_NAME: %s", final_name)
    logger.info("[finalize_skill] SKILL_DIR: %s", target)
    logger.info("[finalize_skill] OVERWRITE_POLICY: replace-existing")
    logger.info("[finalize_skill] RUNTIME_CLEANED: true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
