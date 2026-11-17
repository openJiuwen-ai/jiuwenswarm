# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""a2ui_config effective 读库：有行 / 无行 / 查询失败。"""

from __future__ import annotations

import pytest

from jiuwenswarm.enterprise.config.effective import db_queries
from jiuwenswarm.enterprise.config.effective.loader import _fetch_a2ui_config_safe


@pytest.mark.asyncio
async def test_fetch_a2ui_config_returns_row(monkeypatch) -> None:
    row = {
        "agent_template_id": "tpl-1",
        "enabled": True,
        "protocol_version": "0.8",
        "stream_validation_enabled": True,
        "dev_smoke_tools_enabled": False,
        "data": None,
    }

    async def _list_records(table, *, filters=None, order_by=""):
        assert table == "a2ui_config"
        assert filters == {"agent_template_id": "tpl-1"}
        return [row]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    got = await db_queries.fetch_a2ui_config("tpl-1")
    assert got == row
    assert got is not row


@pytest.mark.asyncio
async def test_fetch_a2ui_config_returns_none_when_missing(monkeypatch) -> None:
    async def _list_records(table, *, filters=None, order_by=""):
        return []

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    assert await db_queries.fetch_a2ui_config("tpl-missing") is None
    assert await db_queries.fetch_a2ui_config("") is None
    assert await db_queries.fetch_a2ui_config("   ") is None


@pytest.mark.asyncio
async def test_fetch_a2ui_config_safe_fail_closed_on_error(monkeypatch) -> None:
    async def _boom(_agent_template_id: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_queries, "fetch_a2ui_config", _boom)
    assert (
        await _fetch_a2ui_config_safe("tpl-1", resource_id="bot-1")
    ) is None


@pytest.mark.asyncio
async def test_fetch_a2ui_config_safe_passes_through_row(monkeypatch) -> None:
    row = {"agent_template_id": "tpl-1", "enabled": False}

    async def _ok(_agent_template_id: str):
        return row

    monkeypatch.setattr(db_queries, "fetch_a2ui_config", _ok)
    assert await _fetch_a2ui_config_safe("tpl-1", resource_id="bot-1") == row
