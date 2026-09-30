"""Unit tests for :func:`lifecycle.read_json` transient-failure retry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import lifecycle


def _patch_read_text(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[BaseException | str]
) -> list[Path]:
    """Replace ``Path.read_text`` with a scripted sequence of outcomes.

    Each entry is either an exception instance to raise or a string to return.
    Returns the list of paths the mocked calls received.
    """
    calls: list[Path] = []

    def fake_read_text(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        calls.append(self)
        outcome = outcomes[len(calls) - 1] if len(calls) <= len(outcomes) else outcomes[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    return calls


def _materialized_metadata(tmp_path: Path) -> Path:
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"revision": 1}), encoding="utf-8")
    return path


def test_read_json_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    # Windows surfaces a sharing violation (during a concurrent tmp+rename
    # atomic write) as PermissionError for the very first read attempt.
    path = _materialized_metadata(tmp_path)
    payload = json.dumps({"revision": 7})
    calls = _patch_read_text(monkeypatch, [PermissionError("temporary lock"), payload])
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    value = lifecycle.read_json(path)

    assert value == {"revision": 7}
    assert len(calls) == 2


def test_read_json_succeeds_on_final_retry_attempt(tmp_path, monkeypatch) -> None:
    # Guards the retry-termination boundary: failing every attempt but the
    # last one must still return the parsed payload.
    path = _materialized_metadata(tmp_path)
    payload = json.dumps({"revision": 9})
    outcomes: list[BaseException | str] = [
        PermissionError("lock") for _ in range(lifecycle._READ_RETRY_ATTEMPTS - 1)
    ]
    outcomes.append(payload)
    calls = _patch_read_text(monkeypatch, outcomes)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    value = lifecycle.read_json(path)

    assert value == {"revision": 9}
    assert len(calls) == lifecycle._READ_RETRY_ATTEMPTS


def test_read_json_raises_persistent_permission_error(tmp_path, monkeypatch) -> None:
    path = _materialized_metadata(tmp_path)
    calls = _patch_read_text(
        monkeypatch, [PermissionError("denied")] * lifecycle._READ_RETRY_ATTEMPTS
    )
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        lifecycle.read_json(path)

    assert len(calls) == lifecycle._READ_RETRY_ATTEMPTS


def test_read_json_does_not_swallow_json_errors(tmp_path, monkeypatch) -> None:
    path = _materialized_metadata(tmp_path)
    calls = _patch_read_text(monkeypatch, ["{not json"])

    with pytest.raises(json.JSONDecodeError):
        lifecycle.read_json(path)

    assert len(calls) == 1


def test_read_json_missing_file_returns_empty(tmp_path) -> None:
    assert lifecycle.read_json(tmp_path / "absent.json") == {}
