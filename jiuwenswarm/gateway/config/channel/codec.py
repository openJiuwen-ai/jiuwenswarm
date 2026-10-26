# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ChannelConfig ↔ PersistentStore record 的编解码。

personal（YAML map）与 enterprise（表行）落盘形态不同，用不同 Codec 翻译；
Repository 只操作 ChannelConfig，不判断 edition。
"""

from __future__ import annotations

from typing import Any, Protocol

from jiuwenswarm.gateway.config.channel.models import ChannelConfig


class ChannelConfigCodec(Protocol):
    """频道配置编解码协议：领域对象 ChannelConfig ↔ store 一条 record。

    由装配层按 edition 注入具体实现；Repository 只依赖本协议，不写 edition 分支。
    """

    def identity(self, channel_id: str) -> dict[str, Any]:
        """构造 get / update / delete 用的主键条件。"""

    def from_record(self, record: dict[str, Any]) -> ChannelConfig:
        """store 读出的 record → 领域对象。"""

    def to_record(self, config: ChannelConfig) -> dict[str, Any]:
        """领域对象 → create 用的完整 record。"""

    def to_updates(self, config: ChannelConfig) -> dict[str, Any]:
        """领域对象 → update 用的字段（通常不含主键）。"""


class YamlMapChannelCodec:
    """personal：``config.yaml`` ``/channels`` 映射的编解码。

    YAML 形态::

        web:
          send_file_allowed: true

    对应 store record（map key 注入为 ``id``）::

        {"id": "web", "send_file_allowed": True}
    """

    @staticmethod
    def identity(channel_id: str) -> dict[str, Any]:
        return {"id": str(channel_id)}

    @staticmethod
    def from_record(record: dict[str, Any]) -> ChannelConfig:
        row = dict(record)
        channel_id = str(row.pop("id", "") or "")
        return ChannelConfig(channel_id=channel_id, body=row)

    @staticmethod
    def to_record(config: ChannelConfig) -> dict[str, Any]:
        return {"id": config.channel_id, **dict(config.body)}

    @staticmethod
    def to_updates(config: ChannelConfig) -> dict[str, Any]:
        return dict(config.body)


class DbRowChannelCodec:
    """enterprise：``channel_config`` 表行的编解码。

    业务配置在 ``config`` JSON 列；主键为 ``channel_id``（每网关独立 DB）。

    表行示例::

        {
            "channel_id": "web",
            "channel_name": "web",
            "channel_type": "web",
            "bot_id": "",
            "config": {"send_file_allowed": True},
            "status": "active",
        }
    """

    def __init__(self, *, instance_id: str = "") -> None:
        _ = instance_id  # 兼容旧构造参数；不再写入行键

    @staticmethod
    def identity(channel_id: str) -> dict[str, Any]:
        return {"channel_id": str(channel_id)}

    @staticmethod
    def from_record(record: dict[str, Any]) -> ChannelConfig:
        raw_cfg = record.get("config")
        body = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        return ChannelConfig(
            channel_id=str(record.get("channel_id") or ""),
            body=body,
            channel_name=str(record.get("channel_name") or ""),
            channel_type=str(record.get("channel_type") or ""),
            bot_id=str(record.get("bot_id") or ""),
            status=str(record.get("status") or "active"),
        )

    @staticmethod
    def to_record(config: ChannelConfig) -> dict[str, Any]:
        channel_id = config.channel_id
        return {
            "channel_id": channel_id,
            "channel_name": config.channel_name or channel_id,
            "channel_type": config.channel_type or channel_id,
            "bot_id": config.bot_id,
            "config": dict(config.body),
            "status": config.status or "active",
        }

    def to_updates(self, config: ChannelConfig) -> dict[str, Any]:
        record = self.to_record(config)
        record.pop("channel_id", None)
        return record


__all__ = [
    "ChannelConfigCodec",
    "DbRowChannelCodec",
    "YamlMapChannelCodec",
]
