"""Contract checks for the built-in DeepSeek Harness converter skill."""

from pathlib import Path


SKILL_ROOT = (
    Path(__file__).parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities"
    / "deepseek-harness-plugin-converter"
)


def test_converter_uses_latest_official_sources_and_scoped_source_fetching():
    content = "\n".join(
        [
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/deepseek-harness-plugin.md").read_text(encoding="utf-8"),
        ]
    )

    assert "https://deepseek.com/harness" in content
    assert "https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/" in content
    assert "https://github.com/deepseek-ai/deepseek-harness" in content
    assert "必须使用网络搜索工具" in content
    assert "不能只凭第一次搜索无结果就结束" in content
    assert "直接使用该来源" in content
    assert "普通第三方插件不 clone 完整 DSH 仓库" in content
    assert "缓存" in content
    assert "sparse-checkout" in content


def test_converter_is_self_contained_and_stops_on_required_capability_gaps():
    pages = [
        (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
        *(path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "references").glob("*.md")),
    ]
    content = "\n".join(pages)

    assert "jiuwen-system-plugin-dev" not in content
    assert "缺少必要能力" in content
    assert "立即停止开发" in content
    assert "package.id" in content
    assert "create_extension()" in content
    assert "jiuwenswarm extension install" in content


def test_converter_can_build_and_validate_a_system_plugin_without_another_skill():
    content = "\n".join(
        [
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/conversion-workflow.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/system-plugin-development.md").read_text(encoding="utf-8"),
        ]
    )

    assert "releases/0.2.9.beta3" not in content
    assert "release/v0.1.20.post3" not in content
    assert "https://gitcode.com/openJiuwen/jiuwenswarm/tree/develop" in content
    assert "https://gitcode.com/openJiuwen/agent-core/tree/develop" in content
    assert "https://gitcode.com/openJiuwen/agent-gateway/tree/develop" in content
    assert "jiuwen-system-plugin-dev" not in content
    assert (SKILL_ROOT / "scripts/build_package.py").is_file()
    assert (SKILL_ROOT / "scripts/run_tests.py").is_file()
    assert (SKILL_ROOT / "scripts/finalize_package.py").is_file()
    assert (SKILL_ROOT / "assets/package_example/manifest.json").is_file()
    assert (SKILL_ROOT / "assets/package_example/example_plugin/extension.py").is_file()
