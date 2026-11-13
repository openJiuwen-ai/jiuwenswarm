"""A child-free procfs is mounted once so nested user+pid namespaces can mount proc."""

from __future__ import annotations

import errno

import pytest

from jiuwenbox.supervisor import visible_proc


@pytest.fixture(autouse=True)
def _reset():
    visible_proc.reset_visible_proc_state()
    yield
    visible_proc.reset_visible_proc_state()


def test_mounts_once(monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    mountpoint = parent / "mnt"
    monkeypatch.setattr(visible_proc, "_PARENT", str(parent))
    monkeypatch.setattr(visible_proc, "_MOUNTPOINT", str(mountpoint))
    calls: list[tuple] = []

    def _mount(source, target, fstype, flags):
        calls.append((source, target, fstype, flags))

    monkeypatch.setattr(visible_proc, "_mount", _mount)
    monkeypatch.setattr(visible_proc.os.path, "ismount", lambda path: False)
    visible_proc.ensure_visible_proc()
    visible_proc.ensure_visible_proc()
    assert calls == [
        (None, str(parent), None, visible_proc.MS_REC | visible_proc.MS_PRIVATE),
        ("proc", str(mountpoint), "proc", 0),
        (None, str(mountpoint), None, visible_proc.MS_PRIVATE),
    ]
    assert (parent.stat().st_mode & 0o777) == 0o700


def test_busy_mountpoint_is_ready(monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    mountpoint = parent / "mnt"
    monkeypatch.setattr(visible_proc, "_PARENT", str(parent))
    monkeypatch.setattr(visible_proc, "_MOUNTPOINT", str(mountpoint))

    mounted = False

    def _mount(source, target, fstype, flags):
        nonlocal mounted
        if source == "proc":
            mounted = True
            raise OSError(errno.EBUSY, "busy", target)

    monkeypatch.setattr(visible_proc, "_mount", _mount)
    monkeypatch.setattr(
        visible_proc.os.path,
        "ismount",
        lambda path: mounted and path == str(mountpoint),
    )
    visible_proc.ensure_visible_proc()
    assert visible_proc.visible_proc_is_ready() is True


def test_permission_error_does_not_raise(monkeypatch, tmp_path):
    parent = tmp_path / "parent"
    mountpoint = parent / "mnt"
    monkeypatch.setattr(visible_proc, "_PARENT", str(parent))
    monkeypatch.setattr(visible_proc, "_MOUNTPOINT", str(mountpoint))

    def _mount(source, target, fstype, flags):
        if source == "proc":
            raise OSError(errno.EPERM, "denied", target)

    monkeypatch.setattr(visible_proc, "_mount", _mount)
    monkeypatch.setattr(visible_proc.os.path, "ismount", lambda path: False)
    visible_proc.ensure_visible_proc()
    assert visible_proc.visible_proc_is_ready() is False
