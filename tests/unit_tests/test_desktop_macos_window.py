# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""macOS desktop window control behavior."""

from __future__ import annotations

import sys
import types

import pytest

from jiuwenswarm.instance_manager.config import calculate_instance_ports


@pytest.fixture
def desktop_app(monkeypatch):
    if "webview" not in sys.modules:
        monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    sys.modules.pop("jiuwenswarm.channels.desktop.desktop_app", None)
    from jiuwenswarm.channels.desktop import desktop_app as mod

    return mod


def test_macos_context_paste_dispatches_native_action_on_ui_thread(desktop_app, monkeypatch, tmp_path):
    actions = []

    class Responder:
        def respondsToSelector_(self, selector):
            return selector == "paste:"

        def paste_(self, sender):
            actions.append(("paste", sender))

    def call_after(callback):
        actions.append("ui-dispatch")
        callback()

    monkeypatch.setitem(sys.modules, "PyObjCTools", types.SimpleNamespace(
        AppHelper=types.SimpleNamespace(callAfter=call_after),
    ))
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")
    runtime = desktop_app.DesktopRuntime(frontend_host="127.0.0.1", ports=calculate_instance_ports(0))
    runtime.window = types.SimpleNamespace(native=types.SimpleNamespace(firstResponder=lambda: Responder()))
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    desktop_app._WindowApi(runtime).paste_clipboard()
    assert actions == ["ui-dispatch", ("paste", None)]

    runtime.window.native.firstResponder = lambda: None
    with pytest.raises(RuntimeError, match="Focused view"):
        runtime.paste_clipboard()


def test_pywebview_native_paste_api_is_only_exposed_on_macos(desktop_app, monkeypatch):
    runtime = types.SimpleNamespace(paste_clipboard=lambda: None)
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    assert callable(desktop_app._WindowApi(runtime).paste_clipboard)
    monkeypatch.setattr(desktop_app.sys, "platform", "win32")
    assert not hasattr(desktop_app._WindowApi(runtime), "paste_clipboard")
