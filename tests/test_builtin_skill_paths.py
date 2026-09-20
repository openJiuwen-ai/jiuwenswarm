"""Static checks for paths embedded in bundled skill instructions."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SKILLS_ROOT = (
    PROJECT_ROOT / "jiuwenswarm" / "resources" / "agent" / "workspace" / "skills"
)

LEGACY_SKILLS_ROOT = re.compile(
    r"(?:~[/\\]|%USERPROFILE%[/\\])\.jiuwenswarm[/\\]agent[/\\]skills[/\\]",
    re.IGNORECASE,
)
CURRENT_SKILL_PATH = re.compile(
    r"(?:~[/\\]|%USERPROFILE%[/\\])\.jiuwenswarm[/\\]agent[/\\]workspace[/\\]skills[/\\]([^/\\\s`'\"]+)",
    re.IGNORECASE,
)


def test_builtin_skill_documents_use_current_skill_root():
    """Bundled skill paths must use the current root and their own directory."""
    skill_dirs = sorted(path for path in BUILTIN_SKILLS_ROOT.iterdir() if path.is_dir())
    assert skill_dirs, f"no bundled skills found under {BUILTIN_SKILLS_ROOT}"

    violations = []
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        assert skill_file.is_file(), f"missing SKILL.md: {skill_dir}"

        for line_number, line in enumerate(
            skill_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if LEGACY_SKILLS_ROOT.search(line):
                violations.append(f"{skill_file}:{line_number}: legacy skills root")

            for match in CURRENT_SKILL_PATH.finditer(line):
                referenced_name = match.group(1).rstrip(".,;:)]}")
                if referenced_name not in {skill_dir.name, "<skill-name>", "<name>"}:
                    violations.append(
                        f"{skill_file}:{line_number}: references {referenced_name!r}"
                    )

    assert not violations, "invalid bundled skill paths:\n" + "\n".join(violations)
