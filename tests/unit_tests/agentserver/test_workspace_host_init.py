# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""宿主机 workspace 初始化：marker 跳过 / 空盘 copytree / 部分文件 materialize."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.runtime.agent_adapter.workspace_host_init import (
    host_init_workspace_sync,
)


def _sample_dirs() -> list[dict]:
    return [
        {
            "name": "agent",
            "path": "agent",
            "is_file": False,
            "children": [
                {
                    "name": "SOUL.md",
                    "path": "SOUL.md",
                    "is_file": True,
                    "default_content": "# soul\n",
                }
            ],
        }
    ]


def test_host_init_skips_when_marker_exists(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".workspace").write_text("", encoding="utf-8")
    (root / "noise.txt").write_text("x", encoding="utf-8")

    result = host_init_workspace_sync(str(root), _sample_dirs())
    assert result["status"] == "skipped_marker"
    assert not (root / "agent" / "SOUL.md").exists()


def test_host_init_copytree_on_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    result = host_init_workspace_sync(str(root), _sample_dirs())
    assert result["status"] == "copytree"
    assert (root / ".workspace").is_file()
    assert (root / "agent" / "SOUL.md").read_text(encoding="utf-8") == "# soul\n"


def test_host_init_materialize_when_partial(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")

    result = host_init_workspace_sync(str(root), _sample_dirs())
    assert result["status"] == "materialize"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (root / ".workspace").is_file()
    assert (root / "agent" / "SOUL.md").is_file()
