#!/usr/bin/env python3
"""Validate and import an AgentGroup using the product's package lifecycle."""

import argparse
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def validate_package_content(package: Path) -> None:
    """Reject unfinished package content and malformed skill metadata."""
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package)
        if any(part in {".git", "__pycache__", ".state"} for part in relative.parts):
            continue
        if path.suffix not in {".md", ".py", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "[TODO" in text:
            raise ValueError(f"{relative}: unresolved [TODO] placeholder")
        if path.name != "SKILL.md":
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"{relative}: missing YAML frontmatter")
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError(f"{relative}: unclosed YAML frontmatter") from exc
        try:
            metadata = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise ValueError(f"{relative}: invalid YAML frontmatter") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"{relative}: frontmatter must be a mapping")
        if metadata.get("name") != path.parent.name:
            raise ValueError(f"{relative}: frontmatter name must equal directory name {path.parent.name!r}")
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{relative}: description must be a non-empty string")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Path to the completed group package")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        from openjiuwen.agent_teams.experts import load_agent_group_package
        from jiuwenswarm.common.utils import get_user_workspace_dir
        from jiuwenswarm.server.runtime.extension_package_manager import (
            import_agent_group,
        )

        package = args.package.expanduser().absolute()
        if not (package / "README.md").is_file():
            raise ValueError("Package must contain README.md")
        if package.is_symlink() or any(path.is_symlink() for path in package.rglob("*")):
            raise ValueError("Package must not contain symbolic links")
        validate_package_content(package)
        templates = load_agent_group_package(package)
        logger.info("RESULT: PASS (%d agents)", len(templates))
        if not args.validate_only:
            result = import_agent_group({"path": str(package)})
            logger.info("REGISTERED: %s", result["id"])
            logger.info("INSTALLED: %s", result["id"])
            destination = get_user_workspace_dir() / ".agent_teams" / "agent_groups" / "local" / result["id"]
            logger.info("PACKAGE: %s", destination)
        return 0
    except (ImportError, OSError, TypeError, ValueError) as exc:
        logger.error("ERROR: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
