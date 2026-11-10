# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration-style unit tests for Swarm Skill v2 host policy paths."""

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
