"""Regression tests for /rewind file restore line-ending preservation (issue #2244)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files


def test_restore_session_files_preserves_crlf_bytes(tmp_path: Path) -> None:
    """CRLF old_content must be written back verbatim, not translated to CRCRLF."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"MODIFIED-A\r\nMODIFIED-B\r\n")

    original_crlf = "ORIGINAL-A\r\nORIGINAL-B\r\n"
    files_to_restore = {
        str(target): {
            "restore_content": original_crlf,
            "action": "write",
        }
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_restore.return_value = files_to_restore

    with patch(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        return_value=mock_diff,
    ):
        result = restore_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    assert str(target) in result["restored_files"]
    assert target.read_bytes() == original_crlf.encode("utf-8")
    assert b"\r\r\n" not in target.read_bytes()


def test_restore_session_files_preserves_lf_bytes(tmp_path: Path) -> None:
    """LF old_content must stay LF (not rewritten to platform linesep)."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"MODIFIED-A\nMODIFIED-B\n")

    original_lf = "ORIGINAL-A\nORIGINAL-B\n"
    files_to_restore = {
        str(target): {
            "restore_content": original_lf,
            "action": "write",
        }
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_restore.return_value = files_to_restore

    with patch(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        return_value=mock_diff,
    ):
        result = restore_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    assert target.read_bytes() == original_lf.encode("utf-8")
