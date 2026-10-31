"""企业版团队：网关 WebSocket 推送失败时，chat.file 改走直播。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.enterprise.team import session_live as live


def _download_info(file_path: Path, session_id: str, user_id: str) -> dict:
    del session_id, user_id
    return {
        "name": file_path.name,
        "size": 4,
        "mime_type": "text/plain",
        "download_url": "/file-api/download?token=t",
        "download_token": "t",
    }


def _install_live(session_id: str) -> tuple[asyncio.Queue, asyncio.Task]:
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(asyncio.sleep(30))
    live._lives[session_id] = live._SessionLive(queue=queue, task=task)
    return queue, task


async def _remove_live(session_id: str, task: asyncio.Task) -> None:
    live._lives.pop(session_id, None)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_enterprise_file_uses_session_live_when_gateway_push_fails(tmp_path):
    file_path = tmp_path / "index.html"
    file_path.write_text("<html></html>", encoding="utf-8")
    session_id = "web-team-file"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []

    async def _push(_message: dict) -> bool:
        return False

    async def _run():
        queue, task = _install_live(session_id)
        try:
            with patch.object(sfu, "send_runtime_push", _push), patch(
                "jiuwenswarm.enterprise.edition.is_enterprise",
                return_value=True,
            ), patch(
                "jiuwenswarm.agents.harness.common.tools.obs_file_delivery.build_object_file_download_info",
                _download_info,
            ), patch(
                "jiuwenswarm.enterprise.team.push_fanout.publish",
                published.append,
            ), patch(
                "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            ) as append_history:
                result = await toolkit.send_file(str(file_path))
            assert queue.empty()
            return result, append_history
        finally:
            await _remove_live(session_id, task)

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result, append_history = asyncio.run(_run())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "成功发送" in result
    assert len(published) == 1
    wire = published[0]
    assert wire["session_id"] == session_id
    assert wire["metadata"]["delivery_scope"] == "session_live"
    assert wire["body"]["event_type"] == "chat.file"
    assert wire["body"]["delta"]["files"][0]["name"] == "index.html"
    assert append_history.call_args.kwargs["event_type"] == "chat.file"


def test_personal_file_push_failure_does_not_use_session_live(tmp_path):
    file_path = tmp_path / "index.html"
    file_path.write_text("<html></html>", encoding="utf-8")
    session_id = "web-personal-file"
    toolkit = sfu.SendFileToolkit(
        request_id="req-personal",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []

    async def _push(_message: dict) -> bool:
        return False

    async def _run():
        _queue, task = _install_live(session_id)
        try:
            with patch.object(sfu, "send_runtime_push", _push), patch(
                "jiuwenswarm.enterprise.edition.is_enterprise",
                return_value=False,
            ), patch(
                "jiuwenswarm.enterprise.team.push_fanout.publish",
                published.append,
            ), patch(
                "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            ):
                return await toolkit.send_file(str(file_path))
        finally:
            await _remove_live(session_id, task)

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result = asyncio.run(_run())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "active Runtime push host" in result
    assert published == []
