"""Unit tests for fork_session, particularly channel_metadata propagation."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_source_meta(sessions_dir: Path, session_id: str, meta: dict) -> None:
    """Write a metadata.json for a source session."""
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _read_target_meta(sessions_dir: Path, session_id: str) -> dict:
    """Read metadata.json for a target session."""
    return json.loads(
        (sessions_dir / session_id / "metadata.json").read_text(encoding="utf-8")
    )


# Patch _enqueue_write to do sync writes during tests
def _sync_enqueue_write():
    """Return a replacement for _enqueue_write that writes synchronously."""
    from jiuwenswarm.server.runtime.session.session_metadata import _write_metadata_sync

    def _replacement(session_id: str, metadata: dict) -> None:
        _write_metadata_sync(session_id, metadata)

    return _replacement


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestForkSessionChannelMetadata:
    """Verify fork_session copies channel_metadata from the source session."""

    @staticmethod
    def _setup(monkeypatch, tmp_path):
        """Common setup for fork_session tests."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        # Ensure writes are synchronous during tests
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata._enqueue_write",
            _sync_enqueue_write(),
        )
        return sessions_dir

    def test_copies_channel_metadata_with_project_dir(self, tmp_path, monkeypatch):
        """fork_session should copy channel_metadata (including project_dir)"""

        sessions_dir = self._setup(monkeypatch, tmp_path)

        source_id = "tui_source_001"
        target_id = "tui_target_001"

        source_meta = {
            "session_id": source_id,
            "channel_id": "tui",
            "user_id": "testuser",
            "created_at": 1700000000.0,
            "last_message_at": 1700000100.0,
            "title": "My Test Session",
            "message_count": 5,
            "mode": "code.normal",
            "channel_metadata": {
                "project_dir": "/Users/test/my-project",
                "cwd": "/Users/test/my-project",
                "git_branch": "main",
            },
        }
        _write_source_meta(sessions_dir, source_id, source_meta)
        (sessions_dir / source_id / "history.jsonl").write_text("", encoding="utf-8")

        with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=[],
        ), patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
            return_value=[],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

            result = fork_session(
                source_session_id=source_id,
                target_session_id=target_id,
                title="",
                channel_id="tui",
            )

        assert result["session_id"] == target_id
        assert result["source_session_id"] == source_id

        target_meta = _read_target_meta(sessions_dir, target_id)
        assert "channel_metadata" in target_meta, (
            "fork_session should copy channel_metadata from source"
        )
        assert target_meta["channel_metadata"]["project_dir"] == "/Users/test/my-project"
        assert target_meta["channel_metadata"]["cwd"] == "/Users/test/my-project"
        assert target_meta["channel_metadata"]["git_branch"] == "main"

    def test_no_channel_metadata_in_source(self, tmp_path, monkeypatch):
        """If source has no channel_metadata, fork_session should not add one."""
        sessions_dir = self._setup(monkeypatch, tmp_path)

        source_id = "tui_no_meta_001"
        target_id = "tui_no_meta_target_001"

        source_meta = {
            "session_id": source_id,
            "channel_id": "tui",
            "user_id": "testuser",
            "created_at": 1700000000.0,
            "last_message_at": 1700000100.0,
            "title": "No Channel Metadata",
            "message_count": 3,
            "mode": "code.normal",
        }
        _write_source_meta(sessions_dir, source_id, source_meta)
        (sessions_dir / source_id / "history.jsonl").write_text("", encoding="utf-8")

        with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=[],
        ), patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
            return_value=[],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

            result = fork_session(
                source_session_id=source_id,
                target_session_id=target_id,
                title="",
                channel_id="tui",
            )

        assert result["session_id"] == target_id
        target_meta = _read_target_meta(sessions_dir, target_id)
        assert "channel_metadata" not in target_meta, (
            "fork_session should not add an empty channel_metadata"
        )

    def test_channel_metadata_is_deep_copied(self, tmp_path, monkeypatch):
        """Verify channel_metadata is a deep copy, not a shared reference."""
        sessions_dir = self._setup(monkeypatch, tmp_path)

        source_id = "tui_deepcopy_001"
        target_id = "tui_deepcopy_target_001"

        source_meta = {
            "session_id": source_id,
            "channel_id": "tui",
            "user_id": "testuser",
            "created_at": 1700000000.0,
            "last_message_at": 1700000100.0,
            "title": "Deep Copy Test",
            "message_count": 2,
            "mode": "code.normal",
            "channel_metadata": {
                "project_dir": "/Users/test/deep-project",
                "custom_field": "custom_value",
            },
        }
        _write_source_meta(sessions_dir, source_id, source_meta)
        (sessions_dir / source_id / "history.jsonl").write_text("", encoding="utf-8")

        with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=[],
        ), patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
            return_value=[],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

            fork_session(
                source_session_id=source_id,
                target_session_id=target_id,
                title="",
                channel_id="tui",
            )

        target_meta = _read_target_meta(sessions_dir, target_id)
        target_meta["channel_metadata"]["project_dir"] = "/modified/path"
        target_meta["channel_metadata"]["custom_field"] = "modified"

        source_meta_reread = json.loads(
            (sessions_dir / source_id / "metadata.json").read_text(encoding="utf-8")
        )
        assert source_meta_reread["channel_metadata"]["project_dir"] == "/Users/test/deep-project"
        assert source_meta_reread["channel_metadata"]["custom_field"] == "custom_value"
