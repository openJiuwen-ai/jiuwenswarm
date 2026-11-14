"""Tests for skill_display.md parsing and builtin UI whitelist."""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager, SkillRpcError


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Create an explicit, isolated config file for tests that read settings."""
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: path)
    return path


@pytest.fixture(params=["en", "zh"])
def preferred_language(request, config_file):
    """Set the preferred language in the isolated config file."""
    config_file.write_text(f"preferred_language: {request.param}\n", encoding="utf-8")
    return request.param


def _make_skill_dir(skills_dir, name, *, description="English description", display=None):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    if display is not None:
        lines = ["---"]
        for key, value in display.items():
            lines.append(f"{key}: {value}")
        lines.append("---\n")
        (skill_dir / "skill_display.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir


def _init_manager(monkeypatch, skills_dir, builtin_dir):
    skills_dir.mkdir(parents=True, exist_ok=True)
    builtin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    return SkillManager()


def test_apply_skill_display_from_file_overrides_ui_fields(tmp_path, preferred_language):
    skill_dir = _make_skill_dir(
        tmp_path,
        "demo-skill",
        description="Trigger description",
        display={
            "display_name_zh": "演示技能",
            "description_zh": "中文描述",
            "display_name_en": "Demo Skill",
            "description_en": "English UI description",
        },
    )
    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name_zh"] == "演示技能"
    assert meta["description_zh"] == "中文描述"
    assert meta["display_name_en"] == "Demo Skill"
    assert meta["description_en"] == "English UI description"
    assert meta["display_name"] == ("Demo Skill" if preferred_language == "en" else "演示技能")
    assert meta["description"] == ("English UI description" if preferred_language == "en" else "中文描述")
    assert meta["name"] == "demo-skill"
    assert "description: Trigger description" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_apply_skill_display_fallback_without_file(tmp_path):
    skill_dir = _make_skill_dir(
        tmp_path,
        "fallback-skill",
        description="From SKILL.md",
    )
    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name_zh"] == "fallback-skill"
    assert meta["description_zh"] == "From SKILL.md"
    assert meta["display_name_en"] == ""
    assert meta["description_en"] == ""
    # No skill_display.md → do not overwrite response display_name/description
    assert meta.get("display_name") is None or meta.get("display_name") != "fallback-skill"
    assert meta["description"] == "From SKILL.md"


def test_apply_skill_display_falls_back_to_builtin_source(monkeypatch, tmp_path, preferred_language):
    """Older local installs without skill_display.md should reuse builtin display."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    local = _make_skill_dir(
        skills_dir,
        "skill-creator",
        description="Create new skills in English.",
    )
    _make_skill_dir(
        builtin_dir,
        "skill-creator",
        description="Create new skills in English.",
        display={
            "display_name_zh": "Skill Creator",
            "description_zh": "创建、修改、优化和评估单智能体技能。",
            "display_name_en": "Skill Creator",
            "description_en": "Create new skills in English.",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )

    meta = SkillManager._parse_skill_md(local / "SKILL.md")
    assert meta is not None
    SkillManager._apply_skill_display(meta, local)

    assert meta["description_zh"] == "创建、修改、优化和评估单智能体技能。"
    assert meta["description"] == (
        "Create new skills in English." if preferred_language == "en" else "创建、修改、优化和评估单智能体技能。"
    )
    assert meta["display_name"] == "Skill Creator"


def test_scan_local_skills_includes_display_fields(monkeypatch, tmp_path, preferred_language):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        skills_dir,
        "local-demo",
        description="Trigger",
        display={
            "display_name_zh": "本地演示",
            "description_zh": "本地中文描述",
            "display_name_en": "Local Demo",
            "description_en": "Local English description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    listed = manager._scan_local_skills()
    skill = next(s for s in listed if s["name"] == "local-demo")
    assert skill["display_name_zh"] == "本地演示"
    assert skill["description_zh"] == "本地中文描述"
    assert skill["display_name"] == ("Local Demo" if preferred_language == "en" else "本地演示")
    assert skill["description"] == ("Local English description" if preferred_language == "en" else "本地中文描述")


@pytest.mark.asyncio
async def test_skills_get_returns_display_fields(monkeypatch, tmp_path, preferred_language):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        skills_dir,
        "detail-demo",
        description="Trigger",
        display={
            "display_name_zh": "详情演示",
            "description_zh": "详情中文描述",
            "display_name_en": "Detail Demo",
            "description_en": "Detail English description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    detail = await manager.handle_skills_get({"name": "detail-demo"})
    assert detail["display_name_zh"] == "详情演示"
    assert detail["description_zh"] == "详情中文描述"
    assert detail["display_name"] == ("Detail Demo" if preferred_language == "en" else "详情演示")
    assert detail["description"] == ("Detail English description" if preferred_language == "en" else "详情中文描述")
    assert detail["content"].strip() == "# detail-demo"


@pytest.mark.asyncio
async def test_skills_get_uninstalled_builtin_whitelist(monkeypatch, tmp_path, preferred_language):
    """未安装且在白名单内的内置技能可通过 skills.get 返回."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        builtin_dir,
        "gitcode-api",
        description="GitCode CLI trigger description",
        display={
            "display_name_zh": "GitCode API",
            "description_zh": "中文描述",
            "display_name_en": "GitCode API",
            "description_en": "English UI description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    detail = await manager.handle_skills_get({"name": "gitcode-api"})
    assert detail["name"] == "gitcode-api"
    assert detail["source"] == "builtin"
    assert detail["is_builtin"] is True
    assert detail["is_builtin_source"] is True
    assert detail["installed"] is False
    assert detail["display_name_zh"] == "GitCode API"
    assert detail["description_zh"] == "中文描述"
    assert detail["display_name"] == "GitCode API"
    assert detail["description"] == ("English UI description" if preferred_language == "en" else "中文描述")


@pytest.mark.asyncio
async def test_skills_get_rejects_uninstalled_builtin_outside_whitelist(
    monkeypatch, tmp_path
):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(builtin_dir, "hidden-builtin", description="Should not be gettable")
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    with pytest.raises(SkillRpcError, match="未找到 skill"):
        await manager.handle_skills_get({"name": "hidden-builtin"})


def test_builtin_scan_whitelist_filters_non_listed(monkeypatch, tmp_path, config_file):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(builtin_dir, "skill-creator", description="Creator")
    _make_skill_dir(builtin_dir, "hidden-builtin", description="Should be hidden")

    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)
    names = {s["name"] for s in manager._scan_builtin_skills()}

    assert "skill-creator" in names
    assert "hidden-builtin" not in names


def test_scan_builtin_skills_includes_display_fields(monkeypatch, tmp_path, preferred_language):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(
        builtin_dir,
        "skill-creator",
        description="Trigger",
        display={
            "display_name_zh": "内置演示",
            "description_zh": "内置中文描述",
            "display_name_en": "Builtin Demo",
            "description_en": "Builtin English description",
        },
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    listed = manager._scan_builtin_skills()
    skill = next(s for s in listed if s["name"] == "skill-creator")
    assert skill["display_name_zh"] == "内置演示"
    assert skill["description_zh"] == "内置中文描述"
    assert skill["display_name"] == ("Builtin Demo" if preferred_language == "en" else "内置演示")
    assert skill["description"] == ("Builtin English description" if preferred_language == "en" else "内置中文描述")


def test_try_find_skill_file_skips_skill_display(tmp_path):
    skill_dir = tmp_path / "only-display"
    skill_dir.mkdir()
    (skill_dir / "skill_display.md").write_text(
        "---\ndisplay_name_zh: X\n---\n",
        encoding="utf-8",
    )
    assert SkillManager._try_find_skill_file(skill_dir) is None

    (skill_dir / "SKILL.md").write_text(
        "---\nname: only-display\ndescription: d\n---\n",
        encoding="utf-8",
    )
    assert SkillManager._try_find_skill_file(skill_dir).name == "SKILL.md"


@pytest.mark.parametrize("missing_field", ["display_name", "description", "both"])
def test_selected_language_missing_fields_preserve_original_metadata(
    tmp_path, preferred_language, missing_field,
):
    other_language = "zh" if preferred_language == "en" else "en"
    display = {
        f"display_name_{other_language}": "Other language name",
        f"description_{other_language}": "Other language description",
    }
    if missing_field == "description":
        display[f"display_name_{preferred_language}"] = "Selected name"
    if missing_field == "display_name":
        display[f"description_{preferred_language}"] = "Selected description"
    skill_dir = _make_skill_dir(tmp_path, "partial-display", display=display)
    meta = {"name": "partial-display", "display_name": "Market Name", "description": "Original description"}

    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name"] == ("Selected name" if missing_field == "description" else "Market Name")
    assert meta["description"] == ("Selected description" if missing_field == "display_name" else "Original description")


@pytest.mark.parametrize(
    ("configured_language", "expected_language"),
    [(None, "zh"), ("", "zh"), ("unsupported", "zh"), (" EN-us ", "en"), ("en", "en"), ("zh-CN", "zh"), ("cn", "zh")],
)
def test_skill_display_config_language_normalization(tmp_path, config_file, configured_language, expected_language):
    content = "{}\n" if configured_language is None else f'preferred_language: "{configured_language}"\n'
    config_file.write_text(content, encoding="utf-8")
    skill_dir = _make_skill_dir(tmp_path, "normalized", display={
        "display_name_zh": "中文名称", "description_zh": "中文简介",
        "display_name_en": "English name", "description_en": "English description",
    })
    meta = {"name": "normalized", "description": "Trigger"}

    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name"] == ("English name" if expected_language == "en" else "中文名称")
    assert meta["description"] == ("English description" if expected_language == "en" else "中文简介")


def test_skill_display_unreadable_config_defaults_to_chinese(monkeypatch, tmp_path):
    def unreadable_config():
        raise OSError("config unavailable")

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", unreadable_config)
    skill_dir = _make_skill_dir(tmp_path, "unreadable", display={
        "display_name_zh": "中文名称", "description_zh": "中文简介",
        "display_name_en": "English name", "description_en": "English description",
    })
    meta = {"name": "unreadable", "description": "Trigger"}

    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name"] == "中文名称"
    assert meta["description"] == "中文简介"


@pytest.mark.asyncio
async def test_skills_list_and_detail_follow_config_changes_without_recreating_manager(
    monkeypatch, tmp_path, config_file,
):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    skill_dir = _make_skill_dir(skills_dir, "subtraction", display={
        "display_name_zh": "减法助手", "description_zh": "精确完成减法",
        "display_name_en": "Subtraction Helper", "description_en": "Subtract exactly",
    })
    originals = {name: (skill_dir / name).read_bytes() for name in ("SKILL.md", "skill_display.md")}
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    for language, name, description in (
        ("en", "Subtraction Helper", "Subtract exactly"),
        ("zh", "减法助手", "精确完成减法"),
        ("en", "Subtraction Helper", "Subtract exactly"),
    ):
        next_stamp = config_file.stat().st_mtime_ns + 1_000_000_000
        config_file.write_text(f"preferred_language: {language}\n", encoding="utf-8")
        # Make separate config edits deterministic on filesystems with coarse timestamps.
        os.utime(config_file, ns=(next_stamp, next_stamp))
        listed = await manager.handle_skills_list({})
        skill = next(item for item in listed["skills"] if item["name"] == "subtraction")
        detail = await manager.handle_skills_get({"name": "subtraction"})
        for result in (skill, detail):
            assert result["name"] == "subtraction"
            assert result["display_name"] == name
            assert result["description"] == description
            assert result["display_name_zh"] == "减法助手"
            assert result["display_name_en"] == "Subtraction Helper"
        assert detail["content"].strip() == "# subtraction"

    for filename, original in originals.items():
        assert (skill_dir / filename).read_bytes() == original


def test_marketplace_scan_uses_configured_display_language(monkeypatch, tmp_path, preferred_language):
    manager = _init_manager(monkeypatch, tmp_path / "skills", tmp_path / "builtin")
    manager._state["marketplaces"] = [{
        "name": "demo-market", "url": "https://example.invalid/demo.git", "enabled": True,
    }]
    _make_skill_dir(manager._marketplace_dir / "demo-market" / "skills", "market-demo", display={
        "display_name_zh": "市场演示", "description_zh": "市场中文简介",
        "display_name_en": "Market Demo", "description_en": "Market English description",
    })

    listed = manager._scan_marketplace_skills()

    skill = next(item for item in listed if item["name"] == "market-demo")
    assert skill["display_name"] == ("Market Demo" if preferred_language == "en" else "市场演示")
    assert skill["description"] == ("Market English description" if preferred_language == "en" else "市场中文简介")


@pytest.mark.parametrize("failure", ["missing", "invalid_yaml", "not_mapping"])
def test_skill_display_invalid_config_defaults_to_chinese(tmp_path, config_file, failure):
    if failure == "missing":
        config_file.unlink()
    else:
        config_file.write_text("[" if failure == "invalid_yaml" else "- invalid\n", encoding="utf-8")
    skill_dir = _make_skill_dir(tmp_path, "invalid-config", display={
        "display_name_zh": "中文名称", "description_zh": "中文简介",
        "display_name_en": "English name", "description_en": "English description",
    })
    meta = {"name": "invalid-config", "description": "Trigger"}

    SkillManager._apply_skill_display(meta, skill_dir)

    assert meta["display_name"] == "中文名称"
    assert meta["description"] == "中文简介"


def _make_display_scan_manager(monkeypatch, tmp_path):
    """Build several real skills across every supported scan source."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    display = {
        "display_name_zh": "中文名称", "description_zh": "中文简介",
        "display_name_en": "English name", "description_en": "English description",
    }
    for name in ("local-one", "local-two"):
        _make_skill_dir(skills_dir, name, display=display)
    for name in ("skill-creator", "gitcode-api"):
        _make_skill_dir(builtin_dir, name, display=display)
    mcp_dir = tmp_path / "mcp"
    for name in ("mcp-one", "mcp-two"):
        _make_skill_dir(mcp_dir, name, display=display)
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)
    monkeypatch.setattr(manager, "_mcp_skills_dirs", lambda: [{"dir": str(mcp_dir)}])
    manager._state["marketplaces"] = [{
        "name": "demo-market", "url": "https://example.invalid/demo.git", "enabled": True,
    }]
    for name in ("market-one", "market-two"):
        _make_skill_dir(manager._marketplace_dir / "demo-market" / "skills", name, display=display)
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("enterprise", [False, True])
async def test_skills_list_reads_language_once_per_request(
    monkeypatch, tmp_path, config_file, enterprise, mocker,
):
    from jiuwenswarm.common import config

    manager = _make_display_scan_manager(monkeypatch, tmp_path)
    monkeypatch.setattr("jiuwenswarm.server.runtime.skill.skill_manager.is_enterprise", lambda: enterprise)
    read_config = mocker.spy(config, "get_config")
    expected_names = {"local-one", "local-two", "mcp-one", "mcp-two"}
    if not enterprise:
        expected_names.update({"skill-creator", "gitcode-api", "market-one", "market-two"})

    for language, name, description in (
        ("en", "English name", "English description"),
        ("zh", "中文名称", "中文简介"),
        ("en", "English name", "English description"),
    ):
        next_stamp = config_file.stat().st_mtime_ns + 1_000_000_000
        config_file.write_text(f"preferred_language: {language}\n", encoding="utf-8")
        os.utime(config_file, ns=(next_stamp, next_stamp))
        read_config.reset_mock()

        response = await manager.handle_skills_list({})

        assert read_config.call_count == 1
        assert {skill["name"] for skill in response["skills"]} == expected_names
        for skill in response["skills"]:
            assert skill["display_name"] == name
            assert skill["description"] == description


@pytest.mark.parametrize("source", ["local", "builtin", "marketplace"])
def test_standalone_scans_read_language_once(
    monkeypatch, tmp_path, preferred_language, source, mocker,
):
    from jiuwenswarm.common import config

    manager = _make_display_scan_manager(monkeypatch, tmp_path)
    read_config = mocker.spy(config, "get_config")
    scans = {
        "local": manager._scan_local_skills,
        "builtin": manager._scan_builtin_skills,
        "marketplace": manager._scan_marketplace_skills,
    }

    skills = scans[source]()

    assert len(skills) == (4 if source == "local" else 2)
    assert read_config.call_count == 1
    for skill in skills:
        assert skill["display_name"] == ("English name" if preferred_language == "en" else "中文名称")
        assert skill["description"] == ("English description" if preferred_language == "en" else "中文简介")
