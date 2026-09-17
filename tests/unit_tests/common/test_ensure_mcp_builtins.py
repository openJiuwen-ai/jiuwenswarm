# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for _ensure_mcp_builtins — zip-seed 预置 MCP 包初始化.

覆盖: 首次解压 / 版本一致跳过 / 版本升级覆盖 / 嵌套层拍平 / overwrite
强制覆盖 / 无种子 zip 时不阻断启动.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.common.utils import (
    CopyDiffResult,
    _ensure_mcp_builtins,
    _find_mcp_builtins_seed,
    _read_zip_index_version,
)


def _make_seed_zip(
    zip_path: Path,
    version: str = "v0.1",
    nested: bool = True,
) -> None:
    """造一个种子 zip; nested=True 时顶层带 mcp_builtins/ 目录."""
    index = {"version": version, "mcps": [
        {"source": "huaweiyun-mcp", "name": "华为云"},
        {"source": "harmonyos-mcp", "name": "鸿蒙"},
    ]}
    files = {
        "index.json": json.dumps(index, ensure_ascii=False),
        "huaweiyun-mcp/mcp.json": '{"mcpServers":{"x":{"url":"http://h/mcp"}}}',
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
# _read_zip_index_version
# ---------------------------------------------------------------------------


def test_read_zip_version_nested(seed_dir: Path) -> None:
    zip_path = seed_dir / "mcp_builtins_v0.1.zip"
    _make_seed_zip(zip_path, version="v0.1", nested=True)
    assert _read_zip_index_version(zip_path) == "v0.1"


def test_read_zip_version_flat(seed_dir: Path) -> None:
    zip_path = seed_dir / "mcp_builtins_v0.2.zip"
    _make_seed_zip(zip_path, version="v0.2", nested=False)
    assert _read_zip_index_version(zip_path) == "v0.2"


def test_read_zip_version_missing_index(seed_dir: Path) -> None:
    zip_path = seed_dir / "mcp_builtins_v0.3.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("foo/bar.txt", "x")
    assert _read_zip_index_version(zip_path) is None


def test_read_zip_version_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip")
    assert _read_zip_version_bad_zip(bad) is None


def _read_zip_version_bad_zip(path: Path) -> None:  # noqa: D401
    """wrapper keeps the call shape identical for the bad-zip case."""
    return _read_zip_index_version(path)


# ---------------------------------------------------------------------------
# _find_mcp_builtins_seed
# ---------------------------------------------------------------------------


def test_find_seed_picks_latest_versioned(seed_dir: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    _make_seed_zip(seed_dir / "mcp_builtins_v0.5.zip", "v0.5")
    found = _find_mcp_builtins_seed(seed_dir)
    assert found is not None
    assert found.name == "mcp_builtins_v0.5.zip"


def test_find_seed_none_when_absent(seed_dir: Path) -> None:
    assert _find_mcp_builtins_seed(seed_dir) is None


# ---------------------------------------------------------------------------
# _ensure_mcp_builtins
# ---------------------------------------------------------------------------


def _new_diff() -> CopyDiffResult:
    return CopyDiffResult([], [], [])


def test_first_install_extracts_nested(seed_dir: Path, tmp_path: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1", nested=True)
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    assert dest.is_dir()
    # 嵌套层已拍平: index.json 直接在 mcp_builtins_dir 下, 不多一层目录.
    assert (dest / "index.json").is_file()
    assert (dest / "huaweiyun-mcp" / "mcp.json").is_file()
    assert not (dest / "mcp_builtins").exists()


def test_first_install_extracts_flat(seed_dir: Path, tmp_path: Path) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1", nested=False)
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    assert (dest / "index.json").is_file()
    assert (dest / "harmonyos-mcp" / "mcp.json").is_file()


def test_first_install_normalizes_backslash_zip_paths(seed_dir: Path, tmp_path: Path) -> None:
    """A zip whose member names use backslashes (Windows Compress-Archive style)
    instead of forward slashes must still extract into a proper directory tree.
    On Linux, ``ZipFile.extractall`` treats backslash as a literal filename
    char, flattening 725 entries into 725 weirdly-named files with zero dirs
    — list_marketplace_mcps then sees zero packages. Our extractor normalizes
    ``\\`` → ``/`` so the layout is correct regardless of how the zip was built.
    """
    index = {"version": "v0.1", "mcps": [
        {"source": "huaweiyun-mcp", "name": "华为云"},
        {"source": "harmonyos-mcp", "name": "鸿蒙"},
    ]}
    files = {
        "index.json": json.dumps(index, ensure_ascii=False),
        "huaweiyun-mcp/mcp.json": '{"mcpServers":{"x":{"url":"http://h/mcp"}}}',
        "harmonyos-mcp/mcp.json": '{"mcpServers":{"y":{"url":"http://hm/mcp"}}}',
    }
    zip_path = seed_dir / "mcp_builtins_v0.1.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            # Write with backslashes — simulates a Windows-built zip.
            zf.writestr(name.replace("/", "\\"), body)
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    # The directory tree must be reconstructed, not flattened into
    # "huaweiyun-mcp\\mcp.json"-named files.
    assert (dest / "index.json").is_file(), "index.json missing"
    assert (dest / "huaweiyun-mcp" / "mcp.json").is_file(), \
        "pkg dir not reconstructed from backslash zip"
    assert (dest / "harmonyos-mcp" / "mcp.json").is_file()
    # And no literal-backslash file should be sitting at top level.
    top_names = [p.name for p in dest.iterdir()]
    assert not any("\\" in n for n in top_names), \
        f"backslash-named file leaked to top level: {top_names}"


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

    # 版本已升, 旧文件被清(整目录覆盖), index.json 反映新版本.
    assert not (dest / "stale-from-v0.1.txt").exists()
    with (dest / "index.json").open(encoding="utf-8") as fh:
        assert json.load(fh)["version"] == "v0.2"


def test_overwrite_forces_reinstall_even_on_version_match(
    seed_dir: Path, tmp_path: Path
) -> None:
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1")
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    (dest / "user-edit.txt").write_text("dirty", encoding="utf-8")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=True, cumulative_diff=_new_diff())

    assert not (dest / "user-edit.txt").exists()
    assert (dest / "index.json").is_file()


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
    _make_seed_zip(seed_dir / "mcp_builtins_v0.1.zip", "v0.1", nested=False)
    dest = tmp_path / "ws" / "mcp" / "mcp_builtins"
    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())
    (dest / "old-marker.txt").write_text("old", encoding="utf-8")
    old_index_mtime = (dest / "index.json").stat().st_mtime_ns

    # 换成损坏种子(version 不一致触发 need_extract,但 zip 本身损坏).
    (seed_dir / "mcp_builtins_v0.1.zip").unlink()
    bad_zip = seed_dir / "mcp_builtins_v0.2.zip"
    bad_zip.write_bytes(b"not a zip, will raise BadZipFile")

    _ensure_mcp_builtins(seed_dir, dest, overwrite=False, cumulative_diff=_new_diff())

    # 旧目录必须原样保留: 脏文件还在, index.json 未被截断/删除.
    assert (dest / "old-marker.txt").read_text(encoding="utf-8") == "old"
    assert (dest / "index.json").is_file()
    assert (dest / "index.json").stat().st_mtime_ns == old_index_mtime
    # 损坏解压不留半残临时目录.
    assert not (dest.parent / f".{dest.name}.tmp.{os.getpid()}").exists()
    # 也不留其它 .tmp 残留.
    tmp_leaks = [p for p in dest.parent.iterdir() if ".tmp." in p.name]
    assert not tmp_leaks, f"temp dir leaked: {tmp_leaks}"
