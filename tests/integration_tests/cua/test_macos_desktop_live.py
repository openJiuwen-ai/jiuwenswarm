"""Opt-in desktop acceptance of original CUA tools and REPL on an owned fixture."""

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from unittest.mock import MagicMock

from PIL import Image
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.subagents.cua_agent import create_cua_agent
import psutil
import pytest

from jiuwenswarm.agents.harness.computer_use.rail import CuaExtensionRail
from jiuwenswarm.agents.harness.computer_use.macos_backend import MacOSBackend

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("CUA_MACOS_LIVE_TEST") != "1",
    reason="Needs an unlocked macOS desktop, Swift and accessibility/screen-recording permissions",
)


async def test_existing_tools_drive_two_native_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPUTER_USE_DESKTOP", raising=False)
    bundle = tmp_path / "MacCuaFixture.app"
    executable = bundle / "Contents/MacOS/MacCuaFixture"
    executable.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures/MacDesktopFixture.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(executable)], check=True)
    with (bundle / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "local.jiuwenswarm.cua-fixture", "CFBundleExecutable": "MacCuaFixture",
                      "CFBundleName": "Mac CUA Fixture", "CFBundlePackageType": "APPL", "CFBundleVersion": "1"}, handle)
    state_file = tmp_path / "state.json"
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", MagicMock())
    await Runner.start()
    rail = CuaExtensionRail()
    agent = create_cua_agent(
        MagicMock(), rails=[rail], auto_create_workspace=False,
        enable_sys_operation=False, enable_skill_discovery=False, enable_read_image_multimodal=True,
    )
    await agent._ensure_initialized()
    tools = {tool.card.name: tool for tool in rail.tools}
    pid = None
    caffeinate = None

    async def invoke(name, **arguments):
        result = await tools[name].invoke(arguments)
        assert result.success, (name, result.error)
        return result

    def state():
        return json.loads(state_file.read_text())

    async def until(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                await asyncio.sleep(.1)

    def point(row, fixture_point):
        info = state()["windows"][0]
        return {"x": info[fixture_point][0] * row["bounds"]["width"] / info["width"],
                "y": info[fixture_point][1] * row["bounds"]["height"] / info["height"]}

    try:
        await rail.native_rail.before_invoke(AgentCallbackContext(agent=agent))
        launched = await invoke("launch_app", path=str(bundle), additional_arguments=[str(state_file)])
        await until(state_file.exists)
        pid = state()["pid"]
        caffeinate = subprocess.Popen(["caffeinate", "-di", "-w", str(pid)])
        await invoke("list_apps")
        await invoke("get_screen_size")
        await invoke("get_cursor_position")
        result = await invoke("list_windows", pid=pid)
        rows = json.loads(result.data["content"].split("\n", 1)[1])["windows"]
        own = state()["windows"]
        first = next(row for row in rows if row["window_id"] == own[0]["id"])
        second = next(row for row in rows if row["window_id"] == own[1]["id"])
        target = dict(pid=pid, window_id=first["window_id"])
        before = rail.desktop.backend.foreground_window_id()
        captured = await invoke("get_window_state", **target)
        assert rail.desktop.backend.foreground_window_id() == before
        first = json.loads(captured.data["content"].split("\n", 1)[1])
        png = base64.b64decode(captured.data["multimodal"][0]["data_url"].split(",", 1)[1])
        (tmp_path / "window.png").write_bytes(png)
        with Image.open(io.BytesIO(png)) as image:
            assert image.size == (first["bounds"]["width"], first["bounds"]["height"])

        await invoke("type_text", **target, **point(first, "field"), text="中文 hello")
        await until(lambda: state()["windows"][0]["text"] == "中文 hello")
        assert state()["windows"][1]["text"] == ""
        await invoke("hotkey", **target, keys=["cmd", "a"])
        await invoke("type_text", **target, text="替换成功")
        await invoke("press_key", **target, key="return")
        await until(lambda: state()["windows"][0]["text"] == "替换成功")
        second_target = dict(pid=pid, window_id=second["window_id"])
        info = own[1]
        await invoke("type_text", **second_target,
            x=info["field"][0] * second["bounds"]["width"] / info["width"],
            y=info["field"][1] * second["bounds"]["height"] / info["height"], text="second window")
        await until(lambda: state()["windows"][1]["text"] == "second window")
        assert state()["windows"][0]["text"] == "替换成功"
        await invoke("bring_to_front", **target)
        await invoke("click", **target, **point(first, "box"))
        await until(lambda: state()["windows"][0]["checked"])
        surface = point(first, "surface")
        for name in ("move_cursor", "double_click", "right_click"):
            await invoke(name, **target, **surface)
        await invoke("click", **target, **surface, button="middle")
        await invoke("click", **target, **surface, count=3)
        await invoke("scroll", **target, **surface, direction="down", amount=3)
        await until(lambda: state()["windows"][0]["wheels"] > 0)
        await invoke("zoom", **target, x1=surface["x"] - 20, y1=surface["y"] - 15,
            x2=surface["x"] + 20, y2=surface["y"] + 15)
        origin = rail.desktop.zoom_origins[pid]
        await invoke("click", **target, from_zoom=True, x=surface["x"] - origin.x, y=surface["y"] - origin.y)
        for button in ("left", "right"):
            await invoke("drag", **target, from_x=surface["x"] - 15, from_y=surface["y"],
                to_x=surface["x"] + 15, to_y=surface["y"], button=button, duration_ms=150)
        await until(lambda: state()["windows"][0]["drags"] > 5)
        events = state()["windows"][0]
        assert 2 in events["clicks"] and 3 in events["clicks"]
        assert events["rights"] >= 1 and events["middles"] >= 1

        pending = asyncio.create_task(tools["drag"].invoke(dict(target, from_x=surface["x"] - 15,
            from_y=surface["y"], to_x=surface["x"] + 15, to_y=surface["y"], duration_ms=5000)))
        await asyncio.sleep(.3)
        started = time.monotonic()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert time.monotonic() - started < 2
        await invoke("click", **target, **point(first, "box"))
        await until(lambda: not state()["windows"][0]["checked"])

        directory = await invoke("cua_repl", code=(
            "app = computer.app('local.jiuwenswarm.cua-fixture')\n"
            "print((await app.read({'full': True})).text)"))
        text = directory.data["content"]
        selected = re.search(r'(\S+) "CUA Fixture 1"', text)
        assert selected, text
        observed = await invoke("cua_repl", code=(
            f"first = app.window({selected.group(1)!r})\n"
            "view = await first.read({'full': True})\nprint(view.text)\n"
            "printImage(view.screenshot)"))
        assert "Fixture input 1" in observed.data["content"]
        assert observed.data["multimodal"][0]["data_url"].startswith("data:image/png;base64,")
        await invoke("cua_repl", code=(
            "field = first.find(['Fixture input 1'])\n"
            "print(await field.typeText('REPL 接入成功', {'replace': True}))\n"
            "print((await first.read({'full': True})).text)"))
        await until(lambda: state()["windows"][0]["text"] == "REPL 接入成功")
        assert state()["windows"][1]["text"] == "second window"
        # Native keyboard tools use the current responder, not an AX reference.
        await invoke("click", **target, **point(first, "field"))
        await invoke("hotkey", **target, keys=["cmd", "a"])
        await invoke("type_text", **target, text="原工具继续执行")
        await until(lambda: state()["windows"][0]["text"] == "原工具继续执行")
        monkeypatch.setenv("COMPUTER_USE_DESKTOP", "shared")
        shared = MacOSBackend()
        try:
            await asyncio.to_thread(shared.list_windows)
            before = await asyncio.to_thread(shared.foreground_window_id)
            with pytest.raises(Exception, match="requires exclusive mode"):
                await asyncio.to_thread(shared.activate, target["window_id"])
            assert await asyncio.to_thread(shared.foreground_window_id) == before
        finally:
            await asyncio.to_thread(shared.close)
    finally:
        await rail.close()
        if pid and psutil.pid_exists(pid):
            process = psutil.Process(pid)
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
        if caffeinate is not None:
            caffeinate.terminate()
            caffeinate.wait(timeout=3)
        await Runner.stop()
