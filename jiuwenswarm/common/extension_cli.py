# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Instance-owned local control transport for extension CLI subprocesses."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
METHODS = frozenset({"install", "query", "reload", "uninstall"})
MAX_FRAME = 8 * 1024 * 1024
ENV_PREFIX = "JIUWENSWARM_EXTENSION_CLI_"
Handler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def merge_query_states(local: Mapping[str, Any], remote: Mapping[str, Any]) -> dict[str, Any]:
    """Merge Gateway and AgentServer query states for a cross-host query."""

    merged = dict(remote)
    merged["active"] = list(local.get("active") or []) + list(remote.get("active") or [])
    merged["installed"] = list(local.get("installed") or []) + list(remote.get("installed") or [])
    merged["installed_packages"] = (
        list(local.get("installed_packages") or []) + list(remote.get("installed_packages") or [])
    )
    merged["published"] = {**remote.get("published", {}), **local.get("published", {})}
    merged["inflight"] = {**remote.get("inflight", {}), **local.get("inflight", {})}
    return merged


class ControlError(Exception):
    """A local management failure that contains no connection credentials."""


def context_path(domain: str) -> Path:
    """Return exactly one current-instance record; never enumerate services."""
    from jiuwenswarm.common.utils import get_user_workspace_dir

    if domain not in {"agent_server", "gateway"}:
        raise ControlError("Unsupported extension domain")
    key = "AGENT_SERVER_PORT" if domain == "agent_server" else "GATEWAY_PORT"
    port = os.environ.get(key) or (os.environ.get("AGENT_PORT") if domain == "agent_server" else None)
    # No port guessing: a manual terminal without startup context has no target.
    if not port or not port.isdecimal() or not 0 < int(port) < 65536:
        raise ControlError("当前终端没有 Swarm 实例信息，请在运行中的 Swarm agent 内执行。")
    return get_user_workspace_dir() / "run" / f"extension-cli-{domain}-{int(port)}.json"


def read_context(domain: str) -> dict[str, Any]:
    """Use the parent host's context, or its exact same-instance record."""
    value = os.environ.get(ENV_PREFIX + domain.upper())
    try:
        if value:
            context = json.loads(value)
        else:
            path = context_path(domain)
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ControlError("本机管理连接记录不合法。")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise ControlError("本机管理连接记录的访问权限过宽。")
            context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ControlError("当前实例的扩展管理入口不可用，请确认 Swarm 已启动并包含 CLI 支持。") from None
    if (
        not isinstance(context, dict) or context.get("domain") != domain
        or type(context.get("port")) is not int or not 0 < context["port"] < 65536
        or not isinstance(context.get("token"), str) or len(context["token"]) < 32
        or not isinstance(context.get("instance"), str) or not context["instance"]
    ):
        raise ControlError("本机管理连接记录不合法。")
    return context


async def request(context: Mapping[str, Any], action: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Send one request to the current host without retrying a mutation."""
    writer: asyncio.StreamWriter | None = None
    request_id = uuid.uuid4().hex
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection("127.0.0.1", context["port"], limit=MAX_FRAME)
            frame = {
                "id": request_id, "token": context["token"], "instance": context["instance"],
                "action": action, "params": params,
            }
            writer.write(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            raw = await reader.readline()
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise ControlError("本机管理响应与请求不匹配。")
            return response
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class LocalControl:
    """Expose four management operations to this local host's subprocesses.

    The random capability is scoped to one host lifetime. This uses the local
    account trust boundary, not browser login credentials or remote user IDs.
    """

    def __init__(self, domain: str, handler: Handler) -> None:
        self.domain = domain
        self._handler = handler
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._slots = asyncio.Semaphore(16)
        self._context: dict[str, Any] = {}
        self._record: Path | None = None
        self._published = ""

    async def start(self) -> None:
        """Bind only loopback and publish the actual endpoint after binding."""
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=MAX_FRAME)
        self._context = {
            "domain": self.domain, "port": self._server.sockets[0].getsockname()[1],
            "token": secrets.token_urlsafe(32), "instance": uuid.uuid4().hex,
        }
        self._published = json.dumps(self._context)
        key = ENV_PREFIX + self.domain.upper()
        os.environ[key] = self._published
        # Subprocesses use the environment directly. A sibling host (Gateway)
        # also publishes one protected record for the same instance.
        try:
            path = context_path(self.domain)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(self._published)
                os.replace(temporary, path)
                self._record = path
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ControlError):
            logger.debug("[ExtensionCLI] sibling-host record unavailable; inherited context remains usable")

    async def close(self) -> None:
        """Stop accepting operations, drain handlers and revoke published context."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        key = ENV_PREFIX + self.domain.upper()
        if os.environ.get(key) == self._published:
            os.environ.pop(key, None)
        if self._record is not None:
            try:
                if self._record.read_text(encoding="utf-8") == self._published:
                    self._record.unlink()
            except OSError:
                pass

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        request_id = ""
        try:
            async with self._slots:
                async with asyncio.timeout(10):
                    raw = await reader.readline()
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError("invalid frame")
                request_id = frame.get("id", "")
                token = frame.get("token")
                if (
                    not isinstance(token, str) or not token.isascii()
                    or not hmac.compare_digest(token, self._context["token"])
                    or frame.get("instance") != self._context["instance"]
                ):
                    response = {"ok": False, "payload": {"code": "AUTH_REQUIRED", "error": "本机管理授权无效。"}}
                elif not isinstance(frame.get("action"), str) or frame["action"] not in METHODS or not isinstance(
                    frame.get("params"), dict,
                ):
                    response = {"ok": False, "payload": {"code": "INVALID_PARAMS", "error": "无效的包管理请求。"}}
                else:
                    response = await self._handler(frame["action"], frame["params"])
                response = {**response, "id": request_id}
                writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()
        except (OSError, ValueError, TimeoutError):
            logger.debug("[ExtensionCLI] local connection closed or rejected")
        except Exception:  # noqa: BLE001 - keep the host running after a failed management request
            logger.exception("[ExtensionCLI] local request failed")
            try:
                response = {"id": request_id, "ok": False, "payload": {
                    "code": "INTERNAL_ERROR", "error": "包管理结果未确认。", "outcome": "unknown",
                }}
                writer.write(json.dumps(response).encode("utf-8") + b"\n")
                await writer.drain()
            except OSError:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            if task is not None:
                self._tasks.discard(task)


def source_domain(path: Path) -> str:
    """Read the declared host without importing package code."""
    import json
    from zipfile import BadZipFile, ZipFile

    try:
        if path.is_dir():
            manifest_path = path / "manifest.json"
            if manifest_path.stat().st_size > 65536:
                raise ValueError("manifest exceeds 64 KiB")
            raw = manifest_path.read_bytes()
        else:
            with ZipFile(path) as archive:
                entry = archive.getinfo("manifest.json")
                if entry.file_size > 65536:
                    raise ValueError("manifest exceeds 64 KiB")
                raw = archive.read(entry)
        manifest = json.loads(raw)
    except (OSError, KeyError, BadZipFile, ValueError, UnicodeError) as error:
        raise ValueError("Cannot read package manifest") from error
    package = manifest.get("package") if isinstance(manifest, dict) else None
    domain = package.get("domain") if isinstance(package, dict) else None
    return "gateway" if isinstance(domain, str) and domain.strip().lower() == "gateway" else "agentserver"
