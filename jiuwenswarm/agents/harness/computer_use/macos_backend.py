"""Agent Core's synchronous desktop contract over our owned Swift driver.

The original tools use foreground input. Their separate native session defaults
to exclusive; an explicit COMPUTER_USE_DESKTOP is honored without escalation.
The REPL's own mode, namespace and capture bindings are never changed.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading

from openjiuwen.harness.tools.cua.backend import WindowInfo
from openjiuwen.harness.tools.cua.desktop_tools import CuaToolError
from openjiuwen.harness.tools.cua.desktop_tools import CuaDesktop
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool.schema import ToolOutput


class MacOSBackend:
    def __init__(self, python=None, *, executable=None):
        self.python, self.executable = python or sys.executable, executable
        self.process = None
        self.session = None
        self.next_id = 0
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.target = None
        self.observation_only = False
        self.closed = False
        self.inflight = None

    def begin_call(self, *, observation_only=False):
        self.target = None
        self.observation_only = observation_only

    def _start(self):
        if self.closed:
            raise CuaToolError("The native desktop session is closed; delegate a fresh task.")
        if self.process is not None:
            return
        if self.executable is None:
            prepared = subprocess.run(
                [self.python, "-m", "jiuwenswarm.agents.harness.computer_use.worker", "--prepare-native"],
                stdout=subprocess.PIPE, text=True, timeout=180, check=True,
            )
            self.executable = json.loads(prepared.stdout)["executable"]
        if self.closed:
            raise CuaToolError("The desktop task ended during native preparation; no driver was started.")
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
            raise CuaToolError("The native driver does not provide the desktop backend adapter.")

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
            raise CuaToolError("Native delivery is unknown; observe before continuing. No input was replayed.") from error
        finally:
            self.inflight = None
        if isinstance(reply, BaseException):
            self.close()
            raise CuaToolError(str(reply)) from reply
        if reply.get("id") != identifier or reply.get("version") != 5:
            self.close()
            raise CuaToolError("Native response identity changed; delivery is unknown. No retry.")
        if hello:
            self.session = reply.get("session")
        elif reply.get("session") != self.session:
            self.close()
            raise CuaToolError("Native response belongs to another session. No retry.")
        if "error" in reply:
            failure = reply["error"]
            raise CuaToolError(
                f"{failure['detail']} Effect: {failure['effect']}. Next: {failure['next']}")
        return reply["result"]

    def _call(self, operation, **arguments):
        with self.lock:
            self._start()
            return self._exchange("cuaDesktop", dict(operation=operation, arguments=arguments, target=self.target))

    def list_windows(self):
        return [WindowInfo(**row) for row in self._call("windows")["windows"]]

    def activate(self, window_id):
        result = self._call("bind" if self.observation_only else "activate", window_id=window_id)
        self.target = result["target"]
        return True

    def foreground_window_id(self):
        return self._call("foreground")["window_id"]

    def screenshot(self, region=None):
        return base64.b64decode(self._call("screenshot", region=region)["png"], validate=True)

    def screen_size(self):
        value = self._call("screen")
        return value["width"], value["height"]

    def cursor_position(self):
        value = self._call("cursor")
        return value["x"], value["y"]

    def click(self, x, y, button, count):
        self._call("click", x=x, y=y, button=button, count=count)

    def move_cursor(self, x, y):
        self._call("move", x=x, y=y)

    def drag(self, from_xy, to_xy, button, duration_s):
        self._call("drag", start=list(from_xy), end=list(to_xy), button=button, duration=duration_s)

    def scroll(self, x, y, direction, ticks):
        self._call("scroll", x=x, y=y, direction=direction, ticks=ticks)

    def type_text(self, text):
        self._call("text", text=text)

    def press_key(self, key, modifiers):
        self._call("key", key=normalize_key(key, modifiers))

    def hotkey(self, keys):
        if len(keys) < 2:
            raise CuaToolError("hotkey requires modifiers followed by one key")
        self.press_key(keys[-1], keys[:-1])

    def launch(self, args):
        return self.launch_application(dict(path=args[0], additional_arguments=list(args[1:])))["pid"]

    def launch_application(self, arguments):
        return self._call("launch", **arguments)

    def kill(self, pid):
        raise CuaToolError("Force termination is not exposed by this desktop subagent.")

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
                    self.process.stdin.write(json.dumps(dict(version=5, session=self.session, cancel=identifier)) + "\n")
                    self.process.stdin.flush()
            except OSError:
                pass  # The owning call still waits for cleanup/EOF, never replays.


def normalize_key(key, modifiers):
    aliases = {"return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape",
               "backspace": "BackSpace", "delete": "Delete", "tab": "Tab", "space": "space",
               "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
               "up": "Up", "down": "Down", "left": "Left", "right": "Right"}
    modifier_names = {"cmd": "super", "command": "super", "win": "super", "windows": "super",
                      "super": "super", "ctrl": "ctrl", "control": "ctrl", "alt": "alt",
                      "option": "alt", "shift": "shift"}
    normalized = []
    for item in modifiers:
        if str(item).lower() not in modifier_names:
            raise CuaToolError("Unsupported modifier: " + str(item))
        normalized.append(modifier_names[str(item).lower()])
    lower = str(key).lower()
    value = aliases.get(lower, lower.upper() if lower.startswith("f") and lower[1:].isdigit() else lower)
    return "+".join([*normalized, value])


def install_macos_backend(desktop: CuaDesktop, python=None):
    """Bridge the pinned core's missing desktop-factory hook, per agent only."""
    if desktop._backend is not None:
        raise RuntimeError("Install the macOS desktop adapter before core CUA preflight.")
    desktop._backend_factory = lambda: MacOSBackend(python)


class MacOSLaunchTool(Tool):
    """LaunchServices resolves macOS names/bundle IDs instead of PATH/aumid."""

    def __init__(self, original, desktop):
        card = original.card.model_copy(deep=True)
        card.description = "Launch a macOS app by name, bundle_id or absolute .app path; optional additional_arguments. Returns its real window-owner PIDs."
        card.input_params["properties"]["bundle_id"]["description"] = "macOS bundle identifier, e.g. com.apple.calculator."
        card.input_params["properties"]["name"]["description"] = "An installed application's exact name. Use an absolute .app path if ambiguous."
        card.input_params["properties"]["path"]["description"] = "Absolute .app bundle path on this Mac."
        super().__init__(card)
        self.desktop = self._desktop = desktop

    async def invoke(self, inputs, **kwargs):
        import asyncio
        try:
            value = await asyncio.to_thread(self.desktop.backend.launch_application, dict(inputs))
            windows = [WindowInfo(**row).to_dict() for row in value["windows"]]
            pid = windows[0]["pid"] if windows else value["pid"]
            note = "" if windows else " No window appeared within 10s; call list_windows to inspect it."
            content = "✅ Launched application (pid " + str(pid) + ")." + note + "\n" + json.dumps({"windows": windows}, ensure_ascii=False)
            return ToolOutput(success=True, data={"content": content})
        except Exception as error:
            return ToolOutput(success=False, error="❌ launch_app: " + str(error))

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
