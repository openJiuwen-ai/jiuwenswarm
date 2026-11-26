# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""loader.load_effective_enterprise_config 槽位集合容错回归。

DEFAULT_AGENT_LOAD_SLOTS 是「TemplateRefSlot 枚举 + react 裸字符串」的混合集合
（e2a 协议包登记 TemplateRefSlot.REACT 前的仓内接线），loader 必须对两种元素
形态都能取值收敛，不能在 ``slot.value`` 上炸掉。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.enterprise.config.effective import DEFAULT_AGENT_LOAD_SLOTS
from jiuwenswarm.enterprise.config.effective import loader
from jiuwenswarm.enterprise.config.effective.loader import (
    load_effective_enterprise_config,
)


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        user_id="u1",
        metadata={"routing": {"group_id": "g1", "bot_id": "b1"}},
    )


def _stub_db(monkeypatch) -> None:
    """挡掉 DB 读取，让加载流程走到 react 槽位实体。"""
    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(
        loader.db_queries,
        "list_records",
        _fake_list_records,
    )
    monkeypatch.setattr(
        loader.db_queries,
        "fetch_advanced_config",
        _fake_advanced_config,
    )
    monkeypatch.setattr(
        loader.db_queries,
        "fetch_templates_by_slot",
        _fake_fetch_templates_by_slot,
    )
    # 实例/模板行有进程级 TTL 缓存，逐用例失效避免跨用例串数据
    loader.invalidate_enterprise_config_caches()


async def _fake_list_records(table: str, *, filters=None, order_by: str = ""):
    if table == "instance_agent_resource":
        return [{"resource_id": "b1", "ref_template_id": "at1", "enabled": True}]
    if table == "agent_template":
        return [
            {
                "template_id": "at1",
                "enabled": True,
                "template_ref": {"react": ["r1"]},
            }
        ]
    return []


async def _fake_advanced_config(agent_template_id: str):
    return None


async def _fake_fetch_templates_by_slot(slot: str, template_ids: list[str]):
    if slot == "react":
        return [{"template_id": "r1", "data": {"max_iterations": 100}}]
    if slot == "mcp":
        return [{"template_id": "m1", "data": {}}]
    return []


@pytest.mark.asyncio
async def test_load_degrades_when_react_query_fails(monkeypatch) -> None:
    """react 查询抛错（如 react_template 表未建）时降级，其余槽位不受拖累。

    回归背景：react_template 表随 gateway 建表 PR 发版，表未就绪时 SQL 层
    报错曾直接 raise，配置了 react 引用的实例整体配置加载失败。
    """
    _stub_db(monkeypatch)

    async def _react_query_boom(slot: str, template_ids: list[str]):
        if slot == "react":
            raise RuntimeError("no such table: react_template")
        if slot == "mcp":
            return [{"template_id": "m1", "data": {}}]
        return []

    monkeypatch.setattr(
        loader.db_queries,
        "fetch_templates_by_slot",
        _react_query_boom,
    )

    template_ref = {"react": ["r1"], "mcp": ["m1"]}
    monkeypatch.setattr(
        loader.db_queries,
        "list_records",
        _fake_list_records_with_refs(template_ref),
    )

    result = await load_effective_enterprise_config(
        _request(), DEFAULT_AGENT_LOAD_SLOTS
    )

    assert result is not None
    # react 槽位降级为未加载，mcp 槽位正常生效
    assert result.react is None
    assert result.mcp == [{"template_id": "m1", "data": {}}]


def _fake_list_records_with_refs(template_ref: dict):
    async def _list_records(table: str, *, filters=None, order_by: str = ""):
        if table == "instance_agent_resource":
            return [{"resource_id": "b1", "ref_template_id": "at1", "enabled": True}]
        if table == "agent_template":
            return [
                {
                    "template_id": "at1",
                    "enabled": True,
                    "template_ref": template_ref,
                }
            ]
        return []

    return _list_records


@pytest.mark.asyncio
async def test_load_degrades_when_execution_guard_query_fails(monkeypatch) -> None:
    """execution_guard_template 表未建时跳过该槽位，其余槽位仍加载。"""
    _stub_db(monkeypatch)

    async def _guard_query_boom(slot: str, template_ids: list[str]):
        if slot == "execution_guard":
            raise RuntimeError("no such table: execution_guard_template")
        if slot == "mcp":
            return [{"template_id": "m1", "data": {}}]
        return []

    monkeypatch.setattr(loader.db_queries, "fetch_templates_by_slot", _guard_query_boom)
    monkeypatch.setattr(
        loader.db_queries,
        "list_records",
        _fake_list_records_with_refs({"execution_guard": ["g1"], "mcp": ["m1"]}),
    )

    result = await load_effective_enterprise_config(
        _request(), DEFAULT_AGENT_LOAD_SLOTS
    )

    assert result is not None
    assert result.execution_guard is None
    assert result.mcp == [{"template_id": "m1", "data": {}}]


@pytest.mark.asyncio
async def test_load_execution_guard_slot_returns_template_row(monkeypatch) -> None:
    """execution_guard 裸字符串槽位进入加载链路，实体落到 result.execution_guard。

    降级用例在槽位被 normalize 或 load_slots 丢掉时也会得到 None。
    这里要求查询确实以该槽位被调用，并带回模板行。
    """
    _stub_db(monkeypatch)
    guard_row = {
        "template_id": "g1",
        "enabled": True,
        "body": {
            "model_anomaly_detection_rail": {"transient_max_retries": 9},
        },
    }
    calls: list[tuple[str, list[str]]] = []

    async def _fetch(slot: str, template_ids: list[str]):
        calls.append((slot, list(template_ids)))
        if slot == "execution_guard":
            return [guard_row]
        return []

    monkeypatch.setattr(loader.db_queries, "fetch_templates_by_slot", _fetch)
    monkeypatch.setattr(
        loader.db_queries,
        "list_records",
        _fake_list_records_with_refs({"execution_guard": ["g1"]}),
    )

    result = await load_effective_enterprise_config(
        _request(), DEFAULT_AGENT_LOAD_SLOTS
    )

    assert result is not None
    assert ("execution_guard", ["g1"]) in calls
    assert result.execution_guard == [guard_row]


@pytest.mark.asyncio
async def test_load_with_mixed_enum_and_str_slots(monkeypatch) -> None:
    """混合槽位集合（枚举 + react 裸字符串）能完整走通加载流程。

    回归背景：loader 曾直接 ``slot.value for slot in slots``，裸字符串
    "react" 触发 AttributeError，企业版每次 Agent 配置加载都会失败。
    """
    _stub_db(monkeypatch)

    result = await load_effective_enterprise_config(
        _request(), DEFAULT_AGENT_LOAD_SLOTS
    )

    assert result is not None
    # react 槽位以字符串参与比较，实体正确落到 result.react
    assert result.react == [{"template_id": "r1", "data": {"max_iterations": 100}}]
    assert result.resource_id == "b1"
    # load_slots 已收敛为字符串集合
    assert "react" in result.debug["load_slots"]
    assert "execution_guard" in result.debug["load_slots"]


@pytest.mark.asyncio
async def test_load_with_pure_enum_slots(monkeypatch) -> None:
    """纯枚举集合（e2a 原始 DEFAULT_AGENT_LOAD_SLOTS）保持原行为。"""
    _stub_db(monkeypatch)

    from e2a.template_slots import TemplateRefSlot

    request = SimpleNamespace(
        user_id="u1",
        metadata={
            "routing": {
                "group_id": "g1",
                "bot_id": "b1",
            }
        },
    )
    monkeypatch.setattr(
        loader.db_queries,
        "list_records",
        _fake_list_records_with_mcp,
    )
    result = await load_effective_enterprise_config(
        request, frozenset({TemplateRefSlot.MCP})
    )

    assert result is not None
    assert result.mcp == [{"template_id": "m1", "data": {}}]


async def _fake_list_records_with_mcp(
    table: str, *, filters=None, order_by: str = ""
):
    if table == "instance_agent_resource":
        return [{"resource_id": "b1", "ref_template_id": "at1", "enabled": True}]
    if table == "agent_template":
        return [
            {
                "template_id": "at1",
                "enabled": True,
                "template_ref": {"mcp": ["m1"]},
            }
        ]
    return []


@pytest.mark.asyncio
async def test_load_slots_converged_to_strings(monkeypatch) -> None:
    """react 裸字符串进入 load_slots 后是字符串 "react"，可被下游比较消费。"""
    _stub_db(monkeypatch)

    result = await load_effective_enterprise_config(
        _request(), frozenset({"react"})
    )

    assert result is not None
    assert result.debug["load_slots"] == ["react"]
    assert result.react is not None
