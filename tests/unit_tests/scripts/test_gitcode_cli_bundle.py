# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内置 gitcode CLI 的打包约定：spec 打入原生二进制，pin 与校验脚本期望版本一致。

连接器 cli.json 的 minVersion 是精确相等比较，内置版本不一致即退回 pip 安装。
"""

from __future__ import annotations

import runpy
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _verifier():
    return runpy.run_path(str(ROOT / "scripts/verify_gitcode_cli_bundle.py"))


def test_pyinstaller_bundle_stages_the_gitcode_binary():
    spec = (ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")

    assert "from gc_cli.wrapper import get_binary_path" in spec
    assert '_bundled_binaries.append((_gc_stage, "."))' in spec


def test_gitcode_bundle_verifier_refuses_to_pass_outside_the_frozen_exe(monkeypatch):
    verifier = _verifier()
    monkeypatch.delattr(sys, "frozen", raising=False)

    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier["verify_gitcode_cli_bundle"]()


def test_dependencies_pins_the_version_the_frozen_verifier_expects():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = _verifier()["EXPECTED_VERSION"]

    pins = [
        requirement
        for requirement in pyproject["project"]["dependencies"]
        if requirement.startswith("gitcode-cli")
    ]

    assert pins == [f"gitcode-cli=={expected}"]


def test_all_desktop_builds_run_frozen_gitcode_verifier():
    for name in ("build-exe.ps1", "build-exe.bat", "build-macos.sh"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")

        assert "verify_gitcode_cli_bundle.py" in source
