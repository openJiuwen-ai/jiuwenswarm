# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Channel 配置 Repository：只谈 PersistentStore，不判断 edition。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.config.channel.codec import ChannelConfigCodec
from jiuwenswarm.gateway.config.channel.models import (
    CHANNEL_CONFIG_STORE_NAME,
    ChannelConfig,
    channels_map,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore


def _with_body(
    existing: ChannelConfig | None,
    channel_id: str,
    body: dict[str, Any],
) -> ChannelConfig:
    """保留 enterprise 行上的 name / type / bot / status，只换 body。"""
    if existing is None:
        return ChannelConfig(channel_id=channel_id, body=body)
    return ChannelConfig(
        channel_id=channel_id,
        body=body,
        channel_name=existing.channel_name,
        channel_type=existing.channel_type,
        bot_id=existing.bot_id,
        status=existing.status,
    )


def _find_xiaoyi_app(
    apps: list[Any],
    api_id: str,
    agent_id: str,
) -> dict[str, Any] | None:
    """``apps[]`` 匹配：api_id → agent_id → is_default → 唯一 app。"""
    api_id = str(api_id or "").strip()
    agent_id = str(agent_id or "").strip()
    if api_id:
        for app in apps:
            if isinstance(app, dict) and str(app.get("api_id") or "").strip() == api_id:
                return app
    if agent_id:
        for app in apps:
            if isinstance(app, dict) and str(app.get("agent_id") or "").strip() == agent_id:
                return app
    for app in apps:
        if isinstance(app, dict) and app.get("is_default", False):
            return app
    if len(apps) == 1 and isinstance(apps[0], dict):
        return apps[0]
    return None


class ChannelConfigRepository:
    """``channel_config`` 的领域读写。不判断 edition。"""

    def __init__(self, store: PersistentStore, codec: ChannelConfigCodec) -> None:
        self._store = store
        self._codec = codec

    async def get(self, channel_id: str) -> ChannelConfig | None:
        row = await self._store.get(
            CHANNEL_CONFIG_STORE_NAME,
            self._codec.identity(channel_id),
        )
        if row is None:
            return None
        return self._codec.from_record(row)

    async def list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ChannelConfig]:
        rows = await self._store.list(
            CHANNEL_CONFIG_STORE_NAME,
            filters=filters,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        return [self._codec.from_record(row) for row in rows]

    async def list_as_map(
        self,
        *,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """返回 ChannelManager 当前使用的 ``{channel_id: body}``。"""
        return channels_map(await self.list(filters=filters))

    async def create(self, config: ChannelConfig) -> ChannelConfig:
        row = await self._store.create(
            CHANNEL_CONFIG_STORE_NAME,
            self._codec.to_record(config),
        )
        return self._codec.from_record(row)

    async def update(self, config: ChannelConfig) -> ChannelConfig | None:
        row = await self._store.update(
            CHANNEL_CONFIG_STORE_NAME,
            self._codec.identity(config.channel_id),
            self._codec.to_updates(config),
        )
        if row is None:
            return None
        return self._codec.from_record(row)

    async def upsert(self, config: ChannelConfig) -> ChannelConfig:
        """有则浅合并更新，无则插入。"""
        updated = await self.update(config)
        if updated is not None:
            return updated
        return await self.create(config)

    async def delete(self, channel_id: str) -> bool:
        return await self._store.delete(
            CHANNEL_CONFIG_STORE_NAME,
            self._codec.identity(channel_id),
        )

    async def merge_body(
        self, channel_id: str, updates: dict[str, Any]
    ) -> ChannelConfig:
        """浅合并顶层字段，对应原来的 ``update_channel_in_config``。"""
        existing = await self.get(channel_id)
        body = dict(existing.body) if existing else {}
        body.update(updates)
        return await self.upsert(_with_body(existing, channel_id, body))

    async def replace_subsection_with_cleanup(
        self,
        channel_id: str,
        subsection_id: str,
        conf: dict[str, Any] | list[Any] | Any,
        keep_keys: set[str],
    ) -> ChannelConfig:
        """整段替换 subsection，并丢掉不在 ``keep_keys`` 里的字段。

        ``store.update`` 浅合并不删键；有多余字段时先 delete 再 create。
        """
        existing = await self.get(channel_id)
        body = dict(existing.body) if existing else {}
        body[subsection_id] = conf
        extra = set(body) - keep_keys
        cleaned = {key: value for key, value in body.items() if key in keep_keys}
        config = _with_body(existing, channel_id, cleaned)
        if existing is None:
            return await self.create(config)
        if extra:
            await self.delete(channel_id)
            return await self.create(config)
        return await self.upsert(config)

    async def merge_or_replace_subsection(
        self,
        channel_id: str,
        subsection_id: str,
        conf: dict[str, Any] | list[Any] | Any,
    ) -> ChannelConfig:
        """dict 合并进 subsection，其它类型整段替换。"""
        existing = await self.get(channel_id)
        body = dict(existing.body) if existing else {}
        if isinstance(conf, dict):
            current = body.get(subsection_id)
            nested = dict(current) if isinstance(current, dict) else {}
            nested.update(conf)
            body[subsection_id] = nested
        else:
            body[subsection_id] = conf
        return await self.upsert(_with_body(existing, channel_id, body))

    async def update_app_fields(
        self,
        channel_id: str,
        app_identifier: str,
        field_values: dict[str, Any],
        *,
        app_id_key: str = "app_id",
    ) -> bool:
        """按 ``app_id`` 改 ``apps[]`` 里一项。找不到返回 False。"""
        existing = await self.get(channel_id)
        if existing is None:
            return False
        apps = existing.body.get("apps")
        if not isinstance(apps, list):
            return False
        found = False
        new_apps: list[Any] = []
        for app in apps:
            if isinstance(app, dict) and app.get(app_id_key) == app_identifier:
                new_apps.append({**app, **field_values})
                found = True
            else:
                new_apps.append(app)
        if not found:
            return False
        body = dict(existing.body)
        body["apps"] = new_apps
        await self.upsert(_with_body(existing, channel_id, body))
        return True

    async def update_xiaoyi_runtime(
        self,
        conf: dict[str, Any],
        *,
        api_id: str = "",
        agent_id: str = "",
    ) -> ChannelConfig:
        """合并小艺运行时字段；有 ``push_id`` 时同步到匹配的 ``apps[]``。"""
        existing = await self.get("xiaoyi")
        body = dict(existing.body) if existing else {}
        body.update(conf)
        push_id = conf.get("push_id")
        if push_id:
            apps = body.get("apps")
            if isinstance(apps, list) and apps:
                target = _find_xiaoyi_app(apps, api_id, agent_id)
                if target is not None:
                    target["push_id"] = str(push_id)
        return await self.upsert(_with_body(existing, "xiaoyi", body))


__all__ = ["ChannelConfigRepository"]
