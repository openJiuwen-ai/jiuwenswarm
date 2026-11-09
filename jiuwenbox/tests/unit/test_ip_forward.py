"""IPv4 forwarding setup must not require a writable /proc/sys when already on."""

from __future__ import annotations

import errno
import io

import pytest

from jiuwenbox.supervisor import network


@pytest.fixture(autouse=True)
def _reset_proc_sys_state():
    network.reset_proc_sys_mount_state()
    yield
    network.reset_proc_sys_mount_state()


def test_already_enabled_does_not_write(monkeypatch):
    opened: list[str] = []

    def _open(path, mode="r", *args, **kwargs):
        opened.append(mode)
        assert path == network.ip_forward_path()
        if "w" in mode:
            raise AssertionError("write should be skipped when forwarding is already on")
        return io.StringIO("1\n")

    monkeypatch.setattr("builtins.open", _open)
    network.ensure_ip_forward()
    assert opened == ["r"]


def test_disabled_writes_without_remount(monkeypatch):
    modes: list[str] = []

    def _open(path, mode="r", *args, **kwargs):
        modes.append(mode)
        if "w" in mode:
            return io.StringIO()
        return io.StringIO("0\n")

    def _remount():
        raise AssertionError("remount is only for a read-only /proc/sys")

    monkeypatch.setattr("builtins.open", _open)
    monkeypatch.setattr(network, "_remount_proc_sys_rw", _remount)
    network.ensure_ip_forward()
    assert modes == ["r", "w"]


def test_read_only_proc_sys_remounts_once_then_writes(monkeypatch):
    writes = {"n": 0}
    remounts = {"n": 0}

    def _open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            writes["n"] += 1
            if writes["n"] == 1:
                raise OSError(errno.EROFS, "Read-only file system")
            return io.StringIO()
        return io.StringIO("0\n")

    def _remount():
        remounts["n"] += 1

    monkeypatch.setattr("builtins.open", _open)
    monkeypatch.setattr(network, "_remount_proc_sys_rw", _remount)
    network.ensure_ip_forward()
    assert writes["n"] == 2
    assert remounts["n"] == 1


def test_permission_error_does_not_remount(monkeypatch):
    def _open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError(errno.EPERM, "Operation not permitted")
        return io.StringIO("0\n")

    def _remount():
        raise AssertionError("EPERM is missing capability, not a read-only mount")

    monkeypatch.setattr("builtins.open", _open)
    monkeypatch.setattr(network, "_remount_proc_sys_rw", _remount)
    with pytest.raises(network.NetworkSetupError):
        network.ensure_ip_forward()
