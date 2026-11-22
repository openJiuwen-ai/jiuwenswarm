"""Admission-path lifecycle reads must ride out transient Errno 13 windows.

Atomic writers publish via tmp+rename; on Windows a reader landing in the
replacement window receives PermissionError (issue #4923). ``read_json`` sits
on every request's admission path, so it retries instead of failing requests.
"""

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc


class _FlakyPath:
    """Path stand-in raising PermissionError for its first N reads."""

    def __init__(self, payload: str, failures: int) -> None:
        self._payload = payload
        self._failures = failures
        self.reads = 0

    def exists(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        self.reads += 1
        if self.reads <= self._failures:
            raise PermissionError(13, "Permission denied")
        return self._payload


def test_read_json_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert lc.read_json(tmp_path / "absent.json") == {}


def test_read_json_parses_dict_payload(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    assert lc.read_json(target) == {"session_id": "s1"}


def test_read_json_rejects_non_dict_payload(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(lc.LifecycleError):
        lc.read_json(target)


def test_read_json_rides_out_transient_sharing_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lc.sys, "platform", "win32")
    flaky = _FlakyPath(json.dumps({"session_id": "s1"}), failures=3)
    assert lc.read_json(flaky) == {"session_id": "s1"}
    assert flaky.reads == 4


def test_read_json_raises_after_exhausting_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lc.sys, "platform", "win32")
    monkeypatch.setattr(lc.time, "sleep", lambda _seconds: None)
    flaky = _FlakyPath("{}", failures=lc._SHARING_RETRY_ATTEMPTS + 50)
    with pytest.raises(PermissionError):
        lc.read_json(flaky)
    assert flaky.reads == lc._SHARING_RETRY_ATTEMPTS


def test_read_json_does_not_retry_permission_error_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sharing retry is Windows-only: a POSIX denial is not transient."""
    monkeypatch.setattr(lc.sys, "platform", "linux")
    flaky = _FlakyPath(json.dumps({"session_id": "s1"}), failures=1)
    with pytest.raises(PermissionError):
        lc.read_json(flaky)
    assert flaky.reads == 1
