"""Manager for hot-pluggable inference privacy proxies.

Provides CRUD operations for proxy configurations and runtime management:
- create/start/stop/update/delete proxies
- list proxies and their status
- get logs for debugging
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import _contains_control_chars, _contains_crlf_or_null
from jiuwenbox.proxy.inference_privacy_proxy import InferencePrivacyProxyConfig, ProxyRoute, InferencePrivacyProxy

configure_logging()
logger = logging.getLogger(__name__)


def _strip_trailing_newline(value: str) -> str:
    """Remove a single trailing CR/LF/CRLF; preserve internal spaces and special chars."""
    if value.endswith("\n"):
        value = value[:-1]
    if value.endswith("\r"):
        value = value[:-1]
    return value


def _resolve_basic_credentials(basic_auth) -> tuple[str, str, str]:
    """Resolve and validate Basic credentials from a ``ProxyBasicAuth``.

    Performs all structural checks here (rather than in the Pydantic model) so
    that a rejection raises ``ValueError`` with a generic message that never
    contains the secret. Returns ``(username, password, password_file)``.

    Checks: non-empty username; exactly one of password / password_file; file
    exists, is a regular file, and is readable; CR/LF/NUL/control-char
    injection prevention on username and resolved password.
    """
    username = basic_auth.username
    if not username or not username.strip():
        raise ValueError("basic_auth.username cannot be empty")
    if _contains_crlf_or_null(username) or _contains_control_chars(username):
        raise ValueError("basic_auth.username contains invalid characters")

    password_file = basic_auth.password_file
    if basic_auth.password and password_file:
        raise ValueError(
            "basic_auth.password and basic_auth.password_file are mutually exclusive"
        )
    if not basic_auth.password and not password_file:
        raise ValueError("basic_auth requires one of password or password_file")

    if password_file:
        path = Path(password_file)
        if not path.is_file():
            raise ValueError(
                f"basic_auth.password_file not found or not a regular file: {password_file}"
            )
        if not os.access(path, os.R_OK):
            raise ValueError(
                f"basic_auth.password_file is not readable: {password_file}"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"basic_auth.password_file is not readable: {password_file}"
            ) from exc
        password = _strip_trailing_newline(content)
        if not password:
            # An empty or newline-only file reduces to "" which has no CR/LF,
            # NUL or control chars and would otherwise pass the injection
            # checks below. Reject it: otherwise the route reports
            # password_configured=True while injecting ``Basic base64(user:)``
            # (empty password) and the upstream returns 401.
            raise ValueError("basic_auth.password_file is empty")
        # Non-blocking permission warning: do not reject 0640/0644 (K8s/Docker
        # Secret defaults); only flag group/other access as a hardening nudge.
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "basic_auth.password_file '%s' is group/other accessible "
                    "(mode=%o); recommend 0600",
                    password_file,
                    mode,
                )
        except OSError:
            pass
    else:
        password = basic_auth.password

    if _contains_crlf_or_null(password) or _contains_control_chars(password):
        raise ValueError("basic_auth.password contains invalid characters")
    return username, password, password_file


def build_proxy_route(route_data) -> ProxyRoute:
    """Build a runtime ``ProxyRoute`` from a ``ProxyRouteEntry``.

    Resolves Basic credentials (reading ``password_file``) so both the YAML
    startup path (``load_from_policy``) and the REST create/update path share
    one runtime model. The resolved password lives only in memory. Raises
    ``ValueError`` (generic, no secret) on auth-scheme conflicts, unreadable
    file, or invalid content.
    """
    if route_data.api_key and route_data.basic_auth is not None:
        raise ValueError("api_key and basic_auth are mutually exclusive")

    basic_username = ""
    basic_password = ""
    basic_password_file = ""
    if route_data.basic_auth is not None:
        basic_username, basic_password, basic_password_file = _resolve_basic_credentials(
            route_data.basic_auth
        )
    return ProxyRoute(
        path_prefix=route_data.path_prefix,
        target_endpoint=route_data.target_endpoint,
        api_key=route_data.api_key,
        skip_cert_verify=route_data.skip_cert_verify,
        basic_username=basic_username,
        basic_password=basic_password,
        basic_password_file=basic_password_file,
    )


def _auth_type(route: ProxyRoute) -> str:
    if route.basic_username:
        return "basic"
    if route.api_key:
        return "api_key"
    return "none"


def _redact_basic(route: ProxyRoute) -> dict[str, Any] | None:
    """Return a password-free view of a route's Basic auth config, or None."""
    if not route.basic_username:
        return None
    return {
        "username": route.basic_username,
        "password_configured": True,
        "password_file": route.basic_password_file,
    }


class ProxyState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class ProxyInstance:
    config: InferencePrivacyProxyConfig
    state: ProxyState = ProxyState.STOPPED
    proxy: InferencePrivacyProxy | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    error_message: str | None = None
    log_lines: list[str] = field(default_factory=list)
    route_states: dict[str, ProxyState] = field(default_factory=dict)
    _log_max_lines: int = 1000

    def add_log(self, line: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        self.log_lines.append(f"[{timestamp}] {line}")
        if len(self.log_lines) > self._log_max_lines:
            self.log_lines = self.log_lines[-self._log_max_lines:]

    def get_logs(self) -> str:
        return "\n".join(self.log_lines)

    def get_route_state(self, route_name: str) -> ProxyState:
        return self.route_states.get(route_name, ProxyState.STOPPED)

    def set_route_state(self, route_name: str, state: ProxyState) -> None:
        self.route_states[route_name] = state

    def get_enabled_route_count(self) -> int:
        return sum(1 for s in self.route_states.values() if s == ProxyState.RUNNING)


class InferencePrivacyProxyManager:
    def __init__(self) -> None:
        self._proxies: dict[str, ProxyInstance] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _route_name(route: ProxyRoute) -> str:
        return route.path_prefix.lstrip("/").replace("/", "-") or "default"

    def reset(self) -> None:
        """Clear all tracked proxies. Intended for isolated tests."""
        self._proxies.clear()

    def get_global_instance(self) -> ProxyInstance | None:
        """Return the shared proxy instance for test inspection."""
        return self._proxies.get("default")

    async def list_proxies(self) -> list[dict[str, Any]]:
        """List all proxy configurations and their state.
        
        Returns one entry per route with independent state.
        """
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                return []

            result = []
            for route in global_instance.config.routes:
                proxy_name = self._route_name(route)
                route_state = global_instance.get_route_state(proxy_name)
                result.append({
                    "name": proxy_name,
                    "state": route_state.value,
                    "listen_host": global_instance.config.listen_host,
                    "listen_port": global_instance.config.listen_port,
                    "route": {
                        "path_prefix": route.path_prefix,
                        "target_endpoint": route.target_endpoint,
                        "api_key": route.api_key[:10] + "..." if len(route.api_key) > 10 else route.api_key,
                        "auth_type": _auth_type(route),
                        "basic_auth": _redact_basic(route),
                    },
                    "created_at": global_instance.created_at.isoformat() if global_instance.created_at else None,
                    "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                    "error_message": global_instance.error_message,
                })
            return result

    async def get_proxy(self, name: str) -> dict[str, Any] | None:
        """Get a specific proxy's details (route info with its own state)."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                return None

            for route in global_instance.config.routes:
                proxy_name = self._route_name(route)
                if proxy_name == name:
                    route_state = global_instance.get_route_state(proxy_name)
                    return {
                        "name": name,
                        "state": route_state.value,
                        "listen_host": global_instance.config.listen_host,
                        "listen_port": global_instance.config.listen_port,
                        "route": {
                            "path_prefix": route.path_prefix,
                            "target_endpoint": route.target_endpoint,
                            "api_key": route.api_key,
                            "skip_cert_verify": route.skip_cert_verify,
                            "target_host": route.target_host,
                            "target_port": route.target_port,
                            "use_tls": route.use_tls,
                            "auth_type": _auth_type(route),
                            "basic_auth": _redact_basic(route),
                        },
                        "created_at": global_instance.created_at.isoformat() if global_instance.created_at else None,
                        "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                        "error_message": global_instance.error_message,
                    }
            return None

    async def create_proxy(self, name: str, config: InferencePrivacyProxyConfig) -> dict[str, Any]:
        """Create or add a route to the global proxy."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            
            if global_instance is not None and global_instance.config.listen_port == 0:
                raise ValueError(
                    "Cannot create route: proxy disabled (listen_port=0). "
                    "Set listen_port > 0 in policy YAML first."
                )
            
            if global_instance is None and config.listen_port == 0:
                raise ValueError(
                    "Cannot create route: listen_port=0 (disabled). "
                    "Set listen_port > 0 in policy YAML first."
                )
            
            if global_instance is None:
                global_instance = ProxyInstance(config=config)
                global_instance.state = ProxyState.STOPPED
                for route in config.routes:
                    route_name = self._route_name(route)
                    global_instance.set_route_state(route_name, ProxyState.STOPPED)
                global_instance.add_log(f"Global proxy created with {len(config.routes)} routes")
                self._proxies["default"] = global_instance
                logger.info("Created global proxy: listen_port=%d, routes=%d", config.listen_port, len(config.routes))
            else:
                for route in config.routes:
                    route_name = self._route_name(route)
                    existing_names = [
                        self._route_name(existing_route)
                        for existing_route in global_instance.config.routes
                    ]
                    if route_name in existing_names:
                        raise ValueError(f"Proxy '{name}' already exists")
                    global_instance.config.routes.append(route)
                    global_instance.set_route_state(route_name, ProxyState.STOPPED)
                    global_instance.add_log(f"Added route: {route.path_prefix} -> {route.target_endpoint}")
                    logger.info("Added route '%s' to global proxy", route_name)
            
            route = config.routes[0] if config.routes else None
            proxy_name = self._route_name(route) if route else name
            route_state = global_instance.get_route_state(proxy_name)

            return {
                "name": proxy_name,
                "state": route_state.value,
                "created_at": global_instance.created_at.isoformat(),
            }

    async def start_proxy(self, name: str) -> dict[str, Any]:
        """Start a specific route (enable it for routing)."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                raise ValueError(f"Proxy '{name}' not found")

            route = None
            for r in global_instance.config.routes:
                if self._route_name(r) == name:
                    route = r
                    break

            if route is None:
                raise ValueError(f"Proxy '{name}' not found")

            if global_instance.config.listen_port == 0:
                warning_msg = (
                    "Cannot start proxy: listen_port=0 (disabled). "
                    "Proxy is disabled by default. "
                    "Set listen_port > 0 in policy YAML to enable."
                )
                global_instance.add_log(warning_msg)
                logger.warning(warning_msg)
                return {
                    "name": name,
                    "state": global_instance.get_route_state(name).value,
                    "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                    "error_message": None,
                }

            current_state = global_instance.get_route_state(name)
            if current_state == ProxyState.RUNNING:
                global_instance.add_log(f"Route '{name}' already running")
                return {
                    "name": name,
                    "state": current_state.value,
                    "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                }

            global_instance.set_route_state(name, ProxyState.STARTING)
            global_instance.add_log(f"Starting route '{name}'...")

            try:
                if global_instance.proxy is None or not global_instance.proxy.is_running:
                    proxy = InferencePrivacyProxy(global_instance.config, log_callback=global_instance.add_log)
                    await proxy.start()
                    global_instance.proxy = proxy
                    global_instance.state = ProxyState.RUNNING
                    global_instance.started_at = datetime.now(timezone.utc)
                    global_instance.add_log(f"Global proxy started on port {global_instance.config.listen_port}")
                    logger.info("Global proxy started on port %d", global_instance.config.listen_port)

                global_instance.proxy.enable_route(route.path_prefix)
                global_instance.set_route_state(name, ProxyState.RUNNING)
                global_instance.add_log(f"Route '{name}' enabled for routing")
                logger.info("Route '%s' enabled", name)
            except Exception as e:
                global_instance.set_route_state(name, ProxyState.ERROR)
                global_instance.error_message = str(e)
                global_instance.add_log(f"Failed to start route '{name}': {e}")
                logger.error("Failed to start route '%s': %s", name, e)
                raise

            return {
                "name": name,
                "state": global_instance.get_route_state(name).value,
                "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                "error_message": global_instance.error_message,
            }

    async def stop_proxy(self, name: str) -> dict[str, Any]:
        """Stop a specific route (disable it, stop global proxy if all routes disabled)."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                raise ValueError(f"Proxy '{name}' not found")

            route = None
            for r in global_instance.config.routes:
                if self._route_name(r) == name:
                    route = r
                    break

            if route is None:
                raise ValueError(f"Proxy '{name}' not found")

            current_state = global_instance.get_route_state(name)
            if current_state == ProxyState.STOPPED:
                global_instance.add_log(f"Route '{name}' already stopped")
                return {
                    "name": name,
                    "state": current_state.value,
                }

            global_instance.set_route_state(name, ProxyState.STOPPING)
            global_instance.add_log(f"Stopping route '{name}'...")

            try:
                if global_instance.proxy:
                    global_instance.proxy.disable_route(route.path_prefix)
                    global_instance.add_log(f"Route '{name}' disabled")

                global_instance.set_route_state(name, ProxyState.STOPPED)

                enabled_count = global_instance.get_enabled_route_count()
                if enabled_count == 0 and global_instance.proxy:
                    global_instance.add_log("All routes disabled, stopping global proxy...")
                    await global_instance.proxy.stop()
                    global_instance.proxy = None
                    global_instance.state = ProxyState.STOPPED
                    global_instance.started_at = None
                    global_instance.add_log("Global proxy stopped")
                    logger.info("Global proxy stopped (no enabled routes)")

                logger.info("Route '%s' stopped", name)
            except Exception as e:
                global_instance.set_route_state(name, ProxyState.ERROR)
                global_instance.error_message = str(e)
                global_instance.add_log(f"Failed to stop route '{name}': {e}")
                logger.error("Failed to stop route '%s': %s", name, e)
                raise

            return {
                "name": name,
                "state": global_instance.get_route_state(name).value,
                "error_message": global_instance.error_message,
            }

    async def update_proxy(self, name: str, config: InferencePrivacyProxyConfig) -> dict[str, Any]:
        """Update a route in the global proxy (keeps path_prefix, updates target/api_key)."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                raise ValueError(f"Proxy '{name}' not found")

            route_idx = None
            old_path_prefix = None
            for i, r in enumerate(global_instance.config.routes):
                derived_name = r.path_prefix.lstrip("/").replace("/", "-") or "default"
                if derived_name == name:
                    route_idx = i
                    old_path_prefix = r.path_prefix
                    break

            if route_idx is None:
                raise ValueError(f"Proxy '{name}' not found")

            was_running = global_instance.state == ProxyState.RUNNING

            if was_running:
                global_instance.state = ProxyState.STOPPING
                global_instance.add_log("Stopping proxy for route update...")
                try:
                    if global_instance.proxy:
                        await global_instance.proxy.stop()
                        global_instance.proxy = None
                except Exception as e:
                    global_instance.add_log(f"Failed to stop for update: {e}")

            if config.routes:
                new_route = config.routes[0]
                updated_route = ProxyRoute(
                    path_prefix=old_path_prefix,
                    target_endpoint=new_route.target_endpoint,
                    api_key=new_route.api_key,
                    skip_cert_verify=new_route.skip_cert_verify,
                    basic_username=new_route.basic_username,
                    basic_password=new_route.basic_password,
                    basic_password_file=new_route.basic_password_file,
                )
                global_instance.config.routes[route_idx] = updated_route
                global_instance.add_log(f"Route updated: {old_path_prefix} -> {updated_route.target_endpoint}")

            if was_running:
                global_instance.state = ProxyState.STARTING
                global_instance.add_log("Restarting proxy with updated route...")
                try:
                    proxy = InferencePrivacyProxy(global_instance.config, log_callback=global_instance.add_log)
                    await proxy.start()
                    global_instance.proxy = proxy
                    global_instance.state = ProxyState.RUNNING
                    global_instance.started_at = datetime.now(timezone.utc)
                    global_instance.add_log("Proxy restarted successfully")
                except Exception as e:
                    global_instance.state = ProxyState.ERROR
                    global_instance.error_message = str(e)
                    global_instance.add_log(f"Failed to restart: {e}")

            logger.info("Updated route '%s'", name)

            return {
                "name": name,
                "state": global_instance.state.value,
                "started_at": global_instance.started_at.isoformat() if global_instance.started_at else None,
                "error_message": global_instance.error_message,
            }

    async def delete_proxy(self, name: str) -> dict[str, Any]:
        """Delete a route from the global proxy."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                raise ValueError(f"Proxy '{name}' not found")

            route_idx = None
            for i, r in enumerate(global_instance.config.routes):
                if self._route_name(r) == name:
                    route_idx = i
                    break

            if route_idx is None:
                raise ValueError(f"Proxy '{name}' not found")

            global_instance.config.routes.pop(route_idx)
            global_instance.add_log(f"Deleted route: {name}")
            logger.info("Deleted route '%s'", name)

            if len(global_instance.config.routes) == 0:
                if global_instance.state == ProxyState.RUNNING:
                    try:
                        if global_instance.proxy:
                            await global_instance.proxy.stop()
                    except Exception as e:
                        logger.warning("Error stopping proxy during delete: %s", e)
                del self._proxies["default"]
                logger.info("Deleted global proxy (no routes remaining)")

            return {"name": name, "deleted": True}

    async def get_proxy_logs(self, name: str, lines: int | None = None) -> dict[str, Any]:
        """Get logs for the global proxy."""
        async with self._lock:
            global_instance = self._proxies.get("default")
            if global_instance is None:
                raise ValueError(f"Proxy '{name}' not found")

            route_exists = any(
                self._route_name(r) == name
                for r in global_instance.config.routes
            )
            if not route_exists:
                raise ValueError(f"Proxy '{name}' not found")

            logs = global_instance.get_logs()
            if lines:
                log_list = logs.split("\n")
                logs = "\n".join(log_list[-lines:])

            return {
                "name": name,
                "logs": logs,
            }

    async def load_from_policy(self, proxies_config: Any) -> None:
        """Load global proxy from policy configuration at startup.
        
        Creates ONE global proxy with ALL routes sharing the same listen_port.
        All routes are started (enabled) by default.
        """
        if not proxies_config or proxies_config.listen_port <= 0:
            return

        # Build each route independently so a single misconfigured route
        # (e.g. a Basic route whose password_file is missing/unreadable or
        # whose password contains invalid characters) is skipped without
        # blocking the remaining valid Bearer / X-Api-Key / Basic / no-auth
        # routes. ``build_proxy_route`` raises generic, secret-free
        # ``ValueError`` for every expected assembly failure.
        routes: list[ProxyRoute] = []
        skipped: list[str] = []
        for route_data in proxies_config.routes or []:
            prefix = getattr(route_data, "path_prefix", "<unknown>")
            try:
                routes.append(build_proxy_route(route_data))
            except ValueError as e:
                # Secret-free message by construction (see
                # ``_resolve_basic_credentials``); skip this route only.
                skipped.append(prefix)
                logger.error("Skipping proxy route '%s' during policy load: %s", prefix, e)
            except Exception:  # noqa: BLE001
                # Unexpected assembly error: skip without echoing the message
                # (library reprs may echo raw input) so no credential can leak.
                skipped.append(prefix)
                logger.error(
                    "Skipping proxy route '%s' during policy load: "
                    "unexpected error during route assembly",
                    prefix,
                )

        if not routes:
            if skipped:
                logger.error(
                    "No proxy routes loaded (%d skipped: %s); global proxy not started.",
                    len(skipped),
                    ", ".join(skipped),
                )
            else:
                logger.info("No routes configured")
            return

        try:
            config = InferencePrivacyProxyConfig(
                listen_port=proxies_config.listen_port,
                listen_host=proxies_config.listen_host or "127.0.0.1",
                routes=routes,
            )

            await self.create_proxy("default", config)

            route_names = [r.path_prefix.lstrip("/").replace("/", "-") or "default" for r in routes]
            for route_name in route_names:
                await self.start_proxy(route_name)

            logger.info(
                "Started global proxy on %s:%d with %d routes: %s%s",
                config.listen_host,
                proxies_config.listen_port,
                len(routes),
                ", ".join(f"{r.path_prefix}->{r.target_endpoint}" for r in routes),
                f" (skipped {len(skipped)} invalid: {', '.join(skipped)})" if skipped else "",
            )
        except Exception as e:
            # ``create_proxy`` / ``start_proxy`` failures are not per-route
            # assembly errors; log and keep the server running without the
            # proxy rather than crashing startup.
            logger.error("Failed to load global proxy: %s", e)


_proxy_manager: InferencePrivacyProxyManager | None = None


def get_proxy_manager() -> InferencePrivacyProxyManager:
    """Get the global proxy manager instance."""
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = InferencePrivacyProxyManager()
    return _proxy_manager
