"""Verify source-preserving launch, official stage generation and reporting."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.benchmarks.duplex_metrics import official_scores, summarize_events, paired_success
from jiuwenswarm.benchmarks.interruptbench_runner import instrument_runner


def test_missing_scores_stay_in_denominator_and_partial_score_is_not_success(tmp_path):
    actions = tmp_path / "actions"
    actions.mkdir()
    for task, score in (("0", 1), ("1", 0.5)):
        (actions / f"{task}.json").write_text(json.dumps({"score": score}))
    result = official_scores(tmp_path, ["0", "1", "2"], benchmark="interruptbench")
    assert result["success_rate"] == 1 / 3
    assert result["scored"] == 2
    assert result["tasks"][-1]["status"] == "missing_official_score"
    assert summarize_events([])["total_tokens"] is None


def test_paired_report_uses_final_stage_and_retains_missing_scores():
    def result(policy, stage, score):
        return {"policy": policy, "repeat": 0, "stage": stage,
                "official": {"tasks": [{"task_id": "0", "score": score, "success": score == 1}]}}
    report = paired_success([result("official", 1, 1), result("official", 2, 0),
                             result("model", 1, 0), result("model", 2, 1)], "official")
    assert report["comparisons"]["model"]["success_delta"] == 1
    report = paired_success([result("official", 2, 1), result("model", 2, None)], "official")
    assert report["comparisons"]["model"]["success_delta"] == -1
    assert report["comparisons"]["model"]["pairs_with_missing_scores"] == 1


def test_upstream_runner_hook_is_at_exact_injection_branch():
    root = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not root:
        pytest.skip("official checkout required")
    path = Path(root) / "Eval/run.py"
    instrument_runner(path.read_text(encoding="utf-8"), str(path))
    with pytest.raises(ValueError, match="one official injection branch"):
        instrument_runner("pass", "wrong.py")


def test_agentradio_original_startup_builds_protocol_and_launches_native(tmp_path):
    pytest.importorskip("harbor")
    pytest.importorskip("multi_agent.coral_multi_agent_passive")
    from jiuwenswarm.benchmarks.agentradio_harbor import native_startup, MULTI_AGENT_DIR
    shell = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
    if not shell or not Path(shell).is_file():
        pytest.skip("Bash required")
    root = tmp_path / "agent"
    root.mkdir()
    shutil.copytree(MULTI_AGENT_DIR / "passive_scripts", root / "passive_scripts")
    fake = root / "native-python"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > native-arguments.txt\n', newline="\n")
    fake.chmod(0o755)
    source = (MULTI_AGENT_DIR / "startup_passive.sh").read_text()
    rewritten = native_startup(source, "model")
    assert source.split("exec claude")[0] == rewritten.split("exec /tmp/jiuwen")[0]
    script = root / "startup_passive.sh"
    script.write_text(rewritten.replace("/tmp/jiuwen-bench-venv/bin/python", shlex.quote(fake.as_posix())), newline="\n")
    env = {**os.environ, "CORAL_AGENT_ID": "agent-4", "CORAL_SESSION_ID": "test-session",
           "CORAL_CONNECTION_URL": "http://127.0.0.1:1/unused"}
    subprocess.run([shell, str(script)], env=env, check=True, capture_output=True, timeout=15)
    instance = root / "instances/test-session/agent-4"
    assert "Phase 5" in (instance / "CLAUDE.md").read_text()
    assert "agent-4" in (instance / "CLAUDE.md").read_text()
    assert "jiuwenswarm.benchmarks.agentradio_peer" in (instance / "native-arguments.txt").read_text()


def test_official_multistage_generator_keeps_prior_updates_and_boundaries(tmp_path):
    source = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not source:
        pytest.skip("official checkout required")
    root = Path(source) / "Eval"
    raw = root / "interrupt_config/raw/3mixed.json"
    records = json.loads(raw.read_text(encoding="utf-8"))
    record = next(item for item in records if item["task_id"] == 0)
    initial = tmp_path / "initial"
    subprocess.run([sys.executable, str(root / "interrupt_config/make_transformed_intent_configs.py"),
        "--base_config_dir", str(root / "config_files/wa/test_webarena_lite"),
        "--raw_interrupt_file", str(raw), "--out_dir", str(initial)], check=True, capture_output=True)
    previous = tmp_path / "baseline"
    (previous / "trajectories").mkdir(parents=True)
    # Shape fixtures exercise the official generator, not browser task scoring.
    (previous / "trajectories/0.json").write_text(json.dumps({"task_id": 0, "actions": [{}] * 8}))
    prior = []
    for stage, expected_k in ((1, 4), (2, 6), (3, 7)):
        config_dir, spec = tmp_path / f"c{stage}", tmp_path / f"s{stage}.json"
        subprocess.run([sys.executable, str(root / "interrupt_config/make_multi_interrupt_stage.py"),
            "--raw_interrupt_file", str(raw), "--base_config_dir", str(initial),
            "--result_dirs", str(previous), "--stage", str(stage), "--out_config_dir", str(config_dir),
            "--out_interrupt_spec", str(spec)], check=True, capture_output=True)
        update = json.loads(spec.read_text())["tasks"]["0"]
        assert update["interrupt_at_action"] == expected_k
        assert update["update_intent"] == record["updates"][stage - 1]
        intent = json.loads((config_dir / "0.json").read_text())["intent"]
        assert all(message in intent for message in prior)
        prior.append(update["update_intent"])
        previous = tmp_path / f"stage{stage}"
        (previous / "trajectories").mkdir(parents=True)
        (previous / "trajectories/0.json").write_text(json.dumps({"task_id": 0, "actions": [{}] * 8,
            "interrupt": update}))
