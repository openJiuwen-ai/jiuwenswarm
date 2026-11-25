"""Contract checks for the built-in system plugin development skill."""

from pathlib import Path


SKILL_ROOT = (
    Path(__file__).parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities"
    / "jiuwen-system-plugin-dev"
)


def test_system_plugin_source_defaults_use_release_branches():
    content = "\n".join(
        [
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/source-reading.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/package-workflow.md").read_text(encoding="utf-8"),
        ]
    )

    assert "releases/0.2.9.beta3" not in content
    assert "release/v0.1.20.post3" not in content
    assert "https://gitcode.com/openJiuwen/jiuwenswarm/tree/releases/0.2.10" in content
    assert "https://gitcode.com/openJiuwen/agent-core/tree/release/v0.1.22" in content
    assert "https://gitcode.com/openJiuwen/agent-gateway/tree/release/0.1.1" in content
    for branch in ("releases/0.2.10", "release/v0.1.22", "release/0.1.1"):
        assert content.count(f"--branch {branch}") == 1
        assert content.count(f"checkout {branch}") == 1
    assert "--branch develop" not in content
