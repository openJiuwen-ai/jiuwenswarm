# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration-style unit tests for Swarm Skill host policy paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.swarm_skill_session import (
    SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND,
    TEAM_ORIGIN_SESSION_GENERATED,
    SwarmSkillSessionError,
    clear_member_skill_allow_lists,
    should_skip_live_template_reconcile,
)


def _write_swarm(root: Path, name: str = "sop-swarm") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                "description: demo",
                "kind: swarm-skill",
                "---",
                "",
                "# body",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_auto_bind_swarm_sets_session_generated_and_open_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.handlers import chat as chat_handlers

    skills_root = tmp_path / "skills"
    _write_swarm(skills_root)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: skills_root,
    )

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    meta: dict = {"mode": "team"}

    def fake_get_meta(session_id, **kwargs):
        return dict(meta)

    def fake_update_meta(**kwargs):
        meta["team_name"] = kwargs.get("team_name") or meta.get("team_name")
        meta["team_origin"] = kwargs.get("team_origin") or meta.get("team_origin")
        meta["temporary_team_session"] = kwargs.get(
            "temporary_team_session", meta.get("temporary_team_session")
        )
        meta["skill_id"] = kwargs.get("skill_id", meta.get("skill_id"))
        if kwargs.get("team_template_snapshot") is not None:
            meta["team_template_snapshot"] = kwargs["team_template_snapshot"]
        meta["team_template_id"] = kwargs.get(
            "team_template_id", meta.get("team_template_id")
        )

    entity_snapshot = {
        "agents": {
            "leader": {"skills": ["preset-a"]},
            "teammate": {"skills": ["preset-b"]},
        },
        "predefined_members": [{"member_name": "x", "skills": ["c"]}],
    }
    entity = SimpleNamespace(
        team_name="gen_team",
        template_id="default",
        created_at=1.0,
        template_snapshot=entity_snapshot,
    )
    written: dict = {}

    class FakeEntityStore:
        def get(self, team_name):
            return entity

        def write(self, **kwargs):
            written.update(kwargs)
            entity.template_snapshot = kwargs["template_snapshot"]
            return entity

        def delete_team_directory(self, team_name):
            return True

    binding = SimpleNamespace(team_name="gen_team", template_id="default")

    class FakeBindingStore:
        def bind_session(self, **kwargs):
            return binding

        def unbind_session(self, **kwargs):
            return None

        def delete(self, team_name):
            return True

    async def fake_create(**kwargs):
        return binding, {"template_id": "default"}

    monkeypatch.setattr(chat_handlers, "_sessions_dir_for_request", lambda ctx: sessions_root)
    monkeypatch.setattr(chat_handlers, "_effective_config_for_request", lambda ctx: {})
    monkeypatch.setattr(chat_handlers, "_is_team_metadata_mode", lambda meta: True)
    monkeypatch.setattr(
        chat_handlers,
        "_session_team_binding_lock",
        lambda session_id: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        fake_get_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
        fake_update_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    monkeypatch.setattr(chat_handlers, "_create_generated_team_binding", fake_create)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.TeamManager.build_session_scoped_team_name",
        staticmethod(lambda team, sid: f"{team}__{sid}"),
    )

    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s_swarm",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "mode": "team",
            "content": "run playbook",
            "skills": ["sop-swarm"],
        },
        is_stream=False,
    )
    ctx = MagicMock()
    result = await chat_handlers._ensure_auto_team_binding_for_chat(ctx, request)

    assert result is binding
    assert meta["team_origin"] == TEAM_ORIGIN_SESSION_GENERATED
    assert meta["skill_id"] == "sop-swarm"
    assert meta["team_template_snapshot"]["agents"]["leader"]["skills"] == []
    assert written["template_snapshot"]["agents"]["teammate"]["skills"] == []
    assert should_skip_live_template_reconcile(meta) is True

    # Second send: idempotent reuse, no second create.
    created = {"n": 0}

    async def boom_create(**kwargs):
        created["n"] += 1
        raise AssertionError("must not create again")

    monkeypatch.setattr(chat_handlers, "_create_generated_team_binding", boom_create)
    again = await chat_handlers._ensure_auto_team_binding_for_chat(ctx, request)
    assert again == "gen_team"
    assert created["n"] == 0


@pytest.mark.asyncio
async def test_bound_team_rejects_swarm_via_auto_bind_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.handlers import chat as chat_handlers

    skills_root = tmp_path / "skills"
    _write_swarm(skills_root)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: skills_root,
    )
    monkeypatch.setattr(chat_handlers, "_sessions_dir_for_request", lambda ctx: tmp_path)
    monkeypatch.setattr(chat_handlers, "_is_team_metadata_mode", lambda meta: True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *a, **k: {"mode": "team", "team_name": "oc_team_x"},
    )

    request = AgentRequest(
        request_id="r2",
        channel_id="web",
        session_id="s_bound",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team", "content": "x", "skills": ["sop-swarm"]},
        is_stream=False,
    )
    with pytest.raises(SwarmSkillSessionError) as exc:
        await chat_handlers._ensure_auto_team_binding_for_chat(MagicMock(), request)
    assert exc.value.code == SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND


def test_clear_forces_skills_key_even_when_absent() -> None:
    snapshot = {"agents": {"leader": {"model": "m"}, "teammate": {}}}
    clear_member_skill_allow_lists(snapshot)
    assert snapshot["agents"]["leader"]["skills"] == []
    assert snapshot["agents"]["teammate"]["skills"] == []


def test_pipeline_maps_swarm_error_payload() -> None:
    """Ensure SwarmSkillSessionError exposes stable wire payload fields."""
    err = SwarmSkillSessionError(
        "blocked",
        code=SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND,
        session_id="s1",
    )
    assert err.payload["code"] == SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND
    assert err.payload["session_id"] == "s1"


@pytest.mark.asyncio
async def test_plain_auto_bind_is_not_session_generated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbound team chat without Swarm must keep sticky (non-temporary) metadata."""
    from jiuwenswarm.server.handlers import chat as chat_handlers

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    meta: dict = {"mode": "team"}
    created_kwargs: dict = {}

    def fake_get_meta(session_id, **kwargs):
        return dict(meta)

    def fake_update_meta(**kwargs):
        created_kwargs.update(kwargs)
        meta["team_name"] = kwargs.get("team_name") or meta.get("team_name")
        meta["team_origin"] = kwargs.get("team_origin", meta.get("team_origin"))
        meta["temporary_team_session"] = kwargs.get(
            "temporary_team_session", meta.get("temporary_team_session")
        )
        meta["skill_id"] = kwargs.get("skill_id", meta.get("skill_id"))

    binding = SimpleNamespace(team_name="plain_gen", template_id="default")

    class FakeBindingStore:
        def bind_session(self, **kwargs):
            return binding

        def unbind_session(self, **kwargs):
            return None

        def delete(self, team_name):
            return True

    class FakeEntityStore:
        def delete_team_directory(self, team_name):
            return True

    async def fake_create(**kwargs):
        created_kwargs["description"] = kwargs.get("description")
        return binding, {"template_id": "default"}

    monkeypatch.setattr(chat_handlers, "_sessions_dir_for_request", lambda ctx: sessions_root)
    monkeypatch.setattr(chat_handlers, "_effective_config_for_request", lambda ctx: {})
    monkeypatch.setattr(chat_handlers, "_is_team_metadata_mode", lambda meta: True)
    monkeypatch.setattr(
        chat_handlers,
        "_session_team_binding_lock",
        lambda session_id: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        fake_get_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
        fake_update_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    monkeypatch.setattr(chat_handlers, "_create_generated_team_binding", fake_create)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.TeamManager.build_session_scoped_team_name",
        staticmethod(lambda team, sid: f"{team}__{sid}"),
    )

    request = AgentRequest(
        request_id="r_plain",
        channel_id="web",
        session_id="s_plain",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team", "content": "hello plain team"},
        is_stream=False,
    )
    result = await chat_handlers._ensure_auto_team_binding_for_chat(MagicMock(), request)
    assert result is binding
    assert meta.get("team_origin") in ("", None)
    assert meta.get("temporary_team_session") in (False, None)
    assert should_skip_live_template_reconcile(meta) is False
    assert created_kwargs.get("description") == "hello plain team"


@pytest.mark.asyncio
async def test_swarm_auto_bind_keeps_user_query_for_title_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.handlers import chat as chat_handlers

    skills_root = tmp_path / "skills"
    _write_swarm(skills_root)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: skills_root,
    )

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    meta: dict = {"mode": "team"}
    captured: dict = {}

    def fake_get_meta(session_id, **kwargs):
        return dict(meta)

    def fake_update_meta(**kwargs):
        captured["user_content"] = kwargs.get("user_content")
        meta["team_name"] = kwargs.get("team_name")
        meta["team_origin"] = kwargs.get("team_origin")
        meta["temporary_team_session"] = kwargs.get("temporary_team_session")
        meta["skill_id"] = kwargs.get("skill_id")

    entity = SimpleNamespace(
        team_name="gen_team",
        template_id="default",
        created_at=1.0,
        template_snapshot={"agents": {"leader": {"skills": ["x"]}}},
    )

    class FakeEntityStore:
        def get(self, team_name):
            return entity

        def write(self, **kwargs):
            entity.template_snapshot = kwargs["template_snapshot"]
            return entity

        def delete_team_directory(self, team_name):
            return True

    binding = SimpleNamespace(team_name="gen_team", template_id="default")

    class FakeBindingStore:
        def bind_session(self, **kwargs):
            return binding

        def unbind_session(self, **kwargs):
            return None

        def delete(self, team_name):
            return True

    async def fake_create(**kwargs):
        captured["description"] = kwargs.get("description")
        return binding, {"template_id": "default"}

    monkeypatch.setattr(chat_handlers, "_sessions_dir_for_request", lambda ctx: sessions_root)
    monkeypatch.setattr(chat_handlers, "_effective_config_for_request", lambda ctx: {})
    monkeypatch.setattr(chat_handlers, "_is_team_metadata_mode", lambda meta: True)
    monkeypatch.setattr(
        chat_handlers,
        "_session_team_binding_lock",
        lambda session_id: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        fake_get_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
        fake_update_meta,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    monkeypatch.setattr(chat_handlers, "_create_generated_team_binding", fake_create)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.TeamManager.build_session_scoped_team_name",
        staticmethod(lambda team, sid: f"{team}__{sid}"),
    )

    request = AgentRequest(
        request_id="r_sop",
        channel_id="web",
        session_id="s_sop",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "mode": "team",
            "content": "user asks for research",
            "skills": ["sop-swarm"],
        },
        is_stream=False,
    )
    await chat_handlers._ensure_auto_team_binding_for_chat(MagicMock(), request)
    assert captured["description"] == "user asks for research"
    assert captured["user_content"] == "user asks for research"
    assert "[Team collaboration SOP]" not in str(captured["description"])
    assert "[Team collaboration SOP]" in str(request.params.get("content") or "")


def test_catalog_only_bool_coercion() -> None:
    from jiuwenswarm.server.handlers.team import _coerce_request_bool

    assert _coerce_request_bool("false", default=True) is False
    assert _coerce_request_bool("0", default=True) is False
    assert _coerce_request_bool("true", default=False) is True
    assert _coerce_request_bool(True, default=False) is True
    assert _coerce_request_bool(None, default=False) is False


def test_sample_templates_do_not_pollute_modes_team_default_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team import config_loader
    from jiuwenswarm.agents.harness.team import sample_templates

    monkeypatch.setattr(
        sample_templates,
        "load_sample_team_templates",
        lambda: {"research_insight": {"sample": True, "team_name": "research"}},
    )
    empty_cfg = {"modes": {"team": {}}}
    assert config_loader._get_modes_team(empty_cfg) == {}
    assert config_loader.list_team_template_summaries(empty_cfg) == []
    listed = config_loader.list_team_template_summaries(empty_cfg, catalog_only=True)
    assert any(item.get("template_id") == "research_insight" for item in listed)


def test_sample_template_bind_resolves_snapshot_outside_default_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catalog samples must bind: snapshot + create validation share sample lookup."""
    from jiuwenswarm.agents.harness.team import config_loader
    from jiuwenswarm.agents.harness.team import sample_templates
    from jiuwenswarm.server.handlers import team as team_handlers
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStore,
        TeamBindingStoreError,
    )
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    sample_body = {
        "sample": True,
        "expose_in_catalog": True,
        "team_name": "research_insight",
        "display_name": "研究洞察（样例）",
        "agents": {"leader": {}},
        "leader": {"member_name": "research-lead"},
    }
    monkeypatch.setattr(
        sample_templates,
        "load_sample_team_templates",
        lambda: {"research_insight": dict(sample_body)},
    )
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    empty_cfg = {"modes": {"team": {}}}
    assert "research_insight" not in {
        item.get("template_id")
        for item in config_loader.list_team_template_summaries(empty_cfg)
    }
    snapshot = config_loader.get_team_template_snapshot(
        empty_cfg, template_id="research_insight"
    )
    assert snapshot.get("sample") is True
    assert snapshot.get("display_name") == "研究洞察（样例）"

    binding = team_handlers._create_team_binding_from_template(
        team_name="oc_team_sample_research",
        template_id="research_insight",
        config_base=empty_cfg,
    )
    assert binding.template_id == "research_insight"
    entity = entity_store.get("oc_team_sample_research")
    assert entity is not None
    assert entity.template_snapshot.get("sample") is True

    with pytest.raises(TeamBindingStoreError) as missing:
        team_handlers._create_team_binding_from_template(
            team_name="oc_team_missing_sample",
            template_id="not_a_real_sample",
            config_base=empty_cfg,
        )
    assert missing.value.code == "NOT_FOUND"


def test_create_team_binding_rejects_empty_template_id_before_store_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-6: empty template_id → NOT_FOUND without binding create/delete churn."""
    from jiuwenswarm.server.handlers import team as team_handlers
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStore,
        TeamBindingStoreError,
    )
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )
    cfg = {
        "modes": {
            "team": {
                "first": {
                    "team_name": "first",
                    "agents": {"leader": {}},
                    "leader": {"member_name": "lead"},
                }
            }
        }
    }
    with pytest.raises(TeamBindingStoreError) as exc_info:
        team_handlers._create_team_binding_from_template(
            team_name="oc_team_empty_template",
            template_id="",
            config_base=cfg,
        )
    assert exc_info.value.code == "NOT_FOUND"
    assert "template_id not found" in str(exc_info.value)
    assert binding_store.list() == []
    assert entity_store.get("oc_team_empty_template") is None
