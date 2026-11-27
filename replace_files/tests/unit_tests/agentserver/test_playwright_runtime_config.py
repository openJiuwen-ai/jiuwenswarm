from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BROWSER_MOVE_SRC = PROJECT_ROOT / "jiuwenclaw" / "agentserver" / "tools" / "browser-move" / "src"
if str(BROWSER_MOVE_SRC) not in sys.path:
    sys.path.insert(0, str(BROWSER_MOVE_SRC))

from playwright_runtime import config as runtime_config


def test_default_launcher_prefers_local_cli(monkeypatch, tmp_path: Path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    cli_path = appdata / "npm" / "node_modules" / "@playwright" / "mcp" / "cli.js"
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text("// stub", encoding="utf-8")

    monkeypatch.delenv("PLAYWRIGHT_MCP_CLI_PATH", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(runtime_config, "resolve_playwright_mcp_cwd", lambda: str(tmp_path / "workspace"))
    monkeypatch.setattr(runtime_config.shutil, "which", lambda name: "C:\\node\\node.exe" if name == "node" else None)

    command, args = runtime_config._default_playwright_mcp_launcher()

    assert command == "C:\\node\\node.exe"
    assert args == [str(cli_path.resolve())]


def test_default_launcher_falls_back_to_npx_without_latest(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PLAYWRIGHT_MCP_CLI_PATH", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.setattr(runtime_config, "resolve_playwright_mcp_cwd", lambda: str(tmp_path / "workspace"))
    monkeypatch.setattr(runtime_config.shutil, "which", lambda name: None)

    command, args = runtime_config._default_playwright_mcp_launcher()

    assert command == "npx"
    assert args == ["-y", "@playwright/mcp"]


def test_build_playwright_mcp_config_passthroughs_npm_network_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime_config, "resolve_playwright_mcp_cwd", lambda: str(tmp_path / "workspace"))
    monkeypatch.setenv("PLAYWRIGHT_MCP_COMMAND", "npx")
    monkeypatch.setenv("PLAYWRIGHT_MCP_ARGS", "-y @playwright/mcp")
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmmirror.com")
    monkeypatch.setenv("NPM_CONFIG_HTTPS_PROXY", "http://127.0.0.1:7890")

    cfg = runtime_config.build_playwright_mcp_config()

    assert cfg.params["command"] == "npx"
    assert cfg.params["args"] == ["-y", "@playwright/mcp"]
    assert cfg.params["env"]["NPM_CONFIG_REGISTRY"] == "https://registry.npmmirror.com"
    assert cfg.params["env"]["NPM_CONFIG_HTTPS_PROXY"] == "http://127.0.0.1:7890"
