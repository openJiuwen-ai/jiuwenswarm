"""Unit tests for fork_session, particularly channel_metadata propagation."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


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

    def _replacement(session_id: str, metadata: dict, *, sync_write: bool = False) -> None:
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
            "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
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
            "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
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

    def test_fork_copies_equipment_model_and_applies_current_selection(self, tmp_path, monkeypatch):
        sessions_dir = self._setup(monkeypatch, tmp_path)
        _write_source_meta(sessions_dir, "source", {
            "session_id": "source",
            "title": "Configured chat",
            "model": "selected-model",
            "session_equipment": {
                "agent_template_name": "expert-a",
                "plugin_names": ["plugin-a"],
                "mcp": ["connector-a"],
            },
        })

        from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

        fork_session(
            source_session_id="source",
            target_session_id="fork",
            channel_id="web",
            session_equipment_override={
                "agent_template_name": "expert-b",
                "mcp": ["connector-b", "connector-b"],
            },
        )

        target_meta = _read_target_meta(sessions_dir, "fork")
        assert target_meta["model"] == "selected-model"
        assert target_meta["session_equipment"] == {
            "agent_template_name": "expert-b",
            "plugin_names": ["plugin-a"],
            "mcp": ["connector-b"],
        }
        assert _read_target_meta(sessions_dir, "source")["session_equipment"]["agent_template_name"] == "expert-a"

    def test_repeated_forks_get_distinct_branch_titles(self, tmp_path, monkeypatch):
        sessions_dir = self._setup(monkeypatch, tmp_path)
        _write_source_meta(sessions_dir, "source", {
            "session_id": "source", "title": "接口调试",
        })
        from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

        titles = [
            fork_session(
                source_session_id="source",
                target_session_id=f"fork-{index}",
                channel_id="web",
            )["title"]
            for index in range(1, 4)
        ]

        assert titles == [
            "接口调试 (Branch)",
            "接口调试 (Branch 2)",
            "接口调试 (Branch 3)",
        ]

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
            "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
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

    def test_history_is_rebound_to_target_and_keeps_source_provenance(
        self, tmp_path, monkeypatch
    ):
        sessions_dir = self._setup(monkeypatch, tmp_path)
        source_id = "web_source"
        target_id = "web_target"
        source_record = {
            "id": "request-1:assistant",
            "role": "assistant",
            "event_type": "chat.final",
            "session_id": source_id,
            "event_payload": {
                "parent_session_id": source_id,
                "nested": {"sessionId": source_id},
            },
            "content": "source answer",
        }
        _write_source_meta(
            sessions_dir,
            source_id,
            {
                "session_id": source_id,
                "title": "Source",
                "message_count": 1,
                "mode": "agent.work.normal",
            },
        )
        (sessions_dir / source_id / "history.jsonl").write_text("\n", encoding="utf-8")
        write_history = MagicMock()
        flush_history = MagicMock()

        with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=[source_record],
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.flush_history_writes",
            flush_history,
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.write_history_records",
            write_history,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
            return_value=[],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

            fork_session(
                source_session_id=source_id,
                target_session_id=target_id,
                channel_id="web",
            )

        copied = write_history.call_args.args[1][0]
        flush_history.assert_called_once_with()
        assert copied["session_id"] == target_id
        assert copied["event_payload"]["parent_session_id"] == target_id
        assert copied["event_payload"]["nested"]["sessionId"] == target_id
        assert copied["forked_from"] == {
            "session_id": source_id,
            "original_id": "request-1:assistant",
        }
        assert source_record["session_id"] == source_id
        assert source_record["event_payload"]["parent_session_id"] == source_id

    def test_message_fork_copies_history_only_through_selected_assistant(
        self, tmp_path, monkeypatch
    ):
        sessions_dir = self._setup(monkeypatch, tmp_path)
        source_id = "web_source"
        target_id = "web_target"
        records = [
            {
                "id": "request-1:user",
                "role": "user",
                "request_id": "request-1",
                "timestamp": 100.0,
                "content": "first question",
            },
            {
                "id": "request-1:assistant",
                "role": "assistant",
                "request_id": "measurement-1",
                "event_type": "context.usage",
                "timestamp": 101.0,
                "content": "",
            },
            {
                "id": "request-1:assistant",
                "role": "assistant",
                "request_id": "request-1",
                "event_type": "chat.final",
                "timestamp": 102.0,
                "content": "first answer",
            },
            {
                "id": "request-1:assistant",
                "role": "assistant",
                "request_id": "request-1",
                "event_type": "chat.usage_summary",
                "timestamp": 103.0,
                "content": "",
            },
            {
                "id": "request-2:user",
                "role": "user",
                "request_id": "request-2",
                "timestamp": 104.0,
                "content": "later question",
            },
        ]
        _write_source_meta(
            sessions_dir,
            source_id,
            {
                "session_id": source_id,
                "title": "Source",
                "message_count": len(records),
                "last_message_at": 104.0,
                "mode": "agent.work.normal",
            },
        )
        (sessions_dir / source_id / "history.jsonl").write_text("\n", encoding="utf-8")
        write_history = MagicMock()

        with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=records,
        ), patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.write_history_records",
            write_history,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
            return_value=[],
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import fork_session

            fork_session(
                source_session_id=source_id,
                target_session_id=target_id,
                channel_id="web",
                cutoff_message_id="request-1:assistant",
                cutoff_role="assistant",
                cutoff_content="first answer",
                cutoff_timestamp=102.0,
            )

        copied_records = write_history.call_args.args[1]
        assert [record["event_type"] for record in copied_records[1:]] == [
            "context.usage",
            "chat.final",
        ]
        assert all(record.get("content") != "later question" for record in copied_records)

        target_meta = _read_target_meta(sessions_dir, target_id)
        assert target_meta["message_count"] == 3
        assert target_meta["last_message_at"] == 102.0
        assert target_meta["forked_at"]["message_id"] == "request-1:assistant"
