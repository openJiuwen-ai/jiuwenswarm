# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Browser MCP integration helpers for JiuWenSwarm."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import importlib.util
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from urllib.parse import urlparse
from pathlib import Path
from typing import IO, Any

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.runner import Runner

from jiuwenswarm.common.utils import get_logs_dir
from jiuwenswarm.common.local_env_config import (
    effective_tip,
    export_agent_environ,
    get_local_config,
    resolve_env_ns,
)

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
_BROWSER_MOVE_CLIENT_PATCHED = False

# Model credentials baked into the long-lived browser subprocess at spawn.
# Only these trigger idle restart after hot-reload (skills/embed/etc. ignored).
_MODEL_CREDENTIAL_ENV_KEYS = (
    "API_KEY",
    "API_BASE",
    "MODEL_NAME",
    "MODEL_PROVIDER",
)
# Do not backfill these from bare os.environ — tip / API_* mapping is authoritative.
_SPAWN_ENV_NO_BARE_OS_FALLBACK = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    }
)

TenantKey = tuple[str, str]

# Max concurrent credential generations kept per tenant while busy.
_MAX_SLOTS_PER_TENANT = 3

# Request-scoped pin: chat request keeps using the generation captured at start.
_pinned_runtime_hash: ContextVar[str | None] = ContextVar(
    "browser_runtime_pinned_hash",
    default=None,
)


@dataclass
class BrowserRuntimePin:
    """Opaque handle returned by ``pin_browser_runtime_generation``."""

    token: Token
    tenant_key: TenantKey
    env_hash: str


@dataclass
class BrowserRuntimeSlot:
    """One long-lived browser MCP subprocess for a credential generation."""

    env_hash: str
    process: subprocess.Popen[str] | None = None
    server_url: str | None = None
    stdout_handle: IO[str] | None = None
    stderr_handle: IO[str] | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class TenantRuntimeBag:
    """Per-(service_id, agent_id) multi-generation browser runtime registry."""

    slots: dict[str, BrowserRuntimeSlot] = field(default_factory=dict)
    # env_hash -> number of chat requests currently pinned to this generation
    pin_counts: dict[str, int] = field(default_factory=dict)


_RUNTIMES: dict[TenantKey, TenantRuntimeBag] = {}
_RUNTIMES_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


def _normalize_tenant_key(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> TenantKey:
    return resolve_env_ns(service_id, agent_id)


def _safe_log_token(value: str) -> str:
    text = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))
    return (text or "x")[:64]


def _short_hash_token(env_hash: str) -> str:
    return hashlib.md5(env_hash.encode()).hexdigest()[:8]


def _browser_runtime_log_paths(key: TenantKey, env_hash: str) -> tuple[Path, Path]:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    suffix = (
        f"{_safe_log_token(key[0])}__{_safe_log_token(key[1])}"
        f"__{_short_hash_token(env_hash)}"
    )
    return (
        logs_dir / f"browser_runtime_{suffix}_stdout.log",
        logs_dir / f"browser_runtime_{suffix}_stderr.log",
    )


def _close_slot_log_handles(slot: BrowserRuntimeSlot) -> None:
    for attr in ("stdout_handle", "stderr_handle"):
        handle = getattr(slot, attr)
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:
            pass
        setattr(slot, attr, None)


def _get_or_create_bag(key: TenantKey) -> TenantRuntimeBag:
    with _RUNTIMES_LOCK:
        bag = _RUNTIMES.get(key)
        if bag is None:
            bag = TenantRuntimeBag()
            _RUNTIMES[key] = bag
        return bag


def _get_or_create_slot(key: TenantKey, env_hash: str) -> BrowserRuntimeSlot:
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        slot = bag.slots.get(env_hash)
        if slot is None:
            slot = BrowserRuntimeSlot(env_hash=env_hash)
            bag.slots[env_hash] = slot
        return slot


def _slot_process_alive(slot: BrowserRuntimeSlot) -> bool:
    return slot.process is not None and slot.process.poll() is None


def _tip_credential_value(
    name: str,
    *,
    service_id: str,
    agent_id: str,
    default: str = "",
) -> str:
    tip = effective_tip(service_id, agent_id)
    if name in tip and tip[name] is not None and str(tip[name]) != "":
        return str(tip[name]).strip()
    # Fall through to overlay-aware reader when tip miss (bound request / ns os).
    value = get_local_config(name, default or None)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _browser_env(name: str, default: str = "") -> str:
    value = get_local_config(name, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _model_credential_fingerprint(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Stable fingerprint of tip-backed model credentials for one tenant bag."""
    sid, aid = _normalize_tenant_key(service_id, agent_id)
    return "|".join(
        f"{key}={_tip_credential_value(key, service_id=sid, agent_id=aid)}"
        for key in _MODEL_CREDENTIAL_ENV_KEYS
    )


def resolve_browser_runtime_env_hash(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Return pinned generation for this request, else current tip fingerprint."""
    pinned = _pinned_runtime_hash.get()
    if pinned:
        return pinned
    return _model_credential_fingerprint(service_id=service_id, agent_id=agent_id)


def pin_browser_runtime_generation(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> BrowserRuntimePin:
    """Pin this chat request to the current tip credential generation.

    In-flight requests keep the old generation after tip hot-reload; new requests
    pin the updated tip and route to a new runtime slot.
    """
    key = _normalize_tenant_key(service_id, agent_id)
    env_hash = _model_credential_fingerprint(service_id=key[0], agent_id=key[1])
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        bag.pin_counts[env_hash] = bag.pin_counts.get(env_hash, 0) + 1
    token = _pinned_runtime_hash.set(env_hash)
    return BrowserRuntimePin(token=token, tenant_key=key, env_hash=env_hash)


def reset_browser_runtime_generation(pin: BrowserRuntimePin | None) -> None:
    """Release a request-scoped browser runtime pin."""
    if pin is None:
        return
    try:
        _pinned_runtime_hash.reset(pin.token)
    except ValueError:
        logger.debug("reset browser runtime pin token failed", exc_info=True)
    bag = _get_or_create_bag(pin.tenant_key)
    with _RUNTIMES_LOCK:
        count = bag.pin_counts.get(pin.env_hash, 0)
        if count <= 1:
            bag.pin_counts.pop(pin.env_hash, None)
        else:
            bag.pin_counts[pin.env_hash] = count - 1
    # Opportunistic GC when the last pin on an obsolete generation drops.
    current = _model_credential_fingerprint(
        service_id=pin.tenant_key[0],
        agent_id=pin.tenant_key[1],
    )
    if pin.env_hash != current:
        gc_obsolete_browser_runtime_slots(
            service_id=pin.tenant_key[0],
            agent_id=pin.tenant_key[1],
        )


def list_browser_runtime_env_hashes(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> list[str]:
    """Test/helper: env_hash keys currently registered for one tenant."""
    key = _normalize_tenant_key(service_id, agent_id)
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        return list(bag.slots.keys())


def mark_browser_runtime_stale_if_credentials_changed(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> bool:
    """Return True when any live slot diverges from the current tip fingerprint.

    Multi-generation mode no longer flips a single ``stale`` flag; obsolete
    slots are GC'd at idle (or when pin_count drops to zero).
    """
    key = _normalize_tenant_key(service_id, agent_id)
    current = _model_credential_fingerprint(service_id=key[0], agent_id=key[1])
    bag = _get_or_create_bag(key)
    diverged = False
    with _RUNTIMES_LOCK:
        for env_hash, slot in list(bag.slots.items()):
            if not _slot_process_alive(slot):
                continue
            if env_hash != current:
                diverged = True
                break
    if diverged:
        logger.info(
            "Browser runtime has obsolete generation(s) for tenant=(%s,%s); "
            "new requests use tip hash, old slots kept until idle GC",
            key[0],
            key[1],
        )
    return diverged


def gc_obsolete_browser_runtime_slots(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
    force: bool = False,
) -> int:
    """Stop runtime slots that are not the current tip generation.

    Skips generations that still have request pins unless ``force=True``.
    """
    key = _normalize_tenant_key(service_id, agent_id)
    current = _model_credential_fingerprint(service_id=key[0], agent_id=key[1])
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        victims = []
        for env_hash in list(bag.slots.keys()):
            if env_hash == current:
                continue
            if not force and bag.pin_counts.get(env_hash, 0) > 0:
                continue
            victims.append(env_hash)
    stopped = 0
    for env_hash in victims:
        logger.info(
            "GC obsolete browser runtime for tenant=(%s,%s) env_hash=%s",
            key[0],
            key[1],
            _short_hash_token(env_hash),
        )
        if _stop_slot(key, env_hash):
            stopped += 1
    return stopped


def stop_stale_browser_runtime_if_idle(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> bool:
    """Compatibility wrapper: GC obsolete generations (idle / ensure path)."""
    return (
        gc_obsolete_browser_runtime_slots(
            service_id=service_id,
            agent_id=agent_id,
        )
        > 0
    )


def notify_browser_runtime_after_reload(
    *,
    idle: bool,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """After config reload: keep old generations for pinned requests; GC when idle."""
    mark_browser_runtime_stale_if_credentials_changed(
        service_id=service_id,
        agent_id=agent_id,
    )
    if idle:
        gc_obsolete_browser_runtime_slots(
            service_id=service_id,
            agent_id=agent_id,
        )


def reset_browser_runtime_reload_state_for_tests() -> None:
    """Test helper: drop all runtime slots (call stop_* first to kill processes)."""
    with _RUNTIMES_LOCK:
        _RUNTIMES.clear()
    # Clear any leftover pin ContextVar for the current task.
    try:
        _pinned_runtime_hash.set(None)
    except Exception:
        logger.debug("clear browser runtime pin ContextVar failed", exc_info=True)


def stop_all_browser_runtime_servers() -> None:
    """Stop every tenant browser runtime (tests / shutdown)."""
    with _RUNTIMES_LOCK:
        keys = list(_RUNTIMES.keys())
    for sid, aid in keys:
        stop_local_browser_runtime_server(service_id=sid, agent_id=aid)


def _env_bool(name: str, default: bool = False) -> bool:
    value = _browser_env(name, "").lower()
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


def _tools_dir() -> Path:
    return Path(__file__).resolve().parent


def _repo_root() -> Path:
    # jiuwenswarm/agents/harness/common/tools → repo root is parents[5]
    return Path(__file__).resolve().parents[5]


def _browser_move_server_script() -> Path:
    return _tools_dir() / "browser-move" / "src" / "playwright_runtime_mcp_server.py"


def _browser_move_src_root() -> Path:
    return _tools_dir() / "browser-move" / "src"


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


def _build_browser_runtime_subprocess_env(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, str]:
    sid, aid = _normalize_tenant_key(service_id, agent_id)
    env = dict(export_agent_environ(sid, aid))
    spawn_passthrough_keys = (
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
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    )
    for key in spawn_passthrough_keys:
        if key in env:
            continue
        # Model API aliases come from tip + API_* mapping below — not bare os.environ.
        if key in _SPAWN_ENV_NO_BARE_OS_FALLBACK:
            continue
        value = os.getenv(key)
        if value:
            env[key] = str(value)

    api_key = _tip_credential_value("API_KEY", service_id=sid, agent_id=aid)
    api_base = _tip_credential_value("API_BASE", service_id=sid, agent_id=aid)
    model_provider = _tip_credential_value(
        "MODEL_PROVIDER", service_id=sid, agent_id=aid
    ).lower()
    model_name = _tip_credential_value("MODEL_NAME", service_id=sid, agent_id=aid)
    if model_name and not env.get("MODEL_NAME"):
        env["MODEL_NAME"] = model_name
    if model_provider and not env.get("MODEL_PROVIDER"):
        env["MODEL_PROVIDER"] = model_provider
    if api_key and not env.get("API_KEY"):
        env["API_KEY"] = api_key
    if api_base and not env.get("API_BASE"):
        env["API_BASE"] = api_base

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
        _browser_env(_AUTO_RUNTIME_HOST, "")
        or _browser_env(_AUTO_SSE_HOST, "")
        or "127.0.0.1"
    ).strip()


def _runtime_port() -> str:
    return (
        _browser_env(_AUTO_RUNTIME_PORT, "")
        or _browser_env(_AUTO_SSE_PORT, "")
        or "8940"
    ).strip()


def _runtime_path(transport: str) -> str:
    env_path = _browser_env(_AUTO_RUNTIME_PATH, "")
    if not env_path and transport == "sse":
        env_path = _browser_env(_AUTO_SSE_PATH, "")
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


def _preferred_port_for_tenant(
    key: TenantKey,
    base_port: int,
    env_hash: str = "",
) -> int:
    digest = hashlib.md5(f"{key[0]}\0{key[1]}\0{env_hash}".encode()).hexdigest()
    offset = int(digest[:4], 16) % 200
    return max(1, base_port + offset)


def _terminate_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            logger.debug("force-kill browser runtime process failed", exc_info=True)


def _stop_slot(key: TenantKey, env_hash: str) -> bool:
    """Stop and remove one generation slot. Returns True if a live process was stopped."""
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        slot = bag.slots.pop(env_hash, None)
    if slot is None:
        return False
    proc = slot.process
    slot.process = None
    slot.server_url = None
    alive = proc is not None and proc.poll() is None
    _terminate_process(proc)
    _close_slot_log_handles(slot)
    return alive


def _prune_excess_slots(key: TenantKey, keep_hash: str) -> None:
    """Keep current generation + pinned ones; drop oldest unpinned beyond max."""
    bag = _get_or_create_bag(key)
    with _RUNTIMES_LOCK:
        if len(bag.slots) <= _MAX_SLOTS_PER_TENANT:
            return
        candidates = sorted(
            (
                (slot.created_at, env_hash)
                for env_hash, slot in bag.slots.items()
                if env_hash != keep_hash and bag.pin_counts.get(env_hash, 0) <= 0
            ),
            key=lambda item: item[0],
        )
        overflow = len(bag.slots) - _MAX_SLOTS_PER_TENANT
        victims = [env_hash for _, env_hash in candidates[: max(0, overflow)]]
    for env_hash in victims:
        logger.info(
            "Pruning excess browser runtime for tenant=(%s,%s) env_hash=%s",
            key[0],
            key[1],
            _short_hash_token(env_hash),
        )
        _stop_slot(key, env_hash)


def _start_local_server(
    transport: str,
    host: str,
    port: int,
    path: str,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
    env_hash: str | None = None,
) -> str:
    key = _normalize_tenant_key(service_id, agent_id)
    target_hash = env_hash or _model_credential_fingerprint(
        service_id=key[0],
        agent_id=key[1],
    )
    slot = _get_or_create_slot(key, target_hash)

    normalized = _normalize_client_type(transport)
    if normalized not in {"sse", "streamable-http"}:
        raise ValueError(f"Unsupported auto-start transport: {transport}")

    server_script = _browser_move_server_script()
    if not server_script.exists():
        raise FileNotFoundError(f"browser runtime server script not found: {server_script}")

    command = (_browser_env("BROWSER_RUNTIME_MCP_COMMAND", "") or sys.executable).strip()
    env = _build_browser_runtime_subprocess_env(
        service_id=key[0],
        agent_id=key[1],
    )
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
    stdout_log_path, stderr_log_path = _browser_runtime_log_paths(key, target_hash)
    _close_slot_log_handles(slot)
    stdout_handle = stdout_log_path.open("a", encoding="utf-8")
    stderr_handle = stderr_log_path.open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
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

    slot.process = proc
    slot.stdout_handle = stdout_handle
    slot.stderr_handle = stderr_handle
    slot.env_hash = target_hash
    slot.created_at = time.time()
    logger.info(
        "Started browser runtime subprocess: tenant=(%s,%s) env_hash=%s transport=%s "
        "url=http://%s:%s%s stdout_log=%s stderr_log=%s",
        key[0],
        key[1],
        _short_hash_token(target_hash),
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
        _stop_slot(key, target_hash)
        raise
    slot.server_url = f"http://{host}:{port}{path}"
    _prune_excess_slots(key, target_hash)
    return slot.server_url


def _parse_local_server_url(server_url: str) -> tuple[str, int, str]:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise ValueError(f"Invalid browser runtime server URL: {server_url}")
    return parsed.hostname, int(parsed.port), parsed.path or "/mcp"


def stop_local_browser_runtime_server(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
    env_hash: str | None = None,
) -> None:
    """Stop one generation (``env_hash``) or all generations for a tenant."""
    key = _normalize_tenant_key(service_id, agent_id)
    bag = _get_or_create_bag(key)
    if env_hash is not None:
        _stop_slot(key, env_hash)
        return
    with _RUNTIMES_LOCK:
        hashes = list(bag.slots.keys())
    for h in hashes:
        _stop_slot(key, h)


def restart_local_browser_runtime_server(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> str | None:
    key = _normalize_tenant_key(service_id, agent_id)
    bag = _get_or_create_bag(key)
    transport = _normalize_client_type(
        _browser_env("BROWSER_RUNTIME_MCP_CLIENT_TYPE", "streamable-http")
    )
    with _RUNTIMES_LOCK:
        slots = list(bag.slots.values())
    current_url = next((s.server_url for s in slots if s.server_url), None)
    had_local_server = any(
        s.server_url is not None or _slot_process_alive(s) for s in slots
    )

    if transport not in {"sse", "streamable-http"}:
        stop_local_browser_runtime_server(service_id=key[0], agent_id=key[1])
        return None

    host = _runtime_host()
    path = _runtime_path(transport)
    env_hash = _model_credential_fingerprint(service_id=key[0], agent_id=key[1])
    preferred_port = _preferred_port_for_tenant(key, int(_runtime_port()), env_hash)
    if current_url:
        host, preferred_port, path = _parse_local_server_url(current_url)

    stop_local_browser_runtime_server(service_id=key[0], agent_id=key[1])

    if not had_local_server:
        return None

    # Port may remain in TIME_WAIT after process exit; retry until released.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _is_port_available(host, preferred_port):
            return _start_local_server(
                transport,
                host,
                preferred_port,
                path,
                service_id=key[0],
                agent_id=key[1],
                env_hash=env_hash,
            )
        time.sleep(0.3)
    raise RuntimeError(
        f"Browser runtime port is still occupied after shutdown: {host}:{preferred_port}"
    )


def _ensure_local_server_started(
    transport: str,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    key = _normalize_tenant_key(service_id, agent_id)
    normalized = _normalize_client_type(transport)
    if normalized not in {"sse", "streamable-http"}:
        raise ValueError(f"Unsupported auto-start transport: {transport}")

    requested_hash = resolve_browser_runtime_env_hash(
        service_id=key[0],
        agent_id=key[1],
    )
    current_tip_hash = _model_credential_fingerprint(
        service_id=key[0],
        agent_id=key[1],
    )
    # Prefer pinned generation if its slot is still alive; otherwise fall back to tip.
    target_hash = requested_hash
    slot = _get_or_create_slot(key, target_hash)
    if not _slot_process_alive(slot) and target_hash != current_tip_hash:
        logger.info(
            "Pinned browser runtime missing for tenant=(%s,%s) env_hash=%s; "
            "falling back to current tip",
            key[0],
            key[1],
            _short_hash_token(target_hash),
        )
        target_hash = current_tip_hash
        slot = _get_or_create_slot(key, target_hash)

    if _slot_process_alive(slot) and slot.server_url:
        return slot.server_url

    # Spawning always uses current tip env; only allowed for current tip hash.
    if target_hash != current_tip_hash:
        target_hash = current_tip_hash
        slot = _get_or_create_slot(key, target_hash)
        if _slot_process_alive(slot) and slot.server_url:
            return slot.server_url

    host = _runtime_host()
    preferred_port = _preferred_port_for_tenant(
        key, int(_runtime_port()), target_hash
    )
    path = _runtime_path(normalized)
    port = _pick_available_port(host, preferred_port)
    return _start_local_server(
        normalized,
        host,
        port,
        path,
        service_id=key[0],
        agent_id=key[1],
        env_hash=target_hash,
    )


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

    server_id = (
        _browser_env("BROWSER_RUNTIME_MCP_SERVER_ID", "") or _BROWSER_MCP_DEFAULT_ID
    ).strip()
    server_name = (
        _browser_env("BROWSER_RUNTIME_MCP_SERVER_NAME", "") or _BROWSER_MCP_DEFAULT_NAME
    ).strip()
    client_type = _normalize_client_type(
        _browser_env("BROWSER_RUNTIME_MCP_CLIENT_TYPE", "streamable-http")
    )

    if client_type not in _SUPPORTED_CLIENT_TYPES:
        raise ValueError(
            "BROWSER_RUNTIME_MCP_CLIENT_TYPE must be one of stdio|sse|streamable-http."
        )

    if client_type == "sse":
        server_path = (
            _browser_env("BROWSER_RUNTIME_MCP_SERVER_PATH", "") or _build_server_url("sse")
        ).strip()
        return McpServerConfig(
            server_id=server_id,
            server_name=server_name,
            server_path=server_path,
            client_type="sse",
        )

    if client_type == "streamable-http":
        server_path = (
            _browser_env("BROWSER_RUNTIME_MCP_SERVER_PATH", "")
            or _build_server_url("streamable-http")
        ).strip()
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

    command = (_browser_env("BROWSER_RUNTIME_MCP_COMMAND", "") or sys.executable).strip()
    args_raw = _browser_env("BROWSER_RUNTIME_MCP_ARGS", "")
    if args_raw.strip():
        args = _parse_args(args_raw)
    else:
        args = [str(server_script), "--transport", "stdio", "--no-banner", "--log-level", "ERROR"]

    params: dict[str, Any] = {
        "command": command,
        "args": args,
        "cwd": str(_repo_root()),
    }
    timeout_raw = (_browser_env("BROWSER_RUNTIME_MCP_TIMEOUT_S", "") or "300").strip()
    try:
        timeout_s = int(timeout_raw)
        if timeout_s > 0:
            params["timeout_s"] = timeout_s
    except ValueError:
        pass

    sid, aid = resolve_env_ns()
    subprocess_env = _build_browser_runtime_subprocess_env(
        service_id=sid,
        agent_id=aid,
    )
    if subprocess_env:
        params["env"] = subprocess_env

    return McpServerConfig(
        server_id=server_id,
        server_name=server_name,
        server_path=(
            _browser_env("BROWSER_RUNTIME_MCP_SERVER_PATH", "")
            or "stdio://playwright-runtime-wrapper"
        ).strip(),
        client_type="stdio",
        params=params,
    )


async def register_browser_runtime_mcp_server(agent: Any, *, tag: str = "agent.main") -> bool:
    """Register browser runtime MCP server and add to agent abilities."""
    _ensure_browser_move_client_patch()
    cfg = build_browser_runtime_mcp_config()
    if cfg is None:
        return False

    sid, aid = resolve_env_ns()

    async def _ensure_started(transport: str) -> str:
        return await asyncio.to_thread(
            _ensure_local_server_started,
            transport,
            service_id=sid,
            agent_id=aid,
        )

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
            auto_url = await _ensure_started("sse")
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
            auto_url = await _ensure_started("sse")
            retry_cfg = _build_sse_retry_config(cfg, auto_url)
            ok, retry_err = await _register_once(retry_cfg)
            if ok:
                return True
            error_text = f"{error_text} | {retry_err}".strip(" |")
        except Exception as exc:
            error_text = f"{error_text} | {exc}".strip(" |")
    elif _normalize_client_type(cfg.client_type) == "streamable-http":
        try:
            auto_url = await _ensure_started("streamable-http")
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
