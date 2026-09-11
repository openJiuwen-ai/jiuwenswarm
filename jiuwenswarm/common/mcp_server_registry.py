# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程内 MCP Server 注册表：CRUD、工具缓存、周期扫描、全局 worker 池。

chat.send 的 ``mcp_server_list`` 只读这份缓存，不在对话路径上 ``list_tools``。
旧路径 ``office_claw_mcp`` / ``request_mcp_servers`` 不走本模块。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from jiuwenswarm.common.mcp_config import (
    _PooledMcpWorker,
    _normalize_mcp_client_type,
    _run_mcp_worker,
    _validate_request_scoped_remote_mcp,
    create_mcp_tool,
    list_request_mcp_server_tools,
    shutdown_pooled_mcp_worker,
)

logger = logging.getLogger(__name__)

# 周期扫描仅覆盖 remote HTTP MCP（§6.3 / D4）；playwright / openapi 不扫。
_REMOTE_SCAN_CLIENT_TYPES = frozenset({"sse", "streamable-http"})
_MCP_SCAN_TIMEOUT_S = 30.0
_PER_REQUEST_ENV_MARKERS = (
    "OFFICE_CLAW_INVOCATION_ID",
    "OFFICE_CLAW_CALLBACK_TOKEN",
)
_REDACT_KEYS = frozenset(
    {"authorization", "token", "secret", "password", "api_key", "apikey", "auth"}
)


class McpRegistryChatError(ValueError):
    """chat.send 新路径可预期失败（未知 server / 字段非法）。"""


class UnknownMcpServerError(McpRegistryChatError):
    def __init__(self, names: str | list[str]) -> None:
        if isinstance(names, str):
            names = [names]
        cleaned = [str(n).strip() for n in names if str(n).strip()]
        self.names = cleaned
        super().__init__("unknown mcp server: " + ", ".join(cleaned))


class DisabledMcpServerError(McpRegistryChatError):
    def __init__(self, name: str) -> None:
        self.name = str(name).strip()
        super().__init__(f"mcp server disabled: {self.name}")


@dataclass(frozen=True)
class McpRegistrySettings:
    scan_interval_s: float = 600.0
    scan_concurrency: int = 3
    worker_idle_ttl_s: float = 600.0
    scan_fail_threshold: int = 3


@dataclass
class McpServerEntry:
    name: str
    config: dict[str, Any]
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class CachedServerRecord:
    name: str
    tools: list[dict[str, Any]]
    version: int
    last_scan_at: float
    last_scan_ok: bool
    last_error: str = ""
    connect_params: dict[str, Any] = field(default_factory=dict)
    scan_fail_count: int = 0


def get_mcp_registry_settings() -> McpRegistrySettings:
    yaml_vals: dict[str, Any] = {}
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config() or {}
        mcp = cfg.get("mcp") if isinstance(cfg, dict) else None
        if isinstance(mcp, dict) and isinstance(mcp.get("registry"), dict):
            yaml_vals = dict(mcp["registry"])
    except Exception:
        yaml_vals = {}

    def _num(env_name: str, yaml_key: str, default: float, *, as_int: bool = False) -> float:
        raw = str(os.environ.get(env_name, "") or "").strip()
        if raw:
            try:
                value = int(raw) if as_int else float(raw)
                return float(value)
            except ValueError:
                pass
        yval = yaml_vals.get(yaml_key)
        if isinstance(yval, (int, float)):
            return float(int(yval) if as_int else yval)
        return default

    interval = max(1.0, _num("MCP_REGISTRY_SCAN_INTERVAL_S", "scan_interval_s", 600.0))
    concurrency = max(1, int(_num("MCP_REGISTRY_SCAN_CONCURRENCY", "scan_concurrency", 3.0, as_int=True)))
    ttl = max(1.0, _num("MCP_REGISTRY_WORKER_IDLE_TTL_S", "worker_idle_ttl_s", 600.0))
    fail_n = max(1, int(_num("MCP_REGISTRY_SCAN_FAIL_THRESHOLD", "scan_fail_threshold", 3.0, as_int=True)))
    return McpRegistrySettings(
        scan_interval_s=interval,
        scan_concurrency=concurrency,
        worker_idle_ttl_s=ttl,
        scan_fail_threshold=fail_n,
    )


def config_fingerprint(config: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(config or {}), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_remote_mcp_config(config: Mapping[str, Any]) -> bool:
    """True when this server participates in periodic remote scans."""

    client_type = _normalize_mcp_client_type(config.get("type"))
    return client_type in _REMOTE_SCAN_CLIENT_TYPES


def _admit_server_config(name: str, config: Mapping[str, Any]) -> None:
    """CRUD 准入：create_mcp_tool 白名单/危险参数；sse/http 再做 SSRF 主机屏蔽。"""

    create_mcp_tool(json.dumps({**dict(config), "name": name}, ensure_ascii=False))
    if is_remote_mcp_config(config):
        _validate_request_scoped_remote_mcp(name, dict(config))


def redact_mcp_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """list 接口脱敏：auth_* / 敏感 env 只回 key 不回 value。"""

    out: dict[str, Any] = {}
    for key, value in dict(config or {}).items():
        key_l = str(key).lower()
        if key_l in {"auth_headers", "auth_query_params"} and isinstance(value, dict):
            out[key] = {str(k): "***" for k in value}
            continue
        if key_l == "env" and isinstance(value, dict):
            redacted_env: dict[str, Any] = {}
            for env_k, env_v in value.items():
                env_kl = str(env_k).lower()
                if any(token in env_kl for token in _REDACT_KEYS):
                    redacted_env[str(env_k)] = "***"
                else:
                    redacted_env[str(env_k)] = env_v
            out[key] = redacted_env
            continue
        if any(token in key_l for token in _REDACT_KEYS):
            out[key] = "***"
            continue
        out[key] = value
    return out


def _worker_pool_key(server_name: str, params: Mapping[str, Any]) -> str:
    env = params.get("env")
    if not isinstance(env, Mapping):
        return server_name
    if not any(marker in env for marker in _PER_REQUEST_ENV_MARKERS):
        return server_name
    env_fp = hashlib.sha256(
        json.dumps({str(k): str(v) for k, v in env.items()}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{server_name}#{env_fp}"


class GlobalMcpWorkerPool:
    """按 server_name（可附 env 指纹）跨请求复用 ``_PooledMcpWorker``。"""

    def __init__(self) -> None:
        self._workers: dict[str, _PooledMcpWorker] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        server_name: str,
        params: Mapping[str, Any],
        *,
        force_rebuild: bool = False,
    ) -> _PooledMcpWorker:
        key = _worker_pool_key(server_name, params)
        copied = dict(params)
        env = copied.get("env")
        if isinstance(env, Mapping):
            copied["env"] = dict(env)
        async with self._lock:
            worker = self._workers.get(key)
            if force_rebuild and worker is not None:
                self._workers.pop(key, None)
                await shutdown_pooled_mcp_worker(worker)
                worker = None
            if worker is not None and worker.alive:
                worker.last_used = time.monotonic()
                return worker
            worker = _PooledMcpWorker(server_name)
            self._workers[key] = worker
            worker.task = asyncio.create_task(_run_mcp_worker(copied, worker))
            worker.last_used = time.monotonic()
            return worker

    async def close_server(self, server_name: str) -> None:
        name = str(server_name or "").strip()
        async with self._lock:
            keys = [k for k in self._workers if k == name or k.startswith(f"{name}#")]
            workers = [self._workers.pop(k) for k in keys]
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker)

    async def reap_idle(self, ttl_s: float) -> None:
        cutoff = time.monotonic() - max(1.0, float(ttl_s))
        async with self._lock:
            idle_keys = [
                key
                for key, worker in self._workers.items()
                if getattr(worker, "last_used", 0.0) <= cutoff
            ]
            workers = [self._workers.pop(k) for k in idle_keys]
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker)

    async def close_all(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            await shutdown_pooled_mcp_worker(worker)


class McpServerRegistry:
    """进程内存注册表。registry 权威，tool_cache 为扫描产物。"""

    def __init__(self, settings: McpRegistrySettings | None = None) -> None:
        self.settings = settings or get_mcp_registry_settings()
        self._lock = asyncio.Lock()
        self._registry: dict[str, McpServerEntry] = {}
        self._cache: dict[str, CachedServerRecord] = {}
        self.worker_pool = GlobalMcpWorkerPool()
        self._scanner_task: asyncio.Task[None] | None = None
        self._scanner_stop = asyncio.Event()

    async def add_servers(self, servers: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(servers)
        to_scan: list[tuple[int, str, dict[str, Any]]] = []
        pending: set[str] = set()
        async with self._lock:
            existing = set(self._registry)
        for index, item in enumerate(servers):
            if not isinstance(item, Mapping):
                results[index] = {"name": "", "ok": False, "error": "server entry must be an object"}
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                results[index] = {"name": "", "ok": False, "error": "name is required"}
                continue
            config = {k: copy.deepcopy(v) for k, v in item.items() if k != "name"}
            try:
                _admit_server_config(name, config)
            except ValueError as exc:
                results[index] = {"name": name, "ok": False, "error": str(exc)}
                continue
            if name in existing or name in pending:
                results[index] = {"name": name, "ok": False, "error": "already exists"}
                continue
            pending.add(name)
            to_scan.append((index, name, config))

        sem = asyncio.Semaphore(self.settings.scan_concurrency)

        async def _scan_and_commit(name: str, config: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                tools, params, error = await self._discover(name, config)
                if error:
                    return {"name": name, "ok": False, "error": error}
                now = time.monotonic()
                entry = McpServerEntry(
                    name=name,
                    config=copy.deepcopy(config),
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                record = CachedServerRecord(
                    name=name,
                    tools=copy.deepcopy(tools),
                    version=1,
                    last_scan_at=now,
                    last_scan_ok=True,
                    last_error="",
                    connect_params=copy.deepcopy(params),
                )
                async with self._lock:
                    if name in self._registry:
                        return {"name": name, "ok": False, "error": "already exists"}
                    self._registry[name] = entry
                    self._cache[name] = record
                tool_names = [str(t.get("name") or "") for t in tools]
                return {
                    "name": name,
                    "ok": True,
                    "tools": tool_names,
                    "tools_count": len(tool_names),
                }

        scanned = await asyncio.gather(*[_scan_and_commit(n, c) for _, n, c in to_scan])
        for (index, _, _), item in zip(to_scan, scanned):
            results[index] = item
        return [item if item is not None else {"name": "", "ok": False, "error": "internal error"} for item in results]

    async def remove_servers(self, names: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                results.append({"name": "", "ok": False, "error": "name is required"})
                continue
            async with self._lock:
                existed = self._registry.pop(name, None) is not None
                self._cache.pop(name, None)
            if not existed:
                results.append({"name": name, "ok": False, "error": "not found"})
                continue
            await self.worker_pool.close_server(name)
            results.append({"name": name, "ok": True})
        return results

    async def update_servers(self, servers: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in servers:
            if not isinstance(item, Mapping):
                results.append({"name": "", "ok": False, "error": "server entry must be an object"})
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                results.append({"name": "", "ok": False, "error": "name is required"})
                continue
            config = {k: copy.deepcopy(v) for k, v in item.items() if k != "name"}
            try:
                _admit_server_config(name, config)
            except ValueError as exc:
                results.append({"name": name, "ok": False, "error": str(exc)})
                continue
            async with self._lock:
                current = self._registry.get(name)
                if current is None:
                    results.append({"name": name, "ok": False, "error": "not found"})
                    continue
                same = config_fingerprint(current.config) == config_fingerprint(config)
                if same:
                    current.updated_at = time.monotonic()
                    cached = self._cache.get(name)
                    tool_names = [str(t.get("name") or "") for t in (cached.tools if cached else [])]
                    results.append(
                        {
                            "name": name,
                            "ok": True,
                            "skipped": True,
                            "tools": tool_names,
                            "tools_count": len(tool_names),
                        }
                    )
                    continue
            tools, params, error = await self._discover(name, config)
            if error:
                results.append({"name": name, "ok": False, "error": error})
                continue
            now = time.monotonic()
            async with self._lock:
                current = self._registry.get(name)
                if current is None:
                    results.append({"name": name, "ok": False, "error": "not found"})
                    continue
                old = self._cache.get(name)
                self._registry[name] = replace(
                    current,
                    config=copy.deepcopy(config),
                    updated_at=now,
                )
                self._cache[name] = CachedServerRecord(
                    name=name,
                    tools=copy.deepcopy(tools),
                    version=(old.version + 1) if old is not None else 1,
                    last_scan_at=now,
                    last_scan_ok=True,
                    last_error="",
                    connect_params=copy.deepcopy(params),
                )
            await self.worker_pool.close_server(name)
            tool_names = [str(t.get("name") or "") for t in tools]
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "tools": tool_names,
                    "tools_count": len(tool_names),
                }
            )
        return results

    async def list_servers(self) -> list[dict[str, Any]]:
        async with self._lock:
            items: list[dict[str, Any]] = []
            for name, entry in self._registry.items():
                cached = self._cache.get(name)
                items.append(
                    {
                        "name": name,
                        "config": redact_mcp_config(entry.config),
                        "enabled": entry.enabled,
                        "tools_count": len(cached.tools) if cached is not None else 0,
                        "version": cached.version if cached is not None else 0,
                        "last_scan_at": cached.last_scan_at if cached is not None else 0.0,
                        "last_scan_ok": cached.last_scan_ok if cached is not None else False,
                    }
                )
            return items

    async def get_server(self, name: str) -> dict[str, Any] | None:
        key = str(name or "").strip()
        async with self._lock:
            entry = self._registry.get(key)
            cached = self._cache.get(key)
            if entry is None:
                return None
            return {
                "name": key,
                "config": redact_mcp_config(entry.config),
                "enabled": entry.enabled,
                "tools": copy.deepcopy(cached.tools) if cached is not None else [],
                "version": cached.version if cached is not None else 0,
                "last_scan_at": cached.last_scan_at if cached is not None else 0.0,
                "last_scan_ok": cached.last_scan_ok if cached is not None else False,
                "last_error": cached.last_error if cached is not None else "",
            }

    async def snapshot_for_chat(
        self, names: list[str]
    ) -> list[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
        """按名称读取工具快照。缺省/停用抛错。无 IO。"""

        snapshots: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        unknown: list[str] = []
        disabled: list[str] = []
        async with self._lock:
            for raw in names:
                name = str(raw or "").strip()
                if not name:
                    continue
                entry = self._registry.get(name)
                cached = self._cache.get(name)
                if entry is None or cached is None:
                    unknown.append(name)
                    continue
                if not entry.enabled:
                    disabled.append(name)
                    continue
                snapshots.append(
                    (name, copy.deepcopy(cached.tools), copy.deepcopy(cached.connect_params))
                )
        if unknown:
            raise UnknownMcpServerError(unknown)
        if disabled:
            raise DisabledMcpServerError(disabled[0])
        return snapshots

    def start_scanner(self) -> None:
        if self._scanner_task is not None and not self._scanner_task.done():
            return
        self._scanner_stop = asyncio.Event()
        self._scanner_task = asyncio.create_task(self._scanner_loop(), name="mcp-registry-scanner")

    async def stop_scanner(self) -> None:
        self._scanner_stop.set()
        task = self._scanner_task
        self._scanner_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await self.worker_pool.close_all()

    async def scan_once(self) -> None:
        """单轮扫描（单测 / 扫描循环复用）。"""

        async with self._lock:
            snapshot = [
                entry
                for entry in list(self._registry.values())
                if entry.enabled and is_remote_mcp_config(entry.config)
            ]
        sem = asyncio.Semaphore(self.settings.scan_concurrency)

        async def _scan_one(entry: McpServerEntry) -> None:
            async with sem:
                async with self._lock:
                    if self._registry.get(entry.name) is not entry:
                        return
                tools, params, error = await self._discover(entry.name, entry.config)
                async with self._lock:
                    if self._registry.get(entry.name) is not entry:
                        return
                    current = self._cache.get(entry.name)
                    now = time.monotonic()
                    if error:
                        fail_count = (current.scan_fail_count + 1) if current is not None else 1
                        if current is not None:
                            current.last_scan_at = now
                            current.last_scan_ok = False
                            current.last_error = error
                            current.scan_fail_count = fail_count
                        if fail_count >= self.settings.scan_fail_threshold:
                            logger.error(
                                "[McpServerRegistry] remote scan failed %s times: name=%s error=%s",
                                fail_count,
                                entry.name,
                                error,
                            )
                        return
                    if current is not None and current.tools == tools:
                        current.last_scan_at = now
                        current.last_scan_ok = True
                        current.last_error = ""
                        current.scan_fail_count = 0
                        if params:
                            current.connect_params = copy.deepcopy(params)
                        return
                    version = (current.version + 1) if current is not None else 1
                    self._cache[entry.name] = CachedServerRecord(
                        name=entry.name,
                        tools=copy.deepcopy(tools),
                        version=version,
                        last_scan_at=now,
                        last_scan_ok=True,
                        last_error="",
                        connect_params=copy.deepcopy(params) if params else (
                            copy.deepcopy(current.connect_params) if current is not None else {}
                        ),
                    )
                    logger.info(
                        "[McpServerRegistry] tools changed: name=%s version=%s count=%s",
                        entry.name,
                        version,
                        len(tools),
                    )

        if snapshot:
            await asyncio.gather(*[_scan_one(entry) for entry in snapshot])
        await self.worker_pool.reap_idle(self.settings.worker_idle_ttl_s)

    async def _scanner_loop(self) -> None:
        while not self._scanner_stop.is_set():
            try:
                await self.scan_once()
            except Exception:
                logger.exception("[McpServerRegistry] scanner cycle failed")
            jitter = 1.0 + random.uniform(0.0, 0.1)
            timeout = self.settings.scan_interval_s * jitter
            try:
                await asyncio.wait_for(self._scanner_stop.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass

    async def _discover(
        self, name: str, config: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        try:
            tools, params = await asyncio.wait_for(
                list_request_mcp_server_tools(name, dict(config)),
                timeout=_MCP_SCAN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return [], {}, f"connect timeout after {int(_MCP_SCAN_TIMEOUT_S)}s"
        except Exception as exc:
            return [], {}, str(exc)
        if not params:
            return [], {}, "discovery failed"
        return list(tools or []), dict(params), ""


_REGISTRY: McpServerRegistry | None = None


def get_mcp_server_registry() -> McpServerRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = McpServerRegistry()
    return _REGISTRY


def reset_mcp_server_registry_for_tests() -> McpServerRegistry:
    global _REGISTRY
    _REGISTRY = McpServerRegistry()
    return _REGISTRY


async def start_mcp_registry_runtime() -> None:
    get_mcp_server_registry().start_scanner()


async def stop_mcp_registry_runtime() -> None:
    global _REGISTRY
    if _REGISTRY is None:
        return
    await _REGISTRY.stop_scanner()


def extract_mcp_server_list(params: Any) -> list[str] | None:
    """字段存在则返回名称列表（可空）；缺省返回 None（走旧路径）。"""

    if not isinstance(params, dict) or "mcp_server_list" not in params:
        return None
    raw = params.get("mcp_server_list")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise McpRegistryChatError("mcp_server_list must be a list of server names")
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names
