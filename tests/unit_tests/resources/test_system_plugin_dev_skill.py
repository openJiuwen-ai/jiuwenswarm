"""Contract checks for the built-in system plugin development skill."""

from pathlib import Path


SKILL_ROOT = (
    Path(__file__).parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities"
    / "jiuwen-system-plugin-dev"
)


def test_system_plugin_source_defaults_use_develop_for_all_repositories():
    content = "\n".join(
        [
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/source-reading.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/package-workflow.md").read_text(encoding="utf-8"),
        ]
    )

    assert "releases/0.2.9.beta3" not in content
    assert "release/v0.1.20.post3" not in content
    assert "https://gitcode.com/openJiuwen/jiuwenswarm/tree/develop" in content
    assert "https://gitcode.com/openJiuwen/agent-core/tree/develop" in content
    assert "https://gitcode.com/openJiuwen/agent-gateway/tree/develop" in content
    assert content.count("--branch develop") == 3
