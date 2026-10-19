# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import migrate_xiaoyi_agent_groups as migration


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _source_group(tmp_path: Path) -> Path:
    root = tmp_path / "family-learning-companion"
    _write_json(
        root / "manifest.json",
        {
            "schemaVersion": "0.0.1",
            "package_type": "agent_group",
            "name": "family-learning-companion",
            "description": "小艺 Work 学习陪伴",
            "agents": ["leader"],
            "skills": [],
        },
    )
    _write_json(
        root / "agents" / "leader" / "manifest.json",
        {
            "schemaVersion": "0.0.1",
            "packageType": "agent_template",
            "name": "leader",
            "description": "主理人",
            "persona": {"dir": "persona"},
            "skills": [],
        },
    )
    persona = root / "agents" / "leader" / "persona" / "ROLE.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("# 主理人\n", encoding="utf-8")
    (root / "README.md").write_text("# 原始详情\n\n保留这段说明。\n", encoding="utf-8")
    return root


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_complete_pipeline_emits_importable_single_root_zip(tmp_path: Path) -> None:
    source = _source_group(tmp_path)
    output = tmp_path / "output"

    report = migration.migrate_sources(
        [source],
        output_dir=output,
        validate_runtime=False,
    )

    archive = output / "family-learning-companion.zip"
    assert report["groupCount"] == 1
    assert archive.is_file()
    assert (output / "migration-report.json").is_file()
    with zipfile.ZipFile(archive) as package:
        roots = {Path(name).parts[0] for name in package.namelist() if name}
        assert roots == {"family-learning-companion"}
        manifest = json.loads(package.read("family-learning-companion/manifest.json"))
        assert manifest["package_type"] == "agent_group"
        readme = package.read("family-learning-companion/README.md").decode()
        assert "保留这段说明" in readme
        assert "小艺 Work" not in readme


def test_commit_failure_restores_previous_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_group(tmp_path)
    output = tmp_path / "output"
    migration.migrate_sources(
        [source],
        output_dir=output,
        validate_runtime=False,
    )
    (output / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    before = _file_snapshot(output)

    (source / "README.md").write_text(
        "# 新版详情\n\n这次内容不应在失败后泄漏到输出目录。\n",
        encoding="utf-8",
    )
    real_replace = migration._replace_directory
    calls = 0

    def fail_candidate_commit(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated candidate commit failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(migration, "_replace_directory", fail_candidate_commit)

    with pytest.raises(migration.MigrationError, match="previous output was restored"):
        migration.migrate_sources(
            [source],
            output_dir=output,
            overwrite=True,
            validate_runtime=False,
        )

    assert calls == 3
    assert _file_snapshot(output) == before
    assert not list(tmp_path.glob(".output.workswarm-backup-*"))
    assert not list(tmp_path.glob(".output.workswarm-commit-*"))
