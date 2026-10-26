# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""send_file enterprise OBS path + distributed honest failure."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    SendFileToolkit,
    _SENT_FILE_PATHS_BY_SESSION,
    _partition_sent_files,
)


@pytest.fixture(autouse=True)
def _clear_dedup():
    _SENT_FILE_PATHS_BY_SESSION.clear()
    yield
    _SENT_FILE_PATHS_BY_SESSION.clear()


def test_send_file_obs_enterprise_writes_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", raising=False)
    file_path = tmp_path / "out.docx"
    file_path.write_bytes(b"docx-bytes")

    toolkit = SendFileToolkit(
        request_id="r-obs",
        session_id="sess-obs",
        channel_id="web",
    )
    mock_session = MagicMock()
    mock_session.write_stream = AsyncMock()

    with (
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_subagent_parent_session",
            return_value=mock_session,
        ),
        patch(
            "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
            return_value=MagicMock(),
        ),
        patch(
            "jiuwenswarm.channels.web.minio_upload.upload_local_file_to_minio",
            return_value={
                "url": "http://127.0.0.1:9000/b/downloads/x_out.docx",
                "name": "out.docx",
                "size": 10,
            },
        ),
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "对象存储" in result
    assert "代理" in result
    assert "成功发送" not in result or "已上传" in result
    mock_session.write_stream.assert_awaited_once()
    call_arg = mock_session.write_stream.await_args.args[0]
    assert call_arg.type == "chat.file"
    assert call_arg.payload["event_type"] == "chat.file"
    assert call_arg.payload["files"][0]["url"].startswith("http")
    # Dedup marked
    new_paths, skipped = _partition_sent_files("sess-obs", [str(file_path)])
    assert new_paths == []
    assert skipped == [str(file_path)]


def test_send_file_obs_skipped_when_personal(tmp_path, monkeypatch):
    """Personal edition must keep send_push local path (no MinIO)."""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.delenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", raising=False)
    file_path = tmp_path / "local.txt"
    file_path.write_text("hi", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r-p",
        session_id="sess-p",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _Fake:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_Fake),
    )

    upload = MagicMock()
    with (
        patch(
            "jiuwenswarm.channels.web.minio_upload.upload_local_file_to_minio",
            upload,
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ),
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in result
    upload.assert_not_called()
    assert mock_server.send_push.await_count == 1


def test_distributed_fails_when_delivered_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.delenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", raising=False)
    file_path = tmp_path / "big.bin"
    file_path.write_bytes(b"0123456789")

    toolkit = SendFileToolkit(
        request_id="r-d",
        session_id="sess-d",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=0)

    class _Fake:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_Fake),
    )

    ft_cfg = MagicMock()
    ft_cfg.enabled = True

    with (
        patch(
            "jiuwenswarm.common.file_transfer_config.get_file_transfer_config",
            return_value=ft_cfg,
        ),
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "发送失败" in result or "失败" in result
    assert "分布式发送成功" not in result
    assert "成功发送" not in result
    # Must not mark dedup on failure
    new_paths, skipped = _partition_sent_files("sess-d", [str(file_path)])
    assert new_paths == [str(file_path)]
    assert skipped == []
