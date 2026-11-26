# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarmflow 企业策略:解析 / 归一化 / 合并的单元测试(企业最小接入设计方案 §4.2)。"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.enterprise.config.config_provider import (
    _apply_enterprise_policy,
    _merge_swarmflow_policy,
)
from jiuwenswarm.enterprise.config.effective import db_queries
from jiuwenswarm.enterprise.config.effective.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)
from jiuwenswarm.server.runtime.agent_adapter import team_helpers

_POLICY_FULL = {
    "allowed": True,
    "default_enabled": True,
    "max_workflows": 2,
    "agents_per_run": 3,
    "max_agents_total": 4,
    "swarmflow_budget": 1000,
}


def _config_base(*, enable_swarmflow=False, swarmflow_budget=None, policy=None) -> dict:
    team_cfg: dict = {"enable_swarmflow": enable_swarmflow}
    if swarmflow_budget is not None:
        team_cfg["swarmflow_budget"] = swarmflow_budget
    if policy is not None:
        team_cfg["swarmflow"] = policy
    return {"modes": {"team": {"jiuwen_team": team_cfg}}}


# ---------------------------------------------------------------------------
# _resolve_session_swarmflow_config:企业策略 + 会话偏好的解析
# ---------------------------------------------------------------------------


def test_resolver_new_session_uses_policy_default_enabled(monkeypatch) -> None:
    """无 params / 无持久化时,企业 default_enabled 作为新会话缺省。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {},
        _config_base(enable_swarmflow=False, policy={"allowed": True, "default_enabled": True}),
        session_id="s1",
    )
    assert result["enable_swarmflow"] is True


def test_resolver_new_session_falls_back_to_yaml_without_policy_default(monkeypatch) -> None:
    """企业未下发 default_enabled(旧 advanced_config 行)时回落 config.yaml。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {}, _config_base(enable_swarmflow=True), session_id="s1"
    )
    assert result["enable_swarmflow"] is True


def test_resolver_gate_forces_disable_when_forbidden(monkeypatch) -> None:
    """企业 allowed=false 时,即使请求要求开启也强制关闭,但保留用户意图。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": True}, _config_base(policy={"allowed": False}), session_id="s1",
    )
    assert result["enable_swarmflow"] is False
    assert result["swarmflow_allowed"] is False
    assert result["swarmflow_user_enable"] is True


def test_resolver_policy_absent_keeps_spec_base(monkeypatch) -> None:
    """无企业策略(legacy 行)时门禁放行,并发键不注入,由 spec 沿用 yaml 底座。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config({}, _config_base(), session_id="s1")
    assert result["swarmflow_allowed"] is True
    assert "swarmflow_max_workflows" not in result
    assert "swarmflow_agents_per_run" not in result
    assert "swarmflow_max_agents_total" not in result
    assert result["swarmflow_budget"] is None
    assert result["swarmflow_budget_cap"] is None
    assert result["swarmflow_user_budget"] is None


def test_resolver_partial_policy_only_carries_provided_keys(monkeypatch) -> None:
    """企业只下发部分并发键时,未下发的键不进结果(spec 底座兜底)。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {},
        _config_base(policy={"allowed": True, "max_workflows": 4}),
        session_id="s1",
    )
    assert result["swarmflow_max_workflows"] == 4
    assert "swarmflow_agents_per_run" not in result
    assert "swarmflow_max_agents_total" not in result


def test_resolver_params_win_over_persisted_and_yaml(monkeypatch) -> None:
    """优先级:请求 params > 会话持久化 > 缺省。"""
    monkeypatch.setattr(
        team_helpers,
        "restore_session_swarmflow_config",
        lambda sid: {"enable_swarmflow": False, "swarmflow_budget": 50},
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": True, "swarmflow_budget": 100},
        _config_base(policy=_POLICY_FULL),
        session_id="s1",
    )
    assert result["enable_swarmflow"] is True
    assert result["swarmflow_budget"] == 100


def test_resolver_persisted_budget_capped_by_enterprise(monkeypatch) -> None:
    """会话持久化预算超出企业上限时按上限封顶,用户意图保留原值。"""
    monkeypatch.setattr(
        team_helpers,
        "restore_session_swarmflow_config",
        lambda sid: {"enable_swarmflow": True, "swarmflow_budget": 5000},
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {}, _config_base(policy={"allowed": True, "swarmflow_budget": 1000}), session_id="s1",
    )
    assert result["swarmflow_budget"] == 1000
    assert result["swarmflow_budget_cap"] == 1000
    assert result["swarmflow_user_budget"] == 5000


def test_resolver_budget_cap_min_of_user_and_enterprise(monkeypatch) -> None:
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": True, "swarmflow_budget": 500},
        _config_base(policy={"allowed": True, "swarmflow_budget": 200}), session_id="s1",
    )
    assert result["swarmflow_budget"] == 200
    assert result["swarmflow_budget_cap"] == 200
    assert result["swarmflow_user_budget"] == 500


def test_resolver_concurrency_clamps_engine_invariants(monkeypatch) -> None:
    """agents_per_run 钳到 max_agents_total;max_workflows 向下收敛到 max_agents_total。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {},
        _config_base(
            policy={"allowed": True, "max_workflows": 2, "agents_per_run": 10, "max_agents_total": 4}
        ),
        session_id="s1",
    )
    assert result["swarmflow_max_workflows"] == 2
    assert result["swarmflow_agents_per_run"] == 4
    assert result["swarmflow_max_agents_total"] == 4


def test_resolver_concurrency_clamps_workflows_down_to_agents_total(monkeypatch) -> None:
    """企业 max_agents_total 是硬上限：max_workflows 向下收敛，L3 绝不被抬高。"""
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config(
        {},
        _config_base(policy={"allowed": True, "max_workflows": 100, "max_agents_total": 4}),
        session_id="s1",
    )
    assert result["swarmflow_max_workflows"] == 4
    assert result["swarmflow_max_agents_total"] == 4


def test_resolver_worktree_follows_project_dir(monkeypatch) -> None:
    monkeypatch.setattr(
        team_helpers, "restore_session_swarmflow_config", lambda sid: None
    )
    result = team_helpers._resolve_session_swarmflow_config({}, _config_base(), session_id="s1")
    assert result["swarmflow_worktree"] is False
    result = team_helpers._resolve_session_swarmflow_config(
        {}, _config_base(), session_id="s1", project_dir="/tmp/proj"
    )
    assert result["swarmflow_worktree"] is True


# ---------------------------------------------------------------------------
# db_queries:expert_package_template.swarmflow 的归一化与读取
# ---------------------------------------------------------------------------


def test_normalize_swarmflow_maps_swarmflow_budget_key() -> None:
    assert db_queries._normalize_swarmflow({"swarmflow_budget": 1000}) == {"swarmflow_budget": 1000}


def test_normalize_swarmflow_ignores_legacy_budget_key() -> None:
    assert db_queries._normalize_swarmflow({"budget": 500}) is None


def test_normalize_swarmflow_coerces_bool_and_int() -> None:
    out = db_queries._normalize_swarmflow(
        {"allowed": "false", "default_enabled": "true", "max_workflows": "2", "swarmflow_budget": "999"}
    )
    assert out["allowed"] is False
    assert out["default_enabled"] is True
    assert out["max_workflows"] == 2
    assert out["swarmflow_budget"] == 999


def test_normalize_swarmflow_null_and_empty() -> None:
    assert db_queries._normalize_swarmflow(None) is None
    assert db_queries._normalize_swarmflow({}) is None


def test_normalize_swarmflow_invalid_numeric_keys_absent() -> None:
    """数值键无效 / null / 空串一律视为未下发:键不进结果,保留部署底座。"""
    # 全部数值键无效 → 整体视为无策略
    assert db_queries._normalize_swarmflow({"max_workflows": "x", "agents_per_run": 0}) is None
    assert db_queries._normalize_swarmflow(
        {"max_workflows": None, "max_agents_total": "", "swarmflow_budget": None}
    ) is None
    # 有效键保留,无效键剔除
    out = db_queries._normalize_swarmflow({"allowed": True, "max_workflows": None})
    assert out == {"allowed": True}


@pytest.mark.asyncio
async def test_fetch_expert_package_swarmflow_first_non_empty(monkeypatch) -> None:
    rows = [
        {"template_id": "a", "swarmflow": None},
        {"template_id": "b", "swarmflow": json.dumps(_POLICY_FULL)},
    ]
    seen: dict = {}

    async def fake_list_records(table, *, filters=None, order_by=""):
        seen["table"] = table
        seen["filters"] = filters
        return rows

    monkeypatch.setattr(db_queries, "list_records", fake_list_records)
    result = await db_queries.fetch_expert_package_swarmflow(["a", "b"])
    assert result["allowed"] is True
    assert result["swarmflow_budget"] == 1000
    assert seen["table"] == "expert_package_template"
    assert seen["filters"]["template_id"] == ["a", "b"]
    assert seen["filters"]["enabled"] is True


@pytest.mark.asyncio
async def test_fetch_expert_package_swarmflow_follows_ref_order(monkeypatch) -> None:
    """命中顺序跟随入参引用顺序,与存储层返回顺序无关。"""
    policy_a = {"allowed": False, "max_workflows": 1}
    policy_b = {"allowed": True, "max_workflows": 9}
    rows = [
        {"template_id": "b", "swarmflow": json.dumps(policy_b)},
        {"template_id": "a", "swarmflow": json.dumps(policy_a)},
    ]

    async def fake_list_records(table, *, filters=None, order_by=""):
        return rows

    monkeypatch.setattr(db_queries, "list_records", fake_list_records)
    result = await db_queries.fetch_expert_package_swarmflow(["a", "b"])
    assert result == {"allowed": False, "max_workflows": 1}


@pytest.mark.asyncio
async def test_fetch_expert_package_swarmflow_skips_bad_json(monkeypatch) -> None:
    async def fake_list_records(table, *, filters=None, order_by=""):
        return [{"swarmflow": "not-json"}]

    monkeypatch.setattr(db_queries, "list_records", fake_list_records)
    assert await db_queries.fetch_expert_package_swarmflow(["a"]) is None


@pytest.mark.asyncio
async def test_fetch_expert_package_swarmflow_empty_ids_skips_query(monkeypatch) -> None:
    called = False

    async def fake_list_records(table, *, filters=None, order_by=""):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(db_queries, "list_records", fake_list_records)
    assert await db_queries.fetch_expert_package_swarmflow([]) is None
    assert called is False


# ---------------------------------------------------------------------------
# config_provider:策略合并进 config 快照
# ---------------------------------------------------------------------------


def test_apply_enterprise_policy_fails_closed_when_load_failed() -> None:
    """swarmflow 策略加载失败 ≠ 策略缺省:解析侧按 fail-closed 强制关门。"""
    loaded = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        swarmflow=None,
        swarmflow_load_failed=True,
    )
    merged = _apply_enterprise_policy({}, loaded)
    swarm = (
        ((merged.get("modes") or {}).get("team") or {}).get("jiuwen_team") or {}
    ).get("swarmflow")
    assert swarm == {"allowed": False}


def test_apply_enterprise_policy_keeps_policy_without_failure_flag() -> None:
    """加载成功且无策略时不注入 allowed=False(缺省放行语义不变)。"""
    loaded = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        swarmflow=None,
    )
    merged = _apply_enterprise_policy({}, loaded)
    jiuwen_team = ((merged.get("modes") or {}).get("team") or {}).get("jiuwen_team") or {}
    assert "swarmflow" not in jiuwen_team


def test_merge_swarmflow_policy_writes_nested_config() -> None:
    base = {"modes": {"team": {"jiuwen_team": {}}}}
    merged = _merge_swarmflow_policy(base, {"allowed": False, "max_workflows": 1})
    swarm = merged["modes"]["team"]["jiuwen_team"]["swarmflow"]
    assert swarm == {"allowed": False, "max_workflows": 1}
    # 原 base 不被污染(深拷贝)
    assert "swarmflow" not in base["modes"]["team"]["jiuwen_team"]


def test_merge_swarmflow_policy_preserves_local_keys() -> None:
    base = {"modes": {"team": {"jiuwen_team": {"swarmflow": {"budget": 1}}}}}
    merged = _merge_swarmflow_policy(base, {"allowed": True})
    assert merged["modes"]["team"]["jiuwen_team"]["swarmflow"] == {"budget": 1, "allowed": True}


# ---------------------------------------------------------------------------
# schemas:EffectiveEnterpriseConfig.swarmflow 字段
# ---------------------------------------------------------------------------


def test_effective_enterprise_config_swarmflow_field() -> None:
    routing = RoutingContext(group_id="g", bot_id="b", user_id="u")
    cfg = EffectiveEnterpriseConfig(routing=routing)
    assert cfg.swarmflow is None
    cfg = EffectiveEnterpriseConfig(
        routing=routing, swarmflow={"allowed": True}
    )
    assert cfg.as_dict()["swarmflow"] == {"allowed": True}


def test_effective_enterprise_config_swarmflow_type() -> None:
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(EffectiveEnterpriseConfig)}
    assert fields["swarmflow"].type.startswith("dict")
