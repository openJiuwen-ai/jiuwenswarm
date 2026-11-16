# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the enterprise MCP template boundary on request connectors."""

from __future__ import annotations

from jiuwenswarm.common.mcp_config import (
    CONNECTOR_NOT_IN_TEMPLATE,
    CONNECTOR_TEMPLATE_MISMATCH,
    filter_request_mcp_servers_by_template,
)


def _template_entry(
    name: str = "corp-docs",
    *,
    transport: str = "streamable-http",
    url: str = "https://corp.example.com/mcp",
) -> dict[str, str]:
    return {"name": name, "transport": transport, "url": url, "enabled": "True"}


def test_keeps_matching_connector_with_normalized_transport() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {
            "corp-docs": {
                "type": "streamableHttp",
                "url": " https://corp.example.com/mcp ",
            }
        },
        [_template_entry()],
    )

    assert rejected == {}
    assert kept == {
        "corp-docs": {"type": "streamableHttp", "url": " https://corp.example.com/mcp "}
    }


def test_rejects_connector_missing_from_template() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {"rogue": {"type": "sse", "url": "https://evil.example.com/sse"}},
        [_template_entry()],
    )

    assert kept is None
    assert rejected == {"rogue": CONNECTOR_NOT_IN_TEMPLATE}


def test_rejects_same_name_with_different_url() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {"corp-docs": {"type": "streamable-http", "url": "https://evil.example.com/mcp"}},
        [_template_entry()],
    )

    assert kept is None
    assert rejected == {"corp-docs": CONNECTOR_TEMPLATE_MISMATCH}


def test_rejects_transport_mismatch() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {"corp-docs": {"type": "sse", "url": "https://corp.example.com/mcp"}},
        [_template_entry()],
    )

    assert kept is None
    assert rejected == {"corp-docs": CONNECTOR_TEMPLATE_MISMATCH}


def test_rejects_everything_when_template_is_empty() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {
            "corp-docs": {"type": "streamable-http", "url": "https://corp.example.com/mcp"},
            "other": {"type": "sse", "url": "https://other.example.com/sse"},
        },
        [],
    )

    assert kept is None
    assert rejected == {
        "corp-docs": CONNECTOR_NOT_IN_TEMPLATE,
        "other": CONNECTOR_NOT_IN_TEMPLATE,
    }


def test_keeps_matching_connector_and_drops_unknown_ones() -> None:
    kept, rejected = filter_request_mcp_servers_by_template(
        {
            "corp-docs": {"type": "streamable-http", "url": "https://corp.example.com/mcp"},
            "rogue": {"type": "sse", "url": "https://evil.example.com/sse"},
        },
        [_template_entry()],
    )

    assert kept is not None
    assert set(kept) == {"corp-docs"}
    assert rejected == {"rogue": CONNECTOR_NOT_IN_TEMPLATE}


def test_template_transport_takes_precedence_over_type() -> None:
    """模板同时带 transport / type 时，以建连实际读取的 transport 为准。"""
    kept, rejected = filter_request_mcp_servers_by_template(
        {"corp-docs": {"type": "streamable-http", "url": "https://corp.example.com/mcp"}},
        [
            {
                "name": "corp-docs",
                "transport": "streamable-http",
                "type": "sse",
                "url": "https://corp.example.com/mcp",
            }
        ],
    )

    assert rejected == {}
    assert kept is not None and set(kept) == {"corp-docs"}


def test_stdio_template_entry_never_matches_request_connector() -> None:
    """stdio 模板条目不参与匹配，避免同名请求用任意 command 借模板名通过。"""
    kept, rejected = filter_request_mcp_servers_by_template(
        {
            "corp-local": {
                "command": "/bin/sh",
                "args": ["-c", "id"],
                "cwd": "/tmp",
            }
        },
        [{"name": "corp-local", "transport": "stdio", "enabled": True}],
    )

    assert kept is None
    assert rejected == {"corp-local": CONNECTOR_NOT_IN_TEMPLATE}


def test_no_request_connectors_returns_empty_result() -> None:
    assert filter_request_mcp_servers_by_template(None, [_template_entry()]) == (None, {})
    assert filter_request_mcp_servers_by_template({}, [_template_entry()]) == (None, {})
