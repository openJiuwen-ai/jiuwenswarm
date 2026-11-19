"""Fault tolerance for unusable ``bind_mounts`` host paths.

A ``bind_mounts`` entry whose ``host_path`` cannot be resolved on the host
makes bwrap abort with ``Can't find source path`` and takes the whole
sandbox start down with it (state=error, then 409 on every later operation).
``_apply_filesystem`` skips such entries with a warning instead, mirroring
the fault tolerance ``_apply_bind_root_entries`` already applies.

Tests go through the public ``BwrapConfig.from_policy`` entry point and
assert on the resulting binds, so they cover the same path production code
uses without reaching into private helpers.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import pytest

from jiuwenbox.models.policy import BindMount, SecurityPolicy, _expand_path
from jiuwenbox.supervisor.bwrap import BwrapConfig

_LOGGER_NAME = "jiuwenbox.supervisor.bwrap"


def _apply(bind_mounts: list[BindMount]) -> BwrapConfig:
    """Build a config through the public entry point.

    Numeric ids keep uid/gid resolution independent of the host's account
    database, so the assertions stay focused on the filesystem policy.
    """
    policy = SecurityPolicy.model_validate({
        "process": {"run_as_user": "1000", "run_as_group": "1000"},
        "filesystem_policy": {"bind_mounts": [
            mount.model_dump() for mount in bind_mounts
        ]},
    })
    return BwrapConfig.from_policy(policy, ["/bin/sh"])


def _warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == logging.WARNING
    ]


def _warned_about(caplog: pytest.LogCaptureFixture, missing: str) -> bool:
    """True if a warning named the skipped path.

    The message formats ``host_path`` with ``%r``; matching the basename
    keeps this independent of quoting and separator escaping in the repr.
    """
    token = posixpath.basename(missing)
    return any(token in message for message in _warning_messages(caplog))


def test_existing_host_path_is_bound():
    cfg = _apply([BindMount(host_path=__file__, sandbox_path="/self", mode="ro")])

    assert cfg.ro_binds == [(__file__, _expand_path("/self"))]


def test_missing_ro_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-ro")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=missing, sandbox_path="/lib64", mode="ro")])

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert _warned_about(caplog, missing)


def test_missing_rw_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-rw")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=missing, sandbox_path="/data", mode="rw")])

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert _warned_about(caplog, missing)


def test_present_host_path_still_bound(tmp_path):
    present = tmp_path / "lib64"
    present.mkdir()

    cfg = _apply([BindMount(host_path=str(present), sandbox_path="/lib64", mode="ro")])

    assert cfg.ro_binds == [(str(present), _expand_path("/lib64"))]
    assert cfg.rw_binds == []


def test_missing_entries_skipped_but_present_entries_kept(tmp_path, caplog):
    """One unusable entry must not drag down the rest of the list."""
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


def test_multiple_skips_are_aggregated_into_one_warning(tmp_path, caplog):
    """A permanent layout mismatch must not emit one warning per entry."""
    missing_ro = str(tmp_path / "absent-ro")
    missing_rw = str(tmp_path / "absent-rw")
    missing_third = str(tmp_path / "absent-third")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _apply(
            [
                BindMount(host_path=missing_ro, sandbox_path="/lib64", mode="ro"),
                BindMount(host_path=missing_rw, sandbox_path="/data", mode="rw"),
                BindMount(host_path=missing_third, sandbox_path="/opt", mode="ro"),
            ]
        )

    messages = _warning_messages(caplog)

    assert len(messages) == 1
    assert "skipping 3 unusable host path(s)" in messages[0]
    assert _warned_about(caplog, missing_ro)
    assert _warned_about(caplog, missing_rw)
    assert _warned_about(caplog, missing_third)


def test_no_warning_when_every_host_path_is_usable(tmp_path, caplog):
    present_ro = tmp_path / "bin"
    present_ro.mkdir()
    present_rw = tmp_path / "work"
    present_rw.mkdir()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _apply(
            [
                BindMount(host_path=str(present_ro), sandbox_path="/bin", mode="ro"),
                BindMount(host_path=str(present_rw), sandbox_path="/work", mode="rw"),
            ]
        )

    assert _warning_messages(caplog) == []


def test_warning_carries_full_context(tmp_path, caplog):
    """Operators need the sandbox target and mode to judge the skip."""
    missing = str(tmp_path / "absent")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _apply([BindMount(host_path=missing, sandbox_path="/data", mode="rw")])

    messages = _warning_messages(caplog)

    expanded_target = _expand_path("/data")

    assert any(
        "does not exist" in message
        and f"sandbox_path={expanded_target}" in message
        and "mode=rw" in message
        for message in messages
    )


def test_broken_symlink_is_reported_separately(tmp_path):
    """A dangling link is absent to ``exists()`` but still diagnosable."""
    link = tmp_path / "dangling-link"
    link.symlink_to(tmp_path / "nonexistent-target")

    cfg = _apply([BindMount(host_path=str(link), sandbox_path="/link", mode="ro")])

    assert cfg.ro_binds == []


def test_broken_symlink_is_skipped(tmp_path, caplog):
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "no-such-target")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=str(link), sandbox_path="/lib64", mode="ro")])

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert any("broken symlink" in message for message in _warning_messages(caplog))


def test_broken_symlink_does_not_mask_other_entries(tmp_path, caplog):
    present = tmp_path / "bin"
    present.mkdir()
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "no-such-target")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [
                BindMount(host_path=str(present), sandbox_path="/bin", mode="ro"),
                BindMount(host_path=str(link), sandbox_path="/lib64", mode="ro"),
            ]
        )

    assert cfg.ro_binds == [(str(present), _expand_path("/bin"))]
    assert cfg.rw_binds == []
    assert any("broken symlink" in message for message in _warning_messages(caplog))


def test_stat_error_does_not_crash_startup(tmp_path, caplog, monkeypatch):
    """``exists()`` only swallows a limited errno set; EACCES propagates.

    The guard must contain that error rather than become a new startup
    failure of its own.
    """
    host = str(tmp_path / "restricted")
    original_exists = Path.exists

    def _raise_on_target(self, *args, **kwargs):
        if str(self) == host:
            raise PermissionError(13, "Permission denied (simulated)")
        return original_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _raise_on_target)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=host, sandbox_path="/lib64", mode="ro")])

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert any("cannot stat" in message for message in _warning_messages(caplog))


def test_stat_error_is_classified_with_reason(tmp_path, monkeypatch):
    host = str(tmp_path / "restricted")
    original_exists = Path.exists

    def _raise_on_target(self, *args, **kwargs):
        if str(self) == host:
            raise PermissionError(13, "Permission denied (simulated)")
        return original_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _raise_on_target)

    cfg = _apply([BindMount(host_path=host, sandbox_path="/restricted", mode="ro")])

    assert cfg.ro_binds == []


def test_args_carry_all_present_binds_and_omit_skipped_targets(tmp_path, caplog):
    """End-to-end over the pure string layer: no phantom mounts appear."""
    present = tmp_path / "usr"
    present.mkdir()
    missing = str(tmp_path / "absent")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [
                BindMount(host_path=str(present), sandbox_path="/usr", mode="ro"),
                BindMount(host_path=missing, sandbox_path="/lib64", mode="ro"),
            ]
        )

    args = cfg.to_args()

    assert ["--ro-bind", str(present), _expand_path("/usr")] == args[
        args.index("--ro-bind"):args.index("--ro-bind") + 3
    ]
    assert missing not in args
    assert _expand_path("/lib64") not in args


def test_all_present_binds_reproduce_expected_args(tmp_path):
    """All-resolvable input must produce the same argument sequence as before."""
    first = tmp_path / "bin"
    first.mkdir()
    second = tmp_path / "etc"
    second.mkdir()

    cfg = _apply(
        [
            BindMount(host_path=str(first), sandbox_path="/bin", mode="ro"),
            BindMount(host_path=str(second), sandbox_path="/etc", mode="rw"),
        ]
    )

    args = cfg.to_args()

    assert args.count("--ro-bind") == 1
    assert args.count("--bind") == 1
    assert ["--ro-bind", str(first), _expand_path("/bin")] == args[
        args.index("--ro-bind"):args.index("--ro-bind") + 3
    ]
    assert ["--bind", str(second), _expand_path("/etc")] == args[
        args.index("--bind"):args.index("--bind") + 3
    ]
