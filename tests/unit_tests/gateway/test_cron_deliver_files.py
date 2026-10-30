# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron run deliverables collection and re-delivery (方案 A)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.cron.models import CronRunState

from tests.unit_tests.gateway.test_cron_scheduler import (
    FakeMessageHandler,
    _make_scheduler,
)


def _svc_with_recorder(monkeypatch, download_error=False):
    handler = FakeMessageHandler()
    store = SimpleNamespace()
    svc = _make_scheduler(store, handler=handler)
    import jiuwenswarm.agents.harness.common.tools.web_file_download as wfd

    def _fake_info(file_path, file_name, session_id="", **_kwargs):
        _ = (session_id, file_path, file_name)
        if download_error:
            raise ValueError("outbound download requires an existing session")
        return {
            "name": file_name,
            "size": 123,
            "mime_type": "image/jpeg",
            "download_url": "/file-api/download?token=u",
            "download_token": "u",
        }

    monkeypatch.setattr(wfd, "build_file_download_info", _fake_info)
    return svc, handler


def _history_file(tmp_path, records):
    hist = tmp_path / "history.jsonl"
    hist.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    return hist


class TestCollectRunDeliverables:
    def test_collects_this_run_chat_file(self, tmp_path, monkeypatch):
        job_id = "job-1"
        run_request = f"cron-{job_id}:1793264100"
        hist = _history_file(
            tmp_path,
            [
                {"role": "user", "request_id": run_request, "content": "task"},
                {
                    "role": "assistant",
                    "request_id": run_request,
                    "event_type": "chat.file",
                    "files": [{"path": "C:/tmp/a.jpg", "name": "a.jpg", "artifact_id": "art-1"}],
                },
                {
                    "role": "assistant",
                    "request_id": run_request,
                    "event_type": "chat.final",
                    "content": "done",
                },
            ],
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_read_history_path",
            lambda sid: hist,
        )
        svc = _make_scheduler(SimpleNamespace(), handler=FakeMessageHandler())

        got = svc._collect_run_deliverables("sess-exec", job_id)  # pylint: disable=protected-access

        assert got == [{"path": "C:/tmp/a.jpg", "name": "a.jpg", "artifact_id": "art-1"}]

    def test_skips_other_runs_and_bad_lines(self, tmp_path, monkeypatch):
        hist = _history_file(
            tmp_path,
            [
                "{broken json",
                {
                    "role": "assistant",
                    "request_id": "cron-other-job:1",
                    "event_type": "chat.file",
                    "files": [{"path": "C:/tmp/other.jpg"}],
                },
                {"role": "assistant", "request_id": "cron-job-9:2", "event_type": "chat.file", "files": []},
            ],
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_read_history_path",
            lambda sid: hist,
        )
        svc = _make_scheduler(SimpleNamespace(), handler=FakeMessageHandler())

        got = svc._collect_run_deliverables("sess-exec", "job-9")  # pylint: disable=protected-access

        assert got == []

    def test_empty_history_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_read_history_path",
            lambda sid: tmp_path / "missing.jsonl",
        )
        svc = _make_scheduler(SimpleNamespace(), handler=FakeMessageHandler())

        got = svc._collect_run_deliverables("sess-x", "job-1")  # pylint: disable=protected-access

        assert got == []


class TestPushRunDeliverables:
    @pytest.mark.asyncio
    async def test_pushes_files_with_download_info(self, tmp_path, monkeypatch):
        real_file = tmp_path / "a.jpg"
        real_file.write_bytes(b"x")
        missing = tmp_path / "missing.jpg"
        svc, handler = _svc_with_recorder(monkeypatch)
        job = SimpleNamespace(id="job-1", name="测试任务", session_id="desktop_1a_1")
        state = CronRunState(
            run_id="job-1:123",
            job_id="job-1",
            wake_at_iso="2026-10-29T16:00:00+08:00",
            push_at_iso="2026-10-29T16:00:00+08:00",
            deliver_files=[
                {"path": str(real_file), "name": "a.jpg", "artifact_id": "art-1"},
                {"path": str(missing), "name": "missing.jpg"},
            ],
        )

        await svc._push_run_deliverables(  # pylint: disable=protected-access
            job=job,
            state=state,
            channel_id="web",
            msg_session_id=None,
            routing_session_id="desktop_1a_9",
            metadata=None,
            group_digital_avatar=False,
            app_id=None,
        )

        chat_file_msgs = [
            m
            for m in handler.published
            if getattr(m, "event_type", None).value == "chat.file"
        ]
        assert len(chat_file_msgs) == 1
        # 前端 resolveConversationId 靠 session_id 归属会话：cron 文本由前端
        # 专门的 cron 会话流逻辑接住，chat.file 没有——必须自带绑定会话，
        # 否则 resolveConversationId 返回 undefined、整帧被忽略。
        assert chat_file_msgs[0].session_id == "desktop_1a_9"
        payload = chat_file_msgs[0].payload
        # 前端 chat.file 处理第一步 resolveEventSessionId(payload) 只认
        # payload.session_id（不认 wire 顶层）——缺失即整帧被静默忽略。
        # 这里带 job 绑定会话（= 用户发起会话），文件卡落在绑定会话聊天流里。
        assert payload.get("session_id") == "desktop_1a_1"
        assert isinstance(payload.get("cron"), dict)
        assert payload["cron"]["job_id"] == "job-1"
        assert payload["cron"]["run_id"] == "job-1:123"
        assert payload["cron"]["run_id"] == "job-1:123"
        files = payload["files"]
        assert len(files) == 1
        assert files[0]["name"] == "a.jpg"
        assert files[0]["download_url"] == "/file-api/download?token=u"
        assert files[0]["download_token"] == "u"
        assert files[0]["size"] == 123
        assert files[0]["artifact_id"] == "art-1"

    @pytest.mark.asyncio
    async def test_token_failure_keeps_plain_payload(self, tmp_path, monkeypatch):
        real_file = tmp_path / "b.jpg"
        real_file.write_bytes(b"x")
        svc, handler = _svc_with_recorder(monkeypatch, download_error=True)
        job = SimpleNamespace(id="job-2", name="测试任务", session_id="desktop_1a_2")
        state = CronRunState(
            run_id="job-2:1",
            job_id="job-2",
            wake_at_iso="2026-10-29T16:00:00+08:00",
            push_at_iso="2026-10-29T16:00:00+08:00",
            deliver_files=[{"path": str(real_file), "name": "b.jpg"}],
        )

        await svc._push_run_deliverables(  # pylint: disable=protected-access
            job=job,
            state=state,
            channel_id="web",
            msg_session_id=None,
            routing_session_id="desktop_1a_9",
            metadata=None,
            group_digital_avatar=False,
            app_id=None,
        )

        chat_file_msgs = [
            m
            for m in handler.published
            if getattr(m, "event_type", None).value == "chat.file"
        ]
        assert len(chat_file_msgs) == 1
        files = chat_file_msgs[0].payload["files"]
        assert files[0]["name"] == "b.jpg"
        assert "download_url" not in files[0]

    @pytest.mark.asyncio
    async def test_all_files_missing_pushes_nothing(self, tmp_path, monkeypatch):
        svc, handler = _svc_with_recorder(monkeypatch)
        job = SimpleNamespace(id="job-3", name="测试任务", session_id="desktop_1a_3")
        state = CronRunState(
            run_id="job-3:1",
            job_id="job-3",
            wake_at_iso="2026-10-29T16:00:00+08:00",
            push_at_iso="2026-10-29T16:00:00+08:00",
            deliver_files=[{"path": str(tmp_path / "nope.jpg"), "name": "nope.jpg"}],
        )

        await svc._push_run_deliverables(  # pylint: disable=protected-access
            job=job,
            state=state,
            channel_id="web",
            msg_session_id=None,
            routing_session_id="desktop_1a_9",
            metadata=None,
            group_digital_avatar=False,
            app_id=None,
        )

        assert not [
            m
            for m in handler.published
            if getattr(m, "event_type", None).value == "chat.file"
        ]

    @pytest.mark.asyncio
    async def test_no_deliverables_pushes_nothing(self, monkeypatch):
        svc, handler = _svc_with_recorder(monkeypatch)
        job = SimpleNamespace(id="job-4", name="测试任务", session_id=None)
        state = CronRunState(
            run_id="job-4:1",
            job_id="job-4",
            wake_at_iso="2026-10-29T16:00:00+08:00",
            push_at_iso="2026-10-29T16:00:00+08:00",
            deliver_files=[],
        )

        await svc._push_run_deliverables(  # pylint: disable=protected-access
            job=job,
            state=state,
            channel_id="web",
            msg_session_id=None,
            routing_session_id="desktop_1a_9",
            metadata=None,
            group_digital_avatar=False,
            app_id=None,
        )

        assert handler.published == []


class TestCollectOrdering:
    @pytest.mark.asyncio
    async def test_deliver_files_collected_before_persist(self, tmp_path, monkeypatch):
        """回收必须发生在 result_text 对外可见/任何挂起点之前（评审 P2 回归用例）。

        旧实现把回收放在 ``await _persist_run`` 之后，而 result_text 此前已写入：
        push 事件若落在该挂起窗口，会以空 deliver_files 发布最终推送并置
        pushed_final，本轮产物文件永久丢失且无任何日志。
        """
        from jiuwenswarm.gateway.cron.store import CronJobStore

        from tests.unit_tests.gateway.test_cron_scheduler import _create_one_job

        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, targets="web")
        run_request = f"cron-{job.id}:1234"
        hist = _history_file(
            tmp_path,
            [
                {
                    "role": "assistant",
                    "request_id": run_request,
                    "event_type": "chat.file",
                    "files": [{"path": "C:/tmp/a.jpg", "name": "a.jpg"}],
                }
            ],
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_read_history_path",
            lambda sid: hist,
        )
        svc = _make_scheduler(store, handler=FakeMessageHandler())
        seen = {}

        async def fake_persist(job_arg, state_arg):
            seen["deliver_files"] = list(state_arg.deliver_files)
            seen["result_text"] = state_arg.result_text

        monkeypatch.setattr(svc, "_persist_run", fake_persist)
        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]
        assert seen["deliver_files"], (
            "deliver_files 必须在 _persist_run 等挂起点之前回收完成（否则 push 竞态丢文件）"
        )
