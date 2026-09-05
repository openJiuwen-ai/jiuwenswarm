# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import update_playwright_mcp_runtime as update_runtime


def _write_layout(
    root: Path,
    *,
    generated_lock: bytes,
    bin_files: dict[str, bytes],
) -> Path:
    source = root / "playwright-mcp"
    files = {
        "@playwright/mcp/cli.js": b"console.log('mcp');\n",
        "@playwright/mcp/package.json": b'{"version":"0.0.78"}\n',
        "playwright-core/bin/install_media_pack.ps1": b"runtime powershell\n",
        "playwright-core/bin/launch.cmd": b"runtime cmd\n",
        ".package-lock.json": generated_lock,
    }
    files.update({f".bin/{name}": content for name, content in bin_files.items()})
    files["example/node_modules/.bin/nested"] = generated_lock
    for relative, content in files.items():
        path = source / "node_modules" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return source


def _build_archive(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    output: Path,
) -> bytes:
    monkeypatch.setattr(update_runtime, "SOURCE_DIR", source)
    update_runtime._write_deterministic_zip(output)
    return output.read_bytes()


def test_archive_is_identical_for_synthetic_windows_and_posix_npm_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_source = _write_layout(
        tmp_path / "windows",
        generated_lock=b"windows generated lock\n",
        bin_files={
            "playwright": b"windows shell shim\n",
            "playwright.cmd": b"windows cmd shim\n",
            "playwright.ps1": b"windows powershell shim\n",
        },
    )
    posix_source = _write_layout(
        tmp_path / "posix",
        generated_lock=b"posix generated lock\n",
        bin_files={"playwright": b"../@playwright/mcp/cli.js"},
    )

    windows_archive = tmp_path / "windows.zip"
    posix_archive = tmp_path / "posix.zip"
    windows_bytes = _build_archive(monkeypatch, windows_source, windows_archive)
    posix_bin_link = posix_source / "node_modules" / ".bin" / "playwright"
    original_is_symlink = Path.is_symlink

    def is_posix_symlink(path: Path) -> bool:
        # Model npm's POSIX link without requiring Windows symlink privileges.
        return path == posix_bin_link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_posix_symlink)
    posix_bytes = _build_archive(monkeypatch, posix_source, posix_archive)

    assert windows_bytes == posix_bytes
    with zipfile.ZipFile(windows_archive) as archive:
        names = archive.namelist()
    assert names == [
        "node_modules/@playwright/mcp/cli.js",
        "node_modules/@playwright/mcp/package.json",
        "node_modules/playwright-core/bin/install_media_pack.ps1",
        "node_modules/playwright-core/bin/launch.cmd",
    ]


def test_archive_rejects_symlink_before_treating_it_as_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_layout(
        tmp_path,
        generated_lock=b"generated lock\n",
        bin_files={"playwright": b"ignored shim\n"},
    )
    linked_file = source / "node_modules" / "@playwright" / "mcp" / "linked.js"
    linked_file.write_bytes(b"link target contents\n")
    original_is_file = Path.is_file
    original_is_symlink = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return path == linked_file or original_is_symlink(path)

    def is_file(path: Path) -> bool:
        if path == linked_file:
            pytest.fail("is_file() followed a symlink before it was rejected")
        return original_is_file(path)

    monkeypatch.setattr(update_runtime, "SOURCE_DIR", source)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "is_file", is_file)

    with pytest.raises(RuntimeError, match="Symbolic links.*linked.js"):
        update_runtime._write_deterministic_zip(tmp_path / "runtime.zip")


def test_generated_npm_filter_is_narrow() -> None:
    assert update_runtime._is_generated_npm_path(Path(".package-lock.json"))
    assert update_runtime._is_generated_npm_path(Path(".bin/playwright.cmd"))
    assert update_runtime._is_generated_npm_path(
        Path("dependency/node_modules/.bin/playwright")
    )
    assert not update_runtime._is_generated_npm_path(
        Path("playwright-core/bin/install_media_pack.ps1")
    )
    assert not update_runtime._is_generated_npm_path(
        Path("dependency/.package-lock.json")
    )


def test_npm_install_disables_platform_specific_bin_links() -> None:
    assert "--bin-links=false" in update_runtime.NPM_INSTALL_ARGUMENTS
