# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for persistent team binding catalog."""

from __future__ import annotations

import json
import threading

import pytest

from jiuwenswarm.server.runtime import team_binding_store
from jiuwenswarm.server.runtime.team_binding_store import (
    TeamBindingStore,
    TeamBindingStoreError,
)


def test_default_store_uses_agent_teams_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        team_binding_store,
        "get_user_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        team_binding_store,
        "get_agent_root_dir",
        lambda: tmp_path / "agent",
    )

    store = TeamBindingStore()

    assert store.path == tmp_path / ".agent_teams" / "bindings.json"


def test_default_store_migrates_legacy_bindings(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "agent" / "teams" / "bindings.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "teams": {
                    "research_team": {
                        "team_name": "research_team",
                        "template_id": "default",
                        "created_at": 1,
                        "updated_at": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        team_binding_store,
        "get_user_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        team_binding_store,
        "get_agent_root_dir",
        lambda: tmp_path / "agent",
    )

    store = TeamBindingStore()

    assert store.path == tmp_path / ".agent_teams" / "bindings.json"
    assert store.get("research_team") is not None
    assert store.path.is_file()
    assert not legacy_path.exists()


def test_team_binding_store_creates_and_persists_binding(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")

    binding = store.create(team_name="research_team", template_id="default")
    updated = store.bind_session(team_name="research_team", session_id="sess-a")
    updated = store.bind_session(team_name="research_team", session_id="sess-b")
    duplicate = store.bind_session(team_name="research_team", session_id="sess-a")

    assert binding.team_name == "research_team"
    assert updated.template_id == "default"
    assert updated.session_ids == ("sess-a", "sess-b")
    assert duplicate.session_ids == ("sess-a", "sess-b")
    assert duplicate.last_session_id == "sess-a"

    reloaded = TeamBindingStore(tmp_path / "teams" / "bindings.json").get(
        "research_team"
    )
    assert reloaded is not None
    assert reloaded.team_name == "research_team"
    assert reloaded.session_ids == ("sess-a", "sess-b")


def test_team_binding_store_unbinds_deleted_session(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")
    store.bind_session(team_name="research_team", session_id="sess-a")
    store.bind_session(team_name="research_team", session_id="sess-b")

    updated = store.unbind_session(team_name="research_team", session_id="sess-b")

    assert updated is not None
    assert updated.session_ids == ("sess-a",)
    assert updated.last_session_id == "sess-a"
    reloaded = TeamBindingStore(tmp_path / "teams" / "bindings.json").get("research_team")
    assert reloaded is not None
    assert reloaded.session_ids == ("sess-a",)


def test_team_binding_store_unbinds_by_session_when_team_name_missing(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")
    store.bind_session(team_name="research_team", session_id="sess-a")

    updated = store.unbind_session(session_id="sess-a")

    assert updated is not None
    assert updated.session_ids == ()
    assert updated.last_session_id == ""


def test_team_binding_store_rebinds_session_exclusively(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")
    store.create(team_name="review_team", template_id="default")
    store.bind_session(team_name="research_team", session_id="sess-a")

    rebound = store.bind_session(team_name="review_team", session_id="sess-a")

    assert rebound.session_ids == ("sess-a",)
    assert store.get("research_team").session_ids == ()
    assert store.get("review_team").session_ids == ("sess-a",)


def test_team_binding_store_does_not_rewrite_idempotent_binding(tmp_path) -> None:
    path = tmp_path / "teams" / "bindings.json"
    store = TeamBindingStore(path)
    store.create(team_name="research_team", template_id="default")
    first = store.bind_session(team_name="research_team", session_id="sess-a")
    persisted = path.read_text(encoding="utf-8")

    duplicate = store.bind_session(team_name="research_team", session_id="sess-a")

    assert duplicate.updated_at == first.updated_at
    assert path.read_text(encoding="utf-8") == persisted


def test_team_binding_store_reads_legacy_template_snapshot_but_does_not_rewrite_it(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")
    payload = {
        "version": 1,
        "teams": {
            "legacy_team": {
                "team_name": "legacy_team",
                "template_id": "default",
                "created_at": 1,
                "updated_at": 1,
                "session_ids": [],
                "last_session_id": "",
                "legacy": False,
                "template_snapshot": {"team_name": "template_team"},
            }
        },
    }
    (tmp_path / "bindings.json").write_text(json.dumps(payload), encoding="utf-8")

    binding = store.get("legacy_team")
    assert binding is not None
    assert binding.template_snapshot == {"team_name": "template_team"}

    store.bind_session(team_name="legacy_team", session_id="sess-a")
    rewritten = json.loads((tmp_path / "bindings.json").read_text(encoding="utf-8"))
    assert "template_snapshot" not in rewritten["teams"]["legacy_team"]


@pytest.mark.parametrize(
    "team_name",
    ["", ".", "..", "../escape", r"..\escape", "line\nbreak", "a" * 65],
)
def test_team_binding_store_rejects_invalid_team_name(tmp_path, team_name: str) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")

    with pytest.raises(TeamBindingStoreError) as exc_info:
        store.create(team_name=team_name, template_id="default")

    assert exc_info.value.code == "BAD_REQUEST"


@pytest.mark.parametrize("team_name", ["123", "has space", "中文团队", "team:name", "团队🚀"])
def test_team_binding_store_accepts_display_names(tmp_path, team_name: str) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")

    binding = store.create(team_name=team_name, template_id="default")

    assert binding.team_name == team_name


def test_team_binding_store_rejects_duplicate_team_name(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")
    store.create(team_name="ops_team", template_id="default")

    with pytest.raises(TeamBindingStoreError) as exc_info:
        store.create(team_name="ops_team", template_id="default")

    assert exc_info.value.code == "CONFLICT"


def test_team_binding_store_concurrent_create_keeps_valid_json(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")
    errors: list[BaseException] = []

    def create_team(index: int) -> None:
        try:
            store.create(team_name=f"team_{index}", template_id="default")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=create_team, args=(index,))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    payload = json.loads((tmp_path / "bindings.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["teams"]) == {f"team_{index}" for index in range(12)}
    assert {binding.team_name for binding in store.list()} == {
        f"team_{index}" for index in range(12)
    }
