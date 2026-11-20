"""Verify the packaging helper without executing system plugin entries."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import shutil
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import pytest
from openjiuwen.core.extension import ExtensionFailure

SKILL_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def builder() -> ModuleType:
    path = SKILL_ROOT / "scripts" / "build_package.py"
    spec = importlib.util.spec_from_file_location("package_builder_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path: Path) -> Path:
    target = tmp_path / "package source with spaces"
    shutil.copytree(Path(__file__).resolve().parent / "greeting_package", target)
    return target


def test_manifest_is_at_zip_root_and_content_is_repeatable(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    first = builder.build_package(source, tmp_path / "first.zip")
    second = builder.build_package(source, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert "manifest.json" in archive.namelist()
        assert "greeting/extension.py" in archive.namelist()
        assert archive.testzip() is None


def test_cache_and_environment_directories_are_excluded(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    for name in ("__pycache__", ".venv", ".git"):
        directory = source / name
        directory.mkdir(exist_ok=True)
        (directory / "local.py").write_text("local only", encoding="utf-8")
    result = builder.build_package(source, tmp_path / "clean.zip")
    with ZipFile(result) as archive:
        assert all(not any(part in {"__pycache__", ".venv", ".git"} for part in Path(name).parts)
                   for name in archive.namelist())


def test_existing_output_requires_explicit_overwrite(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    output = tmp_path / "existing.zip"
    output.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        builder.build_package(source, output)
    assert output.read_bytes() == b"original"
    builder.build_package(source, output, overwrite=True)
    with ZipFile(output) as archive:
        assert "manifest.json" in archive.namelist()


def test_output_inside_source_is_refused(builder: ModuleType, source: Path) -> None:
    with pytest.raises(ValueError, match="源码目录之外"):
        builder.build_package(source, source / "recursive.zip")


@pytest.mark.parametrize("package_id", ["has space", "a..b", "../escape"])
def test_invalid_manifest_is_not_packaged(
    builder: ModuleType, source: Path, tmp_path: Path, package_id: str,
) -> None:
    path = source / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["package"]["id"] = package_id
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ExtensionFailure):
        builder.build_package(source, tmp_path / "invalid.zip")
    assert not (tmp_path / "invalid.zip").exists()


def test_entry_is_validated_but_never_executed(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist.txt"
    (source / "greeting" / "extension.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    builder.build_package(source, tmp_path / "no-execution.zip")
    assert not marker.exists()


def test_missing_entry_is_rejected(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    (source / "greeting" / "extension.py").unlink()
    with pytest.raises(ExtensionFailure):
        builder.build_package(source, tmp_path / "missing.zip")


def test_local_environment_file_is_not_silently_shipped(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    (source / ".env").write_text("LOCAL_ONLY=yes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="本地环境配置"):
        builder.build_package(source, tmp_path / "env.zip")


def test_environment_template_can_be_shipped(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    (source / ".env.example").write_text("EXAMPLE_SETTING=\n", encoding="utf-8")
    result = builder.build_package(source, tmp_path / "with-template.zip")
    with ZipFile(result) as archive:
        assert ".env.example" in archive.namelist()


def test_non_zip_output_is_rejected(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=".zip 后缀"):
        builder.build_package(source, tmp_path / "not-a-zip.txt")


def test_excluded_entry_is_not_silently_omitted(builder: ModuleType, source: Path, tmp_path: Path) -> None:
    entry = source / "node_modules" / "extension.py"
    entry.parent.mkdir()
    entry.write_text("def create_extension(): return object()\n", encoding="utf-8")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extensions"][0]["module"] = "node_modules/extension.py"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="入口未包含"):
        builder.build_package(source, tmp_path / "missing-entry.zip")


def test_cli_json_supports_chinese_output_path(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "系统插件成品.zip"
    completed = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / "build_package.py"), str(source), str(output)],
        cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8")
    result = json.loads(completed.stdout)
    assert result["zip"] == str(output) and output.is_file()
