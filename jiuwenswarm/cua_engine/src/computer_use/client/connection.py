"""One native request at a time; cancellation and uncertain input never replay."""
import asyncio
import atexit
import codecs
import json
import logging
import math
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..errors import ComputerUseError
from ..paths import native_driver
from ..protocol.captures import capture_metadata
from ..protocol.execution import (
    decode_begin,
    decode_capture_request,
    decode_execution_result,
    decode_inspect_query,
    decode_unit_command,
)
from ..protocol.primitives import finite, record, safe_integer, trim
from ..protocol.session import PROTOCOL_VERSION, decode_failure, decode_hello, desktop_mode, failure_error
from .diagnostics import SDK_VERSION, diagnostic, local_failure
from .preview import get_preview_handler, read_previews

request_cleanup_grace_ms, driver_termination_grace_ms = 750, 1000


def _json(value):

    def fallback(value):
        if isinstance(value, Mapping):
            return dict(value)
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False, default=fallback) + "\n"


def _now():
    return asyncio.get_running_loop().time() * 1000


class Connection:

    def __init__(self, invalidate, driver=None, *, protocol_version=PROTOCOL_VERSION):
        if type(protocol_version) is not int or protocol_version != PROTOCOL_VERSION:
            raise ValueError("The native connection requires protocol 5")
        self.protocol_version = protocol_version
        self.invalidate, self.driver = invalidate, driver
        self._child = self._pending = self._hello = self._handshake = self._retiring = None
        self._buffer = b""
        self._requesting = self._waiting_for_exit = False
        self._next_id, self._generation = 1, 0
        self._diagnostic = self._stderr_buffer = ""
        self._traces, self._readers = {}, set()
        self._preview_transport = None
        self._child_uses_eof = False

    @property
    def session(self):
        return self._hello["session"] if self._hello is not None else None

    @property
    def platform(self):
        return self._hello["platform"] if self._hello is not None else None

    @property
    def capabilities(self):
        return frozenset(self._hello['capabilities']) if self._hello is not None else frozenset()

    @property
    def session_identity(self):
        """An internal lease cannot migrate across process retirement/reconnect."""
        return (self._generation, self.session) if self._hello is not None else None

    def _track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)
        return task

    async def _connect(self):
        if self._child is not None:
            return
        if self.driver is None and sys.platform not in ("darwin", "win32"):
            raise local_failure(f"platform {sys.platform} is not implemented",
                                "run the MCP server on a supported system with its matching backend",
                                "mode_unsupported", "connection")
        driver = self.driver if self.driver is not None else native_driver()
        command = driver["command"]
        if self.driver is None and not Path(command).exists():
            raise local_failure(f"{sys.platform} driver missing",
                                "build the native driver on the MCP host, then read the App again", "execution_failed",
                                "connection")
        self._buffer, self._diagnostic, self._stderr_buffer, self._next_id = b"", "", "", 1
        self._traces.clear()
        windows = sys.platform == "win32"
        # Windows uses native stdio handles. The POSIX 3/4 descriptor bridge
        # and preview pipe are not part of its driver's transport contract.
        preview = get_preview_handler() if not windows else None
        read_fd = write_fd = -1
        lifetime = 4 if os.environ.get("COMPUTER_USE_LIFETIME_FD") == "4" else -1
        if preview is not None:
            read_fd, write_fd = os.pipe()
        try:
            env = dict(os.environ)
            if windows:
                env.pop("COMPUTER_USE_PREVIEW_FD", None)
                env.pop("COMPUTER_USE_LIFETIME_FD", None)
                program = [command, *driver.get("args", [])]
                options = dict(creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                env["COMPUTER_USE_PREVIEW_FD"] = "3" if preview is not None else ""
                program = [
                    sys.executable, "-m", "computer_use.driver_exec",
                    str(write_fd),
                    str(lifetime), command, *driver.get("args", [])
                ]
                options = dict(pass_fds=tuple(fd for fd in (write_fd, lifetime) if fd >= 0))
            child = await asyncio.create_subprocess_exec(*program, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                         env=env, **options)
        except BaseException:
            if read_fd >= 0:
                os.close(read_fd)
            raise
        finally:
            if write_fd >= 0:
                os.close(write_fd)
        self._child = child
        self._child_uses_eof = windows
        if preview is not None:
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0))
            self._preview_transport = transport
            self._track(read_previews(reader, preview))
        diagnostic(
            dict(kind="connection", sdkVersion=SDK_VERSION, platform=sys.platform,
                 desktopMode=os.environ.get("COMPUTER_USE_DESKTOP", "shared"), executable=command, pid=child.pid))
        atexit.register(self._on_exit)
        self._track(self._read_stdout(child))
        self._track(self._read_stderr(child))
        self._track(self._watch(child))

    def _on_exit(self):
        child = self._child
        if child is not None and child.returncode is None:
            # TerminateProcess cannot run the driver's finally blocks. EOF
            # lets its reader cancel native work and release paired input.
            try:
                if self._child_uses_eof:
                    child.stdin.close()
                else:
                    child.terminate()
            except ProcessLookupError:
                logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

    async def _read_stdout(self, child):
        try:
            while True:
                data = await child.stdout.read(65536)
                if not data:
                    break
                if self._child is child:
                    self._receive(data)
            if self._child is child:
                self.close(
                    RuntimeError("driver disconnected" + (" with an incomplete frame" if self._buffer else "") +
                                 (": " + trim(self._diagnostic) if self._diagnostic else "")))
        except (OSError, ValueError) as error:
            if self._child is child:
                self.close(error)

    async def _read_stderr(self, child):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                data = await child.stderr.read(65536)
                if not data:
                    break
                if self._child is not child:
                    continue
                text = decoder.decode(data)
                self._diagnostic = (self._diagnostic + text)[-4096:]
                self._stderr_buffer += text
                while "\n" in self._stderr_buffer:
                    line, self._stderr_buffer = self._stderr_buffer.split("\n", 1)
                    try:
                        value = json.loads(line)
                    except ValueError:
                        value = line
                    envelope = value if isinstance(value, dict) else {}
                    request_id = envelope.get("requestId")
                    event = dict(kind="backend_diagnostic", value=envelope.get("message", value))
                    if finite(request_id):
                        event["requestId"] = request_id
                        if request_id in self._traces:
                            event["traceId"] = self._traces[request_id]
                    diagnostic(event)
                if len(self._stderr_buffer) > 65536:
                    diagnostic(dict(kind="backend_diagnostic", value=self._stderr_buffer, partial=True))
                    self._stderr_buffer = ""
        except (OSError, ValueError):
            logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

    async def _watch(self, child):
        code = await child.wait()
        if self._child is child:
            self.close(
                RuntimeError(f"driver exited ({code})" + (": " + trim(self._diagnostic) if self._diagnostic else "")))

    async def request(self, method, params, options=None):
        if self._requesting:
            raise local_failure("another system call is in progress", "await it before starting the next")
        self._requesting = True
        try:
            return await self._request(method, params, options.get("timeoutMs") if options is not None else None)
        finally:
            self._requesting = False

    async def connect(self, timeout_ms=30000):
        """Negotiate a session without sending a UI command or inventing a binding."""
        return await self.request(None, {}, dict(timeoutMs=timeout_ms))

    def _write(self, value):
        if self._child is None or self._child.stdin.is_closing():
            raise RuntimeError("driver input channel closed")
        self._child.stdin.write(_json(value).encode("utf-8"))

    async def _negotiate(self, timeout_ms):
        if self._hello is not None:
            return
        mode, loop = desktop_mode(), asyncio.get_running_loop()
        future = loop.create_future()
        timer = loop.call_later(
            timeout_ms / 1000, lambda: self.close(RuntimeError("protocol hello timed out; no system command was sent")))
        self._handshake = dict(future=future, timer=timer)
        self._write(dict(version=self.protocol_version, id=0, method="hello", params=dict(desktopMode=mode)))
        await asyncio.shield(future)

    async def _request(self, method, params, budget):
        if self._pending is not None or self._waiting_for_exit:
            raise local_failure("another system call is in progress", "await it before starting the next")
        if method is not None and method not in ("begin", "inspect", "capture", "execute", "finish"):
            raise local_failure("unsupported native protocol method: " + str(method), "use the current Computer API",
                                "invalid_request", str(method))
        if method == "begin":
            try:
                params = decode_begin(params)
            except (TypeError, ValueError) as error:
                raise local_failure(str(error), "correct the native operation intent", "invalid_request",
                                    method) from error
        if method == "inspect":
            try:
                params = {**params, "query": decode_inspect_query(params.get("query"))}
            except (TypeError, ValueError) as error:
                raise local_failure(str(error), "correct the native inspection request", "invalid_request",
                                    method) from error
        if method == 'capture':
            try:
                params = decode_capture_request(params)
            except (TypeError, ValueError) as error:
                raise local_failure(str(error), 'correct the native capture scope or budget', 'invalid_request',
                                    method) from error
        if method == 'execute':
            try:
                params = {**params, 'command': decode_unit_command(params.get('command'))}
            except (TypeError, ValueError) as error:
                raise local_failure(str(error), 'correct the native unit command', 'invalid_request', method) from error
        deadline = _now() + max(
            1,
            min(budget if budget is not None else 30000,
                self.driver.get("timeoutMs", 30000) if self.driver is not None else 30000))

        def remaining():
            value = math.ceil(deadline - _now())
            if value < 1:
                raise local_failure("request budget exhausted before dispatch; no system command was sent",
                                    "read the target again when ready", "execution_failed", "connection")
            return value

        if self._retiring is not None:
            self._waiting_for_exit = True
            generation = self._generation
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(self._retiring), remaining() / 1000)
                except TimeoutError:
                    raise local_failure(
                        "previous driver is still stopping",
                        "wait for it to exit before reading the app again; no replacement driver was started",
                        "execution_failed", "connection") from None
                if generation != self._generation:
                    raise local_failure("connection closed before the request started", "read the app again when ready",
                                        "execution_failed", "connection")
            finally:
                self._waiting_for_exit = False
        try:
            desktop_mode()
            remaining()
            await self._connect()
            await self._negotiate(remaining())
        except asyncio.CancelledError:
            self.close(RuntimeError("connection request cancelled before dispatch"))
            raise
        except Exception as error:
            failure = error if isinstance(error, ComputerUseError) else ComputerUseError(
                dict(code="execution_failed", effect="none", target=method or "connection", detail=str(error),
                     next="check the backend configuration and connect again; no system command was sent"), cause=error)
            self.close(failure)
            if failure is error:
                raise
            raise failure from error
        if method is None:
            remaining()
            return dict(self._hello)
        if not safe_integer(self._next_id):
            error = local_failure("request id space exhausted", "read the app in a new session", "execution_failed",
                                  method)
            self.close(error)
            raise error
        capability = ("nativeApplicationRoots" if method == "inspect" and params.get('query', {}).get('kind') == 'roots'
                      else "nativeRelationPages" if method == "inspect" and 'relationPage' in params.get('query', {})
                      else "nativeNodePages" if method == "inspect" and 'childrenPage' in params.get('query', {}) else
                      "nativeCapturedText" if method == "inspect" and params.get("query", {}).get("kind") ==
                      "capturedText" else "nativeRelations" if method == "inspect" and params.get("query", {}).
                      get("kind") == "relation" else "nativeCapture" if method == "capture" else "nativeOperations")
        if capability is not None and capability not in self._hello["capabilities"]:
            raise local_failure(
                f"backend does not provide {capability} in this desktop session",
                "use supported native elements, or connect a desktop with the required capture/input capabilities",
                "mode_unsupported", method)
        if (method == 'inspect' and 'followRelations' in params.get('query', {})
                and 'nativeFollowRelations' not in self._hello['capabilities']):
            raise local_failure('backend does not provide nativeFollowRelations',
                                'rebuild the driver before requesting additional relationship collection',
                                'mode_unsupported', method)
        if method == 'capture' and 'nativeCaptureKinds' not in self._hello['capabilities']:
            raise local_failure('backend does not provide nativeCaptureKinds',
                                'rebuild the driver before using explicit native capture scopes', 'mode_unsupported',
                                method)
        missing_bound_contracts = ((method == 'begin' and params.get('mode') == 'write' or method == 'execute')
                                   and 'nativeBoundContracts' not in self._hello['capabilities'])
        if missing_bound_contracts:
            raise local_failure('backend does not provide nativeBoundContracts',
                                'rebuild the native driver before using the contract-bound execution protocol',
                                'mode_unsupported', method)
        missing_unit_readiness = ((method == 'begin' and params.get('mode') == 'write' or method == 'inspect'
                                   and params.get('query', {}).get('kind') in ('inputContext', 'launch'))
                                  and 'nativeUnitReadinessV2' not in self._hello['capabilities'])
        if missing_unit_readiness:
            raise local_failure('backend does not provide nativeUnitReadinessV2',
                                'rebuild the driver before using per-unit execution readiness', 'mode_unsupported',
                                method)
        missing_launch_support = (
            (method == 'begin' and params.get('intent', {}).get('kind') == 'launch'
             or method == 'inspect' and params.get('query', {}).get('kind') == 'launch'
             or method == 'execute' and params.get('command', {}).get('kind') == 'launch_application')
            and 'nativeLaunch' not in self._hello['capabilities'])
        if missing_launch_support:
            raise local_failure('backend does not provide nativeLaunch',
                                'rebuild the native driver before launching through the execution pipeline',
                                'mode_unsupported', method)
        missing_action_intents = (method == "begin" and params.get("intent", {}).get("kind") in ("click", "openMenu")
                                  and params.get('target', {}).get('capture') is None
                                  and "nativeActionIntents" not in self._hello["capabilities"])
        if missing_action_intents:
            raise local_failure("backend does not provide nativeActionIntents",
                                "update the native driver before using this execution route", "mode_unsupported",
                                method)
        if (method == "begin" and params.get("intent", {}).get("kind") in ("focus", "key")
                and "nativeKeyboardIntents" not in self._hello["capabilities"]):
            raise local_failure("backend does not provide nativeKeyboardIntents",
                                "update the native driver before preparing or sending keys", "mode_unsupported", method)
        if (method == "begin" and params.get("intent", {}).get("kind") in ("insert", "paste")
                and "nativeTextInput" not in self._hello["capabilities"]):
            raise local_failure("backend does not provide nativeTextInput",
                                "update the native driver before inserting or pasting text", "mode_unsupported", method)
        missing_window_scope = (method == "begin" and params.get("mode") == "write"
                                and params.get("target", {}).get("node") is None
                                and params.get("target", {}).get("surface") is not None
                                and "nativeWindowScope" not in self._hello["capabilities"])
        if missing_window_scope:
            raise local_failure("backend does not provide nativeWindowScope",
                                "update the driver before resolving a target inside a native operation",
                                "mode_unsupported", method)
        if (method == 'inspect' and params.get('query', {}).get('within')
                and 'nativeWindowScope' not in self._hello['capabilities']):
            raise local_failure('backend does not provide nativeWindowScope',
                                'update the driver before binding logical target constraints', 'mode_unsupported',
                                method)
        missing_actuator_support = (method == 'inspect' and 'actuator' in params.get('query', {})
                                    and params['query']['actuator'].get('relation') == 'containedEditor'
                                    and 'nativeActuators' not in self._hello['capabilities'])
        if missing_actuator_support:
            raise local_failure('backend does not provide nativeActuators',
                                'update the driver before selecting an internal editor', 'mode_unsupported', method)
        if (method == 'inspect' and params.get('query', {}).get('actuator', {}).get('relation') == 'scrollPage'
                and 'nativeScroll' not in self._hello['capabilities']):
            raise local_failure('backend does not provide nativeScroll',
                                'update the driver before selecting native page scrolling', 'mode_unsupported', method)
        if (method == 'execute' and params.get('command', {}).get('kind') == 'native_scroll'
                and not {'nativeScroll', 'nativeScrollPattern'} & set(self._hello['capabilities'])):
            raise local_failure('backend does not provide native scrolling',
                                'update the driver before executing native scrolling', 'mode_unsupported', method)
        if (method == 'inspect' and params.get('query', {}).get('actuator', {}).get('relation') == 'scrollPointer'
                and 'nativeElementScroll' not in self._hello['capabilities']):
            raise local_failure('backend does not provide nativeElementScroll',
                                'update the driver before selecting an element wheel route', 'mode_unsupported', method)
        if (method == 'inspect' and params.get('query', {}).get('actuator', {}).get('contentPath')
                and 'nativeScrollContentPath' not in self._hello['capabilities']):
            raise local_failure('backend does not provide nativeScrollContentPath',
                                'update the driver before selecting moving scroll content', 'mode_unsupported', method)
        if method == 'begin' and params.get('mode') == 'write' and params.get('target', {}).get('capture') is not None:
            capability = 'nativeDesktopInput' if params['target'].get('surface') is None else 'nativePointer'
            if capability not in self._hello['capabilities']:
                raise local_failure('backend does not provide ' + capability,
                                    'update the driver before using this observed input scope', 'mode_unsupported',
                                    method)
        id_, trace_id = self._next_id, str(uuid.uuid4())
        self._next_id += 1
        self._traces[id_] = trace_id
        if len(self._traces) > 1024:
            self._traces.pop(next(iter(self._traces)))
        target, timeout_ms = self._command_target(method, params), min(remaining(), self._hello["maxTimeoutMs"])
        diagnostic(
            dict(kind="command_start", traceId=trace_id, id=id_, method=method, params=params, target=target,
                 timeoutMs=timeout_ms))
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pure = (method == 'capture' or method == 'inspect' and params.get('query', {}).get('kind') == 'capturedText')
        pending = dict(id=id_, method=method, traceId=trace_id, target=target, future=future, cancelSent=False,
                       startedAt=_now(), pure=pure)

        def timeout():
            if self._pending is not pending:
                return
            diagnostic(
                dict(kind="request_cancelling", traceId=trace_id, id=id_, method=method, timeoutMs=timeout_ms,
                     cleanupGraceMs=request_cleanup_grace_ms))
            grace = max(0, pending["startedAt"] + timeout_ms + request_cleanup_grace_ms - _now())

            def terminal_timeout():
                if self._pending is pending:
                    self.close(
                        RuntimeError((f"request timed out after {timeout_ms} ms; no terminal response within "
                                      f"{request_cleanup_grace_ms} ms cleanup grace")))

            pending["timer"] = loop.call_later(grace / 1000, terminal_timeout)
            self._cancel_pending(pending)

        pending["timer"] = loop.call_later(timeout_ms / 1000, timeout)
        self._pending = pending
        try:
            self._write(
                dict(version=self.protocol_version, session=self._hello["session"], id=id_, method=method,
                     params=params, timeoutMs=timeout_ms))
        except Exception as error:
            self.close(error)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.close(RuntimeError("host cancelled the request"))
            if future.done() and not future.cancelled():
                future.exception()
            raise

    def _receive(self, data):
        try:
            self._buffer += data
            if len(self._buffer) > 64 * 1024 * 1024:
                raise ValueError("protocol: response frame too large")
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if line.endswith(b"\r"):
                    line = line[:-1]
                if line.startswith(b"\xef\xbb\xbf"):
                    raise ValueError("protocol: BOM is not allowed")

                def invalid_constant(value):
                    raise ValueError("protocol: invalid JSON constant " + value)

                value = json.loads(line.decode("utf-8", errors="strict"), parse_constant=invalid_constant)
                if not isinstance(value, dict):
                    raise ValueError("protocol: expected response object")
                if self._handshake is not None:
                    if self._buffer:
                        raise ValueError("protocol: unsolicited data after hello")
                    self._hello = decode_hello(value, desktop_mode())
                    if os.environ.get("COMPUTER_USE_DEMO_FOREGROUND"
                                      ) == "1" and "demoForeground" not in self._hello["capabilities"]:
                        raise local_failure("demoForeground is not supported by this native backend",
                                            "rebuild the backend on the MCP host or disable demoForeground",
                                            "mode_unsupported", "connection")
                    handshake, self._handshake = self._handshake, None
                    handshake["timer"].cancel()
                    diagnostic(dict(kind="protocol_hello", version=self.protocol_version, **self._hello))
                    handshake["future"].set_result(None)
                    continue
                response = record(value, ["version", "session", "id", "error" if "error" in value else "result"])
                response_scope_changed = not finite(response["version"]) or response[
                    "version"] != self.protocol_version or self._hello is None or response["session"] != self._hello[
                        "session"]
                if response_scope_changed:
                    raise ValueError("protocol: response belongs to a different version or session")
                pending = self._pending
                if pending is None or not finite(response["id"]) or response["id"] != pending["id"]:
                    raise ValueError("protocol: unexpected response id")
                if self._buffer:
                    raise ValueError("protocol: unsolicited data after response")
                result = error = failure = None
                if "error" in response:
                    failure = decode_failure(response["error"])
                    error = failure_error(pending["method"], failure)
                else:
                    result = decode_execution_result(pending["method"], response["result"])
                desktop_capture = (pending['method'] == 'capture' and result is not None
                                   and result.get('kind') == 'desktopCapture')
                if desktop_capture and result["observation"]["mode"] != self._hello["desktopMode"]:
                    raise ValueError("protocol: desktop observation mode differs from the negotiated session")
                summary = result
                if desktop_capture:
                    summary = {
                        **result, "screenshot":
                        dict(source="desktop", **(capture_metadata(result["screenshot"]) or {}))
                        if result["screenshot"] is not None else None
                    }
                elif pending['method'] == 'capture' and result is not None:
                    summary = {**result, 'image': dict(source='window', **(capture_metadata(result['image']) or {}))}
                diagnostic(
                    dict(
                        kind="command_end", traceId=pending["traceId"], id=pending["id"], method=pending["method"],
                        durationMs=_now() - pending["startedAt"], target=pending["target"],
                        status="error" if error is not None else "ok",
                        **(dict(error=str(error), failure=failure, stage="backend") if error is not None else dict(
                            result=summary))))
                pending["timer"].cancel()
                self._pending = None
                if error is not None:
                    pending["future"].set_exception(error)
                else:
                    pending["future"].set_result(result)
        except Exception as error:
            self.close(error)

    @staticmethod
    def _command_target(method, params):
        return dict(screen="desktop") if method == 'capture' and params.get('kind') == 'desktop' else None

    def _cancel_pending(self, pending):
        if pending["cancelSent"]:
            return
        pending["cancelSent"] = True
        if self._child is None or self._hello is None:
            return
        try:
            self._write(dict(version=self.protocol_version, session=self._hello["session"], cancel=pending["id"]))
        except Exception:
            logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

    def close(self, error=None):
        error = error if error is not None else RuntimeError("connection closed")
        self._generation += 1
        child, pending, uses_eof = self._child, self._pending, self._child_uses_eof
        if pending is not None:
            self._cancel_pending(pending)
        self._child = self._hello = None
        handshake, self._handshake = self._handshake, None
        if handshake is not None:
            handshake["timer"].cancel()
            failure = error if isinstance(error, ComputerUseError) else ComputerUseError(
                dict(code="execution_failed", target="hello", effect="none",
                     detail=f"{error}; no system command was sent; hello did not complete",
                     next="check the backend configuration before connecting again"), cause=error)
            if not handshake["future"].done():
                handshake["future"].set_exception(failure)
        self._pending = None
        if pending is not None:
            pure, target = pending['pure'], pending["target"] or {}
            failure = ComputerUseError(
                dict(
                    code="execution_failed" if pure else "execution_unknown", effect="none" if pure else "possible",
                    target=target.get("selector", target.get("screen", pending["method"])),
                    detail=(f"{error}; "
                            f"{'read-only request failed' if pure else 'command may have partially executed'}"
                            f"; current observation invalidated"),
                    next="reconnect by reading the target; inspect its state before retrying an action"), cause=error,
                method=pending["method"])
            diagnostic(
                dict(kind="command_end", traceId=pending["traceId"], id=pending["id"], method=pending["method"],
                     durationMs=_now() - pending["startedAt"], target=pending["target"], status="error",
                     stage="connection", error=str(failure),
                     failure={key: getattr(failure, key)
                              for key in ("code", "effect", "target", "detail", "next")}))
            pending["timer"].cancel()
            if not pending["future"].done():
                pending["future"].set_exception(failure)
        self.invalidate()
        self._buffer = b""
        atexit.unregister(self._on_exit)
        if self._preview_transport is not None:
            self._preview_transport.close()
            self._preview_transport = None
        if child is not None:
            child.stdin.close()
            if child.returncode is None and not uses_eof:
                try:
                    child.terminate()
                except ProcessLookupError:
                    logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

            async def retire():
                try:
                    try:
                        await asyncio.wait_for(child.wait(), driver_termination_grace_ms / 1000)
                    except TimeoutError:
                        if child.returncode is None:
                            diagnostic(
                                dict(kind="driver_forced_exit", pid=child.pid,
                                     terminationGraceMs=driver_termination_grace_ms))
                            try:
                                child.kill()
                            except ProcessLookupError:
                                logging.getLogger(__name__).debug(
                                    "Best-effort cleanup or preview delivery failed", exc_info=True
                                )
                        await child.wait()
                finally:
                    if self._retiring is asyncio.current_task():
                        self._retiring = None

            self._retiring = asyncio.create_task(retire())

    async def wait_closed(self):
        if self._retiring is not None:
            await asyncio.shield(self._retiring)
