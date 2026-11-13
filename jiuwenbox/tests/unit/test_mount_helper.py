"""Checkout mounts have to create paths on a read-only sandbox root."""

from __future__ import annotations

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
