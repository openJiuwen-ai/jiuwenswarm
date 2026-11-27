# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace agent identity file tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.enterprise.tenant import agent_identity_store
from jiuwenswarm.enterprise.tenant.agent_identity_store import (
    agent_identity_path,
    persist_agent_identity,
    read_agent_identity_file,
)


@pytest.fixture()
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        agent_identity_store,
        "get_multi_tenant_user_workspace_dir",
        lambda tenant_key: tmp_path / f"workspace_{tenant_key}",
    )
    return tmp_path


def _identity(
    *,
    agent_name: str = "agent_svc-1_agent-1",
    agent_id: str = "svc-1_agent-1",
    runtime_template_id: str = "svc-1",
    agent_template_id: str = "agent-1",
) -> dict[str, str]:
    return {
        "agent_name": agent_name,
        "agent_id": agent_id,
        "runtime_template_id": runtime_template_id,
        "agent_template_id": agent_template_id,
    }


def test_persist_writes_list_schema(workspace_root: Path) -> None:
    persist_agent_identity("tk", _identity())

    payload = json.loads(agent_identity_path("tk").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["current_agent_id"] == "svc-1_agent-1"
    assert payload["agents"] == [
        {
            "agent_id": "svc-1_agent-1",
            "runtime_template_id": "svc-1",
            "agent_template_id": "agent-1",
            "updated_at": payload["agents"][0]["updated_at"],
        }
    ]
    assert "agent_name" not in payload["agents"][0]


def test_persist_is_idempotent_for_same_identity(workspace_root: Path) -> None:
    persist_agent_identity("tk", _identity())
    first = agent_identity_path("tk").read_text(encoding="utf-8")

    persist_agent_identity("tk", _identity())

    assert agent_identity_path("tk").read_text(encoding="utf-8") == first


def test_persist_upserts_by_agent_id_and_keeps_history(workspace_root: Path) -> None:
    persist_agent_identity("tk", _identity())
    persist_agent_identity(
        "tk",
        _identity(
            agent_name="agent_svc-2_agent-2",
            agent_id="svc-2_agent-2",
            runtime_template_id="svc-2",
            agent_template_id="agent-2",
        ),
    )
    persist_agent_identity(
        "tk",
        _identity(runtime_template_id="svc-1-rebound"),
    )

    state = read_agent_identity_file("tk")
    assert state["current_agent_id"] == "svc-1_agent-1"
    assert [item["agent_id"] for item in state["agents"]] == [
        "svc-1_agent-1",
        "svc-2_agent-2",
    ]
    assert state["agents"][0]["runtime_template_id"] == "svc-1-rebound"
    assert "agent_name" not in state["current"]


def test_persist_skips_identity_without_agent_id(workspace_root: Path) -> None:
    persist_agent_identity("tk", {"agent_name": "agent_only"})
    assert not agent_identity_path("tk").exists()

    # agent_name 不再参与落盘；只有 agent_id 的身份仍然有效。
    persist_agent_identity("tk", {"agent_id": "id_only"})
    state = read_agent_identity_file("tk")
    assert state["current_agent_id"] == "id_only"
    assert state["current"] == {
        "agent_id": "id_only",
        "runtime_template_id": "",
        "agent_template_id": "",
        "updated_at": state["current"]["updated_at"],
    }


def test_persist_rebuilds_corrupted_file(workspace_root: Path) -> None:
    path = agent_identity_path("tk")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    persist_agent_identity("tk", _identity())

    state = read_agent_identity_file("tk")
    assert state["current_agent_id"] == "svc-1_agent-1"
    assert len(state["agents"]) == 1


def test_persist_swallows_io_errors(workspace_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(agent_identity_store, "_atomic_write", _raise)

    persist_agent_identity("tk", _identity())

    assert not agent_identity_path("tk").exists()
