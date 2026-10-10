"""Behavior tests for the bundled reproducible-experiment skill runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[3]
    / "jiuwenswarm"
    / "resources"
    / "agent"
    / "workspace"
    / "skills"
    / "reproducible-experiment"
    / "scripts"
    / "reproducible_experiment.py"
)


def _spec(project: Path, command: list[str], artifacts: list[str]) -> Path:
    path = project / "experiment.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment_id": "test-experiment",
                "command": command,
                "cwd": ".",
                "seed": 42,
                "timeout_seconds": 30,
                "expected_artifacts": artifacts,
                "env_allowlist": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(*args: object) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def test_run_and_verify_success(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('result.json').write_text('{\"score\": 1}')",
        ],
        ["result.json"],
    )
    output = tmp_path / "run"

    completed = _run(
        "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
    )
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["passed"] is True
    assert manifest["seed"] == 42
    assert manifest["spec"]["path"] == "spec.json"
    assert manifest["artifacts"][0]["path"] == "result.json"

    verified = _run(
        "verify",
        "--manifest",
        output / "manifest.json",
        "--project-root",
        tmp_path,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["verified"] is True


def test_verify_detects_artifact_tampering(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "open('result.txt', 'w').write('original')"],
        ["result.txt"],
    )
    output = tmp_path / "run"
    assert (
        _run(
            "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
        ).returncode
        == 0
    )
    (tmp_path / "result.txt").write_text("tampered", encoding="utf-8")

    verified = _run(
        "verify",
        "--manifest",
        output / "manifest.json",
        "--project-root",
        tmp_path,
    )
    assert verified.returncode == 1
    assert json.loads(verified.stdout)["mismatches"] == ["hash:result.txt"]


def test_verify_detects_spec_tampering(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "open('result.txt', 'w').write('ok')"],
        ["result.txt"],
    )
    output = tmp_path / "run"
    assert (
        _run(
            "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
        ).returncode
        == 0
    )
    (output / "spec.json").write_text("{}", encoding="utf-8")

    verified = _run(
        "verify",
        "--manifest",
        output / "manifest.json",
        "--project-root",
        tmp_path,
    )
    assert verified.returncode == 1
    assert json.loads(verified.stdout)["mismatches"] == ["spec"]


def test_verify_detects_artifact_removed_from_manifest(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "open('result.txt', 'w').write('ok')"],
        ["result.txt"],
    )
    output = tmp_path / "run"
    assert (
        _run(
            "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
        ).returncode
        == 0
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = _run(
        "verify",
        "--manifest",
        manifest_path,
        "--project-root",
        tmp_path,
    )
    assert verified.returncode == 1
    assert json.loads(verified.stdout)["mismatches"] == ["manifest:artifact_set"]


def test_verify_detects_forged_success_outcome(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "open('result.txt', 'w').write('ok')"],
        ["result.txt"],
    )
    output = tmp_path / "run"
    assert (
        _run(
            "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
        ).returncode
        == 0
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["exit_code"] = 7
    manifest["status"] = "process_failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = _run(
        "verify",
        "--manifest",
        manifest_path,
        "--project-root",
        tmp_path,
    )
    assert verified.returncode == 1
    assert json.loads(verified.stdout)["mismatches"] == ["manifest:process_outcome"]


def test_failed_process_keeps_manifest_and_logs(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"],
        ["result.txt"],
    )
    output = tmp_path / "run"

    completed = _run(
        "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
    )
    assert completed.returncode == 1
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "process_failed"
    assert manifest["exit_code"] == 7
    assert manifest["missing_artifacts"] == ["result.txt"]
    assert (output / "stdout.bin").read_bytes().strip() == b"failed"


def test_unchanged_preexisting_artifact_fails_gate(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("stale", encoding="utf-8")
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "print('does not write the artifact')"],
        ["result.txt"],
    )
    output = tmp_path / "run"

    completed = _run(
        "run", "--spec", spec, "--project-root", tmp_path, "--output-dir", output
    )
    assert completed.returncode == 1
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "artifact_gate_failed"
    assert manifest["stale_artifacts"] == ["result.txt"]


def test_secret_option_is_rejected_before_execution(tmp_path: Path) -> None:
    sentinel = tmp_path / "should-not-exist"
    spec = _spec(
        tmp_path,
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(sentinel)!r}).touch()",
            "--api-key=<redacted>",
        ],
        ["result.txt"],
    )

    completed = _run(
        "run",
        "--spec",
        spec,
        "--project-root",
        tmp_path,
        "--output-dir",
        tmp_path / "run",
    )
    assert completed.returncode == 2
    assert "forbidden credential" in completed.stderr
    assert not sentinel.exists()


def test_secret_environment_name_is_rejected(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "print('unused')"],
        ["result.txt"],
    )
    payload = json.loads(spec.read_text(encoding="utf-8"))
    payload["env_allowlist"] = ["OPENAI_API_KEY"]
    spec.write_text(json.dumps(payload), encoding="utf-8")

    completed = _run(
        "run",
        "--spec",
        spec,
        "--project-root",
        tmp_path,
        "--output-dir",
        tmp_path / "run",
    )
    assert completed.returncode == 2
    assert "OPENAI_API_KEY" in completed.stderr


def test_artifact_path_traversal_is_rejected(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        [sys.executable, "-c", "print('unused')"],
        ["../outside.txt"],
    )

    completed = _run(
        "run",
        "--spec",
        spec,
        "--project-root",
        tmp_path,
        "--output-dir",
        tmp_path / "run",
    )
    assert completed.returncode == 2
    assert "must stay under" in completed.stderr


def test_output_directory_outside_project_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    spec = _spec(
        project,
        [sys.executable, "-c", "open('result.txt', 'w').write('unused')"],
        ["result.txt"],
    )

    completed = _run(
        "run",
        "--spec",
        spec,
        "--project-root",
        project,
        "--output-dir",
        tmp_path / "outside",
    )
    assert completed.returncode == 2
    assert "output directory must stay under project root" in completed.stderr
    assert not (tmp_path / "outside").exists()
