"""win_proxy 端口清理: 一次查询整个端口段, 只结束沙箱进程."""

import subprocess
import sys

from jiuwenswarm.server.sandbox.jiuwenbox_runner import (
    _cleanup_stale_win_proxy_ports,
    _parse_win_proxy_listener_output,
    _win_proxy_listener_command,
)


def test_listener_command_covers_the_port_range_once() -> None:
    command = _win_proxy_listener_command(60080, 60089)
    assert "60080..60089" in command
    assert command.count("Get-NetTCPConnection") == 1


def test_parse_listener_output_groups_ports_by_pid() -> None:
    text = (
        "4242|python|60080,60081\n"
        "noise\n"
        "7|svchost|60082\n"
        "not-a-pid|python|60083\n"
        "99|jiuwenswarm|"
    )
    assert _parse_win_proxy_listener_output(text) == {
        4242: ("python", [60080, 60081]),
        7: ("svchost", [60082]),
    }


def test_cleanup_queries_once_and_stops_only_sandbox_pids(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[str] = []

    def fake_run(argv, **kwargs):
        command = argv[-1]
        calls.append(command)
        completed = subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if "Get-NetTCPConnection" in command:
            completed.stdout = b"4242|python|60080\n7|svchost|60081\n"
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "jiuwenswarm.server.sandbox.jiuwenbox_runner._get_powershell",
        lambda: "powershell",
    )
    # Keep the test platform-neutral: the real helper touches Windows-only
    # subprocess attributes even when sys.platform is monkeypatched.
    monkeypatch.setattr(
        "jiuwenswarm.server.sandbox.jiuwenbox_runner._win_hidden_kwargs",
        dict,
    )

    _cleanup_stale_win_proxy_ports()

    assert len(calls) == 2
    assert "Get-NetTCPConnection" in calls[0]
    assert "60080..60089" in calls[0]
    assert calls[1] == "Stop-Process -Id 4242 -Force"
