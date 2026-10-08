"""Unit tests for bind_mounts host_path existence check.

Verifies that ``_apply_filesystem`` skips bind_mounts entries whose
``host_path`` does not exist on the host, aligning with the graceful
handling already present in ``_apply_bind_root_entries``.
"""

from __future__ import annotations

from jiuwenbox.models.policy import BindMount, FilesystemPolicy
from jiuwenbox.supervisor.bwrap import BwrapConfig


def _make_fs(bind_mounts: list[BindMount]) -> FilesystemPolicy:
    return FilesystemPolicy(
        read_only=[],
        read_write=[],
        bind_mounts=bind_mounts,
        bind_root_entries=[],
        device=[],
    )


def test_existing_host_path_is_bound():
    """A bind_mount whose host_path exists should be added to ro_binds/rw_binds."""
    fs = _make_fs([
        BindMount(host_path="/etc/hostname", sandbox_path="/etc/hostname", mode="ro"),
    ])
    cfg = BwrapConfig(command=["/bin/sh"])
    BwrapConfig._apply_filesystem(cfg, fs)
    assert ("/etc/hostname", "/etc/hostname") in cfg.ro_binds


def test_missing_host_path_is_skipped():
    """A bind_mount whose host_path does not exist should be skipped, not crash."""
    fs = _make_fs([
        BindMount(host_path="/nonexistent/path/abc123", sandbox_path="/mnt/test", mode="ro"),
    ])
    cfg = BwrapConfig(command=["/bin/sh"])
    BwrapConfig._apply_filesystem(cfg, fs)
    assert len(cfg.ro_binds) == 0
    assert len(cfg.rw_binds) == 0


def test_missing_rw_host_path_is_skipped():
    """A rw bind_mount whose host_path does not exist should also be skipped."""
    fs = _make_fs([
        BindMount(host_path="/nonexistent/path/xyz789", sandbox_path="/mnt/rw", mode="rw"),
    ])
    cfg = BwrapConfig(command=["/bin/sh"])
    BwrapConfig._apply_filesystem(cfg, fs)
    assert len(cfg.ro_binds) == 0
    assert len(cfg.rw_binds) == 0


def test_mixed_existing_and_missing_paths():
    """Only existing host_paths should be bound; missing ones skipped."""
    fs = _make_fs([
        BindMount(host_path="/etc/hostname", sandbox_path="/etc/hostname", mode="ro"),
        BindMount(host_path="/nonexistent/missing", sandbox_path="/mnt/missing", mode="ro"),
        BindMount(host_path="/etc/hosts", sandbox_path="/etc/hosts", mode="rw"),
    ])
    cfg = BwrapConfig(command=["/bin/sh"])
    BwrapConfig._apply_filesystem(cfg, fs)
    assert ("/etc/hostname", "/etc/hostname") in cfg.ro_binds
    assert ("/etc/hosts", "/etc/hosts") in cfg.rw_binds
    assert ("/nonexistent/missing", "/mnt/missing") not in cfg.ro_binds
    assert len(cfg.ro_binds) == 1
    assert len(cfg.rw_binds) == 1
