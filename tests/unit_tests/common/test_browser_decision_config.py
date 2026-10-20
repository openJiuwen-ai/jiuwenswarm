# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host wiring preserves the runtime and its existing security/browser settings."""

import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserRunGuardrails,
    RuntimeSettings,
)

from jiuwenswarm.agents.harness.common.browser_config import (
    apply_browser_decision_config,
    browser_decision_state,
    browser_decision_updates,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import code_subagents
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep

_JEV_MODULE = "openjiuwen.harness.tools.browser_move.decision"
# jiuwenswarm must also run on an openjiuwen without Jev; only these cases need it.
requires_jev = pytest.mark.skipif(
    importlib.util.find_spec(_JEV_MODULE) is None,
    reason="installed openjiuwen has no Jev decision support",
)


@pytest.fixture
def settings():
    return RuntimeSettings(
        provider="OpenAI",
        api_key="synthetic-not-used",
        api_base="https://example.invalid/v1",
        model_name="original",
        mcp_cfg=McpServerConfig(
            server_id="browser",
            server_name="browser",
            server_path="stdio://playwright",
            client_type="stdio",
            params={"env": {"BROWSER_SESSION": "one"}},
        ),
        guardrails=BrowserRunGuardrails(),
    )


@pytest.fixture
def shipped_config():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3]
    return yaml.safe_load(
        (root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "config", [None, {}, {"browser": {"decision": {"mode": "llm"}}}]
)
def test_defaults_keep_existing_settings_identity(settings, config):
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert apply_browser_decision_config(spec, config) is spec
    assert spec.factory_kwargs["settings"] is settings


@requires_jev
def test_custom_jev_uses_typesafe_wire_format_without_changing_browser_runtime(settings, monkeypatch):
    monkeypatch.setenv("JEV_CUSTOM_API_KEY", "synthetic-custom-key")
    config = {"browser": {"decision": {"mode": "hybrid", "provider": "openrouter", "min_confidence": 0.55}}}
    updates, error = browser_decision_updates(
        "hybrid", "custom", config, model="jev-preview", api_base="https://jev.example.test/v1",
    )
    assert error is None
    config["browser"]["decision"].update(updates)
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    apply_browser_decision_config(spec, config)
    decision = spec.factory_kwargs["settings"].decision
    assert decision.provider == "typesafe"
    assert decision.api_base == "https://jev.example.test/v1"
    assert decision.model == "jev-preview"
    assert decision.api_key_env == "JEV_CUSTOM_API_KEY"
    assert decision.min_confidence == 0.55
    assert spec.factory_kwargs["settings"].mcp_cfg is settings.mcp_cfg
    assert spec.factory_kwargs["settings"].guardrails is settings.guardrails
    assert browser_decision_state(config)["decision_provider"] == "custom"


def test_disabling_invalid_custom_endpoint_remains_possible():
    config = {"browser": {"decision": {"mode": "hybrid", "provider": "custom", "api_base": ""}}}
    assert browser_decision_updates("llm", None, config) == ({"mode": "llm"}, None)


@pytest.mark.parametrize("mode", ["LLM", " llm ", "typo", "HYBRID", "", None, 1, ["hybrid"]])
def test_unrecognized_mode_matches_llm_settings_view(settings, mode, monkeypatch):
    warning = Mock()
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.browser_config.logger.warning", warning)
    config = {"browser": {"decision": {"mode": mode}}}
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert browser_decision_state(config)["decision_mode"] == "llm"
    assert apply_browser_decision_config(spec, config) is spec
    assert spec.factory_kwargs["settings"] is settings
    warning.assert_called_once_with("browser.decision.mode is unrecognized; using llm")


@pytest.mark.parametrize("mode", ["LLM", " llm ", "typo", None])
def test_llm_save_repairs_unrecognized_saved_mode_without_jev(mode, monkeypatch):
    monkeypatch.setitem(sys.modules, _JEV_MODULE, None)
    config = {"browser": {"decision": {"mode": mode, "unknown_option": 1}}}
    assert browser_decision_updates("llm", None, config) == ({"mode": "llm"}, None)
    assert browser_decision_updates(None, None, config) == ({}, None)
    assert config["browser"]["decision"]["mode"] == mode


def test_llm_save_keeps_a_valid_environment_reference(monkeypatch):
    monkeypatch.setenv("TEST_JEV_SAVED_MODE", "llm")
    config = {"browser": {"decision": {"mode": "${TEST_JEV_SAVED_MODE}"}}}
    assert browser_decision_updates("llm", None, config) == ({}, None)


@requires_jev
@pytest.mark.parametrize("mode", ["llm", "${TEST_JEV_DISABLED_MODE}", "LLM", " llm ", "typo"])
def test_llm_mode_or_fallback_disables_policy_when_reusing_a_spec(settings, monkeypatch, mode):
    monkeypatch.setenv("TEST_JEV_DISABLED_MODE", "llm")
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    apply_browser_decision_config(spec, {"browser": {"decision": {"mode": "hybrid"}}})
    apply_browser_decision_config(spec, {"browser": {"decision": {"mode": mode}}})
    updated = spec.factory_kwargs["settings"]
    assert updated.decision.mode == "llm"
    assert updated.mcp_cfg is settings.mcp_cfg
    assert updated.guardrails is settings.guardrails


@requires_jev
def test_openrouter_hybrid_config_uses_existing_credential_reference(settings):
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    apply_browser_decision_config(
        spec,
        {
            "browser": {
                "decision": {
                    "mode": "hybrid",
                    "provider": "openrouter",
                    "api_key_env": "API_KEY",
                }
            }
        },
    )
    decision = spec.factory_kwargs["settings"].decision
    assert decision.provider == "openrouter" and decision.mode == "hybrid"
    assert decision.model == "typesafe/jev-1.13"
    assert decision.api_base == "https://openrouter.ai/api/alpha"
    assert decision.api_key_env == "API_KEY"
    assert spec.factory_kwargs["settings"].mcp_cfg is settings.mcp_cfg


@requires_jev
@pytest.mark.parametrize("provider", ["openrouter", "typesafe"])
def test_separate_head_thresholds_reach_browser_runtime(settings, provider):
    from openjiuwen.harness.tools.browser_move.decision import BrowserDecisionConfig

    if not hasattr(BrowserDecisionConfig, "action_threshold"):
        pytest.skip("installed openjiuwen does not support separate Jev thresholds")
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    apply_browser_decision_config(spec, {"browser": {"decision": {
        "mode": "hybrid", "provider": provider,
        "action_min_confidence": 0.50, "target_min_confidence": 0.60,
    }}})
    configured = spec.factory_kwargs["settings"]
    assert configured.decision.action_threshold == 0.50
    assert configured.decision.target_threshold == 0.60
    assert configured.mcp_cfg is settings.mcp_cfg and configured.guardrails is settings.guardrails


def test_shipped_template_is_llm_only_and_embeds_no_credential(settings, shipped_config):
    # Jev is opt-in: a fresh install must work on an openjiuwen without Jev.
    config = shipped_config
    raw = config["browser"]["decision"]
    assert "api_key" not in raw
    assert raw["min_confidence"] == 0.55
    assert "action_min_confidence" not in raw and "target_min_confidence" not in raw
    assert (raw["mode"], raw["provider"], raw["api_key_env"]) == (
        "llm",
        "openrouter",
        "OPENROUTER_API_KEY",
    )
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert apply_browser_decision_config(spec, config) is spec
    assert spec.factory_kwargs["settings"] is settings


@requires_jev
@pytest.mark.parametrize("mode", ["shadow", "hybrid"])
@pytest.mark.parametrize("provider", ["openrouter", "typesafe"])
def test_shipped_template_enables_jev_with_official_sdk_fields(
    settings, shipped_config, monkeypatch, mode, provider
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-used")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-not-used")
    updates, error = browser_decision_updates(mode, provider, shipped_config)
    assert error is None
    shipped_config["browser"]["decision"].update(updates)
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    apply_browser_decision_config(spec, shipped_config)
    configured = spec.factory_kwargs["settings"]
    assert configured.decision.mode == mode
    assert configured.decision.provider == provider
    assert configured.decision.min_confidence == 0.55
    assert configured.mcp_cfg is settings.mcp_cfg
    assert configured.guardrails is settings.guardrails


@pytest.mark.parametrize("mode", ["shadow", "hybrid"])
def test_jev_mode_on_an_openjiuwen_without_jev_keeps_the_llm_browser_agent(
    settings, monkeypatch, mode
):
    # Until the Jev SDK ships, a Jev config must not stop the browser agent from building.
    monkeypatch.setitem(sys.modules, _JEV_MODULE, None)
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert apply_browser_decision_config(spec, {"browser": {"decision": {"mode": mode}}}) is spec
    assert spec.factory_kwargs["settings"] is settings


@requires_jev
@pytest.mark.parametrize("mode", ["shadow", "hybrid"])
def test_policy_config_preserves_runtime_and_credential_reference(
    settings, mode, monkeypatch
):
    monkeypatch.setenv("TEST_JEV_CONFIG_MODE", mode)
    spec = SimpleNamespace(
        factory_kwargs={"settings": settings, "auto_create_workspace": False}
    )
    apply_browser_decision_config(
        spec,
        {
            "browser": {
                "decision": {
                    "mode": "${TEST_JEV_CONFIG_MODE}",
                    "api_key_env": "TEST_JEV_KEY",
                    "candidate_limit": 12,
                }
            }
        },
    )
    updated = spec.factory_kwargs["settings"]
    assert (
        updated.decision.mode == mode and updated.decision.api_key_env == "TEST_JEV_KEY"
    )
    assert (
        updated.mcp_cfg is settings.mcp_cfg
        and updated.guardrails is settings.guardrails
    )
    assert updated.instance is settings.instance and settings.decision.mode == "llm"
    assert spec.factory_kwargs["auto_create_workspace"] is False


@pytest.mark.parametrize(
    "raw",
    [
        "hybrid",
        pytest.param({"mode": "hybrid", "max_retries": 9}, marks=requires_jev),
        pytest.param({"mode": "hybrid", "api_base": "http://example.test"}, marks=requires_jev),
        pytest.param({"mode": "hybrid", "candidate_limti": 12}, marks=requires_jev),
        pytest.param({"mode": "shadow", "candidate_limti": 12}, marks=requires_jev),
    ],
)
def test_invalid_configuration_is_explicit(settings, raw):
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    with pytest.raises(ValueError, match=r"browser\.decision"):
        apply_browser_decision_config(
            spec,
            {"browser": {"decision": raw}},
        )
    assert spec.factory_kwargs["settings"] is settings


@requires_jev
@pytest.mark.parametrize(
    "adapter_type,module",
    [
        (interface_deep.JiuWenSwarmDeepAdapter, interface_deep),
        (interface_code.JiuwenSwarmCodeAdapter, interface_code),
    ],
)
def test_both_adapters_attach_policy_before_existing_security_preparation(
    settings, monkeypatch, adapter_type, module
):
    adapter = object.__new__(adapter_type)
    adapter._workspace_dir = "/workspace"
    adapter._sys_operation = object()
    adapter._model_cache = {}
    adapter._coding_memory_rail = None
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setenv("BROWSER_DRIVER", "managed")
    monkeypatch.setattr(
        module,
        "build_browser_agent_config",
        lambda *args, **kwargs: SimpleNamespace(factory_kwargs={"settings": settings}),
    )
    prepared = []
    monkeypatch.setattr(
        adapter,
        "_prepare_browser_runtime_security",
        lambda spec: prepared.append(spec.factory_kwargs["settings"].decision.mode),
    )
    if module is interface_deep:
        monkeypatch.setattr(adapter, "_sync_mcp_credentials_environment", lambda: True)
        monkeypatch.setattr(module, "_load_custom_subagents", lambda **kwargs: [])
    subagents = {
        name: {"enabled": False}
        for name in (
            "general_agent",
            "research_agent",
            "explore_agent",
            "plan_agent",
            "code_agent",
            interface_deep.STATUSLINE_SETUP_AGENT_TYPE,
        )
    }
    subagents["browser_agent"] = {"enabled": True}
    result, _ = adapter._build_configured_subagents(
        object(),
        {"subagents": subagents},
        {"browser": {"decision": {"mode": "hybrid"}}},
    )
    assert result[-1].factory_kwargs["settings"].decision.mode == "hybrid"
    assert prepared == ["hybrid"]


@requires_jev
def test_swarm_passes_policy_through_member_runtime_settings(settings, monkeypatch):
    monkeypatch.setattr(
        code_subagents,
        "build_browser_agent_config",
        lambda *args, **kwargs: SimpleNamespace(factory_kwargs={"settings": settings}),
    )
    seen = []

    def prepare(resolved, config, session_id, *, member_id):
        seen.append((resolved.decision.mode, session_id, member_id))
        return resolved

    monkeypatch.setattr(code_subagents, "apply_swarm_browser_settings", prepare)
    ctx = SwarmBuildContext(
        mode="code.team",
        role="teammate",
        member_name="alice",
        session_id="one",
        config={"browser": {"decision": {"mode": "hybrid"}}},
    )
    ctx.extras["_parent_model"] = object()
    spec = code_subagents.build_swarm_browser_agent({}, ctx)
    assert spec.factory_kwargs["settings"].decision.mode == "hybrid"
    assert spec.factory_kwargs["auto_create_workspace"] is False
    assert seen == [("hybrid", "one", "alice")]
