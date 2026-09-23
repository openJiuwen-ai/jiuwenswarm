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
        def orderOut_(self, sender) -> None:
            actions.append(("order_out", sender))

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

    class FakeWindowDelegate:
        def windowShouldClose_(self, window) -> bool:
            actions.append(("original_should_close", window))
            return True

    browser_view = types.SimpleNamespace(
        AppDelegate=FakeAppDelegate,
        WindowDelegate=FakeWindowDelegate,
    )
    selectors = []

    def fake_selector(callback, selector, signature):
        selectors.append((selector, signature))
        return callback

    objc = types.SimpleNamespace(_C_NSBOOL=b"Z", selector=fake_selector)

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

    assert selectors == [
        (b"windowShouldClose:", b"Z@:@"),
        (b"applicationShouldHandleReopen:hasVisibleWindows:", b"Z@:@Z"),
    ]
    assert actions == [
        ("button", appkit.NSWindowCloseButton),
        ("target", native_window),
        ("action", "orderOut:"),
    ]
    delegate = FakeWindowDelegate()
    assert delegate.windowShouldClose_(native_window) is False
    assert actions[-1] == ("order_out", None)

    runtime._allow_window_close = True
    assert delegate.windowShouldClose_(native_window) is True
    assert actions[-1] == ("original_should_close", native_window)

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


def test_explicit_macos_exit_allows_window_destruction(
    desktop_app, monkeypatch, tmp_path
) -> None:
    destroyed = []

    class ImmediateThread:
        def __init__(self, target, daemon) -> None:
            self._target = target
            assert daemon is True

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")
    runtime = desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports=calculate_instance_ports(0),
    )
    runtime.window = types.SimpleNamespace(destroy=lambda: destroyed.append(True))

    assert runtime.close_window() is True
    assert runtime._allow_window_close is True
    assert destroyed == [True]


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
