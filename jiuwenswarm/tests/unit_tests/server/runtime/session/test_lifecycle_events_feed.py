"""JW-56：lifecycle 事件轮询的 mtime 短路与死资源/孤儿锁回收。

1. event_snapshots：目录无变化时命中 mtime 快照缓存（不重读文件）；
   状态变化（atomic_json 替写）/ 文件新增 / 文件删除都会使缓存失效。
2. GC 两段式回收：
   - T+24h 压缩：completed+deleted 资源重写为最小墓碑（guard 准入语义
     保留），伴生 .lock 回收；
   - T+7d 退出：墓碑 JSON 整体回收；
   - 保留期内、running、归档、瞬时不可读（OSError）资源不动；
   - 孤儿锁单独回收；损坏 JSON（含二进制垃圾）超退出期回收。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from jiuwenswarm.common import paths as common_paths
from jiuwenswarm.server.runtime.session import lifecycle as lc


@pytest.fixture(autouse=True)
def _reset_lifecycle_global_state():
    """重置 event_snapshots 缓存。GC 节流键为各测试独立的数据目录路径，
    天然互不压占窗口，无需触碰模块级节流时间戳（G.CLS.11 合规）。"""
    lc.clear_snapshot_cache()
    yield
    lc.clear_snapshot_cache()


def _setup_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: root / "sessions")
    # 事件流与 GC 经 common.paths.get_lifecycle_dir 解析 lifecycle 目录，
    # 必须与模块内 seam 打桩到同一 root，两套解析路径才不会分叉。
    monkeypatch.setattr(
        common_paths, "get_lifecycle_dir", lambda: root / "lifecycle"
    )
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

    lc.clear_snapshot_cache()
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

    lc.clear_snapshot_cache()
    running = lc.event_snapshots()
    assert running[0]["completed"] is False

    # completed 写回同一文件（atomic_json tmp+rename，mtime 必变）
    value = lc.read_json(directory / "session_s1.json")
    value["operation"]["status"] = "completed"
    lc.atomic_json(directory / "session_s1.json", value)
    # Windows 文件时间源存在毫秒级量化：连续两次 atomic_json 可能落在同一
    # mtime 桶里导致缓存命中（生产中 2s 轮询间隔远大于量化桶，不可达）。
    # 这里显式拉开 mtime，保证测试确定性。
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

    lc.clear_snapshot_cache()
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
    monkeypatch.setattr(
        common_paths, "get_lifecycle_dir", lambda: root_a / "lifecycle"
    )
    assert lc.event_snapshots()[0]["completed"] is False


# ---------- events_feed：GC 两段式回收 ----------


def test_gc_compresses_dead_resource_to_tombstone(
    monkeypatch, tmp_path: Path
) -> None:
    """T+24h：死删除资源压成最小墓碑，锁回收，guard 语义保留。

    断言覆盖三段检视发现过的两个缺陷（曾因断言只查压缩器常量字段而漏检）：
    1. 先删后压导致墓碑字段全部归零——必须断言原始 operation_id/revision/
       generation/resource_id 原样保留；
    2. 墓碑缺 operation.project_id——事件流的项目归属回退 default。
    """
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    path = directory / "session_dead.json"
    original = dict(
        revision=7,
        generation=3,
        deleted=True,
        blocked=True,
        write_blocked=True,
        operation=dict(
            operation_id="op-abc123",
            resource_type="session",
            resource_id="dead",
            kind="delete",
            status="completed",
            project_id="proj_42",
            channel_id="web",
            revision=7,
        ),
    )
    lc.atomic_json(path, original)
    lock = path.with_name(path.name + ".lock")
    lock.write_bytes(b"\x00")
    stamp = time.time() - (lc.GC_TOMBSTONE_COMPRESS_AFTER + 3600)
    os.utime(path, (stamp, stamp))

    lc.maybe_sweep()
    # JSON 保留（墓碑），锁回收。
    assert path.exists()
    assert not lock.exists()
    # 压缩为最小墓碑：准入语义字段保留，且原始字段不归零、不丢失。
    value = lc.read_json(path)
    assert value["tombstone"] is True
    assert value["deleted"] is True
    assert value["blocked"] is True
    assert value["revision"] == 7
    assert value["generation"] == 3
    assert value["operation"]["operation_id"] == "op-abc123"
    assert value["operation"]["resource_id"] == "dead"
    assert value["operation"]["revision"] == 7
    assert value["operation"]["project_id"] == "proj_42"
    assert value["operation"]["channel_id"] == "web"
    assert "result" not in value["operation"]
    # 墓碑仍被 guard 语义消费：state() 读取后 deleted=True 拦截生效。
    state = lc.state("session", "dead")
    assert state.get("deleted") is True
    # 事件流（watcher 消费）的项目归属不回退 default。
    lc.clear_snapshot_cache()
    events = lc.event_snapshots()
    assert events and events[0]["payload"]["project_id"] == "proj_42"


def test_gc_expires_tombstone_after_seven_days(
    monkeypatch, tmp_path: Path
) -> None:
    """T+7d：墓碑退出，文件整体回收。"""
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    # 先压缩
    path = _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_COMPRESS_AFTER + 3600,
        with_lock=True,
    )
    lc.maybe_sweep()
    assert path.exists()  # 已是墓碑

    # 墓碑的 mtime 被 GC 重置（atomic_json 压缩写），把它拨到退出期外。
    # 生产中两轮 GC 间隔 6h，这里用显式 now 推进到下一轮节流窗口之外。
    old = time.time() - (lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600)
    os.utime(path, (old, old))
    lc.maybe_sweep(now=time.time() + 6 * 3600 + 1)
    assert not path.exists()  # 墓碑退出
    assert not path.with_name(path.name + ".lock").exists()


def test_gc_reclaims_very_old_dead_resource_directly(
    monkeypatch, tmp_path: Path
) -> None:
    """超过退出期的非墓碑死资源直接回收，不经过压缩中间态。"""
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,
        with_lock=True,
    )

    lc.maybe_sweep()
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
        mtime_age=60.0,  # 远小于压缩期
        with_lock=True,
    )
    # 归档夹具必须对齐生产形态：complete(archived=True) 写的是
    # blocked=True/deleted=False/kind=archive——夹具用 blocked=False 会对
    # "归档不回收"给出假保证（检视意见 2②）。
    archived = _write_resource(
        directory,
        "session_archived.json",
        status="completed",
        kind="archive",
        deleted=False,
        blocked=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,
        with_lock=True,
    )

    lc.maybe_sweep()
    assert running.exists()
    assert fresh_dead.exists()  # 压缩期内不回收：watcher 可能还没取走最终 revision
    assert archived.exists()  # 归档不是死数据：blocked=True 也不判死
    for p in (running, fresh_dead, archived):
        assert p.with_name(p.name + ".lock").exists()


def test_gc_keeps_real_archived_record_recoverable(
    monkeypatch, tmp_path: Path
) -> None:
    """归档记录（生产形态：blocked=True, deleted=False）不被 GC 破坏。

    三轮检视问题 1 实测复现过：死亡判据若把 blocked 当死亡标志，归档记录
    在 24h~7d 窗口内被压成 deleted 墓碑（begin(unarchive) 抛 NOT_FOUND），
    7d 后被整删。此处用真实 complete(archived=True) 形态的数据断言两个
    时间窗内归档记录与 unarchive 可达性都完好。
    """
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    path = directory / "session_arch.json"
    archived_record = dict(
        revision=5,
        generation=2,
        blocked=True,
        write_blocked=True,
        deleted=False,
        operation=dict(
            operation_id="op-arch",
            resource_type="session",
            resource_id="arch",
            kind="archive",
            status="completed",
            project_id="proj_1",
            revision=5,
        ),
    )
    lc.atomic_json(path, archived_record)

    # 窗口一：过压缩期（24h~7d）。
    stamp = time.time() - (lc.GC_TOMBSTONE_COMPRESS_AFTER + 3600)
    os.utime(path, (stamp, stamp))
    lc.maybe_sweep()
    value = lc.read_json(path)
    assert value.get("deleted") is False  # 没有被压成 deleted 墓碑
    assert value.get("tombstone") is None
    assert value.get("operation", {}).get("kind") == "archive"
    operation = lc.begin("session", "arch", "unarchive")
    assert operation.get("kind") == "unarchive"  # 取消归档仍可达

    # 窗口二：过退出期（7d+）。begin 之后 operation 变 running——
    # 重新写回 completed 归档形态再测。
    value = lc.read_json(path)
    value["operation"].update(kind="archive", status="completed")
    lc.atomic_json(path, value)
    stamp = time.time() - (lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600)
    os.utime(path, (stamp, stamp))
    lc.maybe_sweep(now=time.time() + 6 * 3600 + 1)
    assert path.exists()  # 归档记录不被删除
    value = lc.read_json(path)
    assert value.get("deleted") is False


def test_gc_skips_transiently_unreadable_resource(monkeypatch, tmp_path: Path) -> None:
    """瞬时 OSError（共享冲突/杀软握持）不判死：宁可漏删不可误删。

    read_json 的 PermissionError 重试（_SHARING_RETRY_ATTEMPTS 次）耗尽后
    抛 OSError——存活资源绝不能因此丢执行栅栏。
    """
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    path = _write_resource(
        directory, "session_alive.json", status="running", with_lock=True
    )

    real_read_json = lc.read_json

    def flaky_read_json(p: Path) -> dict:
        if p == path:
            raise PermissionError(13, "Permission denied", str(p))
        return real_read_json(p)

    monkeypatch.setattr(lc, "read_json", flaky_read_json)
    lc.maybe_sweep()
    # 存活资源：即使读失败也绝不能删。
    assert path.exists()
    assert path.with_name(path.name + ".lock").exists()


def test_gc_skips_transiently_unreadable_dead_resource(
    monkeypatch, tmp_path: Path
) -> None:
    """瞬时 OSError 的**死资源**也跳过（检视意见 3④：running 用例的断言
    靠存活分支兜底，覆盖不到"瞬时不可读不判死"的本体——死资源 + 读失败
    才是这个语义的核心场景：误判可删 → 直接删除带终态的记录）。
    """
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    path = _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,  # 已到退出期
        with_lock=True,
    )

    real_read_json = lc.read_json

    def flaky_read_json(p: Path) -> dict:
        if p == path:
            raise PermissionError(13, "Permission denied", str(p))
        return real_read_json(p)

    monkeypatch.setattr(lc, "read_json", flaky_read_json)
    lc.maybe_sweep()
    # 死资源 + 瞬时不可读：宁可漏删（下轮 GC 再收），不可本轮误删。
    assert path.exists()
    assert path.with_name(path.name + ".lock").exists()


def test_gc_reclaims_orphan_lock_without_json(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    orphan = directory / "session_gone.json.lock"
    orphan.write_bytes(b"\x00")

    lc.maybe_sweep()
    assert not orphan.exists()


def test_gc_is_throttled_by_interval(monkeypatch, tmp_path: Path) -> None:
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,
        with_lock=True,
    )

    lc.maybe_sweep()
    assert not (directory / "session_dead.json").exists()

    # 间隔内再死一个新资源，不会被立即回收（节流）。
    _write_resource(
        directory,
        "session_dead2.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,
        with_lock=True,
    )
    lc.maybe_sweep()
    assert (directory / "session_dead2.json").exists()
    assert (directory / "session_dead2.json.lock").exists()


def test_gc_survives_broken_json(monkeypatch, tmp_path: Path) -> None:
    """损坏 JSON 超退出期按死文件回收。"""
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    broken = directory / "session_broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    old = time.time() - (lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600)
    os.utime(broken, (old, old))

    lc.maybe_sweep()
    assert not broken.exists()


def test_gc_survives_binary_garbage_and_sweeps_rest(
    monkeypatch, tmp_path: Path
) -> None:
    # 非 UTF-8 的二进制垃圾（杀软截断/磁盘坏块）抛 UnicodeDecodeError，
    # 不是 JSONDecodeError：GC 必须回收它且不中止本轮对后续死文件的清理。
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    garbage = directory / "session_a_garbage.json"
    garbage.write_bytes(b"\x80\x81\x9f\xfe\xff\x00binary")
    dead_after = _write_resource(
        directory,
        "session_b_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600,
        with_lock=True,
    )
    old = time.time() - (lc.GC_TOMBSTONE_EXPIRE_AFTER + 3600)
    os.utime(garbage, (old, old))

    lc.maybe_sweep()
    assert not garbage.exists()
    assert not dead_after.exists()  # 垃圾文件排在字母序前面，不得阻断后续回收
    assert not (directory / "session_b_dead.json.lock").exists()


def test_gc_tombstone_keeps_guard_semantics(
    monkeypatch, tmp_path: Path
) -> None:
    """墓碑存在期间，guard()/begin() 对已删 id 的拦截持续有效。"""
    root = _setup_root(monkeypatch, tmp_path)
    directory = _resource_dir(root)
    _write_resource(
        directory,
        "session_dead.json",
        status="completed",
        deleted=True,
        mtime_age=lc.GC_TOMBSTONE_COMPRESS_AFTER + 3600,
        with_lock=True,
    )
    lc.maybe_sweep()

    # guard：已删除 → NOT_FOUND "permanently deleted"
    with pytest.raises(lc.LifecycleError) as exc_info:
        lc.guard(session_id="dead")
    assert exc_info.value.code == "NOT_FOUND"

    # begin（非 delete action）：已删除 → NOT_FOUND
    with pytest.raises(lc.LifecycleError) as exc_info:
        lc.begin("session", "dead", "archive")
    assert exc_info.value.code == "NOT_FOUND"
