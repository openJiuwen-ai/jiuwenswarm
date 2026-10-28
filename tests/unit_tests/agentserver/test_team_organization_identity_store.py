# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json

import pytest

from jiuwenswarm.server.runtime import team_organization_identity_store as store


@pytest.fixture(autouse=True)
def identity_home(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "team_home", lambda name: tmp_path / name)


def test_identity_survives_reload_and_atomic_replace():
    store.save_organization_team_identity(
        "org-expert-test", agent_group_name="technology-insight", display_name="技术洞察专家团"
    )
    assert store.load_organization_team_identity("org-expert-test") == {
        "agent_group_name": "technology-insight", "display_name": "技术洞察专家团",
    }
    store.save_organization_team_identity(
        "org-expert-test", agent_group_name="technology-insight", display_name="更新后的名称"
    )
    assert store.load_organization_team_identity("org-expert-test")["display_name"] == "更新后的名称"
    assert len(list(store._identity_path("org-expert-test").parent.iterdir())) == 1


@pytest.mark.parametrize("payload", ["invalid json", "[]", json.dumps({"team_id": "other"})])
def test_invalid_identity_uses_database_fallback(payload):
    path = store._identity_path("org-expert-test")
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    assert store.load_organization_team_identity("org-expert-test") == {}


def test_legacy_team_without_identity_does_not_create_file():
    assert store.load_organization_team_identity("org-expert-old") == {}
    assert not store._identity_path("org-expert-old").exists()
