# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import zipfile
from pathlib import Path

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
