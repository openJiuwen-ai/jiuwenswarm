# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows desktop close, persistence, and tray behavior."""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

from jiuwenswarm.instance_manager.config import calculate_instance_ports


@pytest.fixture
def desktop_app(monkeypatch):
    if "webview" not in sys.modules:
        monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    sys.modules.pop("jiuwenswarm.channels.desktop.desktop_app", None)
    from jiuwenswarm.channels.desktop import desktop_app as mod

    return mod


class _Window:
    def __init__(self) -> None:
        self.hidden = False
        self.minimized = False
        self.shown = False
        self.maximized = False
        self.destroyed = False

    def hide(self) -> None:
        self.hidden = True

    def minimize(self) -> None:
        self.minimized = True

    def show(self) -> None:
        self.shown = True

    def maximize(self) -> None:
        self.maximized = True

    def destroy(self) -> None:
        self.destroyed = True


def _runtime(desktop_app, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")
    runtime = desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports=calculate_instance_ports(0),
    )
    runtime.window = _Window()
    return runtime


def test_close_action_preference_round_trip(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(desktop_app, "get_user_workspace_dir", lambda: tmp_path)

    assert desktop_app._load_close_action() is None
    assert desktop_app._save_close_action(desktop_app.CLOSE_ACTION_HIDE) is True
    assert desktop_app._load_close_action() == desktop_app.CLOSE_ACTION_HIDE
    assert (
        tmp_path / "config" / desktop_app.DESKTOP_WINDOW_PREFERENCES_FILENAME
    ).is_file()

    assert desktop_app._save_close_action(desktop_app.CLOSE_ACTION_ASK) is True
    assert desktop_app._load_close_action() == desktop_app.CLOSE_ACTION_ASK


def test_window_api_reads_and_updates_close_action(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: True)
    monkeypatch.setattr(desktop_app, "get_user_workspace_dir", lambda: tmp_path)
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    api = desktop_app._WindowApi(runtime)

    # pywebview removes the receiver from the JS-facing method parameters.
    assert inspect.ismethod(api.get_close_action)
    assert inspect.getfullargspec(api.get_close_action).args[1:] == []
    assert inspect.getfullargspec(api.set_close_action).args[1:] == ["action"]
    assert api.get_close_action() == desktop_app.CLOSE_ACTION_ASK
    assert api.set_close_action(desktop_app.CLOSE_ACTION_QUIT) is True
    assert api.get_close_action() == desktop_app.CLOSE_ACTION_QUIT
    assert api.set_close_action("invalid") is False


def test_window_api_hides_close_action_setting_outside_windows(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    api = desktop_app._WindowApi(runtime)
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: False)

    assert api.get_close_action() is None
    assert api.set_close_action(desktop_app.CLOSE_ACTION_HIDE) is False


def test_windows_close_prompt_adapts_to_wrapped_message(desktop_app) -> None:
    source = inspect.getsource(desktop_app.DesktopRuntime._prompt_windows_close_action)

    assert "message.Size = Size(382, message.GetPreferredSize(Size(382, 0)).Height)" in source
    assert "first_option_top = max(62, message.Bottom + 14)" in source
    assert "dialog.ClientSize = Size(430, cancel_button.Bottom + 20)" in source


def test_saved_ask_prompts_again(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: True)
    monkeypatch.setattr(
        desktop_app, "_load_close_action", lambda: desktop_app.CLOSE_ACTION_ASK
    )
    monkeypatch.setattr(
        runtime,
        "_prompt_windows_close_action",
        lambda: (desktop_app.CLOSE_ACTION_QUIT, False),
    )

    assert runtime._on_closing() is None
    assert runtime._allow_window_close is True


def test_close_prompts_remembers_and_hides_to_tray(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    saved: list[str] = []
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: True)
    monkeypatch.setattr(desktop_app, "_load_close_action", lambda: None)
    monkeypatch.setattr(
        desktop_app, "_save_close_action", lambda action: saved.append(action) or True
    )
    monkeypatch.setattr(
        runtime,
        "_prompt_windows_close_action",
        lambda: (desktop_app.CLOSE_ACTION_HIDE, True),
    )
    monkeypatch.setattr(runtime, "_ensure_windows_tray", lambda: True)

    assert runtime._on_closing() is False
    assert runtime.window.hidden is True
    assert runtime.window.minimized is False
    assert saved == [desktop_app.CLOSE_ACTION_HIDE]
    assert runtime._allow_window_close is False


def test_saved_hide_falls_back_to_taskbar_if_tray_creation_fails(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: True)
    monkeypatch.setattr(
        desktop_app, "_load_close_action", lambda: desktop_app.CLOSE_ACTION_HIDE
    )
    monkeypatch.setattr(runtime, "_ensure_windows_tray", lambda: False)

    assert runtime._on_closing() is False
    assert runtime.window.hidden is False
    assert runtime.window.minimized is True


def test_saved_quit_allows_native_close(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    monkeypatch.setattr(desktop_app, "_is_windows_desktop", lambda: True)
    monkeypatch.setattr(
        desktop_app, "_load_close_action", lambda: desktop_app.CLOSE_ACTION_QUIT
    )

    assert runtime._on_closing() is None
    assert runtime._allow_window_close is True
    assert runtime.window.hidden is False
    assert runtime.window.minimized is False


def test_explicit_close_bypasses_native_close_prompt(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)

    class _ImmediateThread:
        def __init__(self, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(desktop_app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_app.threading, "Thread", _ImmediateThread)

    assert runtime.close_window() is True
    assert runtime._allow_window_close is True
    assert runtime.window.destroyed is True


def test_tray_restore_shows_and_maximizes_window(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)

    assert runtime.show_and_maximize_window() is True
    assert runtime.window.shown is True
    assert runtime.window.maximized is True
