"""预置技能清单解析与安装路径推断。"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill.skill_prebuilt import (
    SkillPrebuiltItem,
    parse_agent_skill_prebuilt,
)


def test_install_mode_provider() -> None:
    item = SkillPrebuiltItem(
        id="crm/lead",
        source_id="skillhub",
        version_id="1.0.0",
    )
    assert item.install_mode() == "provider"
    assert item.is_provider_path() is True


def test_install_mode_url() -> None:
    item = SkillPrebuiltItem(
        id="search/weather",
        source="https://artifacts.example.com/skills/weather.zip",
    )
    assert item.install_mode() == "url"
    assert item.package_url.startswith("https://")


def test_install_mode_provider_preferred_over_url() -> None:
    item = SkillPrebuiltItem(
        id="crm/lead",
        source="https://artifacts.example.com/skills/lead.zip",
        source_id="skillhub",
        version_id="1.0.0",
    )
    assert item.install_mode() == "provider"


def test_install_mode_incomplete_is_none() -> None:
    assert SkillPrebuiltItem(id="x", source_id="hub").install_mode() is None
    assert SkillPrebuiltItem(id="x", source="ftp://bad").install_mode() is None


def test_parse_url_and_provider() -> None:
    cfg = parse_agent_skill_prebuilt(
        "agent-1",
        "svc-1",
        [
            {
                "skill_id": "a/url",
                "package_url": "https://example.com/a.zip",
                "enabled": True,
                "data": {"sha256": "a" * 64},
            },
            {
                "skill_id": "b/legacy_ignored",
                "skill_source": "https://example.com/b.zip",
                "enabled": True,
            },
            {
                "skill_id": "c/provider",
                "source_id": "hub",
                "version_id": "9",
                "enabled": True,
            },
            {"skill_id": "d/disabled", "package_url": "https://example.com/d.zip", "enabled": False},
            {"skill_id": "", "package_url": "https://example.com/empty.zip"},
        ],
    )
    # skill_source 已忽略：b 无 package_url / provider 字段，仍进入 skills 但不可安装
    assert [s.id for s in cfg.skills] == ["a/url", "b/legacy_ignored", "c/provider"]
    by_id = {s.id: s for s in cfg.skills}
    assert by_id["a/url"].install_mode() == "url"
    assert by_id["a/url"].sha256 == "a" * 64
    assert by_id["b/legacy_ignored"].install_mode() is None
    assert by_id["c/provider"].install_mode() == "provider"
    assert len(cfg.items_with_source) == 2
