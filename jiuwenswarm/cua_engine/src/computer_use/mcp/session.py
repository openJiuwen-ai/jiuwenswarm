"""Supervise one persistent Python worker and its inherited native lifetimes."""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import socket
import sys

from computer_use._json import loads as jsonLoads

from .._json import dumps
from ..client.preview import is_preview_event
from .history import HISTORY_KEY, HistoryGate
from .journal import Journal
from .output import OutputBuffer, is_output

RESET_NOTICE = ('Execution session cleared. Recreate App variables and read again. '
                'Actions may already have executed; do not replay them '
                'automatically.')


def execution_error(message):
    return dict(content=[dict(type="text", text=message)], isError=True)


@dataclass(frozen=True)
class _ExecutionRequest:
    code: str
    signal: asyncio.Event | None
    history: dict[str, object] | None
    title: str | None
    on_progress: Callable[[], object] | None
    generation: int


class ExecutionSession:

    def __init__(self, timeout_ms=45000, on_preview=None, demo_foreground=False):
        self.demo_foreground = demo_foreground
        self.timeout_ms, self.on_preview = timeout_ms, on_preview
        self._child = self._pending = self._retiring = self._resetting = self._exited = None
        self._writer = self._reader = None
        self._closed = self._starting = False
        self._start_task = None
        self._generation = 0
        self._next_id = 1
        self._journal, self._history = Journal(), HistoryGate()
        self._tasks = set()

    def _track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def execute(self, code, signal=None, history=None, title=None, on_progress=None):
        # Bind queued work to this generation before a reset can yield control.
        request = _ExecutionRequest(code, signal, history, title, on_progress, self._generation)
        coroutine = self._execute(request)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return coroutine
        return loop.create_task(coroutine)

    async def _execute(self, request):
        if self._closed:
            return execution_error("Execution session is closed.")
        if request.generation != self._generation:
            return execution_error(
                "Execution was reset before code was sent. No code was executed; recreate variables before continuing.")
        if self._resetting is not None:
            result = await asyncio.shield(self._resetting)
            return result if result.get("isError") else await self._execute(request)
        if self._retiring is not None:
            return execution_error(
                "Native cleanup is still pending. Call computer_use_reset before continuing; no code was executed.")
        if self._pending is not None or self._starting:
            return execution_error("Another call is running; await it before starting the next.")
        if request.signal is not None and request.signal.is_set():
            return execution_error("Call cancelled before execution.")
        self._starting = True
        starting = self._track(self._start())
        self._start_task = starting

        def started(_):
            if self._start_task is starting:
                self._start_task = None

        starting.add_done_callback(started)
        try:
            await asyncio.shield(starting)
        except asyncio.CancelledError:
            await asyncio.shield(self.reset("Call cancelled."))
            raise
        except Exception as error:
            return execution_error(str(error))
        finally:
            # Keep admission reserved until this task can publish _pending.
            self._starting = False
        if request.generation != self._generation or self._closed:
            return execution_error(('Execution was reset before code was sent. No code was executed; '
                                    'recreate variables before '
                                    'continuing.'))
        if request.signal is not None and request.signal.is_set():
            return execution_error("Call cancelled before execution.")
        id_ = self._next_id
        self._next_id += 1
        policy = self._history.begin(request.history)
        self._journal.record(dict(kind="execution_start", executionId=id_, title=request.title,
                                  code=request.code, history=policy))
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = dict(id=id_, title=request.title, startedAt=loop.time(), output=OutputBuffer(),
                       future=future, timer=None, cancel=None)

        def stalled():
            self.reset(f"Execution exceeded {self.timeout_ms} ms without native command progress.")

        def progress():
            if pending["timer"] is not None:
                pending["timer"].cancel()
            pending["timer"] = loop.call_later(self.timeout_ms / 1000, stalled)
            try:
                if request.on_progress is not None:
                    result = request.on_progress()
                    if hasattr(result, "__await__"):
                        self._track(result)
            except Exception:
                logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

        def cleanup():
            if pending["timer"] is not None:
                pending["timer"].cancel()
            if pending["cancel"] is not None:
                pending["cancel"].cancel()

        pending.update(progress=progress, cleanup=cleanup)
        pending["timer"] = loop.call_later(self.timeout_ms / 1000, stalled)
        self._pending = pending
        if request.signal is not None:

            async def cancel():
                await request.signal.wait()
                self.reset("Call cancelled.")

            pending["cancel"] = self._track(cancel())
        try:
            self._writer.write((dumps(dict(code=request.code, executionId=id_, history=policy)) + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as error:
            self.reset(f"Cannot send code: {error}")
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(self.reset("Call cancelled."))
            raise

    async def _start(self):
        if self._child is not None:
            return
        env = dict(os.environ, COMPUTER_USE_REMOTE_PREVIEW="1" if self.on_preview is not None else "",
                   COMPUTER_USE_DEMO_FOREGROUND="1" if self.demo_foreground else "0", COMPUTER_USE_LIFETIME_FD="4")
        if os.name == "nt":
            from .windows_worker import start_windows_worker
            child, self._reader, self._writer, job, listener = await start_windows_worker(env)
            self._child = child

            async def exited():
                try:
                    await child.wait()
                    await job.wait_empty()
                finally:
                    job.close()
                    await listener.wait_closed()

            self._exited = self._track(exited())
            self._track(self._receive(child, self._reader))
            return
        parent, child_socket = socket.socketpair()
        life_read, life_write = os.pipe()
        try:
            child = await asyncio.create_subprocess_exec(sys.executable, "-m", "computer_use.driver_exec",
                                                         str(child_socket.fileno()), str(life_write), sys.executable,
                                                         "-m", "computer_use.mcp.runtime",
                                                         stdin=asyncio.subprocess.DEVNULL,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL,
                                                         pass_fds=(child_socket.fileno(), life_write), env=env)
        except BaseException:
            parent.close()
            os.close(life_read)
            raise
        finally:
            child_socket.close()
            os.close(life_write)
        self._child = child
        parent.setblocking(False)
        self._reader, self._writer = await asyncio.open_connection(sock=parent, limit=12 * 1024 * 1024)
        lifetime = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(lifetime), os.fdopen(life_read, "rb", buffering=0))

        async def exited():
            try:
                await child.wait()
                while await lifetime.read(65536):
                    pass
            finally:
                transport.close()

        self._exited = self._track(exited())
        self._track(self._receive(child, self._reader))

    async def _receive(self, child, reader):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if self._child is not child:
                    continue
                message = jsonLoads(line)
                if not isinstance(message, dict):
                    raise ValueError("Execution process returned invalid output.")
                if "preview" in message:
                    if is_preview_event(message["preview"]):
                        try:
                            if self.on_preview is not None:
                                self.on_preview(message["preview"])
                        except Exception:
                            logging.getLogger(__name__).debug(
                                "Best-effort cleanup or preview delivery failed", exc_info=True
                            )
                elif "diagnostic" in message:
                    event = message["diagnostic"]
                    if isinstance(event, dict):
                        self._journal.record({**event, "executionId": message.get("executionId")})
                        if self._pending is not None and message.get(
                                "executionId") == self._pending["id"] and event.get("kind") in ("command_start",
                                                                                                "command_end"):
                            self._pending["progress"]()
                elif "output" in message:
                    pending = self._pending
                    if pending is None or message.get("executionId") != pending["id"]:
                        continue
                    if not is_output(message["output"]):
                        raise ValueError("Execution process returned invalid output.")
                    block = message["output"]
                    self._journal.record(
                        dict(
                            kind="output", executionId=pending["id"], content=dict(
                                type="image", mimeType=block["mimeType"]) if block["type"] == "image" else block))
                    pending["output"].append(block, message.get("mergeText") is True)
                else:
                    if not isinstance(message.get("content"), list) or any(not is_output(block)
                                                                           for block in message["content"]):
                        raise ValueError("Execution process returned invalid output.")
                    self._finish(message)
            if self._child is child:
                self.reset(f"Execution process exited ({child.returncode}).")
        except Exception as error:
            if self._child is child:
                self.reset(f"Execution process failed: {error}")

    def _finish(self, result):
        pending, self._pending = self._pending, None
        if pending is None:
            return
        duration = (asyncio.get_running_loop().time() - pending["startedAt"]) * 1000
        self._journal.record(
            dict(
                kind="execution_end", executionId=pending["id"], durationMs=duration,
                isError=result.get("isError", False), content=[
                    dict(type="image", mimeType=block["mimeType"], bytes=len(block["data"]) * 3 //
                         4) if block["type"] == "image" else block for block in result["content"]
                ]))
        result["_meta"] = {
            **result.get("_meta", {}), "computer-use": {
                **self._journal.metadata(), "executionId": pending["id"], "durationMs": duration,
                **({"title": pending["title"]} if pending["title"] else {})
            }, HISTORY_KEY: self._history.complete()
        }
        pending["cleanup"]()
        if not pending["future"].done():
            pending["future"].set_result(result)

    def reset(self, reason="Execution reset requested."):
        if self._resetting is not None:
            return self._resetting
        self._generation += 1
        starting = self._start_task
        child, self._child = self._child, None
        if child is not None and child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)
        earlier = self._retiring if self._retiring is not None else self._exited
        self._exited = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._pending is not None:
            self._pending["cleanup"]()

        async def retire():
            if starting is not None:
                try:
                    await asyncio.shield(starting)
                except Exception:
                    logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)
            # Process creation yields in Python. A reset during that yield must
            # retire the newly created worker before admitting any later call.
            late, self._child = self._child, None
            if late is not None and late.returncode is None:
                try:
                    late.kill()
                except ProcessLookupError:
                    logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)
            if self._writer is not None:
                self._writer.close()
                self._writer = None
            exited, self._exited = self._exited, None
            if earlier is not None:
                await asyncio.shield(earlier)
            if exited is not None and exited is not earlier:
                await asyncio.shield(exited)

        retired = self._track(retire())
        self._retiring = retired

        async def reset():
            failure = None
            try:
                if retired is not None:
                    await asyncio.wait_for(asyncio.shield(retired), 5)
            except TimeoutError:
                failure = ('Native cleanup is still pending; capture stop is unconfirmed. Actions '
                           'may already have executed; do not replay them. Retry computer_use_reset '
                           'before continuing.')
            except Exception as error:
                failure = str(error)
            if retired is None or retired.done():
                if self._retiring is retired:
                    self._retiring = None
            elif retired is not None:

                def clear(_):
                    if self._retiring is retired:
                        self._retiring = None

                retired.add_done_callback(clear)
            result = execution_error(reason + "\n" + (failure if failure is not None else RESET_NOTICE))
            result["content"] = (self._pending["output"].content.copy()
                                 if self._pending is not None else []) + result["content"]
            self._journal.record(dict(kind="execution_reset", reason=reason, complete=failure is None))
            self._finish(result)
            self._history = HistoryGate()
            if failure is not None:
                return execution_error(failure)
            try:
                if self.on_preview is not None:
                    self.on_preview(dict(kind="reset"))
            except Exception:
                logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)
            return dict(content=[
                dict(
                    type="text", text=('Execution session reset. Python variables, App/Element handles and '
                                       'observations cleared; session capture streams stopped and preview reset '
                                       'requested. Apps, documents, files and completed actions are unchanged. '
                                       'The next computer_use call starts a fresh '
                                       'environment.'))
            ])

        task = asyncio.create_task(reset())
        self._resetting = task

        def clear(_):
            if self._resetting is task:
                self._resetting = None

        task.add_done_callback(clear)
        return task

    def close(self):
        self._closed = True
        return self.reset("MCP connection closed.")
