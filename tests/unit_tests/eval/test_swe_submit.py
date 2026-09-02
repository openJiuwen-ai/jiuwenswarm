# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPT_DIR))

from swe_submit import package_swe_submission  # noqa: E402


def test_package_writes_official_trees(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    preds = raw / "swe_preds.jsonl"
    preds.write_text(
        json.dumps(
            {
                "instance_id": "django__django-10880",
                "model_name_or_path": "gpt-5-mini",
                "model_patch": "diff --git a/a.py b/a.py\n+ok\n",
            }
        )
        + "\n"
        + json.dumps(
            {
                "instance_id": "django__django-13109",
                "model_name_or_path": "gpt-5-mini",
                "model_patch": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (raw / "django__django-10880.traj.json").write_text(
        json.dumps({"instance_id": "django__django-10880", "usage": {"prompt_tokens": 1}}),
        encoding="utf-8",
    )
    logs = tmp_path / "logs" / "run_evaluation" / "swe" / "gpt-5-mini" / "django__django-10880"
    logs.mkdir(parents=True)
    (logs / "patch.diff").write_text("diff --git a/a.py b/a.py\n+ok\n", encoding="utf-8")
    (logs / "report.json").write_text(
        json.dumps({"django__django-10880": {"resolved": True}}),
        encoding="utf-8",
    )
    (logs / "test_output.txt").write_text("passed\n", encoding="utf-8")

    out = tmp_path / "submission"
    result = package_swe_submission(
        predictions=preds,
        out_dir=out,
        split="verified",
        run_id="swe",
        logs_root=tmp_path / "logs" / "run_evaluation" / "swe" / "gpt-5-mini",
        trajs=raw,
        sub_id="20260901_gpt-5-mini",
    )
    assert result.resolved == ["django__django-10880"]
    assert result.no_generation == ["django__django-13109"]
    repo = out / "submission-repo"
    assert (repo / "all_preds.jsonl").is_file()
    assert (repo / "trajs" / "django__django-10880.traj.json").is_file()
    assert (repo / "logs" / "django__django-10880" / "patch.diff").is_file()
    assert (repo / "logs" / "django__django-10880" / "test_output.txt").is_file()
    meta = (out / "entry" / "metadata.yaml").read_text(encoding="utf-8")
    assert "jiuwenswarm" in meta
    readme = (out / "entry" / "README.md").read_text(encoding="utf-8")
    assert "1 / 500" in readme
    assert "Unofficial helper" in readme
    assert "not re-graded" in readme
    results = json.loads((out / "entry" / "results" / "results.json").read_text())
    assert results["resolved"] == ["django__django-10880"]
