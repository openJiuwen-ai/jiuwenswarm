# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""default_socket_path 两级兜底与目录 fail-closed 行为的单测。

macOS 上 tempfile.gettempdir() 解析为 /var/folders/<32>/T（约 49 字节），
第一级兜底路径仍可能超过 AF_UNIX sun_path 预算（100 字节），必须落到
第二级 /tmp。该场景无需真机：monkeypatch gettempdir 返回长路径即可。
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from jiuwenswarm.server.supervisor.worker_process import (
    _make_runtime_dir,
    default_socket_path,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX socket 兜底逻辑")


@pytest.fixture(autouse=True)
def _cleanup_dirs():
    created: list[Path] = []
    yield created
    for directory in created:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_socket_override(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)


def _tmp_dir(prefix: str) -> Path:
    """在字面量 /tmp 下建目录（/tmp 短，长度可预算；不依赖 gettempdir）。"""
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    return directory


def test_short_workspace_keeps_infra_socket_path(monkeypatch, _cleanup_dirs) -> None:
    """infra socket 路径在长度预算内时，不触发兜底。"""
    short_root = _tmp_dir("jw-ws-")
    _cleanup_dirs.append(short_root)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: short_root
    )

    result = default_socket_path()

    assert Path(result).parent == short_root / "infra" / "run"
    assert Path(result).name.startswith("runtime-")
    assert len(os.fsencode(result)) < 100


def test_first_level_fallback_uses_gettempdir(monkeypatch, _cleanup_dirs) -> None:
    """workspace 超长且 gettempdir 较短时，落到第一级兜底目录。"""
    long_workspace = Path(tempfile.mkdtemp(prefix="L" * 90, dir="/tmp"))
    _cleanup_dirs.append(long_workspace)
    short_tmp = _tmp_dir("jw-tmp-")
    _cleanup_dirs.append(short_tmp)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: long_workspace
    )
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(short_tmp))

    result = default_socket_path()

    assert Path(result).parent == short_tmp / f"jiuwenswarm-runtime-{os.getuid()}"
    assert len(os.fsencode(result)) < 100


def test_second_level_fallback_lands_in_tmp_when_gettempdir_long(
    tmp_path, monkeypatch, _cleanup_dirs
) -> None:
    """模拟 macOS：gettempdir 解析为超长 /var/folders/<32>/T，须退回 /tmp。"""
    long_workspace = Path(tempfile.mkdtemp(prefix="L" * 90, dir="/tmp"))
    _cleanup_dirs.append(long_workspace)
    # 模拟 macOS 的长 gettempdir（tmp_path 在 Linux CI 也足够拼出 ≥100 路径）；
    # 该目录须真实存在，mkdir 不会创建父级
    long_tmp = tmp_path / ("T" * 60)
    long_tmp.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: long_workspace
    )
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(long_tmp))

    result = default_socket_path()

    assert Path(result).parent == Path("/tmp") / f"jiuwenswarm-runtime-{os.getuid()}"
    assert len(os.fsencode(result)) < 100


def test_make_runtime_dir_fail_closed_on_symlink(tmp_path) -> None:
    """兜底目录被他人预置为符号链接时必须 fail-closed。"""
    payload = tmp_path / "payload"
    payload.mkdir()
    link = tmp_path / f"jiuwenswarm-runtime-{os.getuid()}"
    link.symlink_to(payload, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unexpected owner"):
        _make_runtime_dir(tmp_path)


def test_make_runtime_dir_fail_closed_on_foreign_owner(tmp_path, monkeypatch) -> None:
    """目录归属者与当前 uid 不一致时必须 fail-closed。"""
    real_uid = os.getuid()
    directory = tmp_path / f"jiuwenswarm-runtime-{real_uid}"
    directory.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 424242)

    with pytest.raises(RuntimeError, match="unexpected owner"):
        _make_runtime_dir(tmp_path)
