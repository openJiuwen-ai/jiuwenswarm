"""Hot-load path guards must reject package escapes, including sidecars."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.auto_harness.service import (
    validate_harness_config,
    validate_harness_config_paths,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_validate_rejects_sidecar_tools_yaml_absolute_file(tmp_path: Path) -> None:
    """Clean harness_config.yaml must not hide escapes in tools/tools.yaml."""
    package = tmp_path / "pkg"
    _write(package / "harness_config.yaml", "name: p\nid: p\n")
    _write(
        package / "tools" / "tools.yaml",
        "tools:\n  - file: /tmp/evil.py\n    class: Evil\n",
    )

    with pytest.raises(ValueError, match=r"escapes package|/tmp/evil|安全校验"):
        validate_harness_config(package / "harness_config.yaml", package_dir=package)


def test_validate_rejects_resources_skills_dirs_outside_package(tmp_path: Path) -> None:
    """resources.skills.dirs must be bounded after loader expands dirs → dir."""
    package = tmp_path / "pkg"
    outside = tmp_path / "outside_skills"
    outside.mkdir()
    outside_posix = outside.resolve().as_posix()
    _write(
        package / "harness_config.yaml",
        "name: p\nid: p\nresources:\n  skills:\n"
        f"    dirs: ['{outside_posix}']\n",
    )

    with pytest.raises(ValueError, match=r"escapes package|安全校验|Invalid package-local"):
        validate_harness_config(package / "harness_config.yaml", package_dir=package)


def test_validate_rejects_params_file_path_escape(tmp_path: Path) -> None:
    """harness.tool.file params.file_path must not load package-outside .py."""
    package = tmp_path / "pkg"
    _write(
        package / "harness_config.yaml",
        "name: p\nid: p\ntools:\n"
        "  - type: harness.tool.file\n"
        "    params:\n"
        "      file_path: /tmp/evil.py\n"
        "      class_name: Evil\n",
    )

    with pytest.raises(ValueError, match=r"escapes package|/tmp/evil|安全校验"):
        validate_harness_config_paths(
            package / "harness_config.yaml",
            package_dir=package,
        )


def test_validate_allows_in_package_sidecar_tool(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write(package / "harness_config.yaml", "name: p\nid: p\n")
    _write(package / "tools" / "ok.py", "class Ok:\n    pass\n")
    _write(
        package / "tools" / "tools.yaml",
        "tools:\n  - file: tools/ok.py\n    class: Ok\n",
    )

    validate_harness_config(package / "harness_config.yaml", package_dir=package)
