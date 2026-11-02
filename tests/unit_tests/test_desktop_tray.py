import sys
import types

import pytest

from jiuwenswarm.channels.desktop import tray as tray_module


class _FakeMenuItem:
    def __init__(self, text, action, checked=None, radio=False, default=False, visible=True, enabled=True):
        self.text = text
        self.action = action
        self.default = default
        self.enabled = enabled


class _FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class _FakeIcon:
    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.notifications: list[tuple[str, str]] = []
        self.run_detached_called = False
        self.stopped = False

    def run_detached(self):
        self.run_detached_called = True

    def stop(self):
        self.stopped = True

    def notify(self, message, title=None):
        self.notifications.append((title, message))


@pytest.fixture(autouse=True)
def fake_pystray(monkeypatch):
    fake_module = types.SimpleNamespace(Icon=_FakeIcon, MenuItem=_FakeMenuItem, Menu=_FakeMenu)
    monkeypatch.setitem(sys.modules, "pystray", fake_module)
    yield fake_module


def _controller(on_open=None, on_quit=None) -> tray_module.TrayController:
    return tray_module.TrayController(
        "WorkSwarm",
        icon_path=None,
        on_open=on_open or (lambda: None),
        on_quit=on_quit or (lambda: None),
    )


def test_render_icon_paints_the_state_color_at_the_dot(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    controller = _controller()

    center = tray_module._ICON_SIZE - tray_module._DOT_RADIUS - 2
    for state, expected_rgb in (
        ("idle", tray_module._STATE_COLORS["idle"][:3]),
        ("running", tray_module._STATE_COLORS["running"][:3]),
        ("waiting", tray_module._STATE_COLORS["waiting"][:3]),
        ("error", tray_module._STATE_COLORS["error"][:3]),
    ):
        image = controller._render_icon(state)
        assert image.getpixel((center, center))[:3] == expected_rgb


def test_render_icon_falls_back_to_idle_color_for_an_unknown_state():
    controller = _controller()
    center = tray_module._ICON_SIZE - tray_module._DOT_RADIUS - 2
    image = controller._render_icon("not-a-real-state")
    assert image.getpixel((center, center))[:3] == tray_module._STATE_COLORS["idle"][:3]


def test_update_state_falls_back_to_idle_for_an_unknown_state():
    controller = _controller()
    controller.update_state("not-a-real-state", "", "", "")
    assert controller._state == "idle"


def test_update_state_only_notifies_when_a_title_is_given():
    controller = _controller()
    controller.update_state("running", "", "", "")
    assert controller._icon.notifications == []

    controller.update_state("error", "Heartbeat task failed", "boom", "job-1")
    assert controller._icon.notifications == [("Heartbeat task failed", "boom")]


def test_start_runs_the_icon_detached_so_the_host_loop_is_not_blocked():
    controller = _controller()
    controller.start()
    assert controller._icon.run_detached_called is True


def test_stop_stops_the_icon_on_windows_and_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    controller = _controller()
    controller.stop()
    assert controller._icon.stopped is True


def test_stop_is_a_noop_on_macos(monkeypatch):
    # pystray's darwin backend calls NSApplication.stop_() on the *shared* app
    # instance pywebview's own window also runs on -- stopping it here would
    # end pywebview's run loop too, not just the tray's.
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)
    controller = _controller()
    controller.stop()
    assert controller._icon.stopped is False


def test_open_menu_item_invokes_the_on_open_callback():
    calls = []
    controller = _controller(on_open=lambda: calls.append("opened"))
    open_item = next(item for item in controller._icon.menu.items if getattr(item, "default", False))
    open_item.action(controller._icon, open_item)
    assert calls == ["opened"]


def test_quit_menu_item_invokes_the_on_quit_callback():
    calls = []
    controller = _controller(on_quit=lambda: calls.append("quit"))
    quit_item = next(item for item in controller._icon.menu.items if getattr(item, "text", None) == "Quit")
    quit_item.action(controller._icon, quit_item)
    assert calls == ["quit"]


def test_open_and_quit_callback_exceptions_are_swallowed_not_raised():
    def _raise():
        raise RuntimeError("boom")

    controller = _controller(on_open=_raise, on_quit=_raise)
    open_item = next(item for item in controller._icon.menu.items if getattr(item, "default", False))
    quit_item = next(item for item in controller._icon.menu.items if getattr(item, "text", None) == "Quit")

    # Must not raise -- a broken callback must not take the tray icon down with it.
    open_item.action(controller._icon, open_item)
    quit_item.action(controller._icon, quit_item)


def test_notify_swallows_errors_from_the_underlying_icon():
    controller = _controller()

    def _raise(message, title=None):
        raise RuntimeError("no notification backend available")

    controller._icon.notify = _raise

    # Must not raise.
    controller.notify("title", "body")
