# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Acceptance sampling must preserve failures and use the correct execution boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def benchmark():
    root = Path(__file__).resolve().parents[3]
    scripts = root / "scripts" / "benchmarks"
    original_path = sys.path[:]
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "issue4689_benchmark_under_test", scripts / "issue4689_performance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = original_path


def metadata(**overrides):
    return {"labels": ["baseline", "candidate"], "cases": ["chat"],
            "cold_samples": 1, "warm_same_samples": 0, "warm_new_samples": 0, **overrides}


def row(label, cost=1000, **overrides):
    return {"label": label, "case": "chat", "condition": "cold", "ok": True,
            "entry_ms": cost, **overrides}


def test_nearest_rank_does_not_interpolate_away_the_slowest_sample(benchmark):
    result = benchmark.population([10, 100, 20, 30])
    assert result == {"n": 4, "mean_ms": 40, "p50_ms": 20, "p95_ms": 100, "p99_ms": 100}


def test_threshold_failure_preserves_both_populations(benchmark):
    result = benchmark.summarize([row("baseline", 4000), row("candidate", 2600)], metadata())
    assert result["metrics"]["baseline"]["chat.cold"]["p95_ms"] == 4000
    assert result["metrics"]["candidate"]["chat.cold"]["p95_ms"] == 2600
    assert not result["acceptance"]["chat.cold"]["passed"]
    assert not result["all_passed"]


def test_failure_cannot_disappear_from_a_fast_successful_population(benchmark):
    failed = row("candidate", ok=False, error="actual request failed")
    result = benchmark.summarize([row("baseline"), row("candidate"), failed], metadata())
    assert result["acceptance"]["chat.cold"]["passed"]
    assert result["failed_attempts"] == [failed]
    assert not result["all_passed"]


def test_interrupted_sampling_cannot_pass_with_too_few_samples(benchmark):
    result = benchmark.summarize([row("baseline"), row("candidate")], metadata(cold_samples=30))
    assert len(result["incomplete_populations"]) == 2
    assert all(item["expected"] == 30 and item["observed"] == 1
               for item in result["incomplete_populations"])
    assert not result["all_passed"]


def test_zero_warm_diagnostic_does_not_fabricate_a_warm_population(benchmark):
    result = benchmark.summarize([row("baseline"), row("candidate")], metadata())
    assert set(result["metrics"]["candidate"]) == {"chat.cold"}
    assert result["all_passed"]


def test_environment_cannot_reuse_inherited_worker_or_fake_preparation(benchmark, monkeypatch, tmp_path):
    for key in ("JIUWENSWARM_BUILD_ID", "JIUWENSWARM_RUNTIME_SOCKET", "JIUWENSWARM_RUNTIME_WORKSPACE_READY",
                "PYTHONPROFILEIMPORTTIME", "JIUWENSWARM_SDK_ENTRY_LOG", "PYTHONHOME"):
        monkeypatch.setenv(key, "inherited-value")
    env = benchmark.isolated_environment(tmp_path / "source", tmp_path / "sample")
    assert not any(key in env for key in ("JIUWENSWARM_BUILD_ID", "JIUWENSWARM_RUNTIME_SOCKET",
        "JIUWENSWARM_RUNTIME_WORKSPACE_READY", "PYTHONPROFILEIMPORTTIME", "JIUWENSWARM_SDK_ENTRY_LOG", "PYTHONHOME"))
    assert env["HOME"] == env["USERPROFILE"] == env["JIUWENSWARM_HOME"]
    assert env["JIUWENSWARM_DATA_DIR"] != env["HOME"]


def test_fixture_changes_only_model_transport(benchmark, tmp_path):
    template = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    config = yaml.safe_load(template.read_text(encoding="utf-8"))
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    benchmark.configure_fixture(tmp_path, 12345)
    updated = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    default = updated["models"].pop("defaults")
    config["models"].pop("defaults")
    assert updated == config
    assert default[0]["model_client_config"]["api_base"] == "http://127.0.0.1:12345/v1"


@pytest.mark.parametrize("arguments", [
    ["--candidate-executable", "candidate.exe"],
    ["--baseline-only", "--baseline-executable", "baseline.exe"],
])
def test_frozen_runs_require_same_form_and_explicit_baseline_overlay(benchmark, monkeypatch, tmp_path, arguments):
    monkeypatch.setattr(sys, "argv", ["benchmark", "--baseline", str(tmp_path / "baseline"),
        "--candidate", str(tmp_path / "candidate"), "--output", str(tmp_path / "reports/samples.jsonl"), *arguments])
    with pytest.raises(SystemExit) as error:
        benchmark.arguments()
    assert error.value.code == 2


def test_explicit_node_wins_over_environment_and_path(benchmark, monkeypatch, tmp_path):
    explicit = tmp_path / "explicit-node.exe"
    explicit.write_bytes(b"fixture executable")
    explicit.chmod(0o755)
    monkeypatch.setenv("JIUWENSWARM_NODE_EXECUTABLE", str(tmp_path / "missing-env-node.exe"))
    monkeypatch.setattr(benchmark.shutil, "which", lambda value: None)
    assert benchmark.discover_node_binary(str(explicit)) == explicit.resolve()


def test_missing_explicit_node_fails_instead_of_falling_back(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark.shutil, "which", lambda value: None)
    with pytest.raises(FileNotFoundError, match="configured Node executable"):
        benchmark.discover_node_binary(str(tmp_path / "missing-node.exe"))


def test_optional_node_is_discovered_from_path(benchmark, monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    node.write_bytes(b"fixture executable")
    node.chmod(0o755)
    monkeypatch.delenv("JIUWENSWARM_NODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(benchmark.shutil, "which", lambda value: str(node))
    assert benchmark.discover_node_binary() == node.resolve()


def test_node_requires_a_runnable_file(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark.shutil, "which", lambda value: str(tmp_path))
    with pytest.raises(PermissionError, match="not a runnable file"):
        benchmark.discover_node_binary()


class _Responses:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send(self, wire):
        self.sent.append(json.loads(wire))

    async def recv(self):
        return json.dumps(next(self.frames))


@pytest.mark.asyncio
async def test_frozen_prepared_snapshot_cannot_replace_missing_sdk_entry(benchmark, monkeypatch, tmp_path):
    ticks = iter((100, 300))
    monkeypatch.setattr(benchmark.time, "perf_counter_ns", lambda: next(ticks))
    ws = _Responses([
        {"request_id": "original", "is_final": True},
        {"request_id": "snapshot-original", "is_final": True, "body": {"result": {
            "runtime": {"dispatches": [{"request_id": "original", "entered_at_ns": 150}]}}}},
    ])
    with pytest.raises(FileNotFoundError):
        await benchmark.request_entry(ws, request_id="original", session_id="session", mode="agent",
            entry_log=tmp_path / "absent-native-entry.jsonl", endpoint="native-sdk", code=tmp_path)
    assert [wire["req_method"] for wire in ws.sent] == ["chat.send", "session.get_metadata"]


@pytest.mark.asyncio
async def test_native_entry_must_identify_the_real_sdk_sender(benchmark, monkeypatch, tmp_path):
    ticks = iter((100, 300))
    monkeypatch.setattr(benchmark.time, "perf_counter_ns", lambda: next(ticks))
    ws = _Responses([
        {"request_id": "original", "is_final": True},
        {"request_id": "snapshot-original", "is_final": True, "body": {"result": {
            "runtime": {"dispatches": [{"request_id": "original", "entered_at_ns": 150}]}}}},
    ])
    entries = tmp_path / "entries.jsonl"
    entries.write_text(json.dumps({"request_id": "original", "entered_at_ns": 200, "pid": os.getpid(),
        "sender_owner": "FakeAgent", "sender_module": "fake_adapter"}) + "\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="not the actual DeepAgent"):
        await benchmark.request_entry(ws, request_id="original", session_id="session", mode="agent",
            entry_log=entries, endpoint="native-sdk", code=tmp_path)
