"""Checkout mounts have to create paths on a read-only sandbox root."""

from __future__ import annotations

import ctypes
import errno

import pytest

from jiuwenbox.supervisor import mount_helper


def test_writable_root_is_restored_after_checkout_mounts(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(mount_helper, "_root_is_readonly", lambda: True)
    monkeypatch.setattr(
        mount_helper,
        "_remount_root",
        lambda read_only: calls.append(("remount", read_only)),
    )

    def action() -> None:
        calls.append(("create", "/home"))

    mount_helper.with_writable_root(action)

    assert calls == [("remount", False), ("create", "/home"), ("remount", True)]


def test_writable_root_stays_writable_when_checkout_did_not_need_it(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(mount_helper, "_root_is_readonly", lambda: False)
    monkeypatch.setattr(
        mount_helper,
        "_remount_root",
        lambda read_only: calls.append(read_only),
    )

    mount_helper.with_writable_root(lambda: None)

    assert calls == []


def test_readonly_root_is_restored_when_creating_a_mount_point_fails(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(mount_helper, "_root_is_readonly", lambda: True)
    monkeypatch.setattr(
        mount_helper,
        "_remount_root",
        lambda read_only: calls.append(read_only),
    )

    def action() -> None:
        raise OSError(30, "Read-only file system", "/home")

    with pytest.raises(OSError, match="Read-only"):
        mount_helper.with_writable_root(action)

    assert calls == [False, True]


def test_checkout_clone_is_private_before_attaching(monkeypatch):
    calls = []

    def syscall(*args):
        calls.append(args)
        if args[0] == mount_helper.SYS_OPEN_TREE:
            return 42
        assert args[:2] == (mount_helper.SYS_MOUNT_SETATTR, 42)
        assert args[3] == mount_helper.AT_EMPTY_PATH
        assert ctypes.string_at(args[2]) == b""
        attr = mount_helper._MountAttr.from_address(args[4])
        assert (attr.attr_set, attr.attr_clr, attr.propagation, attr.userns_fd) == (
            0, 0, mount_helper.MS_PRIVATE, 0,
        )
        assert args[5] == ctypes.sizeof(attr)
        return 0

    monkeypatch.setattr(mount_helper, "_syscall_long", syscall)
    assert mount_helper._open_tree("/source/python") == 42
    assert [call[0] for call in calls] == [mount_helper.SYS_OPEN_TREE, mount_helper.SYS_MOUNT_SETATTR]


def test_checkout_rejects_shared_clone_and_closes_fd_if_privatizing_fails(monkeypatch):
    calls = iter((42, -1))
    closed = []
    monkeypatch.setattr(mount_helper, "_syscall_long", lambda *args: next(calls))
    monkeypatch.setattr(mount_helper.ctypes, "get_errno", lambda: errno.ENOSYS)
    monkeypatch.setattr(mount_helper.os, "close", closed.append)

    with pytest.raises(OSError, match="mount_setattr private /source/python") as error:
        mount_helper._open_tree("/source/python")

    assert error.value.errno == errno.ENOSYS
    assert closed == [42]
