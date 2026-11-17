# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""permission_runtime: update + check_by_key + check_by_temp_config."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime import (
    PermissionLevel,
    PermissionResult,
    check_permission_by_key,
    check_permission_by_temp_config,
    update_permission_config,
)


def test_update_permission_config_delegates_to_rail() -> None:
    rail = MagicMock()
    cfg = {"enabled": True, "tools": {"bash": "ask"}}
    update_permission_config(rail, cfg)
    rail.update_config.assert_called_once_with(cfg)

    rail.reset_mock()
    update_permission_config(rail, cfg, tool_names=["bash"])
    rail.update_config.assert_called_once_with(cfg, ["bash"])


@pytest.mark.asyncio
async def test_check_by_key_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MagicMock()
    manager.has_config.return_value = False
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.get_permission_manager",
        lambda: manager,
    )
    assert await check_permission_by_key("bash", {}, "group:g/bot:b/user:u") is None
    manager.check_permission.assert_not_called()


@pytest.mark.asyncio
async def test_check_by_key_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = PermissionResult(PermissionLevel.ALLOW, "tools:bash", "")
    manager = MagicMock()
    manager.has_config.return_value = True
    manager.check_permission = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.get_permission_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_checks.get_permission_manager",
        lambda: manager,
    )
    out = await check_permission_by_key("bash", {"cmd": "ls"}, "default")
    assert out is expected
    manager.check_permission.assert_awaited_once_with(
        "bash", {"cmd": "ls"}, "default", config=None
    )


@pytest.mark.asyncio
async def test_check_by_temp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = PermissionResult(PermissionLevel.DENY, "tools:bash", "blocked")
    manager = MagicMock()
    manager.check_permission = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.get_permission_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_checks.get_permission_manager",
        lambda: manager,
    )
    cfg = {"enabled": True, "tools": {"bash": "deny"}}
    out = await check_permission_by_temp_config("bash", {}, cfg)
    assert out is expected
    manager.check_permission.assert_awaited_once_with(
        "bash", {}, None, config=cfg
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "via_key",
    [False, True],
    ids=["temp_config", "keyed"],
)
async def test_check_expands_fetch_urls(
    monkeypatch: pytest.MonkeyPatch,
    via_key: bool,
) -> None:
    """keyed / temp_config 共用 core 的 fetch 展开，url 列表都要拆开判。"""
    allow = PermissionResult(PermissionLevel.ALLOW, "ok", "")
    deny = PermissionResult(PermissionLevel.DENY, "bad", "")
    manager = MagicMock()
    manager.has_config.return_value = True
    manager.check_permission = AsyncMock(side_effect=[allow, deny])
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.get_permission_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_checks.get_permission_manager",
        lambda: manager,
    )
    args = {"url": ["https://a.example", "https://b.example"]}
    if via_key:
        out = await check_permission_by_key("mcp_fetch_webpage", args, "default")
    else:
        out = await check_permission_by_temp_config(
            "mcp_fetch_webpage", args, {"enabled": True}
        )
    assert out is not None
    assert out.permission == PermissionLevel.DENY
    assert manager.check_permission.await_count == 2
    first_args = manager.check_permission.await_args_list[0].args
    second_args = manager.check_permission.await_args_list[1].args
    assert first_args[1]["url"] == "https://a.example"
    assert second_args[1]["url"] == "https://b.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "urls",
    [[], [123], [None], ["   "]],
    ids=["empty_list", "non_string", "none_element", "blank_string"],
)
@pytest.mark.parametrize("via_key", [False, True], ids=["temp_config", "keyed"])
async def test_invalid_fetch_url_list_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    via_key: bool,
    urls: list[object],
) -> None:
    """委托后 core 仍须在展开前拒绝非法 url 列表，且不触达权限引擎。"""
    manager = MagicMock()
    manager.has_config.return_value = True
    manager.check_permission = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.get_permission_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_checks.get_permission_manager",
        lambda: manager,
    )

    args = {"url": urls}
    if via_key:
        out = await check_permission_by_key("mcp_fetch_webpage", args, "default")
    else:
        out = await check_permission_by_temp_config(
            "mcp_fetch_webpage", args, {"enabled": True}
        )

    assert out is not None
    assert out.permission == PermissionLevel.DENY
    assert out.matched_rule == "host:invalid_urls"
    manager.check_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_url_expansion_merges_sdk_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多条 URL 的 warnings / external_paths / ask_accesses 必须求并集。"""
    first = PermissionResult(
        PermissionLevel.ASK,
        "rule:a",
        "",
        warnings=["w1"],
        external_paths=["/a"],
        ask_accesses=[("/a", "read")],
    )
    second = PermissionResult(
        PermissionLevel.DENY,
        "rule:b",
        "blocked",
        warnings=["w2"],
        external_paths=["/b"],
        ask_accesses=[("/b", "write")],
    )
    manager = MagicMock()
    manager.check_permission = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_checks.get_permission_manager",
        lambda: manager,
    )

    out = await check_permission_by_temp_config(
        "mcp_fetch_webpage",
        {"url": ["https://a.example", "https://b.example"]},
        {"enabled": True},
    )

    assert out.permission == PermissionLevel.DENY
    assert out.matched_rule == "rule:b"
    assert out.warnings == ["w1", "w2"]
    assert out.external_paths == ["/a", "/b"]
    assert out.ask_accesses == [("/a", "read"), ("/b", "write")]
