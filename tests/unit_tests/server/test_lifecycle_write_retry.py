"""Lifecycle writes must ride out transient Windows sharing violations too.

``atomic_json`` publishes via tmp+rename; on Windows ``os.replace`` fails with
PermissionError while any unlocked handle still holds the target open —
CPython opens files without FILE_SHARE_DELETE, so even a concurrent
``read_json`` in the middle of its first attempt blocks the rename.  Batch
lifecycle operations (e.g. cron job delete sweeping hundreds of session
files) hit this regularly; one failed rename used to fail the whole RPC
even though the background finalizer would have converged.
"""

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc


class _FlakyReplace:
    """os.replace stand-in failing its first N calls, then renaming for real."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0

    def __call__(self, src: Path, dst: Path) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise PermissionError(5, "Permission denied")
        Path(src).rename(dst)


def test_atomic_json_writes_payload(tmp_path: Path) -> None:
    target = tmp_path / "session_x.json"
    lc.atomic_json(target, {"session_id": "x"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"session_id": "x"}


def test_atomic_json_rides_out_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc.time, "sleep", lambda _seconds: None)
    flaky = _FlakyReplace(failures=3)
    monkeypatch.setattr(lc.os, "replace", flaky)
    target = tmp_path / "session_y.json"
    lc.atomic_json(target, {"session_id": "y"})  # must not raise
    assert json.loads(target.read_text(encoding="utf-8")) == {"session_id": "y"}
    assert flaky.calls == 4
    # no leftover tmp files
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_atomic_json_raises_after_exhausting_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc.time, "sleep", lambda _seconds: None)
    flaky = _FlakyReplace(failures=lc._SHARING_RETRY_ATTEMPTS + 50)
    monkeypatch.setattr(lc.os, "replace", flaky)
    target = tmp_path / "session_z.json"
    with pytest.raises(PermissionError):
        lc.atomic_json(target, {"session_id": "z"})
    assert flaky.calls == lc._SHARING_RETRY_ATTEMPTS
    # the finally-unlink still cleaned up the tmp file
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_atomic_json_overwrites_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "session_v.json"
    lc.atomic_json(target, {"revision": 1})
    lc.atomic_json(target, {"revision": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"revision": 2}
