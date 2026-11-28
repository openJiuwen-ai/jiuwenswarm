"""企业版团队：chat.file 优先走会话直播，非团队会话保持网关推送。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.enterprise.team import session_live as live


def _download_info(file_path: Path, session_id: str, user_id: str, metadata=None) -> dict:
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
                "jiuwenswarm.enterprise.is_enterprise",
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


def test_enterprise_team_file_prefers_session_live_over_successful_push(tmp_path):
    """A host push that "succeeds" is not enough for an enterprise team session.

    The gateway accepts the server push even when the browser has no session
    WebSocket; only the session live subscription reaches it. The file must use
    that path, otherwise no download card and no artifact entry appear.
    """
    file_path = tmp_path / "report.md"
    file_path.write_text("# report\n", encoding="utf-8")
    session_id = "web-team-live-preferred"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []
    push_calls: list[dict] = []

    async def _push(message: dict) -> bool:
        push_calls.append(message)
        return True

    async def _run():
        _queue, task = _install_live(session_id)
        try:
            with patch.object(sfu, "send_runtime_push", _push), patch(
                "jiuwenswarm.enterprise.is_enterprise",
                return_value=True,
            ), patch(
                "jiuwenswarm.agents.harness.common.tools.obs_file_delivery.build_object_file_download_info",
                _download_info,
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

    assert "成功发送" in result
    assert push_calls == []
    assert len(published) == 1
    assert published[0]["metadata"]["delivery_scope"] == "session_live"
    assert published[0]["body"]["event_type"] == "chat.file"


def test_enterprise_team_file_with_non_web_target_keeps_gateway_push(tmp_path):
    """Live only reaches web subscribers; non-web targets still need the push."""
    file_path = tmp_path / "report.md"
    file_path.write_text("# report\n", encoding="utf-8")
    session_id = "web-team-cross-channel"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []
    push_calls: list[dict] = []

    async def _push(message: dict) -> bool:
        push_calls.append(message)
        return True

    async def _run():
        _queue, task = _install_live(session_id)
        try:
            with patch.object(sfu, "send_runtime_push", _push), patch(
                "jiuwenswarm.enterprise.is_enterprise",
                return_value=True,
            ), patch(
                "jiuwenswarm.agents.harness.common.tools.obs_file_delivery.build_object_file_download_info",
                _download_info,
            ), patch(
                "jiuwenswarm.enterprise.team.push_fanout.publish",
                published.append,
            ), patch(
                "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            ):
                return await toolkit.send_file(str(file_path), target_channels=["feishu"])
        finally:
            await _remove_live(session_id, task)

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result = asyncio.run(_run())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "成功发送" in result
    assert len(published) == 1
    assert published[0]["metadata"]["delivery_scope"] == "session_live"
    assert len(push_calls) == 1
    assert push_calls[0]["metadata"]["send_file_targets"] == ["feishu"]


def test_enterprise_team_file_live_success_ignores_gateway_push_error(tmp_path):
    """A failed non-web push must not turn a delivered file into a failure.

    Live already reached web subscribers. Reporting an error would skip
    ``_mark_files_sent`` and the history write, so an agent retry would publish
    the same card twice.
    """
    file_path = tmp_path / "report.md"
    file_path.write_text("# report\n", encoding="utf-8")
    session_id = "web-team-live-push-raises"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []

    async def _push(_message: dict) -> bool:
        raise RuntimeError("gateway transport down")

    async def _run():
        _queue, task = _install_live(session_id)
        try:
            with patch.object(sfu, "send_runtime_push", _push), patch(
                "jiuwenswarm.enterprise.is_enterprise",
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
                result = await toolkit.send_file(str(file_path), target_channels=["feishu"])
            return result, append_history
        finally:
            await _remove_live(session_id, task)

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result, append_history = asyncio.run(_run())
        sent = set().union(*sfu._SENT_FILE_PATHS_BY_SESSION.values())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "提交文件失败" not in result
    assert len(published) == 1
    assert append_history.call_args.kwargs["event_type"] == "chat.file"
    assert sfu._normalize_sent_file_path(str(file_path)) in sent


def test_enterprise_team_file_without_live_falls_back_to_push(tmp_path):
    file_path = tmp_path / "report.md"
    file_path.write_text("# report\n", encoding="utf-8")
    session_id = "web-team-no-live"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    published: list[dict] = []
    push_calls: list[dict] = []

    async def _push(message: dict) -> bool:
        push_calls.append(message)
        return True

    async def _run():
        with patch.object(sfu, "send_runtime_push", _push), patch(
            "jiuwenswarm.enterprise.is_enterprise",
            return_value=True,
        ), patch(
            "jiuwenswarm.agents.harness.common.tools.obs_file_delivery.build_object_file_download_info",
            _download_info,
        ), patch(
            "jiuwenswarm.enterprise.team.push_fanout.publish",
            published.append,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ):
            return await toolkit.send_file(str(file_path))

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result = asyncio.run(_run())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "成功发送" in result
    assert published == []
    assert len(push_calls) == 1


def test_enterprise_team_file_live_error_falls_back_to_push(tmp_path):
    file_path = tmp_path / "report.md"
    file_path.write_text("# report\n", encoding="utf-8")
    session_id = "web-team-live-error"
    toolkit = sfu.SendFileToolkit(
        request_id="req-file",
        session_id=session_id,
        channel_id="web",
    )
    push_calls: list[dict] = []

    async def _push(message: dict) -> bool:
        push_calls.append(message)
        return True

    def _boom(_message: dict) -> bool:
        raise RuntimeError("live down")

    async def _run():
        with patch.object(sfu, "send_runtime_push", _push), patch(
            "jiuwenswarm.enterprise.is_enterprise",
            return_value=True,
        ), patch(
            "jiuwenswarm.agents.harness.common.tools.obs_file_delivery.build_object_file_download_info",
            _download_info,
        ), patch(
            "jiuwenswarm.enterprise.team.session_live.publish_team_session_file",
            _boom,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ):
            return await toolkit.send_file(str(file_path))

    sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)
    try:
        result = asyncio.run(_run())
    finally:
        sfu._SENT_FILE_PATHS_BY_SESSION.pop(session_id, None)

    assert "成功发送" in result
    assert len(push_calls) == 1


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
                "jiuwenswarm.enterprise.is_enterprise",
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
