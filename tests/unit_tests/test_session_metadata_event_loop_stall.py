# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""验证 update_session_metadata 在慢盘场景下不阻塞事件循环（OA.05000010）。

背景（2026-10-15 生产事故）：普通消息的元数据更新（不传 pinned/pin_order）
经 ``preserve_pin_fields=True`` 触发 ``_merge_pin_fields_from_disk``——每次
更新都在调用方线程（AgentServer 事件循环）同步读 metadata.json。慢盘/NFS
叠加高频消息时事件循环停摆，请求超时且 pong 停发。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import session_metadata as sm

_SIMULATED_SLOW_READ_SECONDS = 1.5
_HEARTBEAT_INTERVAL = 0.05


@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    sm._METADATA_CACHE.clear()
    return d


def _init_session(sid: str, sessions_dir: Path) -> Path:
    sm.init_session_metadata(session_id=sid, channel_id="web", user_id="u1", title="t")
    # 预热读缓存，模拟稳态：_read_metadata 命中 miss 时不回填缓存，缓存仅由
    # 写入路径(_enqueue_write)填充。先走一次完整更新并等 worker 落盘，让后续
    # 读路径缓存命中不触盘，使测试只对"每次更新必经的 preserve_pin merge 读盘"敏感。
    sm.update_session_metadata(session_id=sid, model="warmup-model")
    sm._METADATA_QUEUE.join()
    return sessions_dir / sid / "metadata.json"


@pytest.mark.asyncio
async def test_update_session_metadata_keeps_event_loop_responsive_on_slow_disk(
    monkeypatch, sessions_dir
):
    sid = "sess_stall_001"
    meta_path = _init_session(sid, sessions_dir)

    original_read_text = Path.read_text

    def slow_read_text(self, *args, **kwargs):
        if self == meta_path:
            time.sleep(_SIMULATED_SLOW_READ_SECONDS)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_read_text)

    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.3)

    # 普通消息路径的元数据更新：不传 pinned/pin_order → preserve_pin_fields=True，
    # 缓存命中时读路径不触盘；任何 metadata.json 读取都意味着调用方线程被阻塞。
    sm.update_session_metadata(session_id=sid, model="foo-model")

    await asyncio.sleep(0.3)
    hb.cancel()
    try:
        await hb
    except asyncio.CancelledError:
        pass

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "heartbeat never ticked"
    max_gap = max(gaps)
    assert max_gap < _SIMULATED_SLOW_READ_SECONDS, (
        f"event loop stalled {max_gap:.2f}s during metadata update; "
        "disk reads must not run on the event loop thread"
    )

    # 更新最终落盘（后台线程允许慢，但不允许丢）
    sm._METADATA_QUEUE.join()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["model"] == "foo-model"


def test_update_session_metadata_preserves_disk_pin_fields(sessions_dir):
    """去掉调用方线程 merge 后，落盘仍必须保留磁盘上的最新 pin 字段。

    模拟跨进程场景：Gateway 写入 pinned=True/pin_order=3 后，AgentServer
    以陈旧缓存（pinned=False）执行普通更新——磁盘 pin 字段不得被覆盖。
    """
    sid = "sess_pin_preserve_001"
    meta_path = _init_session(sid, sessions_dir)

    # 模拟 Gateway 进程写入置顶状态
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["pinned"] = True
    data["pin_order"] = 3
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # AgentServer 普通消息更新（内存缓存中 pinned=False，已陈旧）
    sm.update_session_metadata(session_id=sid, model="foo-model")
    sm._METADATA_QUEUE.join()

    written = json.loads(meta_path.read_text(encoding="utf-8"))
    assert written["pinned"] is True
    assert written["pin_order"] == 3
    assert written["model"] == "foo-model"


def test_worker_write_failure_drops_stale_pin_cache(sessions_dir, monkeypatch, caplog):
    """worker 落盘失败时必须失效未 merge 的缓存视图,且告警带 session_id。

    _enqueue_write 顶部缓存的是未 merge 磁盘 pin 字段的 metadata。若 worker
    落盘失败(磁盘满/文件锁超时/权限)后不失效,读路径将一直命中该陈旧视图
    (表现为置顶排序长期错位),且告警无 session_id 无法定位是哪个会话。
    """
    sid = "sess_write_fail_001"
    meta_path = _init_session(sid, sessions_dir)

    # 模拟 Gateway 进程写入置顶状态:磁盘真源已更新,而本进程缓存陈旧
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["pinned"] = True
    data["pin_order"] = 3
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _disk_full(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(sm, "_write_metadata_sync", _disk_full)

    with caplog.at_level("WARNING"):
        sm.update_session_metadata(session_id=sid, model="foo-model")
        sm._METADATA_QUEUE.join()

    cache_key = sm._metadata_cache_key(sid, None)
    assert cache_key not in sm._METADATA_CACHE, (
        "failed write must drop the stale un-merged cache entry"
    )
    assert any(sid in rec.getMessage() for rec in caplog.records), (
        "failure warning must carry the session_id for triage"
    )
    # 缓存失效后,下一次读退化为磁盘读,看到 Gateway 写入的置顶状态
    assert sm._read_metadata(sid)["pinned"] is True
