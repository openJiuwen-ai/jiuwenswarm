# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host wiring preserves the runtime and its existing security/browser settings."""

import importlib.util
import sys
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserRunGuardrails,
    RuntimeSettings,
)

from jiuwenswarm.agents.harness.common.browser_config import (
    apply_browser_decision_config,
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


@pytest.mark.parametrize(
    "config", [None, {}, {"browser": {"decision": {"mode": "llm"}}}]
)
def test_defaults_keep_existing_settings_identity(settings, config):
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert apply_browser_decision_config(spec, config) is spec
    assert spec.factory_kwargs["settings"] is settings


@requires_jev
@pytest.mark.parametrize("mode", ["llm", "${TEST_JEV_DISABLED_MODE}"])
def test_explicit_llm_disables_policy_when_reusing_a_spec(settings, monkeypatch, mode):
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


def test_shipped_template_is_llm_only_and_embeds_no_credential(settings):
    # Jev is opt-in: a fresh install must work on an openjiuwen without Jev.
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load(
        (root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8")
    )
    raw = config["browser"]["decision"]
    assert "api_key" not in raw
    assert (raw["mode"], raw["provider"], raw["api_key_env"]) == (
        "llm",
        "openrouter",
        "OPENROUTER_API_KEY",
    )
    spec = SimpleNamespace(factory_kwargs={"settings": settings})
    assert apply_browser_decision_config(spec, config) is spec
    assert spec.factory_kwargs["settings"] is settings


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
        pytest.param({"mode": "bad"}, marks=requires_jev),
        pytest.param({"mode": "hybrid", "max_retries": 9}, marks=requires_jev),
        pytest.param({"mode": "hybrid", "api_base": "http://example.test"}, marks=requires_jev),
    ],
)
def test_invalid_configuration_is_explicit(settings, raw):
    with pytest.raises(ValueError):
        apply_browser_decision_config(
            SimpleNamespace(factory_kwargs={"settings": settings}),
            {"browser": {"decision": raw}},
        )


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
