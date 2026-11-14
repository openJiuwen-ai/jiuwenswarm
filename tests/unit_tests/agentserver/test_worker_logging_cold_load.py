# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import logging

import pytest

from jiuwenswarm.server.worker.lifecycle import _cold_load_logging_levels


@pytest.mark.asyncio
async def test_enterprise_cold_load_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """企业版：worker 启动时调用 DB 日志级别冷加载。"""
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: True)
    calls: list[bool] = []

    async def _fake_reload() -> None:
        calls.append(True)

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.capabilities.logging."
        "reload_logging_levels_from_gateway_db",
        _fake_reload,
    )

    await _cold_load_logging_levels()
    assert calls == [True]


@pytest.mark.asyncio
async def test_personal_edition_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """非企业版：不读库。"""
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: False)
    calls: list[bool] = []

    async def _fail_reload() -> None:
        calls.append(True)

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.capabilities.logging."
        "reload_logging_levels_from_gateway_db",
        _fail_reload,
    )

    await _cold_load_logging_levels()
    assert calls == []


@pytest.mark.asyncio
async def test_is_enterprise_import_failure_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """is_enterprise 导入失败只告警，不阻断 worker 启动。"""
    monkeypatch.delattr("jiuwenswarm.enterprise.is_enterprise")

    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.server.worker.lifecycle"):
        await _cold_load_logging_levels()

    assert "logging_config cold load skipped" in caplog.text


@pytest.mark.asyncio
async def test_reload_failure_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """读库失败只告警，不阻断 worker 启动。"""
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: True)

    async def _broken_reload() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.capabilities.logging."
        "reload_logging_levels_from_gateway_db",
        _broken_reload,
    )

    await _cold_load_logging_levels()
