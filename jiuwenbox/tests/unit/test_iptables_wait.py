"""iptables invocations wait for the xtables lock."""
# pylint: disable=protected-access

from __future__ import annotations

import subprocess

from jiuwenbox.supervisor import network as network_mod


def _completed() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_iptables_binary_waits_by_default(monkeypatch):
    monkeypatch.delenv(network_mod.ENV_IPTABLES_WAIT_SECONDS, raising=False)
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen.append(cmd)
        return _completed()

    monkeypatch.setattr(network_mod.subprocess, "run", fake_run)
    network_mod._run_iptables_binary("iptables", ["-L", "OUTPUT", "-n"])
    assert seen == [["iptables", "-w", "5", "-L", "OUTPUT", "-n"]]


def test_iptables_binary_wait_comes_from_env(monkeypatch):
    monkeypatch.setenv(network_mod.ENV_IPTABLES_WAIT_SECONDS, "12")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen.append(cmd)
        return _completed()

    monkeypatch.setattr(network_mod.subprocess, "run", fake_run)
    network_mod._run_iptables_binary(
        "iptables",
        ["-D", "FORWARD", "-j", "ACCEPT"],
        namespace="jbx-sb",
    )
    assert seen == [[
        "ip", "netns", "exec", "jbx-sb",
        "iptables", "-w", "12", "-D", "FORWARD", "-j", "ACCEPT",
    ]]


def test_iptables_restore_waits_and_keeps_noflush(monkeypatch):
    monkeypatch.setenv(network_mod.ENV_IPTABLES_WAIT_SECONDS, "3")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen.append(cmd)
        return _completed()

    monkeypatch.setattr(network_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(network_mod, "_iptables_restore_binary", lambda ip_version: "iptables-restore")
    network_mod._run_iptables_restore(
        "*filter\n-I INPUT -j ACCEPT\nCOMMIT\n",
        ip_version=4,
        namespace=None,
        noflush=True,
    )
    assert seen == [["iptables-restore", "--wait", "3", "--noflush"]]


def test_invalid_wait_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv(network_mod.ENV_IPTABLES_WAIT_SECONDS, "forever")
    assert network_mod._iptables_wait_seconds() == network_mod.DEFAULT_IPTABLES_WAIT_SECONDS
    assert "forever" in caplog.text
