from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig

from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@dataclass(frozen=True)
class _Settings:
    mcp_cfg: McpServerConfig


def _adapter(adapter_type: type[Any]) -> Any:
    adapter = object.__new__(adapter_type)
    adapter._workspace_dir = "/workspace"
    adapter._sys_operation = object()
    adapter._model_cache = {}
    adapter._coding_memory_rail = None
    adapter._browser_runtime_settings = None
    return adapter


def _subagent_config() -> dict[str, object]:
    return {
        "subagents": {
            interface_deep.STATUSLINE_SETUP_AGENT_TYPE: {"enabled": False},
            "general_agent": {"enabled": False},
            "research_agent": {"enabled": False},
            "explore_agent": {"enabled": False},
            "plan_agent": {"enabled": False},
            "code_agent": {"enabled": False},
            "browser_agent": {"enabled": True},
        }
    }


@pytest.mark.parametrize(
    ("adapter_type", "module"),
    (
        (JiuWenSwarmDeepAdapter, interface_deep),
        (JiuwenSwarmCodeAdapter, interface_code),
    ),
)
def test_deep_agent_spec_provider_passes_prepared_browser_settings(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[Any],
    module: Any,
) -> None:
    adapter = _adapter(adapter_type)
    captured: dict[str, object] = {}
    original_cfg = McpServerConfig(
        server_id="browser-original",
        server_name="browser-original",
        server_path="stdio://browser-original",
        client_type="stdio",
        params={},
    )
    guarded_cfg = McpServerConfig(
        server_id="browser-guarded",
        server_name="browser-guarded",
        server_path="stdio://browser-guarded",
        client_type="stdio",
        params={},
    )
    profile = SimpleNamespace(network_guard_enforced=True)

    def build_browser(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(factory_kwargs={"settings": _Settings(original_cfg)})

    monkeypatch.setattr(
        interface_deep,
        "apply_browser_runtime_security_profile",
        lambda config: (guarded_cfg, profile),
    )
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(module, "build_browser_agent_config", build_browser)
    if adapter_type is JiuWenSwarmDeepAdapter:
        monkeypatch.setattr(
            adapter,
            "_sync_mcp_credentials_environment",
            lambda: True,
        )
        monkeypatch.setattr(interface_deep, "_load_custom_subagents", lambda **_: [])

    subagents, _ = adapter._build_configured_subagents(
        object(),
        _subagent_config(),
        {},
    )

    assert subagents is not None
    assert "settings" not in captured
    assert captured["sys_operation"] is adapter._sys_operation
    assert subagents[-1].factory_kwargs["settings"].mcp_cfg is guarded_cfg
    assert adapter._browser_runtime_settings.mcp_cfg is guarded_cfg
    if adapter_type is JiuwenSwarmCodeAdapter:
        assert subagents[-1].factory_kwargs["auto_create_workspace"] is False


@pytest.mark.parametrize(
    ("adapter_type", "module"),
    (
        (JiuWenSwarmDeepAdapter, interface_deep),
        (JiuwenSwarmCodeAdapter, interface_code),
    ),
)
def test_default_managed_browser_stays_unverified_and_uses_upstream_settings(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[Any],
    module: Any,
) -> None:
    adapter = _adapter(adapter_type)
    captured_final: dict[str, object] = {}
    settings = SimpleNamespace(
        mcp_cfg=McpServerConfig(
            server_id="playwright_official_stdio",
            server_name="playwright-official",
            server_path="stdio://playwright",
            client_type="stdio",
            params={
                "command": "npx",
                "args": [
                    "-y",
                    "@playwright/mcp@latest",
                    "--caps=pdf,vision,devtools,config,network,storage,testing",
                ],
                "cwd": ".",
                "env": {},
                "timeout_s": 30,
            },
        )
    )

    def build_browser(*args: object, **kwargs: object) -> object:
        captured_final.update(kwargs)
        return SimpleNamespace(factory_kwargs={"settings": settings})

    for name in (
        "BROWSER_DRIVER",
        "PLAYWRIGHT_CDP_URL",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_MCP_CONFIG",
        "PLAYWRIGHT_MCP_EXTENSION",
        "PLAYWRIGHT_MCP_INIT_PAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(adapter, "_resolve_headless_from_config", lambda _: False)
    monkeypatch.setattr(
        adapter, "_resolve_managed_browser_binary_from_config", lambda _: ""
    )
    monkeypatch.setattr(
        adapter, "_resolve_managed_browser_type_from_config", lambda _: "auto"
    )
    monkeypatch.setattr(interface_deep, "build_browser_agent_config", build_browser)
    monkeypatch.setattr(module, "build_browser_agent_config", build_browser)
    if adapter_type is JiuWenSwarmDeepAdapter:
        monkeypatch.setattr(
            adapter,
            "_sync_mcp_credentials_environment",
            lambda: True,
        )
        monkeypatch.setattr(interface_deep, "_load_custom_subagents", lambda **_: [])

    subagents, _ = adapter._build_configured_subagents(
        object(),
        _subagent_config(),
        {},
    )

    assert subagents is not None
    assert adapter._browser_runtime_settings is None
    assert adapter._browser_runtime_security_profile.network_guard_enforced is False
    assert (
        adapter._browser_runtime_security_profile.failure_reason
        == "unsupported_browser_driver"
    )
    assert "settings" not in captured_final
    assert captured_final["sys_operation"] is adapter._sys_operation
    assert subagents[-1].factory_kwargs["settings"] is settings
