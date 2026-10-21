"""Synchronous native desktop transport for embedding in existing tool hosts.

Owns only framing, session identity, cancellation and process cleanup. The host
keeps its tool schema, window objects and result conversion.
"""

import json
import os
import queue
import subprocess
import threading


class DesktopSession:
    def __init__(self, executable):
        self.executable = executable
        self.process = self.session = self.inflight = None
        self.next_id = 0
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.closed = False

    def _start(self):
        if self.closed:
            raise RuntimeError("The native desktop session is closed; delegate a fresh task.")
        if self.process is not None:
            return
        if callable(self.executable):
            self.executable = self.executable()
        if self.closed:
            raise RuntimeError("The desktop task ended during native preparation; no driver was started.")
        env = dict(os.environ)
        env.setdefault("COMPUTER_USE_DESKTOP", "exclusive")
        self.process = subprocess.Popen(
            [self.executable, "--cua-backend"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        process = self.process

        def read():
            try:
                for line in process.stdout:
                    self.responses.put(json.loads(line))
            except Exception as error:
                self.responses.put(error)
            finally:
                self.responses.put(EOFError("macOS driver disconnected; prior actions may have executed"))

        threading.Thread(target=read, name="macos-cua-responses", daemon=True).start()
        hello = self._exchange("hello", {"desktopMode": env["COMPUTER_USE_DESKTOP"]}, hello=True)
        if "cuaDesktopBackend" not in hello["capabilities"]:
            self.close()
            raise RuntimeError("The native driver does not provide the desktop backend adapter.")

    def _exchange(self, method, params, *, hello=False):
        identifier = self.next_id
        self.next_id += 1
        request = dict(version=5, id=identifier, method=method, params=params)
        if not hello:
            request.update(session=self.session, timeoutMs=20000)
        try:
            with self.write_lock:
                self.inflight = identifier
                self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                self.process.stdin.flush()
            reply = self.responses.get(timeout=25)
        except (OSError, queue.Empty) as error:
            self.close()
            raise RuntimeError(
                "Native delivery is unknown; observe before continuing. No input was replayed."
            ) from error
        finally:
            self.inflight = None
        if isinstance(reply, BaseException):
            self.close()
            raise RuntimeError(str(reply)) from reply
        if reply.get("id") != identifier or reply.get("version") != 5:
            self.close()
            raise RuntimeError("Native response identity changed; delivery is unknown. No retry.")
        if hello:
            self.session = reply.get("session")
        elif reply.get("session") != self.session:
            self.close()
            raise RuntimeError("Native response belongs to another session. No retry.")
        if "error" in reply:
            failure = reply["error"]
            raise RuntimeError(
                f"{failure['detail']} Effect: {failure['effect']}. Next: {failure['next']}")
        return reply["result"]

    def call(self, operation, arguments, *, target=None):
        with self.lock:
            self._start()
            return self._exchange("cuaDesktop", dict(operation=operation, arguments=arguments, target=target))

    def close(self):
        self.closed = True
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Native input cleanup is unconfirmed; desktop input must remain blocked.") from error

    def cancel_current(self):
        identifier = self.inflight
        if self.process is not None and identifier and self.session:
            try:
                with self.write_lock:
                    request = dict(version=5, session=self.session, cancel=identifier)
                    self.process.stdin.write(json.dumps(request) + "\n")
                    self.process.stdin.flush()
            except OSError:
                pass  # The owning call still waits for cleanup/EOF, never replays.
