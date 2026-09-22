#!/usr/bin/env python3
"""Validate and import an AgentGroup using the product's package lifecycle."""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Path to the completed group package")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package
        from jiuwenswarm.common.utils import get_user_workspace_dir
        from jiuwenswarm.server.runtime.extension_package_manager import (
            import_agent_group,
        )

        package = args.package.expanduser().absolute()
        if not (package / "README.md").is_file():
            raise ValueError("Package must contain README.md")
        if package.is_symlink() or any(path.is_symlink() for path in package.rglob("*")):
            raise ValueError("Package must not contain symbolic links")
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
