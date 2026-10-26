#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Browser backend service with sticky sessions and guardrails."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import shlex
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.store.base_kv_store import BaseKVStore
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.middleware.base import (
    AgentCallbackEvent,
    AgentMiddleware,
    AnyAgentCallback,
)
from playwright_runtime import REPO_ROOT
from playwright_runtime.agents import augment_browser_task_prompt, build_browser_worker_agent
from playwright_runtime.config import BrowserRunGuardrails, resolve_playwright_mcp_cwd
from playwright_runtime.drivers.managed_browser import ManagedBrowserDriver, _default_chrome_user_data_dir
from playwright_runtime.hooks import BrowserCancellationMiddleware, BrowserRunCancelled
from playwright_runtime.profiles import BrowserProfile, BrowserProfileStore

from jiuwenswarm.agents.harness.common.tools.browser_timeout_policy import (
    allow_short_timeout_override,
    resolve_browser_task_timeout,
)
from jiuwenswarm.common.utils import get_user_workspace_dir

MAX_ITERATION_MESSAGE = "Max iterations reached without completion"


def extract_json_object(text: Any) -> Dict[str, Any]:
    """Best-effort JSON extraction from model text."""
    if isinstance(text, dict):
        return text
    if text is None:
        return {}

    raw = str(text).strip()
    if not raw:
        return {}

    marker_result = "### Result"
    marker_ran = "### Ran Playwright code"
    if marker_result in raw and marker_ran in raw:
        start = raw.find(marker_result) + len(marker_result)
        end = raw.find(marker_ran, start)
        if end > start:
            raw = raw[start:end].strip()

    # Some wrappers return JSON as a quoted string.
    for _ in range(2):
        try:
            parsed = json.loads(raw)
        except Exception:
            break
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            raw = parsed.strip()
            continue
        break

    if "```json" in raw:
        start = raw.find("```json") + len("```json")
        end = raw.find("```", start)
        if end > start:
            block = raw[start:end].strip()
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        snippet = raw[first:last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return {}


class BrowserService:
    """Backend browser service with sticky logical sessions."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        api_base: str,
        model_name: str,
        mcp_cfg: McpServerConfig,
        guardrails: BrowserRunGuardrails,
        cancel_store: Optional[BaseKVStore] = None,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.api_base = api_base
        self.model_name = model_name
        self.mcp_cfg = mcp_cfg
        self.guardrails = guardrails
        self._cancel_store: BaseKVStore = cancel_store or InMemoryKVStore()

        self.started = False
        self._browser_agent: Optional[ReActAgent] = None
        self._locks: Dict[str, asyncio.Lock] = {}
        self._sessions: set[str] = set()
        self._inflight_tasks: Dict[str, set[asyncio.Task[Any]]] = {}
        self._pending_middlewares: List[AgentMiddleware] = []
        self._pending_callbacks: List[Tuple[AgentCallbackEvent, AnyAgentCallback, int]] = []
        self._screenshot_subdir = "screenshots"
        self._mcp_cwd = self._resolve_mcp_cwd()
        self._screenshots_dir = self._mcp_cwd / self._screenshot_subdir
        self._profile_store = BrowserProfileStore(self._resolve_profile_store_path())
        self._profile_name = (os.getenv("BROWSER_PROFILE_NAME") or "jiuwenswarm").strip() or "jiuwenswarm"
        self._driver_mode = self._resolve_driver_mode()
        self._active_profile: Optional[BrowserProfile] = None
        self._managed_driver: Optional[ManagedBrowserDriver] = None
        self._registered_cdp_endpoint: str = ""
        self._failure_context_by_session: Dict[str, str] = {}

        logger.info(
            "BrowserService initialized: "
            f"driver_mode={self._driver_mode}, profile_name={self._profile_name}, "
            f"profile_store={self._resolve_profile_store_path()}, mcp_server={self._server_resource_id()}, "
            f"mcp_cwd={self._mcp_cwd}"
        )

    @staticmethod
    def _parse_env_args(value: str) -> List[str]:
        raw = (value or "").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [part for part in shlex.split(raw) if part]

    @staticmethod
    def _runtime_state_root() -> Path:
        configured = (os.getenv("BROWSER_RUNTIME_STATE_DIR") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return get_user_workspace_dir() / "browser-move"

    def _resolve_profile_store_path(self) -> Path:
        configured = (os.getenv("BROWSER_PROFILE_STORE_PATH") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return self._runtime_state_root() / ".browser" / "profiles.json"

    def _resolve_driver_mode(self) -> str:
        explicit = (os.getenv("BROWSER_DRIVER") or "").strip().lower()
        if explicit:
            if explicit not in {"remote", "managed", "extension"}:
                raise ValueError("BROWSER_DRIVER must be one of: remote, managed, extension")
            return explicit
        return "remote"

    def _refresh_profile_store(self) -> None:
        store_path = self._resolve_profile_store_path()
        self._profile_store = BrowserProfileStore(store_path)
        logger.info(f"BrowserService: refreshed profile store from {store_path}")

    @staticmethod
    def _is_cdp_endpoint_ready(endpoint: str) -> bool:
        base = str(endpoint or "").strip().rstrip("/")
        if not base:
            return False
        try:
            with urlopen(f"{base}/json/version", timeout=1.5) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
                if isinstance(payload, dict):
                    return bool(payload.get("webSocketDebuggerUrl") or payload.get("Browser"))
        except (OSError, ValueError):
            return False
        return False

    def _resolve_existing_cdp_profile(self) -> Optional[BrowserProfile]:
        self._refresh_profile_store()
        candidates: List[BrowserProfile] = []
        selected = self._profile_store.selected_profile()
        if selected is not None:
            candidates.append(selected)
        named = self._profile_store.get_profile(self._profile_name)
        if named is not None and all(named.name != item.name for item in candidates):
            candidates.append(named)
        candidate_names = [profile.name for profile in candidates]
        logger.info(
            "BrowserService: checking existing CDP profiles "
            f"for reuse, candidates={candidate_names or ['(none)']}"
        )
        for profile in candidates:
            endpoint = str(profile.cdp_url or "").strip()
            if endpoint and self._is_cdp_endpoint_ready(endpoint):
                logger.info(
                    "BrowserService: reusing existing CDP profile "
                    f"name={profile.name}, endpoint={endpoint}, driver_type={profile.driver_type}"
                )
                return profile
            logger.info(
                "BrowserService: existing CDP profile not ready "
                f"name={profile.name}, endpoint={endpoint or '(empty)'}"
            )
        logger.info("BrowserService: no reusable existing CDP profile found")
        return None

    def _should_replace_managed_driver(self, profile: BrowserProfile) -> bool:
        if self._managed_driver is None:
            return False
        if profile.driver_type != "managed":
            return True
        if self._active_profile is None:
            return True
        current_endpoint = str(self._active_profile.cdp_url or "").strip()
        target_endpoint = str(profile.cdp_url or "").strip()
        return current_endpoint != target_endpoint

    @staticmethod
    def _env_truthy(name: str, default: bool = False) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _should_close_browser_after_task(self) -> bool:
        return self._driver_mode == "managed" and self._env_truthy(
            "BROWSER_MANAGED_CLOSE_AFTER_TASK",
            default=False,
        )

    @staticmethod
    def _is_browser_connection_error_text(value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        markers = (
            "connectovercdp",
            "econnrefused",
            "browsertype.connectovercdp",
            "????????",
            "cdp ??",
            "browser service may not be started",
            "browser has been closed",
            "target page, context or browser has been closed",
            "failed to connect to browser",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _extract_error_text(error_value: Any) -> str:
        if error_value is None:
            return ""
        parts: List[str] = []
        for attr in ("reason", "message", "msg", "code"):
            value = getattr(error_value, attr, None)
            if value:
                parts.append(str(value))
        for attr in ("args",):
            value = getattr(error_value, attr, None)
            if value:
                parts.extend(str(item) for item in value if item)
        parts.append(str(error_value))
        return " | ".join(part for part in parts if part).strip()

    def _resolve_effective_timeout(self, timeout_s: Optional[int]) -> int:
        effective_timeout = resolve_browser_task_timeout(timeout_s, self.guardrails.timeout_s)
        requested_timeout = None
        if timeout_s is not None:
            try:
                parsed = int(timeout_s)
                if parsed > 0:
                    requested_timeout = parsed
            except (TypeError, ValueError):
                requested_timeout = None
        if (
            requested_timeout is not None
            and effective_timeout != requested_timeout
            and not allow_short_timeout_override()
        ):
            from openjiuwen.core.common.logging import logger as _logger

            _logger.info(
                "BrowserService: clamped browser task timeout from "
                f"{requested_timeout}s to {effective_timeout}s"
            )
        return effective_timeout

    @staticmethod
    def _cancel_key(session_id: str, request_id: Optional[str] = None) -> str:
        rid = (request_id or "").strip() or "*"
        return f"playwright_runtime:cancel:{session_id}:{rid}"

    @staticmethod
    def _inflight_key(session_id: str, request_id: Optional[str] = None) -> str:
        rid = (request_id or "").strip()
        return f"{session_id}:{rid}" if rid else session_id

    def _register_inflight_task(self, session_id: str, request_id: str, task: asyncio.Task[Any]) -> None:
        keys = (self._inflight_key(session_id), self._inflight_key(session_id, request_id))
        for key in keys:
            self._inflight_tasks.setdefault(key, set()).add(task)

    def _unregister_inflight_task(self, session_id: str, request_id: str, task: asyncio.Task[Any]) -> None:
        keys = (self._inflight_key(session_id), self._inflight_key(session_id, request_id))
        for key in keys:
            tasks = self._inflight_tasks.get(key)
            if not tasks:
                continue
            tasks.discard(task)
            if not tasks:
                self._inflight_tasks.pop(key, None)

    def _resolve_mcp_cwd(self) -> Path:
        params = getattr(self.mcp_cfg, "params", {}) or {}
        raw = str(params.get("cwd", "")).strip()
        if raw:
            return Path(raw).expanduser()
        return Path(resolve_playwright_mcp_cwd()).expanduser()

    def _build_managed_profile(self) -> BrowserProfile:
        host = (os.getenv("BROWSER_MANAGED_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        port_raw = (os.getenv("BROWSER_MANAGED_PORT") or "9333").strip()
        try:
            port = int(port_raw)
            if port <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"Invalid BROWSER_MANAGED_PORT: {port_raw}") from exc

        kill_existing_raw = (os.getenv("BROWSER_MANAGED_KILL_EXISTING") or "").strip().lower()
        kill_existing = kill_existing_raw in {"1", "true", "yes", "on"}
        explicit_user_data_dir = (os.getenv("BROWSER_MANAGED_USER_DATA_DIR") or "").strip()
        if explicit_user_data_dir:
            user_data_dir = explicit_user_data_dir
        elif kill_existing:
            user_data_dir = _default_chrome_user_data_dir()
        else:
            user_data_dir = str(self._runtime_state_root() / ".browser-profiles" / self._profile_name)
        browser_binary = (os.getenv("BROWSER_MANAGED_BINARY") or "").strip()
        extra_args = self._parse_env_args(os.getenv("BROWSER_MANAGED_ARGS") or "")
        cdp_url = f"http://{host}:{port}"
        return BrowserProfile(
            name=self._profile_name,
            driver_type="managed",
            cdp_url=cdp_url,
            browser_binary=browser_binary,
            user_data_dir=user_data_dir,
            debug_port=port,
            host=host,
            extra_args=extra_args,
        )

    def _inject_cdp_endpoint(self, endpoint: str) -> None:
        previous_endpoint = self._configured_cdp_endpoint()
        params = dict(getattr(self.mcp_cfg, "params", {}) or {})
        env_map = dict(params.get("env", {}) or {})
        env_map["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = endpoint
        env_map.setdefault("PLAYWRIGHT_MCP_BROWSER", "chrome")
        env_map.pop("PLAYWRIGHT_MCP_DEVICE", None)
        params["env"] = env_map
        self.mcp_cfg.params = params
        if previous_endpoint != endpoint:
            logger.info(
                "BrowserService: updated MCP CDP endpoint "
                f"from {previous_endpoint or '(empty)'} to {endpoint or '(empty)'}"
            )

    def _configured_cdp_endpoint(self) -> str:
        params = dict(getattr(self.mcp_cfg, "params", {}) or {})
        env_map = dict(params.get("env", {}) or {})
        return str(env_map.get("PLAYWRIGHT_MCP_CDP_ENDPOINT") or "").strip()

    def _server_resource_id(self) -> str:
        return (self.mcp_cfg.server_id or "").strip() or self.mcp_cfg.server_name

    async def _remove_registered_mcp_server(self) -> None:
        resource_mgr = Runner.resource_mgr
        server_id = self._server_resource_id()
        logger.info(f"BrowserService: removing registered MCP server server_id={server_id}")

        async def _invoke(method, method_name: str) -> None:
            logger.info(
                "BrowserService: invoking MCP server removal method "
                f"server_id={server_id}, method={method_name}"
            )
            try:
                await method(server_id, ignore_not_exist=True)
                return
            except TypeError:
                await method(server_id)

        direct_method = getattr(resource_mgr, "remove_mcp_server", None)
        if callable(direct_method):
            await _invoke(direct_method, "ResourceMgr.remove_mcp_server")
            return

        direct_method = getattr(resource_mgr, "remove_tool_server", None)
        if callable(direct_method):
            await _invoke(direct_method, "ResourceMgr.remove_tool_server")
            return

        for attr_name in ("tool_mgr", "_tool_mgr", "tool_manager", "_tool_manager"):
            manager = getattr(resource_mgr, attr_name, None)
            if manager is None:
                continue
            nested_method = getattr(manager, "remove_tool_server", None)
            if callable(nested_method):
                await _invoke(nested_method, f"{attr_name}.remove_tool_server")
                return
            nested_method = getattr(manager, "remove_mcp_server", None)
            if callable(nested_method):
                await _invoke(nested_method, f"{attr_name}.remove_mcp_server")
                return

    async def _register_mcp_server(self) -> None:
        endpoint = self._configured_cdp_endpoint()
        logger.info(
            "BrowserService: registering MCP server "
            f"server_id={self._server_resource_id()}, endpoint={endpoint or '(empty)'}"
        )
        register_result = await Runner.resource_mgr.add_mcp_server(self.mcp_cfg, tag="browser.service")
        if register_result is not None and not getattr(register_result, "is_ok", lambda: False)():
            error_value = getattr(register_result, "value", register_result)
            error_text = self._extract_error_text(error_value).lower()
            if "already exist" not in error_text:
                raise RuntimeError(
                    f"Failed to register Playwright MCP server: {self._extract_error_text(error_value)}"
                )
            logger.warning(
                "BrowserService: MCP server already registered, reusing existing registration "
                f"server_id={self._server_resource_id()}, endpoint={endpoint or '(empty)'}, error={error_text}"
            )
        self._registered_cdp_endpoint = self._configured_cdp_endpoint()
        logger.info(
            "BrowserService: MCP server registration ready "
            f"server_id={self._server_resource_id()}, endpoint={self._registered_cdp_endpoint or '(empty)'}"
        )

    async def _refresh_mcp_server_binding(self) -> None:
        logger.warning(
            "BrowserService: refreshing MCP server binding "
            f"server_id={self._server_resource_id()}, old_endpoint={self._registered_cdp_endpoint or '(empty)'}, "
            f"new_endpoint={self._configured_cdp_endpoint() or '(empty)'}"
        )
        await self._remove_registered_mcp_server()
        await self._register_mcp_server()

    async def _ensure_managed_driver_started(self) -> None:
        logger.info(
            "BrowserService: ensuring managed driver is ready "
            f"driver_mode={self._driver_mode}, active_profile={getattr(self._active_profile, 'name', None)}"
        )
        existing_profile = self._resolve_existing_cdp_profile()
        if existing_profile is not None:
            if self._should_replace_managed_driver(existing_profile):
                logger.warning(
                    "BrowserService: existing CDP profile requires managed driver replacement "
                    f"name={existing_profile.name}, endpoint={existing_profile.cdp_url}"
                )
                await self._stop_managed_driver()
            self._active_profile = existing_profile
            self._inject_cdp_endpoint(existing_profile.cdp_url)
            self._profile_store.upsert_profile(existing_profile, select=True)
            return

        if self._driver_mode != "managed":
            logger.info("BrowserService: driver mode is not managed; skip auto-start")
            return
        if self._managed_driver is not None:
            try:
                logger.info("BrowserService: reusing existing managed driver instance")
                endpoint = await asyncio.to_thread(self._managed_driver.start, 10.0, False)
                self._inject_cdp_endpoint(endpoint)
                if self._active_profile is not None:
                    self._active_profile.cdp_url = endpoint
                    self._profile_store.upsert_profile(self._active_profile, select=True)
                logger.info(f"BrowserService: managed driver reused at endpoint={endpoint}")
                return
            except Exception as exc:
                logger.warning(f"BrowserService: existing managed driver reuse failed: {exc}")
                await self._stop_managed_driver()

        profile = self._profile_store.get_profile(self._profile_name)
        if (
            profile is None
            or profile.driver_type != "managed"
            or profile.debug_port <= 0
            or not str(profile.user_data_dir).strip()
        ):
            profile = self._build_managed_profile()
        configured_binary = (os.getenv("BROWSER_MANAGED_BINARY") or "").strip()
        if configured_binary:
            profile.browser_binary = configured_binary
        self._profile_store.upsert_profile(profile, select=True)
        self._active_profile = profile

        kill_existing_raw = (os.getenv("BROWSER_MANAGED_KILL_EXISTING") or "").strip().lower()
        kill_existing = kill_existing_raw in {"1", "true", "yes", "on"}

        logger.info(
            "BrowserService: starting managed browser driver "
            f"profile={profile.name}, host={profile.host}, port={profile.debug_port}, "
            f"user_data_dir={profile.user_data_dir}, kill_existing={kill_existing}"
        )
        driver = ManagedBrowserDriver(profile=profile)
        endpoint = await asyncio.to_thread(driver.start, 20.0, kill_existing)
        self._inject_cdp_endpoint(endpoint)
        profile.cdp_url = endpoint
        self._profile_store.upsert_profile(profile, select=True)
        self._managed_driver = driver
        logger.info(
            "BrowserService: managed browser driver started "
            f"profile={profile.name}, endpoint={endpoint}"
        )

    async def _stop_managed_driver(self) -> None:
        if self._managed_driver is None:
            return
        driver = self._managed_driver
        self._managed_driver = None
        logger.info("BrowserService: stopping managed browser driver")
        await asyncio.to_thread(driver.stop)
        logger.info("BrowserService: managed browser driver stopped")

    async def _reset_browser_runtime(self) -> None:
        logger.warning(
            "BrowserService: resetting browser runtime "
            f"started={self.started}, registered_endpoint={self._registered_cdp_endpoint or '(empty)'}"
        )
        try:
            if self.started:
                await self._remove_registered_mcp_server()
        except Exception as exc:
            logger.warning(f"BrowserService: failed to remove registered MCP server during reset: {exc}")
        self.started = False
        self._registered_cdp_endpoint = ""
        self._browser_agent = None
        await self._stop_managed_driver()
        logger.info("BrowserService: browser runtime reset complete")

    async def _restart_browser_runtime(self) -> None:
        from openjiuwen.core.common.logging import logger as _logger

        _logger.warning("BrowserService: restarting browser runtime")
        await self._reset_browser_runtime()
        await self.ensure_started()

    def _ensure_screenshots_dir(self) -> None:
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_local_screenshot_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        candidates: List[Path] = []
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend(
                [
                    self._mcp_cwd / path,
                    Path.cwd() / path,
                    path,
                ]
            )

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return path

    def _ensure_screenshot_in_folder(self, source_path: Path) -> Path:
        if not source_path.exists() or not source_path.is_file():
            return source_path

        self._ensure_screenshots_dir()
        try:
            source_resolved = source_path.resolve()
            target_dir_resolved = self._screenshots_dir.resolve()
            try:
                source_resolved.relative_to(target_dir_resolved)
                return source_resolved
            except ValueError:
                pass

            target_path = self._screenshots_dir / source_path.name
            if target_path.exists():
                target_resolved = target_path.resolve()
                if target_resolved != source_resolved:
                    target_path = self._screenshots_dir / (
                        f"{source_path.stem}-{uuid.uuid4().hex[:8]}{source_path.suffix}"
                    )

            shutil.copy2(source_path, target_path)
            return target_path
        except Exception:
            return source_path

    def _normalize_screenshot_value(self, screenshot: Any) -> Any:
        """Normalize screenshot for downstream multimodal APIs.

        - Keep remote URLs and existing data URLs as-is.
        - Ensure local screenshots are copied into screenshots/ folder.
        - Convert local image file paths to data URLs.
        """
        if screenshot is None or not isinstance(screenshot, str):
            return screenshot

        raw = screenshot.strip()
        if not raw:
            return None

        lowered = raw.lower()
        if lowered.startswith(("http://", "https://", "data:image/")):
            return raw

        local_path_str = raw[7:] if lowered.startswith("file://") else raw
        local_path = self._resolve_local_screenshot_path(local_path_str)
        if not local_path.exists() or not local_path.is_file():
            return raw
        local_path = self._ensure_screenshot_in_folder(local_path)

        mime_type, _ = mimetypes.guess_type(str(local_path))
        if not mime_type or not mime_type.startswith("image/"):
            return raw

        try:
            encoded = base64.b64encode(local_path.read_bytes()).decode("ascii")
        except Exception:
            return raw
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _is_retryable_transport_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        markers = (
            "session terminated",
            "not connected",
            "endofstream",
            "closedresourceerror",
            "brokenresourceerror",
            "stream closed",
            "connection closed",
            "broken pipe",
            "remoteprotocolerror",
            "readerror",
            "writeerror",
        )
        return any(marker in name or marker in text for marker in markers)

    async def request_cancel(self, session_id: str, request_id: Optional[str] = None) -> None:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required for cancellation")
        await self._cancel_store.set(self._cancel_key(sid, request_id), "1")

        request_id_clean = (request_id or "").strip()
        if request_id_clean:
            keys = [self._inflight_key(sid, request_id_clean)]
        else:
            keys = [self._inflight_key(sid)]

        for key in keys:
            for task in list(self._inflight_tasks.get(key, set())):
                if not task.done():
                    task.cancel()

    async def clear_cancel(self, session_id: str, request_id: Optional[str] = None) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        if request_id:
            await self._cancel_store.delete(self._cancel_key(sid, request_id))
            return
        await self._cancel_store.delete(self._cancel_key(sid, "*"))

    async def is_cancelled(self, session_id: str, request_id: Optional[str] = None) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        if request_id:
            exact = await self._cancel_store.get(self._cancel_key(sid, request_id))
            if exact is not None:
                return True
        wildcard = await self._cancel_store.get(self._cancel_key(sid, "*"))
        return wildcard is not None

    def add_browser_middleware(self, middleware: AgentMiddleware) -> None:
        if self._browser_agent is None:
            self._pending_middlewares.append(middleware)
            return
        self._browser_agent.register_middleware(middleware)

    def add_browser_callback(
        self,
        event: AgentCallbackEvent,
        callback: AnyAgentCallback,
        priority: int = 100,
    ) -> None:
        if self._browser_agent is None:
            self._pending_callbacks.append((event, callback, priority))
            return
        self._browser_agent.register_callback(event, callback, priority=priority)

    def session_new(self, session_id: Optional[str] = None) -> str:
        sid = (session_id or "").strip() or f"browser-{uuid.uuid4().hex}"
        self._sessions.add(sid)
        if sid not in self._locks:
            self._locks[sid] = asyncio.Lock()
        return sid

    async def ensure_started(self) -> None:
        logger.info(
            "BrowserService.ensure_started called "
            f"started={self.started}, configured_endpoint={self._configured_cdp_endpoint() or '(empty)'}, "
            f"registered_endpoint={self._registered_cdp_endpoint or '(empty)'}"
        )
        if self.started:
            await self._ensure_managed_driver_started()
            if self._configured_cdp_endpoint() != self._registered_cdp_endpoint:
                logger.warning(
                    "BrowserService: detected MCP endpoint drift while already started "
                    f"configured={self._configured_cdp_endpoint() or '(empty)'}, "
                    f"registered={self._registered_cdp_endpoint or '(empty)'}"
                )
                await self._refresh_mcp_server_binding()
            return

        if shutil.which("npx") is None:
            raise RuntimeError("npx not found in PATH. Install Node.js first.")

        await self._ensure_managed_driver_started()
        self._ensure_screenshots_dir()
        logger.info("BrowserService: starting Runner and registering browser MCP server")
        await Runner.start()
        await self._register_mcp_server()

        self._browser_agent = build_browser_worker_agent(
            provider=self.provider,
            api_key=self.api_key,
            api_base=self.api_base,
            model_name=self.model_name,
            mcp_cfg=self.mcp_cfg,
            max_steps=self.guardrails.max_steps,
            screenshot_subdir=self._screenshot_subdir,
        )
        self._browser_agent.register_middleware(BrowserCancellationMiddleware(self.is_cancelled))
        for middleware in self._pending_middlewares:
            self._browser_agent.register_middleware(middleware)
        self._pending_middlewares.clear()
        for event, callback, priority in self._pending_callbacks:
            self._browser_agent.register_callback(event, callback, priority=priority)
        self._pending_callbacks.clear()
        self.started = True
        logger.info(
            "BrowserService: started successfully "
            f"registered_endpoint={self._registered_cdp_endpoint or '(empty)'}"
        )

    async def _restart(self) -> None:
        """Tear down and reinitialize the browser service (e.g. after browser/CDP/MCP failure)."""
        await self._restart_browser_runtime()

    async def _run_task_once(self, task: str, session_id: str, request_id: str) -> Dict[str, Any]:
        if self._browser_agent is None:
            raise RuntimeError("BrowserService is not started")

        logger.info(
            "BrowserService: running browser task once "
            f"session_id={session_id}, request_id={request_id}, task_excerpt={self._trim_text(task, 160)}"
        )
        task_body = augment_browser_task_prompt(task)
        task_prompt = (
            f"Session id: {session_id}\n"
            f"Request id: {request_id}\n"
            f"Max steps: {self.guardrails.max_steps}\n"
            f"Max failures: {self.guardrails.max_failures}\n\n"
            f"Task:\n{task_body}\n\n"
            "Perform the task in the current logical browser session/tab for this session id."
        )
        result = await Runner.run_agent(
            self._browser_agent,
            {"query": task_prompt, "conversation_id": session_id, "request_id": request_id},
        )
        output_text = result.get("output") if isinstance(result, dict) else result
        parsed = extract_json_object(output_text)
        if parsed:
            logger.info(
                "BrowserService: browser task produced JSON result "
                f"session_id={session_id}, request_id={request_id}, ok={bool(parsed.get('ok', False))}, "
                f"error={self._trim_text(parsed.get('error'), 120)}"
            )
            return parsed

        output_str = str(output_text) if output_text is not None else ""
        logger.warning(
            "BrowserService: browser task did not return JSON "
            f"session_id={session_id}, request_id={request_id}, output_excerpt={self._trim_text(output_str, 240)}"
        )
        output_lower = output_str.lower()
        if MAX_ITERATION_MESSAGE.lower() in output_lower:
            return {
                "ok": False,
                "final": output_str,
                "page": {"url": "", "title": ""},
                "screenshot": None,
                "error": "max_iterations_reached",
            }

        return {
            "ok": False,
            "final": output_str,
            "page": {"url": "", "title": ""},
            "screenshot": None,
            "error": "Browser worker did not return valid JSON output",
        }

    @staticmethod
    def _is_max_iteration_result(parsed: Dict[str, Any]) -> bool:
        if not isinstance(parsed, dict):
            return False
        if str(parsed.get("error", "")).strip().lower() == "max_iterations_reached":
            return True
        marker = MAX_ITERATION_MESSAGE.lower()
        for key in ("final", "error"):
            value = parsed.get(key)
            if value is None:
                continue
            if marker in str(value).lower():
                return True
        return False

    @staticmethod
    def _build_resume_task(task: str, previous_final: str) -> str:
        base = (task or "").strip()
        previous = (previous_final or "").strip()
        if len(previous) > 1200:
            previous = previous[:1200] + "...[truncated]"
        if previous:
            return (
                f"{base}\n\n"
                "Continuation context:\n"
                "- The previous run reached max iterations before completion.\n"
                "- Continue from the current browser state in this same session.\n"
                "- Avoid repeating already completed steps unless needed for recovery.\n"
                "- Previous partial status (may be incomplete):\n"
                f"{previous}"
            )
        return (
            f"{base}\n\n"
            "Continuation context:\n"
            "- The previous run reached max iterations before completion.\n"
            "- Continue from the current browser state in this same session.\n"
            "- Avoid repeating already completed steps unless needed for recovery."
        )

    @staticmethod
    def _trim_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            return text[:limit] + "...[truncated]"
        return text

    @classmethod
    def _build_failure_summary(
        cls,
        *,
        task: str,
        error: str,
        page_url: str,
        page_title: str,
        final: str,
        screenshot: Any,
        attempt: int,
    ) -> str:
        lines = [
            "Failure summary for continuation:",
            f"- Original task: {cls._trim_text(task, 400) or '(empty)'}",
            f"- Failed attempt: {attempt}",
            f"- Error: {cls._trim_text(error, 300) or '(unknown)'}",
        ]
        if page_url or page_title:
            lines.append(
                f"- Last page: url={cls._trim_text(page_url, 240) or '(unknown)'}, "
                f"title={cls._trim_text(page_title, 120) or '(unknown)'}"
            )
        screenshot_text = cls._trim_text(screenshot, 200)
        if screenshot_text:
            lines.append(f"- Last screenshot: {screenshot_text}")
        final_excerpt = cls._trim_text(final, 1200)
        if final_excerpt:
            lines.append("- Partial output excerpt:")
            lines.append(final_excerpt)
        return "\n".join(lines)

    @staticmethod
    def _build_task_with_failure_context(task: str, failure_summary: str) -> str:
        base = (task or "").strip()
        summary = (failure_summary or "").strip()
        if not summary:
            return base
        return (
            f"{base}\n\n"
            "Previous failed attempt context:\n"
            f"{summary}\n\n"
            "Continuation instructions:\n"
            "- Continue from the current browser state in this same session.\n"
            "- Do not repeat completed steps unless required for recovery.\n"
            "- Prioritize resolving the listed failure."
        )

    async def run_task(
        self,
        task: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        await self.ensure_started()
        sid = self.session_new(session_id)
        rid = (request_id or "").strip() or uuid.uuid4().hex
        effective_timeout = self._resolve_effective_timeout(timeout_s)
        attempts = 2 if self.guardrails.retry_once else 1
        base_task = (task or "").strip()
        previous_failure_summary = self._failure_context_by_session.get(sid, "")

        logger.info(
            "BrowserService.run_task starting "
            f"session_id={sid}, request_id={rid}, timeout={effective_timeout}s, attempts={attempts}, "
            f"driver_mode={self._driver_mode}, prior_failure_context={bool(previous_failure_summary)}"
        )

        async with self._locks[sid]:
            current_task = asyncio.current_task()
            if current_task is not None:
                self._register_inflight_task(sid, rid, current_task)
            try:
                if await self.is_cancelled(sid, rid):
                    await self.clear_cancel(sid, rid)
                    await self.clear_cancel(sid, None)
                    return {
                        "ok": False,
                        "session_id": sid,
                        "request_id": rid,
                        "final": "",
                        "page": {"url": "", "title": ""},
                        "screenshot": None,
                        "error": "cancelled_by_frontend",
                        "attempt": 0,
                        "failure_summary": None,
                    }
                last_error: Optional[str] = None
                used_max_iteration_resume = False
                next_task = self._build_task_with_failure_context(base_task, previous_failure_summary)
                attempt_idx = 0
                max_attempts = attempts + 1  # one extra continuation pass for max-iteration exhaustion
                last_failure_final = ""
                last_failure_page: Dict[str, Any] = {}
                last_failure_screenshot: Any = None
                while attempt_idx < max_attempts:
                    logger.info(
                        "BrowserService.run_task attempt starting "
                        f"session_id={sid}, request_id={rid}, attempt={attempt_idx + 1}, max_attempts={max_attempts}, "
                        f"task_excerpt={self._trim_text(next_task, 180)}"
                    )
                    try:
                        parsed = await asyncio.wait_for(
                            self._run_task_once(task=next_task, session_id=sid, request_id=rid),
                            timeout=float(effective_timeout),
                        )
                        attempt_idx += 1
                        parsed_ok = bool(parsed.get("ok", False))
                        if not parsed_ok:
                            last_error = str(parsed.get("error") or "")
                            last_failure_final = str(parsed.get("final", ""))
                            last_failure_page = parsed.get("page") if isinstance(parsed.get("page"), dict) else {}
                            last_failure_screenshot = parsed.get("screenshot")

                        if (
                            not parsed_ok
                            and self._is_browser_connection_error_text(parsed.get("error") or parsed.get("final"))
                            and attempt_idx < attempts
                        ):
                            try:
                                logger.warning(
                                    "BrowserService.run_task detected browser connection error; restarting runtime "
                                    f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={last_error}"
                                )
                                await self._restart_browser_runtime()
                                next_task = base_task
                                continue
                            except Exception as restart_exc:
                                last_error = f"browser_restart_failed: {restart_exc!r}"
                                logger.error(
                                    "BrowserService.run_task failed to restart browser runtime after connection error "
                                    f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={restart_exc!r}"
                                )

                        if (
                            not parsed_ok
                            and self._is_max_iteration_result(parsed)
                            and not used_max_iteration_resume
                        ):
                            used_max_iteration_resume = True
                            next_task = self._build_resume_task(next_task, str(parsed.get("final", "")))
                            last_error = str(parsed.get("error") or MAX_ITERATION_MESSAGE)
                            logger.warning(
                                "BrowserService.run_task hit max iterations; scheduling continuation "
                                f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={last_error}"
                            )
                            continue

                        page = parsed.get("page") if isinstance(parsed.get("page"), dict) else {}
                        screenshot = self._normalize_screenshot_value(parsed.get("screenshot"))
                        response = {
                            "ok": parsed_ok,
                            "session_id": sid,
                            "request_id": rid,
                            "final": str(parsed.get("final", "")),
                            "page": {
                                "url": str(page.get("url", "")),
                                "title": str(page.get("title", "")),
                            },
                            "screenshot": screenshot,
                            "error": parsed.get("error"),
                            "attempt": attempt_idx,
                        }
                        if parsed_ok:
                            logger.info(
                                "BrowserService.run_task succeeded "
                                f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, "
                                f"page_url={response['page']['url']}, page_title={response['page']['title']}"
                            )
                            self._failure_context_by_session.pop(sid, None)
                            response["failure_summary"] = None
                            return response

                        failure_summary = self._build_failure_summary(
                            task=base_task,
                            error=str(parsed.get("error") or ""),
                            page_url=str(page.get("url", "")),
                            page_title=str(page.get("title", "")),
                            final=str(parsed.get("final", "")),
                            screenshot=parsed.get("screenshot"),
                            attempt=attempt_idx,
                        )
                        self._failure_context_by_session[sid] = failure_summary
                        response["failure_summary"] = failure_summary
                        logger.warning(
                            "BrowserService.run_task returned non-ok result "
                            f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={response['error']}"
                        )
                        return response
                    except TimeoutError:
                        attempt_idx += 1
                        last_error = f"task_timeout: exceeded {effective_timeout}s"
                        logger.warning(
                            "BrowserService.run_task attempt timed out "
                            f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, timeout={effective_timeout}s"
                        )
                        if attempt_idx >= attempts:
                            break
                    except asyncio.CancelledError:
                        await self.clear_cancel(sid, rid)
                        await self.clear_cancel(sid, None)
                        return {
                            "ok": False,
                            "session_id": sid,
                            "request_id": rid,
                            "final": "",
                            "page": {"url": "", "title": ""},
                            "screenshot": None,
                            "error": "cancelled_by_frontend",
                            "attempt": attempt_idx + 1,
                            "failure_summary": None,
                        }
                    except BrowserRunCancelled:
                        attempt_idx += 1
                        await self.clear_cancel(sid, rid)
                        await self.clear_cancel(sid, None)
                        return {
                            "ok": False,
                            "session_id": sid,
                            "request_id": rid,
                            "final": "",
                            "page": {"url": "", "title": ""},
                            "screenshot": None,
                            "error": "cancelled_by_frontend",
                            "attempt": attempt_idx,
                            "failure_summary": None,
                        }
                    except Exception as exc:
                        attempt_idx += 1
                        last_error = str(exc) or repr(exc)
                        logger.warning(
                            "BrowserService.run_task caught exception "
                            f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={last_error}"
                        )
                        if attempt_idx >= attempts:
                            break
                        # Restart before retry on known transport/session failures.
                        if (
                            (not str(exc))
                            or self._is_retryable_transport_error(exc)
                            or self._is_browser_connection_error_text(exc)
                        ):
                            try:
                                logger.warning(
                                    "BrowserService.run_task restarting service after retryable exception "
                                    f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={last_error}"
                                )
                                await self._restart()
                            except Exception as restart_exc:
                                last_error = f"restart_failed: {restart_exc!r}"
                                logger.error(
                                    "BrowserService.run_task failed to restart after retryable exception "
                                    f"session_id={sid}, request_id={rid}, attempt={attempt_idx}, error={restart_exc!r}"
                                )
                                break

                await self.clear_cancel(sid, rid)
                await self.clear_cancel(sid, None)
                page_url = str(last_failure_page.get("url", "")) if isinstance(last_failure_page, dict) else ""
                page_title = str(last_failure_page.get("title", "")) if isinstance(last_failure_page, dict) else ""
                failure_summary = self._build_failure_summary(
                    task=base_task,
                    error=last_error or "unknown browser execution error",
                    page_url=page_url,
                    page_title=page_title,
                    final=last_failure_final,
                    screenshot=last_failure_screenshot,
                    attempt=min(attempt_idx, max_attempts),
                )
                self._failure_context_by_session[sid] = failure_summary
                logger.error(
                    "BrowserService.run_task exhausted attempts "
                    f"session_id={sid}, request_id={rid}, attempts={min(attempt_idx, max_attempts)}, error={last_error}"
                )
                return {
                    "ok": False,
                    "session_id": sid,
                    "request_id": rid,
                    "final": "",
                    "page": {"url": "", "title": ""},
                    "screenshot": None,
                    "error": last_error or "unknown browser execution error",
                    "attempt": min(attempt_idx, max_attempts),
                    "failure_summary": failure_summary,
                }
            finally:
                if current_task is not None:
                    self._unregister_inflight_task(sid, rid, current_task)
                if self._should_close_browser_after_task():
                    try:
                        await self._reset_browser_runtime()
                    except Exception:
                        pass

    async def shutdown(self) -> None:
        try:
            if self.started:
                await Runner.stop()
            self.started = False
        finally:
            await self._stop_managed_driver()
