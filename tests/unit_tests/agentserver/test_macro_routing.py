# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for lightweight MACRO scheduler (LAS-inspired)."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.macro_routing.config import load_macro_routing_config
from jiuwenswarm.agents.harness.macro_routing.gate import route_with_gate
from jiuwenswarm.agents.harness.macro_routing.router import route_macro_mode
from jiuwenswarm.agents.harness.macro_routing.schemas import (
    MACRO_MODES,
    is_auto_mode,
    normalize_macro_mode,
)


def test_normalize_and_auto_aliases():
    assert MACRO_MODES == frozenset({"agent", "team"})
    assert normalize_macro_mode("performance") == "agent"
    assert normalize_macro_mode("agent.fast") == "agent"
    assert normalize_macro_mode("cluster") == "team"
    assert normalize_macro_mode("planning") == "agent"
    assert normalize_macro_mode("agent.plan") == "agent"
    assert normalize_macro_mode("agent") == "agent"
    assert is_auto_mode("auto")
    assert is_auto_mode("agent.auto")
    assert is_auto_mode("macro.auto")
    assert not is_auto_mode("agent.plan")
    assert not is_auto_mode("team")
    assert not is_auto_mode("agent")


def test_gate_greeting_confident_agent():
    decision = route_with_gate("hello")
    assert decision.mode == "agent"
    assert decision.gate_confident is True
    assert decision.confidence >= 0.72


def test_gate_agent_execution_confident():
    decision = route_with_gate("fix the bug in utils.py and rename the helper")
    assert decision.mode == "agent"
    assert decision.gate_confident is True
    assert decision.confidence >= 0.72


def test_gate_team_markers_confident():
    decision = route_with_gate(
        "Build a full stack feature with frontend and backend specialists working in parallel"
    )
    assert decision.mode == "team"
    assert decision.gate_confident is True


def test_gate_spawn_team_of_agents():
    """Explicit 'team of N agents' should route to Cluster."""
    decision = route_with_gate(
        "Spawn a team of 6 agents working on a full-stack feature in parallel"
    )
    assert decision.mode == "team"
    assert decision.gate_confident is True


@pytest.mark.asyncio
async def test_auto_rules_spawn_team_of_agents():
    decision = await route_macro_mode(
        "Spawn a team of 6 agents working on a full-stack feature in parallel",
        requested_mode="auto",
        config_base={"modes": {"macro_routing": {"enabled": True, "strategy": "rules"}}},
    )
    assert decision.mode == "team"
    assert decision.source == "rules"


@pytest.mark.asyncio
async def test_auto_rules_simple_fix_stays_agent():
    decision = await route_macro_mode(
        "Fix the typo in README.md",
        requested_mode="auto",
        config_base={"modes": {"macro_routing": {"enabled": True, "strategy": "rules"}}},
    )
    assert decision.mode == "agent"
    assert decision.source == "rules"


def test_gate_ambiguous_defaults_to_agent():
    decision = route_with_gate("What do you think about this overall?")
    assert decision.mode == "agent"
    assert decision.gate_confident is False
    assert decision.confidence < 0.72


def test_gate_plan_like_query_stays_agent():
    decision = route_with_gate(
        "Should we redesign the architecture and weigh the tradeoffs for a migration roadmap?"
    )
    assert decision.mode == "agent"
    assert decision.mode != "team"


@pytest.mark.asyncio
async def test_forced_mode_untouched():
    decision = await route_macro_mode(
        "Build a full stack feature with frontend and backend in parallel",
        requested_mode="agent",
        config_base={"modes": {"macro_routing": {"enabled": True, "strategy": "rules"}}},
    )
    assert decision.mode == "agent"
    assert decision.source == "forced"


@pytest.mark.asyncio
async def test_forced_team_untouched():
    decision = await route_macro_mode(
        "Fix the typo in README.md",
        requested_mode="team",
        config_base={"modes": {"macro_routing": {"enabled": True, "strategy": "rules"}}},
    )
    assert decision.mode == "team"
    assert decision.source == "forced"


@pytest.mark.asyncio
async def test_auto_rules_resolves_agent():
    decision = await route_macro_mode(
        "fix the failing test and implement the rename",
        requested_mode="auto",
        config_base={"modes": {"macro_routing": {"enabled": True, "strategy": "rules"}}},
    )
    assert decision.mode == "agent"
    assert decision.source == "rules"


@pytest.mark.asyncio
async def test_auto_disabled_falls_back_to_agent():
    decision = await route_macro_mode(
        "fix the failing test",
        requested_mode="agent.auto",
        config_base={"modes": {"macro_routing": {"enabled": False, "strategy": "hybrid"}}},
    )
    assert decision.mode == "agent"
    assert decision.source == "disabled"


@pytest.mark.asyncio
async def test_hybrid_confident_gate_skips_llm(monkeypatch):
    called = {"llm": False}

    async def _fake_llm(*_args, **_kwargs):
        called["llm"] = True
        raise AssertionError("LLM should not be called when gate is confident")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.macro_routing.router.route_with_llm_scheduler",
        _fake_llm,
    )
    decision = await route_macro_mode(
        "hello",
        requested_mode="auto",
        config_base={
            "modes": {
                "macro_routing": {
                    "enabled": True,
                    "strategy": "hybrid",
                    "confidence_threshold": 0.72,
                }
            }
        },
    )
    assert called["llm"] is False
    assert decision.mode == "agent"
    assert decision.source == "hybrid"
    assert decision.gate_confident is True


@pytest.mark.asyncio
async def test_hybrid_uncertain_escalates_to_llm(monkeypatch):
    async def _fake_llm(query, *, gate, model_name=""):
        from jiuwenswarm.agents.harness.macro_routing.schemas import MacroRoutingDecision

        return MacroRoutingDecision(
            mode="agent",
            confidence=0.8,
            rationale="LLM chose agent mode.",
            source="llm",
            features=dict(gate.features),
            gate_confident=True,
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.macro_routing.router.route_with_llm_scheduler",
        _fake_llm,
    )
    decision = await route_macro_mode(
        "What do you think about this overall?",
        requested_mode="auto",
        config_base={
            "modes": {
                "macro_routing": {
                    "enabled": True,
                    "strategy": "hybrid",
                    "confidence_threshold": 0.72,
                }
            }
        },
    )
    assert decision.mode == "agent"
    assert decision.source == "hybrid"
    assert "LLM" in decision.rationale or "agent" in decision.rationale.lower()


def test_load_macro_routing_config_defaults():
    cfg = load_macro_routing_config({"modes": {}})
    assert cfg["enabled"] is True
    assert cfg["strategy"] == "hybrid"
    assert cfg["confidence_threshold"] == 0.72


def test_resources_config_has_macro_routing_not_as_ui_default():
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    macro = data["modes"]["macro_routing"]
    assert macro["enabled"] is True
    assert macro["strategy"] == "llm"
    # Soft check: DEFAULT_MODE in frontend is Agent (Plan is a separate toggle).
    frontend = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "channels"
        / "web"
        / "frontend"
        / "src"
        / "stores"
        / "sessionStore.ts"
    )
    text = frontend.read_text(encoding="utf-8")
    assert "const DEFAULT_MODE: AgentMode = 'agent'" in text
    assert "'auto'" in text
