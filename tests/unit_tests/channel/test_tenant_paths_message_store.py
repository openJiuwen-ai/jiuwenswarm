# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for channel lazy per-tenant path resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.message import (
    MessageStore,
)
from jiuwenswarm.gateway.tenant_paths import (
    normalize_channel_tenant_ids,
    resolve_channel_group_chat_memory_dir,
    tenant_ids_from_message,
    workspace_key_from_channel_ids,
)
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
)


def test_normalize_channel_tenant_ids_defaults():
    assert normalize_channel_tenant_ids(None, None) == ("default", "default")
    assert normalize_channel_tenant_ids("", "  ") == ("default", "default")
    assert normalize_channel_tenant_ids("svc", "office") == ("svc", "office")


def test_workspace_key_from_channel_ids():
    assert workspace_key_from_channel_ids(None, None) == "default"
    assert workspace_key_from_channel_ids("default", "office") == "default_office"
    assert workspace_key_from_channel_ids("svc", "bot") == "svc_bot"


def test_message_store_lazy_paths_isolate_tenants(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    store = MessageStore()
    assert not (tmp_path / "workspace_default_office").exists()

    store.add_message_to_memory(
        "chat_a",
        {"message_id": "1", "content": "hello", "open_id": "u1", "timestamp": 1},
        service_id="default",
        agent_id="office",
    )
    store.add_message_to_memory(
        "chat_a",
        {"message_id": "2", "content": "other", "open_id": "u2", "timestamp": 2},
        service_id="default",
        agent_id="default",
    )

    office_file = (
        resolve_channel_group_chat_memory_dir("default_office") / "chat_a.json"
    )
    default_file = (
        resolve_channel_group_chat_memory_dir("default") / "chat_a.json"
    )
    assert office_file.exists()
    assert default_file.exists()
    assert office_file != default_file
    assert office_file.parts[-6] == "workspace_default_office"
    assert default_file.parts[-6] == "workspace_default"

    office_hist = store.load_memory(
        "chat_a", service_id="default", agent_id="office"
    )
    default_hist = store.load_memory(
        "chat_a", service_id="default", agent_id="default"
    )
    assert isinstance(office_hist, list) and len(office_hist) == 1
    assert office_hist[0]["content"] == "hello"
    assert isinstance(default_hist, list) and len(default_hist) == 1
    assert default_hist[0]["content"] == "other"


def test_message_store_omitted_tenant_uses_default_tree(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    store = MessageStore()
    store.add_message_to_memory(
        "chat_b",
        {"message_id": "1", "content": "x", "open_id": "", "timestamp": 1},
    )
    path = resolve_channel_group_chat_memory_dir("default") / "chat_b.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["content"] == "x"


def test_tenant_ids_from_message_params_and_session():
    msg = SimpleNamespace(
        params={"service_id": "s1", "agent_id": "a1"},
        session_id="ignored",
    )
    assert tenant_ids_from_message(msg) == ("s1", "a1")

    msg2 = SimpleNamespace(params={}, session_id=None)
    assert tenant_ids_from_message(msg2) == ("default", "default")
