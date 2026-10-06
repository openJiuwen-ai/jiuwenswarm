"""Unit tests for the paper pipeline's rigor protocol: paired statistics, execution audit, prompt
addenda (no model calls)."""

from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.paper_pipeline import execution_audit, rigor_stats

# top-level `accuracy` is the mean of per-item `correct`: names differ, so the mapping is explicit
ACC = {"accuracy": "correct"}

def _variant(name: str, correct: list[int], *, preds: list[str] | None = None, tokens: float = 100.0) -> dict:
    preds = preds or [f"{name}-{i}" for i in range(len(correct))]
    records = [
        {"qid": f"q{i}", "predicted_answer": preds[i], "correct": bool(c), "total_tokens": tokens + i}
        for i, c in enumerate(correct)
    ]
    return {
        "accuracy": sum(correct) / len(correct),
        "total_tokens": sum(r["total_tokens"] for r in records) / len(records),
        "per_question": records,
    }


def test_mcnemar_exact_matches_binomial():
    assert rigor_stats.mcnemar_exact(0, 0) == 1.0
    # 10 discordant pairs all one way: p = 2 * 0.5**10
    assert math.isclose(rigor_stats.mcnemar_exact(10, 0), 2 / 1024)
    assert rigor_stats.mcnemar_exact(5, 5) == 1.0


def test_bootstrap_ci_is_seeded_and_brackets_mean():
    values = [1.0] * 30 + [0.0] * 20
    first = rigor_stats.bootstrap_ci(values)
    assert first == rigor_stats.bootstrap_ci(values)
    assert first[0] < 0.6 < first[1]


def test_compute_maps_top_level_metric_to_declared_per_item_field():
    variants = {"proposed": _variant("proposed", [1] * 40 + [0] * 10), "baseline": _variant("baseline", [1] * 25 + [0] * 25)}
    stats = rigor_stats.compute(variants, primary=["accuracy"], metric_fields=ACC)
    assert stats.primary == ["accuracy"]
    by_metric = {s.metric: s for s in stats.metrics["proposed"]}
    assert by_metric["accuracy"].field == "correct"
    assert by_metric["accuracy"].binary
    (pair,) = stats.pairs
    assert (pair.a, pair.b, pair.test) == ("proposed", "baseline", "exact_mcnemar")
    assert math.isclose(pair.mean_diff, 0.3)
    assert pair.ci_low > 0 and pair.p_value < 0.01


def test_compute_anchors_pairs_on_proposed():
    variants = {n: _variant(n, [1, 0] * 10) for n in ("proposed", "a", "b", "c")}
    stats = rigor_stats.compute(variants, primary=["accuracy"], metric_fields=ACC)
    assert sorted((p.a, p.b) for p in stats.pairs) == [("proposed", "a"), ("proposed", "b"), ("proposed", "c")]


def test_choose_pairs_compares_within_tier():
    names = sorted(["proposed_T1", "proposed_T2", "base_T1", "base_T2", "abl_T1", "abl_T2", "full_history"])
    pairs = rigor_stats.choose_pairs(names)
    assert ("proposed_T1", "base_T1") in pairs and ("proposed_T2", "abl_T2") in pairs
    assert ("proposed_T1", "full_history") in pairs
    assert all(rigor_stats.split_condition(b)[1] in ("", rigor_stats.split_condition(a)[1]) for a, b in pairs)
    assert len(pairs) == 6  # 2 tiers x (2 same-tier rivals + 1 reference), not 21


def test_budget_guard_balance_floor(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline.budget_guard import BudgetExceeded, BudgetGuard

    ledger = tmp_path / "none.jsonl"
    guard = BudgetGuard(ledger, balance_reader=lambda: 5.0, soft_floor=6.0, hard_floor=3.0)
    assert "balance is down to 5.00" in guard.notice()
    assert BudgetGuard(ledger, balance_reader=lambda: None, soft_floor=6.0, hard_floor=3.0).notice() == ""
    with pytest.raises(BudgetExceeded):
        BudgetGuard(ledger, balance_reader=lambda: 2.5, soft_floor=6.0, hard_floor=3.0).notice()


def test_annotate_puts_citable_scalars_first():
    variants = {"proposed": _variant("proposed", [1] * 30 + [0] * 20), "baseline": _variant("baseline", [1] * 20 + [0] * 30)}
    stats = rigor_stats.compute(variants, primary=["accuracy"], metric_fields=ACC)
    rigor_stats.annotate(variants, stats)
    keys = list(variants["proposed"])
    assert keys[0] == "accuracy_ci95_low"
    assert "accuracy_diff_vs_baseline_p" in keys
    assert variants["baseline"]["accuracy_diff_vs_proposed"] == -variants["proposed"]["accuracy_diff_vs_baseline"]
    assert variants["proposed"]["per_question"]  # original payload kept


def test_declared_metrics_parses_design_headings():
    design = "## Decision Metrics\n\n**Metric answer_accuracy:**\n- Metric answer_accuracy 0 [current]: ...\n- Metric total_tokens 0 [current]: ..."
    assert execution_audit.declared_metrics(design, ["extra"]) == ["answer_accuracy", "total_tokens", "extra"]


def _codes(findings):
    return {(f.level, f.code) for f in findings}


def test_audit_clean_run(tmp_path: Path):
    variants = {"proposed": _variant("proposed", [1, 0] * 60), "baseline": _variant("baseline", [0, 1] * 60)}
    paths = {}
    for name in variants:
        paths[name] = tmp_path / f"{name}.metrics.json"
        paths[name].write_text("{}")
    findings = execution_audit.audit(variants, declared=["accuracy"], started_at=time.time() - 5, metrics_paths=paths)
    assert findings == []


def test_audit_flags_identical_outputs_and_underpowered():
    same = [f"p{i}" for i in range(20)]
    variants = {
        "proposed": _variant("proposed", [1, 0] * 10, preds=same),
        "baseline": _variant("baseline", [1, 0] * 10, preds=same),
    }
    codes = _codes(execution_audit.audit(variants, declared=["accuracy"], started_at=0, metrics_paths={}))
    assert ("error", "IDENTICAL_OUTPUTS") in codes
    assert ("warn", "UNDERPOWERED") in codes


def test_audit_flags_missing_metric_stale_file_and_smoke_sized(tmp_path: Path):
    stale = tmp_path / "proposed.metrics.json"
    stale.write_text("{}")
    variants = {"proposed": {"other": 1.0, "per_question": [{"qid": "q0", "correct": True}]}}
    codes = _codes(execution_audit.audit(
        variants, declared=["accuracy"], started_at=time.time() + 60, metrics_paths={"proposed": stale}
    ))
    assert {("error", "MISSING_PRIMARY_METRIC"), ("error", "STALE_METRICS"), ("error", "SMOKE_SIZED")} <= codes


def test_audit_flags_item_set_mismatch_and_nan():
    variants = {"proposed": _variant("proposed", [1, 0] * 60), "baseline": _variant("baseline", [1, 0] * 50)}
    variants["baseline"]["accuracy"] = float("nan")
    codes = _codes(execution_audit.audit(variants, declared=["accuracy"], started_at=0, metrics_paths={}))
    assert ("warn", "ITEM_SET_MISMATCH") in codes
    assert ("error", "NON_FINITE_METRIC") in codes


def test_audit_flags_constraint_inactive_but_not_reference_arm():
    variants = {"proposed_t2": _variant("proposed_t2", [1, 0] * 60), "full_history": _variant("full_history", [0, 1] * 60)}
    for name, metrics in variants.items():
        for i, record in enumerate(metrics["per_question"]):
            record["constraint_active"] = i < 2  # 2 of 120 items
    findings = execution_audit.audit(variants, declared=["accuracy"], started_at=0, metrics_paths={})
    inactive = [f for f in findings if f.code == "CONSTRAINT_INACTIVE"]
    assert [f.level for f in inactive] == ["error"]
    assert inactive[0].detail.startswith("proposed_t2")


def test_compact_table_pivots_tiers_and_replaces_wide_tables():
    from jiuwenswarm.agents.harness.common.paper_pipeline import results_table

    variants = {
        "proposed_T1": {"answer_em": 0.42, "answer_em_ci95_low": 0.34, "answer_em_ci95_high": 0.5, "x_diff_vs_y": 1},
        "base_T1": {"answer_em": 0.32, "answer_em_ci95_low": 0.25, "answer_em_ci95_high": 0.39},
        "proposed_T2": {"answer_em": 0.4, "answer_em_ci95_low": 0.33, "answer_em_ci95_high": 0.48},
        "full_history": {"answer_em": 0.32, "answer_em_ci95_low": 0.25, "answer_em_ci95_high": 0.4},
    }
    table = results_table.compact_table(variants, n_items=150)
    assert "Method & T1 & T2 & pooled / unbudgeted" in table
    assert r"proposed & 0.420 [0.340, 0.500] & 0.400 [0.330, 0.480] & --" in table
    assert r"full\_history & -- & -- & 0.320 [0.250, 0.400]" in table
    wide = "\\begin{table}[h]\n\\begin{tabular}{l" + "r" * 40 + "}\nx\n\\end{tabular}\\label{tab:r}\n\\end{table}"
    narrow = "\\begin{table}[h]\n\\begin{tabular}{lrr}\ny\n\\end{tabular}\n\\end{table}"
    text, count = results_table.replace_wide_tables(wide + "\n" + narrow, table)
    assert count == 1 and narrow in text and r"\label{tab:r}" in text and "r" * 40 not in text


def _ledger(path: Path, rows: list[tuple[str, int, int, int]]) -> Path:
    import json

    with path.open("w", encoding="utf-8") as stream:
        for ts, inp, hit, out in rows:
            stream.write(json.dumps({"ts": ts, "model_call": {"model": "deepseek-flash",
                                     "tokens": {"input": inp, "cache_hit": hit, "output": out}}}) + "\n")
    return path


def test_ledger_cost_peak_and_off_peak(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline.budget_guard import ledger_cost

    # 2026-09-28 is a Monday: 02:00 UTC = 10:00 Beijing (peak), 12:00 UTC = 20:00 Beijing (off-peak)
    ledger = _ledger(tmp_path / "l.jsonl", [
        ("2026-09-28T02:00:00+00:00", 1_000_000, 0, 0),
        ("2026-09-28T12:00:00+00:00", 1_000_000, 1_000_000, 1_000_000),
    ])
    cost, tokens = ledger_cost(ledger)
    assert math.isclose(cost, 2.0 + 0.5 * (0.04 + 8.0))
    assert tokens["calls"] == 2 and tokens["cache_hit"] == 1_000_000


def test_budget_guard_soft_notice_and_hard_stop(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.paper_pipeline.budget_guard import BudgetExceeded, BudgetGuard

    ledger = _ledger(tmp_path / "l.jsonl", [("2026-09-28T02:00:00+00:00", 5_000_000, 0, 0)])  # 10 yuan
    assert BudgetGuard(ledger, soft_yuan=20, hard_yuan=30).notice() == ""
    assert "HOST BUDGET NOTICE" in BudgetGuard(ledger, soft_yuan=8, hard_yuan=30).notice()
    with pytest.raises(BudgetExceeded):
        BudgetGuard(ledger, soft_yuan=5, hard_yuan=9).notice()


def test_experiment_model_overrides_subprocess_env(tmp_path: Path, monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution

    from jiuwenswarm.agents.harness.common.paper_pipeline import experiment_model

    env_file = tmp_path / "exp.env"
    env_file.write_text("API_KEY=k2\nAPI_BASE=https://example.invalid/v1\nMODEL_NAME=m-default\n", encoding="utf-8")
    monkeypatch.setattr(execution, "_variant_env", execution._variant_env)  # restored after the test
    monkeypatch.setenv("API_KEY", "pipeline-key")
    override = experiment_model.load_experiment_env(env_file, "qwen-flash")
    assert experiment_model.install_experiment_model(override) == "qwen-flash"
    env = execution._variant_env(None)
    assert (env["API_KEY"], env["MODEL_NAME"]) == ("k2", "qwen-flash")


def test_experiment_key_rotates_without_restart_but_model_stays(tmp_path: Path, monkeypatch):
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution

    from jiuwenswarm.agents.harness.common.paper_pipeline import experiment_model

    env_file = tmp_path / "exp.env"
    env_file.write_text("API_KEY=old\nAPI_BASE=https://example.invalid/v1\nMODEL_NAME=x\n", encoding="utf-8")
    monkeypatch.setattr(execution, "_variant_env", execution._variant_env.__dict__.get("__wrapped__", execution._variant_env))
    experiment_model.install_experiment_model(experiment_model.load_experiment_env(env_file, "qwen-flash"), env_file)
    env_file.write_text("API_KEY=new\nAPI_BASE=https://example.invalid/v1\nMODEL_NAME=other-model\n", encoding="utf-8")
    env = execution._variant_env(None)
    assert (env["API_KEY"], env["MODEL_NAME"]) == ("new", "qwen-flash")


def test_install_appends_protocol_to_every_module_prompt():
    pytest.importorskip("openjiuwen.rsi.artifact_rsi.paper_opt.auto_research")
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.code_implementation import agent as code
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_design import agent as design
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager import agent as manager
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reflection import agent as reflection
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import agent as reporting

    from jiuwenswarm.agents.harness.common.paper_pipeline.research_protocol import install_research_protocol

    install_research_protocol()
    install_research_protocol()  # idempotent
    assert design._load_system_prompt().count("Rigor protocol") == 1
    assert manager._load_system_prompt().count("Rigor protocol") == 1
    assert code.CodeImplementationAgent._render_system_prompt().count("Rigor protocol") == 1
    assert reflection.ReflectionAgent._render_system_prompt().count("Rigor protocol") == 1
    assert Path(reporting._SYSTEM_PROMPT_PATH).read_text(encoding="utf-8").count("Rigor protocol") == 1
    assert "{SKILLS_DIR}" in Path(reporting._SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")
    assert hasattr(execution.ExperimentExecutionAgent.run, "__wrapped__")
