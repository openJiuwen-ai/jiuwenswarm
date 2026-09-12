# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
from pathlib import Path


def test_image_tools_loads_only_the_configured_workspace_dotenv() -> None:
    source_path = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "tools"
        / "image_tools.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    calls = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "load_dotenv"
    ]

    assert len(calls) == 1
    dotenv_path = next(
        keyword.value for keyword in calls[0].keywords if keyword.arg == "dotenv_path"
    )
    assert isinstance(dotenv_path, ast.Call)
    assert isinstance(dotenv_path.func, ast.Name)
    assert dotenv_path.func.id == "get_env_file"
    assert not dotenv_path.args
    assert not dotenv_path.keywords
