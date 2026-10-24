"""Unit tests for jiuwenswarm.agents.harness.common.paper_pipeline (no model calls)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import openjiuwen.harness.subagents as subagents
from jiuwenswarm.agents.harness.common.paper_pipeline import code_agent_budget
from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import scan_log
from jiuwenswarm.agents.harness.common.paper_pipeline.runner import (
    PaperRunOptions,
    find_run_id,
    resume_run,
)


@pytest.fixture
def fake_factory(monkeypatch):
    calls: list[dict] = []

    def create_code_agent(*args, **kwargs):
        calls.append(kwargs)
        return "agent"

    monkeypatch.setattr(subagents, "create_code_agent", create_code_agent)
    return calls


def test_fix_defaults_max_iterations(fake_factory):
    assert code_agent_budget.install_code_agent_iteration_fix(80) == 80
    subagents.create_code_agent("model", enable_task_loop=True)
    assert fake_factory[-1]["max_iterations"] == 80


def test_fix_keeps_explicit_value(fake_factory):
    code_agent_budget.install_code_agent_iteration_fix(80)
    subagents.create_code_agent("model", max_iterations=7)
    assert fake_factory[-1]["max_iterations"] == 7


def test_fix_is_idempotent_and_updates_limit(fake_factory):
    code_agent_budget.install_code_agent_iteration_fix(80)
    wrapped = subagents.create_code_agent
    code_agent_budget.install_code_agent_iteration_fix(120)
    assert subagents.create_code_agent is wrapped
    subagents.create_code_agent("model")
    assert fake_factory[-1]["max_iterations"] == 120


def test_fix_reads_env_var(fake_factory, monkeypatch):
    monkeypatch.setenv(code_agent_budget.ENV_VAR, "33")
    assert code_agent_budget.install_code_agent_iteration_fix() == 33


def test_scan_log_reports_undefined(tmp_path: Path):
    log = tmp_path / "main.log"
    log.write_text(
        "Package natbib Warning: Citation `wu2026abc' on page 2 undefined on input line 10.\n"
        "LaTeX Warning: Reference `fig:1' on page 3 undefined on input line 20.\n"
        "Package natbib Warning: Citation `wu2026abc' on page 4 undefined on input line 30.\n",
        encoding="utf-8",
    )
    check = scan_log(log)
    assert not check.ok
    assert check.undefined_citations == ["wu2026abc"]
    assert check.undefined_references == ["fig:1"]


def test_scan_log_clean(tmp_path: Path):
    log = tmp_path / "main.log"
    log.write_text("Output written on main.pdf (12 pages).\n", encoding="utf-8")
    assert scan_log(log).ok


def test_rewrite_main_tex_to_iclr():
    from jiuwenswarm.agents.harness.common.paper_pipeline.iclr_template import rewrite_main_tex

    src = ("\\documentclass{article}\n\n\\usepackage[preprint]{neurips_2025}\n\\usepackage{hyperref}\n"
           "\\begin{document}\n\\bibliographystyle{plainnat}\n\\bibliography{refs}\n\\end{document}\n")
    out = rewrite_main_tex(src)
    assert "\\usepackage{iclr2027_conference,times}\n" in out
    assert "\\bibliographystyle{iclr2027_conference}" in out
    assert "neurips" not in out
    assert rewrite_main_tex(out) is None  # already converted


def test_convert_to_iclr_skips_without_template_files(tmp_path: Path, monkeypatch):
    from jiuwenswarm.agents.harness.common.paper_pipeline import iclr_template

    monkeypatch.setattr(iclr_template, "TEMPLATE_DIR", tmp_path / "no-templates")
    paper = tmp_path / "paper"
    paper.mkdir()
    src = "\\documentclass{article}\n\\usepackage{neurips_2025}\n\\begin{document}\n\\end{document}\n"
    (paper / "main.tex").write_text(src, encoding="utf-8")
    result = iclr_template.convert_to_iclr(paper)
    assert not result.converted
    assert "not installed" in result.note
    assert (paper / "main.tex").read_text(encoding="utf-8") == src


def test_find_run_id(tmp_path: Path):
    for run_id in ("bl1-r1", "other-r1"):
        (tmp_path / "experiments" / run_id / "manager").mkdir(parents=True)
        (tmp_path / "experiments" / run_id / "manager" / "state.json").write_text("{}", encoding="utf-8")
    assert find_run_id(tmp_path, "bl1") == "bl1-r1"
    with pytest.raises(ValueError):
        find_run_id(tmp_path, "missing")
    with pytest.raises(FileNotFoundError):
        find_run_id(tmp_path / "empty", "bl1")


def test_resume_rejects_unknown_counter(tmp_path: Path):
    opts = PaperRunOptions(run_dir=tmp_path, topic="t", reset_counters=["rounds_used"])
    with pytest.raises(ValueError):
        asyncio.run(resume_run(opts))
