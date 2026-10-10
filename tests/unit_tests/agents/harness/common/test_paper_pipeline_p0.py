"""Regression tests for the P0 fixes (no model calls): condition-aware pairing and metric mapping,
persisted revision settings across a restart, frozen cells and the host cell cap, the evidence /
final-paper acceptance gate."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import (
    execution_audit,
    latex_check,
    revision,
    revision_state,
    rigor_stats,
    runner,
)


# --------------------------------------------------------------------------- fixtures
def _records(correct: list[int], *, model="qwen-flash", budget=325, prefix="q", ids=None, **extra) -> list[dict]:
    ids = ids or [f"{prefix}{i}" for i in range(len(correct))]
    return [{"qid": ids[i], "answer_em": c, "predicted": f"a{i}-{c}", "model": model, "budget_tokens": budget,
             "prompt_tokens": 100 + i, **extra} for i, c in enumerate(correct)]


def _cell(correct: list[int], *, model="qwen-flash", budget=325, tier="T1", dataset="hash-hotpot", **kw) -> dict:
    records = kw.pop("records", None) or _records(correct, model=model, budget=budget)
    return {"answer_em": sum(r["answer_em"] for r in records) / len(records), "tier": tier, "budget_tokens": budget,
            "policy": kw.pop("policy", None), "config": {"model": model, "item_list_hash": dataset},
            "per_question": records, **kw}


def _pattern(n_good: int, n: int = 60) -> list[int]:
    return [1] * n_good + [0] * (n - n_good)


def _four_settings() -> dict[str, dict]:
    return {
        "proposed_T1": _cell(_pattern(40)),
        "relevance_select_T1": _cell(_pattern(30)),
        "m2__proposed_T1": _cell(_pattern(45), model="qwen-plus"),
        "m2__relevance_select_T1": _cell(_pattern(35), model="qwen-plus"),
    }


def _pairs(stats) -> set[tuple[str, str]]:
    return {(p.a, p.b) for p in stats.pairs}


# --------------------------------------------------------------------------- 1. pairing
def test_each_setting_pairs_its_own_method_and_baseline_only():
    stats = rigor_stats.compute(_four_settings(), primary=["answer_em"])
    assert _pairs(stats) == {("proposed_T1", "relevance_select_T1"), ("m2__proposed_T1", "m2__relevance_select_T1")}
    assert all(p.scope == "within_condition" for p in stats.pairs)
    assert stats.conditions["m2__proposed_T1"].setting == "m2"
    assert stats.conditions["m2__proposed_T1"].method == "proposed"
    assert not stats.errors


def test_choose_pairs_never_crosses_settings_by_name_alone():
    pairs = rigor_stats.choose_pairs(sorted(_four_settings()))
    assert set(pairs) == {("proposed_T1", "relevance_select_T1"), ("m2__proposed_T1", "m2__relevance_select_T1")}


def test_cross_setting_pair_only_when_declared():
    variants = _four_settings()
    stats = rigor_stats.compute(variants, primary=["answer_em"], cross_pairs=[("m2__proposed_T1", "proposed_T1")])
    cross = [p for p in stats.pairs if p.scope == "declared_cross_condition"]
    assert [(p.a, p.b) for p in cross] == [("m2__proposed_T1", "proposed_T1")]
    assert "Declared cross-condition comparisons" in rigor_stats.markdown(stats)


def test_same_tier_name_with_different_recorded_budget_is_not_paired():
    variants = {"proposed_T1": _cell(_pattern(40), budget=325), "baseline_T1": _cell(_pattern(30), budget=400)}
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    assert stats.pairs == []
    assert any("recorded budgets differ (325.0 vs 400.0)" in n for n in stats.notes)


def test_tier_without_recorded_budget_is_not_paired_by_name():
    variants = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30))}
    for cell in variants.values():
        del cell["budget_tokens"]
        for r in cell["per_question"]:
            del r["budget_tokens"]
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    assert stats.pairs == [] and any("budget not recorded" in n for n in stats.notes)


def test_different_dataset_or_model_inside_one_setting_is_not_paired():
    variants = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30), dataset="other")}
    assert rigor_stats.compute(variants, primary=["answer_em"]).pairs == []
    variants = {"proposed_T1": _cell(_pattern(40)), "baseline_T1": _cell(_pattern(30), model="qwen-plus")}
    assert rigor_stats.compute(variants, primary=["answer_em"]).pairs == []


def test_unresolved_condition_is_an_error_not_a_guess():
    # the replication cell of bl4 (repl_model__proposed_T1): configured qwen-plus, records from two models
    records = _records(_pattern(40)[:55], model="qwen-flash") + [
        {**r, "model": "qwen-plus"} for r in _records(_pattern(5, 5), model="qwen-plus", prefix="z")]
    variants = {"proposed_T1": _cell([], records=records, model="qwen-plus"),
                "baseline_T1": _cell([], records=_records(_pattern(30)[:55]) + _records([0] * 5, prefix="z"),
                                     model="qwen-plus")}
    stats = rigor_stats.compute(variants, primary=["answer_em"])
    assert stats.pairs == []
    assert any(e.startswith("CONDITION_UNRESOLVED proposed_T1") and "several models" in e for e in stats.errors)


# --------------------------------------------------------------------------- metric mapping
def test_equal_means_do_not_map_different_metrics():
    records = [{"qid": f"q{i}", "constraint_active": False, "em": i % 2} for i in range(40)]
    metrics = {"n_api_errors": 0, "constraint_active": 0.0, "accuracy": 0.5, "per_question": records}
    matched = rigor_stats.match_metrics(metrics, rigor_stats.per_question_fields(records))
    assert matched == {"constraint_active": "constraint_active"}  # n_api_errors is not constraint_active


def test_unmapped_primary_metric_stops_without_substitution():
    variants = {n: {"accuracy": 0.5, "constraint_active": 0.0, "n_api_errors": 0,
                    "per_question": [{"qid": f"q{i}", "em": i % 2, "constraint_active": False} for i in range(40)]}
                for n in ("proposed", "baseline")}
    stats = rigor_stats.compute(variants, primary=["accuracy"])
    assert stats.pairs == [] and stats.primary == ["accuracy"]
    assert any(e.startswith("PRIMARY_METRIC_UNMAPPED accuracy") for e in stats.errors)


def test_declared_mapping_is_verified_against_the_mean():
    records = [{"qid": f"q{i}", "correct": i % 2} for i in range(40)]
    notes: list[str] = []
    matched = rigor_stats.match_metrics({"accuracy": 0.9, "per_question": records},
                                        rigor_stats.per_question_fields(records), {"accuracy": "correct"}, notes)
    assert matched == {} and "averages 0.5000" in notes[0]


# --------------------------------------------------------------------------- item ids
def test_missing_duplicate_or_mismatched_item_ids_refuse_the_pair():
    base = _cell(_pattern(30))
    missing = _cell(_pattern(40))
    del missing["per_question"][3]["qid"]
    stats = rigor_stats.compute({"proposed_T1": missing, "baseline_T1": base}, primary=["answer_em"])
    assert stats.pairs == [] and any("carry no item id" in n for n in stats.notes)

    duplicate = _cell(_pattern(40))
    duplicate["per_question"][5]["qid"] = "q4"
    stats = rigor_stats.compute({"proposed_T1": duplicate, "baseline_T1": base}, primary=["answer_em"])
    assert stats.pairs == [] and any("duplicate item ids ['q4']" in n for n in stats.notes)
    (ci,) = [s for s in stats.metrics["proposed_T1"] if s.metric == "answer_em"]
    assert ci.n == 60  # rows keyed by position: the duplicate does not drop a row from the interval

    other = _cell([], records=_records(_pattern(40)[:59]) + _records([1], prefix="x"))
    stats = rigor_stats.compute({"proposed_T1": other, "baseline_T1": base}, primary=["answer_em"])
    assert stats.pairs == [] and any("item sets differ (1 only in proposed_T1, 1 only in baseline_T1)" in n
                                     for n in stats.notes)


def test_single_setting_untiered_experiment_still_works():
    variants = {n: {"accuracy": 0.5, "per_question": [{"qid": f"q{i}", "accuracy": (i + k) % 2} for i in range(40)]}
                for k, n in enumerate(("proposed", "a", "b"))}
    stats = rigor_stats.compute(variants, primary=["accuracy"])
    assert _pairs(stats) == {("proposed", "a"), ("proposed", "b")}


# --------------------------------------------------------------------------- 2/3. revision state
def _opts(tmp_path: Path, **kw) -> runner.PaperRunOptions:
    return runner.PaperRunOptions(run_dir=tmp_path, topic="t", **kw)


def _write_cell(results: Path, code: Path, name: str, correct: list[int]) -> None:
    records_path = code / "results" / f"{name}.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    records = _records(correct)
    records_path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    payload = _cell(correct, records=records, run_artifacts={"records_path": str(records_path)})
    (results / f"{name}.metrics.json").write_text(json.dumps(payload), encoding="utf-8")


def _open_revision(tmp_path: Path, *, cap: int = 2, **opt_kw) -> tuple[revision_state.RevisionState, Path, Path]:
    exp = tmp_path / "experiments" / "r1"
    results, code = exp / "results", exp / "generated_code"
    results.mkdir(parents=True)
    (code / "run.py").parent.mkdir(parents=True)
    (code / "run.py").write_text("print('v1')", encoding="utf-8")
    for name, good in (("proposed_T1", 40), ("relevance_select_T1", 30), ("adaptive_retain_only", 20)):
        _write_cell(results, code, name, _pattern(good))
    (results / "broken.metrics.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    settings = revision_state.settings_of(_opts(tmp_path, **opt_kw))
    state = revision_state.RevisionState(index=1, start_round=20, max_new_cells=cap, settings=settings,
                                         identity=revision_state.identity_of(settings))
    revision_state.build_manifest(results, code, state.folder(tmp_path))
    revision_state.save(state, tmp_path)
    return state, results, code


def test_settings_survive_a_restart_and_no_second_revision(tmp_path: Path):
    _open_revision(tmp_path, replication_model="qwen-plus", module_models={"reporting": "deepseek-v4-pro"})
    # a new process: plain resume options carry none of the revision settings
    fresh = _opts(tmp_path)
    reopened = revision_state.find_open(tmp_path)
    assert reopened is not None and reopened.start_round == 20 and reopened.max_new_cells == 2
    assert revision_state.restore_settings(fresh, reopened, tmp_path) == []
    assert fresh.replication_model == "qwen-plus" and fresh.module_models == {"reporting": "deepseek-v4-pro"}
    assert revision_state.next_index(tmp_path) == 2 and len(revision_state.all_revisions(tmp_path)) == 1
    # revise on top of an open revision is refused before anything is installed
    with pytest.raises(runner.PaperRunError, match="still open"):
        asyncio.run(runner.run(_opts(tmp_path, review_path=tmp_path / "r.json"), resume=True))


def test_restore_in_a_separate_process(tmp_path: Path):
    import subprocess
    import sys

    _open_revision(tmp_path, replication_model="qwen-plus", module_models={"reporting": "deepseek-v4-pro"})
    code = (
        "import json, sys; from pathlib import Path\n"
        "from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state, runner\n"
        "run = Path(sys.argv[1]); opts = runner.PaperRunOptions(run_dir=run, topic='t')\n"
        "state = revision_state.find_open(run); revision_state.restore_settings(opts, state, run)\n"
        "print(json.dumps([opts.replication_model, opts.module_models, state.start_round, state.max_new_cells,\n"
        "                  revision_state.next_index(run), len(revision_state.all_revisions(run))]))\n"
    )
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["qwen-plus", {"reporting": "deepseek-v4-pro"}, 20, 2,
                                                               2, 1]


def test_explicit_override_is_recorded_and_model_switch_after_execution_needs_consent(tmp_path: Path):
    state, *_ = _open_revision(tmp_path, replication_model="qwen-plus")
    changed = _opts(tmp_path, module_models={"reporting": "other"})
    assert [c["setting"] for c in revision_state.restore_settings(changed, state, tmp_path)] == ["module_models"]
    assert revision_state.load(state.folder(tmp_path) / "revision.json").overrides[0]["to"] == {"reporting": "other"}

    state.executed_new_cells = ["m2__proposed_T1"]
    with pytest.raises(runner.PaperRunError, match="mix answering models"):
        revision_state.restore_settings(_opts(tmp_path, replication_model="qwen-max"), state, tmp_path)
    allowed = revision_state.restore_settings(_opts(tmp_path, replication_model="qwen-max"), state, tmp_path,
                                              allow_change=True)
    assert allowed[0]["setting"] == "replication_model" and state.settings["replication_model"] == "qwen-max"


def test_closed_revision_is_not_reopened(tmp_path: Path):
    state, *_ = _open_revision(tmp_path)
    state.status = revision_state.ACCEPTED
    revision_state.save(state, tmp_path)
    assert revision_state.find_open(tmp_path) is None


def test_runner_restores_revision_settings_on_plain_resume(tmp_path: Path, monkeypatch):
    _open_revision(tmp_path, replication_model="qwen-plus", module_models={"reporting": "deepseek-v4-pro"})
    seen = {}
    from jiuwenswarm.agents.harness.common.paper_pipeline import code_agent_budget, module_models, research_protocol

    monkeypatch.setattr(code_agent_budget, "install_code_agent_iteration_fix", lambda n: 80)
    monkeypatch.setattr(module_models, "install_module_models", lambda m: seen.setdefault("module_models", m))
    monkeypatch.setattr(research_protocol, "install_research_protocol", lambda: [])
    monkeypatch.setattr(revision, "install_revision_protocol", lambda: seen.setdefault("revision_protocol", True))

    async def fake_resume(opts):
        seen["replication_model"] = opts.replication_model

    monkeypatch.setattr(runner, "resume_run", fake_resume)
    asyncio.run(runner.run(_opts(tmp_path), resume=True))
    assert seen == {"module_models": {"reporting": "deepseek-v4-pro"}, "revision_protocol": True,
                    "replication_model": "qwen-plus"}
    events = [json.loads(line) for line in (tmp_path / "resume_log.jsonl").read_text().splitlines()]
    assert any(e["event"] == "revision_restored" for e in events)


def test_activate_revision_restores_replication_model_and_gate(tmp_path: Path, monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import manager as manager_mod
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline import transitions

    _open_revision(tmp_path, replication_model="qwen-plus")
    monkeypatch.setattr(execution, "_variant_env", execution._variant_env.__dict__.get("__wrapped__",
                                                                                      execution._variant_env))
    monkeypatch.setattr(transitions, "sync_host_requirements", lambda state: None)
    monkeypatch.setattr(manager_mod, "sync_host_requirements", transitions.sync_host_requirements)
    monkeypatch.setattr(revision_state, "_ACTIVE", None)
    runner.activate_revision(tmp_path, revision_state.find_open(tmp_path))  # as a fresh process would
    assert execution._variant_env(None)["REPLICATION_MODEL_NAME"] == "qwen-plus"
    assert revision_state.active_guard() is not None
    state = SimpleNamespace(reports=[_report("experiment_execution", 21), _report("reporting", 22)],
                            task_state=SimpleNamespace(requirements=[], latest_execution_status="completed"))
    manager_mod.sync_host_requirements(state)
    rows = {r.id: r for r in state.task_state.requirements}
    assert rows[revision.REQ_EXECUTE].status == "completed"
    assert rows[revision.REQ_EVIDENCE].status == "pending" and "no new cell" in rows[revision.REQ_EVIDENCE].notes
    assert rows[revision.REQ_REPORT].status == "pending"  # a paper alone does not open the gate


def _report(module: str, round_index: int):
    return SimpleNamespace(module=module, outcome="succeeded", round_index=round_index,
                           report_id=f"{module}:{round_index}:1")


# --------------------------------------------------------------------------- frozen cells + cap
def _design(results: Path, text: str = "- Metric answer_em") -> tuple[str, Path]:
    """The run's design file, where the pipeline keeps it (``verify`` reads it from there)."""
    path = results.parent / "design" / "experiment_design.md"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8"), path


def _impl(names: list[str]):
    return SimpleNamespace(workspace_dir="", variants=[SimpleNamespace(name=n, invocation=["x"]) for n in names])


def _fake_execution(results: Path, ran: list[str]):
    def original(agent, inputs):
        variants = []
        for v in inputs.implementation.variants:
            ran.append(v.name)
            payload = _cell(_pattern(42))
            (results / f"{v.name}.metrics.json").write_text(json.dumps(payload), encoding="utf-8")
            variants.append(SimpleNamespace(name=v.name, metrics=payload, process_status="completed", exit_code=0))
        return SimpleNamespace(result=SimpleNamespace(variants=variants, notes="", status="completed"))
    return original


def test_guard_references_frozen_cells_and_enforces_the_cap(tmp_path: Path):
    state, results, code = _open_revision(tmp_path, cap=2)
    manifest = revision_state.load_manifest(state.folder(tmp_path))
    assert sorted(c["name"] for c in manifest["cells"]) == ["adaptive_retain_only", "proposed_T1",
                                                            "relevance_select_T1"]
    assert manifest["not_frozen"] == ["broken: status failed"]
    guard = revision_state.ExecutionGuard(tmp_path, state, manifest)
    names = ["proposed_T1", "relevance_select_T1", "adaptive_retain_only", "m2__proposed_T1",
             "m2__relevance_select_T1", "m2__full_history"]
    inputs = SimpleNamespace(plan=SimpleNamespace(), implementation=_impl(names))
    filtered, plan = guard.filter_inputs(inputs)
    assert [v.name for v in filtered.implementation.variants] == ["m2__proposed_T1", "m2__relevance_select_T1"]
    assert plan.refused == ["m2__full_history"] and len(inputs.implementation.variants) == 6  # input untouched
    ran: list[str] = []
    output = _fake_execution(results, ran)(None, filtered)
    lines = guard.merge(output, plan)
    assert ran == ["m2__proposed_T1", "m2__relevance_select_T1"]  # frozen cells were not re-run
    assert {v.name for v in output.result.variants} == set(names) - {"m2__full_history"}
    assert any("CELL_CAP_REFUSED" in line and "m2__full_history" in line for line in lines)
    assert (state.folder(tmp_path) / "results" / "m2__proposed_T1.metrics.json").is_file()
    assert revision_state.verify_manifest(manifest) == []  # old results untouched

    # a restarted process: the cap counts cells executed before the restart
    again = revision_state.ExecutionGuard(tmp_path, revision_state.find_open(tmp_path), manifest)
    assert again.plan(names + ["ds2__proposed_T1"]).refused == ["m2__full_history", "ds2__proposed_T1"]


def test_changed_frozen_result_stops_its_analysis(tmp_path: Path):
    state, results, code = _open_revision(tmp_path)
    records = code / "results" / "proposed_T1.jsonl"
    records.write_text(records.read_text() + "\n", encoding="utf-8")
    manifest = revision_state.load_manifest(state.folder(tmp_path))
    guard = revision_state.ExecutionGuard(tmp_path, state, manifest)
    filtered, plan = guard.filter_inputs(SimpleNamespace(implementation=_impl(["proposed_T1", "new_T1"])))
    output = _fake_execution(results, [])(None, filtered)
    lines = guard.merge(output, plan)
    assert any(line.startswith("REVISION ERROR FROZEN_RESULT_CHANGED proposed_T1") and "proposed_T1.jsonl" in line
               for line in lines)
    assert "proposed_T1" not in {v.name for v in output.result.variants}
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert not ok and "frozen results changed" in detail


def test_run_audited_executes_only_new_cells_and_writes_a_verdict(tmp_path: Path, monkeypatch):
    state, results, _ = _open_revision(tmp_path, cap=5)
    manifest = revision_state.load_manifest(state.folder(tmp_path))
    monkeypatch.setattr(revision_state, "_ACTIVE", revision_state.ExecutionGuard(tmp_path, state, manifest))
    monkeypatch.setattr(execution_audit, "_results_dir", lambda plan: results)
    monkeypatch.setattr(execution_audit, "_read_design", lambda plan: _design(results))
    ran: list[str] = []
    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=[], run_id="r1"),
                             implementation=_impl(["proposed_T1", "relevance_select_T1", "abl_T1"]))
    output = execution_audit.run_audited(_fake_execution(results, ran), None, inputs)
    assert ran == ["abl_T1"]
    audit = json.loads((results / "audit.json").read_text())
    assert audit["frozen_cells"] == ["proposed_T1", "relevance_select_T1"]
    assert not any(f["code"] == "STALE_METRICS" for f in audit["findings"])  # frozen cells are referenced
    assert audit["verdict"] in ("passed", "failed") and "AUDIT VERDICT" in output.result.notes
    assert "REVISION referenced 2 frozen cells" in output.result.notes


def test_crashed_audit_is_unverified_not_passed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(revision_state, "_ACTIVE", None)
    monkeypatch.setattr(execution_audit, "_results_dir", lambda plan: tmp_path / "results")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(execution_audit, "post_process", boom)
    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=[]), implementation=_impl([]))
    output = execution_audit.run_audited(_fake_execution(tmp_path / "results", []), None, inputs)
    assert "AUDIT VERDICT UNVERIFIED" in output.result.notes
    assert json.loads((tmp_path / "results" / "audit.json").read_text())["verdict"] == "unverified"


# --------------------------------------------------------------------------- 4. audit + acceptance
def _codes(findings):
    return {(f.level, f.code) for f in findings}


def test_missing_primary_metric_is_an_error_even_with_secondaries_present():
    variants = {"proposed": {"total_tokens": 1.0, "per_question": _records(_pattern(30))}}
    codes = _codes(execution_audit.audit(variants, declared=["accuracy", "total_tokens"], started_at=0,
                                         metrics_paths={}))
    assert ("error", "MISSING_PRIMARY_METRIC") in codes
    codes = _codes(execution_audit.audit({"proposed": {"accuracy": 1.0, "per_question": _records(_pattern(30))}},
                                         declared=["accuracy", "total_tokens"], started_at=0, metrics_paths={}))
    assert ("warn", "MISSING_DECLARED_METRIC") in codes and ("error", "MISSING_PRIMARY_METRIC") not in codes


def test_activation_threshold_comes_from_the_host_protocol_only():
    def cell(tier: str, **extra) -> dict:
        records = _records(_pattern(60, 120))
        for i, r in enumerate(records):
            r["constraint_active"] = i < 60  # 50%
        return {"answer_em": 0.5, "tier": tier, "per_question": records, **extra}

    protocol = {"tier_gates": {"t1": 0.6, "t3": 0.2}, "tier_gates_source": "design"}
    assert ("error", "CONSTRAINT_INACTIVE") in _codes(execution_audit.audit(
        {"proposed_T1": cell("T1")}, declared=["answer_em"], started_at=0, metrics_paths={}, protocol=protocol))
    assert ("error", "CONSTRAINT_INACTIVE") not in _codes(execution_audit.audit(
        {"proposed_T3": cell("T3")}, declared=["answer_em"], started_at=0, metrics_paths={}, protocol=protocol))
    # the output lowering its own gate to 0 changes nothing: it is reported, not applied
    lowered = _codes(execution_audit.audit(
        {"proposed_T1": cell("T1", tier_gates={"T1": 0.0}, tier_calibration={"activation_gates": {"T1": 0.0}})},
        declared=["answer_em"], started_at=0, metrics_paths={}, protocol=protocol))
    assert ("error", "CONSTRAINT_INACTIVE") in lowered and ("warn", "OUTPUT_GATE_IGNORED") in lowered
    assert execution_audit.activation_gate("x_T2", protocol)[0] == 0.3
    assert execution_audit.activation_gate("x_T2", None)[1].startswith("default")


def test_identical_predictions_are_classified():
    same = _records(_pattern(30))
    alias = {"a_T1": _cell([], records=same, policy="p"), "b_T1": _cell([], records=same, policy="p")}
    assert ("warn", "IDENTICAL_ALIAS") in _codes(execution_audit.audit(alias, declared=["answer_em"], started_at=0,
                                                                        metrics_paths={}))
    dead = {"a_T1": _cell([], records=same, policy="p"), "b_T1": _cell([], records=same, policy="q")}
    assert ("error", "IDENTICAL_OUTPUTS") in _codes(execution_audit.audit(dead, declared=["answer_em"], started_at=0,
                                                                          metrics_paths={}))
    moved = [{**r, "prompt_tokens": r["prompt_tokens"] + 7} for r in same]
    null = {"a_T1": _cell([], records=same, policy="p"), "b_T1": _cell([], records=moved, policy="q")}
    codes = _codes(execution_audit.audit(null, declared=["answer_em"], started_at=0, metrics_paths={}))
    assert ("warn", "IDENTICAL_PREDICTIONS") in codes and ("error", "IDENTICAL_OUTPUTS") not in codes


def test_structured_exception_waives_only_with_scope(tmp_path: Path):
    findings = [execution_audit.Finding("error", "CONSTRAINT_INACTIVE", "x_T3: low", ["x_T3"]),
                execution_audit.Finding("error", "STALE_METRICS", "y: old", ["y"])]
    (tmp_path / execution_audit.EXCEPTIONS_FILE).write_text(json.dumps({"exceptions": [
        {"code": "CONSTRAINT_INACTIVE", "variants": ["x_T3"], "reason": "T3 is the loose control tier by design",
         "affected_comparisons": ["proposed_T3 vs x_T3"], "affected_claims": ["no claim at T3"]},
        {"code": "STALE_METRICS", "variants": ["y"], "reason": "trying to waive an integrity error here",
         "affected_comparisons": ["a"], "affected_claims": []},
        {"code": "CONSTRAINT_INACTIVE", "variants": ["z"], "reason": "no scope given for this one at all"},
    ]}), encoding="utf-8")
    exceptions, problems = execution_audit.load_exceptions(tmp_path)
    assert len(exceptions) == 1 and len(problems) == 2
    execution_audit.apply_exceptions(findings, exceptions)
    assert findings[0].waived_by["affected_claims"] == ["no claim at T3"] and findings[1].waived_by is None
    assert execution_audit.verdict(findings) == ("failed", ["STALE_METRICS: y: old"])


def test_gate_separates_finished_from_accepted():
    rows = revision.gate_rows("experiment_execution:21:1", "reporting:22:1", (False, "audit verdict failed"))
    assert rows[revision.REQ_EXECUTE][1] and rows[revision.REQ_EVIDENCE][1] is None
    assert rows[revision.REQ_REPORT][1] is None and "blocked" in rows[revision.REQ_REPORT][2]
    rows = revision.gate_rows("experiment_execution:21:1", "reporting:22:1", (True, "ok"))
    assert all(support for _, support, _ in rows.values())
    assert revision.REQ_EVIDENCE not in revision.gate_rows("e", "r", None)  # legacy gate unchanged


def test_evidence_status_needs_a_fresh_passing_audit(tmp_path: Path, monkeypatch):
    state, results, _ = _open_revision(tmp_path, cap=5)
    state.executed_new_cells = ["abl_T1"]
    # a fresh "passed" audit.json alone is not evidence: no protocol, no manifest, nothing hashed
    (results / "audit.json").write_text(json.dumps({"verdict": "passed", "audited_at": time.time() + 5}),
                                        encoding="utf-8")
    ok, detail = revision_state.evidence_status(tmp_path, state)
    assert not ok and "NO_PROTOCOL" in detail and "NO_EVIDENCE_MANIFEST" in detail
    _execute_in_revision(tmp_path, state, results, monkeypatch, ["abl_T1"])
    assert revision_state.evidence_status(tmp_path, state)[0]
    execution_audit.write_unverified(SimpleNamespace(plan=None), RuntimeError("boom"), results=results)
    assert not revision_state.evidence_status(tmp_path, state)[0]


def _execute_in_revision(tmp_path: Path, state, results: Path, monkeypatch, new: list[str]):
    """One real guarded + audited execution of the frozen cells plus ``new`` inside the revision."""
    manifest = revision_state.load_manifest(state.folder(tmp_path))
    monkeypatch.setattr(revision_state, "_ACTIVE", revision_state.ExecutionGuard(tmp_path, state, manifest))
    monkeypatch.setattr(execution_audit, "_results_dir", lambda plan: results)
    monkeypatch.setattr(execution_audit, "_read_design", lambda plan: _design(results))
    names = ["proposed_T1", "relevance_select_T1", "adaptive_retain_only", *new]
    inputs = SimpleNamespace(plan=SimpleNamespace(metrics=[], run_id="r1"), implementation=_impl(names))
    return execution_audit.run_audited(_fake_execution(results, []), None, inputs)


def _fake_latex(tmp_path: Path, monkeypatch, *, codes=None, log="", pdf=b"%PDF-1.5 x"):
    codes = list(codes or [0, 0, 0, 0])
    monkeypatch.setattr(latex_check.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(cmd, cwd, timeout):
        if "pdflatex" in cmd[0]:
            (cwd / "main.pdf").write_bytes(pdf)
            (cwd / "main.log").write_text(log, encoding="utf-8")
        else:
            (cwd / "main.bbl").write_text("\\bibitem{a}", encoding="utf-8")
        return codes.pop(0)

    monkeypatch.setattr(latex_check, "_run", run)
    paper = tmp_path / "paper"
    paper.mkdir(exist_ok=True)
    (paper / "main.tex").write_text("\\cite{a}\\bibliography{refs}", encoding="utf-8")
    (paper / "main.pdf").write_bytes(b"%PDF old")
    return paper


def test_final_check_requires_clean_exit_codes_and_log(tmp_path: Path, monkeypatch):
    paper = _fake_latex(tmp_path, monkeypatch, codes=[0, 0, 1, 1])
    check = latex_check.final_check(paper)
    assert check.status == "failed" and "pdflatex_2 exited with 1" in check.reasons
    assert (paper / "main.pdf").read_bytes() == b"%PDF old"  # a failed build never replaces the paper's PDF

    paper = _fake_latex(tmp_path, monkeypatch, log="LaTeX Warning: Citation `a' on page 1 undefined on input")
    check = latex_check.final_check(paper)
    assert not check.ok and check.undefined_citations == ["a"]

    paper = _fake_latex(tmp_path, monkeypatch)
    check = latex_check.final_check(paper)
    assert check.ok and check.adopted and (paper / "main.pdf").read_bytes() == b"%PDF-1.5 x"

    monkeypatch.setattr(latex_check.shutil, "which", lambda name: None)
    assert latex_check.final_check(paper).status == "unverified"


def test_acceptance_closes_revision_only_when_everything_passes(tmp_path: Path, monkeypatch):
    state, results, _ = _open_revision(tmp_path, cap=5)
    _execute_in_revision(tmp_path, state, results, monkeypatch, ["abl_T1"])
    failed_pdf = latex_check.FinalCheck("failed", ["pdflatex_3 exited with 1"])
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete", paper_check=failed_pdf,
                                     revision=state)
    assert record["deliverable"] is False and record["checks"]["evidence_passed"]
    # not accepted is not closed: the revision stays active for its repair
    assert revision_state.load(state.folder(tmp_path) / "revision.json").status == revision_state.NEEDS_REPAIR

    record = runner.write_acceptance(tmp_path, results, pipeline_status="blocked",
                                     paper_check=latex_check.FinalCheck("passed"), revision=state)
    assert record["deliverable"] is False and state.status == revision_state.NEEDS_REPAIR  # not finished
    record = runner.write_acceptance(tmp_path, results, pipeline_status="complete",
                                     paper_check=latex_check.FinalCheck("passed"), revision=state)
    assert record["deliverable"] and state.status == revision_state.ACCEPTED
