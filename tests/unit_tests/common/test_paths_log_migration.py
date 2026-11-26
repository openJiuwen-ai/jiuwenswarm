# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""日志归位迁移不得自吸收：数据根 ``logs/`` 顶层文件与兼容链目标是正本，不是待归位的散落日志。

回归背景（JW-86）：``_do_migrate_profile_data_root_layout`` 第 4 步遍历数据根
``logs/`` 时，``stray_logs`` 本身就是 ``get_logs_dir()``——普通文件分支无自吸收
守卫时 old == new，被判定为"内容一致的重复"后 ``unlink``，每次启动清空上一轮
顶层日志（含 20MB × 20 份轮转备份）。同类机理也出现在兼容链上：Windows
junction 的 ``is_symlink()`` 恒为 False 但 ``is_dir()`` 为 True，沿链吸收会把
链目标桶里的正本当重复删除。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import jiuwenswarm.common.paths as paths


def _run_migration() -> None:
    """按新进程启动的语义重置进程级防重标志后执行迁移。"""
    paths._partition_migration_done = False
    paths._legacy_migration_done = False
    paths.migrate_profile_data_root_layout()


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的数据根：独立 home + 重置路径缓存与迁移防重标志（monkeypatch 自动还原）。"""
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setattr(paths, "_user_home", None)
    monkeypatch.setattr(paths, "_workspace_base_dir", None)
    monkeypatch.setattr(paths, "_partition_migration_done", False)
    monkeypatch.setattr(paths, "_legacy_migration_done", False)
    root = paths.get_user_workspace_dir()
    config = root / "config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return root


def _make_junction(link: Path, target: Path) -> bool:
    """创建 Windows junction（无需开发者模式/管理员权限）。

    Returns:
        是否成功创建（非 Windows 或创建失败返回 False，调用方据此跳过用例）。
    """
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return result.returncode == 0 and link.is_dir()


def test_top_level_logs_survive_migration(data_root: Path) -> None:
    """数据根 logs/ 顶层日志与轮转备份迁移后仍在——它们本就在新规范位置。"""
    logs = data_root / "logs"
    logs.mkdir(parents=True)
    (logs / "gateway.log").write_text("previous run gateway log\n" * 50, encoding="utf-8")
    (logs / "gateway.log.1").write_text("rotated backup #1\n" * 100, encoding="utf-8")
    (logs / "agent_server.log").write_text("previous run agent log\n", encoding="utf-8")

    _run_migration()

    assert (logs / "gateway.log").read_text(encoding="utf-8") == "previous run gateway log\n" * 50
    assert (logs / "gateway.log.1").is_file()
    assert (logs / "agent_server.log").is_file()


def test_top_level_logs_survive_repeated_migration(data_root: Path) -> None:
    """第二次启动（迁移再次执行）同样不清空顶层日志。"""
    logs = data_root / "logs"
    logs.mkdir(parents=True)
    for name in ("gateway.log", "channel.log", "full.log", "permissions.log"):
        (logs / name).write_text(f"{name} of this run\n" * 20, encoding="utf-8")
    (logs / "gateway.log.1").write_text("rotated backup\n", encoding="utf-8")

    _run_migration()
    _run_migration()

    for name in ("gateway.log", "channel.log", "full.log", "permissions.log", "gateway.log.1"):
        assert (logs / name).is_file(), f"{name} 被迁移删除"


def test_bucket_logs_survive_migration(data_root: Path) -> None:
    """logs/agent、logs/core 桶内日志迁移后仍在（桶自吸收守卫回归）。"""
    logs = data_root / "logs"
    (logs / "agent").mkdir(parents=True)
    (logs / "core").mkdir(parents=True)
    (logs / "agent" / "ws-dev.log").write_text("bucket agent log\n", encoding="utf-8")
    (logs / "core" / "llm.log").write_text("bucket core log\n", encoding="utf-8")

    _run_migration()

    assert (logs / "agent" / "ws-dev.log").is_file()
    assert (logs / "core" / "llm.log").is_file()


def test_legacy_layout_logs_are_absorbed(data_root: Path) -> None:
    """真实旧布局日志仍正确归位：<root>/agent/.logs → logs/agent，logs/logs → logs/core。"""
    legacy_agent_logs = data_root / "agent" / ".logs"
    legacy_agent_logs.mkdir(parents=True)
    (legacy_agent_logs / "ws-dev.log").write_text("legacy agent log\n", encoding="utf-8")
    legacy_core_logs = data_root / "logs" / "logs"
    legacy_core_logs.mkdir(parents=True)
    (legacy_core_logs / "llm.log").write_text("legacy core log\n", encoding="utf-8")

    _run_migration()

    assert (data_root / "logs" / "agent" / "ws-dev.log").read_text(encoding="utf-8") == "legacy agent log\n"
    assert (data_root / "logs" / "core" / "llm.log").read_text(encoding="utf-8") == "legacy core log\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction 兼容链")
def test_junction_compat_links_do_not_absorb_bucket(data_root: Path) -> None:
    """Windows junction 兼容链不被沿链吸收：桶内正本每次启动都保留。

    前次迁移在 ``profile/agent/.logs`` 与 ``logs/logs`` 留下的 junction 链，
    ``is_symlink()`` 恒为 False——无守卫时第 4 步沿链把 ``logs/agent``、
    ``logs/core`` 桶内正当"重复"删除。
    """
    logs = data_root / "logs"
    agent_bucket = logs / "agent"
    core_bucket = logs / "core"
    agent_bucket.mkdir(parents=True)
    core_bucket.mkdir(parents=True)
    (agent_bucket / "ws-dev.log").write_text("bucket agent log\n", encoding="utf-8")
    (core_bucket / "llm.log").write_text("bucket core log\n", encoding="utf-8")
    profile_agent = data_root / "profile" / "agent"
    profile_agent.mkdir(parents=True)

    assert _make_junction(profile_agent / ".logs", agent_bucket)
    assert _make_junction(logs / "logs", core_bucket)

    _run_migration()

    assert (agent_bucket / "ws-dev.log").is_file(), "logs/agent 桶内正本被沿 junction 链吸收删除"
    assert (core_bucket / "llm.log").is_file(), "logs/core 桶内正本被沿 junction 链吸收删除"
    assert (profile_agent / ".logs").is_dir(), "profile/agent/.logs 兼容链被误删"
    assert (logs / "logs").is_dir(), "logs/logs 兼容链被误删"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction 兼容链")
def test_junction_compat_links_do_not_absorb_business_data(data_root: Path) -> None:
    """业务数据/运行态/process_cli 的 junction 兼容链同样不被自吸收。

    同一机理不限于 logs 桶：``agent``、``workspace``、``config/runtime_state``、
    ``profile/agent/process_cli_logs`` 也是兼容链挂载点，``is_symlink()`` 对
    junction 恒为 False——无守卫时迁移会沿链把 profile 树与各桶正本删除。
    """
    logs = data_root / "logs"
    (logs / "agent").mkdir(parents=True)
    (logs / "core").mkdir()
    (logs / "process_cli").mkdir()
    (logs / "agent" / "a.log").write_text("a\n", encoding="utf-8")
    (logs / "core" / "c.log").write_text("c\n", encoding="utf-8")
    (logs / "process_cli" / "p.log").write_text("p\n", encoding="utf-8")
    profile = data_root / "profile"
    (profile / "agent" / "sessions").mkdir(parents=True)
    (profile / "agent" / "sessions" / "s.json").write_text("{}", encoding="utf-8")
    (profile / "runtime_state").mkdir(parents=True)
    (profile / "runtime_state" / "rt.yaml").write_text("x: 1\n", encoding="utf-8")

    assert _make_junction(profile / "agent" / ".logs", logs / "agent")
    assert _make_junction(logs / "logs", logs / "core")
    assert _make_junction(profile / "agent" / "process_cli_logs", logs / "process_cli")
    assert _make_junction(data_root / "agent", profile / "agent")
    assert _make_junction(data_root / "config" / "runtime_state", profile / "runtime_state")
    assert _make_junction(data_root / "workspace", profile)

    _run_migration()

    assert (logs / "agent" / "a.log").is_file(), "logs/agent 桶内正本被 junction 链吸收删除"
    assert (logs / "core" / "c.log").is_file(), "logs/core 桶内正本被 junction 链吸收删除"
    assert (logs / "process_cli" / "p.log").is_file(), "logs/process_cli 桶内正本被 junction 链吸收删除"
    assert (profile / "agent" / "sessions" / "s.json").is_file(), "profile/agent 数据被 junction 链吸收删除"
    assert (profile / "runtime_state" / "rt.yaml").is_file(), "runtime_state 被 junction 链吸收删除"
    assert (data_root / "workspace").is_dir(), "workspace 兼容链被误删"


def test_absorb_file_degrades_on_locked_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名同内容剪源时 unlink 失败（Windows 共享冲突）仅降级告警，不抛出。

    验证 ``_migrate_absorb_file`` 的尽力而为语义：单个文件被残留进程占用时
    正本保留（下次启动重试），而不是把异常抛到整轮迁移。
    """
    old = tmp_path / "logs" / "logs" / "llm.log"
    new = tmp_path / "logs" / "core" / "llm.log"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("same content\n", encoding="utf-8")
    new.write_text("same content\n", encoding="utf-8")

    real_unlink = Path.unlink

    def _locked_unlink(self: Path, *args, **kwargs):
        if self == old:
            raise PermissionError("file is held by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _locked_unlink)

    paths._migrate_absorb_file(old, new)  # 不应抛出

    assert old.is_file(), "被占用源文件应保留，等待下次启动重试"
    assert new.read_text(encoding="utf-8") == "same content\n"


def test_move_if_absent_degrades_on_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整树剪切失败（源侧被占用）仅告警，不中断整轮迁移。"""
    old = tmp_path / ".logs"
    old.mkdir()
    (old / "a.log").write_text("a\n", encoding="utf-8")
    new = tmp_path / "logs" / "agent"

    def _locked_move(*args, **kwargs):
        raise PermissionError("directory is held by another process")

    monkeypatch.setattr(paths.shutil, "move", _locked_move)

    paths._move_if_absent(old, new)  # 不应抛出

    assert (old / "a.log").is_file(), "剪切失败时源数据必须保留"


def test_business_data_migration_unaffected(data_root: Path) -> None:
    """日志守卫不影响既有业务迁移：agent/ 整树迁入 profile/agent。"""
    session = data_root / "agent" / "sessions" / "sess_demo"
    session.mkdir(parents=True)
    (session / "history.json").write_text("[]", encoding="utf-8")
    (data_root / "logs").mkdir()

    _run_migration()

    migrated = data_root / "profile" / "agent" / "sessions" / "sess_demo" / "history.json"
    assert migrated.is_file()
