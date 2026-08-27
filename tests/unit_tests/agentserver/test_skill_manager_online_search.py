from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _skillnet_item(name: str, rank_marker: str) -> dict:
    return {
        "skill_name": name,
        "skill_description": f"SkillNet {rank_marker}",
        "skill_url": f"https://github.com/example/{name}",
        "author": "example",
        "stars": 12,
        "category": "document",
    }


def _clawhub_item(name: str, rank_marker: str, owner_handle: str = "") -> dict:
    return {
        "slug": name,
        "display_name": name,
        "summary": f"ClawHub {rank_marker}",
        "version": "1.0.0",
        "updated_at": 1_750_000_000_000,
        "owner_handle": owner_handle,
    }


def _teamskillshub_item(name: str, rank_marker: str, *, is_team_skill: bool) -> dict:
    return {
        "asset_id": f"asset-{name}",
        "name": name,
        "display_name": f"{name} display",
        "summary": f"TeamSkillsHub {rank_marker}",
        "version": "1.0.0",
        "author": "example",
        "plugin_type": "swarmskill" if is_team_skill else "skill",
        "is_team_skill": is_team_skill,
    }


@pytest.mark.asyncio
async def test_online_search_queries_all_available_sources_without_skillnet(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path))
    manager._set_clawhub_token("test-token")

    async def _clawhub_search(params: dict) -> dict:
        assert params["q"] == "pdf"
        assert params["limit"] == 10
        return {
            "success": True,
            "skills": [_clawhub_item("clawhub-pdf", "first")],
        }

    async def _team_skills_hub_search(params: dict) -> dict:
        assert params["q"] == "pdf"
        assert params["limit"] == 10
        return {
            "success": True,
            "skills": [_teamskillshub_item("team-pdf", "first", is_team_skill=True)],
        }

    async def _unexpected_skillnet_search(params: dict) -> dict:
        raise AssertionError("SkillNet must not participate in Skills online search")

    monkeypatch.setattr(manager, "handle_skills_skillnet_search", _unexpected_skillnet_search)
    monkeypatch.setattr(manager, "handle_skills_clawhub_search", _clawhub_search)
    monkeypatch.setattr(manager, "handle_skills_team_skills_hub_search", _team_skills_hub_search)

    payload = await manager.handle_skills_online_search({"query": "pdf", "limit": 10})

    assert payload["success"] is True
    assert payload["partial"] is False
    assert [item["source"] for item in payload["items"]] == ["teamskillshub", "clawhub"]
    assert [item["is_team_skill"] for item in payload["items"]] == [True, False]
    assert payload["items"][0]["display_name"] == "team-pdf display"
    assert payload["items"][1]["updated_at"] == 1_750_000_000_000
    assert payload["sources"] == [
        {"source": "teamskillshub", "status": "success", "count": 1},
        {"source": "clawhub", "status": "success", "count": 1},
    ]


@pytest.mark.asyncio
async def test_online_search_skips_clawhub_without_token(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path))

    async def _unexpected_skillnet_search(params: dict) -> dict:
        raise AssertionError("SkillNet must not participate in Skills online search")

    async def _unexpected_clawhub_search(params: dict) -> dict:
        raise AssertionError("ClawHub must not be queried without a token")

    async def _team_skills_hub_search(params: dict) -> dict:
        return {"success": True, "skills": []}

    monkeypatch.setattr(manager, "handle_skills_skillnet_search", _unexpected_skillnet_search)
    monkeypatch.setattr(manager, "handle_skills_clawhub_search", _unexpected_clawhub_search)
    monkeypatch.setattr(manager, "handle_skills_team_skills_hub_search", _team_skills_hub_search)

    payload = await manager.handle_skills_online_search({"q": "pdf"})

    assert payload["success"] is True
    assert payload["partial"] is False
    assert payload["sources"] == [
        {"source": "teamskillshub", "status": "success", "count": 0},
        {
            "source": "clawhub",
            "status": "skipped",
            "count": 0,
            "detail_key": "skills.clawhub.errors.tokenNotConfigured",
        },
    ]


@pytest.mark.asyncio
async def test_online_search_returns_partial_results_when_clawhub_fails(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path))
    manager._set_clawhub_token("test-token")

    async def _unexpected_skillnet_search(params: dict) -> dict:
        raise AssertionError("SkillNet must not participate in Skills online search")

    async def _clawhub_search(params: dict) -> dict:
        return {"success": False, "detail": "remote unavailable"}

    async def _team_skills_hub_search(params: dict) -> dict:
        return {
            "success": True,
            "skills": [_teamskillshub_item("pdf", "first", is_team_skill=False)],
        }

    monkeypatch.setattr(manager, "handle_skills_skillnet_search", _unexpected_skillnet_search)
    monkeypatch.setattr(manager, "handle_skills_clawhub_search", _clawhub_search)
    monkeypatch.setattr(manager, "handle_skills_team_skills_hub_search", _team_skills_hub_search)

    payload = await manager.handle_skills_online_search({"q": "pdf"})

    assert payload["success"] is True
    assert payload["partial"] is True
    assert [item["source"] for item in payload["items"]] == ["teamskillshub"]
    assert payload["items"][0]["is_team_skill"] is False
    assert payload["sources"][1]["status"] == "error"
    assert payload["sources"][1]["detail"] == "remote unavailable"


def test_online_search_rrf_is_stable_and_exact_match_wins():
    items = SkillManager._aggregate_online_search_results(
        "target",
        {
            "skillnet": [
                _skillnet_item("other", "first"),
                _skillnet_item("target", "second"),
            ],
            "clawhub": [
                _clawhub_item("another", "first"),
                _clawhub_item("more", "second"),
            ],
        },
        10,
    )

    assert items[0]["name"] == "target"
    assert items[0]["exact_match"] is True
    assert [(item["source"], item["source_rank"]) for item in items[1:]] == [
        ("skillnet", 1),
        ("clawhub", 1),
        ("clawhub", 2),
    ]


def test_online_search_native_score_preserves_zero_and_falls_back_for_none():
    zero_score = SkillManager._normalize_online_search_item(
        "skillnet",
        {"skill_name": "zero", "score": 0, "stars": 12},
        1,
    )
    missing_score = SkillManager._normalize_online_search_item(
        "skillnet",
        {"skill_name": "fallback", "score": None, "stars": 12},
        1,
    )

    assert zero_score["native_score"] == 0
    assert missing_score["native_score"] == 12


def test_online_search_preserves_clawhub_owner_handle():
    item = SkillManager._normalize_online_search_item(
        "clawhub",
        _clawhub_item("weather", "first", owner_handle="openclaw"),
        1,
    )

    assert item["identifier"] == "weather"
    assert item["owner_handle"] == "openclaw"
    assert item["author"] == "openclaw"
    assert item["matched_sources"][0]["owner_handle"] == "openclaw"
    assert item["is_team_skill"] is False


def test_online_search_classifies_team_skills_hub_items():
    team_item = SkillManager._normalize_online_search_item(
        "teamskillshub",
        _teamskillshub_item("research-team", "first", is_team_skill=True),
        1,
    )
    regular_item = SkillManager._normalize_online_search_item(
        "teamskillshub",
        _teamskillshub_item("research", "second", is_team_skill=False),
        2,
    )

    assert team_item["is_team_skill"] is True
    assert regular_item["is_team_skill"] is False
    assert team_item["name"] == "research-team"
    assert team_item["display_name"] == "research-team display"


def test_online_search_keeps_ambiguous_clawhub_slugs_distinct():
    items = SkillManager._aggregate_online_search_results(
        "weather",
        {
            "skillnet": [],
            "clawhub": [
                _clawhub_item("weather", "first", owner_handle="owner-a"),
                _clawhub_item("weather", "second", owner_handle="owner-b"),
            ],
        },
        10,
    )

    assert len(items) == 2
    assert {(item["identifier"], item["owner_handle"]) for item in items} == {
        ("weather", "owner-a"),
        ("weather", "owner-b"),
    }


def test_online_search_merges_identical_normalized_urls():
    items = SkillManager._aggregate_online_search_results(
        "shared",
        {
            "skillnet": [
                {
                    "skill_name": "shared",
                    "skill_url": "http://github.com/example/shared/",
                },
                {
                    "skill_name": "shared-duplicate",
                    "skill_url": "https://github.com/example/shared",
                },
            ],
            "clawhub": [],
        },
        10,
    )

    assert len(items) == 1
    assert items[0]["identifier"] == "http://github.com/example/shared/"
    assert len(items[0]["matched_sources"]) == 2
    assert items[0]["fusion_score"] == pytest.approx(1 / 61 + 1 / 62)


@pytest.mark.asyncio
async def test_online_search_rejects_invalid_input(tmp_path):
    manager = SkillManager(workspace_dir=str(tmp_path))

    missing_query = await manager.handle_skills_online_search({"query": ""})
    invalid_limit = await manager.handle_skills_online_search({"query": "pdf", "limit": "many"})

    assert missing_query["success"] is False
    assert missing_query["detail"] == "缺少参数: query"
    assert invalid_limit["success"] is False
    assert invalid_limit["detail"] == "参数 limit 必须是整数"
