# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Acceptance sampling must preserve failures and use the correct execution boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def benchmark():
    root = Path(__file__).resolve().parents[3]
    scripts = root / "scripts" / "benchmarks"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts" / "benchmarks"))
        spec = importlib.util.spec_from_file_location(
            "issue4689_benchmark_under_test", scripts / "issue4689_performance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


def metadata(**overrides):
    return {"labels": ["baseline", "candidate"], "cases": ["chat"],
            "cold_samples": 1, "warm_same_samples": 0, "warm_new_samples": 0, **overrides}


def row(label, cost=1000, **overrides):
    return {"label": label, "case": "chat", "condition": "cold", "ok": True,
            "entry_ms": cost, **overrides}


def test_nearest_rank_does_not_interpolate_away_the_slowest_sample(benchmark):
    result = benchmark.population([10, 100, 20, 30])
    assert result == {"n": 4, "mean_ms": 40, "max_ms": 100, "p50_ms": 20, "p95_ms": 100, "p99_ms": 100}


def test_source_observer_preserves_public_sdk_method_metadata(benchmark, monkeypatch):
    async def send_input(instance, request):
        return request

    send_input.__module__ = "openjiuwen.harness.deep_agent"
    send_input.__qualname__ = "DeepAgent.send_input"
    agent = type("DeepAgent", (), {"send_input": send_input})
    module = SimpleNamespace(DeepAgent=agent)
    original_loader = SimpleNamespace(exec_module=lambda _module: None)
    spec = SimpleNamespace(loader=original_loader)
    original_meta_path = sys.meta_path[:]
    monkeypatch.setattr("importlib.machinery.PathFinder.find_spec", lambda *_args: spec)
    try:
        namespace = {}
        exec(benchmark.SDK_OBSERVER, namespace)
        observed = sys.meta_path[0].find_spec("openjiuwen.harness.deep_agent")
        observed.loader.exec_module(module)
        assert agent.send_input.__module__ == send_input.__module__
        assert agent.send_input.__qualname__ == send_input.__qualname__
        assert agent.send_input.__wrapped__ is send_input
    finally:
        sys.meta_path[:] = original_meta_path


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
    assert len(result["incomplete_populations"]) == 4
    assert all(item["expected"] == 30 and item["observed"] == 1
               for item in result["incomplete_populations"])
    assert not result["all_passed"]


def test_zero_warm_diagnostic_does_not_fabricate_a_warm_population(benchmark):
    result = benchmark.summarize([row("baseline"), row("candidate")], metadata())
    assert set(result["metrics"]["candidate"]) == {"chat.cold", "worker.cold_cached_checkout"}
    assert result["all_passed"]


def test_first_install_timing_is_required_alongside_prepared_workspace_gate(benchmark):
    samples = [row(label) for label in ("baseline", "candidate")]
    samples += [row(label, 9000, case="fresh") for label in ("baseline", "candidate")]
    result = benchmark.summarize(samples, metadata(cases=["chat", "fresh"], fresh_install_samples=1))
    assert result["acceptance"]["chat.cold"]["required"]
    assert result["acceptance"]["fresh.cold"]["required"]
    assert not result["acceptance"]["fresh.cold"]["passed"]
    assert result["metrics"]["candidate"]["fresh.cold"]["p95_ms"] == 9000
    assert not result["all_passed"]


@pytest.mark.parametrize("case", ["fresh", "new_checkout"])
def test_one_first_deployment_outlier_cannot_hide_outside_thirty_sample_p95(benchmark, case):
    samples = [row(label, 500, case=case, iteration=n)
               for label in ("baseline", "candidate") for n in range(30)]
    samples[-1]["entry_ms"] = 9000
    result = benchmark.summarize(samples, metadata(cases=[case], cold_samples=30, fresh_install_samples=30))
    gate = result["acceptance"][f"{case}.cold"]
    assert gate["p95_ms"] == 500
    assert gate["max_ms"] == 9000
    assert gate["required"] and not gate["passed"]
    assert result["first_deployment_sample_failures"] == [samples[-1]]
    assert not result["all_passed"]


def test_first_identity_build_has_independent_budget_and_each_sample_gate(benchmark):
    samples = [{"label": label, "case": "identity", "condition": condition,
                "identity_ms": 50, "ok": True, "iteration": n}
               for label in ("baseline", "candidate") for n in range(30)
               for condition in ("first_build", "cache_hit")]
    samples[-2]["identity_ms"] = 10000
    result = benchmark.summarize(samples, metadata(cases=["identity"], identity_samples=30))
    gate = result["acceptance"]["source_identity.first_build"]
    assert gate["limit_ms"] == 500 and gate["p95_ms"] == 50
    assert gate["max_ms"] == 10000 and not gate["passed"]
    assert result["acceptance"]["source_identity.cache_hit"]["passed"]
    assert result["first_deployment_sample_failures"] == [samples[-2]]
    assert not result["all_passed"]


def test_optional_all_cold_keeps_original_worker_p95_budget(benchmark):
    result = benchmark.summarize([row(label, 2600, case="optional_all")
                                 for label in ("baseline", "candidate")], metadata(cases=["optional_all"]))
    assert result["acceptance"]["optional_all.cold"]["required"]
    assert result["acceptance"]["optional_all.cold"]["limit_ms"] == 2500
    assert not result["all_passed"]


def test_first_install_functional_failure_still_fails_acceptance(benchmark):
    samples = [row(label) for label in ("baseline", "candidate")]
    samples += [row("baseline", case="fresh"), row("candidate", case="fresh", ok=False, error="initialization failed")]
    result = benchmark.summarize(samples, metadata(cases=["chat", "fresh"], fresh_install_samples=1))
    assert result["failed_attempts"]
    assert not result["all_passed"]


def test_environment_cannot_reuse_inherited_worker_or_fake_preparation(benchmark, monkeypatch, tmp_path):
    for key in ("JIUWENSWARM_BUILD_ID", "JIUWENSWARM_RUNTIME_SOCKET", "JIUWENSWARM_RUNTIME_WORKSPACE_READY",
                "PYTHONPROFILEIMPORTTIME", "JIUWENSWARM_SDK_ENTRY_LOG", "PYTHONHOME"):
        monkeypatch.setenv(key, "inherited-value")
    env = benchmark.isolated_environment(tmp_path / "source", tmp_path / "sample")
    assert not any(key in env for key in ("JIUWENSWARM_BUILD_ID", "JIUWENSWARM_RUNTIME_SOCKET",
        "JIUWENSWARM_RUNTIME_WORKSPACE_READY", "PYTHONPROFILEIMPORTTIME", "JIUWENSWARM_SDK_ENTRY_LOG", "PYTHONHOME"))
    assert env["HOME"] == env["USERPROFILE"] == env["JIUWENSWARM_HOME"]
    assert env["JIUWENSWARM_DATA_DIR"] != env["HOME"]


def test_each_deployment_overrides_checkout_and_inherited_identity_cache(benchmark, monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "shared-cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "inherited-local"))
    environments = [benchmark.isolated_environment(tmp_path / "same-source", tmp_path / f"sample-{n}")
                    for n in range(30)]
    assert len({env["JIUWENSWARM_IDENTITY_CACHE_DIR"] for env in environments}) == 30
    assert len({env["LOCALAPPDATA"] for env in environments}) == 30
    assert all(not Path(env["JIUWENSWARM_IDENTITY_CACHE_DIR"]).exists() for env in environments)


def test_cleanup_accepts_stopped_non_child_zombie_without_waitpid(benchmark):
    from issue4689_front import _wait_stopped
    child = SimpleNamespace(pid=123, status=lambda: benchmark.psutil.STATUS_ZOMBIE,
                            is_running=lambda: True)
    _wait_stopped(child)


def test_cleanup_never_hides_a_live_owned_child_timeout(benchmark, monkeypatch):
    import issue4689_front as front
    ticks = iter((0, 6))
    monkeypatch.setattr(front, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    child = SimpleNamespace(pid=123, status=lambda: benchmark.psutil.STATUS_RUNNING,
                            is_running=lambda: True)
    with pytest.raises(benchmark.psutil.TimeoutExpired):
        front._wait_stopped(child, timeout=5)


def test_cleanup_pid_reuse_cannot_kill_an_unrelated_process(benchmark, monkeypatch):
    import issue4689_front as front
    killed = []
    parent = SimpleNamespace(children=lambda **_kwargs: [])
    reused = SimpleNamespace(create_time=lambda: 2, kill=lambda: killed.append(123))
    monkeypatch.setattr(front.psutil, "Process", lambda pid: parent if pid == 999 else reused)
    proc = SimpleNamespace(pid=999, poll=lambda: 0)
    front.terminate_owned(proc, [(123, 1)])
    assert killed == []


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


def test_optional_all_enables_services_without_weakening_shipped_tool_policy(benchmark, tmp_path):
    template = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    original = yaml.safe_load(template.read_text(encoding="utf-8"))
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config/config.yaml").write_text(yaml.safe_dump(original), encoding="utf-8")
    receipt = benchmark.configure_all_optional(data, sys.executable)
    updated = yaml.safe_load((data / "config/config.yaml").read_text(encoding="utf-8"))
    assert updated["permissions"] == original["permissions"]
    assert updated["progressive_tool_enabled"] == original["progressive_tool_enabled"]
    assert updated["proactive_recommendation"]["enabled"]
    assert updated["symphony"]["enabled"]
    assert updated["mcp"]["servers"][0]["enabled"]
    personal = yaml.safe_load((data / ".personal_context/personal_context.yaml").read_text(encoding="utf-8"))
    assert personal["master_enabled"] and personal["collection_enabled"] and personal["agent_use_enabled"]
    assert receipt["sandbox"] == "unsupported on Windows; platform guard retained"
    compile((tmp_path / "optional_mcp.py").read_text(encoding="utf-8"), "optional_mcp.py", "exec")


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


@pytest.mark.asyncio
async def test_explicit_legacy_baseline_still_requires_real_sdk_entry(benchmark, monkeypatch, tmp_path):
    ticks = iter((100, 300))
    monkeypatch.setattr(benchmark.time, "perf_counter_ns", lambda: next(ticks))
    ws = _Responses([{"request_id": "original", "is_final": True}])
    entries = tmp_path / "entries.jsonl"
    entries.write_text(json.dumps({"request_id": "original", "entered_at_ns": 200, "pid": os.getpid(),
        "jiuwenswarm_module_file": str(tmp_path / "jiuwenswarm/__init__.py")}) + "\n", encoding="utf-8")
    result = await benchmark.request_entry(ws, request_id="original", session_id="session", mode="agent",
        entry_log=entries, endpoint="sdk", code=tmp_path, legacy_baseline=True)
    assert result["entry_ms"] == .0001
    assert result["prepared_at_ns"] is None
    assert result["legacy_baseline_without_snapshot"]
    assert [wire["req_method"] for wire in ws.sent] == ["chat.send"]
