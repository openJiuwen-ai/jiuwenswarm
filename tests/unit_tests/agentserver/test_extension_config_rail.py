# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for extension_config util, inject path, and debug rail (read-only)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

import pytest

from jiuwenswarm.agents.harness.common.rails.extension_config_debug_rail import (
    ExtensionConfigDebugRail,
)
from jiuwenswarm.agents.harness.common.rails.extension_config_util import (
    filter_agent_extension_config,
    get_extension_config_from_ctx,
    is_extension_config_debug_rail_enabled,
    summarize_extension_config_for_log,
    write_extension_config_into_inputs,
)

_DEBUG_RAIL_LOGGER = "jiuwenswarm.agents.harness.common.rails.extension_config_debug_rail"


@contextmanager
def _capture_debug_rail_logs(
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> Iterator[None]:
    """Attach caplog to the rail logger.

    ``jiuwenswarm`` root sets ``propagate=False``, so pytest's default root
    handler never sees these records unless we hang ``caplog.handler`` on the
    named logger (same pattern as gateway agent_client tests).
    """
    target = logging.getLogger(_DEBUG_RAIL_LOGGER)
    target.addHandler(caplog.handler)
    caplog.set_level(level, logger=_DEBUG_RAIL_LOGGER)
    try:
        yield
    finally:
        target.removeHandler(caplog.handler)


def test_filter_keeps_agent_server_drops_gateway_and_disabled() -> None:
    records = [
        {"template_id": "a", "component": "agent_server", "enabled": True},
        {"template_id": "b", "component": "gateway", "enabled": True},
        {"template_id": "c", "component": "agent_server", "enabled": False},
        {"template_id": "d", "enabled": True},  # missing component -> keep
        "not-a-dict",
    ]
    filtered = filter_agent_extension_config(records)
    assert [r["template_id"] for r in filtered] == ["a", "d"]


def test_filter_empty_input() -> None:
    assert filter_agent_extension_config(None) == []
    assert filter_agent_extension_config([]) == []


def test_get_extension_config_from_run_context_extra() -> None:
    payload = [{"template_id": "tpl-1", "template_name": "demo"}]
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            run_context=SimpleNamespace(extra={"extension_config": payload})
        )
    )
    assert get_extension_config_from_ctx(ctx) == payload


def test_get_extension_config_from_dict_inputs() -> None:
    payload = [{"template_id": "tpl-2"}]
    ctx = SimpleNamespace(inputs={"extension_config": payload})
    assert get_extension_config_from_ctx(ctx) == payload


def test_get_extension_config_missing() -> None:
    assert get_extension_config_from_ctx(SimpleNamespace(inputs=None)) is None
    assert get_extension_config_from_ctx(SimpleNamespace(inputs={})) is None


def test_summarize_extension_config_redacts_hook_config() -> None:
    records = [
        {
            "template_id": "tpl-1",
            "template_name": "限制工具调用",
            "component": "agent_server",
            "hook_type": "pre_request",
            "enabled": True,
            "hook_config": {
                "handler": "hooks.limit_tools",
                "params": {"token": "secret", "allowed_tools": ["bash"]},
            },
        }
    ]
    summary = summarize_extension_config_for_log(records)
    assert summary == [
        {
            "template_id": "tpl-1",
            "template_name": "限制工具调用",
            "component": "agent_server",
            "hook_type": "pre_request",
            "enabled": True,
        }
    ]
    assert "hook_config" not in summary[0]
    assert "secret" not in str(summary)


def test_write_injects_filtered_config_into_run_context_extra() -> None:
    inputs: dict = {}
    written = write_extension_config_into_inputs(
        inputs,
        [
            {"template_id": "a", "component": "agent_server", "enabled": True},
            {"template_id": "b", "component": "gateway", "enabled": True},
            {"template_id": "c", "component": "agent_server", "enabled": False},
        ],
    )
    assert written is not None
    assert [r["template_id"] for r in written] == ["a"]
    assert inputs["extension_config"] == written
    assert inputs["run"]["context"]["extra"]["extension_config"] == written


def test_write_aligns_run_extra_when_top_level_already_set() -> None:
    """顶层已有 extension_config 时仍过滤并写入 run.context.extra（修复早退）。"""
    inputs: dict = {
        "extension_config": [
            {
                "template_id": "a",
                "component": "agent_server",
                "enabled": True,
                "hook_config": {"params": {"token": "secret"}},
            },
            {"template_id": "g", "component": "gateway", "enabled": True},
        ],
        "run": {"context": {"extra": {"cron": {"id": "1"}}}},
    }
    written = write_extension_config_into_inputs(inputs, None)
    assert written is not None
    assert [r["template_id"] for r in written] == ["a"]
    extra = inputs["run"]["context"]["extra"]
    assert extra["extension_config"] == written
    # 不覆盖已有 extra 其它字段
    assert extra["cron"] == {"id": "1"}


def test_write_noop_when_only_gateway_and_no_existing() -> None:
    inputs: dict = {}
    written = write_extension_config_into_inputs(
        inputs,
        [{"template_id": "g", "component": "gateway", "enabled": True}],
    )
    assert written is None
    assert "extension_config" not in inputs


def test_debug_rail_env_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_EXTENSION_CONFIG_DEBUG_RAIL", raising=False)
    assert is_extension_config_debug_rail_enabled() is False


def test_debug_rail_env_gate_accepts_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AGENT_EXTENSION_CONFIG_DEBUG_RAIL", value)
        assert is_extension_config_debug_rail_enabled() is True


@pytest.mark.asyncio
async def test_debug_rail_reads_from_run_context(caplog: pytest.LogCaptureFixture) -> None:
    payload = [
        {
            "template_id": "tpl-1",
            "template_name": "限制工具调用",
            "component": "agent_server",
        }
    ]
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            run_context=SimpleNamespace(extra={"extension_config": payload})
        )
    )
    rail = ExtensionConfigDebugRail()
    with _capture_debug_rail_logs(caplog, logging.INFO):
        await rail.before_invoke(ctx)
    assert "extension_config present" in caplog.text
    assert "限制工具调用" in caplog.text


@pytest.mark.asyncio
async def test_debug_rail_missing_logs_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    ctx = SimpleNamespace(inputs=SimpleNamespace(run_context=SimpleNamespace(extra={})))
    rail = ExtensionConfigDebugRail()
    with _capture_debug_rail_logs(caplog, logging.DEBUG):
        await rail.before_invoke(ctx)
    assert "no extension_config found" in caplog.text


@pytest.mark.asyncio
async def test_debug_rail_tool_call_logs_redacted_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = [
        {
            "template_id": "tpl-1",
            "template_name": "限制工具调用",
            "component": "agent_server",
            "hook_type": "pre_request",
            "hook_config": {"params": {"token": "super-secret"}},
        }
    ]
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            run_context=SimpleNamespace(extra={"extension_config": payload})
        )
    )
    rail = ExtensionConfigDebugRail()
    with _capture_debug_rail_logs(caplog, logging.INFO):
        await rail.before_tool_call(ctx)
    assert "summary=" in caplog.text
    assert "限制工具调用" in caplog.text
    assert "super-secret" not in caplog.text
    assert "hook_config" not in caplog.text
