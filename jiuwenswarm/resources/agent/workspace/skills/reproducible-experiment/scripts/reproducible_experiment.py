#!/usr/bin/env python3
"""Run and verify small local experiments with SHA-256 provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
LOGGER = logging.getLogger(__name__)
_SPEC_KEYS = {
    "schema_version",
    "experiment_id",
    "command",
    "cwd",
    "seed",
    "timeout_seconds",
    "expected_artifacts",
    "env_allowlist",
}
_SENSITIVE_NAMES = {
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}


class ExperimentError(ValueError):
    """A safe, user-actionable invocation or verification error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_result(payload: dict[str, Any]) -> None:
    """Serialize the CLI response on stdout; diagnostics use stderr logging."""
    json.dump(payload, sys.stdout, ensure_ascii=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_sensitive_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return normalized in _SENSITIVE_NAMES or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_NAMES
    )


def _command_option(arg: str) -> str | None:
    if arg.startswith("--"):
        return arg[2:].split("=", 1)[0]
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=.*", arg)
    return match.group(1) if match else None


def _validate_no_secrets(command: list[str], env_allowlist: list[str]) -> None:
    for arg in command:
        option = _command_option(arg)
        if option and _is_sensitive_name(option):
            raise ExperimentError(
                f"command contains forbidden credential option/environment: {option}"
            )
    for name in env_allowlist:
        if _is_sensitive_name(name):
            raise ExperimentError(
                f"env_allowlist contains forbidden credential name: {name}"
            )


def _relative_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ExperimentError(f"{field} must stay under its declared root: {value}")
    return path


def _resolve_under(base: Path, value: Any, *, field: str) -> Path:
    relative = _relative_path(value, field=field)
    resolved_base = base.resolve()
    resolved = (resolved_base / relative).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ExperimentError(f"{field} escapes its declared root: {value}") from exc
    return resolved


def _resolve_output_under(project: Path, output_dir: Path) -> Path:
    resolved = (
        output_dir.resolve()
        if output_dir.is_absolute()
        else (project / output_dir).resolve()
    )
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise ExperimentError(
            f"output directory must stay under project root: {output_dir}"
        ) from exc
    return resolved


def _load_spec(path: Path) -> tuple[dict[str, Any], str, bytes]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot read specification {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExperimentError("specification root must be a JSON object")
    unknown = sorted(set(data) - _SPEC_KEYS)
    if unknown:
        raise ExperimentError(f"unknown specification fields: {unknown}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ExperimentError(f"schema_version must be {SCHEMA_VERSION}")
    experiment_id = data.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ExperimentError("experiment_id must be a non-empty string")
    command = data.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ExperimentError("command must be a non-empty array of strings")
    expected = data.get("expected_artifacts")
    if not isinstance(expected, list) or not expected:
        raise ExperimentError("expected_artifacts must be a non-empty array")
    for index, item in enumerate(expected):
        _relative_path(item, field=f"expected_artifacts[{index}]")
    cwd = data.get("cwd", ".")
    _relative_path(cwd, field="cwd")
    timeout = data.get("timeout_seconds", 3600)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise ExperimentError("timeout_seconds must be in (0, 86400]")
    seed = data.get("seed")
    if seed is not None and not isinstance(seed, int):
        raise ExperimentError("seed must be an integer or null")
    allowlist = data.get("env_allowlist", [])
    if not isinstance(allowlist, list) or not all(
        isinstance(item, str) and item for item in allowlist
    ):
        raise ExperimentError("env_allowlist must be an array of names")
    _validate_no_secrets(command, allowlist)
    data.setdefault("cwd", ".")
    data.setdefault("timeout_seconds", 3600)
    data.setdefault("seed", None)
    data.setdefault("env_allowlist", [])
    return data, hashlib.sha256(raw).hexdigest(), raw


def _git_state(project_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain") if commit else None
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def _artifact_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "sha256": _sha256(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_experiment(spec_path: Path, project_root: Path, output_dir: Path) -> int:
    spec, spec_hash, spec_raw = _load_spec(spec_path)
    project = project_root.resolve()
    if not project.is_dir():
        raise ExperimentError(f"project root is not a directory: {project}")
    cwd = _resolve_under(project, spec["cwd"], field="cwd")
    if not cwd.is_dir():
        raise ExperimentError(f"experiment cwd is not a directory: {cwd}")
    artifact_paths = [
        _resolve_under(cwd, item, field=f"expected_artifacts[{index}]")
        for index, item in enumerate(spec["expected_artifacts"])
    ]
    output = _resolve_output_under(project, output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    spec_snapshot_path = output / "spec.json"
    stdout_path = output / "stdout.bin"
    stderr_path = output / "stderr.bin"
    occupied = [
        path.name
        for path in (manifest_path, spec_snapshot_path, stdout_path, stderr_path)
        if path.exists()
    ]
    if occupied:
        raise ExperimentError(f"output directory contains protected files: {occupied}")
    spec_snapshot_path.write_bytes(spec_raw)
    before = {str(path): _artifact_state(path) for path in artifact_paths}
    git_state = _git_state(project)
    started_at = _utc_now()
    start = time.perf_counter()
    status = "completed"
    exit_code = -1
    stdout = b""
    stderr = b""
    try:
        result = subprocess.run(
            spec["command"],
            cwd=cwd,
            capture_output=True,
            timeout=float(spec["timeout_seconds"]),
            check=False,
            shell=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        if exit_code != 0:
            status = "process_failed"
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        stderr += f"\nTIMEOUT after {spec['timeout_seconds']} seconds\n".encode()
    except OSError as exc:
        status = "execution_error"
        stderr = f"EXECUTION_ERROR: {exc}\n".encode("utf-8", "replace")
    duration = round(time.perf_counter() - start, 6)
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

    artifacts: list[dict[str, Any]] = []
    missing: list[str] = []
    stale: list[str] = []
    for declared, path in zip(spec["expected_artifacts"], artifact_paths, strict=True):
        after = _artifact_state(path)
        if not after["exists"]:
            missing.append(declared)
            continue
        previous = before[str(path)]
        if (
            previous.get("exists")
            and previous.get("sha256") == after.get("sha256")
            and previous.get("mtime_ns") == after.get("mtime_ns")
        ):
            stale.append(declared)
        artifacts.append(
            {
                "path": declared.replace("\\", "/"),
                "sha256": after["sha256"],
                "size_bytes": after["size_bytes"],
            }
        )

    passed = exit_code == 0 and not missing and not stale
    if exit_code == 0 and (missing or stale):
        status = "artifact_gate_failed"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": spec["experiment_id"],
        "spec": {"path": "spec.json", "sha256": spec_hash},
        "command": spec["command"],
        "cwd": spec["cwd"].replace("\\", "/"),
        "seed": spec["seed"],
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_seconds": duration,
        "status": status,
        "exit_code": exit_code,
        "passed": passed,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git": git_state,
            "environment": {
                name: os.environ[name]
                for name in spec["env_allowlist"]
                if name in os.environ
            },
        },
        "logs": {
            "stdout": {"path": "stdout.bin", "sha256": _sha256(stdout_path)},
            "stderr": {"path": "stderr.bin", "sha256": _sha256(stderr_path)},
        },
        "artifacts": artifacts,
        "missing_artifacts": missing,
        "stale_artifacts": stale,
    }
    _atomic_json(manifest_path, manifest)
    _write_result(
        {
            "manifest": str(manifest_path),
            "status": status,
            "passed": passed,
            "artifacts": len(artifacts),
            "missing": missing,
            "stale": stale,
        }
    )
    return 0 if passed else 1


def verify_manifest(manifest_path: Path, project_root: Path) -> int:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ExperimentError(f"manifest schema_version must be {SCHEMA_VERSION}")
    project = project_root.resolve()
    cwd = _resolve_under(project, manifest.get("cwd"), field="manifest.cwd")
    mismatches: list[str] = []
    spec_record = manifest.get("spec", {})
    spec_path = _resolve_under(
        manifest_path.parent,
        spec_record.get("path"),
        field="spec.path",
    )
    spec_matches = spec_path.is_file() and _sha256(spec_path) == spec_record.get(
        "sha256"
    )
    if not spec_matches:
        mismatches.append("spec")
    frozen_spec: dict[str, Any] | None = None
    if spec_matches:
        try:
            frozen_spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentError(
                f"cannot read frozen specification {spec_path}: {exc}"
            ) from exc
        for field, default in (
            ("experiment_id", None),
            ("command", None),
            ("cwd", "."),
            ("seed", None),
        ):
            if manifest.get(field) != frozen_spec.get(field, default):
                mismatches.append(f"manifest:{field}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ExperimentError("manifest artifacts must be an array")
    recorded_paths = [
        artifact.get("path") for artifact in artifacts if isinstance(artifact, dict)
    ]
    if frozen_spec is not None:
        expected_paths = frozen_spec.get("expected_artifacts")
        if not isinstance(expected_paths, list):
            raise ExperimentError("frozen spec expected_artifacts must be an array")
        if manifest.get("passed") and sorted(recorded_paths) != sorted(expected_paths):
            mismatches.append("manifest:artifact_set")
    if manifest.get("passed") and (
        manifest.get("missing_artifacts") or manifest.get("stale_artifacts")
    ):
        mismatches.append("manifest:passed_gate")
    if manifest.get("passed") and (
        manifest.get("exit_code") != 0 or manifest.get("status") != "completed"
    ):
        mismatches.append("manifest:process_outcome")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ExperimentError("each manifest artifact must be an object")
        path = _resolve_under(cwd, artifact.get("path"), field="artifact.path")
        if not path.is_file():
            mismatches.append(f"missing:{artifact.get('path')}")
            continue
        if path.stat().st_size != artifact.get("size_bytes") or _sha256(
            path
        ) != artifact.get("sha256"):
            mismatches.append(f"hash:{artifact.get('path')}")
    for stream in ("stdout", "stderr"):
        record = manifest.get("logs", {}).get(stream, {})
        log_path = _resolve_under(
            manifest_path.parent, record.get("path"), field=f"logs.{stream}.path"
        )
        if not log_path.is_file() or _sha256(log_path) != record.get("sha256"):
            mismatches.append(f"log:{stream}")
    verified = bool(manifest.get("passed")) and not mismatches
    _write_result(
        {
            "manifest": str(manifest_path.resolve()),
            "recorded_passed": bool(manifest.get("passed")),
            "verified": verified,
            "mismatches": mismatches,
        }
    )
    return 0 if verified else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    run_parser = subparsers.add_parser("run", help="execute one declared experiment")
    run_parser.add_argument("--spec", required=True, type=Path)
    run_parser.add_argument("--project-root", required=True, type=Path)
    run_parser.add_argument("--output-dir", required=True, type=Path)
    verify_parser = subparsers.add_parser("verify", help="re-hash a recorded run")
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--project-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command_name == "run":
            return run_experiment(args.spec, args.project_root, args.output_dir)
        return verify_manifest(args.manifest, args.project_root)
    except ExperimentError as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
