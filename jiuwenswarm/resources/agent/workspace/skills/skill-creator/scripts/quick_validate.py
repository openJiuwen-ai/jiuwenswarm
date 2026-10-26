# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Quick validation script for skills - minimal version."""

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# Configure logging
logger = logging.getLogger(__name__)


def validate_skill(skill_path: Path | str) -> tuple[bool, str]:
    """Validate a skill directory.

    Args:
        skill_path: Path to the skill directory.

    Returns:
        Tuple of (is_valid, message).
    """
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter
    content = skill_md.read_text()
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    # Extract frontmatter
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    # Parse YAML frontmatter
    try:
        frontmatter: dict[str, Any] = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Define allowed properties
    allowed_properties = {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "compatibility",
    }

    # Check for unexpected properties (excluding nested keys under metadata)
    unexpected_keys = set(frontmatter.keys()) - allowed_properties
    if unexpected_keys:
        return (
            False,
            (
                f"Unexpected key(s) in SKILL.md frontmatter: "
                f"{', '.join(sorted(unexpected_keys))}. "
                f"Allowed properties are: "
                f"{', '.join(sorted(allowed_properties))}"
            ),
        )

    # Check required fields
    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    # Extract name for validation
    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return (
            False,
            f"Name must be a string, got {type(name).__name__}",
        )
    name = name.strip()
    if name:
        # Check naming convention (kebab-case: lowercase with hyphens)
        if not re.match(r"^[a-z0-9-]+$", name):
            return (
                False,
                (
                    f"Name '{name}' should be kebab-case "
                    f"(lowercase letters, digits, and hyphens only)"
                ),
            )
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return (
                False,
                (
                    f"Name '{name}' cannot start/end with hyphen "
                    f"or contain consecutive hyphens"
                ),
            )
        # Check name length (max 64 characters per spec)
        if len(name) > 64:
            return (
                False,
                (
                    f"Name is too long ({len(name)} characters). "
                    f"Maximum is 64 characters."
                ),
            )

    # Extract and validate description
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return (
            False,
            (
                f"Description must be a string, "
                f"got {type(description).__name__}"
            ),
        )
    description = description.strip()
    if description:
        # Check for angle brackets
        if "<" in description or ">" in description:
            return (
                False,
                "Description cannot contain angle brackets (< or >)",
            )
        # Check description length (max 1024 characters per spec)
        if len(description) > 1024:
            return (
                False,
                (
                    f"Description is too long ({len(description)} characters). "
                    f"Maximum is 1024 characters."
                ),
            )

    # Validate compatibility field if present (optional)
    compatibility = frontmatter.get("compatibility", "")
    if compatibility:
        if not isinstance(compatibility, str):
            return (
                False,
                (
                    f"Compatibility must be a string, "
                    f"got {type(compatibility).__name__}"
                ),
            )
        if len(compatibility) > 500:
            return (
                False,
                (
                    f"Compatibility is too long ({len(compatibility)} characters). "
                    f"Maximum is 500 characters."
                ),
            )

    return True, "Skill is valid!"


def main() -> None:
    """Main entry point for CLI usage."""
    if len(sys.argv) != 2:
        logger.info("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    logger.info("%s", message)
    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()
