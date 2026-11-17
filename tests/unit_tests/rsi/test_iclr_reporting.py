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
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import figures, lint
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.bibliography import Bibliography
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
        assert "Treat supplied skill CLIs as black boxes" in text
        assert "at most three repair attempts per section" in text
        assert "unknown citation keys and actual compiler errors must be resolved" in text
        assert (source / name / "SKILL.md").read_bytes() == originals[name]
    writer = (skills / "ts-write/SKILL.md").read_text(encoding="utf-8")
    assert "650-850 words" in " ".join(writer.split()) and "350-500 words" in " ".join(writer.split())
    assert "2000-3000" not in writer and "900-1400" not in writer
    result = _measured_reporting_input().result
    assert lint._extract_unmatched_numbers("Invented result 0.12345", lint.known_numbers(result)) == [0.12345]
    assert lint.check_citations(r"Real \cite{known}; invented \cite{fabricated}", {"known"}) == ["fabricated"]


def test_actual_review_skill_requires_one_isolated_reviewer_call_per_response(tmp_path):
    source = Path(inspect.getfile(ReportingAgent)).parent / "skills" / "ts-review" / "SKILL.md"
    original = source.read_bytes()
    reporter = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    paper = tmp_path / "paper"
    paper.mkdir()
    skills = reporter._materialize_skills(paper, reporter._enabled_skill_dirs())
    review = (skills / "ts-review/SKILL.md").read_text(encoding="utf-8")
    assert "For each three-lens review pass" in review
    assert "at most one reviewer task_tool call per assistant response" in review
    assert "wait for its result before issuing the next" in review
    assert "Keep the draft unchanged between these three isolated reviews" in review
    assert "never include earlier reviewer outputs in a later prompt" in review
    for lens in ("Theory lens", "Empirical lens", "Applied lens"):
        assert lens in review
    assert "three fresh" in review and "after 3 total review passes" in review
    assert source.read_bytes() == original


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
    assert "For each three-lens review pass" in system
    assert "at most one reviewer task_tool call per assistant response" in system
    assert "Treat supplied skill CLIs as black boxes" in system
    assert "about 2860 words" in agent.system_prompt_builder.build()
    assert "about 2860 words" in agent._react_agent.config.prompt_template[0]["content"]
    assert "at most one reviewer task_tool call per assistant response" in agent._react_agent.config.prompt_template[0]["content"]
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
    compiler_stdout = json.dumps({"success": True, "pdf_path": str(paper / "main.pdf"), "error_lines": [], "log_tail": ""})

    def compile_once(command, **kwargs):
        assert command == [sys.executable, "-B", str(script), str(paper)]
        assert kwargs["cwd"] == paper
        assert kwargs["timeout"] == 330
        assert kwargs["check"] is False
        (paper / "main.pdf").write_bytes(b"real CLI output is independently checked upstream")
        return subprocess.CompletedProcess(command, 0, compiler_stdout, "compiler stderr")

    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=None) as run:
        with patch("subprocess.run", side_effect=compile_once) as compile_command:
            assert await agent._run_paper_agent(run_id="iteration-fallback", query="evidence") is None
        compile_command.assert_called_once()
        query = run.call_args.kwargs["query"]
        assert script.as_posix() in query
        assert "Do not use cd /d" in query
        assert "compile before optional bounded lint review" in query
        assert "retain soft numeric and layout notes" in query
    assert (paper / "logs/host_compile.stdout.log").read_text(encoding="utf-8") == compiler_stdout
    assert (paper / "logs/host_compile.stderr.log").read_text(encoding="utf-8") == "compiler stderr"
    evidence = json.loads((paper / "logs/host_compile.json").read_text(encoding="utf-8"))
    assert evidence["command"] == [sys.executable, "-B", str(script), str(paper)]
    assert evidence["returncode"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["missing_section", "empty_section", "missing_refs", "missing_script"])
async def test_final_compile_rejects_incomplete_source_instead_of_existing_pdf(tmp_path, monkeypatch, condition):
    paper, _ = _finished_sections(tmp_path, monkeypatch)
    (paper / "main.pdf").write_bytes(b"Existing PDF cannot replace required final source")
    if condition == "missing_section":
        (paper / "sections/method.tex").unlink()
    elif condition == "empty_section":
        (paper / "sections/method.tex").write_text("", encoding="utf-8")
    elif condition == "missing_refs":
        (paper / "refs.bib").unlink()
    elif condition == "missing_script":
        (paper / ".skills/ts-latex/scripts/compile.py").unlink()
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=None):
        with patch("subprocess.run") as compile_command:
            message = await agent._run_paper_agent(run_id="iteration-fallback", query="evidence")
            assert message and "host compile" in message
            compile_command.assert_not_called()
    assert not (paper / "main.pdf").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_error", [None, "reporting session timeout"])
async def test_final_compile_replaces_pdf_with_last_writer_source(tmp_path, monkeypatch, session_error):
    paper, script = _finished_sections(tmp_path, monkeypatch)
    old_pdf = b"PDF compiled before final abstract edit"
    (paper / "main.pdf").write_bytes(old_pdf)
    (tmp_path / "old-main.pdf").write_bytes(old_pdf)
    script.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "paper = Path(sys.argv[1])\n"
        "(paper / 'main.pdf').write_bytes((paper / 'sections/abstract.tex').read_bytes())\n"
        "print(json.dumps({'success': True, 'pdf_path': str(paper / 'main.pdf'), 'error_lines': [], 'log_tail': ''}))\n",
        encoding="utf-8",
    )
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())

    async def final_writer_edit(*, run_id, query):
        (paper / "sections/abstract.tex").write_text("Final corrected abstract.", encoding="utf-8")
        return session_error

    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, side_effect=final_writer_edit):
        assert await agent._run_paper_agent(run_id="iteration-fallback", query="evidence") == session_error
    assert (paper / "main.pdf").read_bytes() == b"Final corrected abstract."
    assert (tmp_path / "old-main.pdf").read_bytes() == old_pdf


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json_failure", "nonzero"])
@pytest.mark.parametrize("session_error", [None, "reporting session timeout"])
async def test_final_compile_failure_cannot_publish_existing_or_partial_pdf(tmp_path, monkeypatch, failure, session_error):
    paper, script = _finished_sections(tmp_path, monkeypatch)
    old_pdf = b"Old successful PDF"
    (paper / "main.pdf").write_bytes(old_pdf)
    (tmp_path / "old-main.pdf").write_bytes(old_pdf)
    script.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "paper = Path(sys.argv[1])\n"
        "(paper / 'main.pdf').write_bytes(b'Partial PDF from failed compile')\n"
        "print(json.dumps({'success': False, 'pdf_path': None, 'error_lines': ['actual compile failure'], 'log_tail': 'actual compile failure'}))\n"
        "print('compiler stderr', file=sys.stderr)\n"
        + ("raise SystemExit(1)\n" if failure == "nonzero" else ""),
        encoding="utf-8",
    )
    agent = _agent_type()({}, template_dir=_source(tmp_path), model=object())
    agent._latex_runtime = SimpleNamespace(available=True)
    with patch.object(ReportingAgent, "_run_paper_agent", new_callable=AsyncMock, return_value=session_error):
        message = await agent._run_paper_agent(run_id="iteration-fallback", query="evidence")
    assert message and "host compile" in message
    assert not (paper / "main.pdf").exists()
    output = agent._verify_and_build_output(
        run_id="iteration-fallback", workspace=paper, sections_dir=paper / "sections",
        refs_bib_path=paper / "refs.bib", figure_paths=[], known_keys={"verified"},
        result=_measured_reporting_input().result, session_error=message,
    )
    assert output.status == "failed" and output.paper_pdf_path is None
    if session_error:
        assert session_error in output.notes
    assert "compiler stderr" in (paper / "logs/host_compile.stderr.log").read_text(encoding="utf-8")
    assert (tmp_path / "old-main.pdf").read_bytes() == old_pdf


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


def _display_reporting_input():
    inputs = _measured_reporting_input()
    metrics = inputs.result.variants[0].metrics
    metrics.update({
        "generic_accuracy": 11 / 24, "paired_verifier_minus_generic": 1 / 24,
        "input_tokens": 9881, "output_tokens": 935,
        "client_call_seconds": 38.86, "elapsed_seconds": 39.4,
        "exact_mcnemar_p_two_sided": 1, "generic_only_correct": 0,
        "verifier_only_correct": 1, "valid_eval_count": 24,
    })
    metrics["accuracy_wilson_95"]["blindretry"] = [0.2789133373121099, 0.6492513464108051]
    for arm, bounds in metrics["accuracy_wilson_95"].items():
        for label, value in zip(("lower", "upper"), bounds):
            metrics[f"accuracy_wilson_95_{arm}_{label}"] = value
    return inputs


def test_paired_study_display_table_keeps_full_result_context_and_other_evidence(tmp_path):
    inputs = _display_reporting_input()
    original = inputs.model_dump_json()
    known = lint.known_numbers(inputs.result)
    result_json = tmp_path / "results.json"
    result_json.write_text(inputs.result.model_dump_json(), encoding="utf-8")
    original_json = result_json.read_bytes()
    bib = Bibliography("", {"Verified source": "verified"}, {"verified"}, ["Retained evidence"])
    context = {"objective": "Retained objective", "hypothesis": "Retained hypothesis"}
    baseline = ReportingAgent._build_evidence_blocks(inputs, context, "Retained background", bib, None)
    assert "\\begin{tabular}{l" + "r" * 19 + "}" in baseline["results"]
    evidence = _agent_type()._build_evidence_blocks(inputs, context, "Retained background", bib, None)
    assert "\\begin{tabular}{lrrr}" in evidence["results"]
    assert r"paired\_study & 0.375 & 0.4583 & 0.5 \\" in evidence["results"]
    assert evidence["results"].split(r"\begin{tabular}", 1)[0] == baseline["results"].split(r"\begin{tabular}", 1)[0]
    assert "input\\_tokens=9881" in evidence["results"]
    assert "model\\_call\\_count=54" in evidence["results"]
    for block in ("background", "method", "discussion"):
        assert evidence[block] == baseline[block]
    assert evidence["results"].split("Citable sources", 1)[1] == baseline["results"].split("Citable sources", 1)[1]
    assert inputs.model_dump_json() == original
    assert lint.known_numbers(inputs.result) == known
    assert result_json.read_bytes() == original_json
    assert len(inputs.result.variants) == 1 and inputs.result.variants[0].name == "paired_study"


def test_paired_study_display_figure_uses_three_accuracies_on_real_sdk_axes(tmp_path, monkeypatch):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    axes = []
    real_subplots = plt.subplots

    def capture_axes(*args, **kwargs):
        fig, ax = real_subplots(*args, **kwargs)
        axes.append(ax)
        return fig, ax

    monkeypatch.setattr(plt, "subplots", capture_axes)
    inputs = _display_reporting_input()
    original = inputs.model_dump_json()
    figure = figures.build_results_figure(inputs.result, tmp_path / "figures/results.pdf")
    assert len(axes[-1].get_xticklabels()) == 19
    assert "input_tokens" in [label.get_text() for label in axes[-1].get_xticklabels()]
    assert axes[-1].get_ylim()[1] > 9881
    _agent_type()._build_evidence_blocks(inputs, {}, None, Bibliography("", {}, set()), figure)
    assert [label.get_text() for label in axes[-1].get_xticklabels()] == [
        "baseline_accuracy", "generic_accuracy", "verifier_accuracy",
    ]
    assert [bar.get_height() for bar in axes[-1].patches] == [0.375, 11 / 24, 0.5]
    assert axes[-1].get_ylim()[1] < 1
    assert inputs.model_dump_json() == original
    assert figure.is_file() and figure.stat().st_size > 0


@pytest.mark.parametrize("condition", ["other_variant", "missing_accuracy", "multiple_variants", "boolean_accuracy"])
def test_paired_study_display_unknown_results_keep_parent_behavior(tmp_path, condition):
    inputs = _display_reporting_input()
    if condition == "other_variant":
        inputs.result.variants[0].name = "other_study"
    elif condition == "missing_accuracy":
        del inputs.result.variants[0].metrics["verifier_accuracy"]
    elif condition == "multiple_variants":
        inputs.result.variants.append(inputs.result.variants[0].model_copy(deep=True))
    else:
        inputs.result.variants[0].metrics["verifier_accuracy"] = True
    original = inputs.model_dump_json()
    figure = tmp_path / "results.pdf"
    figure.write_bytes(b"Existing parent figure")
    bib = Bibliography("", {}, set())
    baseline = ReportingAgent._build_evidence_blocks(inputs, {}, None, bib, figure)
    assert _agent_type()._build_evidence_blocks(inputs, {}, None, bib, figure) == baseline
    assert figure.read_bytes() == b"Existing parent figure"
    assert inputs.model_dump_json() == original


def test_writer_exact_facts_preserve_nested_findings_without_item_enumeration():
    inputs = _display_reporting_input()
    metrics = inputs.result.variants[0].metrics
    metrics.update({
        "correct_counts": {"initial": 217, "verifiedretry": 271, "lengthmatchedretry": 241},
        "cell_counts": {"sat:easy": {"verifiedretry": 47}},
        "cell_denominators": {"sat:easy": 99},
        "primary_diagnostic_vs_lengthmatched": {"diagnostic_only_correct": 32, "control_only_correct": 2},
        "missing_pair_worst_case_sensitivity": {"planned_denominator": 600, "comparisons": {
            "primary": {"paired_difference_full600_range": [0.03666666666666667, 0.06333333333333334],
                        "enumerated_possible_missing_outcomes": [{"missing_diagnostic_only": 0}] * 45}}},
        "diagnostic_minus_matched_prompt_tokens": [3, 10, 6],
        "length_match_audit": [{"id": "retained-raw-id", "matched_lengths_equal": True}] * 375,
        "historical_usage_all_successful": {"input_tokens": 325449, "cache_hit_tokens": 36480},
        "prior120_separate_context": {"not_pooled": True, "p": 0.453125},
        "claim_scope": "One model; decoded length is NOT token equivalent",
    })
    original = inputs.model_dump_json()
    known = lint.known_numbers(inputs.result)
    evidence = _agent_type()._build_evidence_blocks(inputs, {}, None, Bibliography("", {}, set()), None)
    facts = json.loads(evidence["verified_facts"].split("\n", 1)[1])
    assert facts["correct_counts"] == metrics["correct_counts"]
    assert facts["cell_counts"] == metrics["cell_counts"]
    assert facts["cell_denominators"] == metrics["cell_denominators"]
    assert facts["primary_diagnostic_vs_lengthmatched"] == metrics["primary_diagnostic_vs_lengthmatched"]
    assert facts["missing_pair_worst_case_sensitivity"]["comparisons"]["primary"] == {
        "paired_difference_full600_range": [0.03666666666666667, 0.06333333333333334]}
    assert facts["prompt_token_difference_summary"] == {"count": 3, "minimum": 3, "maximum": 10, "mean": 19 / 3, "total": 19}
    assert facts["decoded_length_match_summary"] == {"count": 375, "equal_count": 375}
    assert facts["historical_usage_all_successful"] == metrics["historical_usage_all_successful"]
    assert facts["prior120_separate_context"] == metrics["prior120_separate_context"]
    assert facts["claim_scope"] == metrics["claim_scope"]
    assert "enumerated_possible_missing_outcomes" not in evidence["verified_facts"]
    assert "retained-raw-id" not in evidence["verified_facts"]
    assert inputs.model_dump_json() == original
    assert lint.known_numbers(inputs.result) == known


def test_primary_lengthmatched_control_reaches_real_figure_and_table(tmp_path, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    axes = []
    real_subplots = plt.subplots
    def capture(*args, **kwargs):
        fig, ax = real_subplots(*args, **kwargs)
        axes.append(ax)
        return fig, ax
    monkeypatch.setattr(plt, "subplots", capture)
    inputs = _display_reporting_input()
    inputs.result.variants[0].metrics["lengthmatched_accuracy"] = 241 / 592
    original = inputs.model_dump_json()
    evidence = _agent_type()._build_evidence_blocks(inputs, {}, None, Bibliography("", {}, set()), tmp_path / "results.pdf")
    assert "lengthmatched_accuracy" in [label.get_text() for label in axes[-1].get_xticklabels()]
    assert len(axes[-1].patches) == 4
    assert 241 / 592 in [bar.get_height() for bar in axes[-1].patches]
    assert r"lengthmatched\_accuracy" in evidence["results"]
    assert r"\begin{tabular}{lrrrr}" in evidence["results"]
    assert inputs.model_dump_json() == original


def test_sdk_manifest_bibliography_recovers_keys_offline_and_rejects_unknown(tmp_path, monkeypatch):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import bibliography
    summary = tmp_path / "PROTOCOL.md"
    summary.write_text("Existing protocol with BibTeX text is not a survey-source record", encoding="utf-8")
    source = tmp_path / "source.html"
    source.write_text("<html>Retained local source</html>", encoding="utf-8")
    assert bibliography.build_bibliography(summary, network_enabled=False).known_keys == set()
    (tmp_path / "source-manifest.json").write_text(json.dumps({"sources": [{
        "title": "Verified local source", "url": "https://example.org/retained-source", "local_path": str(source),
        "citation": {"authors": ["Researcher Example"], "year": "2023", "venue": "Retained venue", "doi": "10.1234/retained"}
    }]}), encoding="utf-8")
    def forbid_network(*args, **kwargs):
        assert kwargs["network_enabled"] is False
        return None
    monkeypatch.setattr(bibliography, "try_enrich_from_network", forbid_network)
    bib = bibliography.build_bibliography(summary, network_enabled=False)
    assert len(bib.known_keys) == 1
    key = next(iter(bib.known_keys))
    assert "Verified local source" in bib.bib_text
    assert lint.check_citations(r"\cite{" + key + "}", bib.known_keys) == []
    assert lint.check_citations(r"\cite{unknown}", bib.known_keys)


def test_paired_study_table_preserves_sdk_evidence_without_display_heading(monkeypatch):
    inputs = _display_reporting_input()
    inputs.result.variants[0].metrics["lengthmatched_accuracy"] = 0.412345
    original = inputs.model_dump_json()
    known = lint.known_numbers(inputs.result)
    bib = Bibliography("", {"Verified source": "verified"}, {"verified"}, ["Retained evidence"])
    context = {"objective": "Retained objective", "hypothesis": "Retained hypothesis"}
    real_build = ReportingAgent._build_evidence_blocks
    marker = "\n\nHost-rendered results table — include exactly as given, do not redraw it:\n\n"

    def changed_sdk(*args):
        evidence = real_build(*args)
        evidence["results"] = evidence["results"].replace(marker, "\n\nSDK results display:\n\n")
        evidence["results"] += "\nRetained SDK numeric caveat: 0.1379."
        return evidence

    monkeypatch.setattr(ReportingAgent, "_build_evidence_blocks", staticmethod(changed_sdk))
    baseline = changed_sdk(inputs, context, "Retained background", bib, None)
    assert marker not in baseline["results"]
    evidence = _agent_type()._build_evidence_blocks(inputs, context, "Retained background", bib, None)
    assert evidence["results"].split(r"\begin{tabular}", 1)[0] == baseline["results"].split(r"\begin{tabular}", 1)[0]
    assert evidence["results"].split(r"\end{tabular}", 1)[1] == baseline["results"].split(r"\end{tabular}", 1)[1]
    assert r"\begin{tabular}{lrrrr}" in evidence["results"]
    assert r"lengthmatched\_accuracy" in evidence["results"]
    assert "0.4123" in evidence["results"]
    assert r"input\_tokens=9881" in evidence["results"]
    assert r"model\_call\_count=54" in evidence["results"]
    for block in ("background", "method", "discussion"):
        assert evidence[block] == baseline[block]
    assert json.loads(evidence["verified_facts"].split("\n", 1)[1])["lengthmatched_accuracy"] == 0.412345
    assert inputs.model_dump_json() == original
    assert lint.known_numbers(inputs.result) == known
