from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rsi.artifact_files_service import (
    RsiArtifactFilesService,
)
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import (
    provider_best_artifact,
    provider_report_to_web,
)


def test_zip_artifact_is_browsable_and_jsonl_is_text(tmp_path: Path):
    task_id = "rsi-paper"
    task_root = tmp_path / task_id
    artifact_dir = task_root / "run" / "artifacts"
    artifact_dir.mkdir(parents=True)
    (task_root / "task.json").write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
    package = artifact_dir / "paper-node.zip"

    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("input/paper/main.tex", "\\section{Original paper}\n")
        archive.writestr(
            "agent_workspace/output/artifacts/patched_paper/main.tex",
            "\\section{Generated paper}\n",
        )
        archive.writestr("trace/agent_trace.jsonl", '{"event":"node"}\n')
        archive.writestr("README.md", "A generated paper artifact\n")

    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    listed = service.list_files({"task_id": task_id, "path": str(package)})

    assert ".rsi_artifact_views" in Path(listed["root"]).parts
    assert Path(listed["initial_path"]).as_posix().endswith("patched_paper/main.tex")
    trace = next(item for item in listed["files"] if item["name"] == "agent_trace.jsonl")
    assert trace["type"] == "application/x-ndjson"

    content = service.read_file({"task_id": task_id, "path": trace["path"]})
    assert content["encoding"] == "text"
    assert content["content"] == '{"event":"node"}\n'


def test_directory_artifact_is_browsable_and_files_are_readable(tmp_path: Path):
    task_id = "rsi-paper-directory"
    task_root = tmp_path / task_id
    artifact_root = task_root / "run" / "artifacts" / "paper-optimization-001"
    (artifact_root / "paper" / "sections").mkdir(parents=True)
    (task_root / "task.json").write_text("{}", encoding="utf-8")
    (artifact_root / "paper" / "main.tex").write_text(
        "\\section{Generated paper}\n", encoding="utf-8", newline="\n"
    )
    (artifact_root / "paper" / "sections" / "method.tex").write_text(
        "\\section{Method}\n", encoding="utf-8"
    )

    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    listed = service.list_files({"task_id": task_id, "path": str(artifact_root)})

    assert listed["root"] == str(artifact_root)
    assert any(item["isDirectory"] for item in listed["files"])
    main = next(item for item in listed["files"] if item["name"] == "main.tex")
    content = service.read_file({"task_id": task_id, "path": main["path"]})
    assert content["encoding"] == "text"
    assert content["content"] == "\\section{Generated paper}\n"


def test_single_file_listing_does_not_include_unrelated_siblings(tmp_path: Path):
    task_id = "rsi-task"
    task_root = tmp_path / task_id
    artifact_dir = task_root / "run" / "artifacts"
    artifact_dir.mkdir(parents=True)
    (task_root / "task.json").write_text("{}", encoding="utf-8")
    selected = artifact_dir / "result.jsonl"
    selected.write_text('{"ok":true}\n', encoding="utf-8")
    (artifact_dir / "unrelated.log").write_text("ignore me\n", encoding="utf-8")

    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    listed = service.list_files({"task_id": task_id, "path": str(selected)})

    assert [item["name"] for item in listed["files"]] == ["result.jsonl"]
    assert listed["initial_path"].endswith("result.jsonl")


def test_best_artifact_prefers_latest_ref_for_best_node():
    report = {
        "status": "running",
        "best_node_id": "node-1",
        "artifact_index": [
            {"artifact_id": "module-package", "node_id": "node-1"},
            {"artifact_id": "iteration-package", "node_id": "node-1"},
        ],
    }

    assert provider_best_artifact(report)["artifact_id"] == "iteration-package"
    projected = provider_report_to_web(report, {"iteration": 1, "status": "running"})
    assert projected["metrics"]["best_artifact_id"] == "iteration-package"


def test_provider_paper_scores_reach_the_web_report():
    projected = provider_report_to_web(
        {
            "status": "completed",
            "score": 8.4,
            "baseline": 7.1,
            "artifact_index": [],
        },
        {"iteration": 2, "status": "completed"},
    )

    assert projected["best_score"] == 8.4
    assert projected["baseline"] == 7.1
