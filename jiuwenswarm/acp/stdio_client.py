# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Minimal ACP JSON-RPC client over subprocess stdin/stdout.

Each message is JSON-RPC over UTF-8. Values may span multiple lines (pretty-printed JSON);
we buffer stdout and parse with ``json.JSONDecoder.raw_decode``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from typing import Any

from jiuwenswarm.acp.subprocess_env import build_acp_subprocess_env

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_TIMEOUT = float(os.getenv("ACP_CHAT_TIMEOUT", "600"))
_DEBUG_CHAT = os.getenv("ACP_CHAT_DEBUG", "").strip().lower() in ("1", "true", "yes")
_JSON_DECODER = json.JSONDecoder()
_MAX_STDOUT_BUFFER = int(os.getenv("ACP_CHAT_MAX_PARSE_BUFFER_BYTES", str(24 * 1024 * 1024)))
_STDOUT_READ_CHUNK = int(os.getenv("ACP_CHAT_STDOUT_READ_CHUNK", str(65536)))
_CLOSE_STDIN_WAIT_S = float(os.getenv("ACP_CLOSE_STDIN_WAIT_S", "4"))
_CLOSE_TERM_WAIT_S = float(os.getenv("ACP_CLOSE_TERM_WAIT_S", "5"))
_CLOSE_KILL_WAIT_S = float(os.getenv("ACP_CLOSE_KILL_WAIT_S", "8"))
_CLOSE_STDERR_JOIN_S = float(os.getenv("ACP_CLOSE_STDERR_JOIN_S", "3"))
_CLOSE_DRAIN_PIPE_S = float(os.getenv("ACP_CLOSE_DRAIN_PIPE_S", "3"))
_KILL_PG = sys.platform != "win32" and os.getenv("ACP_KILL_PROCESS_GROUP", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# When true (default), auto-answer session/request_permission so headless Codex does not stall.
_AUTO_APPROVE = os.getenv("ACP_AUTO_APPROVE_PERMISSIONS", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _jsonrpc_error_message(err: Any) -> str:
    """Human-readable ACP JSON-RPC error for logs and RuntimeError messages."""
    if not isinstance(err, dict):
        return f"ACP JSON-RPC error: {err}"
    code = err.get("code")
    message = err.get("message")
    data = err.get("data")
    parts: list[str] = []
    if code is not None:
        parts.append(str(code))
    if message:
        parts.append(str(message))
    if data is not None:
        parts.append(f"data={data!r}")
    return "ACP JSON-RPC error: " + (" ".join(parts) if parts else repr(err))


def _consume_one_json(buffer: str) -> tuple[Any | None, str]:
    """Parse one JSON value from the start of *buffer* if complete; otherwise (None, *buffer*).

    Leading whitespace is dropped from the consumed prefix; remainder retains extra data
    after the parsed value for the next call.
    """
    s = buffer.lstrip()
    if not s:
        return None, buffer
    if len(s) > _MAX_STDOUT_BUFFER:
        raise RuntimeError(f"ACP stdout parse buffer exceeds {_MAX_STDOUT_BUFFER} bytes")
    try:
        obj, idx = _JSON_DECODER.raw_decode(s)
        rest = s[idx:].lstrip("\r\n\t ")
        return obj, rest
    except json.JSONDecodeError:
        return None, buffer


def _is_peer_jsonrpc_request(msg: dict[str, Any]) -> bool:
    """True if peer sent a JSON-RPC request (expects a response line on stdin).

    Responses carry ``result``/``error``. Notifications omit ``id`` or use notifications only.
    """
    if msg.get("jsonrpc") != "2.0":
        return False
    if not isinstance(msg.get("method"), str):
        return False
    if msg.get("id") is None:
        return False
    if "result" in msg or "error" in msg:
        return False
    return True


def _extract_session_update_text(msg: dict[str, Any]) -> str:
    if msg.get("method") != "session/update":
        return ""
    params = msg.get("params")
    if not isinstance(params, dict):
        return ""
    update = params.get("update")
    if not isinstance(update, dict):
        return ""
    content = update.get("content")
    if not isinstance(content, dict):
        return ""
    if content.get("type") == "text":
        return str(content.get("text") or "")
    return ""


def _merge_env(profile_env: dict[str, Any] | None) -> dict[str, str]:
    return build_acp_subprocess_env(profile_env)


def _resolve_spawn_command(command: str) -> str:
    """Resolve Windows command shims like npx.cmd when shell=False."""
    cmd = (command or "").strip()
    if sys.platform != "win32" or not cmd:
        return cmd
    if any(sep in cmd for sep in ("/", "\\")) or os.path.splitext(cmd)[1]:
        return cmd
    for candidate in (cmd, f"{cmd}.cmd", f"{cmd}.exe", f"{cmd}.bat"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return cmd


def _resolved_session_root(client_cwd: str | None) -> str:
    if client_cwd and client_cwd.strip():
        return os.path.realpath(os.path.abspath(os.path.expanduser(client_cwd.strip())))
    return os.path.realpath(os.getcwd())


def _resolved_path_inside_root(session_root: str, path_in: Any) -> str | None:
    if not isinstance(path_in, str) or not path_in.strip():
        return None
    try:
        cand = os.path.realpath(os.path.abspath(os.path.expanduser(path_in.strip())))
    except OSError:
        return None
    if cand == session_root:
        return cand
    if not cand.startswith(session_root + os.sep):
        return None
    return cand


def _slice_file_lines(text: str, line_raw: Any, limit_raw: Any) -> str:
    lines = text.splitlines(True)
    try:
        line_no = max(1, int(line_raw)) if line_raw is not None else 1
    except (TypeError, ValueError):
        line_no = 1
    start = max(0, line_no - 1)
    try:
        lim = int(limit_raw) if limit_raw is not None else None
    except (TypeError, ValueError):
        lim = None
    if lim is not None and lim > 0:
        return "".join(lines[start:start + lim])
    return "".join(lines[start:])


async def _bounded_drain_reader(stream: asyncio.StreamReader | None, seconds: float) -> None:
    """Read until EOF or *seconds* elapse — ``StreamReader.read`` can block indefinitely without this."""
    if stream is None or seconds <= 0:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        while loop.time() < deadline:
            try:
                blob = await asyncio.wait_for(stream.read(_STDOUT_READ_CHUNK),
                    timeout=max(0.05, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                logger.debug("drain reader stopped on read error: %s", exc)
                break
            if not blob:
                break
    except Exception as exc:
        logger.debug("drain reader stopped: %s", exc)


def _signal_process_leader(proc: asyncio.subprocess.Process, *, brutal: bool) -> None:
    """Signal the managed subprocess (and its POSIX process group when configured)."""
    if proc.pid is None:
        return
    if sys.platform != "win32":
        sig = signal.SIGKILL if brutal else signal.SIGTERM
        if _KILL_PG:
            try:
                os.killpg(proc.pid, sig)
                return
            except OSError as exc:
                logger.debug("killpg(%s) skipped: %s", proc.pid, exc)
        try:
            proc.send_signal(sig)
        except ProcessLookupError as exc:
            logger.debug("send_signal(%s) skipped: %s", proc.pid, exc)
        return
    try:
        if brutal:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError as exc:
        logger.debug("signal process skipped: %s", exc)


class AcpStdioClient:
    """Spawn an ACP-compatible agent and exchange JSON-RPC 2.0 over subprocess stdio."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, Any] | None = None,
    ) -> None:
        self._command = (command or "").strip()
        self._args = list(args or [])
        self._cwd = cwd if cwd else None
        self._env = _merge_env(env)
        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._next_rpc_id = 1
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stdout_buf = ""

    async def _send_json_rpc_response(
        self,
        req_id: Any,
        *,
        result: Any | None = None,
        error: Any | None = None,
    ) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        line = json.dumps(body, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _handle_peer_request(self, msg: dict[str, Any]) -> None:
        """Respond to outbound JSON-RPC from the spawned agent so it does not block."""
        req_id = msg.get("id")
        method = str(msg.get("method") or "").strip()

        # Align with Gateway permission outcome parsing (see interrupt_helpers permiss flow).
        if method == "session/request_permission":
            if _AUTO_APPROVE:
                await self._send_json_rpc_response(
                    req_id,
                    result={
                        "outcome": {
                            "outcome": "selected",
                            "optionId": "allow-once",
                        }
                    },
                )
                logger.info(
                    "[AcpStdioClient] replied to session/request_permission id=%s (allow-once)",
                    req_id,
                )
            else:
                await self._send_json_rpc_response(
                    req_id,
                    result={"outcome": {"outcome": "cancelled"}},
                )
                logger.info(
                    "[AcpStdioClient] replied to session/request_permission id=%s (cancelled)",
                    req_id,
                )
            return

        if method == "fs/read_text_file":
            params = msg.get("params")
            if not isinstance(params, dict):
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32602, "message": "invalid fs/read_text_file params"},
                )
                return
            raw_path = params.get("path")
            root = _resolved_session_root(self._cwd)
            abs_path = _resolved_path_inside_root(root, raw_path)
            if abs_path is None:
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32001, "message": "path denied or invalid"},
                )
                return

            def _load() -> str:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    return fh.read()

            try:
                full = await asyncio.to_thread(_load)
            except FileNotFoundError:
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32002, "message": "ENOENT"},
                )
                return
            except OSError as exc:
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32000, "message": str(exc) or "read failed"},
                )
                return
            chunk = _slice_file_lines(full, params.get("line"), params.get("limit"))
            await self._send_json_rpc_response(req_id, result={"content": chunk})
            if _DEBUG_CHAT:
                logger.info("[AcpStdioClient] fs/read_text_file ok path=%s bytes=%s", abs_path, len(chunk))
            return

        if method == "fs/write_text_file":
            params = msg.get("params")
            if not isinstance(params, dict):
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32602, "message": "invalid fs/write_text_file params"},
                )
                return
            raw_path = params.get("path")
            root = _resolved_session_root(self._cwd)
            abs_path = _resolved_path_inside_root(root, raw_path)
            if abs_path is None:
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32001, "message": "path denied or invalid"},
                )
                return
            wcontent = params.get("content")
            if wcontent is None:
                wbody = ""
            elif isinstance(wcontent, str):
                wbody = wcontent
            else:
                wbody = str(wcontent)

            def _save() -> None:
                os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(wbody)

            try:
                await asyncio.to_thread(_save)
            except OSError as exc:
                await self._send_json_rpc_response(
                    req_id,
                    error={"code": -32000, "message": str(exc) or "write failed"},
                )
                return
            await self._send_json_rpc_response(req_id, result=None)
            if _DEBUG_CHAT:
                logger.info("[AcpStdioClient] fs/write_text_file ok path=%s", abs_path)
            return

        # Minimal stub so agent can progress; callers may tighten per Agent.
        logger.warning("[AcpStdioClient] unhandled peer request method=%s id=%s — error reply", method, req_id)
        await self._send_json_rpc_response(
            req_id,
            error={"code": -32601, "message": f"stdio ACP client: unsupported method {method}"},
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_connected(self) -> bool:
        if self._closed or self._proc is None:
            return False
        rc = self._proc.returncode
        return rc is None

    def _next_id(self) -> int:
        rid = self._next_rpc_id
        self._next_rpc_id += 1
        return rid

    async def _drain_stderr(self) -> None:
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            try:
                txt = line.decode("utf-8", errors="replace").rstrip()
                if txt:
                    if _DEBUG_CHAT:
                        logger.info("[acp-agent stderr] %s", txt)
                    else:
                        logger.debug("[acp-agent stderr] %s", txt)
            except Exception as exc:
                logger.debug("failed to decode stderr line: %s", exc)

    async def _read_one_message(self, timeout: float) -> dict[str, Any] | None:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            while True:
                try:
                    obj, self._stdout_buf = _consume_one_json(self._stdout_buf)
                except RuntimeError as exc:
                    logger.error("%s", exc)
                    raise
                if obj is None:
                    break
                if not isinstance(obj, dict):
                    logger.warning("[AcpStdioClient] dropping non-object JSON-RPC message: %s", type(obj).__name__)
                    continue
                if _DEBUG_CHAT:
                    logger.info(
                        "[AcpStdioClient] rx keys=%s id=%s method=%s",
                        list(obj.keys()),
                        obj.get("id"),
                        obj.get("method"),
                    )
                return obj

            now = asyncio.get_event_loop().time()
            if now >= deadline:
                return None
            remain = max(0.1, deadline - now)
            try:
                chunk = await asyncio.wait_for(
                    self._proc.stdout.read(_STDOUT_READ_CHUNK),
                    timeout=remain,
                )
            except asyncio.TimeoutError:
                continue
            if not chunk:
                if self._stdout_buf.strip():
                    logger.warning(
                        "[AcpStdioClient] EOF while partial JSON buffered (%s bytes)",
                        len(self._stdout_buf),
                    )
                return None
            self._stdout_buf += chunk.decode("utf-8", errors="replace")

    async def _rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> Any:
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remain = max(0.1, deadline - asyncio.get_event_loop().time())
            msg = await self._read_one_message(remain)
            if msg is None:
                proc = self._proc
                if proc is not None and proc.returncode is not None:
                    raise RuntimeError(
                        f"ACP agent process exited ({proc.returncode}) while waiting for {method}."
                    )
                raise RuntimeError(f"ACP agent closed stream or timed out waiting for {method} response")
            if msg.get("method") == "session/update":
                continue
            if _is_peer_jsonrpc_request(msg):
                await self._handle_peer_request(msg)
                continue
            if str(msg.get("id")) == str(rid):
                if "error" in msg:
                    raise RuntimeError(_jsonrpc_error_message(msg["error"]))
                return msg.get("result")

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("AcpStdioClient is closed")
        if not self._command:
            raise ValueError("ACP agent command is empty")

        self._stdout_buf = ""

        spawn_kw: dict[str, Any] = {}
        if _KILL_PG:
            spawn_kw["start_new_session"] = True

        self._proc = await asyncio.create_subprocess_exec(
            _resolve_spawn_command(self._command),
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            **spawn_kw,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        await self._rpc_call(
            "initialize",
            {
                "protocolVersion": 1,
                # Advertise FS so Agents (e.g. Codex ACP) can complete tool turns; omit terminal — unsupported here.
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": "jiuwenswarm", "version": "0.1"},
            },
            timeout=120.0,
        )

        session_cwd = (
            os.path.abspath(os.path.expanduser(self._cwd))
            if self._cwd
            else os.getcwd()
        )
        # Codex ACP expects cwd + mcpServers (see agent deserialization errors).
        new_params: dict[str, Any] = {
            "cwd": session_cwd,
            "mcpServers": [],
        }
        result = await self._rpc_call("session/new", new_params, timeout=60.0)
        if not isinstance(result, dict):
            raise RuntimeError(f"session/new: unexpected result: {result!r}")
        sid = str(result.get("sessionId") or result.get("session_id") or "").strip()
        if not sid:
            raise RuntimeError(f"session/new: missing sessionId in {result!r}")
        self._session_id = sid
        logger.info("[AcpStdioClient] connected sessionId=%s", self._session_id)

    async def chat(self, message: str, *, timeout: float | None = None) -> str:
        if self._closed:
            raise RuntimeError("AcpStdioClient is closed")
        if not self._proc or not self._session_id:
            raise RuntimeError("not connected; call connect() first")

        t_out = timeout if timeout is not None else _DEFAULT_CHAT_TIMEOUT
        rid = self._next_id()
        params = {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": message}],
        }
        payload = {"jsonrpc": "2.0", "id": rid, "method": "session/prompt", "params": params}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

        parts: list[str] = []
        deadline = asyncio.get_event_loop().time() + t_out

        while True:
            remain = max(0.1, deadline - asyncio.get_event_loop().time())
            msg = await self._read_one_message(remain)
            if msg is None:
                proc = self._proc
                if proc is not None and proc.returncode is not None:
                    raise RuntimeError(
                        "ACP agent process exited"
                        f" ({proc.returncode}) before completing session/prompt; check stderr/logs and API auth."
                    )
                raise RuntimeError("timed out or EOF waiting for session/prompt completion")

            if msg.get("method") == "session/update":
                chunk = _extract_session_update_text(msg)
                if chunk:
                    parts.append(chunk)
                continue

            if _is_peer_jsonrpc_request(msg):
                await self._handle_peer_request(msg)
                continue

            if str(msg.get("id")) == str(rid):
                if "error" in msg:
                    raise RuntimeError(_jsonrpc_error_message(msg["error"]))
                return "".join(parts).strip() or _result_text_fallback(msg.get("result"))

            logger.debug("[AcpStdioClient] ignoring unrelated message keys=%s", list(msg.keys()))

    async def aclose(self) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True
        proc = self._proc
        self._proc = None
        stderr_task = self._stderr_task
        self._stderr_task = None
        self._session_id = None

        if proc is None:
            if stderr_task:
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError as exc:
                    logger.debug("stderr task cancelled during close: %s", exc)
            return

        if proc.stdin is not None:
            try:
                closing = getattr(proc.stdin, "is_closing", None)
                if not (callable(closing) and closing()):
                    proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.stdin.wait_closed(), timeout=_CLOSE_STDIN_WAIT_S)
                    except asyncio.TimeoutError as exc:
                        logger.debug("stdin wait_closed timed out after %ss: %s", _CLOSE_STDIN_WAIT_S, exc)
            except OSError as exc:
                logger.debug("closing stdin failed: %s", exc)
            except RuntimeError as exc:
                logger.debug("closing stdin failed: %s", exc)

        if proc.returncode is None:
            try:
                _signal_process_leader(proc, brutal=False)
                await asyncio.wait_for(proc.wait(), timeout=_CLOSE_TERM_WAIT_S)
            except asyncio.TimeoutError as exc:
                logger.debug("process wait (TERM) timed out after %ss: %s", _CLOSE_TERM_WAIT_S, exc)
            except Exception as exc:
                logger.debug("process wait (TERM) failed: %s", exc)

        if proc.returncode is None:
            try:
                _signal_process_leader(proc, brutal=True)
                await asyncio.wait_for(proc.wait(), timeout=_CLOSE_KILL_WAIT_S)
            except Exception as exc:
                logger.debug("process wait (KILL) failed: %s", exc)

        if stderr_task:
            try:
                await asyncio.wait_for(stderr_task, timeout=_CLOSE_STDERR_JOIN_S)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                logger.debug("stderr task join timed out or cancelled: %s", exc)
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError as cancel_exc:
                    logger.debug("stderr task cancelled after join timeout: %s", cancel_exc)
                except Exception as join_exc:
                    logger.debug("stderr task failed after join timeout: %s", join_exc)

        await _bounded_drain_reader(proc.stdout, _CLOSE_DRAIN_PIPE_S)
        await _bounded_drain_reader(proc.stderr, _CLOSE_DRAIN_PIPE_S)

        await asyncio.sleep(0)


def _result_text_fallback(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        # Some agents may only return stopReason
        sr = result.get("stopReason") or result.get("stop_reason")
        return str(sr) if sr else json.dumps(result, ensure_ascii=False)
    return str(result)
