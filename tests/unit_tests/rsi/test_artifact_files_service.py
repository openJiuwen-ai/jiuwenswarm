from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from jiuwenswarm.agents.harness.common.rsi.artifact_files_service import (
    RsiArtifactFilesService,
)
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import (
    provider_best_artifact,
    provider_report_to_web,
)
from jiuwenswarm.agents.harness.common.rsi.errors import RsiArtifactNotFound, RsiBadRequest, RsiPathInvalid


@pytest.mark.parametrize("user_id", ["", "tenant user&other=1"])
def test_download_fields_use_real_token_builder_and_encode_user_route(tmp_path, user_id):
    from jiuwenswarm.agents.harness.common.rsi.services import _download_fields
    from jiuwenswarm.agents.harness.common.tools.web_file_download import validate_file_download_token

    artifact = tmp_path / "result.zip"
    artifact.write_bytes(b"artifact")
    fields = _download_fields(str(artifact), {"session_id": "rsi-session", "_download_user_id": user_id})
    query = parse_qs(urlparse(fields["download_url"]).query)
    assert query["token"] == [fields["download_token"]]
    assert query.get("user_id", []) == ([user_id] if user_id else [])
    assert "other" not in query
    assert validate_file_download_token(fields["download_token"]) is not None


@pytest.mark.parametrize("artifact_id", ["AROOT", "Aepoch-001"])
def test_harness_snapshot_lists_package_files_without_changing_task_state(tmp_path: Path, artifact_id: str):
    task_id = "rsi-harness"
    root = tmp_path / task_id
    (root / "snapshots").mkdir(parents=True)
    metadata = '{"scenario":"HARNESS","active_ref_in_use":true}'
    (root / "task.json").write_text(metadata, encoding="utf-8")
    with zipfile.ZipFile(root / "snapshots" / f"{artifact_id}.zip", "w") as package:
        package.writestr("HARNESS_REFS.yaml", "harness_refs: {}\n")
        package.writestr("PRIMARY_package/README.md", "package overview\n")
        package.writestr("PRIMARY_package/harness_config.yaml", "name: example\n")
        package.writestr("PRIMARY_package/prompt_sections/files/identity.md", "Be precise.\n")
        package.writestr("PRIMARY_package/tools/helper.py", "answer = 42\n")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    listed = service.list_files({"task_id": task_id, "artifact_id": artifact_id})
    assert Path(listed["initial_path"]).parts[-2:] == ("PRIMARY_package", "harness_config.yaml")
    assert {f["name"] for f in listed["files"]} >= {"harness_config.yaml", "identity.md", "helper.py"}
    config = service.read_file({"task_id": task_id, "path": listed["initial_path"]})
    assert config["encoding"] == "text" and config["type"].startswith("text/")
    assert config["content"] == "name: example\n"
    assert (root / "task.json").read_text(encoding="utf-8") == metadata


def test_harness_snapshot_versions_are_independent(tmp_path: Path):
    root = tmp_path / "rsi-versions"
    (root / "snapshots").mkdir(parents=True)
    (root / "task.json").write_text("{}", encoding="utf-8")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    for name, content in [("Aepoch-001", "first"), ("Aepoch-003", "third")]:
        with zipfile.ZipFile(root / "snapshots" / f"{name}.zip", "w") as package:
            package.writestr("package/manifest.json", json.dumps({"name": content}))
        listed = service.list_files({"task_id": root.name, "artifact_id": name})
        file = service.read_file({"task_id": root.name, "path": listed["initial_path"]})
        assert json.loads(file["content"])["name"] == content


@pytest.mark.parametrize("artifact_id", ["../../other", "A/epoch-001", "..\\other", "/absolute"])
def test_harness_snapshot_id_cannot_select_an_arbitrary_path(tmp_path: Path, artifact_id: str):
    root = tmp_path / "rsi-safe"
    root.mkdir()
    (root / "task.json").write_text("{}", encoding="utf-8")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    with pytest.raises(RsiBadRequest):
        service.list_files({"task_id": root.name, "artifact_id": artifact_id})


def test_snapshot_selector_rejects_ambiguity_and_missing_snapshot(tmp_path: Path):
    root = tmp_path / "rsi-safe"
    root.mkdir()
    (root / "task.json").write_text("{}", encoding="utf-8")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    with pytest.raises(RsiBadRequest):
        service.list_files({"task_id": root.name, "artifact_id": "AROOT", "path": "run"})
    with pytest.raises(RsiArtifactNotFound):
        service.list_files({"task_id": root.name, "artifact_id": "AROOT"})


def test_snapshot_cannot_escape_via_archive_members_or_symlinks(tmp_path: Path):
    root = tmp_path / "rsi-archive-boundary"
    (root / "snapshots").mkdir(parents=True)
    (root / "task.json").write_text("{}", encoding="utf-8")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    with zipfile.ZipFile(root / "snapshots/AROOT.zip", "w") as package:
        package.writestr("../models/private.yaml", "private\n")
    with pytest.raises(RsiPathInvalid):
        service.list_files({"task_id": root.name, "artifact_id": "AROOT"})
    assert not (root / "models/private.yaml").exists()
    secret = tmp_path / "outside.zip"
    with zipfile.ZipFile(secret, "w") as package:
        package.writestr("private.yaml", "private\n")
    link = root / "snapshots/Aepoch-001.zip"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")
    with pytest.raises(RsiPathInvalid):
        service.list_files({"task_id": root.name, "artifact_id": "Aepoch-001"})


@pytest.mark.parametrize("suffix", ["yaml", "yml", "toml", "ts", "tsx", "py", "md"])
def test_known_text_files_remain_previewable_with_platform_mime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
):
    from jiuwenswarm.agents.harness.common.rsi import artifact_files_service as module

    monkeypatch.setattr(module.mimetypes, "guess_type", lambda name: ("application/octet-stream", None))
    root = tmp_path / "rsi-mime"
    (root / "run").mkdir(parents=True)
    (root / "task.json").write_text("{}", encoding="utf-8")
    file = root / "run" / f"config.{suffix}"
    file.write_text("hello\n", encoding="utf-8")
    service = RsiArtifactFilesService(SimpleNamespace(tasks_root=tmp_path))
    listed = service.list_files({"task_id": root.name, "path": str(file)})
    assert listed["files"][0]["type"].startswith("text/")
    assert service.read_file({"task_id": root.name, "path": str(file)})["encoding"] == "text"


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
