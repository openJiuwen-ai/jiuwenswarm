# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for persistent team binding catalog."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime import team_binding_store as team_binding_store_module
from jiuwenswarm.server.runtime.team_binding_store import (
    TeamBindingStore,
    TeamBindingStoreError,
    ensure_binding_for_entity,
)


def test_team_binding_store_defaults_to_user_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        team_binding_store_module,
        "get_user_workspace_dir",
        lambda: tmp_path,
    )

    store = TeamBindingStore()

    assert store.path == tmp_path / "agent" / "teams" / "bindings.json"


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


def test_team_binding_store_finds_binding_by_session(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")
    store.create(team_name="review_team", template_id="default")
    store.bind_session(team_name="research_team", session_id="sess-a")
    store.bind_session(team_name="review_team", session_id="sess-b")

    assert store.find_by_session("sess-a").team_name == "research_team"
    assert store.find_by_session("sess-b").team_name == "review_team"
    assert store.find_by_session("sess-missing") is None
    assert store.find_by_session("") is None


def test_team_binding_store_find_by_session_reflects_rebind(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")
    store.create(team_name="review_team", template_id="default")
    store.bind_session(team_name="research_team", session_id="sess-a")

    store.bind_session(team_name="review_team", session_id="sess-a")

    found = store.find_by_session("sess-a")
    assert found is not None
    assert found.team_name == "review_team"
    assert store.get("research_team").session_ids == ()


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


def test_ensure_binding_for_entity_creates_normal_binding(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")
    entity = SimpleNamespace(
        team_name="healed_team",
        template_id="healed_team",
        created_at=123.0,
        template_snapshot={"leader": {"member_name": "lead"}},
    )

    binding = ensure_binding_for_entity(entity, store=store)

    assert binding is not None
    assert binding.team_name == "healed_team"
    assert binding.template_id == "healed_team"
    assert binding.created_at == 123.0
    assert binding.legacy is False
    assert binding.session_ids == ()
    # In-memory create keeps the snapshot; bindings.json intentionally omits it.
    assert binding.template_snapshot == {"leader": {"member_name": "lead"}}
    reloaded = store.get("healed_team")
    assert reloaded is not None
    assert reloaded.team_name == "healed_team"
    assert reloaded.template_id == "healed_team"
    assert reloaded.created_at == 123.0
    assert reloaded.legacy is False
    assert reloaded.session_ids == ()


def test_ensure_binding_for_entity_is_idempotent(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")
    entity = SimpleNamespace(
        team_name="healed_team",
        template_id="healed_team",
        created_at=123.0,
        template_snapshot={"leader": {"member_name": "lead"}},
    )
    first = ensure_binding_for_entity(entity, store=store)
    store.bind_session(team_name="healed_team", session_id="sess-a")

    second = ensure_binding_for_entity(entity, store=store)

    assert first is not None
    assert second is not None
    assert second.session_ids == ("sess-a",)
    assert second.template_id == "healed_team"


def test_ensure_binding_for_entity_returns_none_without_identity(tmp_path) -> None:
    store = TeamBindingStore(tmp_path / "bindings.json")

    assert ensure_binding_for_entity(SimpleNamespace(team_name="", template_id="t"), store=store) is None
    assert ensure_binding_for_entity(SimpleNamespace(team_name="t", template_id=""), store=store) is None
    assert store.list() == []
