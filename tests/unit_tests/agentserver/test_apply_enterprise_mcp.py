# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for apply_enterprise_mcp_to_config."""

from __future__ import annotations

from unittest.mock import patch

from jiuwenswarm.common.mcp_config import build_mcp_server_config
from jiuwenswarm.server.runtime.enterprise_config.apply_mcp import (
    apply_enterprise_mcp_to_config,
    mcp_entity_to_server_entry,
)
from jiuwenswarm.server.runtime.enterprise_config.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def test_apply_enterprise_mcp_replaces_local_servers() -> None:
    """企业槽位加载后整表替换，本地独有 MCP 不再保留。"""
    config_base = {
        "mcp": {
            "servers": [
                {
                    "name": "local-only",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": True,
                },
                {
                    "name": "local-demo",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": True,
                },
            ]
        }
    }
    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        mcp=[
            {
                "template_id": "t1",
                "enabled": True,
                "mcp_entry": {
                    "name": "local-demo",
                    "transport": "SSE",
                    "url": "http://127.0.0.1:9000/sse",
                    "enabled": True,
                },
            },
            {
                "template_id": "t2",
                "enabled": True,
                "mcp_entry": {
                    "name": "remote-tools",
                    "transport": "http",
                    "url": "http://127.0.0.1:9001/mcp",
                    "enabled": True,
                },
            },
        ],
    )

    merged, applied = apply_enterprise_mcp_to_config(config_base, enterprise)
    assert applied is True
    servers = merged["mcp"]["servers"]
    by_name = {item["name"]: item for item in servers}
    assert "local-only" not in by_name
    assert by_name["local-demo"]["transport"] == "sse"
    assert by_name["remote-tools"]["transport"] == "streamable-http"
    assert set(by_name) == {"local-demo", "remote-tools"}


def test_apply_enterprise_mcp_clears_local_when_slot_empty() -> None:
    """槽位已加载但无有效条目时，清空本地 servers。"""
    config_base = {
        "mcp": {
            "servers": [
                {
                    "name": "local-demo",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": True,
                }
            ]
        }
    }
    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        mcp=[],
    )
    merged, applied = apply_enterprise_mcp_to_config(config_base, enterprise)
    assert applied is True
    assert merged["mcp"]["servers"] == []


def test_apply_enterprise_mcp_clears_local_when_slot_not_loaded() -> None:
    """企业配置 mcp 槽位为 None（策略未配）时，仍清空本地 servers。"""
    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        mcp=None,
    )
    base = {
        "mcp": {
            "servers": [{"name": "keep", "transport": "http", "url": "http://x"}]
        }
    }
    merged, applied = apply_enterprise_mcp_to_config(base, enterprise)
    assert applied is True
    assert merged["mcp"]["servers"] == []


def test_clear_local_mcp_servers() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.apply_mcp import (
        clear_local_mcp_servers,
    )

    merged = clear_local_mcp_servers(
        {"mcp": {"servers": [{"name": "x", "transport": "stdio", "command": "c"}]}}
    )
    assert merged["mcp"]["servers"] == []


def test_apply_enterprise_mcp_skips_disabled_templates() -> None:
    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        mcp=[
            {
                "template_id": "t1",
                "enabled": False,
                "mcp_entry": {
                    "name": "disabled-server",
                    "transport": "http",
                    "url": "http://127.0.0.1:9001/mcp",
                },
            }
        ],
    )
    merged, applied = apply_enterprise_mcp_to_config({"mcp": {"servers": []}}, enterprise)
    assert applied is True
    assert merged["mcp"]["servers"] == []


def test_mcp_entity_ignores_entry_enabled_and_forces_true() -> None:
    """模板已启用时，忽略 mcp_entry.enabled，写入 servers 固定 enabled=True。"""
    entry = mcp_entity_to_server_entry(
        {
            "enabled": True,
            "mcp_entry": {
                "name": "x",
                "transport": "http",
                "url": "http://127.0.0.1/mcp",
                "enabled": False,
            },
        }
    )
    assert entry is not None
    assert entry["name"] == "x"
    assert entry["enabled"] is True
    assert entry["transport"] == "streamable-http"
    assert "enabled" in entry


def test_http_transport_alias_registers_as_streamable_http() -> None:
    """TC_MCP_CALL_005：模板 transport=http 经企业合并后须可被 SDK 注册。"""
    entity = {
        "template_id": "sds-24af77be869a-mcp",
        "enabled": True,
        "mcp_entry": {
            "name": "qa-streamable-http",
            "transport": "http",
            "url": "http://192.168.1.96:18016/mcp",
            "timeout_s": 10,
        },
    }
    entry = mcp_entity_to_server_entry(entity)
    assert entry is not None
    assert entry["transport"] == "streamable-http"

    cfg = build_mcp_server_config(entry, server_id_scope="jiuwenswarm")
    assert cfg is not None
    assert cfg.client_type == "streamable-http"
    assert cfg.server_name == "qa-streamable-http"


_WARN = "jiuwenswarm.server.runtime.enterprise_config.apply_mcp.logger.warning"


def test_mcp_entity_warns_when_entity_not_dict() -> None:
    with patch(_WARN) as warn:
        assert mcp_entity_to_server_entry("not-a-dict") is None  # type: ignore[arg-type]
    warn.assert_called_once()
    message = warn.call_args[0][0] % warn.call_args[0][1:]
    assert "not a dict" in message
    assert "str" in message


def test_mcp_entity_warns_when_mcp_entry_missing() -> None:
    with patch(_WARN) as warn:
        assert mcp_entity_to_server_entry({"template_id": "t-missing", "enabled": True}) is None
    warn.assert_called_once()
    message = warn.call_args[0][0] % warn.call_args[0][1:]
    assert "template_id=t-missing" in message
    assert "mcp_entry" in message


def test_mcp_entity_warns_when_mcp_entry_not_dict() -> None:
    with patch(_WARN) as warn:
        assert (
            mcp_entity_to_server_entry(
                {"template_id": "t-list", "enabled": True, "mcp_entry": ["bad"]}
            )
            is None
        )
    warn.assert_called_once()
    message = warn.call_args[0][0] % warn.call_args[0][1:]
    assert "template_id=t-list" in message
    assert "list" in message


def test_mcp_entity_warns_when_name_empty() -> None:
    with patch(_WARN) as warn:
        assert (
            mcp_entity_to_server_entry(
                {
                    "template_id": "t-empty",
                    "enabled": True,
                    "mcp_entry": {"name": "  ", "transport": "http", "url": "http://x"},
                }
            )
            is None
        )
    warn.assert_called_once()
    message = warn.call_args[0][0] % warn.call_args[0][1:]
    assert "template_id=t-empty" in message
    assert "mcp_entry.name is empty" in message


def test_mcp_entity_disabled_template_does_not_warn() -> None:
    with patch(_WARN) as warn:
        assert (
            mcp_entity_to_server_entry(
                {
                    "template_id": "t-off",
                    "enabled": False,
                    "mcp_entry": {
                        "name": "disabled-server",
                        "transport": "http",
                        "url": "http://x",
                    },
                }
            )
            is None
        )
    warn.assert_not_called()


def test_apply_enterprise_mcp_warns_for_non_dict_entity() -> None:
    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        mcp=["broken"],  # type: ignore[list-item]
    )
    with patch(_WARN) as warn:
        merged, applied = apply_enterprise_mcp_to_config({"mcp": {"servers": []}}, enterprise)
    assert applied is True
    assert merged["mcp"]["servers"] == []
    warn.assert_called_once()
    message = warn.call_args[0][0] % warn.call_args[0][1:]
    assert "not a dict" in message


def test_enterprise_mcp_template_headers_map_to_sdk_auth_headers() -> None:
    """企业模板 ``mcp_entry.headers`` 必须进 SDK ``auth_headers``，否则 AS→MCP 401。

    对齐现场 TC_MCP_CALL_002：Bearer 写在模板 headers，不放在 chat 请求头。
    """
    entity = {
        "template_id": "sds-8f1d60f974d1-mcp",
        "enabled": True,
        "mcp_entry": {
            "name": "qa-bearer-streamable-http",
            "transport": "streamable-http",
            "url": "http://192.168.1.96:18016/mcp",
            "headers": {
                "Authorization": "Bearer sds-dev-mcp-bearer-token",
            },
            "timeout_s": 10,
        },
    }
    entry = mcp_entity_to_server_entry(entity)
    assert entry is not None
    assert entry["headers"]["Authorization"] == "Bearer sds-dev-mcp-bearer-token"

    cfg = build_mcp_server_config(entry, server_id_scope="jiuwenswarm")
    assert cfg is not None
    assert cfg.client_type == "streamable-http"
    assert cfg.auth_headers == {
        "Authorization": "Bearer sds-dev-mcp-bearer-token",
    }
    # 不得再只塞进 params.headers（SDK 客户端不读该字段）
    assert "headers" not in (cfg.params or {})
    assert cfg.params.get("timeout_s") == 10

    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(
            group_id="sds-8f1d60f974d1-group",
            bot_id="sds-8f1d60f974d1-resource",
            user_id="sds-8f1d60f974d1-user",
        ),
        mcp=[entity],
    )
    merged, applied = apply_enterprise_mcp_to_config({"mcp": {"servers": []}}, enterprise)
    assert applied is True
    built = build_mcp_server_config(
        merged["mcp"]["servers"][0], server_id_scope="jiuwenswarm"
    )
    assert built is not None
    assert built.auth_headers.get("Authorization") == (
        "Bearer sds-dev-mcp-bearer-token"
    )
