"""Persistent Python worker. User code never shares the MCP server's stdout."""
import ast
import asyncio
import base64
import builtins
import inspect
import io
import os
import pprint
import signal
import socket
import sys
from contextvars import ContextVar

from computer_use._json import loads as jsonLoads

from .. import Computer
from .._identity import Record, WeakIdentityMap
from .._json import dumps
from ..client.host import (
    set_action_handler,
    set_diagnostic_handler,
    set_execution_guard,
    set_observation_handler,
    set_preview_handler,
    set_text_page_handler,
)
from ..client.images import image_delivered, image_label, image_metadata, require_image_delivery, reset_image_delivery
from ..client.preview import latest_preview_sink
from ..client.result_presentation import result_receipt, text_source_label
from ..observation.action_presentation import ActionPresentation
from .output import OutputBuffer
from .image_delivery import prepare_image
from .presentation import ObservationOutput, ObservationPresenter

_runs = ContextVar("computer_use_execution", default=None)


class Runtime:

    def __init__(self, send, computer_factory=Computer):
        self.send, self.computer_factory = send, computer_factory
        self.presenter = ObservationPresenter()
        self.command_runs = {}
        self.computers = []
        require_image_delivery()
        set_execution_guard(self.guard)
        set_diagnostic_handler(self.diagnostic)
        set_action_handler(self.action)
        set_text_page_handler(self.text_page)
        set_observation_handler(self.observation)
        if os.environ.get("COMPUTER_USE_REMOTE_PREVIEW") == "1":

            async def preview(event):
                self.send(dict(preview=event))

            set_preview_handler(latest_preview_sink(preview))
        self.task_namespace = self.namespace()

    @staticmethod
    def guard():
        run = _runs.get()
        if run is None or not run["active"] or run["phase"] != "code":
            raise RuntimeError(('Computer operation belongs to an ended tool execution. Effect: no '
                                'command sent. Next: await every operation within a current tool call; '
                                'do not use background timers for '
                                'input.'))
        pending = asyncio.get_running_loop().create_future()
        run["pending"].add(pending)

        def done():
            run["pending"].discard(pending)
            if not pending.done():
                pending.set_result(None)

        return done

    def diagnostic(self, event):
        run = _runs.get()
        execution_id = run["id"] if run is not None else None
        trace = event.get("traceId")
        if isinstance(trace, str):
            if event["kind"] == "command_start":
                self.command_runs[trace] = execution_id
                if len(self.command_runs) > 1024:
                    self.command_runs.pop(next(iter(self.command_runs)))
            else:
                execution_id = self.command_runs.get(trace)
        elif event["kind"] == "backend_diagnostic":
            execution_id = None
        self.send(dict(diagnostic=event, **({"executionId": execution_id} if execution_id is not None else {})))

    def emit(self, block, merge_text=False):
        run = _runs.get()
        if run is None or not run["active"]:
            return
        run["output"].append(block, merge_text)
        self.send(dict(output=block, mergeText=merge_text, executionId=run["id"]))

    def flush_observations(self, run):

        def emit(text, observation):
            self.emit(dict(type="text", text=text))
            if observation["state"].get("screenshot") is not None:
                run["imageContexts"].set(observation["state"]["screenshot"], True)

        def emit_image(png):
            if not any(delivery.source is png for delivery in run["images"]):
                self.print_image(png)

        run["observations"].flush(emit, emit_image)

    def action(self, receipt):
        run = _runs.get()
        if run is None or not run["active"]:
            return
        self.flush_observations(run)
        self.emit(dict(type="text", text=run["actions"].render(receipt)))

    def text_page(self, source, page):
        run = _runs.get()
        if run is None or not run["active"]:
            return
        self.flush_observations(run)
        self.emit(dict(type="text", text=text_source_label(source)), True)
        run["textPages"].set(page, True)

    def observation(self, event):
        run = _runs.get()
        if event["kind"] in ("invalidate", "discard-pending"):
            if event["kind"] == "invalidate":
                self.presenter.invalidate(event["source"])
            if run is not None:
                if event["kind"] == "invalidate":
                    run["observations"].invalidate(event["source"])
                else:
                    run["observations"].discard_pending(event["source"])
            return
        if run is None or not run["active"]:
            return
        observation = event["observation"]
        run["observations"].observe(observation)
        data = dict(kind="observation", scope=observation["scope"], view=observation["view"],
                    text=observation["state"]["text"])
        if "observed" in observation:
            data["observed"] = observation["observed"]
        self.send(dict(diagnostic=dict(**data, elementRefs=list(observation["refs"])), executionId=run["id"]))

    def print(self, *values, sep=" ", end="\n", file=None, flush=False):
        run = _runs.get()
        if run is None or not run["active"]:
            return
        if file is not None and file not in (sys.stdout, sys.stderr):
            _ORIGINAL_PRINT(*values, sep=sep, end=end, file=file, flush=flush)
            return
        if len(values) == 1:
            receipt = result_receipt(values[0])
            if receipt is not None and run["actions"].has(receipt):
                return
            if receipt is not None:
                self.emit(
                    dict(type="text",
                         text=f"Earlier action [{receipt['status']}]: {receipt['target']}. {receipt['detail']}"))
                return
            if run["textPages"].has(values[0]):
                values = (dict(values[0]), )
        text = (" " if sep is None else sep).join(
            value if isinstance(value, str) else pprint.pformat(value, depth=6, sort_dicts=False) for value in values)
        if end not in (None, "\n"):
            text += end
        self.emit(dict(type="text", text=text), True)
        observed = run["observations"].printed(text)
        if observed is not None and observed["state"].get("screenshot") is not None:
            run["imageContexts"].set(observed["state"]["screenshot"], True)

    def print_image(self, png):
        if not isinstance(png, (bytes, bytearray, memoryview)):
            raise TypeError("printImage() expects PNG bytes, such as state.screenshot.")
        delivery = prepare_image(png)
        data = base64.b64encode(delivery.png).decode("ascii")
        run = _runs.get()
        self.emit(dict(type="text", text=image_label(
            png, run is not None and run["imageContexts"].has(png), delivery.coordinates)))
        self.emit(dict(type="image", mimeType="image/png", data=data))
        if run is not None and run["active"]:
            run["images"].append(delivery)
            self.send(dict(diagnostic=dict(kind="image_output", capture=image_metadata(png)), executionId=run["id"]))

    async def finish(self, error=None):
        run = _runs.get()
        if run is None or not run["active"] or run["phase"] != "code":
            return
        run["phase"] = "draining"
        if run["pending"]:
            if error is None:
                error = RuntimeError(('Computer operations were not awaited. Pending operations finished '
                                      'before this result; inspect their receipts and observations before '
                                      'continuing.'))
            await asyncio.gather(*list(run["pending"]), return_exceptions=True)
        try:
            self.flush_observations(run)
        except Exception as failure:
            if error is None:
                error = failure
        for delivery in run["images"]:
            image_delivered(delivery.source, delivery.coordinates)
        run["active"] = False
        run["phase"] = "finished"
        result = dict(content=run["output"].content.copy())
        message = None
        if error is not None:
            message = (type(error).__name__ + ": " + str(error))[:4096]
            result["content"].append(
                dict(type="text",
                     text=message + "\nEarlier statements may have executed. Observe before retrying an action."))
            result["isError"] = True
        elif not result["content"]:
            result["content"].append(
                dict(
                    type="text", text=('Completed (no output). Reads return observations automatically; use '
                                       'print() for other '
                                       'values.')))
        self.send(result)
        return result

    def namespace(self):
        computer = self.computer_factory()
        self.computers.append(computer)
        return dict(__name__="__computer_use__", __builtins__=builtins, computer=computer, print=self.print,
                    printImage=self.print_image)

    async def execute(self, message):
        code, id_, history = message["code"], message["executionId"], message["history"]
        if history["resetText"]:
            self.presenter.invalidate()
        if history["resetImages"]:
            reset_image_delivery()
        run = Record(id=id_, active=True, phase="code", output=OutputBuffer(),
                     observations=ObservationOutput(self.presenter, history["incremental"]), images=[], pending=set(),
                     actions=ActionPresentation(), textPages=WeakIdentityMap(), imageContexts=WeakIdentityMap())
        token = _runs.set(run)
        try:
            try:
                namespace = self.task_namespace
                compiled = compile(code + "\n", "computer-use-agent.py", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                                   dont_inherit=True)
                evaluated = eval(compiled, namespace, namespace)
                if inspect.isawaitable(evaluated):
                    await evaluated
            except BaseException as error:
                return await self.finish(error)
            return await self.finish()
        finally:
            _runs.reset(token)

    async def close(self):
        for computer in self.computers:
            # SDK host/registry code shares state owned by these exact handle objects.
            # pylint: disable-next=protected-access
            transport = computer._controller.transport
            if hasattr(transport, "close"):
                transport.close()
        for computer in self.computers:
            # SDK host/registry code shares state owned by these exact handle objects.
            # pylint: disable-next=protected-access
            transport = computer._controller.transport
            if hasattr(transport, 'wait_closed'):
                await transport.wait_closed()


_ORIGINAL_PRINT = builtins.print


class ConsoleWriter(io.TextIOBase):
    """Discard raw process output; only the explicit print hook emits content."""

    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        return len(text)

    def flush(self):
        pass


async def main():
    port = os.environ.pop("COMPUTER_USE_WORKER_PORT", None)
    if port is not None:
        token = os.environ.pop("COMPUTER_USE_WORKER_TOKEN", "")
        if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise RuntimeError("Invalid internal worker authentication.")
        reader, writer = await asyncio.open_connection("127.0.0.1", int(port), limit=10 * 1024 * 1024)
        writer.write((token + "\t" + str(os.getpid()) + "\n").encode("ascii"))
        await writer.drain()
    else:
        ipc = socket.socket(fileno=3)
        try:
            ipc.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=ipc, limit=10 * 1024 * 1024)
        except BaseException:
            ipc.close()
            raise

    def send(message):
        writer.write((dumps(message) + "\n").encode("utf-8"))

    runtime = Runtime(send)
    sys.stdout = sys.stderr = ConsoleWriter(runtime)
    builtins.print = runtime.print

    async def stop():
        await runtime.close()
        writer.close()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(stop()))
    except RuntimeError:
        pass
    current = None
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            if current is not None and not current.done():
                raise RuntimeError("Worker received concurrent executions")
            current = asyncio.create_task(runtime.execute(jsonLoads(line)))
        if current is not None and not current.done():
            current.cancel()
    finally:
        await runtime.close()
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
