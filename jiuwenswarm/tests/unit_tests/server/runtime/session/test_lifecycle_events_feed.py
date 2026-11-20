"""JW-56：lifecycle 事件轮询的 mtime 短路与死资源/孤儿锁回收。

1. event_snapshots：目录无变化时命中 mtime 快照缓存（不重读文件）；
   状态变化（atomic_json 替写）/ 文件新增 / 文件删除都会使缓存失效。
2. GC：completed+deleted 且过保留期的资源 JSON 与同名 .lock 被回收；
   保留期内、running 中、归档（非 deleted）资源不动；孤儿锁单独回收。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc


@pytest.fixture(autouse=True)
def _reset_lifecycle_global_state():
    """隔离全局缓存与 GC 节流时间戳（lifecycle 模块级状态，测试间必须重置）。"""
    lc._clear_snapshot_cache()
    global_state = lc
    global_state._last_gc_attempt = 0.0
    yield
    lc._clear_snapshot_cache()
    global_state._last_gc_attempt = 0.0


def _setup_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: root / "sessions")
    return root


def _resource_dir(root: Path) -> Path:
    directory = root / "lifecycle" / "resources"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_resource(
    directory: Path,
    name: str,
    *,
    status: str = "running",
    kind: str = "session",
    deleted: bool = False,
    blocked: bool = False,
    mtime_age: float = 0.0,
    with_lock: bool = False,
) -> Path:
    path = directory / name
    value = dict(
        revision=1,
        deleted=deleted,
        blocked=blocked,
        operation=dict(
            operation_id=f"op-{name}",
            resource_type=kind,
            resource_id=name.removeprefix("session_").removesuffix(".json"),
            kind="delete" if deleted else "archive",
            status=status,
            project_id="default",
        ),
    )
    lc.atomic_json(path, value)
    if with_lock:
        lock = path.with_name(path.name + ".lock")
        lock.write_bytes(b"\x00")
    if mtime_age > 0:
        stamp = time.time() - mtime_age
        import os

        os.utime(path, (stamp, stamp))
        if with_lock:
            os.utime(lock, (stamp, stamp))
    return path


# ---------- event_snapshots：mtime 短路缓存 ----------


def test_snapshot_cache_hits_when_directory_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(directory, "session_s1.json", status="running")

    lc._clear_snapshot_cache()
    first = lc.event_snapshots()
    second = lc.event_snapshots()
    assert first == second
    assert len(first) == 1
    assert first[0]["payload"]["resource_id"] == "s1"


def test_snapshot_cache_invalidates_on_state_write(
    monkeypatch, tmp_path: Path
) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(directory, "session_s1.json", status="running")

    lc._clear_snapshot_cache()
    running = lc.event_snapshots()
    assert running[0]["completed"] is False

    # completed 写回同一文件（atomic_json tmp+rename，mtime 必变）
    value = lc.read_json(directory / "session_s1.json")
    value["operation"]["status"] = "completed"
    lc.atomic_json(directory / "session_s1.json", value)
    # Windows 文件时间源存在毫秒级量化：连续两次 atomic_json 可能落在同一
    # mtime 桶里导致缓存命中（生产中 2s 轮询间隔远大于量化桶，不可达）。
    # 这里显式拉开 mtime，保证测试确定性。
    import os

    stamp = time.time() + 1.0
    os.utime(directory / "session_s1.json", (stamp, stamp))

    updated = lc.event_snapshots()
    assert updated[0]["completed"] is True


def test_snapshot_cache_invalidates_on_file_removal(
    monkeypatch, tmp_path: Path
) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    p1 = _write_resource(directory, "session_s1.json", status="running")
    _write_resource(directory, "session_s2.json", status="running")

    lc._clear_snapshot_cache()
    assert len(lc.event_snapshots()) == 2
    p1.unlink()
    assert len(lc.event_snapshots()) == 1


def test_snapshot_cache_isolated_per_data_directory(
    monkeypatch, tmp_path: Path
) -> None:
    # 缓存键含目录路径：切换数据目录不会串台。
    root_a = _setup_root(monkeypatch, tmp_path / "a")
    da = _resource_dir(root_a)
    _write_resource(da, "session_s1.json", status="running")
    assert lc.event_snapshots()[0]["completed"] is False

    # 切换数据目录：同名文件、不同状态，缓存不得串台。
    root_b = _setup_root(monkeypatch, tmp_path / "b")
    db = _resource_dir(root_b)
    _write_resource(db, "session_s1.json", status="completed")
    assert lc.event_snapshots()[0]["completed"] is True

    # 切回目录 a：缓存仍在且内容正确。
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: root_a / "sessions")
    assert lc.event_snapshots()[0]["completed"] is False


# ---------- events_feed：GC ----------


def test_gc_reclaims_dead_resource_and_lock(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc._GC_RETENTION_SECONDS + 3600,
        with_lock=True,
    )

    lc._maybe_sweep()
    assert not (directory / "session_dead.json").exists()
    assert not (directory / "session_dead.json.lock").exists()


def test_gc_keeps_running_and_fresh_resources(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    running = _write_resource(
        directory, "session_running.json", status="running", with_lock=True
    )
    fresh_dead = _write_resource(
        directory,
        "session_fresh.json",
        status="completed",
        deleted=True,
        mtime_age=60.0,  # 远小于保留期
        with_lock=True,
    )
    archived = _write_resource(
        directory,
        "session_archived.json",
        status="completed",
        deleted=False,
        mtime_age=lc._GC_RETENTION_SECONDS + 3600,
        with_lock=True,
    )

    lc._maybe_sweep()
    assert running.exists()
    assert fresh_dead.exists()  # 保留期内不回收：watcher 可能还没取走最终 revision
    assert archived.exists()  # 归档不是死数据
    for p in (running, fresh_dead, archived):
        assert p.with_name(p.name + ".lock").exists()


def test_gc_reclaims_orphan_lock_without_json(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    orphan = directory / "session_gone.json.lock"
    orphan.write_bytes(b"\x00")

    lc._maybe_sweep()
    assert not orphan.exists()


def test_gc_is_throttled_by_interval(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc._GC_RETENTION_SECONDS + 3600,
        with_lock=True,
    )

    lc._maybe_sweep()
    assert not (directory / "session_dead.json").exists()

    # 间隔内再死一个新资源，不会被立即回收（节流）。
    _write_resource(
        directory,
        "session_dead2.json",
        status="completed",
        deleted=True,
        mtime_age=lc._GC_RETENTION_SECONDS + 3600,
        with_lock=True,
    )
    lc._maybe_sweep()
    assert (directory / "session_dead2.json").exists()
    assert (directory / "session_dead2.json.lock").exists()


def test_gc_survives_broken_json(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    broken = directory / "session_broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    import os

    old = time.time() - (lc._GC_RETENTION_SECONDS + 3600)
    os.utime(broken, (old, old))

    lc._maybe_sweep()
    assert not broken.exists()  # 损坏文件超过保留期视作死文件回收


def test_gc_survives_binary_garbage_and_sweeps_rest(
    monkeypatch, tmp_path: Path
) -> None:
    # 非 UTF-8 的二进制垃圾（杀软截断/磁盘坏块）抛 UnicodeDecodeError，
    # 不是 JSONDecodeError：GC 必须回收它且不中止本轮对后续死文件的清理。
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    garbage = directory / "session_a_garbage.json"
    garbage.write_bytes(b"\x80\x81\x9f\xfe\xff\x00binary")
    dead_after = directory / "session_b_dead.json"
    _write_resource(
        directory,
        "session_b_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc._GC_RETENTION_SECONDS + 3600,
        with_lock=True,
    )
    import os

    old = time.time() - (lc._GC_RETENTION_SECONDS + 3600)
    os.utime(garbage, (old, old))

    lc._maybe_sweep()
    assert not garbage.exists()
    assert not dead_after.exists()  # 垃圾文件排在字母序前面，不得阻断后续回收
    assert not (directory / "session_b_dead.json.lock").exists()
