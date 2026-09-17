# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for team_helpers.query_team_human_members_for_join (路线 B 纯查询契约).

路线 B 后该函数退化为纯查询：不再校验 session_id↔team_name、不拼文案，
只查 team.db 取全部成员（未 role 过滤）。team_name 空/DB miss/DB 异常均
返回空 list；session_id 仅日志。mismatch 与文案由 gateway 负责。
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
    query_team_human_members_for_join,
)


def _members(*names: str) -> list[dict[str, Any]]:
    return [{"member_id": n, "role": "human_agent"} for n in names]


@pytest.mark.anyio
async def test_empty_team_name_returns_empty() -> None:
    """team_name 空 → 直接返回 []，不查 DB。"""
    with mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
        ".TeamMonitorHandler.get_member_list_from_db",
    ) as m_db:
        assert await query_team_human_members_for_join("sess-1", "") == []
        m_db.assert_not_called()


@pytest.mark.anyio
async def test_db_miss_returns_empty() -> None:
    """DB 返回 None（miss / db 不可达）→ 归一为 []。"""
    with mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
        ".TeamMonitorHandler.get_member_list_from_db",
        return_value=None,
    ):
        assert await query_team_human_members_for_join("sess-1", "team-1") == []


@pytest.mark.anyio
async def test_db_returns_members_passes_through_unfiltered() -> None:
    """DB 有成员 → 原样返回，不 role 过滤（交 server/gateway 过滤）。"""
    raw = [
        {"member_id": "reviewer-1", "role": "human_agent"},
        {"member_id": "leader-1", "role": "team_leader"},
    ]
    with mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
        ".TeamMonitorHandler.get_member_list_from_db",
        return_value=raw,
    ) as m_db:
        result = await query_team_human_members_for_join("sess-1", "team-1")
    assert result == raw
    m_db.assert_awaited_once_with("team-1")


@pytest.mark.anyio
async def test_db_returns_empty_list_passes_through() -> None:
    """DB 返回空 list（team 存在但无成员）→ 原样返回 []。"""
    with mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
        ".TeamMonitorHandler.get_member_list_from_db",
        return_value=[],
    ):
        assert await query_team_human_members_for_join("sess-1", "team-1") == []


@pytest.mark.anyio
async def test_db_exception_returns_empty() -> None:
    """DB 抛异常 → 捕获后返回 []，不外泄异常（文案由 gateway 拼"不存在"）。"""
    with mock.patch(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers"
        ".TeamMonitorHandler.get_member_list_from_db",
        side_effect=RuntimeError("db down"),
    ):
        assert await query_team_human_members_for_join("sess-1", "team-1") == []
