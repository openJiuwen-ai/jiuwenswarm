import asyncio
from importlib import import_module
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common import workspace
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_design.schemas import ExperimentPlan, ResearchBrief
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution.schemas import ExperimentResult, VariantResult
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import lint
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.schemas import ReportingInput
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.sections import DOCUMENT_ORDER

TEMPLATES = ("iclr2026_conference.sty", "iclr2026_conference.bst", "natbib.sty", "fancyhdr.sty")


def _source(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    for name in TEMPLATES:
        (source / name).write_text("public-template-" + name, encoding="utf-8")
    (source / "private.txt").write_text("must not copy", encoding="utf-8")
    return source


def _agent_type():
    return import_module("jiuwenswarm.agents.harness.common.rsi.iclr_reporting").IclrReportingAgent


def test_templates_are_in_fresh_workspace_before_upstream_builder(tmp_path, monkeypatch):
    from openjiuwen.harness.prompts import SystemPromptBuilder
    from openjiuwen.harness.schema.config import DeepAgentConfig

    monkeypatch.setattr(workspace, "_PROJECT_ROOT", tmp_path / "run")
    source = _source(tmp_path)
    paper = workspace.paper_workspace_dir("iteration-001")
    sentinel = SimpleNamespace(deep_config=DeepAgentConfig(system_prompt="upstream"),
                               system_prompt_builder=SystemPromptBuilder(), apply_prompt_builder_to_react_agent=lambda: None)

    def upstream(self, *, run_id):
        assert run_id == "iteration-001"
        for name in TEMPLATES:
            assert (paper / name).read_bytes() == (source / name).read_bytes()
        assert not (paper / "private.txt").exists()
        return sentinel

    with patch.object(ReportingAgent, "_build_paper_agent", autospec=True, side_effect=upstream) as build:
        agent = _agent_type()({}, template_dir=source, model=object())
        assert agent._build_paper_agent(run_id="iteration-001") is sentinel
        build.assert_called_once_with(agent, run_id="iteration-001")


@pytest.mark.asyncio
async def test_iclr_query_is_appended_and_real_session_result_is_preserved(tmp_path):
    source = _source(tmp_path)
    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value="upstream failure") as run:
        agent = _agent_type()({}, template_dir=source, model=object())
        assert await agent._run_paper_agent(run_id="iteration-001", query="original evidence") == "upstream failure"
        run.assert_awaited_once()
        query = run.call_args.kwargs["query"]
        assert query.startswith("original evidence\n")
        assert r"\usepackage{iclr2026_conference,times}" in query
        assert r"\bibliographystyle{iclr2026_conference}" in query
        assert r"\author{Anonymous Authors}" in query
        assert r"\iclrfinalcopy" in query
        assert "NeurIPS" in query
        assert "four decimal places" in query
        assert "Finish all seven sections, then compile before optional bounded lint review" in query
        assert "retain soft numeric and layout notes" in query
        assert "about 2860 words" in query


@pytest.mark.asyncio
async def test_actual_sdk_sandbox_can_read_materialized_templates(tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.sys_operation.cwd import init_cwd

    monkeypatch.setattr(workspace, "_PROJECT_ROOT", tmp_path / "run")
    source = _source(tmp_path)
    model = Model(
        model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="unused-test", api_base="http://127.0.0.1:1/v1", max_retries=0),
        model_config=ModelRequestConfig(model="offline-test"),
    )
    reporter = _agent_type()({"reporting":{"method_figure":{"enabled":False}}}, template_dir=source, model=model)
    monkeypatch.setattr(reporter, "_write_latex_runtime_config", lambda paper: None)
    reporter._latex_runtime = object()
    agent = reporter._build_paper_agent(run_id="iteration-sandbox")
    paper = workspace.paper_workspace_dir("iteration-sandbox")
    assert Path(agent.deep_config.workspace.root_path).resolve() == paper.resolve()
    assert agent.deep_config.restrict_to_work_dir is True
    init_cwd(str(paper), str(tmp_path / "run"), workspace=str(paper))
    for name in TEMPLATES:
        result = await agent.deep_config.sys_operation.fs().read_file(str(paper / name))
        assert result.data.content == "public-template-" + name


def test_actual_materialized_compile_assembles_iclr_anonymous_document(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_PROJECT_ROOT", tmp_path / "run")
    source = _source(tmp_path)
    paper = workspace.paper_workspace_dir("iteration-compile")
    (paper / "sections").mkdir(parents=True)
    (paper / "title.txt").write_text("Actual Assembly Test", encoding="utf-8")
    (paper / "sections" / "abstract.tex").write_text("Retained abstract evidence.", encoding="utf-8")
    upstream = Path(inspect.getfile(ReportingAgent)).parent / "skills" / "ts-latex" / "scripts" / "compile.py"
    original = upstream.read_bytes()
    reporter = _agent_type()({}, template_dir=source, model=object())
    skills = reporter._materialize_skills(paper, reporter._enabled_skill_dirs())
    script = skills / "ts-latex" / "scripts" / "compile.py"
    spec = importlib.util.spec_from_file_location("offline_iclr_compile", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("sys.argv", [str(script), str(paper)])
    with patch.object(module, "compile_document", return_value=SimpleNamespace(success=False, log_tail="offline assembly-only test")) as compiler:
        module.main()
        compiler.assert_called_once()
    document = (paper / "main.tex").read_text(encoding="utf-8")
    assert r"\usepackage{iclr2026_conference,times}" in document
    assert r"\bibliographystyle{iclr2026_conference}" in document
    assert r"\author{Anonymous Authors}" in document
    assert "neurips_2025" not in document
    assert r"\iclrfinalcopy" not in document
    assert "Retained abstract evidence." in document
    assert upstream.read_bytes() == original


def test_real_materialized_skills_use_short_budget_and_compile_before_bounded_review(tmp_path):
    source = Path(inspect.getfile(ReportingAgent)).parent / "skills"
    originals = {name: (source / name / "SKILL.md").read_bytes() for name in ("ts-write", "ts-review")}
    reporter = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    paper = tmp_path / "paper"
    paper.mkdir()
    skills = reporter._materialize_skills(paper, reporter._enabled_skill_dirs())
    for name in originals:
        text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
        assert "about 2860 words" in text
        assert "all seven drafts, including abstract, before any lint" in text
        assert "compile before optional review" in text
        assert "at most three repair attempts per section" in text
        assert "unknown citation keys and actual compiler errors must be resolved" in text
        assert (source / name / "SKILL.md").read_bytes() == originals[name]
    writer = (skills / "ts-write/SKILL.md").read_text(encoding="utf-8")
    assert "650-850 words" in " ".join(writer.split()) and "350-500 words" in " ".join(writer.split())
    assert "2000-3000" not in writer and "900-1400" not in writer
    result = _measured_reporting_input().result
    assert lint._extract_unmatched_numbers("Invented result 0.12345", lint.known_numbers(result)) == [0.12345]
    assert lint.check_citations(r"Real \cite{known}; invented \cite{fabricated}", {"known"}) == ["fabricated"]


def test_actual_sdk_writer_system_prompt_receives_same_short_completion_contract(tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

    monkeypatch.setattr(workspace, "_PROJECT_ROOT", tmp_path / "run")
    model = Model(
        model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="unused-test", api_base="http://127.0.0.1:1/v1", max_retries=0),
        model_config=ModelRequestConfig(model="offline-test"),
    )
    reporter = _agent_type()({"reporting": {"method_figure": {"enabled": False}}}, template_dir=_source(tmp_path), model=model)
    monkeypatch.setattr(reporter, "_write_latex_runtime_config", lambda paper: None)
    reporter._latex_runtime = object()
    original_builder = ReportingAgent._build_paper_agent
    before = {}

    def capture_original(self, *, run_id):
        original = original_builder(self, run_id=run_id)
        before["config"] = original.deep_config
        before["rails"] = {id(rail) for rail in original.configured_rails()}
        before["rail_types"] = sorted(type(rail).__name__ for rail in original.configured_rails())
        before["tools"] = [(card.id, card.name) for card in original.ability_manager.list()]
        assert original._react_agent._get_llm() is model
        return original

    with patch.object(ReportingAgent, "_build_paper_agent", autospec=True, side_effect=capture_original):
        agent = reporter._build_paper_agent(run_id="short-paper-sandbox")
    system = agent.deep_config.system_prompt
    assert "about 2860 words" in system
    assert "all seven drafts, including abstract, before any lint" in system
    assert "compile before optional review" in system
    assert "at most three repair attempts per section" in system
    assert "about 2860 words" in agent.system_prompt_builder.build()
    assert "about 2860 words" in agent._react_agent.config.prompt_template[0]["content"]
    assert agent.system_prompt_builder is agent._react_agent.system_prompt_builder
    assert {id(rail) for rail in agent.configured_rails()} == before["rails"]
    assert sorted(type(rail).__name__ for rail in agent.configured_rails()) == before["rail_types"]
    assert [(card.id, card.name) for card in agent.ability_manager.list()] == before["tools"]
    assert agent.deep_config.model is model and agent._react_agent._get_llm() is model
    assert agent.deep_config.sys_operation is before["config"].sys_operation
    assert agent.deep_config.workspace is before["config"].workspace
    assert agent.deep_config.max_iterations == 40
    assert agent.deep_config.restrict_to_work_dir is True


def _finished_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_PROJECT_ROOT", tmp_path / "run")
    paper = workspace.paper_workspace_dir("iteration-fallback")
    (paper / "sections").mkdir(parents=True)
    for name in DOCUMENT_ORDER:
        (paper / "sections" / f"{name}.tex").write_text("Retained evidence.", encoding="utf-8")
    (paper / "title.txt").write_text("Retained title", encoding="utf-8")
    (paper / "refs.bib").write_text("@article{verified,title={Real source}}", encoding="utf-8")
    script = paper / ".skills/ts-latex/scripts/compile.py"
    script.parent.mkdir(parents=True)
    script.write_text("# materialized skill", encoding="utf-8")
    return paper, script


@pytest.mark.asyncio
async def test_finished_sections_compile_via_actual_cli_contract_and_keep_evidence(tmp_path, monkeypatch):
    paper, script = _finished_sections(tmp_path, monkeypatch)
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())

    def compile_once(command, **kwargs):
        assert command == [sys.executable, "-B", str(script), str(paper)]
        assert kwargs["cwd"] == paper
        assert kwargs["timeout"] == 330
        assert kwargs["check"] is False
        (paper / "main.pdf").write_bytes(b"real CLI output is independently checked upstream")
        return subprocess.CompletedProcess(command, 0, "compiler stdout", "compiler stderr")

    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=None) as run:
        with patch("subprocess.run", side_effect=compile_once) as compile_command:
            assert await agent._run_paper_agent(run_id="iteration-fallback", query="evidence") is None
        compile_command.assert_called_once()
        query = run.call_args.kwargs["query"]
        assert script.as_posix() in query
        assert "Do not use cd /d" in query
        assert "compile before optional bounded lint review" in query
        assert "retain soft numeric and layout notes" in query
    assert (paper / "logs/host_compile.stdout.log").read_text(encoding="utf-8") == "compiler stdout"
    assert (paper / "logs/host_compile.stderr.log").read_text(encoding="utf-8") == "compiler stderr"
    evidence = json.loads((paper / "logs/host_compile.json").read_text(encoding="utf-8"))
    assert evidence["command"] == [sys.executable, "-B", str(script), str(paper)]
    assert evidence["returncode"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["missing_section", "empty_section", "missing_refs", "existing_pdf", "session_timeout"])
async def test_compile_fallback_preserves_incomplete_existing_and_failed_sessions(tmp_path, monkeypatch, condition):
    paper, _ = _finished_sections(tmp_path, monkeypatch)
    error = "reporting session timeout" if condition == "session_timeout" else None
    if condition == "missing_section":
        (paper / "sections/method.tex").unlink()
    elif condition == "empty_section":
        (paper / "sections/method.tex").write_text("", encoding="utf-8")
    elif condition == "missing_refs":
        (paper / "refs.bib").unlink()
    elif condition == "existing_pdf":
        (paper / "main.pdf").write_bytes(b"preserve existing PDF")
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=error):
        with patch("subprocess.run") as compile_command:
            assert await agent._run_paper_agent(run_id="iteration-fallback", query="evidence") == error
            compile_command.assert_not_called()
    if condition == "existing_pdf":
        assert (paper / "main.pdf").read_bytes() == b"preserve existing PDF"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "no_pdf", "nonzero"])
async def test_compile_fallback_failure_stays_diagnostic_and_retains_logs(tmp_path, monkeypatch, failure):
    paper, _ = _finished_sections(tmp_path, monkeypatch)
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    result = subprocess.CompletedProcess([], 1 if failure == "nonzero" else 0, "raw output", "raw error")
    effect = subprocess.TimeoutExpired("compile", 330, output=b"raw output", stderr=b"raw error") if failure == "timeout" else None
    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=None):
        with patch("subprocess.run", return_value=result, side_effect=effect):
            message = await agent._run_paper_agent(run_id="iteration-fallback", query="evidence")
    assert message and "host compile" in message
    assert not (paper / "main.pdf").exists()
    assert (paper / "logs/host_compile.stdout.log").read_text(encoding="utf-8") == "raw output"
    assert (paper / "logs/host_compile.stderr.log").read_text(encoding="utf-8") == "raw error"
    evidence = json.loads((paper / "logs/host_compile.json").read_text(encoding="utf-8"))
    assert evidence["timed_out"] is (failure == "timeout")


def _measured_reporting_input():
    run_id = "measured-reporting"
    result = ExperimentResult(run_id=run_id, workspace_dir=f"experiments/{run_id}", status="completed", variants=[
        VariantResult(name="paired_study", exit_code=0, log_path="results/paired.log", process_status="completed", metrics={
            "baseline_accuracy": 0.375, "generic_accuracy": 10 / 24, "verifier_accuracy": 0.5,
            "paired_verifier_minus_generic": 2 / 24,
            "accuracy_wilson_95": {
                "initial": [0.21159367559548778, 0.5729003755732572],
                "blindretry": [0.24467602609811764, 0.6116533413477124],
                "verifiedretry": [0.3142742581957335, 0.6857257418042665],
            },
            "input_tokens": 9739, "output_tokens": 904, "model_call_count": 54,
        }),
    ])
    plan = ExperimentPlan(run_id=run_id, design_session_id="design-session", design_path="design/plan.md",
                          code_agent_instruction_path="design/code.md", created_at="2026-10-05T00:00:00Z",
                          updated_at="2026-10-05T00:00:00Z")
    return ReportingInput(survey=ResearchBrief(resource_paths=["sources/summary.md"]), plan=plan, result=result)


@pytest.mark.asyncio
async def test_measured_nested_intervals_reach_real_sdk_lint_without_accepting_unknown(tmp_path):
    inputs = _measured_reporting_input()
    bounds = [value for pair in inputs.result.variants[0].metrics["accuracy_wilson_95"].values() for value in pair]
    rendered = [round(value, 4) for value in bounds]
    assert not any(value in lint.known_numbers(inputs.result) for value in rendered)
    text = "Measured confidence bounds: " + " ".join(f"{value:.4f}" for value in bounds)
    sentinel = object()

    async def upstream(normalized):
        known = lint.known_numbers(normalized.result)
        assert all(value in known for value in rendered)
        assert lint._extract_unmatched_numbers(text, known) == []
        assert lint._extract_unmatched_numbers("Measured difference: 0.0833", known) == []
        assert lint._extract_unmatched_numbers("Measured difference: 0.08333", known) == [0.08333]
        assert lint._extract_unmatched_numbers("Unknown result: 0.12345", known) == [0.12345]
        return sentinel

    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    with patch.object(ReportingAgent, "_run_async", new_callable=AsyncMock, side_effect=upstream):
        assert await agent.arun(inputs) is sentinel


@pytest.mark.parametrize("route", ["run", "arun"])
def test_reporting_routes_copy_only_result_and_preserve_all_original_data(tmp_path, route):
    inputs = _measured_reporting_input()
    original = inputs.model_dump_json()
    sentinel = object()
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    with patch.object(ReportingAgent, "_run_async", new_callable=AsyncMock, return_value=sentinel) as upstream:
        result = agent.run(inputs) if route == "run" else asyncio.run(agent.arun(inputs))
        assert result is sentinel
        normalized = upstream.await_args.args[0]
        assert normalized is not inputs and normalized.result is not inputs.result
        assert normalized.plan is inputs.plan and normalized.survey is inputs.survey
        assert normalized.result.status == inputs.result.status
        metrics = normalized.result.variants[0].metrics
        for name, value in inputs.result.variants[0].metrics.items():
            assert metrics[name] == value
        assert len(metrics) == len(inputs.result.variants[0].metrics) + 6
        assert metrics["accuracy_wilson_95_initial_lower"] == 0.21159367559548778
        metrics["accuracy_wilson_95"]["initial"][0] = -1
        metrics["input_tokens"] = -1
    assert inputs.model_dump_json() == original
