"""个人版会话列表不展示团队版 IM 运行时会话。"""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.session import session_metadata


def _write_session(root: Path, session_id: str, *, channel_id: str, title: str) -> None:
    directory = root / session_id
    directory.mkdir(parents=True)
    payload = {
        "session_id": session_id,
        "channel_id": channel_id,
        "title": title,
        "work_mode": "work",
        "project_id": "",
        "created_at": 1,
        "last_message_at": 1,
        "message_count": 0,
        "mode": "agent.work.normal",
    }
    (directory / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_product_session_lists_omit_im_runtime_sessions(tmp_path: Path, monkeypatch) -> None:
    _write_session(tmp_path, "web_personal", channel_id="web", title="")
    _write_session(
        tmp_path,
        "ca_im_agent_feishu_oc_group",
        channel_id="web",
        title="",
    )
    _write_session(tmp_path, "heartbeat_probe", channel_id="web", title="probe")
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)

    listed, total = session_metadata.get_all_sessions_metadata(limit=20, offset=0)
    collected = session_metadata.collect_all_sessions_metadata()

    assert total == 1
    assert [item["session_id"] for item in listed] == ["web_personal"]
    assert [item["session_id"] for item in collected] == ["web_personal"]
