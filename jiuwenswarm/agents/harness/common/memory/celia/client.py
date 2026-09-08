"""Async stdio JSON-RPC client for ``celia_memory_mcp_server``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .client_env import build_child_env
from .config import CeliaConfig
from .errors import CeliaMcpError, CeliaMcpTimeout, CeliaUnavailable
from .protocol import notification, request

logger = logging.getLogger(__name__)

RestartCallback = Callable[[], Awaitable[None] | None]


def _decode_text(value: str) -> object:
    decoded: object = value
    for _ in range(2):
        if not isinstance(decoded, str):
            break
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            break
    return decoded


def _redact_diagnostic(value: str) -> str:
    """Redact common credential forms before writing subprocess diagnostics."""
    return re.sub(
        r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)"
        r"(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1\2<redacted>",
        value[:4000],
    )


def _request_session_id(params: dict[str, Any]) -> str:
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if isinstance(arguments, dict):
        return str(arguments.get("sessionId") or arguments.get("session_id") or "")[:200]
    return ""


class CeliaMcpClient:
    """One MCP subprocess with concurrent request support and restart handling."""

    _MAX_RESTARTS = 10
    _MAX_BACKOFF = 30.0
    _STABILITY_WINDOW = 30.0

    def __init__(self, config: CeliaConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._generation = 0
        self._ready = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._next_id = 1
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._should_restart = True
        self._restart_attempts = 0
        self._ready_at = 0.0
        self._restart_callbacks: list[RestartCallback] = []

    def is_connected(self) -> bool:
        process = self._process
        return bool(process and process.returncode is None and self._ready.is_set())

    def add_restart_callback(self, callback: RestartCallback) -> None:
        self._restart_callbacks.append(callback)

    async def start(self) -> None:
        if self.is_connected():
            return
        async with self._start_lock:
            if self.is_connected():
                return
            self._should_restart = True
            if not self.config.server_binary_path:
                raise CeliaUnavailable("Celia server binary path is empty")
            self._generation += 1
            generation = self._generation
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.config.normalized_binary_path,
                    self.config.normalized_db_path,
                    "--log-file",
                    self.config.log_path or os.devnull,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=build_child_env(self.config),
                )
            except (OSError, ValueError) as exc:
                self._process = None
                raise CeliaUnavailable("failed to spawn Celia MCP server") from exc

            process = self._process
            self._stdout_task = asyncio.create_task(
                self._read_stdout(process, generation),
                name=f"celia-mcp-stdout-{generation}",
            )
            self._stderr_task = asyncio.create_task(
                self._read_stderr(process, generation),
                name=f"celia-mcp-stderr-{generation}",
            )
            try:
                await asyncio.wait_for(
                    self._initialize(generation),
                    timeout=self.config.startup_timeout,
                )
            except Exception:
                await self._terminate_process(process, generation)
                raise

            self._ready_at = time.monotonic()
            self._ready.set()
            self._restart_attempts = 0

    async def wait_ready(self, timeout_ms: int | None = None) -> bool:
        try:
            timeout = None if timeout_ms is None else timeout_ms / 1000.0
            await asyncio.wait_for(self.start(), timeout=timeout)
            return self.is_connected()
        except Exception:
            logger.warning("[CeliaMcpClient] wait_ready failed", exc_info=True)
            return False

    async def _initialize(self, generation: int) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "jiuwenswarm-memory-celia",
                    "version": "0.1.0",
                },
            },
            timeout=self.config.startup_timeout,
            generation=generation,
        )
        await self._write(notification("notifications/initialized"), generation)

    async def list_tools(self) -> set[str]:
        result = await self._request("tools/list", {}, timeout=self.config.request_timeout)
        if not isinstance(result, dict):
            return set()
        tools = result.get("tools")
        if not isinstance(tools, list):
            return set()
        return {
            str(item.get("name"))
            for item in tools
            if isinstance(item, dict) and item.get("name")
        }

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        timeout_ms: int | None = None,
        trace_id: str | None = None,
    ) -> object:
        await self.start()
        augmented = dict(args or {})
        if trace_id and "traceId" not in augmented:
            augmented["traceId"] = trace_id
        timeout = self.config.request_timeout if timeout_ms is None else timeout_ms / 1000.0
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": augmented},
            timeout=timeout,
        )
        if isinstance(result, dict) and result.get("isError"):
            session_id = augmented.get("sessionId") or augmented.get("session_id") or ""
            detail = result.get("error") or result.get("message") or ""
            if not detail and isinstance(result.get("content"), list):
                detail = " ".join(
                    str(item.get("text"))
                    for item in result["content"]
                    if isinstance(item, dict) and item.get("text") is not None
                )
            detail = _redact_diagnostic(str(detail))[:1000]
            logger.warning(
                "[CeliaMcpClient] MCP tool returned isError: method=tools/call "
                "tool=%s error=%s sessionId=%s db=%s",
                name,
                detail,
                str(session_id)[:200],
                self.config.normalized_db_path,
            )
            raise CeliaMcpError(
                f"Celia tool failed: {name}"
                + (f": {detail}" if detail else "")
            )
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if not isinstance(content, list):
            return result
        texts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("text") is not None
        ]
        if not texts:
            return result
        raw = texts[0] if len(texts) == 1 else "\n".join(texts)
        return _decode_text(raw)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        generation: int | None = None,
    ) -> object:
        if not self._process or self._process.returncode is not None:
            raise CeliaUnavailable("Celia MCP server is not connected")
        if generation is not None and generation != self._generation:
            raise CeliaUnavailable("stale Celia MCP generation")

        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(request(request_id, method, params), self._generation)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            logger.warning(
                "[CeliaMcpClient] MCP request timed out: method=%s request_id=%s "
                "sessionId=%s db=%s",
                method,
                request_id,
                _request_session_id(params),
                self.config.normalized_db_path,
            )
            raise CeliaMcpTimeout(f"Celia MCP request timed out: {method}") from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        except Exception as exc:
            self._pending.pop(request_id, None)
            logger.warning(
                "[CeliaMcpClient] MCP request failed: method=%s request_id=%s "
                "exception=%s message=%s sessionId=%s db=%s",
                method,
                request_id,
                type(exc).__name__,
                _redact_diagnostic(str(exc)),
                _request_session_id(params),
                self.config.normalized_db_path,
            )
            raise

    async def _write(self, payload: dict[str, Any], generation: int) -> None:
        process = self._process
        if not process or process.stdin is None:
            raise CeliaUnavailable("Celia MCP stdin is unavailable")
        if generation != self._generation:
            raise CeliaUnavailable("stale Celia MCP generation")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _read_stdout(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        assert process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.warning("[CeliaMcpClient] ignored invalid MCP stdout line")
                    continue
                if not isinstance(payload, dict) or "id" not in payload:
                    continue
                request_id = payload.get("id")
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if payload.get("error"):
                    logger.warning(
                        "[CeliaMcpClient] MCP JSON-RPC error: request_id=%s db=%s",
                        request_id,
                        self.config.normalized_db_path,
                    )
                    future.set_exception(CeliaMcpError(f"Celia MCP error for id={request_id}"))
                else:
                    future.set_result(payload.get("result"))
        except asyncio.CancelledError:
            return
        finally:
            await self._handle_process_exit(process, generation)

    async def _read_stderr(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        assert process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                logger.warning(
                    "[CeliaMcpClient][stderr][%s] %s",
                    generation,
                    _redact_diagnostic(text),
                )
        except asyncio.CancelledError:
            return

    async def _handle_process_exit(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        if generation != self._generation or process is not self._process:
            return
        returncode = await process.wait()
        self._process = None
        self._ready.clear()
        logger.error(
            "[CeliaMcpClient] Celia MCP process exited: code=%s binary=%s db=%s",
            returncode,
            self.config.normalized_binary_path,
            self.config.normalized_db_path,
        )
        self._reject_pending(CeliaUnavailable(f"Celia MCP exited with code {returncode}"))
        for callback in list(self._restart_callbacks):
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.debug("[CeliaMcpClient] restart callback failed", exc_info=True)
        if not self._should_restart:
            return
        if self._ready_at and time.monotonic() - self._ready_at >= self._STABILITY_WINDOW:
            self._restart_attempts = 0
        self._restart_attempts += 1
        self._schedule_restart()

    def _schedule_restart(self) -> None:
        if self._restart_task and not self._restart_task.done():
            return
        if self._restart_attempts > self._MAX_RESTARTS:
            logger.error("[CeliaMcpClient] restart limit reached")
            return
        self._restart_task = asyncio.create_task(self._restart_loop(), name="celia-mcp-restart")

    async def _restart_loop(self) -> None:
        while self._should_restart and self._restart_attempts <= self._MAX_RESTARTS:
            attempt = max(self._restart_attempts, 1)
            delay = min(2 ** (attempt - 1), self._MAX_BACKOFF)
            try:
                await asyncio.sleep(delay)
                if not self._should_restart:
                    return
                await self.start()
                return
            except asyncio.CancelledError:
                return
            except Exception:
                self._restart_attempts += 1
                logger.warning(
                    "[CeliaMcpClient] restart attempt %d failed; next delay <= %.0fs",
                    attempt,
                    self._MAX_BACKOFF,
                    exc_info=True,
                )
        if self._should_restart:
            logger.error("[CeliaMcpClient] restart limit reached")

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
        *,
        grace_ms: int = 5000,
    ) -> None:
        if process is self._process:
            self._process = None
        self._ready.clear()
        self._reject_pending(CeliaUnavailable("Celia MCP process closed"))
        if process.returncode is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), timeout=max(grace_ms, 0) / 1000.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        current = asyncio.current_task()
        for task in (self._stdout_task, self._stderr_task):
            if task and task is not current and not task.done():
                task.cancel()
        self._stdout_task = None
        self._stderr_task = None

    def _reject_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def close(self, grace_ms: int = 5000) -> None:
        self._should_restart = False
        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
        async with self._start_lock:
            process = self._process
            if process is None:
                self._ready.clear()
                self._reject_pending(CeliaUnavailable("Celia MCP process closed"))
                return
            await self._terminate_process(process, self._generation, grace_ms=grace_ms)
