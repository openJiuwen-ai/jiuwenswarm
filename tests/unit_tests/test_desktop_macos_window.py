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


def test_macos_close_hides_window_and_dock_reopens_it(
    desktop_app, monkeypatch, tmp_path
) -> None:
    actions = []
    shown = []

    class FakeButton:
        def setTarget_(self, target) -> None:
            actions.append(("target", target))

        def setAction_(self, action: str) -> None:
            actions.append(("action", action))

    button = FakeButton()

    class FakeNativeWindow:
        def standardWindowButton_(self, button_type):
            actions.append(("button", button_type))
            return button

    native_window = FakeNativeWindow()
    appkit = types.SimpleNamespace(NSWindowCloseButton=123)
    app_helper = types.SimpleNamespace(callAfter=lambda callback: callback())

    class FakeAppDelegate:
        @staticmethod
        def instancesRespondToSelector_(_selector) -> bool:
            return False

    browser_view = types.SimpleNamespace(AppDelegate=FakeAppDelegate)
    objc = types.SimpleNamespace(
        _C_NSBOOL=b"Z",
        selector=lambda callback, signature: callback,
    )

    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        types.SimpleNamespace(AppHelper=app_helper),
    )
    monkeypatch.setitem(sys.modules, "objc", objc)
    monkeypatch.setitem(
        sys.modules,
        "webview.platforms.cocoa",
        types.SimpleNamespace(BrowserView=browser_view),
    )
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")

    runtime = desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports=calculate_instance_ports(0),
    )
    runtime.window = types.SimpleNamespace(
        native=native_window,
        show=lambda: shown.append(True),
    )

    runtime._configure_macos_window_lifecycle()

    assert actions == [
        ("button", appkit.NSWindowCloseButton),
        ("target", native_window),
        ("action", "orderOut:"),
    ]
    reopen = FakeAppDelegate.applicationShouldHandleReopen_hasVisibleWindows_
    assert reopen(None, None, False) is True
    assert shown == [True]


def test_non_macos_close_button_is_unchanged(desktop_app, monkeypatch, tmp_path) -> None:
    native_calls = []

    class FakeNativeWindow:
        def standardWindowButton_(self, button_type):
            native_calls.append(button_type)

    monkeypatch.setattr(desktop_app.sys, "platform", "win32")
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")
    runtime = desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports=calculate_instance_ports(0),
    )
    runtime.window = types.SimpleNamespace(native=FakeNativeWindow())

    runtime._configure_macos_window_lifecycle()

    assert native_calls == []
