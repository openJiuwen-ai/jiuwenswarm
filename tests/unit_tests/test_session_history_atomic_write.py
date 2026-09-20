# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime.session import session_history


@pytest.mark.parametrize(
    ("filename", "records", "expected"),
    [
        ("history.jsonl", [{"content": "中文"}, {"id": 2}], '{"content": "中文"}\n{"id": 2}\n'),
        ("history.json", [{"content": "中文"}], '[\n  {\n    "content": "中文"\n  }\n]'),
        ("history.jsonl", [], ""),
        ("history.json", [], "[]"),
    ],
)
def test_atomic_write_preserves_format_and_cleans_temporary_files(
    tmp_path, monkeypatch, filename, records, expected
):
    path = tmp_path / filename
    original = b"original history"
    expected_bytes = expected.replace("\n", os.linesep).encode("utf-8")
    path.write_bytes(original)
    replace = os.replace
    replaced = []

    def check_replace(source, destination):
        assert destination == path
        assert path.read_bytes() == original
        assert source.read_bytes() == expected_bytes
        assert source.stat().st_dev == path.parent.stat().st_dev
        replace(source, destination)
        replaced.append(destination)

    monkeypatch.setattr(session_history.os, "replace", check_replace)

    session_history._write_records_to_path(path, records)

    assert replaced == [path]
    assert path.read_bytes() == expected_bytes
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("failure_stage", ["open", "write", "fsync", "replace"])
def test_atomic_write_failure_preserves_history_and_cleans_temporary_files(
    tmp_path, monkeypatch, failure_stage
):
    path = tmp_path / "history.jsonl"
    original = b"original history"
    path.write_bytes(original)
    original_open = Path.open
    opened_files = []

    @contextmanager
    def failing_open(temporary_path, *args, **kwargs):
        if failure_stage == "open":
            raise OSError("injected open failure")
        with original_open(temporary_path, *args, **kwargs) as fh:
            opened_files.append(fh)
            if failure_stage == "write":
                writer = Mock(wraps=fh)
                writer.write.side_effect = OSError("injected write failure")
                yield writer
            else:
                yield fh

    def fail_operation(*args):
        raise OSError(f"injected {failure_stage} failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", failing_open)
        if failure_stage in {"fsync", "replace"}:
            patch.setattr(session_history.os, failure_stage, fail_operation)

        with pytest.raises(OSError, match=f"injected {failure_stage} failure"):
            session_history._write_records_to_path(path, [{"content": "updated"}])

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert len(opened_files) == (failure_stage != "open")
    assert all(fh.closed for fh in opened_files)
