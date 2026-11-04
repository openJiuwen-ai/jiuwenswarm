# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only query scans must not join scans that enqueue metadata migrations."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import Mock

from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session import session_metadata


def test_read_only_scan_projects_legacy_metadata_without_scheduling_writes(
    tmp_path, monkeypatch
):
    root = tmp_path / "sessions"
    folder = root / "process_cli_legacy"
    folder.mkdir(parents=True)
    file = folder / "metadata.json"
    file.write_text(
        json.dumps(
            {
                "session_id": folder.name,
                "channel_id": "process_cli",
                "mode": "agent",
                "created_at": 1,
                "last_message_at": 2,
            }
        ),
        encoding="utf-8",
    )
    original = file.read_bytes()
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "_build_project_lookup", lambda: ({}, {}))
    enqueue = Mock(side_effect=AssertionError("read-only scan scheduled a migration"))
    monkeypatch.setattr(session_metadata, "_enqueue_inference_write", enqueue)
    result = session_metadata.collect_all_sessions_metadata(enable_writeback=False)
    assert result[0]["mode"] == "agent.work.normal"
    assert file.read_bytes() == original
    enqueue.assert_not_called()


def test_overlapping_read_only_and_default_scans_use_separate_results(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    entered, readonly_entered, release = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    policies = []

    def scan(_user_id, *, enable_writeback=True):
        policies.append(enable_writeback)
        (entered if enable_writeback else readonly_entered).set()
        assert release.wait(3)
        return [{"writeback": enable_writeback}]

    monkeypatch.setattr(session_metadata, "_collect_all_sessions_metadata", scan)
    with ThreadPoolExecutor(max_workers=2) as executor:
        default = executor.submit(session_metadata.collect_all_sessions_metadata)
        assert entered.wait(3)
        readonly = executor.submit(
            session_metadata.collect_all_sessions_metadata, enable_writeback=False
        )
        try:
            assert readonly_entered.wait(3)
        finally:
            release.set()
        assert default.result(3) == [{"writeback": True}]
        assert readonly.result(3) == [{"writeback": False}]
    assert sorted(policies) == [False, True]
