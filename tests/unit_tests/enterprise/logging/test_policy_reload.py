# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

import pytest

import jiuwenswarm.enterprise.capabilities.logging.policy_reload as policy_reload
from jiuwenswarm.common.utils import SafeRotatingFileHandler, _FILE_HANDLER_LEVEL_MAP
from jiuwenswarm.enterprise.capabilities.logging import (
    reload_logging_levels_from_gateway_db,
)

_DB_ROW = {
    "id": 1,
    "level": "DEBUG",
    "console_level": "WARNING",
    "gateway": None,
    "channel": "ERROR",
    "agent_server": "DEBUG",
    "full": None,
    "revision": 3,
    "created_at": "2026-11-11 00:00:00",
    "updated_at": "2026-11-11 00:00:00",
}


def _patch_db(monkeypatch: pytest.MonkeyPatch, rows_or_error) -> list[tuple]:
    """把 ``jiuwenswarm.enterprise.config.effective.db_queries`` 换成桩模块。

    真实模块链依赖 ``agent_gateway``（dev 环境可能未安装），故直接注入
    sys.modules 桩，与生产侧的懒加载导入方式兼容。返回调用记录 ``[(table, filters)]``。
    """
    calls: list[tuple] = []
    stub = types.ModuleType("jiuwenswarm.enterprise.config.effective.db_queries")

    if isinstance(rows_or_error, Exception):
        async def _list_records(table, **kwargs):
            _ = table, kwargs
            calls.append((table, kwargs))
            raise rows_or_error
    else:
        async def _list_records(table, **kwargs):
            _ = table, kwargs
            calls.append((table, kwargs))
            return rows_or_error

    stub.list_records = _list_records  # type: ignore[attr-defined]
    parent = types.ModuleType("jiuwenswarm.enterprise.config.effective")
    parent.db_queries = stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.enterprise.config.effective", parent)
    monkeypatch.setitem(
        sys.modules, "jiuwenswarm.enterprise.config.effective.db_queries", stub
    )
    return calls


@pytest.mark.asyncio
async def test_enterprise_row_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """企业版 + DB 行存在：行转 payload 并应用，仅保留级别字段。"""
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: True)
    _patch_db(monkeypatch, [_DB_ROW])
    applied: list[dict] = []
    monkeypatch.setattr(
        policy_reload,
        "apply_logging_config_payload",
        lambda payload: applied.append(payload),
    )

    payload = await reload_logging_levels_from_gateway_db()

    assert payload == {
        "level": "DEBUG",
        "console_level": "WARNING",
        "gateway": None,
        "channel": "ERROR",
        "agent_server": "DEBUG",
        "full": None,
    }
    assert applied == [payload]


@pytest.mark.asyncio
async def test_personal_edition_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """非企业版：不读库不应用。"""
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: False)
    calls = _patch_db(monkeypatch, [{"level": "DEBUG"}])

    assert await reload_logging_levels_from_gateway_db() is None
    assert calls == []


@pytest.mark.asyncio
async def test_no_row_keeps_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 无行（Manager 未下发过）：保持 config.yaml 默认级别，不应用。"""
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: True)
    _patch_db(monkeypatch, [])
    applied: list[dict] = []
    monkeypatch.setattr(
        policy_reload,
        "apply_logging_config_payload",
        lambda payload: applied.append(payload),
    )

    assert await reload_logging_levels_from_gateway_db() is None
    assert applied == []


@pytest.mark.asyncio
async def test_db_failure_keeps_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """读库失败：告警并保持 config.yaml 默认级别，不阻断启动。"""
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: True)
    _patch_db(monkeypatch, RuntimeError("db down"))
    applied: list[dict] = []
    monkeypatch.setattr(
        policy_reload,
        "apply_logging_config_payload",
        lambda payload: applied.append(payload),
    )

    assert await reload_logging_levels_from_gateway_db() is None
    assert applied == []


@pytest.mark.asyncio
async def test_enterprise_row_end_to_end_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不 mock apply：真实走 apply_logging_config_payload → update_log_levels。

    level=DEBUG 先覆盖全部组件，再被行的 channel=ERROR 单项覆盖：
    root = min(DEBUG, ERROR, DEBUG, DEBUG) = DEBUG；
    channel.log handler=ERROR、agent_server.log handler=DEBUG、console=WARNING。
    """
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: True)
    _patch_db(monkeypatch, [_DB_ROW])
    policy_reload.apply_logging_config_payload({"op": "delete"})
    try:
        payload = await reload_logging_levels_from_gateway_db()

        assert payload is not None
        root = logging.getLogger("jiuwenswarm")
        assert root.level == logging.DEBUG
        for h in root.handlers:
            # 判断顺序与 update_log_levels 一致：SafeRotatingFileHandler 继承自 StreamHandler
            if isinstance(h, SafeRotatingFileHandler):
                attr = _FILE_HANDLER_LEVEL_MAP[Path(h.baseFilename).name]
                if attr == "channel":
                    assert h.level == logging.ERROR
                else:  # gateway / agent_server / full / permissions 随 level=DEBUG
                    assert h.level == logging.DEBUG
            elif isinstance(h, logging.StreamHandler):
                assert h.level == logging.WARNING
    finally:
        policy_reload.apply_logging_config_payload({"op": "delete"})


@pytest.mark.asyncio
async def test_read_timeout_keeps_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """读库挂起超时：按超时降级告警，保持 config.yaml 默认级别，不阻断启动。"""
    monkeypatch.setattr(policy_reload, "is_enterprise", lambda: True)
    # 压小超时，避免测试真等 5 秒。
    monkeypatch.setattr(policy_reload, "_LOGGING_CONFIG_READ_TIMEOUT", 0.05)
    stub = types.ModuleType("jiuwenswarm.enterprise.config.effective.db_queries")

    async def _hang(table, **kwargs):
        _ = table, kwargs
        await asyncio.sleep(5)  # 远超 0.05s 超时
        return [_DB_ROW]

    stub.list_records = _hang  # type: ignore[attr-defined]
    parent = types.ModuleType("jiuwenswarm.enterprise.config.effective")
    parent.db_queries = stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.enterprise.config.effective", parent)
    monkeypatch.setitem(
        sys.modules, "jiuwenswarm.enterprise.config.effective.db_queries", stub
    )
    applied: list[dict] = []
    monkeypatch.setattr(
        policy_reload,
        "apply_logging_config_payload",
        lambda payload: applied.append(payload),
    )

    assert await reload_logging_levels_from_gateway_db() is None
    assert applied == []
