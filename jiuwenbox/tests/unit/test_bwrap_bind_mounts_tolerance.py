# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for cross-distro bind_mounts fault tolerance.

Pins the fix for the bug where a ``bind_mounts`` entry whose ``host_path``
does not exist on the host (e.g. ``/lib64`` on Debian-style arm64 layouts
when the default policy was derived from an x86_64 layout) caused bwrap to
fail outright with ``Can't find source path ...`` and drag the whole sandbox
down (``state=error`` → subsequent ops 409 Conflict).

``_apply_filesystem`` now skips missing ``host_path`` entries with a warning,
mirroring the existing fault-tolerance philosophy already in
``_apply_bind_root_entries``. These tests exercise the policy→config path
(:meth:`BwrapConfig._apply_filesystem`) directly so they run on any host
without a sandbox runtime.
"""

from __future__ import annotations

import logging
import os

from jiuwenbox.models.policy import BindMount, FilesystemPolicy
from jiuwenbox.models.policy import _expand_path
from jiuwenbox.supervisor.bwrap import BwrapConfig

_LOGGER_NAME = "jiuwenbox.supervisor.bwrap"


def _apply(bind_mounts: list[BindMount]) -> BwrapConfig:
    cfg = BwrapConfig(command=["/bin/sh"])
    fs = FilesystemPolicy(bind_mounts=bind_mounts)
    BwrapConfig._apply_filesystem(cfg, fs)
    return cfg


def _warned_about(caplog, missing: str) -> bool:
    """True if caplog saw a ``does not exist`` warning naming ``missing``.

    bwrap formats ``host_path`` with ``%r`` (repr), which escapes backslashes
    on Windows -- so match the path's basename (no separators to escape)
    rather than the raw path string.
    """
    token = os.path.basename(missing)
    return any(
        "does not exist" in rec.message and token in rec.message
        for rec in caplog.records
    )


def test_missing_ro_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-ro")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [BindMount(host_path=missing, sandbox_path="/lib64", mode="ro")]
        )

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert _warned_about(caplog, missing)


def test_missing_rw_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-rw")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=missing, sandbox_path="/data", mode="rw")])

    assert cfg.rw_binds == []
    assert cfg.ro_binds == []
    assert _warned_about(caplog, missing)


def test_present_host_path_still_bound(tmp_path):
    present = tmp_path / "lib64"
    present.mkdir()
    sandbox = "/lib64"
    cfg = _apply(
        [BindMount(host_path=str(present), sandbox_path=sandbox, mode="ro")]
    )

    assert cfg.ro_binds == [(str(present), _expand_path(sandbox))]
    assert cfg.rw_binds == []


def test_missing_entries_skipped_but_present_entries_kept(tmp_path, caplog):
    """A single missing entry must not drag down the rest of the list."""
    present_ro = tmp_path / "bin"
    present_ro.mkdir()
    present_rw = tmp_path / "work"
    present_rw.mkdir()
    missing = str(tmp_path / "no-such-lib")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [
                BindMount(host_path=str(present_ro), sandbox_path="/bin", mode="ro"),
                BindMount(host_path=missing, sandbox_path="/lib64", mode="ro"),
                BindMount(host_path=str(present_rw), sandbox_path="/work", mode="rw"),
            ]
        )

    assert cfg.ro_binds == [(str(present_ro), _expand_path("/bin"))]
    assert cfg.rw_binds == [(str(present_rw), _expand_path("/work"))]
    assert _warned_about(caplog, missing)


def test_warning_emitted_at_warning_level(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    missing = str(tmp_path / "absent")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _apply([BindMount(host_path=missing, sandbox_path="/lib64", mode="ro")])

    assert any(
        rec.levelno == logging.WARNING and rec.name == _LOGGER_NAME
        for rec in caplog.records
    )
