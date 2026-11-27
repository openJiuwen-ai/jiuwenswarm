"""The personal edition's local, authenticated Agent Memory HTTP listener."""

import logging
import sqlite3
from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from socket import SHUT_RDWR, socket
from threading import Condition, Lock, Thread
from typing import Any
from urllib.parse import urlsplit

import httpx

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.enterprise import is_enterprise

logger = logging.getLogger(__name__)
_HOST = "127.0.0.1"
_MAX_CONNECTIONS = 32
_READ_TIMEOUT = 5.0
_SHUTDOWN_TIMEOUT = 5.0
_REVOCATION_DB = "pending-revocations.sqlite3"


def _memory_root() -> Path:
    return get_agent_root_dir() / "memory" / "managed-http"


def _memory_identity(options: dict[str, Any]) -> tuple[str, str]:
    return str(options.get("identity_org") or "local"), str(options.get("identity_user") or "developer")


class _MemoryHTTPServer(ThreadingHTTPServer):
    """Bound connection handling and track workers independently of server_close."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: Any) -> None:
        self._idle = Condition()
        self._connections: set[socket] = set()
        self._management_calls = 0
        super().__init__(address, handler)

    def process_request(self, request: socket, client_address: tuple[str, int]) -> None:
        with self._idle:
            admitted = len(self._connections) < _MAX_CONNECTIONS
            if admitted:
                self._connections.add(request)
        if not admitted:
            self.shutdown_request(request)
            return
        try:
            request.settimeout(_READ_TIMEOUT)
            super().process_request(request, client_address)
        except BaseException:
            self._connection_done(request)
            raise

    def process_request_thread(self, request: socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_done(request)

    def _connection_done(self, request: socket) -> None:
        with self._idle:
            self._connections.remove(request)
            self._idle.notify_all()

    def disconnect_clients(self) -> None:
        with self._idle:
            for connection in self._connections:
                try:
                    connection.shutdown(SHUT_RDWR)
                except OSError:
                    pass

    def begin_management_call(self) -> None:
        with self._idle:
            self._management_calls += 1

    def end_management_call(self) -> None:
        with self._idle:
            self._management_calls -= 1
            self._idle.notify_all()

    def wait_for_operations(self) -> None:
        with self._idle:
            self._idle.wait_for(lambda: not self._connections and not self._management_calls)


def _memory_security(key: str, org: str, user: str) -> Any:
    from jiuwen_memory import api

    if not hasattr(api, "COLLECTIVE_CONTEXT_HEADER"):
        from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

        return build_dev_security_runtime(identities={key: {"actor": {"org": org, "user": user}, "role": "user"}})

    security = api.build_configured_security_runtime({
        "security": {"default": {"target": "standard", "params": {"authenticator": {
            "target": "trusted", "params": {"gateway_key": key, "key_store": {"target": "memory"}},
        }}}},
    })
    api.register_trusted_principal(security.authenticator, api.Scope(org=org, user=user))
    return security


def _listener_handler(server: Any, org: str, user: str) -> Any:
    handler = server.handler_cls()
    if server.authenticator.mode() != "trusted":
        return handler

    class LocalMemoryHandler(handler):
        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if parsed and self.command == "POST":
                for name, value in (("X-Org-Id", org), ("X-Principal-Type", "user"), ("X-Principal-Id", user)):
                    del self.headers[name]
                    self.headers[name] = value
            return parsed

    return LocalMemoryHandler


def supports_managed_memory_portal() -> bool:
    from jiuwen_memory_entry.http_server.__main__ import HttpServer

    return hasattr(HttpServer, "build_personal")


def _build_server(host_config: dict[str, Any], options: dict[str, Any], root: Path, key: str) -> Any:
    from jiuwen_memory_entry.http_server.__main__ import HttpServer, load_config
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import _agent_default_config

    from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
        _inject_sdk_creds_and_root,
        _validate_sdk_extraction,
    )
    from jiuwenswarm.common.config import _merge_config_dict

    sdk = _agent_default_config()
    source = options.get("sdk", {})
    globals_source = source.get("globals", {})
    if "embedder_dim" in globals_source:
        sdk["globals"]["embedder_dim"] = globals_source["embedder_dim"]
    sdk["globals"].update({
        k: v for k, v in globals_source.items() if k.startswith(("llm_", "embedder_"))
    })
    for name in ("llm", "embedder", "extractor"):
        if name in source:
            _merge_config_dict(sdk, {name: source[name]})
    _validate_sdk_extraction({"sdk": sdk})
    sdk["markdown_store"]["default"]["params"]["root"] = str(root)
    sdk["kv_store"]["default"] = {"target": "sqlite", "params": {"db_path": str(root / "content.sqlite3")}}
    sdk["audit"]["default"]["params"]["db_path"] = str(root / "audit.sqlite3")
    _inject_sdk_creds_and_root(sdk, host_config)

    org, user = _memory_identity(options)
    config = load_config([{"memory_api": sdk}])
    security_runtime = _memory_security(key, org, user)
    if not supports_managed_memory_portal():
        return HttpServer.build(config, security_runtime=security_runtime)

    from jiuwen_memory.api import Scope, document_llm_instances

    owner = Scope(org=org, user=user)

    def authorize_configuration(context: Any, operation: str) -> bool:
        if context.auth.actor != owner:
            return False
        server.api.check_configuration_access(owner, security=context, write=operation == "write")
        return True

    server = HttpServer.build_personal(
        config,
        llm_instances=document_llm_instances(sdk),
        authorize=authorize_configuration,
        security_runtime=security_runtime,
    )
    return server


@dataclass(frozen=True)
class _Listener:
    server: Any
    httpd: _MemoryHTTPServer
    thread: Thread
    api_key: str = field(repr=False)
    identity_org: str
    identity_user: str

    @property
    def url(self) -> str:
        return f"http://{_HOST}:{self.httpd.server_port}"


class ManagedMemoryHttp:
    """Own the authenticated, process-local HTTP listener for the memory plugin."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._revocation_lock = Lock()
        self._listener: _Listener | None = None
        self._closing: Future[None] | None = None

    def status(self) -> dict[str, Any]:
        listener = self._listener
        if listener is None or not listener.thread.is_alive():
            return {"running": False}
        return {"running": True, "base_url": listener.url}

    def owns_url(self, url: str) -> bool:
        return self.status().get("base_url") == url

    def connection_options(self) -> dict[str, str]:
        listener = self._listener
        if listener is None or not listener.thread.is_alive():
            raise ValueError("Built-in memory HTTP service is not running")
        return {
            "base_url": listener.url,
            "api_key": listener.api_key,
            "identity_org": listener.identity_org,
            "identity_user": listener.identity_user,
        }

    def collective_owner(self) -> tuple[str, str, str] | None:
        """Resolve the personal listener's identity without exposing its token."""
        listener = self._listener
        if listener is None or not listener.thread.is_alive():
            return None
        return listener.identity_org, "", listener.identity_user

    def call_collective_management(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run host-only membership operations on the listener's own store."""
        if method not in {"init_collective_dimension", "add_collective_members", "remove_collective_members",
                          "list_collective_members"}:
            raise ValueError("Unsupported collective management operation")
        with self._lock:
            listener = self._listener
            if listener is None or not listener.thread.is_alive():
                raise ValueError("Built-in memory HTTP service is not running")
            listener.httpd.begin_management_call()
        try:
            return getattr(listener.server.api, method)(*args, **kwargs)
        finally:
            listener.httpd.end_management_call()

    def revoke_membership(self, kind: str, subject: str, *, team: str = "", project: str = "") -> None:
        """Journal a revocation before attempting it on the personal listener."""
        with self._lock:
            listener = self._listener
            if listener is None or not listener.thread.is_alive():
                listener = None
                _, options = bundled_memory_http_config()
                org, user = _memory_identity(options)
            else:
                org, user = listener.identity_org, listener.identity_user
            root = _memory_root()
            root.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(root / _REVOCATION_DB, timeout=1, isolation_level=None)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "org TEXT, user TEXT, kind TEXT, subject TEXT, team TEXT, project TEXT)"
                )
                db.execute(
                    "INSERT INTO pending (org, user, kind, subject, team, project) VALUES (?, ?, ?, ?, ?, ?)",
                    (org, user, kind, subject, team, project),
                )
            if listener is not None:
                listener.httpd.begin_management_call()
        if listener is not None:
            try:
                self._replay_revocations(listener.server.api, root, org, user)
            finally:
                listener.httpd.end_management_call()

    def _replay_revocations(self, api: Any, root: Path, org: str, user: str) -> None:
        from jiuwenswarm.agents.harness.common.memory.team_memory_members import (
            apply_membership_revocation,
            supports_team_memory_membership,
        )

        path = root / _REVOCATION_DB
        if not path.is_file() or not supports_team_memory_membership():
            return
        with self._revocation_lock, closing(sqlite3.connect(path, timeout=1, isolation_level=None)) as db:
            pending = db.execute(
                "SELECT id, kind, subject, team, project FROM pending WHERE org = ? AND user = ? ORDER BY id",
                (org, user),
            ).fetchall()
            for event_id, kind, subject, team, project in pending:
                apply_membership_revocation(api, org, user, kind, subject, team, project)
                db.execute("DELETE FROM pending WHERE id = ?", (event_id,))

    def start(self, host_config: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closing is not None:
                if not self._closing.done():
                    raise ValueError("Built-in memory HTTP service is still shutting down")
                self._finish_close_locked()
            if self._listener is not None and not self._listener.thread.is_alive():
                self._stop_locked()
            if self._listener is None:
                try:
                    self._listener = self._start_listener(host_config, options)
                except Exception as exc:
                    # Some upstream startup errors include configuration values.
                    logger.warning("Managed memory HTTP startup failed (%s)", type(exc).__name__)
                    raise ValueError("Could not start the built-in memory HTTP service") from None
            return {"running": True, "base_url": self._listener.url, "api_key": self._listener.api_key}

    def _start_listener(self, host_config: dict[str, Any], options: dict[str, Any]) -> _Listener:
        root = _memory_root()
        root.mkdir(parents=True, exist_ok=True)
        key = token_urlsafe(32)
        org, user = _memory_identity(options)
        server = _build_server(host_config, options, root, key)
        httpd = None
        try:
            server.start()
            self._replay_revocations(server.api, root, org, user)
            httpd = _MemoryHTTPServer((_HOST, 0), _listener_handler(server, org, user))
            thread = Thread(target=httpd.serve_forever, name="managed-memory-http", daemon=True)
            thread.start()
            return _Listener(server, httpd, thread, key, org, user)
        except BaseException:
            if httpd is not None:
                httpd.server_close()
            server.close()
            raise

    def _stop_locked(self) -> None:
        listener = self._listener
        if listener is not None:
            shutdown_future: Future[None] = Future()
            Thread(target=self._close_listener, args=(listener, shutdown_future),
                   name="managed-memory-http-close", daemon=True).start()
            self._closing = shutdown_future
            self._listener = None
        self._finish_close_locked()

    @staticmethod
    def _close_listener(listener: _Listener, shutdown_future: Future[None]) -> None:
        try:
            try:
                if listener.thread.is_alive():
                    listener.httpd.shutdown()
                listener.thread.join()
            finally:
                try:
                    listener.httpd.server_close()
                finally:
                    listener.httpd.disconnect_clients()
                    listener.httpd.wait_for_operations()
                    listener.server.close()
        except BaseException as exc:
            shutdown_future.set_exception(exc)
        else:
            shutdown_future.set_result(None)

    def _finish_close_locked(self) -> None:
        shutdown_future = self._closing
        if shutdown_future is not None:
            try:
                shutdown_future.result(timeout=_SHUTDOWN_TIMEOUT)
            finally:
                if shutdown_future.done() and shutdown_future.exception() is None:
                    self._closing = None

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_locked()
            return {"running": False}


managed_memory_http = ManagedMemoryHttp()


def _bundled_memory_plugin_settings(host: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from jiuwenswarm.agents.harness.common.memory.plugin import MEMORY_PLUGIN_ID
    from jiuwenswarm.server.runtime.extension_package_manager import (
        bundled_memory_plugin_dir,
        read_plugin_package_config,
    )

    package = read_plugin_package_config(bundled_memory_plugin_dir())
    plugins = host.get("plugins", {})
    overrides = plugins.get(MEMORY_PLUGIN_ID, {}) if isinstance(plugins, dict) else None
    if not isinstance(overrides, dict):
        raise ValueError("Plugin configuration must be a mapping")
    return package, overrides


def bundled_memory_http_config() -> tuple[dict[str, Any], dict[str, Any]]:
    from jiuwenswarm.agents.harness.common.memory.plugin import memory_http_server_options

    host = get_config()
    return host, memory_http_server_options(host, *_bundled_memory_plugin_settings(host))


async def open_bundled_memory_portal(host: dict[str, Any]) -> tuple[str, str]:
    """Issue a browser launch link using the plugin's live listener and personal scope."""
    from jiuwenswarm.agents.harness.common.memory.plugin import resolve_memory_plugin_options

    if not supports_managed_memory_portal():
        raise ImportError("Installed JiuwenMemory does not provide the managed HTTP Portal")

    options = resolve_memory_plugin_options(host, *_bundled_memory_plugin_settings(host))
    base_url = options["base_url"]
    agent = str(options.get("agent_id") or "main_agent")
    user = str(options.get("user_id") or "__default__")
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(
            f"{base_url}/portal/api/launch",
            headers={"Origin": base_url, "Authorization": f"Bearer {options['api_key']}"},
            json={"write_target": {"space": "", "agent": agent, "session": user}},
        )
        response.raise_for_status()
        url = response.json()["portal_url"]
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.netloc) != ("http", urlsplit(base_url).netloc):
        raise ValueError("Invalid managed memory Portal origin")
    return agent, url


def start_bundled_memory_http() -> None:
    """Start the personal edition listener with host-side model configuration."""
    if not is_enterprise():
        managed_memory_http.start(*bundled_memory_http_config())


def public_memory_plugin_settings() -> dict[str, Any]:
    return {"mode": "http", "managed_http": managed_memory_http.status()}
