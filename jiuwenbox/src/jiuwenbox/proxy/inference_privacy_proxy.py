"""HTTP-aware inference privacy proxy with path-based routing.

Supports multiple target endpoints through path prefix routing:
- POST http://127.0.0.1:8080/openai/v1/chat/completions
  -> forwards to https://api.openai.com/v1/chat/completions
  -> injects API key for openai route

Features:
- Path-based routing to multiple targets
- API key injection via Authorization header
- Request/response logging
- HTTPS target support
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from jiuwenbox.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


PLACEHOLDER = "<placeholder>"


@dataclass
class ProxyRoute:
    path_prefix: str
    target_endpoint: str
    api_key: str = ""
    skip_cert_verify: bool = False
    _target_host: str = ""
    _target_port: int = 0
    _use_tls: bool = False
    _target_base_path: str = ""

    def __post_init__(self):
        (
            self._target_host,
            self._target_port,
            self._use_tls,
            self._target_base_path,
        ) = self._parse_endpoint(self.target_endpoint)
        if not self.path_prefix.startswith("/"):
            self.path_prefix = "/" + self.path_prefix
        self.path_prefix = self.path_prefix.rstrip("/")

    @staticmethod
    def _parse_endpoint(endpoint: str) -> tuple[str, int, bool, str]:
        """Parse endpoint URL to extract host, port, TLS, and base path."""
        endpoint = endpoint.strip()
        
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            parsed = urlparse(endpoint)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            use_tls = parsed.scheme == "https"
            base_path = parsed.path.rstrip("/") or ""
            return host, port, use_tls, base_path
        
        if ":" in endpoint:
            parts = endpoint.rsplit(":", 1)
            host = parts[0]
            try:
                port = int(parts[1])
                use_tls = port == 443
            except ValueError:
                host = endpoint
                port = 443
                use_tls = True
            return host, port, use_tls, ""
        
        return endpoint, 443, True, ""

    @property
    def target_host(self) -> str:
        return self._target_host

    @property
    def target_port(self) -> int:
        return self._target_port

    @property
    def use_tls(self) -> bool:
        return self._use_tls

    @property
    def target_base_path(self) -> str:
        return self._target_base_path


@dataclass
class InferencePrivacyProxyConfig:
    listen_port: int = 0
    listen_host: str = "127.0.0.1"
    routes: list[ProxyRoute] = field(default_factory=list)


class InferencePrivacyProxy:
    def __init__(
        self,
        config: InferencePrivacyProxyConfig,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._server: asyncio.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._running = False
        self._log_callback = log_callback
        self._ssl_contexts: dict[str, ssl.SSLContext] = {}
        self._enabled_routes: set[str] = set()

    @property
    def is_running(self) -> bool:
        """Return whether the proxy accept loop is active."""
        return self._running

    @property
    def server(self) -> asyncio.Server | None:
        """Expose the listener for tests and management code."""
        return self._server

    def enable_route(self, path_prefix: str) -> None:
        """Enable a route for routing."""
        normalized = path_prefix.rstrip("/")
        self._enabled_routes.add(normalized)
        self._log(f"Route '{normalized}' enabled")

    def disable_route(self, path_prefix: str) -> None:
        """Disable a route (requests will return 404)."""
        normalized = path_prefix.rstrip("/")
        self._enabled_routes.discard(normalized)
        self._log(f"Route '{normalized}' disabled")

    def is_route_enabled(self, path_prefix: str) -> bool:
        """Check if a route is enabled."""
        normalized = path_prefix.rstrip("/")
        return normalized in self._enabled_routes

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._log_callback:
            self._log_callback(message)

    def _get_ssl_context(self, route: ProxyRoute) -> ssl.SSLContext:
        """Get or create SSL context for a route."""
        if route.path_prefix not in self._ssl_contexts:
            ctx = ssl.create_default_context()
            if route.skip_cert_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_contexts[route.path_prefix] = ctx
        return self._ssl_contexts[route.path_prefix]

    def _match_route(self, path: str) -> ProxyRoute | None:
        """Match request path to the longest matching enabled route.
        
        Longest-prefix matching ensures specific routes take precedence
        over generic routes that could incorrectly match due to string
        prefix collision (e.g., '/api' matching '/api-v2/chat').
        
        With single-level path_prefix requirement:
        - '/api' matches '/api/chat' and '/api-v2/chat' (string prefix)
        - '/api-v2' matches '/api-v2/chat' (longer, more specific)
        - Request '/api-v2/chat' -> matches '/api-v2' (len=6) not '/api' (len=4)
        
        Args:
            path: Request path to match
            
        Returns:
            ProxyRoute with longest matching prefix, or None if no match
        """
        path = path.rstrip("/")
        best_match = None
        best_length = 0
        
        for route in self.config.routes:
            if path.startswith(route.path_prefix) and self.is_route_enabled(route.path_prefix):
                prefix_len = len(route.path_prefix)
                if prefix_len > best_length:
                    best_match = route
                    best_length = prefix_len
        
        return best_match

    def match_route(self, path: str) -> ProxyRoute | None:
        """Public wrapper for route matching."""
        return self._match_route(path)

    @staticmethod
    def _rewrite_path(path: str, route: ProxyRoute) -> str:
        """Rewrite request path by stripping prefix and adding target base path."""
        path = path.rstrip("/")
        stripped = path[len(route.path_prefix):]
        
        if not stripped:
            return route.target_base_path or "/"
        
        if route.target_base_path:
            return route.target_base_path + stripped
        return stripped

    def rewrite_path(self, path: str, route: ProxyRoute) -> str:
        """Public wrapper for request path rewriting."""
        return self._rewrite_path(path, route)

    def _inject_api_key(self, headers: bytes, route: ProxyRoute) -> bytes:
        """Inject API key into headers (wildcard replacement).

        Replaces ANY existing key with configured key:
        - Authorization: Bearer <any-key> -> Authorization: Bearer {route.api_key}
        - X-Api-Key: <any-key> -> X-Api-Key: {route.api_key}
        """
        if not route.api_key:
            return headers

        headers_str = headers.decode(errors="replace")

        patterns = [
            (r'Authorization:\s*Bearer\s+\S+', f'Authorization: Bearer {route.api_key}'),
            (r'X-Api-Key:\s*\S+', f'X-Api-Key: {route.api_key}'),
        ]

        result = headers_str
        injected = False

        for pattern, replacement in patterns:
            new_result = re.sub(pattern, replacement, result)
            if new_result != result:
                injected = True
                result = new_result

        if injected:
            self._log(f"Injected API key for route '{route.path_prefix}'")

        return result.encode()

    def inject_api_key(self, headers: bytes, route: ProxyRoute) -> bytes:
        """Public wrapper for API key header injection."""
        return self._inject_api_key(headers, route)

    async def _handle_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        conn_id = id(client_writer)
        peer_addr = client_writer.get_extra_info('peername')
        self._log(f"[{conn_id}] New HTTP connection from {peer_addr or 'unknown'}")

        try:
            request_data = await client_reader.read(65536)
            if not request_data:
                self._log(f"[{conn_id}] No data received")
                return

            request_text = request_data.decode(errors="replace")
            headers_end = request_text.find("\r\n\r\n")
            if headers_end == -1:
                self._log(f"[{conn_id}] Invalid HTTP request: no headers end")
                error_response = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                client_writer.write(error_response.encode())
                await client_writer.drain()
                return

            headers_raw = request_text[:headers_end]
            body = request_text[headers_end + 4:]
            header_lines = headers_raw.split("\r\n")
            
            request_line = header_lines[0]
            parts = request_line.split(" ")
            if len(parts) < 3:
                self._log(f"[{conn_id}] Invalid HTTP request line")
                error_response = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                client_writer.write(error_response.encode())
                await client_writer.drain()
                return

            method = parts[0]
            original_path = parts[1]
            version = parts[2]

            route = self._match_route(original_path)
            if route is None:
                self._log(f"[{conn_id}] No route matched for path: {original_path}")
                error_response = (
                    "HTTP/1.1 404 Not Found\r\n"
                    "Content-Type: text/plain\r\n"
                    "Content-Length: 20\r\n"
                    "\r\n"
                    f"No route matched: {original_path}"
                )
                client_writer.write(error_response.encode())
                await client_writer.drain()
                return

            self._log(f"[{conn_id}] Route '{route.path_prefix}' matched for {method} {original_path}")

            new_path = self._rewrite_path(original_path, route)
            new_request_line = f"{method} {new_path} {version}"
            
            new_headers = []
            for i, line in enumerate(header_lines):
                if i == 0:
                    new_headers.append(new_request_line)
                elif line.lower().startswith("host:"):
                    new_headers.append(f"Host: {route.target_host}")
                else:
                    new_headers.append(line)
            
            new_headers_raw = "\r\n".join(new_headers)
            new_headers_bytes = self._inject_api_key(new_headers_raw.encode(), route)
            
            new_request = new_headers_bytes + "\r\n\r\n".encode() + body.encode(errors="replace")

            ssl_ctx = self._get_ssl_context(route) if route.use_tls else None
            
            target_reader, target_writer = await asyncio.open_connection(
                route.target_host,
                route.target_port,
                ssl=ssl_ctx,
            )
            self._log(f"[{conn_id}] Connected to {route.target_host}:{route.target_port}")

            target_writer.write(new_request)
            await target_writer.drain()

            request_bytes = len(new_request)
            response_bytes = 0

            async def forward_remaining_request():
                nonlocal request_bytes
                try:
                    while True:
                        data = await client_reader.read(8192)
                        if not data:
                            break
                        target_writer.write(data)
                        await target_writer.drain()
                        request_bytes += len(data)
                except Exception as exc:
                    logger.debug("[%s] Request forwarding stopped: %s", conn_id, exc)

            async def forward_response():
                nonlocal response_bytes
                try:
                    while True:
                        data = await target_reader.read(8192)
                        if not data:
                            break
                        client_writer.write(data)
                        await client_writer.drain()
                        response_bytes += len(data)
                except Exception as exc:
                    logger.debug("[%s] Response forwarding stopped: %s", conn_id, exc)

            request_task = asyncio.create_task(forward_remaining_request())
            response_task = asyncio.create_task(forward_response())

            _, pending = await asyncio.wait(
                [request_task, response_task],
                return_when=asyncio.ALL_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug("[%s] Cancelled pending proxy task", conn_id)
            self._log(f"[{conn_id}] Complete: {request_bytes} bytes sent, {response_bytes} bytes received")

        except Exception as e:
            self._log(f"[{conn_id}] Error: {e}")
            try:
                error_response = (
                    "HTTP/1.1 502 Bad Gateway\r\n"
                    "Content-Type: text/plain\r\n"
                    f"Content-Length: {len(str(e))}\r\n"
                    "\r\n"
                    f"{e}"
                )
                client_writer.write(error_response.encode())
                await client_writer.drain()
            except Exception as exc:
                logger.debug("[%s] Failed to send error response: %s", conn_id, exc)
        finally:
            self._log(f"[{conn_id}] Connection closed")
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception as exc:
                logger.debug("[%s] Failed to close client writer: %s", conn_id, exc)

    async def start(self) -> None:
        if self._running:
            return

        self._running = True

        self._server = await asyncio.start_server(
            self._handle_connection,
            self.config.listen_host,
            self.config.listen_port,
        )

        addr = self._server.sockets[0].getsockname()
        routes_info = ", ".join(f"{r.path_prefix}->{r.target_endpoint}" for r in self.config.routes)
        self._log(f"HTTP proxy listening on {addr[0]}:{addr[1]} with routes: {routes_info}")

        async def serve():
            while self._running:
                await asyncio.sleep(1)

        self._serve_task = asyncio.create_task(serve())

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self._serve_task:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except asyncio.CancelledError:
                logger.debug("Proxy serve task cancelled")
            self._serve_task = None

        self._log("HTTP proxy stopped")
