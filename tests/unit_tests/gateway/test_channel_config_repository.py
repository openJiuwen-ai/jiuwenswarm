# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ChannelConfigRepository 与装配层 Codec 选型。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.gateway.config.channel import (
    ChannelConfig,
    ChannelConfigRepository,
    DbRowChannelCodec,
    YamlMapChannelCodec,
    channels_map,
)
from jiuwenswarm.gateway.storage.backends.file_persistent import FilePersistentBackend
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend
from jiuwenswarm.gateway.storage_assembly.layouts import build_gateway_store_registry

_YAML_CONFIG = """\
logging:
  level: INFO
channels:
  web:
    send_file_allowed: true
  feishu:
    send_file_allowed: true
    apps:
      - name: 飞书默认应用
        enabled: false
"""


def _yaml_repo(store) -> ChannelConfigRepository:
    return ChannelConfigRepository(store, YamlMapChannelCodec())


def _db_repo(store) -> ChannelConfigRepository:
    return ChannelConfigRepository(store, DbRowChannelCodec())


def test_yaml_codec_roundtrip() -> None:
    codec = YamlMapChannelCodec()
    config = ChannelConfig(
        channel_id="web",
        body={"send_file_allowed": True, "enabled": True},
    )
    record = codec.to_record(config)
    assert record == {
        "id": "web",
        "send_file_allowed": True,
        "enabled": True,
    }
    assert codec.identity("web") == {"id": "web"}
    decoded = codec.from_record(record)
    assert decoded.channel_id == "web"
    assert decoded.body == {"send_file_allowed": True, "enabled": True}


def test_db_codec_roundtrip() -> None:
    codec = DbRowChannelCodec()
    config = ChannelConfig(
        channel_id="web",
        body={"send_file_allowed": True},
        channel_name="Web",
        channel_type="web",
        bot_id="bot-1",
        status="active",
    )
    record = codec.to_record(config)
    assert record["channel_id"] == "web"
    assert record["config"] == {"send_file_allowed": True}
    assert "id" not in record
    assert codec.identity("web") == {"channel_id": "web"}
    decoded = codec.from_record(
        {
            **record,
            "id": 12,
            "jiuwenclaw_id": "inst-1",
        }
    )
    assert decoded.channel_id == "web"
    assert decoded.body == {"send_file_allowed": True}
    assert decoded.channel_name == "Web"
    assert decoded.status == "active"


@pytest.mark.asyncio
async def test_yaml_repo_list_as_map_matches_channel_manager_shape() -> None:
    store = InMemoryPersistentBackend()
    await store.create(
        "channel_config",
        {"id": "web", "send_file_allowed": True},
    )
    await store.create(
        "channel_config",
        {"id": "feishu", "send_file_allowed": True, "apps": [{"name": "a"}]},
    )
    repo = _yaml_repo(store)

    mapping = await repo.list_as_map()
    assert mapping == {
        "web": {"send_file_allowed": True},
        "feishu": {"send_file_allowed": True, "apps": [{"name": "a"}]},
    }
    got = await repo.get("feishu")
    assert got is not None
    assert got.channel_id == "feishu"
    assert got.body["apps"][0]["name"] == "a"


@pytest.mark.asyncio
async def test_yaml_repo_upsert_update_delete() -> None:
    store = InMemoryPersistentBackend()
    repo = _yaml_repo(store)

    created = await repo.upsert(
        ChannelConfig(channel_id="telegram", body={"enabled": False, "bot_token": ""})
    )
    assert created.channel_id == "telegram"
    assert created.body["enabled"] is False

    updated = await repo.upsert(
        ChannelConfig(channel_id="telegram", body={"enabled": True})
    )
    assert updated.body["enabled"] is True
    # 浅合并：未出现在 updates 里的字段仍在
    row = await store.get("channel_config", {"id": "telegram"})
    assert row is not None
    assert row["bot_token"] == ""
    assert row["enabled"] is True

    assert await repo.delete("telegram") is True
    assert await repo.get("telegram") is None


@pytest.mark.asyncio
async def test_db_repo_list_active_and_as_map() -> None:
    store = InMemoryPersistentBackend()
    await store.create(
        "channel_config",
        {
            "channel_id": "web",
            "channel_name": "Web",
            "channel_type": "web",
            "bot_id": "default",
            "config": {"send_file_allowed": True},
            "status": "active",
        },
    )
    await store.create(
        "channel_config",
        {
            "channel_id": "feishu",
            "channel_name": "Feishu",
            "channel_type": "feishu",
            "bot_id": "bot-a",
            "config": {"apps": []},
            "status": "inactive",
        },
    )
    repo = _db_repo(store)

    active = await repo.list(filters={"status": "active"})
    assert [item.channel_id for item in active] == ["web"]
    assert channels_map(active) == {"web": {"send_file_allowed": True}}

    mapping = await repo.list_as_map()
    assert set(mapping) == {"web", "feishu"}
    assert mapping["feishu"] == {"apps": []}


@pytest.mark.asyncio
async def test_personal_file_backend_reads_channels_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_YAML_CONFIG, encoding="utf-8")
    store = FilePersistentBackend(
        registry=build_gateway_store_registry(config_file=config_file),
    )
    repo = _yaml_repo(store)

    mapping = await repo.list_as_map()
    assert mapping["web"] == {"send_file_allowed": True}
    assert mapping["feishu"]["send_file_allowed"] is True
    assert mapping["feishu"]["apps"][0]["name"] == "飞书默认应用"

    updated = await repo.update(
        ChannelConfig(channel_id="web", body={"enabled": True})
    )
    assert updated is not None
    assert updated.body["enabled"] is True
    assert updated.body["send_file_allowed"] is True

    text = config_file.read_text(encoding="utf-8")
    assert "level: INFO" in text
    web = await repo.get("web")
    assert web is not None
    assert web.body["send_file_allowed"] is True
    assert web.body["enabled"] is True


@pytest.mark.asyncio
async def test_merge_body_and_replace_subsection_cleanup() -> None:
    store = InMemoryPersistentBackend()
    repo = _yaml_repo(store)
    await repo.create(
        ChannelConfig(
            channel_id="feishu",
            body={"send_file_allowed": True, "app_id": "old", "apps": []},
        )
    )

    merged = await repo.merge_body("telegram", {"bot_token": "t1", "enabled": False})
    assert merged.body["bot_token"] == "t1"

    cleaned = await repo.replace_subsection_with_cleanup(
        "feishu",
        "apps",
        [{"name": "a", "app_id": "cli_a"}],
        {"apps", "send_file_allowed"},
    )
    assert cleaned.body == {
        "send_file_allowed": True,
        "apps": [{"name": "a", "app_id": "cli_a"}],
    }
    assert "app_id" not in cleaned.body


@pytest.mark.asyncio
async def test_update_app_fields_and_xiaoyi_runtime() -> None:
    store = InMemoryPersistentBackend()
    repo = _yaml_repo(store)
    await repo.create(
        ChannelConfig(
            channel_id="feishu",
            body={
                "apps": [
                    {"app_id": "cli_a", "last_chat_id": ""},
                    {"app_id": "cli_b", "last_chat_id": ""},
                ]
            },
        )
    )
    assert await repo.update_app_fields(
        "feishu", "cli_b", {"last_chat_id": "oc_1"}
    )
    feishu = await repo.get("feishu")
    assert feishu is not None
    assert feishu.body["apps"][0]["last_chat_id"] == ""
    assert feishu.body["apps"][1]["last_chat_id"] == "oc_1"

    await repo.create(
        ChannelConfig(
            channel_id="xiaoyi",
            body={
                "apps": [
                    {"api_id": "api-1", "agent_id": "ag-1", "push_id": ""},
                ]
            },
        )
    )
    await repo.update_xiaoyi_runtime(
        {"last_session_id": "s1", "push_id": "p1"},
        api_id="api-1",
    )
    xiaoyi = await repo.get("xiaoyi")
    assert xiaoyi is not None
    assert xiaoyi.body["last_session_id"] == "s1"
    assert xiaoyi.body["apps"][0]["push_id"] == "p1"


@pytest.mark.asyncio
async def test_file_backend_replace_subsection_writes_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "channels:\n  feishu:\n    send_file_allowed: true\n    app_id: old\n    apps: []\n",
        encoding="utf-8",
    )
    store = FilePersistentBackend(
        registry=build_gateway_store_registry(config_file=config_file),
    )
    repo = _yaml_repo(store)
    await repo.replace_subsection_with_cleanup(
        "feishu",
        "apps",
        [{"name": "新应用", "app_id": "cli_new"}],
        {"apps", "send_file_allowed"},
    )
    mapping = await repo.list_as_map()
    assert mapping["feishu"]["apps"][0]["app_id"] == "cli_new"
    assert "app_id" not in mapping["feishu"]
    text = config_file.read_text(encoding="utf-8")
    assert "cli_new" in text
    assert "app_id: old" not in text


@pytest.mark.asyncio
async def test_create_channel_config_repository_selects_codec(monkeypatch) -> None:
    from jiuwenswarm.gateway.storage_assembly import create_channel_config_repository

    store = InMemoryPersistentBackend()
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    personal = create_channel_config_repository(store)
    await personal.upsert(ChannelConfig(channel_id="web", body={"enabled": True}))
    yaml_row = await store.get("channel_config", {"id": "web"})
    assert yaml_row is not None
    assert yaml_row["id"] == "web"
    assert yaml_row["enabled"] is True

    db_store = InMemoryPersistentBackend()
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    enterprise = create_channel_config_repository(
        db_store, instance_id=""
    )
    await enterprise.upsert(ChannelConfig(channel_id="web", body={"enabled": True}))
    db_row = await db_store.get("channel_config", {"channel_id": "web"})
    assert db_row is not None
    assert db_row["channel_id"] == "web"
    assert "jiuwenclaw_id" not in db_row
    assert db_row["config"] == {"enabled": True}
