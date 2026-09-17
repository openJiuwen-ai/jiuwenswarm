# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the mode-token resolver in memory.config.

Covers is_memory_enabled / is_proactive_memory mapping from various
mode-token formats to the live modes.agent.* / modes.code tree layout.
"""

from typing import Any, Dict, Optional

import pytest


# ---------------------------------------------------------------------------
# Inline copy of the resolver logic to avoid heavy imports.
# Mirror of jiuwenswarm/agentserver/memory/config.py — keep in sync.
# ---------------------------------------------------------------------------

def _resolve_mode_memory(mode: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    modes_cfg = (config or {}).get("modes", {}) if isinstance(config, dict) else {}
    if not isinstance(modes_cfg, dict):
        return {}
    token = (mode or "").strip()
    if "." in token:
        top, sub = token.split(".", 1)
        # Special handling for "code.*" -> modes.code (ignore sub_mode)
        if top == "code":
            node = modes_cfg.get("code", {})
        else:
            node = modes_cfg.get(top, {})
            if isinstance(node, dict):
                node = node.get(sub, {})
    elif token == "code":
        node = modes_cfg.get("code", {})
    else:
        agent_node = modes_cfg.get("agent", {}) if isinstance(modes_cfg.get("agent"), dict) else {}
        node = agent_node.get(token, {})
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
            "agent": {
                "fast": {"memory": {"enabled": True, "is_proactive": False}},
                "plan": {"memory": {"enabled": True, "is_proactive": True}},
            },
            "code": {
                "memory": {},
            },
        },
    }


# ---------------------------------------------------------------------------
# Mode-token routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", ["agent.plan", "plan"])
def test_plan_tokens_route_to_modes_agent_plan(cfg, token):
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is True


@pytest.mark.parametrize("token", ["agent.fast", "fast"])
def test_fast_tokens_route_to_modes_agent_fast(cfg, token):
    assert is_memory_enabled(token, cfg) is True
    assert is_proactive_memory(token, cfg) is False


def test_code_token_reads_modes_code(cfg):
    # modes.code.memory is empty → disabled
    assert is_memory_enabled("code", cfg) is False
    assert is_proactive_memory("code", cfg) is False


@pytest.mark.parametrize("token", ["code.normal", "code.team", "code.anything"])
def test_code_sub_tokens_route_to_modes_code(cfg, token):
    # "code.*" should resolve to modes.code, same as bare "code"
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


def test_missing_enabled_defaults_to_false_but_proactive_independent():
    cfg = {"modes": {"agent": {"plan": {"memory": {"is_proactive": True}}}}}
    assert is_memory_enabled("plan", cfg) is False
    assert is_proactive_memory("plan", cfg) is True


# ---------------------------------------------------------------------------
# Engine × mode boundary — mode-level check is engine-agnostic by design.
# Caller (interface_deep) ANDs it with the engine gate.
# ---------------------------------------------------------------------------

def test_mode_check_is_engine_agnostic(cfg):
    assert is_memory_enabled("plan", cfg) is True
