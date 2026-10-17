# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the mode-token resolver in memory.config.

Covers is_memory_enabled / is_proactive_memory mapping from various
mode-token formats to the live modes.agent.* / modes.code tree layout.
"""

from typing import Any, Dict, Optional

import pytest


# ---------------------------------------------------------------------------
# Inline copy of the resolver logic to avoid heavy imports.
# Mirror of jiuwenswarm/agents/harness/common/memory/config.py — keep in sync.
# ---------------------------------------------------------------------------

# agent 工作族：旧 token（agent / agent.plan / agent.fast / plan / fast）+
# 新三段命名 agent.work.* canonical。
_AGENT_WORK_TOKENS = frozenset({
    "agent", "agent.plan", "agent.fast", "plan", "fast",
    "agent.work.normal", "agent.work.plan",
})

# code profile 族：旧 code 系（code / code.normal / code.plan / code.team /
# team.plan.code）+ 新三段命名 agent.code.* / team.code.* canonical。
_CODE_PROFILE_TOKENS = frozenset({
    "code", "code.normal", "code.plan", "code.team", "team.plan.code",
    "agent.code.normal", "agent.code.plan", "team.code.normal", "team.code.plan",
})


def _resolve_mode_memory(mode: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    modes_cfg = (config or {}).get("modes", {}) if isinstance(config, dict) else {}
    if not isinstance(modes_cfg, dict):
        return {}
    token = (mode or "").strip()
    if token in _CODE_PROFILE_TOKENS or token.startswith("code."):
        node = modes_cfg.get("code", {})
    elif token in _AGENT_WORK_TOKENS:
        node = modes_cfg.get("agent", {})
    else:
        # team 工作族（team / team.plan / team.plan.normal + 新 team.work.*）没有
        # 对应的记忆配置节点，不落到 modes.agent / modes.code 兜底。
        return {}
    if not isinstance(node, dict):
        return {}
    mem = node.get("memory", {})
    return mem if isinstance(mem, dict) else {}


def is_memory_enabled(mode: str, config: Optional[Dict[str, Any]] = None) -> bool:
    return bool(_resolve_mode_memory(mode, config).get("enabled", False))


def is_proactive_memory(mode: str, config: Optional[Dict[str, Any]] = None) -> bool:
    return bool(_resolve_mode_memory(mode, config).get("is_proactive", False))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg() -> Dict[str, Any]:
    """A config shaped like the real config.yaml — matches what jiuwenswarm ships."""
    return {
        "modes": {
            "agent": {"memory": {"enabled": True, "is_proactive": False}},
            "code": {"memory": {"enabled": True, "is_proactive": True}},
        },
    }


# ---------------------------------------------------------------------------
# Mode-token routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", ["agent.plan", "plan"])
def test_plan_tokens_route_to_merged_agent(cfg, token):
    # plan / fast 已合并为单一 agent 模式，统一读 modes.agent。
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is False


@pytest.mark.parametrize("token", ["agent.fast", "fast"])
def test_fast_tokens_route_to_merged_agent(cfg, token):
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is False


def test_code_token_reads_modes_code(cfg):
    assert is_memory_enabled("code", cfg) is True
    assert is_proactive_memory("code", cfg) is True


@pytest.mark.parametrize("token", ["code.normal", "code.team", "code.anything"])
def test_code_sub_tokens_route_to_modes_code(cfg, token):
    # "code.*" should resolve to modes.code, same as bare "code"
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is True


# 新三段命名 canonical：agent.work.* -> modes.agent；agent.code.* / team.code.*
# -> modes.code；team.work.* -> {}（无节点，保持旧 team 行为）。
@pytest.mark.parametrize("token", ["agent.work.normal", "agent.work.plan"])
def test_new_agent_work_canonical_routes_to_agent_node(cfg, token):
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is False


@pytest.mark.parametrize("token", [
    "agent.code.normal", "agent.code.plan",
    "team.code.normal", "team.code.plan",
])
def test_new_code_canonical_routes_to_code_node(cfg, token):
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is True


@pytest.mark.parametrize("token", ["team.work.normal", "team.work.plan", "team", "team.plan.normal"])
def test_team_work_tokens_have_no_memory_node(cfg, token):
    assert is_memory_enabled(token, cfg) is False
    assert is_proactive_memory(token, cfg) is False


@pytest.mark.parametrize("token", ["weird", ""])
def test_unknown_or_empty_token_returns_disabled(cfg, token):
    assert is_memory_enabled(token, cfg) is False
    assert is_proactive_memory(token, cfg) is False


def test_claw_legacy_path_no_longer_matches():
    # Regression: the old buggy path used modes.claw.* — must NOT match now.
    legacy_cfg = {"modes": {"claw": {"plan": {"memory": {"enabled": True}}}}}
    assert is_memory_enabled("plan", legacy_cfg) is False


# ---------------------------------------------------------------------------
# Defensive — malformed configs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    None,
    {},
    {"modes": "nope"},
    {"modes": {"agent": "bad"}},
    {"modes": {"agent": {"plan": {"memory": "bad"}}}},
])
def test_malformed_configs_return_disabled(cfg):
    assert is_memory_enabled("plan", cfg) is False


def test_agent_tokens_with_missing_agent_memory_are_disabled():
    # 合并后 plan/fast 归一到 modes.agent；modes.agent.memory 缺失时一律 disabled
    # （不再读取 modes.agent.plan.memory 等历史子节点）。
    cfg = {"modes": {"agent": {"plan": {"memory": {"is_proactive": True}}}}}
    assert is_memory_enabled("plan", cfg) is False
    assert is_proactive_memory("plan", cfg) is False


# ---------------------------------------------------------------------------
# Engine × mode boundary — mode-level check is engine-agnostic by design.
# Caller (interface_deep) ANDs it with the engine gate.
# ---------------------------------------------------------------------------

def test_mode_check_is_engine_agnostic(cfg):
    assert is_memory_enabled("plan", cfg) is True
