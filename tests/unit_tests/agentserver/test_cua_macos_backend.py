"""Desktop adapter contracts, independent of a real macOS desktop."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.computer_use.macos_backend import MacOSBackend, MacOSDesktop, normalize_key
from jiuwenswarm.agents.harness.computer_use.rail import ComputerUseRail


@pytest.mark.parametrize("key, modifiers, expected", [
    ("return", [], "Return"), ("ENTER", [], "Return"), ("pageup", [], "Page_Up"),
    ("f12", ["ctrl", "option"], "ctrl+alt+F12"), ("a", ["cmd"], "super+a"),
    ("Escape", ["shift"], "shift+Escape"), ("v", ["win"], "super+v"),
])
def test_key_translation(key, modifiers, expected):
    assert normalize_key(key, modifiers) == expected


def test_unknown_modifier_is_not_replaced_by_another_key():
    with pytest.raises(Exception, match="Unsupported modifier"):
        normalize_key("a", ["hyper"])


@pytest.mark.parametrize("configured, expected", [(None, "exclusive"), ("shared", "shared")])
def test_native_session_honors_the_host_mode(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("COMPUTER_USE_DESKTOP", raising=False)
    else:
        monkeypatch.setenv("COMPUTER_USE_DESKTOP", configured)
    factory = MagicMock()
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.macos_backend.subprocess.Popen", factory)
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.macos_backend.threading.Thread", MagicMock())
    backend = MacOSBackend(executable="owned-driver")
    backend._exchange = MagicMock(return_value={"capabilities": ["cuaDesktopBackend"]})
    backend.screen_size = MagicMock()
    backend._start()
    assert factory.call_args.kwargs["env"]["COMPUTER_USE_DESKTOP"] == expected
    assert factory.call_args.args[0] == ["owned-driver", "--cua-backend"]
    backend.close()


def test_closed_task_never_starts_a_late_driver(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("jiuwenswarm.agents.harness.computer_use.macos_backend.subprocess.Popen", factory)
    backend = MacOSBackend(executable="owned-driver")
    backend.close()
    with pytest.raises(Exception, match="session is closed"):
        backend.screen_size()
    factory.assert_not_called()


def test_cancel_addresses_only_the_inflight_native_request():
    backend = MacOSBackend(executable="unused")
    backend.session, backend.inflight = "child-session", 27
    backend.process = SimpleNamespace(stdin=MagicMock())
    backend.cancel_current()
    assert json.loads(backend.process.stdin.write.call_args.args[0]) == {
        "version": 5, "session": "child-session", "cancel": 27}


def test_each_tool_call_forgets_the_previous_recipient():
    backend = MacOSBackend(executable="unused")
    backend.target = "previous-window"
    backend.begin_call(observation_only=True)
    assert backend.target is None and backend.observation_only
    backend._call = MagicMock(return_value={"target": "captured-window"})
    assert backend.activate(123)
    backend._call.assert_called_once_with("bind", window_id=123)
    backend.begin_call()
    assert backend.target is None and not backend.observation_only


def test_native_error_keeps_effect_and_does_not_retry():
    backend = MacOSBackend(executable="unused")
    backend.session = "session"
    backend.next_id = 1
    backend.process = SimpleNamespace(stdin=MagicMock())
    backend.responses.put({"id": 1, "version": 5, "session": "session", "error": {
        "detail": "target changed", "effect": "possible", "next": "observe again"}})
    with pytest.raises(Exception, match="Effect: possible"):
        backend._exchange("cuaDesktop", {})
    assert backend.process.stdin.write.call_count == 1
    assert json.loads(backend.process.stdin.write.call_args.args[0])["session"] == "session"


def test_macos_backend_is_per_child_and_works_when_repl_is_disabled():
    template = ComputerUseRail(repl_enabled=False)
    first, second = template.fork_for_agent(), template.fork_for_agent()
    first.init(MagicMock()); second.init(MagicMock())
    if isinstance(first.desktop, MacOSDesktop):
        assert first.desktop is not second.desktop
        assert first.desktop._backend is None and second.desktop._backend is None
        launch = next(tool for tool in first.tools if tool.card.name == "launch_app")
        assert "macOS" in launch.card.description
    assert len(first.tools) == 17
