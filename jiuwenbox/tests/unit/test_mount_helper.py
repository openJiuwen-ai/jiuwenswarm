"""Checkout mounts have to create paths on a read-only sandbox root."""

from __future__ import annotations

import errno

import pytest

from jiuwenbox.supervisor import mount_helper


def _helper(name: str):
    """Return an internal mount helper without a protected-member access."""
    return getattr(mount_helper, name)


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


def test_make_private_treats_missing_syscall_as_unavailable(monkeypatch):
    monkeypatch.setattr(mount_helper, "_syscall_long", lambda *_args: -1)
    monkeypatch.setattr(mount_helper.ctypes, "get_errno", lambda: errno.ENOSYS)

    assert _helper("_make_private")(3) is False


def test_make_private_treats_raw_enosys_return_as_unavailable(monkeypatch):
    monkeypatch.setattr(mount_helper, "_syscall_long", lambda *_args: -errno.ENOSYS)
    monkeypatch.setattr(mount_helper.ctypes, "get_errno", lambda: 0)

    assert _helper("_make_private")(3) is False


def test_make_private_raises_when_the_syscall_fails_for_another_reason(monkeypatch):
    monkeypatch.setattr(mount_helper, "_syscall_long", lambda *_args: -1)
    monkeypatch.setattr(mount_helper.ctypes, "get_errno", lambda: errno.EINVAL)

    with pytest.raises(OSError) as raised:
        _helper("_make_private")(3)

    assert raised.value.errno == errno.EINVAL


def test_collect_clones_keeps_mount_setattr_result(monkeypatch):
    monkeypatch.setattr(mount_helper, "_open_tree", lambda _src: 5)
    monkeypatch.setattr(mount_helper, "_make_private", lambda _fd: True)
    monkeypatch.setattr(mount_helper.os.path, "isdir", lambda _src: True)
    monkeypatch.setattr(
        mount_helper,
        "_bind_private_clones",
        lambda _mounts: (_ for _ in ()).throw(AssertionError("fallback used")),
    )

    prepared, propagation = _helper("_collect_clones")([
        {"src": "/bin", "dst": "/bin", "ro": True},
    ])

    assert propagation == "mount_setattr"
    assert prepared == [(5, "/bin", True, True)]


def test_collect_clones_drops_shared_clone_when_setattr_is_missing(monkeypatch):
    closed: list[int] = []
    monkeypatch.setattr(mount_helper, "_open_tree", lambda _src: 42)
    monkeypatch.setattr(mount_helper, "_make_private", lambda _fd: False)
    monkeypatch.setattr(mount_helper.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(
        mount_helper,
        "_bind_private_clones",
        lambda mounts: [(7, "/ext", True, True)] if mounts else [],
    )

    prepared, propagation = _helper("_collect_clones")([
        {"src": "/ext", "dst": "/ext", "ro": True},
    ])

    assert propagation == "private-bind"
    assert prepared == [(7, "/ext", True, True)]
    assert closed == [42]


def test_bind_private_clones_makes_the_temporary_bind_private(monkeypatch, tmp_path):
    calls: list[tuple] = []
    work = tmp_path / "work"

    monkeypatch.setattr(mount_helper, "_isolate_mount_namespace", lambda: calls.append("isolate"))
    monkeypatch.setattr(mount_helper.tempfile, "mkdtemp", lambda prefix: str(work))
    work.mkdir()
    monkeypatch.setattr(mount_helper.os.path, "isdir", lambda _src: True)
    monkeypatch.setattr(mount_helper.shutil, "rmtree", lambda *_args, **_kwargs: None)

    def open_tree(src: str) -> int:
        calls.append(("open", src))
        return 9

    monkeypatch.setattr(mount_helper, "_open_tree", open_tree)

    def mount(src, target, _fstype, flags, _data):
        calls.append((
            "mount",
            None if src is None else src.decode(),
            target.decode(),
            flags,
        ))
        return 0

    def umount(target, flags):
        calls.append(("umount", target.decode(), flags))
        return 0

    libc = _helper("_libc")
    monkeypatch.setattr(libc, "mount", mount)
    monkeypatch.setattr(libc, "umount2", umount)

    prepared = _helper("_bind_private_clones")([
        {"src": "/usr/lib/ext", "dst": "/usr/lib/ext", "ro": True},
    ])

    assert prepared == [(9, "/usr/lib/ext", True, True)]
    assert calls[0] == "isolate"
    assert calls[1][0] == "mount"
    assert calls[1][3] == mount_helper.MS_BIND
    assert calls[1][1] == "/usr/lib/ext"
    assert calls[2] == (
        "mount",
        None,
        calls[1][2],
        mount_helper.MS_PRIVATE,
    )
    assert calls[3][0] == "open"
    assert calls[3][1] == calls[1][2]
    assert calls[4][0] == "umount"
