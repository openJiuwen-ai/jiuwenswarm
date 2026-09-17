# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Browser MCP integration helpers for JiuWenSwarm."""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
import importlib.util
from urllib.parse import urlparse
from pathlib import Path
from typing import IO, Any

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.runner import Runner

from jiuwenswarm.common.utils import get_logs_dir

_BROWSER_MCP_DEFAULT_ID = "playwright_runtime_wrapper"
_BROWSER_MCP_DEFAULT_NAME = "playwright-runtime-wrapper"
_SUPPORTED_CLIENT_TYPES = {"stdio", "sse", "streamable-http", "streamable_http", "http"}
_AUTO_SSE_FALLBACK = "BROWSER_RUNTIME_MCP_AUTO_SSE_FALLBACK"
_AUTO_RUNTIME_HOST = "BROWSER_RUNTIME_MCP_HOST"
_AUTO_RUNTIME_PORT = "BROWSER_RUNTIME_MCP_PORT"
_AUTO_RUNTIME_PATH = "BROWSER_RUNTIME_MCP_PATH"
_AUTO_SSE_HOST = "BROWSER_RUNTIME_MCP_SSE_HOST"
_AUTO_SSE_PORT = "BROWSER_RUNTIME_MCP_SSE_PORT"
_AUTO_SSE_PATH = "BROWSER_RUNTIME_MCP_SSE_PATH"
_PROXY_BLOCKLIST = {"http://127.0.0.1:9", "http://localhost:9"}
_BROWSER_RUNTIME_PROCESS: subprocess.Popen[str] | None = None
_BROWSER_RUNTIME_SERVER_URL: str | None = None
_browser_runtime_stdout_handle: IO[str] | None = None
_browser_runtime_stderr_handle: IO[str] | None = None
_BROWSER_MOVE_CLIENT_PATCHED = False
_BROWSER_RUNTIME_STDOUT_LOG = "browser_runtime_stdout.log"
_BROWSER_RUNTIME_STDERR_LOG = "browser_runtime_stderr.log"

logger = logging.getLogger(__name__)


def _browser_runtime_log_paths() -> tuple[Path, Path]:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    return (
        logs_dir / _BROWSER_RUNTIME_STDOUT_LOG,
        logs_dir / _BROWSER_RUNTIME_STDERR_LOG,
    )


def _close_browser_runtime_log_handles() -> None:
    global _browser_runtime_stdout_handle
    global _browser_runtime_stderr_handle

    for attr_name in (
        "_browser_runtime_stdout_handle",
        "_browser_runtime_stderr_handle",
    ):
        handle = globals().get(attr_name)
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:
            pass
        globals()[attr_name] = None


def _env_bool(name: str, default: bool = False) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _parse_args(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
    try:
        return shlex.split(raw, posix=(os.name != "nt"))
    except Exception:
        return raw.split()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _browser_move_server_script() -> Path:
    return (
        _repo_root() / "jiuwenswarm" / "agentserver" / "tools"
        / "browser-move" / "src" / "playwright_runtime_mcp_server.py"
    )


def _browser_move_src_root() -> Path:
    return _repo_root() / "jiuwenswarm" / "agentserver" / "tools" / "browser-move" / "src"


def _normalize_client_type(client_type: str) -> str:
    value = (client_type or "").strip().lower()
    if value in {"http", "streamable_http"}:
        return "streamable-http"
    return value


def _ensure_browser_move_client_patch() -> None:
    global _BROWSER_MOVE_CLIENT_PATCHED
    if _BROWSER_MOVE_CLIENT_PATCHED:
        return

    src_root = _browser_move_src_root()
    if not src_root.exists():
        raise FileNotFoundError(f"browser runtime src root not found: {src_root}")

    # Only add runtime src path. Do not prepend browser-move repo root, otherwise
    # a copied local openjiuwen package may shadow installed openjiuwen.
    src_root_str = str(src_root)
    if src_root_str not in sys.path:
        sys.path.insert(0, src_root_str)

    def _load_class(module_file: Path, module_name: str, class_name: str) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, str(module_file))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load module spec from {module_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, class_name)

    stdio_cls = _load_class(
        src_root / "playwright_runtime" / "clients" / "stdio_client.py",
        "browser_move_stdio_client",
        "BrowserMoveStdioClient",
    )
    sse_module_file = src_root / "playwright_runtime" / "clients" / "sse_client.py"
    sse_cls = None
    if sse_module_file.exists():
        sse_cls = _load_class(
            sse_module_file,
            "browser_move_sse_client",
            "BrowserMoveSseClient",
        )
    streamable_http_cls = _load_class(
        src_root / "playwright_runtime" / "clients" / "streamable_http_client.py",
        "browser_move_streamable_http_client",
        "BrowserMoveStreamableHttpClient",
    )
    apply_patch_fn = _load_class(
        src_root / "playwright_runtime" / "openjiuwen_monkeypatch.py",
        "browser_move_openjiuwen_monkeypatch",
        "apply_openjiuwen_monkeypatch",
    )
    apply_patch_fn()

    import openjiuwen.core.runner.resources_manager.tool_manager as tool_mgr

    original_create_client = tool_mgr.ToolMgr._create_client

    def _patched_create_client(config: McpServerConfig):
        normalized = _normalize_client_type(getattr(config, "client_type", ""))
        if normalized == "sse":
            if sse_cls is not None:
                return sse_cls(config.server_path, config.server_name, config.auth_headers, config.auth_query_params)
            return original_create_client(config)
        if normalized == "streamable-http":
            return streamable_http_cls(
                config.server_path,
                config.server_name,
                config.auth_headers,
                config.auth_query_params,
            )
        if normalized == "stdio":
            return stdio_cls(config.server_path, config.server_name, config.params)
        return original_create_client(config)

    tool_mgr.StdioClient = stdio_cls
    if sse_cls is not None:
        tool_mgr.SseClient = sse_cls
    tool_mgr.StreamableHttpClient = streamable_http_cls
    tool_mgr.ToolMgr._create_client = staticmethod(_patched_create_client)
    _BROWSER_MOVE_CLIENT_PATCHED = True


def _build_browser_runtime_subprocess_env() -> dict[str, str]:
    # Keep full system env on Windows, then override/add the keys we need.
    env: dict[str, str] = dict(os.environ)
    passthrough_keys = [
        "MODEL_NAME",
        "MODEL_PROVIDER",
        "API_KEY",
        "API_BASE",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PLAYWRIGHT_MCP_COMMAND",
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_CDP_URL",
        "PLAYWRIGHT_CDP_HEADERS",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_MCP_CDP_TIMEOUT",
        "PLAYWRIGHT_MCP_BROWSER",
        "PLAYWRIGHT_MCP_DEVICE",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_TOOL_TIMEOUT_S",
        "BROWSER_TIMEOUT_S",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    ]
    for key in passthrough_keys:
        value = os.getenv(key)
        if value:
            env[key] = value

    api_key = (os.getenv("API_KEY") or "").strip()
    api_base = (os.getenv("API_BASE") or "").strip()
    model_provider = (os.getenv("MODEL_PROVIDER") or "").strip().lower()

    # 把本项目的 API_* 透传给浏览器运行时
    if api_key and not env.get("OPENROUTER_API_KEY") and "openrouter.ai" in api_base:
        env["OPENROUTER_API_KEY"] = api_key
    if api_base and not env.get("OPENROUTER_BASE_URL") and "openrouter.ai" in api_base:
        env["OPENROUTER_BASE_URL"] = api_base

    if api_key and not env.get("OPENAI_API_KEY") and "openrouter.ai" not in api_base:
        env["OPENAI_API_KEY"] = api_key
    if api_base and not env.get("OPENAI_BASE_URL") and "openrouter.ai" not in api_base:
        env["OPENAI_BASE_URL"] = api_base

    if model_provider == "openrouter":
        env["MODEL_PROVIDER"] = "openrouter"
    elif model_provider in {"openai", "siliconflow"}:
        env["MODEL_PROVIDER"] = model_provider

    # Remove clearly invalid deny-proxy values that can break child processes.
    for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        proxy_val = (env.get(proxy_key) or "").strip().lower()
        if proxy_val in _PROXY_BLOCKLIST:
            env.pop(proxy_key, None)

    return env


def _runtime_host() -> str:
    return (
        os.getenv(_AUTO_RUNTIME_HOST)
        or os.getenv(_AUTO_SSE_HOST)
        or "127.0.0.1"
    ).strip()


def _runtime_port() -> str:
    return (
        os.getenv(_AUTO_RUNTIME_PORT)
        or os.getenv(_AUTO_SSE_PORT)
        or "8940"
    ).strip()


def _runtime_path(transport: str) -> str:
    env_path = os.getenv(_AUTO_RUNTIME_PATH)
    if not env_path and transport == "sse":
        env_path = os.getenv(_AUTO_SSE_PATH)
    default_path = "/mcp" if _normalize_client_type(transport) == "streamable-http" else "/sse"
    path = (env_path or default_path).strip() or default_path
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _build_server_url(transport: str) -> str:
    host = _runtime_host()
    port = _runtime_port()
    path = _runtime_path(transport)
    return f"http://{host}:{port}{path}"


def _is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_available_port(host: str, preferred_port: int, max_attempts: int = 25) -> int:
    if preferred_port > 0 and _is_port_available(host, preferred_port):
        return preferred_port
    for port in range(preferred_port + 1, preferred_port + max_attempts + 1):
        if _is_port_available(host, port):
            return port
    raise RuntimeError("No available port for browser runtime SSE server.")


def _wait_port_open(host: str, port: int, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.8)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"SSE server did not start in time: {host}:{port}")


def _start_local_server(transport: str, host: str, port: int, path: str) -> str:
    global _BROWSER_RUNTIME_PROCESS
    global _BROWSER_RUNTIME_SERVER_URL

    normalized = _normalize_client_type(transport)
    if normalized not in {"sse", "streamable-http"}:
        raise ValueError(f"Unsupported auto-start transport: {transport}")

    server_script = _browser_move_server_script()
    if not server_script.exists():
        raise FileNotFoundError(f"browser runtime server script not found: {server_script}")

    command = (os.getenv("BROWSER_RUNTIME_MCP_COMMAND") or sys.executable).strip()
    env = _build_browser_runtime_subprocess_env()
    cmd = [
        command,
        str(server_script),
        "--transport",
        normalized,
        "--host",
        host,
        "--port",
        str(port),
        "--path",
        path,
        "--no-banner",
    ]
    stdout_log_path, stderr_log_path = _browser_runtime_log_paths()
    _close_browser_runtime_log_handles()
    stdout_handle = stdout_log_path.open("a", encoding="utf-8")
    stderr_handle = stderr_log_path.open("a", encoding="utf-8")
    try:
        _BROWSER_RUNTIME_PROCESS = subprocess.Popen(
            cmd,
            cwd=str(_repo_root()),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise

    _browser_runtime_stdout_handle = stdout_handle
    _browser_runtime_stderr_handle = stderr_handle
    logger.info(
        "Started browser runtime subprocess: transport=%s url=http://%s:%s%s stdout_log=%s stderr_log=%s",
        normalized,
        host,
        port,
        path,
        stdout_log_path,
        stderr_log_path,
    )
    try:
        _wait_port_open(host, port)
    except Exception:
        stop_local_browser_runtime_server()
        raise
    _BROWSER_RUNTIME_SERVER_URL = f"http://{host}:{port}{path}"
    return _BROWSER_RUNTIME_SERVER_URL


def _parse_local_server_url(server_url: str) -> tuple[str, int, str]:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise ValueError(f"Invalid browser runtime server URL: {server_url}")
    return parsed.hostname, int(parsed.port), parsed.path or "/mcp"


def stop_local_browser_runtime_server() -> None:
    global _BROWSER_RUNTIME_PROCESS
    global _BROWSER_RUNTIME_SERVER_URL

    proc = _BROWSER_RUNTIME_PROCESS
    _BROWSER_RUNTIME_PROCESS = None
    _BROWSER_RUNTIME_SERVER_URL = None

    if proc is None or proc.poll() is not None:
        _close_browser_runtime_log_handles()
        return

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    finally:
        _close_browser_runtime_log_handles()


def restart_local_browser_runtime_server() -> str | None:
    transport = _normalize_client_type(os.getenv("BROWSER_RUNTIME_MCP_CLIENT_TYPE") or "streamable-http")
    current_url = _BROWSER_RUNTIME_SERVER_URL
    current_proc = _BROWSER_RUNTIME_PROCESS

    if transport not in {"sse", "streamable-http"}:
        stop_local_browser_runtime_server()
        return None

    host = _runtime_host()
    path = _runtime_path(transport)
    preferred_port = int(_runtime_port())
    if current_url:
        host, preferred_port, path = _parse_local_server_url(current_url)

    had_local_server = current_url is not None or (current_proc is not None and current_proc.poll() is None)
    stop_local_browser_runtime_server()

    if not had_local_server:
        return None

    # Port may remain in TIME_WAIT after process exit; retry until released.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _is_port_available(host, preferred_port):
            return _start_local_server(transport, host, preferred_port, path)
        time.sleep(0.3)
    raise RuntimeError(
        f"Browser runtime port is still occupied after shutdown: {host}:{preferred_port}"
    )


def _ensure_local_server_started(transport: str) -> str:
    global _BROWSER_RUNTIME_PROCESS
    global _BROWSER_RUNTIME_SERVER_URL
    normalized = _normalize_client_type(transport)
    if normalized not in {"sse", "streamable-http"}:
        raise ValueError(f"Unsupported auto-start transport: {transport}")
    if (
        _BROWSER_RUNTIME_PROCESS is not None
        and _BROWSER_RUNTIME_PROCESS.poll() is None
        and _BROWSER_RUNTIME_SERVER_URL
    ):
        return _BROWSER_RUNTIME_SERVER_URL

    host = _runtime_host()
    preferred_port_raw = _runtime_port()
    path = _runtime_path(normalized)
    preferred_port = int(preferred_port_raw)
    port = _pick_available_port(host, preferred_port)
    return _start_local_server(normalized, host, port, path)


def _build_sse_fallback_config(base_cfg: McpServerConfig, server_url: str | None = None) -> McpServerConfig:
    return McpServerConfig(
        server_id=f"{base_cfg.server_id}_sse",
        server_name=base_cfg.server_name,
        server_path=server_url or _build_server_url("sse"),
        client_type="sse",
    )


def _build_sse_retry_config(base_cfg: McpServerConfig, server_url: str) -> McpServerConfig:
    return McpServerConfig(
        server_id=base_cfg.server_id,
        server_name=base_cfg.server_name,
        server_path=server_url,
        client_type="sse",
    )


def _build_streamable_http_config(base_cfg: McpServerConfig, server_url: str | None = None) -> McpServerConfig:
    return McpServerConfig(
        server_id=base_cfg.server_id,
        server_name=base_cfg.server_name,
        server_path=server_url or _build_server_url("streamable-http"),
        client_type="streamable-http",
    )


def _result_is_ok(result: Any) -> bool:
    if result is None:
        return True
    is_ok = getattr(result, "is_ok", None)
    if callable(is_ok):
        try:
            return bool(is_ok())
        except Exception:
            return False
    return False


def _result_error_text(result: Any) -> str:
    if result is None:
        return ""
    for attr in ("error", "msg"):
        fn = getattr(result, attr, None)
        if callable(fn):
            try:
                value = fn()
                if value is not None:
                    return str(value)
            except Exception:
                pass
    value = getattr(result, "_error", None)
    if value is not None:
        return str(value)
    return str(result)


def build_browser_runtime_mcp_config() -> McpServerConfig | None:
    """Build MCP server config for browser runtime wrapper.

    Env flags:
    - BROWSER_RUNTIME_MCP_ENABLED: 1/0
    - BROWSER_RUNTIME_MCP_CLIENT_TYPE: stdio|sse|streamable-http
    - BROWSER_RUNTIME_MCP_SERVER_PATH: remote MCP endpoint URL
    - BROWSER_RUNTIME_MCP_COMMAND / BROWSER_RUNTIME_MCP_ARGS: stdio command override
    """
    if not _env_bool("BROWSER_RUNTIME_MCP_ENABLED", default=False):
        return None

    server_id = (os.getenv("BROWSER_RUNTIME_MCP_SERVER_ID") or _BROWSER_MCP_DEFAULT_ID).strip()
    server_name = (os.getenv("BROWSER_RUNTIME_MCP_SERVER_NAME") or _BROWSER_MCP_DEFAULT_NAME).strip()
    client_type = _normalize_client_type(os.getenv("BROWSER_RUNTIME_MCP_CLIENT_TYPE") or "streamable-http")

    if client_type not in _SUPPORTED_CLIENT_TYPES:
        raise ValueError(
            "BROWSER_RUNTIME_MCP_CLIENT_TYPE must be one of stdio|sse|streamable-http."
        )

    if client_type == "sse":
        server_path = (os.getenv("BROWSER_RUNTIME_MCP_SERVER_PATH") or _build_server_url("sse")).strip()
        return McpServerConfig(
            server_id=server_id,
            server_name=server_name,
            server_path=server_path,
            client_type="sse",
        )

    if client_type == "streamable-http":
        server_path = (os.getenv("BROWSER_RUNTIME_MCP_SERVER_PATH") or _build_server_url("streamable-http")).strip()
        return McpServerConfig(
            server_id=server_id,
            server_name=server_name,
            server_path=server_path,
            client_type="streamable-http",
        )

    # stdio mode: auto-spawn browser runtime MCP server process.
    server_script = _browser_move_server_script()
    if not server_script.exists():
        raise FileNotFoundError(f"browser runtime server script not found: {server_script}")

    command = (os.getenv("BROWSER_RUNTIME_MCP_COMMAND") or sys.executable).strip()
    args_raw = os.getenv("BROWSER_RUNTIME_MCP_ARGS", "")
    if args_raw.strip():
        args = _parse_args(args_raw)
    else:
        args = [str(server_script), "--transport", "stdio", "--no-banner", "--log-level", "ERROR"]

    params: dict[str, Any] = {
        "command": command,
        "args": args,
        "cwd": str(_repo_root()),
    }
    timeout_raw = (os.getenv("BROWSER_RUNTIME_MCP_TIMEOUT_S") or "300").strip()
    try:
        timeout_s = int(timeout_raw)
        if timeout_s > 0:
            params["timeout_s"] = timeout_s
    except ValueError:
        pass

    subprocess_env = _build_browser_runtime_subprocess_env()
    if subprocess_env:
        params["env"] = subprocess_env

    return McpServerConfig(
        server_id=server_id,
        server_name=server_name,
        server_path=(os.getenv("BROWSER_RUNTIME_MCP_SERVER_PATH") or "stdio://playwright-runtime-wrapper").strip(),
        client_type="stdio",
        params=params,
    )


async def register_browser_runtime_mcp_server(agent: Any, *, tag: str = "agent.main") -> bool:
    """Register browser runtime MCP server and add to agent abilities."""
    _ensure_browser_move_client_patch()
    cfg = build_browser_runtime_mcp_config()
    if cfg is None:
        return False

    async def _register_once(target_cfg: McpServerConfig) -> tuple[bool, str]:
        result = await Runner.resource_mgr.add_mcp_server(target_cfg, tag=tag)
        if _result_is_ok(result):
            agent.ability_manager.add(target_cfg)
            return True, ""

        error_text = _result_error_text(result)
        if "already exist" in error_text.lower():
            agent.ability_manager.add(target_cfg)
            return True, error_text
        return False, error_text

    # Prefer SSE first when stdio fallback is enabled to avoid JSON-RPC corruption
    # from stdout logs in child processes.
    if cfg.client_type == "stdio" and _env_bool(_AUTO_SSE_FALLBACK, default=True):
        sse_cfg = _build_sse_fallback_config(cfg)
        ok, sse_err = await _register_once(sse_cfg)
        if ok:
            return True

        try:
            auto_url = _ensure_local_server_started("sse")
            sse_cfg = _build_sse_fallback_config(cfg, server_url=auto_url)
            ok, auto_sse_err = await _register_once(sse_cfg)
            if ok:
                return True
            sse_err = f"{sse_err} | {auto_sse_err}".strip(" |")
        except Exception as exc:
            sse_err = f"{sse_err} | {exc}".strip(" |")
    else:
        sse_err = ""

    ok, error_text = await _register_once(cfg)
    if ok:
        return True

    if cfg.client_type == "sse":
        try:
            auto_url = _ensure_local_server_started("sse")
            retry_cfg = _build_sse_retry_config(cfg, auto_url)
            ok, retry_err = await _register_once(retry_cfg)
            if ok:
                return True
            error_text = f"{error_text} | {retry_err}".strip(" |")
        except Exception as exc:
            error_text = f"{error_text} | {exc}".strip(" |")
    elif _normalize_client_type(cfg.client_type) == "streamable-http":
        try:
            auto_url = _ensure_local_server_started("streamable-http")
            retry_cfg = _build_streamable_http_config(cfg, auto_url)
            ok, retry_err = await _register_once(retry_cfg)
            if ok:
                return True
            error_text = f"{error_text} | {retry_err}".strip(" |")
        except Exception as exc:
            error_text = f"{error_text} | {exc}".strip(" |")

    if sse_err:
        error_text = f"{error_text} | sse={sse_err}".strip(" |")
    raise RuntimeError(f"Failed to register browser MCP server: {error_text}")
