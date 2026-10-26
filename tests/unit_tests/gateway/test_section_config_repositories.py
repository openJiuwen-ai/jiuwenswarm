# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permissions / logging / memory 单文档 Repository。"""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.config.a2ui import A2uiConfigRepository
from jiuwenswarm.gateway.config.browser import BrowserConfigRepository
from jiuwenswarm.gateway.config.heartbeat import HeartbeatConfigRepository
from jiuwenswarm.gateway.config.locale import PreferredLanguageConfigRepository
from jiuwenswarm.gateway.config.logging import LoggingConfigRepository, db_logging_codec
from jiuwenswarm.gateway.config.memory import MemoryConfigRepository
from jiuwenswarm.gateway.config.permissions import PermissionsConfigRepository
from jiuwenswarm.gateway.config.section import (
    DbBodySectionCodec,
    DbFlatSectionCodec,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.backends.file_persistent import FilePersistentBackend
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend
from jiuwenswarm.gateway.storage_assembly.layouts import build_gateway_store_registry
from jiuwenswarm.gateway.storage_assembly.setup import (
    create_a2ui_config_repository,
    create_browser_config_repository,
    create_heartbeat_config_repository,
    create_logging_config_repository,
    create_memory_config_repository,
    create_permissions_config_repository,
    create_preferred_language_config_repository,
)


@pytest.mark.asyncio
async def test_permissions_yaml_merge_and_enabled() -> None:
    store = InMemoryPersistentBackend()
    repo = PermissionsConfigRepository(store, YamlSectionCodec())
    await repo.replace({"enabled": False, "tools": {"bash": "ask"}})
    await repo.set_enabled(True)
    body = await repo.get_body()
    assert body["enabled"] is True
    assert body["tools"]["bash"] == "ask"


@pytest.mark.asyncio
async def test_permissions_tool_and_rule_helpers() -> None:
    store = InMemoryPersistentBackend()
    repo = PermissionsConfigRepository(store, YamlSectionCodec())
    await repo.replace({"enabled": True, "tools": {}, "rules": []})

    await repo.set_deny_guidance("blocked")
    tools = await repo.update_tool("bash", "deny")
    assert tools["tools"]["bash"] == "deny"

    rule = await repo.create_rule(
        {"tools": ["bash"], "pattern": "rm -rf", "action": "deny"}
    )
    assert rule["id"]
    updated = await repo.update_rule(rule["id"], {"description": "danger"})
    assert updated["description"] == "danger"
    assert await repo.delete_rule(rule["id"]) is True
    assert await repo.delete_tool("bash") is True

    body = await repo.get_body()
    assert body["deny_guidance_message"] == "blocked"
    assert body["tools"] == {}
    assert body["rules"] == []


@pytest.mark.asyncio
async def test_logging_yaml_merge_levels() -> None:
    store = InMemoryPersistentBackend()
    repo = LoggingConfigRepository(store, YamlSectionCodec())
    await repo.replace({"level": "INFO", "gateway": "INFO"})
    await repo.merge_levels({"gateway": "DEBUG", "channel": "WARNING"})
    body = await repo.get_body()
    assert body["level"] == "INFO"
    assert body["gateway"] == "DEBUG"
    assert body["channel"] == "WARNING"


@pytest.mark.asyncio
async def test_logging_db_flat_codec() -> None:
    store = InMemoryPersistentBackend()
    repo = LoggingConfigRepository(
        store, db_logging_codec(), instance_id=""
    )
    await repo.merge_levels({"level": "ERROR", "full": "DEBUG"})
    rows = await store.list("logging_config", limit=1)
    assert rows
    assert rows[0]["level"] == "ERROR"
    assert rows[0]["full"] == "DEBUG"
    assert "body" not in rows[0]
    assert rows[0].get("created_at") is not None
    assert rows[0].get("updated_at") is not None

    created_at = rows[0]["created_at"]
    await repo.merge_levels({"gateway": "WARNING"})
    rows = await store.list("logging_config", limit=1)
    assert rows[0]["gateway"] == "WARNING"
    assert rows[0]["created_at"] == created_at
    assert rows[0].get("updated_at") is not None


@pytest.mark.asyncio
async def test_permissions_db_body_writes_timestamps() -> None:
    store = InMemoryPersistentBackend()
    repo = PermissionsConfigRepository(
        store, DbBodySectionCodec(), instance_id=""
    )
    await repo.merge({"enabled": True})
    rows = await store.list("permissions_config", limit=1)
    assert rows
    assert rows[0].get("created_at") is not None
    assert rows[0].get("updated_at") is not None
    created_at = rows[0]["created_at"]
    await repo.merge({"enabled": False})
    rows = await store.list("permissions_config", limit=1)
    assert rows[0]["body"]["enabled"] is False
    assert rows[0]["created_at"] == created_at
    assert rows[0].get("updated_at") is not None


@pytest.mark.asyncio
async def test_memory_forbidden_helpers() -> None:
    store = InMemoryPersistentBackend()
    repo = MemoryConfigRepository(store, YamlSectionCodec())
    await repo.set_forbidden_enabled(True)
    await repo.merge_forbidden_description({"zh": "禁止", "en": "deny"})
    body = await repo.get_body()
    section = body["forbidden_memory_definition"]
    assert section["enabled"] is True
    assert section["description"]["zh"] == "禁止"


@pytest.mark.asyncio
async def test_memory_replace_and_delete() -> None:
    store = InMemoryPersistentBackend()
    repo = MemoryConfigRepository(store, YamlSectionCodec())
    await repo.replace({"forbidden_memory_definition": {"enabled": True}, "extra": 1})
    body = await repo.get_body()
    assert body["extra"] == 1
    assert await repo.delete() is True
    assert await repo.get_body() == {}


@pytest.mark.asyncio
async def test_logging_replace_and_delete() -> None:
    store = InMemoryPersistentBackend()
    repo = LoggingConfigRepository(store, YamlSectionCodec())
    await repo.replace({"level": "ERROR", "gateway": "DEBUG", "preview_user_content": False})
    body = await repo.get_body()
    assert body["level"] == "ERROR"
    assert body["preview_user_content"] is False
    assert await repo.delete() is True
    assert await repo.get_body() == {}


@pytest.mark.asyncio
async def test_section_yaml_file_overlay(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "permissions:\n  enabled: false\nlogging:\n  level: INFO\nmemory:\n  foo: 1\n",
        encoding="utf-8",
    )
    store = FilePersistentBackend(
        registry=build_gateway_store_registry(config_file=config_path)
    )
    perms = PermissionsConfigRepository(store, YamlSectionCodec())
    await perms.set_enabled(True)
    logging_repo = LoggingConfigRepository(store, YamlSectionCodec())
    await logging_repo.merge_levels({"gateway": "DEBUG"})
    memory = MemoryConfigRepository(store, YamlSectionCodec())
    await memory.merge({"bar": 2})

    text = config_path.read_text(encoding="utf-8")
    assert "enabled: true" in text.lower() or "enabled: True" in text
    assert "gateway: DEBUG" in text or "gateway: debug" in text.lower()
    assert "foo: 1" in text
    assert "bar: 2" in text


def test_factory_codec_selection(monkeypatch) -> None:
    store = InMemoryPersistentBackend()
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    personal = create_permissions_config_repository(store)
    assert isinstance(personal._inner._codec, YamlSectionCodec)
    # 企业版亦仅 yaml：实例级 permissions_config 表已移除，策略走 template 槽位
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    enterprise = create_permissions_config_repository(store, instance_id="x")
    assert isinstance(enterprise._inner._codec, YamlSectionCodec)

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    log_personal = create_logging_config_repository(store)
    assert isinstance(log_personal._inner._codec, YamlSectionCodec)
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    log_enterprise = create_logging_config_repository(store, instance_id="x")
    assert isinstance(log_enterprise._inner._codec, DbFlatSectionCodec)

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    mem_personal = create_memory_config_repository(store)
    assert isinstance(mem_personal._inner._codec, YamlSectionCodec)
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    mem_enterprise = create_memory_config_repository(store, instance_id="x")
    assert isinstance(mem_enterprise._inner._codec, DbBodySectionCodec)

@pytest.mark.asyncio
async def test_heartbeat_browser_a2ui_merge() -> None:
    store = InMemoryPersistentBackend()
    heartbeat = HeartbeatConfigRepository(store, YamlSectionCodec())
    await heartbeat.merge_heartbeat_fields(
        {"every": 120, "target": "web", "active_hours": {"start": "09:00", "end": "18:00"}}
    )
    assert (await heartbeat.get_body())["every"] == 120

    browser = BrowserConfigRepository(store, YamlSectionCodec())
    await browser.merge({"chrome_path": "/usr/bin/chrome", "headless": False})
    body = await browser.get_body()
    assert body["chrome_path"] == "/usr/bin/chrome"
    assert body["headless"] is False

    a2ui = A2uiConfigRepository(store, YamlSectionCodec())
    await a2ui.merge({"enabled": True, "protocol_version": "0.8"})
    assert (await a2ui.get_body())["enabled"] is True


@pytest.mark.asyncio
async def test_preferred_language_scalar_overlay(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "preferred_language: zh\n"
        "browser:\n  headless: true\n"
        "heartbeat:\n  every: 3600\n"
        "a2ui:\n  enabled: false\n",
        encoding="utf-8",
    )
    store = FilePersistentBackend(
        registry=build_gateway_store_registry(config_file=config_path)
    )
    locale = PreferredLanguageConfigRepository(store, YamlSectionCodec())
    assert await locale.get_language() == "zh"
    await locale.set_language("en")
    browser = BrowserConfigRepository(store, YamlSectionCodec())
    await browser.merge({"chrome_path": "C:/chrome.exe"})
    heartbeat = HeartbeatConfigRepository(store, YamlSectionCodec())
    await heartbeat.merge_heartbeat_fields({"target": "feishu"})
    a2ui = A2uiConfigRepository(store, YamlSectionCodec())
    await a2ui.merge({"enabled": True})

    text = config_path.read_text(encoding="utf-8")
    assert "preferred_language: en" in text
    assert "chrome_path:" in text
    assert "target: feishu" in text
    assert "enabled: true" in text.lower() or "enabled: True" in text
    assert "preferred_language:\n  preferred_language:" not in text


def test_new_section_factory_codec_selection(monkeypatch) -> None:
    store = InMemoryPersistentBackend()
    for factory in (
        create_heartbeat_config_repository,
        create_browser_config_repository,
        create_preferred_language_config_repository,
        create_a2ui_config_repository,
    ):
        monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
        personal = factory(store)
        assert isinstance(personal._inner._codec, YamlSectionCodec)
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        try:
            factory(store, instance_id="x")
            raise AssertionError("expected personal-only ValueError")
        except ValueError as exc:
            assert "personal-only" in str(exc)


def test_yaml_only_sections_have_no_db_layout(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("preferred_language: zh\n", encoding="utf-8")
    personal = build_gateway_store_registry(config_file=config_path)
    for name in (
        "heartbeat_config",
        "browser_config",
        "preferred_language_config",
        "a2ui_config",
    ):
        layout = personal.get(name)
        assert layout is not None
        assert layout.file is not None
        assert layout.db is None

    enterprise = build_gateway_store_registry()
    for name in (
        "heartbeat_config",
        "browser_config",
        "preferred_language_config",
        "a2ui_config",
    ):
        assert enterprise.get(name) is None

