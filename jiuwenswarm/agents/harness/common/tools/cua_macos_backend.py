"""Adapt Agent Core's original desktop tools to the embedded engine."""

from __future__ import annotations

import asyncio
import base64
import json

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.tools.cua.backend import WindowInfo
from openjiuwen.harness.tools.cua.desktop_tools import CuaDesktop, CuaToolError

from jiuwenswarm.cua_engine.desktop import DesktopSession
from jiuwenswarm.cua_engine.hosting import prepare_native


class MacOSBackend:
    def __init__(self, *, executable=None):
        self.channel = DesktopSession(self._prepare_driver if executable is None else executable)
        self.target = None
        self.observation_only = False

    def _prepare_driver(self):
        return str(prepare_native())

    def _call(self, operation, **arguments):
        try:
            return self.channel.call(operation, arguments, target=self.target)
        except RuntimeError as error:
            raise CuaToolError(str(error)) from error

    def begin_call(self, *, observation_only=False):
        self.target = None
        self.observation_only = observation_only

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

    @staticmethod
    def kill(pid):
        raise CuaToolError("Force termination is not exposed by this desktop subagent.")

    def close(self):
        self.channel.close()

    def cancel_current(self):
        self.channel.cancel_current()


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


def install_macos_backend(desktop: CuaDesktop):
    """Bridge the pinned core's missing desktop-factory hook, per agent only."""
    # The pinned CuaDesktop has no public backend replacement API.
    # pylint: disable-next=protected-access
    if desktop._backend is not None:
        raise RuntimeError("Install the macOS desktop adapter before core CUA preflight.")
    desktop._backend_factory = MacOSBackend  # pylint: disable=protected-access


class MacOSLaunchTool(Tool):
    """LaunchServices resolves macOS names/bundle IDs instead of PATH/aumid."""

    def __init__(self, original, desktop):
        card = original.card.model_copy(deep=True)
        card.description = (
            "Launch a macOS app by name, bundle_id or absolute .app path; optional additional_arguments. "
            "Returns its real window-owner PIDs."
        )
        card.input_params["properties"]["bundle_id"]["description"] = (
            "macOS bundle identifier, e.g. com.apple.calculator."
        )
        card.input_params["properties"]["name"]["description"] = (
            "An installed application's exact name. Use an absolute .app path if ambiguous."
        )
        card.input_params["properties"]["path"]["description"] = "Absolute .app bundle path on this Mac."
        super().__init__(card)
        self.desktop = self._desktop = desktop

    async def invoke(self, inputs, **kwargs):
        try:
            value = await asyncio.to_thread(self.desktop.backend.launch_application, dict(inputs))
            windows = [WindowInfo(**row).to_dict() for row in value["windows"]]
            pid = windows[0]["pid"] if windows else value["pid"]
            note = "" if windows else " No window appeared within 10s; call list_windows to inspect it."
            content = (
                "✅ Launched application (pid " + str(pid) + ")." + note + "\n"
                + json.dumps({"windows": windows}, ensure_ascii=False)
            )
            return ToolOutput(success=True, data={"content": content})
        except Exception as error:
            return ToolOutput(success=False, error="❌ launch_app: " + str(error))

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
