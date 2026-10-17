from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import patch

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
from jiuwenswarm.agents.harness.common.recommendation.situation_report import (
    _format_skills_for_llm,
)


def test_agent_default_search_never_queries_skillnet(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)
    queried: list[str] = []

    monkeypatch.setattr(toolkit, "_search_builtin_skills", lambda *_args: [])

    async def _search_clawhub(_params):
        queried.append("clawhub")
        return {"success": True, "skills": []}

    async def _search_team_skills_hub(_params):
        queried.append("teamskillshub")
        return {"success": True, "skills": []}

    async def _unexpected_skillnet_search(_params):
        raise AssertionError("agent must not query SkillNet")

    monkeypatch.setattr(manager, "handle_skills_clawhub_search", _search_clawhub)
    monkeypatch.setattr(manager, "handle_skills_team_skills_hub_search", _search_team_skills_hub)
    monkeypatch.setattr(manager, "handle_skills_skillnet_search", _unexpected_skillnet_search)

    result = asyncio.run(toolkit.search_skill("pdf"))

    assert result["success"] is True
    assert result["source"] == "auto"
    assert queried == ["clawhub", "teamskillshub"]


class _SearchManager:
    def __init__(self, skills):
        self.skills = skills
        self.params = None

    def get_installed_plugins(self):
        return []

    async def handle_skills_clawhub_search(self, params):
        self.params = params
        return {"success": True, "skills": self.skills}


def test_uninstall_skill_removes_local_skill_without_plugin_record(
    tmp_path, allow_macos_pytest_temp_sources
):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: local-only-skill\ndescription: local only\n---\nbody\n",
        encoding="utf-8",
    )

    imported = asyncio.run(manager.handle_skills_import_local({"path": str(source)}))
    assert imported["success"] is True
    assert manager.get_installed_plugins() == []
    assert (tmp_path / "workspace" / "skills" / "local-only-skill").is_dir()

    result = asyncio.run(toolkit.uninstall_skill("local-only-skill"))

    assert result["success"] is True
    assert result["removed"] is True
    assert not (tmp_path / "workspace" / "skills" / "local-only-skill").exists()
    assert manager.get_local_skills() == []


def test_uninstall_skill_matches_display_name_case_insensitively(tmp_path, monkeypatch):
    """UI/Agent 可能传入 Weather，内部规范名是 weather，应能成功卸载。"""
    import io
    import zipfile

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "weather/SKILL.md",
            "---\nname: weather\ndescription: Get weather\nversion: 1.0.0\n---\nbody\n",
        )
    zip_content = buf.getvalue()

    class _Resp:
        status_code = 200
        content = zip_content

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, params, headers):
            return _Resp()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: _Client(),
    )
    asyncio.run(manager.handle_skills_clawhub_set_token({"token": "t"}))
    installed = asyncio.run(
        manager.handle_skills_clawhub_download(
            {"slug": "weather", "owner_handle": "steipete", "display_name": "Weather"}
        )
    )
    assert installed["success"] is True
    assert installed["skill"]["name"] == "weather"
    assert installed["skill"]["display_name"] == "Weather"

    # 用展示名大小写卸载
    result = asyncio.run(toolkit.uninstall_skill("Weather"))
    assert result["success"] is True
    assert result["removed"] is True
    assert result["name"] == "weather"
    assert not (tmp_path / "workspace" / "skills" / "weather").exists()


def test_find_installed_by_target_clawhub_owner_slug_interops_with_plain_slug(tmp_path):
    """新 origin=clawhub:owner/slug 时，纯 slug / 旧 origin 都应判已安装。"""
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)
    manager._add_local_skill(
        {
            "name": "weather",
            "display_name": "Weather",
            "origin": "clawhub:steipete/weather",
            "source": "clawhub",
        }
    )

    by_owner = toolkit._find_installed_by_target("steipete/weather", "clawhub")
    by_slug = toolkit._find_installed_by_target("weather", "clawhub")
    assert by_owner is not None and by_owner["name"] == "weather"
    assert by_slug is not None and by_slug["name"] == "weather"

    # 旧版 origin 也应被 owner/slug 与纯 slug 命中
    manager._add_local_skill(
        {
            "name": "legacy-skill",
            "origin": "clawhub:legacy-skill",
            "source": "clawhub",
        }
    )
    assert toolkit._find_installed_by_target("alice/legacy-skill", "clawhub") is not None
    assert toolkit._find_installed_by_target("legacy-skill", "clawhub") is not None


def test_install_skill_clawhub_already_installed_when_origin_has_owner(tmp_path, monkeypatch):
    """Agent 用纯 slug 再装时，应对已有 clawhub:owner/slug 返回 already_installed。"""
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)
    manager._add_local_skill(
        {
            "name": "weather",
            "display_name": "Weather",
            "origin": "clawhub:steipete/weather",
            "source": "clawhub",
        }
    )
    (tmp_path / "workspace" / "skills" / "weather").mkdir(parents=True)

    async def _should_not_download(_params):
        raise AssertionError("should skip download when already installed")

    monkeypatch.setattr(manager, "handle_skills_clawhub_download", _should_not_download)

    result = asyncio.run(toolkit.install_skill("weather", source="clawhub"))
    assert result["success"] is True
    assert result["already_installed"] is True
    assert result["name"] == "weather"


def test_search_builtin_skills_matches_name_and_description(tmp_path):
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()
    user_skills_dir = tmp_path / "user_skills"
    user_skills_dir.mkdir()

    skill_a = builtin_dir / "deep-research"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: openJiuwen-DeepSearch\ndescription: deep search and research report\n---\nbody\n",
        encoding="utf-8",
    )

    skill_b = builtin_dir / "ppt-helper"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text(
        "---\nname: ppt-helper\ndescription: generate PPT slides\n---\nbody\n",
        encoding="utf-8",
    )

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    with patch(
        "jiuwenswarm.common.utils.get_builtin_skills_dir",
        return_value=builtin_dir,
    ), patch(
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        return_value=user_skills_dir,
    ):
        results = toolkit._search_builtin_skills("deep", set(), 10)

    assert len(results) == 1
    assert results[0]["name"] == "openJiuwen-DeepSearch"
    assert results[0]["source"] == "builtin"
    assert results[0]["identifier"] == "openJiuwen-DeepSearch"
    assert results[0]["is_builtin"] is True
    assert results[0]["is_builtin_source"] is True


def test_search_builtin_skills_skips_already_installed(tmp_path):
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()
    user_skills_dir = tmp_path / "user_skills"
    user_skills_dir.mkdir()

    skill_a = builtin_dir / "deep-research"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: openJiuwen-DeepSearch\ndescription: deep search\n---\nbody\n",
        encoding="utf-8",
    )

    installed_copy = user_skills_dir / "deep-research"
    installed_copy.mkdir()

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    with patch(
        "jiuwenswarm.common.utils.get_builtin_skills_dir",
        return_value=builtin_dir,
    ), patch(
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        return_value=user_skills_dir,
    ):
        results = toolkit._search_builtin_skills("deep", set(), 10)

    assert len(results) == 0


def test_search_skill_with_builtin_source(tmp_path):
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()
    user_skills_dir = tmp_path / "user_skills"
    user_skills_dir.mkdir()

    skill_a = builtin_dir / "deep-research"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: openJiuwen-DeepSearch\ndescription: deep search and report\n---\nbody\n",
        encoding="utf-8",
    )

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    with patch(
        "jiuwenswarm.common.utils.get_builtin_skills_dir",
        return_value=builtin_dir,
    ), patch(
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        return_value=user_skills_dir,
    ):
        result = asyncio.run(toolkit.search_skill("deep", source="builtin"))

    assert result["success"] is True
    assert result["source"] == "builtin"
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "openJiuwen-DeepSearch"


def test_search_skill_uses_stable_query_fingerprint_without_changing_search():
    raw_query = "  private customer query  "
    normalized_query = raw_query.strip()
    fingerprint = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
    skills = [
        {
            "display_name": "matching-skill",
            "summary": "matching result",
            "slug": "matching-skill",
        }
    ]
    manager = _SearchManager(skills)
    toolkit = SkillToolkit(manager)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.skill_toolkits.logger.info"
    ) as log_info:
        result = asyncio.run(
            toolkit.search_skill(raw_query, source="clawhub", limit=3)
        )

    assert toolkit._search_query_fingerprint(normalized_query) == fingerprint
    assert toolkit._search_query_fingerprint(raw_query) == fingerprint
    assert manager.params == {"q": normalized_query, "limit": 3}
    assert result["items"][0]["name"] == "matching-skill"
    logged = repr(log_info.call_args_list)
    assert fingerprint in logged
    assert fingerprint in result["query_summary"]
    assert normalized_query not in logged
    assert normalized_query not in result["query_summary"]


def test_search_skill_no_result_detail_uses_query_fingerprint():
    raw_query = "  private empty query  "
    normalized_query = raw_query.strip()
    fingerprint = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
    toolkit = SkillToolkit(_SearchManager([]))

    result = asyncio.run(toolkit.search_skill(raw_query, source="clawhub"))

    assert result["success"] is True
    assert result["items"] == []
    assert fingerprint in result["detail"]
    assert normalized_query not in result["detail"]


def test_install_skill_builtin_source_routes_to_handle_skills_install_builtin(tmp_path):
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()
    user_skills_dir = tmp_path / "workspace" / "skills"
    user_skills_dir.mkdir(parents=True)

    skill_a = builtin_dir / "my-builtin-skill"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: my-builtin-skill\ndescription: a builtin skill\n---\nbody\n",
        encoding="utf-8",
    )

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    with patch(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        return_value=builtin_dir,
    ):
        result = asyncio.run(
            toolkit.install_skill("my-builtin-skill", source="builtin")
        )

    assert result["success"] is True
    assert result["source"] == "builtin"
    assert (user_skills_dir / "my-builtin-skill").is_dir()


def test_format_skills_for_llm_distinguishes_install_status():
    skills = [
        {"name": "installed-skill", "description": "already there", "installed": True, "source": "local"},
        {"name": "builtin-skill", "description": "builtin not installed", "installed": False, "source": "builtin"},
        {"name": "marketplace-skill", "description": "from marketplace", "installed": False, "source": "clawhub"},
    ]
    rendered = _format_skills_for_llm(skills)

    assert "- installed-skill | already there [已安装]" in rendered
    assert "- builtin-skill | builtin not installed [未安装·内置技能]" in rendered
    assert "- marketplace-skill | from marketplace [未安装]" in rendered
