from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.rsi.paper_provider import (
    PaperProvider,
    _ExecutionOutcome,
    _NodeArtifactRequest,
    _read_usage_ledger,
    _safe_reporting_resource_paths,
)
from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters
from openjiuwen.rsi.artifact_rsi.request import ArtifactEngineRequest
from openjiuwen.rsi.artifact_rsi.program_opt import PuctProgramArtifactProvider


def _request(tasks_root: Path, task_id: str = "rsi-paper") -> ArtifactEngineRequest:
    run_dir = tasks_root / task_id / "run"
    return ArtifactEngineRequest(
        task_id=task_id,
        run_dir=str(run_dir),
        artifact_path=None,
        model=object(),
        max_iterations=1,
        optimization_instruction="improve the paper",
    )


def test_supplied_research_summary_precedes_directory_for_reporting(tmp_path: Path):
    source = tmp_path / "research"
    source.mkdir()
    (source / "research_summary.md").write_text("# Curated primary sources", encoding="utf-8")
    tasks_root = tmp_path / "tasks"
    provider = PaperProvider(tasks_root)
    request = replace(_request(tasks_root), artifact_path=str(source))
    captured = {}

    def manager(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="failed", summary="stop after observing inputs")

    provider._run_manager = manager
    provider._execute_request(request, threading.Event())
    assert captured["research_paths"][0] == "input/paper/research/research_summary.md"


def test_real_factory_registers_paper_provider(tmp_path: Path):
    adapters = build_rsi_adapters(tmp_path / "tasks", mode="real")

    assert set(adapters) == {"ARTIFACT:PAPER", "ARTIFACT:PROGRAM"}
    assert isinstance(adapters["ARTIFACT:PAPER"].provider, PaperProvider)
    assert isinstance(adapters["ARTIFACT:PROGRAM"].provider, PuctProgramArtifactProvider)
    assert adapters["ARTIFACT:PAPER"].supports_pause is False
    assert adapters["ARTIFACT:PAPER"].supports_resume is False


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("iclr", [False, True])
def test_paper_provider_wires_the_bundled_autoresearch_runtime(tmp_path: Path, prepared: bool, iclr: bool):
    captured: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, config, **kwargs):
            captured["config"] = config
            captured["components"] = kwargs

        async def arun(self, **kwargs):
            captured["request"] = kwargs
            from openjiuwen.rsi.usage import record_model_usage

            await record_model_usage(
                model="fake-paper-model",
                call_id="fake-paper-call",
                usage={"input_tokens": 11, "output_tokens": 5, "cache_read_tokens": 2},
            )
            return SimpleNamespace(status="complete", summary="dry run")

    model = SimpleNamespace(
        model_client_config=SimpleNamespace(
            client_provider="OpenAI",
            api_key="test-key",
            api_base="http://127.0.0.1/v1",
            timeout=17,
        ),
        model_config=SimpleNamespace(model_name="test-model"),
    )
    tasks_root = tmp_path / "tasks"
    run_dir = tasks_root / "rsi-paper" / "run"
    run_dir.mkdir(parents=True)
    request = ArtifactEngineRequest(
        task_id="rsi-paper",
        run_dir=str(run_dir),
        artifact_path=None,
        model=model,
        max_iterations=1,
        optimization_instruction="improve the paper",
        web_proxy="http://proxy.example.test:7890",
    )

    provider = PaperProvider(tasks_root, config_path=tmp_path / "pipeline.yaml")
    original_load = PaperProvider(tasks_root)._load_config

    def prepared_config(loader):
        config = original_load(loader)
        if prepared:
            config["code_implementation"]["prepared_source_dir"] = str(tmp_path / "prepared")
        if iclr:
            config["reporting"]["template_dir"] = "iclr-template"
        return config

    provider._load_config = prepared_config
    with patch(
        "openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline.manager.ManagerRuntime",
        FakeRuntime,
    ):
        terminal = provider._run_manager(
            request,
            run_dir=run_dir,
            manager_run_id="rsi-paper-iteration-001",
            topic="improve the paper",
            initial_prompt="task prompt",
            research_paths=[],
            artifact_path="/staged/paper",
        )

    assert terminal.status == "complete"
    code_agent = captured["components"].get("code_implementation")
    assert (type(code_agent).__name__ == "PreparedCodeAgent") is prepared
    reporter = captured["components"]["reporting"]._delegate
    assert (type(reporter).__name__ == "IclrReportingAgent") is iclr
    if iclr:
        assert reporter.template_dir == (tmp_path / "iclr-template").resolve()
    assert captured["components"]["artifact_path"] == "/staged/paper"  # type: ignore[index]
    assert captured["config"]["openjiuwen"] == {  # type: ignore[index]
        "base_url": "http://127.0.0.1/v1",
        "model": "test-model",
        "provider": "OpenAI",
        "timeout": 17.0,
    }
    assert captured["config"]["topic_survey"]["web_proxy"] == (  # type: ignore[index]
        "http://proxy.example.test:7890"
    )
    assert captured["config"]["topic_survey"]["search_scope"] == "global"  # type: ignore[index]
    assert captured["request"] == {  # type: ignore[index]
        "topic": "improve the paper",
        "research_paths": [],
        "run_id": "rsi-paper-iteration-001",
        "objective": "improve the paper",
        "initial_prompt": "task prompt",
        "task_mode": "create_new_paper",
    }
    usage = _read_usage_ledger(run_dir)
    assert usage is not None
    assert usage.tokens.input == 11
    assert usage.tokens.output == 5
    assert usage.tokens.cache_hit == 2
    assert usage.call_count == 1


def test_paper_provider_reads_usage_from_persisted_snapshots(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    task_id = "rsi-paper"
    run_dir = tasks_root / task_id / "run"
    provider = PaperProvider(tasks_root)
    provider._initialize_snapshots(task_id, run_dir, 1)  # noqa: SLF001 - snapshot contract
    (run_dir / "model_calls.jsonl").write_text(
        json.dumps(
            {
                "event_id": 1,
                "task_id": task_id,
                "call_id": "call-1",
                "model_call": {
                    "model": "paper-model",
                    "call_count": 1,
                    "tokens": {"input": 4, "output": 9, "cache_hit": 1},
                    "status": "succeeded",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    state = provider.read_state(task_id)
    report = provider.read_report(task_id)
    assert state.usage is not None
    assert report.usage is not None
    assert state.usage.tokens.input == report.usage.tokens.input == 4
    assert state.usage.tokens.output == report.usage.tokens.output == 9
    assert state.usage.call_count == report.usage.call_count == 1


def test_paper_provider_accepts_a_regular_file_and_stages_it(tmp_path: Path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper input")
    tasks_root = tmp_path / "tasks"
    run_dir = tasks_root / "rsi-paper" / "run"
    provider = PaperProvider(tasks_root)

    validation = provider.validate_input(str(source))
    assert validation.valid is True

    staged = provider._stage_input_file(source, run_dir)  # noqa: SLF001 - input staging contract
    assert staged == [run_dir / "input" / "paper" / "paper.pdf"]
    assert staged[0].read_bytes() == b"paper input"


def test_paper_provider_accepts_and_stages_a_paper_directory(tmp_path: Path):
    source = tmp_path / "paper"
    (source / "sections").mkdir(parents=True)
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nPaper\n\\end{document}\n",
        encoding="utf-8",
    )
    (source / "sections" / "method.tex").write_text("\\section{Method}\n", encoding="utf-8")

    run_dir = tmp_path / "tasks" / "rsi-paper" / "run"
    provider = PaperProvider(tmp_path / "tasks")

    assert provider.validate_input(str(source)).valid is True
    staged = provider._stage_input_file(source, run_dir)  # noqa: SLF001 - input staging contract

    assert staged == [run_dir / "input" / "paper" / "paper"]
    assert (staged[0] / "main.tex").is_file()
    assert (staged[0] / "sections" / "method.tex").is_file()


def test_paper_provider_passes_the_staged_artifact_to_manager(tmp_path: Path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "paper_facts.json").write_text("{}", encoding="utf-8")
    (source / "task_spec.json").write_text("{}", encoding="utf-8")
    tasks_root = tmp_path / "tasks"
    run_dir = tasks_root / "rsi-paper" / "run"
    request = ArtifactEngineRequest(
        task_id="rsi-paper",
        run_dir=str(run_dir),
        artifact_path=str(source),
        model=object(),
        max_iterations=1,
        optimization_instruction="improve the paper",
    )
    provider = PaperProvider(tasks_root)
    captured: dict[str, object] = {}

    def fake_manager(*args, **kwargs):
        del args
        captured.update(kwargs)
        return SimpleNamespace(status="complete", summary="dry run")

    with patch.object(provider, "_run_manager", side_effect=fake_manager):
        outcome = provider._execute_request(request, threading.Event())

    assert outcome.status == "completed"
    staged = run_dir / "input" / "paper" / "paper"
    assert captured["artifact_path"] == str(staged)
    assert (staged / "paper_facts.json").is_file()


def test_reporting_resource_order_keeps_binary_paper_from_utf8_reader():
    paths = [
        "input/paper/paper",
        "data/outputs/topic_survey/run/research_summary.md",
        "data/outputs/topic_survey/run/source.pdf",
    ]

    assert _safe_reporting_resource_paths(paths) == [
        "data/outputs/topic_survey/run/research_summary.md",
        "input/paper/paper",
        "data/outputs/topic_survey/run/source.pdf",
    ]


def test_reporting_resource_order_preserves_a_paper_source_directory():
    paths = ["input/paper/paper"]

    assert _safe_reporting_resource_paths(paths) == paths


def test_paper_provider_warns_before_truncating_node_files(tmp_path: Path):
    run_dir = tmp_path / "run"
    files_dir = run_dir / "reports"
    files_dir.mkdir(parents=True)
    for index in range(130):
        (files_dir / f"report-{index:03d}.txt").write_text(str(index), encoding="utf-8")

    provider = PaperProvider(tmp_path / "tasks")
    with patch(
        "jiuwenswarm.agents.harness.common.rsi.paper_provider.logger.warning"
    ) as warning:
        files = provider._collect_provider_files(  # noqa: SLF001 - truncation contract
            run_dir,
            ["reports"],
            node_id="node-1",
        )

    assert len(files) == 128
    warning.assert_called_once()
    message = str(warning.call_args.args[0])
    assert "node %s reported %d files" in message
    assert warning.call_args.args[1:] == ("node-1", 130, 2, 128)


@pytest.mark.parametrize("raises_inside", [False, True])
def test_model_environment_filters_inaccessible_path_and_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises_inside: bool,
):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.latex_runtime import (
        LatexRuntime,
    )

    accessible = tmp_path / "tools"
    accessible.mkdir()
    denied = tmp_path / "WindowsApps"
    denied.mkdir()
    original_path = os.pathsep.join([str(accessible), str(denied), str(tmp_path / "missing")])
    monkeypatch.setenv("PATH", original_path)
    original_is_dir = Path.is_dir
    original_isdir = os.path.isdir

    def is_dir(path):
        if path == denied:
            raise PermissionError("WindowsApps directory is inaccessible")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(os.path, "isdir", lambda path: False if Path(path) == denied else original_isdir(path))
    expected = pytest.raises(RuntimeError, match="test context failure") if raises_inside else nullcontext()
    with expected:
        with PaperProvider._temporary_model_environment(object()):  # noqa: SLF001 - production PATH boundary
            runtime = LatexRuntime(None, None, tuple(Path(entry) for entry in os.environ["PATH"].split(os.pathsep)))
            runtime.with_environment()
            assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
            assert str(accessible) in os.environ["PATH"].split(os.pathsep)
            if raises_inside:
                raise RuntimeError("test context failure")
    assert os.environ["PATH"] == original_path


@pytest.mark.parametrize("host_timeout", [None, "1200"])
def test_model_environment_preserves_reporting_deadline(
    monkeypatch: pytest.MonkeyPatch, host_timeout: str | None,
):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent

    if host_timeout is None:
        monkeypatch.delenv("MODEL_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("MODEL_TIMEOUT", host_timeout)
    model = SimpleNamespace(model_client_config=SimpleNamespace(timeout=300))
    config = PaperProvider._configure_for_model(  # noqa: SLF001 - real SDK timeout precedence
        {"reporting": {"timeout": 900}}, model,
    )
    assert config["openjiuwen"]["timeout"] == 300
    reporting = ReportingAgent(config, model=model)
    with PaperProvider._temporary_model_environment(model):  # noqa: SLF001 - module-specific deadline
        assert int(reporting._setting("timeout", "MODEL_TIMEOUT", default="600")) == int(host_timeout or 900)
    assert os.environ.get("MODEL_TIMEOUT") == host_timeout


def test_paper_provider_logs_when_model_environment_lock_is_busy():
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(api_key="key", api_base="http://localhost"),
        model_config=SimpleNamespace(model_name="model"),
    )
    lock_started = threading.Event()
    release_lock = threading.Event()

    def hold_lock() -> None:
        with PaperProvider._MODEL_ENV_LOCK:  # noqa: SLF001 - lock observability contract
            lock_started.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_started.wait(timeout=1)

    waiter_done = threading.Event()

    def wait_for_lock() -> None:
        with PaperProvider._temporary_model_environment(model):  # noqa: SLF001
            pass
        waiter_done.set()

    with patch(
        "jiuwenswarm.agents.harness.common.rsi.paper_provider.logger.warning"
    ) as warning:
        waiter = threading.Thread(target=wait_for_lock)
        waiter.start()
        time.sleep(0.05)
        assert not waiter_done.is_set()
        release_lock.set()
        waiter.join(timeout=1)

    holder.join(timeout=1)
    assert waiter_done.is_set()
    assert any("等待进程级模型环境锁" in str(call.args[0]) for call in warning.call_args_list)


@pytest.mark.asyncio
async def test_paper_provider_projects_live_manager_reports_and_downloadable_package(
    tmp_path: Path,
):
    tasks_root = tmp_path / "tasks"
    task_id = "rsi-paper"
    run_dir = tasks_root / task_id / "run"
    provider = PaperProvider(tasks_root, poll_interval=0.05)
    report_written = threading.Event()
    release = threading.Event()

    def fake_execution(request: ArtifactEngineRequest, cancel_event: threading.Event):
        del request, cancel_event
        manager_root = run_dir / "experiments" / f"{task_id}-iteration-001"
        artifact = manager_root / "design" / "report.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("# generated paper design\n", encoding="utf-8")
        manager_state = manager_root / "manager" / "state.json"
        manager_state.parent.mkdir(parents=True, exist_ok=True)
        manager_state.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "report_id": "reporting:1:1",
                            "module": "reporting",
                            "mode": "run",
                            "attempt": 1,
                            "outcome": "succeeded",
                            "retryable": False,
                            "runtime_failure": "none",
                            "summary": "paper report compiled",
                            "artifact_paths": [
                                "experiments/"
                                f"{task_id}-iteration-001/design/report.md"
                            ],
                            "handoff": {"report_path": "design/report.md"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        report_written.set()
        release.wait(timeout=5)
        provider._make_iteration_package(  # noqa: SLF001 - seed the provider's final artifact
            run_dir,
            f"{task_id}-iteration-001",
            1,
        )
        return _ExecutionOutcome(
            "completed",
            "paper optimization completed",
            (f"{task_id}-iteration-001",),
        )

    provider._execute_request = fake_execution  # type: ignore[method-assign]
    events: list[str] = []

    async def on_event(event):
        events.append(event.event_type)

    running = asyncio.create_task(provider.run(_request(tasks_root), on_event=on_event))
    assert await asyncio.to_thread(report_written.wait, 2)
    for _ in range(20):
        if "node" in events:
            break
        await asyncio.sleep(0.05)
    assert "node" in events
    node = provider.get_tree(task_id).nodes[-1]
    assert node.summary == "paper report compiled"
    assert node.snapshot_artifact_id
    assert not node.extra["artifact_path"].endswith(".zip")
    assert Path(node.extra["artifact_path"]).is_dir()
    assert node.extra["artifacts"][0]["node_id"] == node.node_id

    release.set()
    result = await running

    assert result.status == "completed"
    artifact = provider.locate_artifact(task_id)
    assert artifact.name == "paper-optimization-001"
    assert Path(artifact.path).is_dir()
    assert provider.read_state(task_id).best_node_id != "ROOT"


@pytest.mark.skipif(os.name != "nt", reason="Physical NUL files are a Windows tool-output issue")
@pytest.mark.parametrize("declare_nul", [False, True])
def test_iteration_package_records_empty_physical_nul_exclusion(tmp_path: Path, declare_nul: bool):
    from jiuwenswarm.agents.harness.common.rsi.artifact_provenance_gate import ArtifactProvenanceGate, GateContext

    run_dir = tmp_path / "tasks" / "rsi-paper" / "run"
    manager_id = "rsi-paper-iteration-001"
    paper = run_dir / "experiments" / manager_id / "paper"
    paper.mkdir(parents=True)
    (paper / "main.pdf").write_bytes(b"%PDF-1.4\nretained real paper")
    physical_nul = Path("\\\\?\\" + str(paper / "NUL"))
    physical_nul.write_bytes(b"")
    state_path = paper.parent / "manager" / "state.json"
    state_path.parent.mkdir()
    declared = [paper.relative_to(run_dir).as_posix(), (paper / "main.pdf").relative_to(run_dir).as_posix()]
    if declare_nul:
        declared.append((paper / "NUL").relative_to(run_dir).as_posix())
    state = json.dumps({"reports":[{"module":"reporting", "mode":"run", "outcome":"succeeded", "artifact_paths":declared}]})
    state_path.write_text(state, encoding="utf-8")
    (run_dir / "model_calls.jsonl").write_text(json.dumps({"call_id":"actual-fixture", "model_call":{"tokens":{"input":19, "output":5}}}) + "\n", encoding="utf-8")
    try:
        provider = PaperProvider(tmp_path / "tasks")
        if declare_nul:
            with pytest.raises(shutil.Error, match="NUL"):
                provider._make_iteration_package(run_dir, manager_id, 1)
            assert physical_nul.is_file() and physical_nul.stat().st_size == 0
            assert state_path.read_text(encoding="utf-8") == state
            return
        with patch("jiuwenswarm.agents.harness.common.rsi.paper_provider.logger.warning") as warning:
            package = provider._make_iteration_package(run_dir, manager_id, 1)
        assert (package / "paper" / "main.pdf").read_bytes() == (paper / "main.pdf").read_bytes()
        assert "NUL" not in {path.name for path in (package / "paper").iterdir()}
        assert physical_nul.is_file() and physical_nul.stat().st_size == 0
        exclusions = json.loads((package / "__rsi_artifact__" / "packaging_exclusions.json").read_text())
        assert exclusions == {"reason":"zero_byte_windows_NUL_tool_output", "source_relative_paths":["paper/NUL"]}
        assert "zero-byte Windows NUL" in warning.call_args.args[0]
        assert state_path.read_text(encoding="utf-8") == state
        assert (package / "manager" / "state.json").read_text(encoding="utf-8") == state
        audit = ArtifactProvenanceGate().evaluate(GateContext("rsi-paper", manager_id, 1, run_dir))
        assert audit.decision == "PASS"
        artifact_check = next(check for check in audit.checks if check.id == "APG003")
        assert artifact_check.status == "PASS"
        state_path.write_text(json.dumps({"reports":[{"module":"reporting", "mode":"run", "outcome":"succeeded", "artifact_paths":declared+["missing.pdf"]}]}), encoding="utf-8")
        blocked = ArtifactProvenanceGate().evaluate(GateContext("rsi-paper", manager_id, 1, run_dir))
        assert blocked.decision == "BLOCKED"
        assert next(check for check in blocked.checks if check.id == "APG003").status == "FAIL"
    finally:
        physical_nul.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Physical NUL files are a Windows tool-output issue")
@pytest.mark.parametrize("declare_nul", [False, True])
def test_iteration_package_preserves_nonempty_physical_nul(tmp_path: Path, declare_nul: bool):
    run_dir = tmp_path / "tasks" / "rsi-paper" / "run"
    manager_id = "rsi-paper-iteration-001"
    paper = run_dir / "experiments" / manager_id / "paper"
    paper.mkdir(parents=True)
    physical_nul = Path("\\\\?\\" + str(paper / "NUL"))
    payload = b"meaningful artifact cannot be silently dropped"
    physical_nul.write_bytes(payload)
    (paper / "main.pdf").write_bytes(b"%PDF-1.4\nretained real paper")
    state_path = paper.parent / "manager" / "state.json"
    state_path.parent.mkdir()
    declared = [(paper / "NUL").relative_to(run_dir).as_posix()] if declare_nul else []
    state = json.dumps({"reports": [{"module": "reporting", "outcome": "succeeded", "artifact_paths": declared}]})
    state_path.write_text(state, encoding="utf-8")
    try:
        provider = PaperProvider(tmp_path / "tasks")
        if declare_nul:
            with pytest.raises(shutil.Error, match="NUL"):
                provider._make_iteration_package(run_dir, manager_id, 1)
        else:
            package = provider._make_iteration_package(run_dir, manager_id, 1)
            relocated = "__rsi_artifact__/windows_reserved_outputs/paper/reserved-NUL.bin"
            assert not PureWindowsPath(relocated).is_reserved()
            assert (package / relocated).read_bytes() == payload
            assert (package / "paper" / "main.pdf").read_bytes() == (paper / "main.pdf").read_bytes()
            assert "NUL" not in {path.name for path in (package / "paper").iterdir()}
            record = json.loads((package / "__rsi_artifact__" / "packaging_reserved_outputs.json").read_text())
            assert record == {"reason": "nonempty_windows_NUL_tool_output", "files": [{
                "source_relative_path": "paper/NUL", "relocated_relative_path": relocated,
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            }]}
            assert not (package / "__rsi_artifact__" / "packaging_exclusions.json").exists()
            assert (package / "manager" / "state.json").read_text(encoding="utf-8") == state
        assert physical_nul.read_bytes() == payload
        assert state_path.read_text(encoding="utf-8") == state
    finally:
        physical_nul.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Physical NUL files are a Windows tool-output issue")
def test_node_package_keeps_reporting_directory_files_with_physical_nul(tmp_path: Path):
    run_dir = tmp_path / "tasks" / "rsi-paper" / "run"
    paper = run_dir / "experiments" / "rsi-paper-iteration-001" / "paper"
    paper.mkdir(parents=True)
    (paper / "main.pdf").write_bytes(b"%PDF-1.4\nnode real artifact")
    physical_nul = Path("\\\\?\\" + str(paper / "NUL"))
    physical_nul.write_bytes(b"")
    try:
        provider = PaperProvider(tmp_path / "tasks")
        declared = [paper.relative_to(run_dir).as_posix()]
        package = provider._make_node_package(_NodeArtifactRequest(
            task_id="rsi-paper", run_dir=run_dir, iteration=1, report_index=1,
            module="reporting", node_id="reporting:1:1", raw_paths=declared,
        ))
        assert package is not None
        copied = package / paper.relative_to(run_dir) / "main.pdf"
        assert copied.read_bytes() == (paper / "main.pdf").read_bytes()
        assert declared == ["experiments/rsi-paper-iteration-001/paper"]
        assert "NUL" not in {path.name for path in copied.parent.iterdir()}
    finally:
        physical_nul.unlink()
