# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Membership lookup is anchored to the saved session and never starts Teams."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime import team_organization_history as history


@pytest.fixture(autouse=True)
def organization_switch(monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": True}})


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"experimental": {"team_organization_ui_enabled": False}}])
async def test_disabled_organization_does_not_read_history(monkeypatch, config):
    monkeypatch.setattr(history, "get_config", lambda: config)
    metadata = Mock(side_effect=AssertionError("history read while disabled"))
    monkeypatch.setattr(history, "get_session_metadata", metadata)
    assert await history.load_persisted_organization("session", "expert", db=object()) is None
    metadata.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,org_ids,members,target,found",
    [
        ("owner", ["org"], ["owner", "expert"], "expert", True),
        ("owner", ["org"], ["owner", "expert"], "foreign", False),
        ("owner", ["org"], ["expert"], "expert", False),
        ("owner", [], ["owner"], "owner", False),
        ("owner", ["one", "two"], ["owner"], "owner", False),
        ("", ["org"], ["owner"], "owner", False),
    ],
)
async def test_history_membership(monkeypatch, owner, org_ids, members, target, found):
    monkeypatch.setattr(history, "get_session_metadata", lambda _: {"team_name": owner})
    lookup = AsyncMock(return_value=org_ids)
    monkeypatch.setattr(history.OrgTaskManager, "find_organization_ids_for_team", lookup)
    organization = SimpleNamespace(leaders=[SimpleNamespace(team_id=name) for name in members])
    manager = Mock(return_value=SimpleNamespace(get_organization=AsyncMock(return_value=organization)))
    monkeypatch.setattr(history, "TeamOrganizationManager", manager)
    db = object()
    result = await history.load_persisted_organization("session", target, db=db)
    assert (result is not None) is found
    if found:
        assert result == (db, organization)
    if owner:
        lookup.assert_awaited_once_with(db, owner)
    else:
        lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_session_does_not_read_metadata(monkeypatch):
    metadata = Mock()
    monkeypatch.setattr(history, "get_session_metadata", metadata)
    assert await history.load_persisted_organization("") is None
    metadata.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_organization_is_not_recreated(monkeypatch):
    monkeypatch.setattr(history, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(history.OrgTaskManager, "find_organization_ids_for_team", AsyncMock(return_value=["org"]))
    manager = SimpleNamespace(get_organization=AsyncMock(return_value=None), initialize=AsyncMock())
    monkeypatch.setattr(history, "TeamOrganizationManager", lambda **_: manager)
    assert await history.load_persisted_organization("session", db=object()) is None
    manager.initialize.assert_not_awaited()


def test_missing_database_does_not_create_file(monkeypatch, tmp_path):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(history, "get_team_entity_store", lambda: SimpleNamespace(get=lambda _: None))
    monkeypatch.setattr(history, "resolve_team_sqlite_db_path", lambda _: missing)
    shared = Mock()
    monkeypatch.setattr(history, "get_shared_db", shared)
    assert history._history_database({}, "owner") is None
    assert not missing.exists()
    shared.assert_not_called()


def test_saved_template_database_precedes_current_config(monkeypatch, tmp_path):
    saved = {"storage": {"type": "sqlite", "params": {"connection_string": str(tmp_path / "saved.db")}}}
    (tmp_path / "saved.db").touch()
    monkeypatch.setattr(
        history, "get_team_entity_store",
        lambda: SimpleNamespace(get=lambda _: SimpleNamespace(template_snapshot=saved)),
    )
    resolver = Mock(return_value=tmp_path / "saved.db")
    shared = Mock(return_value=object())
    monkeypatch.setattr(history, "resolve_team_sqlite_db_path", resolver)
    monkeypatch.setattr(history, "get_shared_db", shared)
    history._history_database({"team_template_snapshot": {"storage": {}}}, "owner")
    resolver.assert_called_once_with(saved)
    assert shared.call_args.args[0].connection_string == str(tmp_path / "saved.db")


@pytest.mark.parametrize("saved_metadata", [True, False])
def test_history_uses_legacy_snapshot_or_current_database(monkeypatch, tmp_path, saved_metadata):
    db_path = tmp_path / "existing.db"
    db_path.touch()
    snapshot = {"storage": {"type": "sqlite"}} if saved_metadata else None
    monkeypatch.setattr(history, "get_team_entity_store", lambda: SimpleNamespace(get=lambda _: None))
    resolver = Mock(return_value=db_path)
    monkeypatch.setattr(history, "resolve_team_sqlite_db_path", resolver)
    monkeypatch.setattr(history, "get_shared_db", Mock(return_value=object()))
    history._history_database({"team_template_snapshot": snapshot}, "owner")
    resolver.assert_called_once_with(snapshot)
