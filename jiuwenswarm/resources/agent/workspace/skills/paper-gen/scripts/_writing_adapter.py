"""Evidence-preserving boundary from paper-gen stages to the writing skill."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from _experiment_output import output_path, resolve_stage3_run_dir


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merge_runtime_runs(contract_runs: object, runtime_runs: object) -> list[dict[str, Any]]:
    """Overlay runtime measurements without discarding contract provenance.

    The module-three contract contains per-run dataset and configuration
    provenance, while runtime-results contains the executed measurements.  A
    replacement loses the former; matching records are therefore merged by
    run_record_id with runtime fields taking precedence.
    """
    canonical = [dict(item) for item in contract_runs if isinstance(item, dict)] if isinstance(contract_runs, list) else []
    if not isinstance(runtime_runs, list):
        return canonical
    runtime = [dict(item) for item in runtime_runs if isinstance(item, dict)]
    by_id = {
        str(item.get("run_record_id") or "").strip(): item
        for item in canonical
        if str(item.get("run_record_id") or "").strip()
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for runtime_run in runtime:
        run_id = str(runtime_run.get("run_record_id") or "").strip()
        contract_run = by_id.get(run_id)
        # Preserve runtime records byte-for-field: their presence in this file
        # is already the observation proof. Only contract rows that are absent
        # from runtime-results receive the explicit ``False`` marker below.
        merged.append({**contract_run, **runtime_run} if contract_run else runtime_run)
        if run_id:
            seen.add(run_id)
    # If the sources overlap, preserve a contract run absent from runtime
    # results so downstream gates can report the mismatch.  With no overlap,
    # the contract list can be an aggregate/summary ledger rather than the
    # runtime ledger, so keep the runtime list authoritative.
    if seen & set(by_id):
        for run_id, item in by_id.items():
            if run_id in seen:
                continue
            # A stage summary can contain the full planned matrix even when
            # runtime-results contains only the runs that really executed.
            # Keep the missing contract row for diagnosis, but never let its
            # planned SUCCESS marker or aggregate metrics masquerade as an
            # observed run in writing/readiness/plotting.
            retained = dict(item)
            retained["planned_status"] = retained.get("status")
            retained["status"] = "MISSING_RUNTIME_RECORD"
            retained["success"] = False
            retained["execution_observed"] = False
            retained["metrics"] = []
            merged.append(retained)
    return merged


def prepare_writing_inputs(
    stage1_dir: str | Path,
    stage2_dir: str | Path,
    stage3_dir: str | Path,
    stage4_dir: str | Path,
    base_dir: str | Path | None = None,
) -> dict[str, str]:
    """Project completed stages into the new writing input contract.

    The projection is intentionally lossless for Module 3 evidence.  In
    particular, it preserves each executed run and its metrics rather than
    replacing them with generated prose or aggregate-only summaries.
    """
    stage1, stage2, stage3, stage4 = map(Path, (stage1_dir, stage2_dir, stage3_dir, stage4_dir))
    inputs_dir = stage4 / "_input"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    stage3_run_dir = resolve_stage3_run_dir(stage3, base_dir)

    m1_source = stage1 / "conception_output.json"
    m1 = _read_object(m1_source, "stage1 conception output")
    if isinstance(m1.get("partial"), dict):
        m1 = dict(m1["partial"])

    method_source = stage2 / "method_design.json"
    plan_source = stage2 / "experiment_plan.json"
    m2 = {
        "method_design": _read_object(method_source, "stage2 method design"),
        "experiment_plan": _read_object(plan_source, "stage2 experiment plan"),
    }
    for key, filename in (("data_plan", "data_plan.json"), ("execution_config", "execution_config.json")):
        candidate = stage2 / filename
        if candidate.is_file():
            m2[key] = _read_object(candidate, f"stage2 {key}")

    m3_source = output_path(stage3_run_dir)
    raw_m3 = _read_object(m3_source, "stage3 experiment output")
    status = str(raw_m3.get("status") or "").upper()
    if status != "PASS":
        raise ValueError(
            f"writing requires stage3 status=PASS; received status={status or 'MISSING'}"
        )
    results = raw_m3.get("experiment_results")
    if not isinstance(results, dict):
        raise ValueError("stage3 experiment output has no experiment_results object")
    runtime_results = stage3_run_dir / "outputs" / "runtime-results.json"
    contract_runs = results.get("experiment_runs")
    runs = contract_runs
    if runtime_results.is_file():
        loaded_runs = json.loads(runtime_results.read_text(encoding="utf-8"))
        if isinstance(loaded_runs, list):
            runs = _merge_runtime_runs(contract_runs, loaded_runs)
    if not isinstance(runs, list) or not runs:
        raise ValueError("writing requires stage3 status=PASS with non-empty experiment_runs")
    if not all(isinstance(run, dict) and run.get("run_record_id") for run in runs):
        raise ValueError("each stage3 experiment run must have a run_record_id")

    m3 = dict(raw_m3)
    m3["status"] = "completed"
    m3["experiment_runs"] = runs
    m3["experiment_results"] = {**results, "experiment_runs": runs}
    m3["effective_execution_records"] = _effective_execution_records(runs, stage3_run_dir)
    m3["execution_evidence_policy"] = (
        "For executed settings, the effective run config and runtime record are authoritative. "
        "Module 2 values describe the original plan; any difference must be reported as a plan revision."
    )
    m3["artifact_root"] = str(stage3_run_dir)
    artifact_manifest = stage3_run_dir / "outputs" / "artifact-manifest.json"
    if artifact_manifest.is_file():
        m3["artifact_manifest_path"] = str(artifact_manifest.resolve())
    implementation_manifest = stage3_run_dir / "implementation-manifest.json"
    if implementation_manifest.is_file():
        m3["implementation_manifest"] = _read_object(
            implementation_manifest, "stage3 implementation manifest"
        )
        m3["implementation_manifest_path"] = str(implementation_manifest.resolve())
    stage3_status = stage3 / "status.json"
    if stage3_status.is_file():
        m3["stage3_orchestration_status"] = _read_object(stage3_status, "stage3 status")

    targets = {"m1": inputs_dir / "m1.json", "m2": inputs_dir / "m2.json", "m3": inputs_dir / "m3.json"}
    for name, payload in (("m1", m1), ("m2", m2), ("m3", m3)):
        targets[name].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    observed_runs = [run for run in runs if run.get("execution_observed") is not False and run.get("success") is True]
    missing_runtime_ids = [
        str(run.get("run_record_id")) for run in runs if run.get("execution_observed") is False
    ]
    manifest = {
        "schema_version": 1,
        "evidence_mode": "verified",
        "module3_status": "completed",
        "execution_record_count": len(observed_runs),
        "contract_record_count": len(runs),
        "missing_runtime_record_ids": missing_runtime_ids,
        "source_files": {
            "module1": {"path": str(m1_source.resolve()), "sha256": _sha256(m1_source)},
            "method_design": {"path": str(method_source.resolve()), "sha256": _sha256(method_source)},
            "experiment_plan": {"path": str(plan_source.resolve()), "sha256": _sha256(plan_source)},
            "module3": {"path": str(m3_source.resolve()), "sha256": _sha256(m3_source)},
        },
        "artifact_root": str(stage3_run_dir),
        "artifact_manifest": (
            {"path": str(artifact_manifest.resolve()), "sha256": _sha256(artifact_manifest)}
            if artifact_manifest.is_file() else None
        ),
        "implementation_manifest": (
            {"path": str(implementation_manifest.resolve()), "sha256": _sha256(implementation_manifest)}
            if implementation_manifest.is_file() else None
        ),
        "stage3_status": (
            {"path": str(stage3_status.resolve()), "sha256": _sha256(stage3_status)}
            if stage3_status.is_file() else None
        ),
    }
    manifest_path = inputs_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = inputs_dir / "writing-adapter-report.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "policy": "lossless evidence projection; no generated measurements or summaries",
                "module3_original_status": status,
                "module3_normalized_status": "completed",
                "execution_record_count": len(observed_runs),
                "contract_record_count": len(runs),
                "missing_runtime_record_ids": missing_runtime_ids,
                "inputs": {name: str(path) for name, path in targets.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {**{name: str(path) for name, path in targets.items()}, "source_manifest": str(manifest_path), "report": str(report_path)}


def _effective_execution_records(
    runs: list[dict[str, Any]],
    stage3_run_dir: Path,
) -> list[dict[str, Any]]:
    """Snapshot configs actually executed while keeping paths inside the run."""
    root = stage3_run_dir.resolve()
    records: list[dict[str, Any]] = []
    for run in runs:
        config_value = run.get("config_path")
        config_payload: dict[str, Any] | None = None
        config_sha256: str | None = None
        if isinstance(config_value, str) and config_value.strip():
            relative = Path(config_value)
            if not relative.is_absolute() and ".." not in relative.parts:
                candidate = (root / relative).resolve()
                if root in candidate.parents and candidate.is_file():
                    try:
                        loaded = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        loaded = None
                    if isinstance(loaded, dict):
                        config_payload = loaded
                        config_sha256 = _sha256(candidate)
        records.append(
            {
                "run_record_id": run.get("run_record_id") or run.get("run_id"),
                "experiment_id": run.get("experiment_id"),
                "dataset": run.get("dataset"),
                "method": run.get("method"),
                "seed": run.get("seed"),
                "execution_observed": run.get("execution_observed") is not False,
                "config_path": config_value,
                "config_sha256": config_sha256,
                "effective_config": config_payload,
            }
        )
    return records
