# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron run deliverables collection and re-delivery (方案 A)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.cron.models import CronJob, CronRunState
from jiuwenswarm.gateway.cron.scheduler import (
    CronSchedulerService,
    _resolve_delivery_session_id,
)

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
        job = SimpleNamespace(
            id="job-1",
            name="测试任务",
            session_id="desktop_1a_source",
            delivery_session_id="desktop_1a_display",
        )
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
        # 展示渠道只认桌面回写的 delivery_session_id，绝不回落 job.session_id
        # （创建来源会话可能已被删除，兜底 append 会重建出纯文件幽灵会话）。
        assert payload.get("session_id") == "desktop_1a_display"
        assert payload.get("session_id") != job.session_id
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


# ── 展示会话路由（delivery_session_id）回归 ─────────────────────────────────
#
# 幽灵会话缺陷回归：文件投递历史兜底绝不允许写入"创建来源/IM 投递路由"语义的
# job.session_id 并把已删除的会话自动重建为纯文件幽灵会话。
# 契约：web/desktop/tui 展示渠道只认 delivery_session_id（桌面回写）；
# 未回写时跳过历史兜底（广播照发）；IM 渠道维持 job.session_id 路由不变。


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    """会话落盘根目录与下载令牌密钥隔离到临时目录."""
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    import jiuwenswarm.server.runtime.session.session_history as sh
    import jiuwenswarm.server.runtime.session.session_metadata as sm

    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_SECRET", "test-secret")
    return root


class _DeliveryRecorder:
    def __init__(self):
        self.frames = []

    async def publish_robot_messages(self, msg):
        self.frames.append(msg)


def _make_delivery_service():
    """最小化构造：_push_run_deliverables 仅依赖 _now_fn/_message_handler."""
    svc = object.__new__(CronSchedulerService)
    svc._now_fn = time.time
    svc._message_handler = _DeliveryRecorder()
    return svc


def _make_job_state(job_id, session_id, delivery_session_id, deliver_file, ts=1794480000):
    job = CronJob(
        id=job_id,
        name="版本验证",
        enabled=True,
        cron_expr="*/1 * * * *",
        timezone="Asia/Shanghai",
        targets="desktop",
        session_id=session_id,
        delivery_session_id=delivery_session_id,
    )
    state = CronRunState(
        run_id=f"{job_id}:{ts}",
        job_id=job_id,
        wake_at_iso="2026-11-12T09:20:00+08:00",
        push_at_iso="2026-11-12T09:20:05+08:00",
        status="succeeded",
        exec_channel_id="__cron__",
        exec_session_id="__cron___exec",
        deliver_files=[
            {"path": str(deliver_file), "name": deliver_file.name, "artifact_id": "art-1"}
        ],
    )
    return job, state


def _push_delivery(svc, job, state, channel_id="desktop"):
    async def _run():
        await svc._push_run_deliverables(
            job=job,
            state=state,
            channel_id=channel_id,
            msg_session_id=None,
            routing_session_id=None,
            metadata=None,
            group_digital_avatar=False,
            app_id=None,
        )
        from jiuwenswarm.server.runtime.session.session_history import flush_history_writes

        flush_history_writes()

    asyncio.run(_run())
    assert svc._message_handler.frames, "应产生 chat.file 广播帧"
    return svc._message_handler.frames[-1]


def _session_history(sessions_root, sid):
    p = sessions_root / sid / "history.jsonl"
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def _list_sessions(sessions_root):
    return sorted(p.name for p in sessions_root.iterdir() if p.is_dir())


def test_resolve_delivery_session_id_by_channel():
    job = CronJob(
        id="j", name="n", enabled=True, cron_expr="*/1 * * * *",
        timezone="Asia/Shanghai", targets="desktop",
        session_id="sess_source", delivery_session_id="sess_display",
    )
    for ch in ("desktop", "web", "tui", "Desktop"):
        assert _resolve_delivery_session_id(job, ch) == "sess_display"
    for ch in ("feishu", "dingtalk", "whatsapp", "wecom", "wechat", "xiaoyi",
               "feishu_enterprise:app1"):
        assert _resolve_delivery_session_id(job, ch) == "sess_source"


def test_no_ghost_session_created_without_delivery_session_id(sessions_root, tmp_path):
    """展示渠道未回写 delivery_session_id：广播照发，但绝不兜底写来源会话."""
    deliver_file = tmp_path / "report.txt"
    deliver_file.write_text("data", encoding="utf-8")
    svc = _make_delivery_service()
    # 事故形态：session_id 指向已被用户删除的创建来源会话
    job, state = _make_job_state("job-ghost", "sess_deleted_source", None, deliver_file)

    frame = _push_delivery(svc, job, state)

    assert frame.payload["event_type"] == "chat.file"
    assert frame.payload["session_id"] is None
    assert "sess_deleted_source" not in _list_sessions(sessions_root)
    assert _list_sessions(sessions_root) == []


def test_file_history_lands_in_delivery_session(sessions_root, tmp_path):
    """delivery_session_id 已回写：文件历史落展示会话，来源会话不被触碰."""
    deliver_file = tmp_path / "report.txt"
    deliver_file.write_text("data", encoding="utf-8")
    (sessions_root / "sess_display").mkdir()
    svc = _make_delivery_service()
    job, state = _make_job_state("job-ok", "sess_deleted_source", "sess_display", deliver_file)

    frame = _push_delivery(svc, job, state)

    assert frame.payload["session_id"] == "sess_display"
    assert frame.payload["files"][0]["download_url"]
    hist = _session_history(sessions_root, "sess_display")
    assert len(hist) == 1
    rec = hist[0]
    assert rec["role"] == "assistant"
    assert rec["event_type"] == "chat.file"
    assert rec["request_id"] == f"cron-{state.run_id}"
    assert rec["files"][0]["name"] == "report.txt"
    assert "sess_deleted_source" not in _list_sessions(sessions_root)


def test_repeat_delivery_no_duplicate_session(sessions_root, tmp_path):
    """多轮投递：文件记录累加进同一展示会话，不产生任何新会话."""
    deliver_file = tmp_path / "report.txt"
    deliver_file.write_text("data", encoding="utf-8")
    (sessions_root / "sess_display").mkdir()
    svc = _make_delivery_service()
    job, state1 = _make_job_state("job-loop", "sess_deleted_source", "sess_display", deliver_file, ts=1794480000)
    _, state2 = _make_job_state("job-loop", "sess_deleted_source", "sess_display", deliver_file, ts=1794480300)

    _push_delivery(svc, job, state1)
    _push_delivery(svc, job, state2)

    assert len(_session_history(sessions_root, "sess_display")) == 2
    assert _list_sessions(sessions_root) == ["sess_display"]


def test_im_channel_keeps_session_id_routing(sessions_root, tmp_path):
    """IM 渠道行为不变：沿用 job.session_id 外部投递路由."""
    deliver_file = tmp_path / "report.txt"
    deliver_file.write_text("data", encoding="utf-8")
    (sessions_root / "sess_im").mkdir()
    svc = _make_delivery_service()
    job, state = _make_job_state("job-im", "sess_im", None, deliver_file)

    frame = _push_delivery(svc, job, state, channel_id="feishu")

    assert frame.payload["session_id"] == "sess_im"
    assert len(_session_history(sessions_root, "sess_im")) == 1
    assert "sess_display" not in _list_sessions(sessions_root)


def test_job_delivery_session_id_roundtrip():
    job = CronJob(
        id="j", name="n", enabled=True, cron_expr="*/1 * * * *",
        timezone="Asia/Shanghai", targets="web", description="d",
        delivery_session_id="sess_display",
    )
    data = job.to_dict()
    assert data["delivery_session_id"] == "sess_display"
    assert CronJob.from_dict(data).delivery_session_id == "sess_display"

    legacy = CronJob(
        id="j", name="n", enabled=True, cron_expr="*/1 * * * *",
        timezone="Asia/Shanghai", targets="web", description="d",
    )
    legacy_data = legacy.to_dict()
    assert "delivery_session_id" not in legacy_data
    assert CronJob.from_dict(legacy_data).delivery_session_id is None


def test_store_update_delivery_session_id(tmp_path):
    """桌面端经 cron.job.update 回写展示会话（controller.update_job → store）."""
    from jiuwenswarm.gateway.cron.store import CronJobStore

    store = CronJobStore(tmp_path / "cron_jobs.json")

    async def _run():
        await store.create_job(
            job_id="job-store",
            name="版本验证",
            cron_expr="*/1 * * * *",
            timezone="Asia/Shanghai",
            description="d",
            targets="web",
            session_id="sess_source",
        )
        created = await store.get_job("job-store")
        assert created is not None and created.delivery_session_id is None
        updated = await store.update_job("job-store", {"delivery_session_id": "sess_display"})
        assert updated.delivery_session_id == "sess_display"
        reread = await store.get_job("job-store")
        assert reread is not None and reread.delivery_session_id == "sess_display"
        cleared = await store.update_job("job-store", {"delivery_session_id": ""})
        assert cleared.delivery_session_id is None

    asyncio.run(_run())
