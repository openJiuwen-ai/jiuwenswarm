"""Sandbox reuse must follow the tenant workspace mounted in its policy."""

import pytest

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder


@pytest.mark.parametrize("project", [None, "shared-project"])
def test_enterprise_isolation_includes_workspace(tmp_path, monkeypatch, project):
    monkeypatch.setattr(builder, "is_enterprise", lambda: True)
    project_dir = tmp_path / project if project else None
    if project_dir:
        project_dir.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"
    key = builder._sandbox_isolation_custom_id
    assert key(project_dir, shared_dir=a) != key(project_dir, shared_dir=b)
    assert key(project_dir, shared_dir=a) == key(project_dir, shared_dir=a / ".")
    monkeypatch.setattr(builder, "get_agent_root_dir", lambda: a)
    assert key(project_dir) == key(project_dir, shared_dir=a)


def test_personal_default_project_sharing_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "is_enterprise", lambda: False)
    key = builder._sandbox_isolation_custom_id
    assert key(tmp_path, shared_dir=tmp_path / "a") == key(tmp_path, shared_dir=tmp_path / "b")
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda project: None)
    assert key(None, shared_dir=tmp_path / "a") == "project_default"


def test_sandbox_cards_use_workspace_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "is_enterprise", lambda: True)
    monkeypatch.setattr(builder, "build_filesystem_policy", lambda *a, **kw: ({}, []))
    monkeypatch.setattr(builder, "build_process_policy", lambda: {})
    cards = [builder.create_sandbox_sysop_card(
        "http://sandbox.invalid", "jiuwenbox", shared_dir=tmp_path / tenant,
    ) for tenant in ("a", "b", "a")]
    assert all(card is not None for card in cards)
    keys = [card.gateway_config.isolation.custom_id for card in cards]
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]
