# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused tests for the ``changeOnBlur`` clamping behaviour of ``ui/Input``.

The actual test logic lives in the frontend ``.mjs`` file so it stays close to
the web project conventions (jsdom + React ``act``).  This Python wrapper
compiles ``Input.tsx`` with esbuild and runs the ``.mjs`` test through
``node --test`` so that pytest — the repository's outermost test runner —
picks it up in a single ``pytest tests/`` invocation, exactly like
``test_trajectory_frontend_artifacts`` already runs ``vite build``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]  # noqa: PTH200
_FRONTEND_ROOT = _REPOSITORY_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend"
_CACHE_DIR = _FRONTEND_ROOT / "node_modules" / ".cache" / "input-change-on-blur"
_TEST_FILE = _FRONTEND_ROOT / "tests" / "inputChangeOnBlur.test.mjs"


def _require_tool(name: str) -> str:
    """Return the tool path, skipping the test when the tool is not installed."""
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"Required tool is unavailable: {name}")
    return executable


def test_input_change_on_blur_clamps_to_min_max_range() -> None:
    node = _require_tool("node")
    npx = _require_tool("npx")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    compile_result = subprocess.run(
        [
            npx,
            "esbuild",
            "src/components/ui/Input/Input.tsx",
            "--bundle",
            "--packages=external",
            "--platform=node",
            "--format=esm",
            f"--outfile={_CACHE_DIR / 'Input.js'}",
            "--loader:.css=empty",
        ],
        cwd=_FRONTEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert compile_result.returncode == 0, (
        f"esbuild failed:\nstdout: {compile_result.stdout}\nstderr: {compile_result.stderr}"
    )

    test_result = subprocess.run(
        [node, "--test", str(_TEST_FILE.relative_to(_FRONTEND_ROOT))],
        cwd=_FRONTEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert test_result.returncode == 0, (
        f"node --test failed:\n{test_result.stdout}\n{test_result.stderr}"
    )
