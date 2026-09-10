# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_ROOT = _REPOSITORY_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend"
_NOTICE_ROOT = Path("third-party") / "deepseek-harness"
_ARTIFACT_FILES = ("LICENSE", "NOTICE.md", "NOTICE.zh.md")


def _require_tool(name: str) -> str:
    """Return the tool path, skipping the test when the tool is not installed.

    Args:
        name: Executable name to look up on PATH.

    Returns:
        Absolute path to the executable.
    """
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"Required artifact build tool is unavailable: {name}")
    return executable


def _assert_notice_contents(contents: dict[str, str]) -> None:
    license_text = contents["LICENSE"]
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 DeepSeek" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text

    for notice_name in ("NOTICE.md", "NOTICE.zh.md"):
        notice_text = contents[notice_name]
        assert "99f6f02fecdb7dff40c3fbc9470f5907c29f74ca" in notice_text
        assert "packages/client/ui-trajectory" in notice_text
        assert "packages/client/ui-theme/src/styles" in notice_text
        assert "packages/client/ui-primitives/src" in notice_text


def test_trajectory_license_and_notice_ship_in_vite_dist_and_python_wheel(tmp_path: Path) -> None:
    npm = _require_tool("npm")
    subprocess.run(
        [npm, "exec", "--", "vite", "build", "--target", "chrome107"],
        cwd=_FRONTEND_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    public_root = _FRONTEND_ROOT / "public" / _NOTICE_ROOT
    dist_root = _FRONTEND_ROOT / "dist" / _NOTICE_ROOT
    public_contents = {
        artifact_name: (public_root / artifact_name).read_text(encoding="utf-8")
        for artifact_name in _ARTIFACT_FILES
    }
    dist_contents = {
        artifact_name: (dist_root / artifact_name).read_text(encoding="utf-8")
        for artifact_name in _ARTIFACT_FILES
    }
    assert dist_contents == public_contents
    _assert_notice_contents(dist_contents)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel_paths = list(tmp_path.glob("*.whl"))
    assert len(wheel_paths) == 1

    wheel_contents: dict[str, str] = {}
    wheel_prefix = "jiuwenswarm/channels/web/frontend/dist"
    with zipfile.ZipFile(wheel_paths[0]) as wheel:
        for artifact_name in _ARTIFACT_FILES:
            archive_name = f"{wheel_prefix}/{_NOTICE_ROOT.as_posix()}/{artifact_name}"
            wheel_contents[artifact_name] = wheel.read(archive_name).decode("utf-8")
    assert wheel_contents == public_contents
    _assert_notice_contents(wheel_contents)
