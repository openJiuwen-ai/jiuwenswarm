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


def test_converter_reuses_the_builtin_development_skill_and_preserves_gap_handling():
    pages = [
        (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
        *(path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "references").glob("*.md")),
    ]
    content = "\n".join(pages)

    assert "../jiuwen-system-plugin-dev/SKILL.md" in content
    assert "缺少必要能力" in content
    assert "立即停止开发" in content
    assert "CAPABILITY_CHOICES.md" in content
    assert "两个域都需要时" in content


def test_converter_shared_development_resources_are_bundled_together():
    shared = SKILL_ROOT.parent / "jiuwen-system-plugin-dev"
    assert (shared / "SKILL.md").is_file()
    for script in ("build_package.py", "run_tests.py", "finalize_package.py"):
        assert (shared / "scripts" / script).is_file()
    for page in ("example.md", "frontend-development.md", "bundle-development.md"):
        assert (shared / "references" / page).is_file()
    assert (shared / "assets/examples/harness-workbench/manifest.json").is_file()
    assert not (SKILL_ROOT / "scripts").exists()
