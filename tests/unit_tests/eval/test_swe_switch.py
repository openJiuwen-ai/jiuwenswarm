# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPT_DIR))

from coding_agent import config_dir_name  # noqa: E402
import eval_env  # noqa: E402
from eval_env import (  # noqa: E402
    is_swe_checkout,
    resolve_swe_root,
    swe_harness_env,
    swe_root_candidates,
)
from run_contextbench import (  # noqa: E402
    _aggregate_pred,
    _swe_query,
    capture_patch,
    resolve_graph_agent,
    resolve_task_mode,
)
from swe_dataset import (  # noqa: E402
    assert_no_gold_leak,
    filter_swe_rows,
    official_split_n,
    resolve_benchmark,
    resolve_include_hints,
    resolve_swe_dataset,
    swe_issue_text,
    swe_split_warning,
)


def test_resolve_benchmark_and_swe_defaults() -> None:
    assert resolve_benchmark(None) == "contextbench"
    assert resolve_benchmark("swe") == "swe"
    with pytest.raises(ValueError):
        resolve_benchmark("other")
    assert resolve_task_mode(None) == "locate"
    assert resolve_task_mode(None, benchmark="swe") == "coding"
    assert resolve_task_mode("coding", benchmark="swe") == "coding"
    with pytest.raises(ValueError):
        resolve_task_mode("locate", benchmark="swe")
    assert resolve_graph_agent("graph", None, benchmark="swe") == "root"
    assert resolve_graph_agent("off", None, benchmark="swe") == "root"


def test_config_dir_name_swe_does_not_reuse_coding_folder() -> None:
    assert config_dir_name(profile="graph", task_mode="coding") == "cfg_b__graph__coding"
    assert (
        config_dir_name(profile="graph", task_mode="coding", benchmark="swe")
        == "cfg_b__graph__swe"
    )
    assert config_dir_name(profile="off", benchmark="swe") == "cfg_b__graph-off__swe"


def test_swe_issue_omits_gold_and_optional_hints() -> None:
    row = {
        "instance_id": "django__django-10880",
        "problem_statement": "Fix the widget.",
        "hints_text": "look at forms",
        "patch": "diff --git a/secret.py b/secret.py\n+gold-only-line\n",
        "FAIL_TO_PASS": '["tests/test_secret.py::test_gold"]',
    }
    assert swe_issue_text(row) == "Fix the widget."
    with_hints = swe_issue_text(row, include_hints=True)
    assert "Fix the widget." in with_hints
    assert "look at forms" in with_hints
    assert "gold-only-line" not in with_hints
    assert "test_gold" not in with_hints
    assert not resolve_include_hints()
    assert resolve_include_hints(hints=True) is True
    with pytest.raises(ValueError):
        resolve_include_hints(hints=True, no_hints=True)
    query = _swe_query(baseline=True, delegate=False, issue=with_hints)
    assert "PATCH_CONTEXT" not in query
    assert_no_gold_leak(query, row)
    product = _swe_query(
        baseline=True, delegate=False, issue="Fix the widget.", product_defaults=True
    )
    assert product == "Fix the widget."
    assert "This is SWE-bench" not in product
    with pytest.raises(RuntimeError):
        assert_no_gold_leak(row["patch"], row)


def test_filter_swe_rows_by_id_and_limit() -> None:
    rows = [
        {"instance_id": "a"},
        {"instance_id": "b"},
        {"instance_id": "c"},
    ]
    assert [r["instance_id"] for r in filter_swe_rows(rows, wanted=["b"], limit=0)] == ["b"]
    assert [r["instance_id"] for r in filter_swe_rows(rows, limit=2)] == ["a", "b"]
    assert [r["instance_id"] for r in filter_swe_rows(rows, offset=2, limit=2)] == ["c"]
    assert [r["instance_id"] for r in filter_swe_rows(rows, offset=1, limit=1)] == ["b"]


def test_resolve_swe_dataset_aliases() -> None:
    assert resolve_swe_dataset(None, split="verified") == "SWE-bench/SWE-bench_Verified"
    assert resolve_swe_dataset("lite") == "SWE-bench/SWE-bench_Lite"
    assert resolve_swe_dataset("my/local") == "my/local"
    assert official_split_n("verified") == 500
    assert official_split_n("test") == 2294
    assert swe_split_warning("test")
    assert swe_split_warning("verified") is None


def test_fail_json_enters_swe_preds(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ok.traj.json").write_text(
        json.dumps(
            {
                "instance_id": "ok",
                "model_patch": "diff --git a/a.py b/a.py\n+x\n",
                "usage": {},
                "traj_data": {"pred_files": [], "pred_spans": {}, "pred_steps": []},
            }
        ),
        encoding="utf-8",
    )
    (raw / "boom.fail.json").write_text(
        json.dumps({"instance_id": "boom", "error": "checkout failed", "model_patch": ""}),
        encoding="utf-8",
    )
    _aggregate_pred(raw)
    rows = [
        json.loads(line)
        for line in (raw / "swe_preds.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = {row["instance_id"] for row in rows}
    assert ids == {"ok", "boom"}
    empty = next(row for row in rows if row["instance_id"] == "boom")
    assert empty["model_patch"] == ""


def test_capture_patch_skips_workspace_files(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "AGENT.md").write_text("# agent\n", encoding="utf-8")
    (repo / "coding_memory").mkdir()
    (repo / "coding_memory" / "x.md").write_text("note\n", encoding="utf-8")
    patch = capture_patch(str(repo))
    assert "x = 2" in patch
    assert "AGENT.md" not in patch
    assert "coding_memory" not in patch


def test_swe_root_candidates_include_workspace_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    monkeypatch.setattr(eval_env, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("SWE_BENCH_ROOT", raising=False)
    found = swe_root_candidates()
    assert any(path.name == "SWE-bench" for path in found)
    swe = tmp_path / "SWE-bench"
    (swe / "swebench" / "harness").mkdir(parents=True)
    (swe / "swebench" / "harness" / "run_evaluation.py").write_text("#\n", encoding="utf-8")
    monkeypatch.setenv("SWE_BENCH_ROOT", str(swe))
    assert is_swe_checkout(swe)
    assert resolve_swe_root() == swe.resolve()
    env = swe_harness_env(swe_root=swe)
    assert str(swe.resolve()) in env["PYTHONPATH"]
    assert env["SWE_BENCH_ROOT"] == str(swe.resolve())


def test_swe_harness_env_forces_amd64_on_darwin(monkeypatch) -> None:
    monkeypatch.setattr(eval_env.sys, "platform", "darwin")
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/arm64")
    env = swe_harness_env()
    assert env["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
    assert env["SWE_BENCH_ALLOW_EXPERIMENTAL_HOST"] == "1"
