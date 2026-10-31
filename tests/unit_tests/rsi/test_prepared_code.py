from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import (
    set_project_root,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.code_implementation.schemas import (
    CodeImplementationInput,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_design.schemas import (
    ExperimentPlan,
)


def _agent_type():
    module_name = "jiuwenswarm.agents.harness.common.rsi.prepared_code"
    assert importlib.util.find_spec(module_name) is not None, "prepared research adapter missing"
    return importlib.import_module(module_name).PreparedCodeAgent


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path):
    set_project_root(tmp_path / "run")
    yield
    set_project_root(None)


def _request(run_id: str = "study-001") -> CodeImplementationInput:
    now = datetime.now(UTC)
    return CodeImplementationInput(
        plan=ExperimentPlan(
            run_id=run_id,
            design_session_id="prepared-study",
            design_path="",
            code_agent_instruction_path="",
            created_at=now,
            updated_at=now,
        )
    )


def _source(tmp_path: Path, *, fail: bool = False) -> Path:
    source = tmp_path / "public-source"
    source.mkdir()
    source.joinpath("dataset.json").write_text("[]", encoding="utf-8")
    source.joinpath("manifest.json").write_text("{}", encoding="utf-8")
    source.joinpath("PROTOCOL.md").write_text("# Frozen public protocol", encoding="utf-8")
    source.joinpath(".env").write_text("PRIVATE_TEST_SENTINEL=do-not-copy", encoding="utf-8")
    source.joinpath("results.json").write_text("not an input", encoding="utf-8")
    if fail:
        script = "import sys\nprint('offline smoke rejected', file=sys.stderr)\nsys.exit(7)\n"
    else:
        script = (
            "import argparse, json\nfrom pathlib import Path\n"
            "p=argparse.ArgumentParser()\n"
            "p.add_argument('--method', choices=['paired_study'])\n"
            "p.add_argument('--smoke-test', action='store_true')\n"
            "p.add_argument('--output', required=True)\n"
            "p.add_argument('--usage-ledger')\np.add_argument('--task-id')\n"
            "p.add_argument('--node-ref')\n"
            "a=p.parse_args()\n"
            "assert Path('dataset.json').is_file()\n"
            "payload={'method':a.method,'status':'completed','smoke_test':a.smoke_test,"
            "'model_call_count':0,'cwd':str(Path.cwd()),'task_id':a.task_id}\n"
            "Path(a.output).write_text(json.dumps(payload), encoding='utf-8')\n"
            "print('offline smoke completed')\n"
        )
    source.joinpath("run.py").write_text(script, encoding="utf-8")
    return source


@pytest.mark.asyncio
async def test_prepared_code_runs_offline_smoke_in_real_workspace(tmp_path: Path):
    source = _source(tmp_path)
    output = await _agent_type()(source, task_id="rsi-parent").arun(_request())
    manifest = output.implementation
    workspace = tmp_path / "run" / "experiments" / "study-001" / "generated_code"

    assert manifest.status == "ready"
    assert manifest.smoke_test_passed
    assert Path(manifest.workspace_dir) == workspace
    assert manifest.files == ["run.py", "dataset.json", "manifest.json", "PROTOCOL.md"]
    assert not workspace.joinpath(".env").exists()
    assert not workspace.joinpath("results.json").exists()
    assert manifest.variants[0].invocation == [
        sys.executable,
        "run.py",
        "--method",
        "paired_study",
        "--usage-ledger",
        str(tmp_path / "run" / "model_calls.jsonl"),
        "--task-id",
        "rsi-parent",
        "--node-ref",
        "study-001",
    ]
    smoke_root = workspace.parent / "smoke_test"
    metrics = json.loads(smoke_root.joinpath("paired_study.metrics.json").read_text(encoding="utf-8"))
    assert Path(metrics["cwd"]) == workspace
    assert metrics["model_call_count"] == 0
    assert metrics["smoke_test"]
    hashes = json.loads(workspace.joinpath("prepared_source_manifest.json").read_text(encoding="utf-8"))
    assert set(hashes["sha256"]) == set(manifest.files)
    assert "offline" in manifest.notes


@pytest.mark.asyncio
async def test_prepared_code_preserves_real_smoke_failure(tmp_path: Path):
    output = await _agent_type()(_source(tmp_path, fail=True), task_id="rsi-parent").arun(_request())
    manifest = output.implementation
    assert manifest.status == "failed"
    assert not manifest.smoke_test_passed
    assert "exit_code=7" in manifest.smoke_failures["paired_study"]
    assert "offline smoke rejected" in manifest.smoke_failures["paired_study"]


@pytest.mark.asyncio
async def test_prepared_code_rejects_run_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="run_id"):
        await _agent_type()(_source(tmp_path), task_id="rsi-parent").arun(_request("../../outside"))
    assert not tmp_path.joinpath("outside").exists()


@pytest.mark.asyncio
async def test_prepared_code_rejects_public_file_resolving_outside_source(tmp_path: Path):
    source = _source(tmp_path)
    outside = tmp_path / "external-dataset.json"
    outside.write_text("[]", encoding="utf-8")
    source.joinpath("dataset.json").unlink()
    try:
        source.joinpath("dataset.json").symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit creating a test symlink")
    with pytest.raises(ValueError, match="source"):
        await _agent_type()(source, task_id="rsi-parent").arun(_request())
