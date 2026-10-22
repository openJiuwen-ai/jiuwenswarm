"""Adapt LOCAL code execution to one private, persistent CUA worker."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
from collections.abc import Awaitable, Callable

from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.core.sys_operation.cwd import get_cwd
from openjiuwen.core.sys_operation.local.code_operation import CodeOperation
from .cua_code_approval import request_code_permission


LOGGER = logging.getLogger(__name__)
BOOTSTRAP = "from jiuwenswarm.cua_engine.local_worker import main\nmain()\n"


class LocalCodeSession:
    """Use the existing LOCAL executor; never fall back from a sandbox."""

    def __init__(
        self,
        sys_operation: SysOperation | None,
        owner: str,
        *,
        session_timeout: int = 3600,
        authorize_cell: Callable[[str, str], Awaitable[None]] = request_code_permission,
    ):
        self.operation = sys_operation
        self.owner = owner
        self.session_id = None
        self.session_timeout = session_timeout
        self.authorize_cell = authorize_cell
        # Referencing the original implementation also lets frozen builds
        # discover its registration without scanning source directories.
        self.code_operation: CodeOperation | None = None

    def _require_local(self) -> SysOperation:
        if self.operation is None or self.operation.mode != OperationMode.LOCAL:
            raise RuntimeError(
                "cua_repl requires an explicitly bound LOCAL executor. "
                "No code was sent; a sandbox was not replaced by host execution."
            )
        return self.operation

    async def authorize(self, code: str, title: str) -> None:
        """Apply the normal code permission policy on every cell, before dispatch."""
        self._require_local()
        if self.session_id is None:
            raise RuntimeError("The CUA execution session is not bound. No code was sent.")
        try:
            await self.authorize_cell(code, title)
        except PermissionError:
            raise
        except Exception as error:
            raise PermissionError(
                "The host code permission check failed. No code was sent; existing variables are unchanged. "
                + str(error)
            ) from error
        LOGGER.info("CUA LOCAL cell authorized owner=%s session=%s title=%s", self.owner, self.session_id, title)

    async def start(self, environment: dict[str, str]) -> tuple[_LocalWorker, asyncio.StreamReader, asyncio.StreamWriter]:
        """Keep execute_code alive as the worker's owner, including cancellation."""
        operation = self._require_local()
        self.code_operation = operation.code()
        if self.code_operation is None:
            raise RuntimeError("The configured LOCAL code executor is unavailable. No code was sent.")
        loop = asyncio.get_running_loop()
        channels = {kind: loop.create_future() for kind in ("code", "lifetime")}
        token = secrets.token_hex(32)
        candidates = set()
        worker = None
        server = None
        job = None

        async def accept(reader, writer):
            task = asyncio.current_task()
            candidates.add(task)
            accepted = False
            try:
                claim = await asyncio.wait_for(reader.readline(), 5)
                parts = claim.rstrip(b"\n").split(b"\t")
                if len(claim) > 120 or len(parts) != 3:
                    return
                credential, kind_raw, pid_raw = parts
                kind = kind_raw.decode("ascii")
                if (kind not in channels or not pid_raw.isdigit()
                        or not hmac.compare_digest(credential, token.encode("ascii"))
                        or channels[kind].done()):
                    return
                accepted = True
                channels[kind].set_result((reader, writer, int(pid_raw)))
            except (OSError, ValueError, asyncio.TimeoutError):
                LOGGER.debug("Rejected an unauthenticated CUA worker connection", exc_info=True)
            finally:
                candidates.discard(task)
                if not accepted:
                    writer.close()
                    await writer.wait_closed()

        try:
            server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=12 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory(prefix="workswarm-local-code-") as temporary:
                configuration = Path(temporary) / "worker.json"
                configuration.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
                configuration.chmod(0o600)
                # LOCAL already inherits the host environment. Pass only the
                # overrides, so its normal execution log does not duplicate
                # unrelated host credentials or application configuration.
                env = {key: value for key, value in environment.items() if os.environ.get(key) != value}
                env["JIUWENSWARM_LOCAL_CODE_CONFIG"] = str(configuration)
                # The shared executor owns spawning, output collection, deadlines
                # and process-tree cancellation. force_file also uses the frozen
                # executable's existing script entry, without an external Python.
                running = asyncio.create_task(self.code_operation.execute_code(
                    BOOTSTRAP, language="python", timeout=self.session_timeout,
                    environment=env, cwd=get_cwd(), options={"force_file": bool(getattr(sys, "frozen", False))},
                ), name="cua-local-code-" + self.owner)
                worker = _LocalWorker(running)
                connecting = asyncio.gather(*channels.values())
                try:
                    done, _ = await asyncio.wait((running, connecting), timeout=60,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if connecting not in done:
                        detail = "LOCAL worker startup timed out."
                        if running in done:
                            result = running.result()
                            data = getattr(result, "data", None)
                            detail = str(getattr(data, "stderr", "") or result.message)[-4000:]
                        raise RuntimeError(detail + " No user code was sent.")
                    code_channel, lifetime_channel = connecting.result()
                finally:
                    if not connecting.done():
                        connecting.cancel()
                        await asyncio.gather(connecting, return_exceptions=True)
                reader, writer, pid = code_channel
                lifetime, lifetime_writer, lifetime_pid = lifetime_channel
                if pid != lifetime_pid:
                    raise RuntimeError("LOCAL worker channel identity changed. No user code was sent.")
                worker.lifetime = lifetime
                worker.lifetime_writer = lifetime_writer
                if os.name == "nt":
                    from computer_use.mcp.windows_worker import WindowsJob

                    job = WindowsJob()
                    job.assign(pid)
                    worker.job = job
                # The bootstrap waits for admission before creating its Runtime
                # or accepting code. Windows Job assignment therefore precedes
                # every user operation and every native descendant.
                writer.write(b"ready\n")
                await writer.drain()
                return worker, reader, writer
        except BaseException:
            if worker is not None:
                worker.kill()
                await asyncio.shield(worker.wait())
            elif job is not None:
                job.close()
            for future in channels.values():
                if future.done() and not future.cancelled():
                    _, writer, _ = future.result()
                    writer.close()
            raise
        finally:
            if server is not None:
                server.close()
            for task in tuple(candidates):
                task.cancel()
            if candidates:
                await asyncio.gather(*tuple(candidates), return_exceptions=True)


class _LocalWorker:
    """Process-like lifetime backed by the shared executor's owned task."""

    def __init__(self, task):
        self.task = task
        self.lifetime = self.lifetime_writer = self.job = None
        self.stopping = False
        self.waiter = None

    @property
    def returncode(self):
        if not self.task.done():
            return None
        if self.task.cancelled():
            return -1
        if self.task.exception() is not None:
            return -1
        result = self.task.result()
        return getattr(getattr(result, "data", None), "exit_code", -1)

    def kill(self):
        self.stopping = True
        if self.job is not None:
            self.job.terminate()
        self.task.cancel()

    async def _wait(self):
        try:
            try:
                # A structured execution error (including a deadline) is not
                # itself a cleanup failure. EOF and the Job/lifetime barrier
                # decide whether native retirement has actually completed.
                await self.task
            except asyncio.CancelledError:
                if not self.stopping:
                    raise
            if self.lifetime is not None:
                while await self.lifetime.read(65536):
                    pass
            if self.job is not None:
                await self.job.wait_empty()
        finally:
            if self.lifetime_writer is not None:
                self.lifetime_writer.close()
            if self.job is not None:
                self.job.close()

    async def wait(self):
        if self.waiter is None:
            self.waiter = asyncio.create_task(self._wait(), name="cua-local-code-retirement")
        await asyncio.shield(self.waiter)
        return self.returncode
