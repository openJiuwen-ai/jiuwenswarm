# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""基线 diff 产物检测（WorkspaceBaselineState / _detect_via_baseline 等）UT。

覆盖 P0（并发去重、超时降级、懒建分支矩阵、diff 核心语义、临时文件排除）
与 P1（extract_effective_project_dir 边界、metadata 副本解析链、基线局部刷新）。
"""

from __future__ import annotations

import asyncio
import tempfile
import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Protobuf gencode version.*",
)

import os
import time
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    BASELINE_SNAPSHOT_TIMEOUT_S,
    TaskExecutionRail,
    WorkspaceBaselineState,
    _detect_via_baseline,
    _diff_snapshot,
    _refresh_baseline_entries,
    _snapshot_workspace,
    extract_effective_project_dir,
    update_baseline_after_hook,
)

_SNAPSHOT_MODULE = (
    "jiuwenswarm.agents.harness.common.rails.task_execution_rail"
)


def _patch_snapshot(monkeypatch, fn) -> dict:
    """替换 _snapshot_workspace 并返回调用计数器。"""
    calls = {"n": 0}

    def wrapper(base, cancel_event=None):
        calls["n"] += 1
        return fn(base, cancel_event)

    monkeypatch.setattr(f"{_SNAPSHOT_MODULE}._snapshot_workspace", wrapper)
    return calls


# ---------------------------------------------------------------------------
# P0: WorkspaceBaselineState.ensure —— 并发去重与超时降级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_concurrent_only_one_snapshot(tmp_path, monkeypatch) -> None:
    """并行 N 个 bash 的 ensure：仅建一次快照，等待者复用（双检锁）。"""

    def fake_snapshot(base, cancel_event=None):
        time.sleep(0.05)  # 拉长快照窗口，放大并发窗口
        return _snapshot_workspace(Path(base), cancel_event)

    calls = _patch_snapshot(monkeypatch, fake_snapshot)
    state = WorkspaceBaselineState()
    await asyncio.gather(*[
        state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path)
        for _ in range(3)
    ])
    assert calls["n"] == 1, "并行 ensure 应只建一次快照"
    assert state.snapshot is not None
    assert not state.disabled

    # 快路径：基线已建后再次调用不碰锁、不重建
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ensure_snapshot_none_disables(tmp_path, monkeypatch) -> None:
    """快照返回 None（超限/扫描失败降级）：一次即置禁用，后续 bash 不重扫。"""

    def fake_snapshot(base, cancel_event=None):
        return None  # 模拟 MAX_SCAN_FILES 超限 / 扫描异常降级

    calls = _patch_snapshot(monkeypatch, fake_snapshot)
    state = WorkspaceBaselineState()
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path)
    assert state.snapshot is None
    assert state.disabled is True
    # 禁用后再次调用：快路径直接返回，不重复扫描
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path)
    assert calls["n"] == 1, "快照失败禁用后不应重试扫描"


@pytest.mark.asyncio
async def test_ensure_timeout_disables_and_waiter_degrades(
    tmp_path, monkeypatch
) -> None:
    """首建者超时置 disabled 后，等待者拿锁双检直接降级，不重复扫描。"""

    def slow_snapshot(base, cancel_event=None):
        time.sleep(BASELINE_SNAPSHOT_TIMEOUT_S + 2)
        return {}

    calls = _patch_snapshot(monkeypatch, slow_snapshot)
    state = WorkspaceBaselineState()
    await asyncio.gather(
        state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path),
        state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path),
    )
    assert state.disabled is True
    assert state.snapshot is None
    assert calls["n"] == 1, "等待者不应重复发起快照扫描"
    # 禁用后再次调用：快路径直接返回（不碰锁、不扫描）
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=tmp_path)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# P0: ensure 分支矩阵 —— 非 bash 跳过 / invoke_tool 解包 / workspace_base 缺失跳过
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_workspace_switch_rebuilds(tmp_path_factory, monkeypatch) -> None:
    """工作区切换（W1->W2）触发基线重建；同 base 再调不重建；回 W1 也重建。"""
    w1 = tmp_path_factory.mktemp("w1")
    w2 = tmp_path_factory.mktemp("w2")
    (w1 / "old.txt").write_text("w1")
    (w2 / "other.txt").write_text("w2")

    calls = _patch_snapshot(
        monkeypatch, lambda base, cancel_event=None: _snapshot_workspace(base)
    )
    state = WorkspaceBaselineState()
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w1)
    assert calls["n"] == 1
    assert state.snapshot is not None
    assert "old.txt" in state.snapshot
    assert state.snapshot_base is not None

    # 切到 W2：重建，基线反映 W2 的内容（不能用 W1 基线 diff W2）
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w2)
    assert calls["n"] == 2, "工作区切换应重建基线"
    assert state.snapshot is not None
    assert "other.txt" in state.snapshot
    assert "old.txt" not in state.snapshot

    # 同 base 再次调用：快路径直接返回，不重建
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w2)
    assert calls["n"] == 2

    # 回 W1：当前记录的是 W2，仍视为切换，重建
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w1)
    assert calls["n"] == 3
    assert "old.txt" in state.snapshot


@pytest.mark.asyncio
async def test_ensure_workspace_switch_resets_disabled(
    tmp_path_factory, monkeypatch
) -> None:
    """W1 失败禁用后切 W2：disabled 重置重新尝试；W2 也失败则再次禁用。"""
    w1 = tmp_path_factory.mktemp("w1")
    w2 = tmp_path_factory.mktemp("w2")

    calls = _patch_snapshot(monkeypatch, lambda base, cancel_event=None: None)
    state = WorkspaceBaselineState()
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w1)
    assert state.disabled is True
    assert state.snapshot_base == w1

    # 切到 W2：disabled 随切换重置，重新尝试
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w2)
    assert state.disabled is True, "W2 也失败，应再次禁用"
    assert calls["n"] == 2, "切换后应重新扫描一次"
    assert state.snapshot_base == w2, "禁用结论应归属 W2"

    # W2 内再次调用：快路径命中（禁用且同 base），不重扫
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w2)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_ensure_switch_concurrent_single_rebuild(
    tmp_path_factory, monkeypatch
) -> None:
    """W1 基线已存在时并行多个 ensure(W2)：双检带 base 比较，只重建一次。"""
    w1 = tmp_path_factory.mktemp("w1")
    w2 = tmp_path_factory.mktemp("w2")

    def slow_snapshot(base, cancel_event=None):
        time.sleep(0.05)  # 拉长窗口，放大并发竞态
        return _snapshot_workspace(base, cancel_event)

    calls = _patch_snapshot(monkeypatch, slow_snapshot)
    state = WorkspaceBaselineState()
    await state.ensure("bash", {}, log_prefix="[T]", workspace_base=w1)
    assert calls["n"] == 1

    await asyncio.gather(*[
        state.ensure("bash", {}, log_prefix="[T]", workspace_base=w2)
        for _ in range(3)
    ])
    assert calls["n"] == 2, "并行切换请求应只重建一次"
    assert state.snapshot is not None
    assert state.snapshot_base == w2


@pytest.mark.asyncio
async def test_ensure_skips_non_exec_tool(tmp_path, monkeypatch) -> None:
    """write_file 等非 CODE_EXEC 工具不建基线（走文本提取路径）。"""
    calls = _patch_snapshot(monkeypatch, lambda base, cancel_event=None: {})
    state = WorkspaceBaselineState()
    await state.ensure("write_file", {"path": "a.py"}, log_prefix="[T]")
    await state.ensure("edit_file", {}, log_prefix="[T]")
    await state.ensure("todo_create", {}, log_prefix="[T]")
    assert calls["n"] == 0
    assert state.snapshot is None

    # invoke_tool 解包出 bash 时应建立基线
    await state.ensure(
        "invoke_tool",
        {"tool_name": "bash", "command": "ls"},
        log_prefix="[T]",
        workspace_base=tmp_path,
    )
    assert calls["n"] == 1
    assert state.snapshot is not None


@pytest.mark.asyncio
async def test_ensure_no_workspace_base_skips(monkeypatch) -> None:
    """workspace_base=None 且 ContextVar 回退也为 None 时跳过（不建基线不报错）。"""
    calls = _patch_snapshot(monkeypatch, lambda base, cancel_event=None: {})
    monkeypatch.setattr(
        f"{_SNAPSHOT_MODULE}.resolve_workspace_base", lambda: None
    )
    state = WorkspaceBaselineState()
    await state.ensure("bash", {}, log_prefix="[T]")
    assert calls["n"] == 0
    assert state.snapshot is None
    assert not state.disabled


# ---------------------------------------------------------------------------
# P0: _detect_via_baseline —— diff 核心语义
# ---------------------------------------------------------------------------


def test_detect_via_baseline_new_file() -> None:
    """新增文件被检出，且出现在本次快照中（供 hook 后局部刷新）。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        (base / "old.txt").write_text("old")
        baseline = _snapshot_workspace(base)
        assert baseline is not None
        (base / "new.png").write_bytes(b"png")
        candidates, snapshot = _detect_via_baseline(baseline, base)
        assert candidates == [str(base / "new.png")]
        assert snapshot is not None
        assert "new.png" in snapshot


def test_detect_via_baseline_unchanged_not_reported() -> None:
    """未变化文件不检出。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        (base / "a.txt").write_text("a")
        baseline = _snapshot_workspace(base)
        assert baseline is not None
        candidates, _ = _detect_via_baseline(baseline, base)
        assert candidates == []


def test_diff_snapshot_touch_same_content_not_reported() -> None:
    """mtime 变但内容相同（touch）：基线携带 hash 后 sha256 对比排除误报。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        f = base / "same.txt"
        f.write_text("stable")
        baseline = _snapshot_workspace(base)
        assert baseline is not None
        # 模拟 hook 后基线写回 hash（_refresh_baseline_entries 的效果）
        _refresh_baseline_entries(baseline, base, [str(f)])
        assert baseline["same.txt"][3] is not None
        # touch：mtime 变化，内容不变
        os.utime(f, (time.time() + 10, time.time() + 10))
        candidates = _diff_snapshot(baseline, _snapshot_workspace(base))
        assert candidates == [], "touch（内容未变）不应误报为变更"


def test_detect_via_baseline_changed_file() -> None:
    """内容变更文件被检出，新 hash 写回本次快照（下次基线参照）。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        f = base / "c.txt"
        f.write_text("v1")
        baseline = _snapshot_workspace(base)
        assert baseline is not None
        f.write_text("v2-longer-content")
        candidates, snapshot = _detect_via_baseline(baseline, base)
        assert candidates == [str(f)]
        assert snapshot is not None
        assert snapshot["c.txt"][3] is not None


def test_detect_via_baseline_excludes_temp_files() -> None:
    """临时/锁文件（~$xxx.pptx、.tmp、.swp、.DS_Store）不检出。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        (base / "ok.txt").write_text("ok")
        baseline = _snapshot_workspace(base)
        assert baseline is not None
        for name in ("~$lock.pptx", "x.tmp", "y.swp", ".DS_Store"):
            (base / name).write_bytes(b"x")
        candidates, _ = _detect_via_baseline(baseline, base)
        assert candidates == []


# ---------------------------------------------------------------------------
# P1: extract_effective_project_dir —— 提取边界
# ---------------------------------------------------------------------------


def test_extract_effective_project_dir_boundaries() -> None:
    assert extract_effective_project_dir(None) is None
    assert extract_effective_project_dir("not-a-dict") is None
    assert extract_effective_project_dir({}) is None
    assert extract_effective_project_dir({"effective_project_dir": None}) is None
    assert extract_effective_project_dir({"effective_project_dir": 123}) is None
    assert extract_effective_project_dir({"effective_project_dir": "   "}) is None
    assert extract_effective_project_dir(
        {"effective_project_dir": "  E:\\ws\\proj  "}
    ) == "E:\\ws\\proj"


# ---------------------------------------------------------------------------
# P1: metadata 副本解析链（set_skill_turbo_request_metadata）
# ---------------------------------------------------------------------------


def test_rail_metadata_workspace_base_resolution(tmp_path) -> None:
    """注入 metadata 后 _resolve_metadata_workspace_base 直读工作区。"""
    rail = TaskExecutionRail()
    assert rail._resolve_metadata_workspace_base() is None

    epd = str(tmp_path.resolve())
    rail.set_skill_turbo_request_metadata({"effective_project_dir": epd})
    assert rail._resolve_metadata_workspace_base() == Path(epd).resolve()

    # 空白值：回退 None（不抛异常）
    rail.set_skill_turbo_request_metadata({"effective_project_dir": "  "})
    assert rail._resolve_metadata_workspace_base() is None
    # None / 非 dict 类型容错
    rail.set_skill_turbo_request_metadata(None)
    assert rail._resolve_metadata_workspace_base() is None
    rail.set_skill_turbo_request_metadata("bad")  # type: ignore[arg-type]
    assert rail._resolve_metadata_workspace_base() is None


# ---------------------------------------------------------------------------
# P1: update_baseline_after_hook —— 基线局部刷新
# ---------------------------------------------------------------------------


def test_update_baseline_after_hook_refresh(tmp_path) -> None:
    """hook 原地改写文件后局部刷新：下一轮 diff 不再误报该文件。"""
    base = tmp_path.resolve()
    f = base / "w.png"
    f.write_bytes(b"v1")
    baseline = _snapshot_workspace(base)
    assert baseline is not None

    # 模拟水印 hook 原地改写
    f.write_bytes(b"v1-watermarked")
    refreshed = update_baseline_after_hook(
        baseline, fired=True, paths=[str(f)], workspace_base=base
    )
    assert refreshed is baseline
    candidates, _ = _detect_via_baseline(refreshed, base)
    assert candidates == [], "hook 改写过的文件刷新后不应再被 diff 出"


def test_update_baseline_after_hook_none_passthrough(tmp_path) -> None:
    """snapshot=None（降级路径）返回 None；fired=False/paths 空：原样返回。"""
    assert update_baseline_after_hook(None, True, ["x"]) is None
    base = tmp_path.resolve()
    (base / "a.txt").write_text("a")
    baseline = _snapshot_workspace(base)
    assert baseline is not None
    assert update_baseline_after_hook(
        baseline, fired=False, paths=[], workspace_base=base
    ) is baseline


def test_update_baseline_after_hook_no_workspace_base(tmp_path) -> None:
    """workspace_base=None：跳过刷新，变化保留给下一轮 diff（宁多勿漏）。"""
    base = tmp_path.resolve()
    f = base / "a.txt"
    f.write_text("a")
    baseline = _snapshot_workspace(base)
    assert baseline is not None
    f.write_text("changed")
    result = update_baseline_after_hook(
        baseline, fired=True, paths=[str(f)], workspace_base=None
    )
    assert result is baseline
    candidates, _ = _detect_via_baseline(result, base)
    assert str(f) in candidates
