# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for safe archive tools (inspect_archive, extract_archive)."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.archive_tools import (
    _format_size,
    extract_archive,
    inspect_archive,
)


@pytest.fixture(autouse=True)
def archive_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep extraction inside this test's workspace, regardless of prior SDK state."""
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.cwd.get_workspace", lambda: str(tmp_path),
    )


def _create_sample_zip(tmp_path: Path) -> Path:
    """Create a sample zip archive containing files and subdirectories."""
    zip_path = tmp_path / "test_archive.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("readme.txt", "This is a readme file.")
        zf.writestr("data/metrics.csv", "id,metric,value\n1,cpu,80\n2,mem,65")
    return zip_path


def _create_sample_tar_gz(tmp_path: Path) -> Path:
    """Create a sample tar.gz archive."""
    tar_path = tmp_path / "test_bundle.tar.gz"
    with tarfile.open(str(tar_path), "w:gz") as tf:
        data1 = b"Hello from file 1 in tar"
        ti1 = tarfile.TarInfo("file1.txt")
        ti1.size = len(data1)
        tf.addfile(ti1, io.BytesIO(data1))

        data2 = b"Hello from nested file"
        ti2 = tarfile.TarInfo("nested/file2.txt")
        ti2.size = len(data2)
        tf.addfile(ti2, io.BytesIO(data2))
    return tar_path


def test_format_size_helper() -> None:
    """Verify human-readable size string calculation."""
    assert _format_size(500) == "500 B"
    assert "KB" in _format_size(2048)
    assert "MB" in _format_size(2 * 1024 * 1024)
    assert "GB" in _format_size(2 * 1024 * 1024 * 1024)


@pytest.mark.asyncio
async def test_inspect_zip_success(tmp_path: Path) -> None:
    """Verify inspection of a zip archive."""
    zip_path = _create_sample_zip(tmp_path)
    result = await inspect_archive.invoke({"archive_path": str(zip_path)})

    assert "Format: ZIP" in result
    assert "Files: 2" in result
    assert "readme.txt" in result
    assert "data/metrics.csv" in result


@pytest.mark.asyncio
async def test_inspect_tar_success(tmp_path: Path) -> None:
    """Verify inspection of a tar.gz archive."""
    tar_path = _create_sample_tar_gz(tmp_path)
    result = await inspect_archive.invoke({"archive_path": str(tar_path)})

    assert "Format: TAR" in result
    assert "Files: 2" in result
    assert "file1.txt" in result
    assert "nested/file2.txt" in result


@pytest.mark.asyncio
async def test_inspect_archive_max_items_truncation(tmp_path: Path) -> None:
    """Verify truncation when archive item count exceeds max_items."""
    zip_path = tmp_path / "large_archive.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        for i in range(10):
            zf.writestr(f"item_{i}.txt", f"content {i}")

    result = await inspect_archive.invoke({"archive_path": str(zip_path), "max_items": 3})
    assert "item_0.txt" in result
    assert "more items omitted" in result


@pytest.mark.asyncio
async def test_inspect_archive_path_escape_notice(tmp_path: Path) -> None:
    """Verify notice appears when inspecting suspicious entry names."""
    zip_path = tmp_path / "escaped_archive.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("../parent_file.txt", "untrusted entry")
        zf.writestr("good.txt", "safe entry")

    result = await inspect_archive.invoke({"archive_path": str(zip_path)})
    assert "Note:" in result
    assert "path segments" in result


@pytest.mark.asyncio
async def test_inspect_archive_double_dot_filename_not_suspicious(tmp_path: Path) -> None:
    """Verify filenames with double dots like report..final.txt are not flagged."""
    zip_path = tmp_path / "double_dot.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("report..final.txt", "valid file")
        zf.writestr("normal.txt", "valid file")

    result = await inspect_archive.invoke({"archive_path": str(zip_path)})
    assert "Note:" not in result


@pytest.mark.asyncio
async def test_extract_archive_zip_success(tmp_path: Path) -> None:
    """Verify safe extraction of zip files to a target directory."""
    zip_path = _create_sample_zip(tmp_path)
    extract_target = tmp_path / "extracted_dir"
    result = await extract_archive.invoke({
        "archive_path": str(zip_path),
        "target_dir": str(extract_target),
    })

    assert "Extraction Successful" in result
    assert "Total Files Extracted: 2" in result
    assert (extract_target / "readme.txt").exists()
    assert (extract_target / "readme.txt").read_text() == "This is a readme file."
    assert (extract_target / "data" / "metrics.csv").exists()


@pytest.mark.asyncio
async def test_extract_archive_tar_default_dir(tmp_path: Path) -> None:
    """Verify extraction to default <archive_name>_extracted directory."""
    tar_path = _create_sample_tar_gz(tmp_path)
    result = await extract_archive.invoke({"archive_path": str(tar_path)})

    assert "Extraction Successful" in result
    expected_dir = tar_path.parent / "test_bundle_extracted"
    assert expected_dir.exists()
    assert (expected_dir / "file1.txt").exists()
    assert (expected_dir / "nested" / "file2.txt").exists()


@pytest.mark.asyncio
async def test_extract_archive_path_escape_skipped(tmp_path: Path) -> None:
    """Verify path traversal entry is skipped during extraction."""
    zip_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("../outside_entry.txt", "untrusted content")
        zf.writestr("normal.txt", "normal content")

    dest_dir = tmp_path / "dest"
    result = await extract_archive.invoke({
        "archive_path": str(zip_path),
        "target_dir": str(dest_dir),
    })

    assert "Extraction Successful" in result
    assert (dest_dir / "normal.txt").exists()
    assert not (tmp_path / "outside_entry.txt").exists()


@pytest.mark.asyncio
async def test_extract_archive_zip_bomb_protection(tmp_path: Path) -> None:
    """Verify uncompressed size limit rejection for zip bombs."""
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("large.txt", "a" * 100)

    dest_dir = tmp_path / "dest_bomb"
    with patch(
        "jiuwenswarm.agents.harness.common.tools.archive_tools._MAX_EXTRACTION_BYTES",
        50,
    ):
        result = await extract_archive.invoke({
            "archive_path": str(zip_path),
            "target_dir": str(dest_dir),
        })
        assert "[ERROR]: extract_archive failed:" in result
        assert "exceeds limit" in result


@pytest.mark.asyncio
async def test_extract_archive_tar_unsafe_symlink_skipped(tmp_path: Path) -> None:
    """Verify escaping symlinks in tar archive are safely skipped."""
    tar_path = tmp_path / "symlink_test.tar"
    with tarfile.open(str(tar_path), "w") as tf:
        normal = b"Safe content"
        ti1 = tarfile.TarInfo("safe.txt")
        ti1.size = len(normal)
        tf.addfile(ti1, io.BytesIO(normal))

        ti2 = tarfile.TarInfo("evil_link")
        ti2.type = tarfile.SYMTYPE
        ti2.linkname = "/outside/secret.txt"
        tf.addfile(ti2)

    dest_dir = tmp_path / "dest_tar_sym"
    result = await extract_archive.invoke({
        "archive_path": str(tar_path),
        "target_dir": str(dest_dir),
    })

    assert "Extraction Successful" in result
    assert (dest_dir / "safe.txt").exists()


@pytest.mark.asyncio
async def test_extract_archive_workspace_boundary(tmp_path: Path) -> None:
    """Verify target_dir cannot escape the designated workspace when configured."""
    fake_workspace = tmp_path / "my_workspace"
    fake_workspace.mkdir()
    outside_dir = tmp_path / "outside_workspace"

    zip_path = fake_workspace / "sample.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("file.txt", "hello")

    with patch(
        "openjiuwen.core.sys_operation.cwd.get_workspace",
        return_value=str(fake_workspace),
    ):
        result = await extract_archive.invoke({
            "archive_path": str(zip_path),
            "target_dir": str(outside_dir),
        })
        assert "[ERROR]: extract_archive failed:" in result
        assert "outside runtime workspace" in result
        assert not outside_dir.exists()


@pytest.mark.asyncio
async def test_archive_tools_errors(tmp_path: Path) -> None:
    """Verify error reporting for non-existent and corrupt files."""
    missing = tmp_path / "does_not_exist.zip"
    res_inspect = await inspect_archive.invoke({"archive_path": str(missing)})
    assert "[ERROR]: inspect_archive failed:" in res_inspect

    res_extract = await extract_archive.invoke({"archive_path": str(missing)})
    assert "[ERROR]: extract_archive failed:" in res_extract
