# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for v2 Swarm Skill session policy helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.swarm_skill_session import (
    SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND,
    SWARM_SKILL_MULTIPLE_NOT_ALLOWED,
    SwarmSkillSessionError,
    assert_chat_swarm_skill_policy,
    build_swarm_sop_hint,
    clear_member_skill_allow_lists,
    inject_swarm_sop_into_params,
    is_session_generated_team,
    parse_request_skill_ids,
    reject_deprecated_dynamic_entry,
)


def _write_skill(dir_path: Path, *, kind: str | None, name: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: demo"]
    if kind is not None:
        lines.append(f"kind: {kind}")
    lines.extend(["---", "", "# body", ""])
    (dir_path / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return dir_path


def test_parse_request_skill_ids_dedupes() -> None:
    assert parse_request_skill_ids({"skills": ["a", "a", " b ", ""]}) == ["a", "b"]
    assert parse_request_skill_ids({"skills": "x"}) == []
    assert parse_request_skill_ids({}) == []


def test_assert_forbidden_when_team_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "sop", kind="swarm-skill", name="sop")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    with pytest.raises(SwarmSkillSessionError) as exc:
        assert_chat_swarm_skill_policy(
            session_id="s1",
            team_name="oc_team_x",
            skill_ids=["sop"],
        )
    assert exc.value.code == SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND


def test_assert_allows_same_swarm_on_session_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "sop", kind="swarm-skill", name="sop")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    swarm, ordinary = assert_chat_swarm_skill_policy(
        session_id="s1",
        team_name="gen_team",
        skill_ids=["sop"],
        metadata={
            "team_origin": "session_generated",
            "skill_id": "sop",
        },
    )
    assert swarm == ["sop"]
    assert ordinary == []


def test_assert_forbids_swarm_after_plain_auto_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "sop", kind="swarm-skill", name="sop")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    with pytest.raises(SwarmSkillSessionError) as exc:
        assert_chat_swarm_skill_policy(
            session_id="s1",
            team_name="gen_team",
            skill_ids=["sop"],
            metadata={"team_origin": "session_generated"},
        )
    assert exc.value.code == SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND


def test_assert_allows_ordinary_when_team_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "xlsx-craft", kind=None, name="xlsx-craft")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    swarm, ordinary = assert_chat_swarm_skill_policy(
        session_id="s1",
        team_name="oc_team_x",
        skill_ids=["xlsx-craft"],
    )
    assert swarm == []
    assert ordinary == ["xlsx-craft"]


def test_assert_rejects_multiple_swarm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "a", kind="team-skill", name="a")
    _write_skill(root / "b", kind="swarm-skill", name="b")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    with pytest.raises(SwarmSkillSessionError) as exc:
        assert_chat_swarm_skill_policy(
            session_id="s1",
            team_name="",
            skill_ids=["a", "b"],
        )
    assert exc.value.code == SWARM_SKILL_MULTIPLE_NOT_ALLOWED


def test_unknown_kind_treated_as_ordinary_when_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "mystery", kind=None, name="mystery")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.primary_team_skill_library_dir",
        lambda: root,
    )
    swarm, ordinary = assert_chat_swarm_skill_policy(
        session_id="s1",
        team_name="oc_team_x",
        skill_ids=["mystery"],
    )
    assert swarm == []
    assert ordinary == ["mystery"]


def test_clear_member_skill_allow_lists() -> None:
    snapshot = {
        "agents": {
            "leader": {"skills": ["a"]},
            "teammate": {"model": "m"},
        },
        "predefined_members": [{"member_name": "x"}],
    }
    clear_member_skill_allow_lists(snapshot)
    assert snapshot["agents"]["leader"]["skills"] == []
    assert snapshot["agents"]["teammate"]["skills"] == []
    assert snapshot["predefined_members"][0]["skills"] == []


def test_should_skip_live_template_reconcile() -> None:
    from jiuwenswarm.server.runtime.swarm_skill_session import (
        should_skip_live_template_reconcile,
    )

    assert should_skip_live_template_reconcile(
        {"team_origin": "session_generated"}
    )
    assert not should_skip_live_template_reconcile({"team_name": "oc_team_x"})


def test_inject_swarm_sop_into_params_idempotent() -> None:
    params = {"content": "do the work"}
    inject_swarm_sop_into_params(params, "team-collab-sop")
    assert "team-collab-sop" in params["content"]
    assert build_swarm_sop_hint("team-collab-sop").splitlines()[0] in params["content"]
    once = params["content"]
    inject_swarm_sop_into_params(params, "team-collab-sop")
    assert params["content"] == once


def test_inject_swarm_sop_into_both_query_and_content() -> None:
    """REST dual-field sends must stamp query (adapter prefers query)."""
    params = {"content": "via content", "query": "via query"}
    inject_swarm_sop_into_params(params, "team-collab-sop")
    prefix = build_swarm_sop_hint("team-collab-sop").splitlines()[0]
    assert prefix in params["query"]
    assert prefix in params["content"]
    assert params["query"].endswith("via query")
    assert params["content"].endswith("via content")


def test_reject_deprecated_dynamic_entry() -> None:
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

    with pytest.raises(TeamBindingStoreError):
        reject_deprecated_dynamic_entry({"entry": "dynamic_skill"})


def test_is_session_generated_team() -> None:
    assert is_session_generated_team({"team_origin": "session_generated"})
    assert is_session_generated_team({"temporary_team_session": True})
    assert not is_session_generated_team({"team_name": "oc_team_x"})
