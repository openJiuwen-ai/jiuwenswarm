# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The installed designer AgentGroup is refreshed when it differs from the repo copy."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from jiuwenswarm.server.runtime.design.team_bridge import _installed_tree_stale


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


_SOURCE = {"manifest.json": "{}", "tools/node_tools.py": "from x import a\n", "agents/clip/persona/clip.md": "clip"}


def test_identical_copy_is_not_stale(tmp_path: Path) -> None:
    source = _tree(tmp_path / "src", _SOURCE)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _tree(installed, {"tools/__pycache__/node_tools.cpython-313.pyc": "bytecode", "extra.txt": "install marker"})
    assert not _installed_tree_stale(source, installed)


def test_newer_but_different_copy_is_stale(tmp_path: Path) -> None:
    """Another checkout sharing the data dir installed a later-dated, incompatible copy."""
    source = _tree(tmp_path / "src", _SOURCE)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    tool = installed / "tools" / "node_tools.py"
    tool.write_text("from x import image_tool_timeout_seconds\n", encoding="utf-8")
    future = time.time() + 86400
    os.utime(tool, (future, future))
    assert _installed_tree_stale(source, installed)


def test_missing_file_is_stale(tmp_path: Path) -> None:
    source = _tree(tmp_path / "src", _SOURCE)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    (installed / "agents" / "clip" / "persona" / "clip.md").unlink()
    assert _installed_tree_stale(source, installed)


def test_missing_source_is_never_stale(tmp_path: Path) -> None:
    assert not _installed_tree_stale(tmp_path / "nope", tmp_path)
