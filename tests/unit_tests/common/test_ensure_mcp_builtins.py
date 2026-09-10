# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for _ensure_mcp_builtins — zip-seed 预置 MCP 包初始化.

覆盖: 首次解压 / 版本一致跳过 / 版本升级覆盖 / 嵌套层拍平 / overwrite
强制覆盖 / 无种子 zip 时不阻断启动.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
import zipfile
from pathlib import Path

import pytest
import jiuwenswarm.common.utils as common_utils

from jiuwenswarm.common.utils import (
    CopyDiffResult,
    _ensure_mcp_builtins,
    _find_mcp_builtins_seed,
    _print_console_progress,
    _read_mcp_builtins_seed_version,
    mcp_builtins_seed_update_needed,
)


def _make_seed_zip(
    zip_path: Path,
    version: str = "v0.1",
    nested: bool = True,
) -> None:
    """造一个种子 zip; nested=True 时顶层带 mcp_builtins/ 目录."""
    files = {
        ".mcp_builtins_version": version,
        "huaweiyun-mcp/manifest.json": (
            '{"version":"1.0.0","package_type":"mcp","id":"huaweiyun-mcp",'
            '"name":"华为云","description":"云服务",'
            '"display_name":{"zh":"华为云","en":"Huawei Cloud"},'
            '"display_description":{"zh":"云服务","en":"Cloud service"},'
            '"integration":{"type":"remote-mcp","file":"mcp.json"}}'
        ),
        "huaweiyun-mcp/mcp.json": '{"mcpServers":{"x":{"url":"http://h/mcp"}}}',
        "harmonyos-mcp/manifest.json": (
            '{"version":"1.0.0","package_type":"mcp","id":"harmonyos-mcp",'
            '"name":"鸿蒙","description":"鸿蒙服务",'
            '"display_name":{"zh":"鸿蒙","en":"HarmonyOS"},'
            '"display_description":{"zh":"鸿蒙服务","en":"HarmonyOS service"},'
            '"integration":{"type":"remote-mcp","file":"mcp.json"}}'
        ),
        "harmonyos-mcp/mcp.json": '{"mcpServers":{"y":{"url":"http://hm/mcp"}}}',
    }
    prefix = "mcp_builtins/" if nested else ""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(prefix + name, body)


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    """template_agent_workspace 临时目录,含一个种子 zip."""
    d = tmp_path / "tmpl" / "agent" / "workspace"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# _read_mcp_builtins_seed_version
# ---------------------------------------------------------------------------


def test_read_zip_version_nested(seed_dir: Path) -> None:
    zip_path = seed_dir / "mcp_builtins_v0.1.zip"
    _make_seed_zip(zip_path, version="v0.1", nested=True)
    assert _read_mcp_builtins_seed_version(zip_path) == "v0.1"


def test_read_zip_version_requires_internal_marker(seed_dir: Path) -> None:
    zip_path = seed_dir / "mcp_builtins_v0.3.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("foo/bar.txt", "x")
    assert _read_mcp_builtins_seed_version(zip_path) is None


def test_read_zip_version_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip")
    assert _read_mcp_builtins_seed_version(bad) is None


# ---------------------------------------------------------------------------
# _find_mcp_builtins_seed
# ---------------------------------------------------------------------------


def test_find_seed_picks_latest_versioned(seed_dir: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    _make_seed_zip(seed_dir / "mcp_builtins_v0.9.zip", "v0.9")
    _make_seed_zip(seed_dir / "mcp_builtins_v0.10.zip", "v0.10")
    found = _find_mcp_builtins_seed(seed_dir)
    assert found is not None
    assert found.name == "mcp_builtins_v0.10.zip"


def test_find_seed_none_when_absent(seed_dir: Path) -> None:
    assert _find_mcp_builtins_seed(seed_dir) is None


def test_seed_update_needed_when_installed_version_is_stale(
    seed_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_zip = seed_dir / "mcp_builtins_v0.2.zip"
    _make_seed_zip(seed_zip, "v0.2.1")
    workspace = tmp_path / "runtime"
    installed = workspace / "agent" / "workspace" / "mcp" / "mcp_builtins"
    installed.mkdir(parents=True)
    (installed / ".mcp_builtins_version").write_text("v0.2", encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.utils._find_package_root", lambda: tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils._find_mcp_builtins_seed", lambda _path: seed_zip
    )

    assert mcp_builtins_seed_update_needed(workspace) is True


def test_seed_update_not_needed_when_installed_version_matches(
    seed_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_zip = seed_dir / "mcp_builtins_v0.2.zip"
    _make_seed_zip(seed_zip, "v0.2.1")
    workspace = tmp_path / "runtime"
    installed = workspace / "agent" / "workspace" / "mcp" / "mcp_builtins"
    installed.mkdir(parents=True)
    (installed / ".mcp_builtins_version").write_text("v0.2.1", encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.utils._find_package_root", lambda: tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils._find_mcp_builtins_seed", lambda _path: seed_zip
    )

    assert mcp_builtins_seed_update_needed(workspace) is False


def test_runtime_workspace_prepares_when_mcp_seed_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "runtime"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "config.yaml").write_text("{}", encoding="utf-8")
    (workspace / "agent" / "workspace" / "mcp" / "mcp_builtins").mkdir(
        parents=True
    )
    prepared: list[Path] = []

    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: workspace)
    monkeypatch.setattr(common_utils, "cleanup_team_files", lambda _workspace: None)
    monkeypatch.setattr(common_utils, "mcp_builtins_seed_update_needed", lambda _workspace: True)
    monkeypatch.setattr(
        common_utils,
        "prepare_workspace",
        lambda *, overwrite, workspace_dir: prepared.append(workspace_dir),
    )
    monkeypatch.setattr(common_utils, "ensure_config_migrated_from_template", lambda _workspace: None)
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert prepared == [workspace]


# ---------------------------------------------------------------------------
# _ensure_mcp_builtins
# ---------------------------------------------------------------------------


def _new_diff() -> CopyDiffResult:
    return CopyDiffResult([], [], [])


def _capture_progress(encoding: str, message: str) -> bytes:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(
        buffer,
        encoding=encoding,
        errors="strict",
        write_through=True,
    )
    try:
        with redirect_stdout(stream):
            _print_console_progress(message)
        return buffer.getvalue()
    finally:
        stream.detach()


def test_console_progress_preserves_unicode_with_utf8() -> None:
    assert _capture_progress("utf-8", "MCP 预置包") == (
        "MCP 预置包".encode() + os.linesep.encode()
    )


def test_console_progress_escapes_unicode_with_cp1252() -> None:
    assert _capture_progress("cp1252", "MCP 预置包") == (
        b"MCP \\u9884\\u7f6e\\u5305" + os.linesep.encode()
    )


def test_first_install_extracts_nested(seed_dir: Path, tmp_path: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1", nested=True)
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    assert dest.is_dir()
    # 嵌套层已拍平，每个包自己持有 manifest，不多一层目录。
    assert (dest / "huaweiyun-mcp" / "manifest.json").is_file()
    assert not (dest / "index.json").exists()
    assert (dest / "huaweiyun-mcp" / "mcp.json").is_file()
    assert not (dest / "mcp_builtins").exists()


def test_version_match_skips(seed_dir: Path, tmp_path: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    # 先装一次.
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    # 在解出来的目录里塞一个 "脏" 文件, 再调一次应跳过(版本一致), 脏文件应保留.
    (dest / "dirty.txt").write_text("keep me", encoding="utf-8")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    assert (dest / "dirty.txt").read_text(encoding="utf-8") == "keep me"


def test_version_mismatch_upgrades_and_wipes_stale(seed_dir: Path, tmp_path: Path) -> None:
    # 旧版本种子先装.
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    (dest / "stale-from-v0.1.txt").write_text("old", encoding="utf-8")

    # 换成新版本种子: 同名 zip 覆盖写, version 变 v0.2.
    seed_new = seed_dir / "mcp_builtins_v0.2.zip"
    _make_seed_zip(seed_new, "v0.2")
    (seed_dir / "mcp_builtins_v0.1.zip").unlink()  # 旧 zip 移除, 只留新版.

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    # 版本已升，旧文件被清，集合版本记录在包目录内部。
    assert not (dest / "stale-from-v0.1.txt").exists()
    assert (dest / ".mcp_builtins_version").read_text(encoding="utf-8") == "v0.2"


def test_existing_dir_without_internal_marker_is_upgraded(
    seed_dir: Path, tmp_path: Path
) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.2.zip", "v0.2")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    dest.mkdir(parents=True)
    (dest / "legacy.txt").write_text("old format", encoding="utf-8")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    assert not (dest / "legacy.txt").exists()
    assert (dest / ".mcp_builtins_version").read_text(encoding="utf-8") == "v0.2"


def test_overwrite_forces_reinstall_even_on_version_match(
    seed_dir: Path, tmp_path: Path
) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    (dest / "user-edit.txt").write_text("dirty", encoding="utf-8")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=True, cumulative_diff=_new_diff())

    assert not (dest / "user-edit.txt").exists()
    assert (dest / "huaweiyun-mcp" / "manifest.json").is_file()


def test_no_seed_zip_skips_silently(tmp_path: Path) -> None:
    empty_template = tmp_path / "no-seed"
    empty_template.mkdir()
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"

    _ensure_mcp_builtins(
        empty_template, dest, overwrite=False, cumulative_diff=_new_diff()
    )
    # 无种子: 不阻断启动, 目录未创建.
    assert not dest.exists()


def test_bad_seed_zip_preserves_existing_dir(seed_dir: Path, tmp_path: Path) -> None:
    """解压失败时旧目录必须原样保留 —— 原子化的核心保证。

    旧实现 rmtree 旧目录后再解压,中途 BadZipFile 会留下「旧目录已删 +
    新目录半残」状态。原子化后先解压到临时目录,失败只清理临时目录、
    旧目录不动,下次启动可重试。
    """
    # 先用好种子装一份 v0.1,塞个脏文件标记旧内容.
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    (dest / "old-marker.txt").write_text("old", encoding="utf-8")
    old_manifest_mtime = (dest / "huaweiyun-mcp" / "manifest.json").stat().st_mtime_ns

    # 换成损坏种子(version 不一致触发 need_extract,但 zip 本身损坏).
    (seed_dir / "mcp_builtins_v0.1.zip").unlink()
    bad_zip = seed_dir / "mcp_builtins_v0.2.zip"
    bad_zip.write_bytes(b"not a zip, will raise BadZipFile")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    # 旧目录必须原样保留: 脏文件和单包 manifest 未被截断/删除。
    assert (dest / "old-marker.txt").read_text(encoding="utf-8") == "old"
    assert (dest / "huaweiyun-mcp" / "manifest.json").is_file()
    assert (dest / "huaweiyun-mcp" / "manifest.json").stat().st_mtime_ns == old_manifest_mtime
    # 损坏解压不留半残临时目录.
    assert not (dest.parent / f".{dest.name}.tmp.{os.getpid()}").exists()
    # 也不留其它 .tmp 拋留.
    tmp_leaks = [p for p in dest.parent.iterdir() if ".tmp." in p.name]
    assert not tmp_leaks, f"temp dir leaked: {tmp_leaks}"
