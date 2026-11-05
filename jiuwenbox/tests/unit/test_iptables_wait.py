"""Serialize legacy firewall updates through the shared xtables lock."""
import subprocess

import pytest

from jiuwenbox.supervisor import network


@pytest.mark.parametrize("version", [4, 6])
@pytest.mark.parametrize("namespace", [None, "jbx-test"])
def test_iptables_waits_for_lock(monkeypatch, version, namespace):
    binary = "iptables-legacy" if version == 4 else "ip6tables-legacy"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(network, "_select_iptables_binary", lambda *_: binary)
    monkeypatch.setattr(network.subprocess, "run", run)
    network.run_iptables(["-D", "FORWARD", "-j", "ACCEPT"], namespace=namespace, ip_version=version)

    expected = [binary, "-w", "5", "-D", "FORWARD", "-j", "ACCEPT"]
    if namespace:
        expected = ["ip", "netns", "exec", namespace, *expected]
    assert calls == [expected]


@pytest.mark.parametrize("version", [4, 6])
@pytest.mark.parametrize("namespace", [None, "jbx-test"])
@pytest.mark.parametrize("noflush", [False, True])
def test_restore_waits_for_lock(monkeypatch, version, namespace, noflush):
    binary = "iptables-legacy-restore" if version == 4 else "ip6tables-legacy-restore"
    calls = []
    script = "*filter\nCOMMIT\n"

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(network, "_iptables_restore_binary", lambda _: binary)
    monkeypatch.setattr(network.subprocess, "run", run)
    network._run_iptables_restore(script, ip_version=version, namespace=namespace, noflush=noflush)

    expected = [binary, "-w", "5"]
    if noflush:
        expected.append("--noflush")
    if namespace:
        expected = ["ip", "netns", "exec", namespace, *expected]
    assert calls == [(expected, script)]
