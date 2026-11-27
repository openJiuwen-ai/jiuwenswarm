from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BROWSER_MOVE_SRC = PROJECT_ROOT / "jiuwenclaw" / "agentserver" / "tools" / "browser-move" / "src"
MIDDLEWARE_BASE_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "single_agent"
    / "middleware"
    / "base.py"
)
SERVICE_FILE = BROWSER_MOVE_SRC / "playwright_runtime" / "service.py"
PATCH_TOOL_MANAGER_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "runner"
    / "resources_manager"
    / "tool_manager.py"
)
PATCH_STDIO_CLIENT_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "foundation"
    / "tool"
    / "mcp"
    / "client"
    / "stdio_client.py"
)
PATCH_LLM_CONFIG_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "foundation"
    / "llm"
    / "schema"
    / "config.py"
)
PATCH_REACT_AGENT_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "single_agent"
    / "agents"
    / "react_agent.py"
)
PATCH_REACT_AGENT_EVOLVE_FILE = (
    BROWSER_MOVE_SRC
    / "openjiuwen_patch_sources"
    / "openjiuwen"
    / "core"
    / "single_agent"
    / "agents"
    / "react_agent_evolve.py"
)


def test_browser_runtime_middleware_shim_maps_to_rail_api() -> None:
    source = MIDDLEWARE_BASE_FILE.read_text(encoding="utf-8")

    assert "from openjiuwen.core.single_agent.rail.base import (" in source
    assert "class AgentMiddleware(AgentRail):" in source
    assert '"AgentMiddleware"' in source


def test_browser_service_registers_rails_instead_of_legacy_middleware_api() -> None:
    source = SERVICE_FILE.read_text(encoding="utf-8")

    assert "register_middleware(" not in source
    assert "register_rail(" in source


def test_patch_tool_manager_uses_config_based_mcp_clients() -> None:
    source = PATCH_TOOL_MANAGER_FILE.read_text(encoding="utf-8")

    assert "return StdioClient(config)" in source
    assert "return StreamableHttpClient(config)" in source
    assert "return PlaywrightClient(config)" in source
    assert "config.server_path, config.server_name, config.params" not in source


def test_patch_stdio_client_supports_current_mcp_registration_contract() -> None:
    source = PATCH_STDIO_CLIENT_FILE.read_text(encoding="utf-8")

    assert '__client_name__ = "stdio"' in source
    assert '__client_type__ = "mcp"' in source
    assert "if isinstance(config, McpServerConfig):" in source


def test_patch_model_client_config_includes_custom_headers() -> None:
    source = PATCH_LLM_CONFIG_FILE.read_text(encoding="utf-8")

    assert "custom_headers" in source
    assert "Developer-provided headers merged per LLM call" in source


def test_patch_react_agents_truncate_tool_result_logging() -> None:
    react_source = PATCH_REACT_AGENT_FILE.read_text(encoding="utf-8")
    evolve_source = PATCH_REACT_AGENT_EVOLVE_FILE.read_text(encoding="utf-8")

    assert "_summarize_tool_result" in react_source
    assert "_summarize_tool_result" in evolve_source
    assert 'logger.info(f"Tool result: {tool_result}")' not in react_source
    assert 'logger.info(f"Tool result: {tool_result}")' not in evolve_source
