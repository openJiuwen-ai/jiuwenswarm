"""Execute normalized experiment tasks and collect only runner-emitted metrics."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from execution_monitor import ExecutionMonitor
from io_utils import (
    build_subprocess_environment,
    expand_placeholders,
    portable_command,
    relative_to_run,
    safe_join,
    write_json_atomic,
    write_text_atomic,
)
from runtime_models import (
    ExecutionSpec,
    ImplementationDefinition,
    ImplementationManifest,
    MetricDefinition,
    ParsedMetric,
    RuntimeRunResult,
)


def execute_all(
    specs: list[ExecutionSpec],
    manifest: ImplementationManifest,
    run_dir: Path,
    *,
    max_retries: int,
    timeout_seconds: int | None,
    max_parallel_runs: int = 1,
    max_parallel_gpu_runs: int = 1,
    monitor_interval_seconds: float = 1.0,
    live_validator: Callable[[], None] | None = None,
    python_executable: str | None = None,
) -> list[RuntimeRunResult]:
    if not specs:
        return []
    # Wall-clock/latency/throughput measurements are invalid when neighbouring
    # runs compete for CPU, disk or GPU. Preserve parallelism for ordinary
    # experiments, but serialize the whole batch whenever the declared metric
    # contract contains a resource-sensitive measurement.
    timing_sensitive = any(
        _is_resource_sensitive_metric(metric)
        for spec in specs
        for metric in spec.metric_names
    )
    workers = 1 if timing_sensitive else min(max(1, max_parallel_runs), len(specs))
    monitor = ExecutionMonitor(
        run_dir,
        [item.run_record_id for item in specs],
        max_workers=workers,
    )
    gpu_slots = threading.BoundedSemaphore(
        1 if timing_sensitive else max(1, max_parallel_gpu_runs)
    )
    validation_lock = threading.Lock()

    def validate_live_state() -> None:
        if live_validator is None:
            return
        # Dataset hashing is intentionally serialized while the experiment
        # processes themselves remain parallel.
        with validation_lock:
            live_validator()

    def run_spec(spec: ExecutionSpec) -> RuntimeRunResult:
        definition = manifest.implementations[spec.method]
        if definition.uses_gpu:
            with gpu_slots:
                return execute_one(
                    spec,
                    definition,
                    run_dir,
                    max_retries=max_retries,
                    timeout_seconds=timeout_seconds,
                    metric_definitions=manifest.metrics,
                    monitor=monitor,
                    monitor_interval_seconds=monitor_interval_seconds,
                    live_validator=validate_live_state,
                    python_executable=python_executable,
                )
        return execute_one(
            spec,
            definition,
            run_dir,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            metric_definitions=manifest.metrics,
            monitor=monitor,
            monitor_interval_seconds=monitor_interval_seconds,
            live_validator=validate_live_state,
            python_executable=python_executable,
        )

    ordered: list[RuntimeRunResult | None] = [None] * len(specs)
    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="experiment-run",
        ) as executor:
            futures = {
                executor.submit(run_spec, spec): index
                for index, spec in enumerate(specs)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
    finally:
        monitor.finish()
    return [item for item in ordered if item is not None]


def _is_resource_sensitive_metric(name: str) -> bool:
    normalized = str(name or "").casefold().replace("-", "_")
    tokens = set(normalized.split("_"))
    return bool(
        tokens.intersection({
            "time", "latency", "runtime", "throughput", "memory", "ram",
            "vram", "energy", "power",
        })
        or normalized.endswith("_seconds")
        or normalized.endswith("_per_second")
    )


def execute_one(
    spec: ExecutionSpec,
    implementation: ImplementationDefinition,
    run_dir: Path,
    *,
    max_retries: int,
    timeout_seconds: int | None,
    metric_definitions: dict[str, MetricDefinition],
    monitor: ExecutionMonitor | None = None,
    monitor_interval_seconds: float = 1.0,
    live_validator: Callable[[], None] | None = None,
    python_executable: str | None = None,
) -> RuntimeRunResult:
    raw_output_dir = safe_join(run_dir, f"raw_results/{spec.run_record_id}")
    config_path = safe_join(run_dir, f"configs/{spec.run_record_id}.json")
    log_path = safe_join(run_dir, f"logs/{spec.run_record_id}.log")
    raw_output_dir.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    dataset_path = Path(spec.dataset_path).resolve()
    config_payload = {
        "experiment_id": spec.experiment_id,
        "experiment_type": spec.experiment_type,
        "hypothesis_ids": spec.hypothesis_ids,
        "dataset": spec.dataset,
        "dataset_path": relative_to_run(dataset_path, run_dir),
        "method": spec.method,
        "metric_names": spec.metric_names,
        "seed": spec.seed,
        "run_record_id": spec.run_record_id,
        "parameters": spec.parameters,
        "split_strategy": spec.split_strategy,
        "preprocessing_pipeline": spec.preprocessing_pipeline,
        "raw_output_dir": relative_to_run(raw_output_dir, run_dir),
        "is_smoke_test": spec.run_record_id.startswith("smoke-"),
    }
    write_json_atomic(config_path, config_payload)

    variables = {
        "run_dir": str(run_dir),
        "experiment_id": spec.experiment_id,
        "run_record_id": spec.run_record_id,
        "dataset": spec.dataset,
        "dataset_path": str(dataset_path),
        "method": spec.method,
        "seed": spec.seed,
        "config_path": str(config_path),
        "raw_output_dir": str(raw_output_dir),
    }
    try:
        metrics_path = _resolve_artifact_path(
            expand_placeholders(implementation.metrics_path, variables),
            run_dir,
        )
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        variables["metrics_path"] = str(metrics_path)
        command = [
            expand_placeholders(argument, variables)
            for argument in implementation.command
        ]
        command = _use_deployed_python(command, python_executable)
        cwd = safe_join(run_dir, implementation.cwd)
    except Exception as exc:
        error = f"invalid implementation command or path: {exc}"
        write_text_atomic(log_path, error + "\n")
        _monitor_event(monitor, spec, "RUN_FAILED", {"error": error})
        return _failure_result(
            spec,
            implementation,
            list(implementation.command),
            config_path,
            log_path,
            run_dir,
            attempts=1,
            duration=0,
            error=error,
            dataset_path=dataset_path,
        )
    if not cwd.is_dir():
        _monitor_event(
            monitor,
            spec,
            "RUN_FAILED",
            {"error": f"implementation cwd does not exist: {implementation.cwd}"},
        )
        return _failure_result(
            spec,
            implementation,
            command,
            config_path,
            log_path,
            run_dir,
            attempts=1,
            duration=0,
            error=f"implementation cwd does not exist: {implementation.cwd}",
        )

    environment = build_subprocess_environment(
        implementation.env,
        implementation.required_env,
    )
    # Some managed Python runtimes enable safe-path mode and omit the script's
    # directory from sys.path. Make sibling modules importable without asking
    # generated implementations to modify global installation state.
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(cwd) + (
        (os.pathsep + existing_pythonpath) if existing_pythonpath else ""
    )
    attempts = 0
    started = time.perf_counter()
    write_text_atomic(log_path, "")
    config_digest = _file_digest(config_path)
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        attempts += 1
        _append_log(log_path, f"=== attempt {attempts}/{max_retries + 1} ===\n")
        try:
            # A retry is a fresh scientific attempt. Never let checkpoints or
            # predictions from the failed attempt satisfy the next attempt's
            # output checks.
            if raw_output_dir.exists():
                shutil.rmtree(raw_output_dir)
            raw_output_dir.mkdir(parents=True, exist_ok=True)
            if metrics_path.is_file():
                metrics_path.unlink()
            elif metrics_path.exists():
                raise ValueError(f"metrics path is not a file: {metrics_path}")
            if log_path.is_symlink():
                raise ValueError("run log cannot be a symbolic link")
            with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                )
                _monitor_event(
                    monitor,
                    spec,
                    "RUN_STARTED",
                    {"attempt": attempts, "pid": process.pid},
                )
                attempt_started = time.monotonic()
                last_heartbeat = 0.0
                validation_error: str | None = None
                timed_out = False
                while process.poll() is None:
                    now = time.monotonic()
                    if timeout_seconds is not None and now - attempt_started > timeout_seconds:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break
                    if now - last_heartbeat >= monitor_interval_seconds:
                        try:
                            _validate_live_run(
                                config_path,
                                config_digest,
                                log_path,
                                live_validator,
                            )
                        except Exception as exc:
                            validation_error = str(exc)
                            process.kill()
                            process.wait()
                            break
                        _monitor_event(
                            monitor,
                            spec,
                            "HEARTBEAT",
                            {
                                "attempt": attempts,
                                "pid": process.pid,
                                "elapsed_seconds": round(now - attempt_started, 3),
                                "log_size_bytes": log_path.stat().st_size,
                                "validation": "PASS",
                            },
                        )
                        last_heartbeat = now
                    time.sleep(min(0.1, monitor_interval_seconds / 2))
                returncode = process.returncode
            if validation_error is not None:
                last_error = f"live validation failed: {validation_error}"
                _append_log(log_path, f"\n--- live validation failed ---\n{last_error}\n")
                _monitor_event(
                    monitor,
                    spec,
                    "VALIDATION_FAILED",
                    {"error": last_error, "attempt": attempts},
                )
                break
            if timed_out:
                last_error = f"command timed out after {timeout_seconds} seconds"
                _append_log(log_path, f"\n--- timeout ---\n{last_error}\n")
                _monitor_event(
                    monitor,
                    spec,
                    "RUN_TIMEOUT",
                    {"error": last_error, "attempt": attempts},
                )
                if attempt < max_retries:
                    _monitor_event(
                        monitor,
                        spec,
                        "ATTEMPT_RETRY",
                        {"next_attempt": attempts + 1, "reason": last_error},
                    )
                    continue
                break
            _append_log(
                log_path,
                f"\n--- returncode: {returncode} ---\n",
            )
            if returncode != 0:
                last_error = f"command exited with code {returncode}"
                if attempt < max_retries:
                    _monitor_event(
                        monitor,
                        spec,
                        "ATTEMPT_RETRY",
                        {"next_attempt": attempts + 1, "reason": last_error},
                    )
                continue
            _validate_live_run(
                config_path,
                config_digest,
                log_path,
                live_validator,
            )
            parsed = [
                item
                for item in parse_metrics(metrics_path)
                if item.name in spec.metric_names
            ]
            _validate_metrics(parsed, spec.metric_names)
            _validate_metric_units(parsed, metric_definitions)
            duration = time.perf_counter() - started
            result = RuntimeRunResult(
                run_record_id=spec.run_record_id,
                experiment_id=spec.experiment_id,
                method=spec.method,
                success=True,
                attempts=attempts,
                duration_seconds=duration,
                uses_gpu=implementation.uses_gpu,
                command=_display_command(command, run_dir, dataset_path),
                config_path=relative_to_run(config_path, run_dir),
                log_path=relative_to_run(log_path, run_dir),
                metrics=parsed,
                error=None,
            )
            _monitor_event(
                monitor,
                spec,
                "RUN_SUCCEEDED",
                {
                    "attempt": attempts,
                    "duration_seconds": round(duration, 6),
                    "metric_count": len(parsed),
                },
            )
            return result
        except Exception as exc:
            last_error = f"execution or metric collection failed: {exc}"
            _append_log(log_path, f"--- exception ---\n{last_error}\n")
            if attempt < max_retries:
                _monitor_event(
                    monitor,
                    spec,
                    "ATTEMPT_RETRY",
                    {"next_attempt": attempts + 1, "reason": last_error},
                )

    duration = time.perf_counter() - started
    result = _failure_result(
        spec,
        implementation,
        command,
        config_path,
        log_path,
        run_dir,
        attempts=attempts,
        duration=duration,
        error=last_error or "experiment failed without a reported error",
        dataset_path=dataset_path,
    )
    # Failed raw outputs are not evidence and may be very large. Keep only the
    # compact failure record/log needed for diagnosis; the enclosing failed
    # stage is removed by paper-gen's cleanup policy.
    if raw_output_dir.exists():
        shutil.rmtree(raw_output_dir)
    if monitor is not None:
        terminal_event = (
            "VALIDATION_FAILED"
            if last_error and "live validation failed" in last_error
            else "RUN_FAILED"
        )
        _monitor_event(
            monitor,
            spec,
            terminal_event,
            {"error": result.error, "attempt": attempts},
        )
    return result


def parse_metrics(path: Path) -> list[ParsedMetric]:
    if not path.is_file():
        raise FileNotFoundError(f"metrics file was not produced: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _parse_csv_metrics(path)
    if suffix != ".json":
        raise ValueError("metrics_path must point to a .json or .csv file")
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return _parse_json_metrics(payload)


def _parse_json_metrics(payload: Any) -> list[ParsedMetric]:
    content = payload.get("metrics", payload) if isinstance(payload, dict) else payload
    records: list[ParsedMetric] = []
    if isinstance(content, dict):
        for name, value in content.items():
            if isinstance(value, dict):
                records.append(
                    ParsedMetric(
                        name=name,
                        value=value["value"],
                        unit=value.get("unit"),
                        split=value.get("split", "test"),
                    )
                )
            else:
                records.append(ParsedMetric(name=name, value=value))
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                raise ValueError("each metrics list item must be an object")
            records.append(
                ParsedMetric(
                    name=item.get("name") or item.get("metric"),
                    value=item["value"],
                    unit=item.get("unit"),
                    split=item.get("split", "test"),
                )
            )
    else:
        raise ValueError("metrics JSON must contain an object or list")
    _validate_finite(records)
    return records


def _parse_csv_metrics(path: Path) -> list[ParsedMetric]:
    records: list[ParsedMetric] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            name = row.get("name") or row.get("metric")
            if not name or row.get("value") in {None, ""}:
                raise ValueError("metrics CSV requires name/metric and value columns")
            records.append(
                ParsedMetric(
                    name=name,
                    value=float(row["value"]),
                    unit=row.get("unit") or None,
                    split=row.get("split") or "test",
                )
            )
    _validate_finite(records)
    return records


def _validate_finite(records: list[ParsedMetric]) -> None:
    if not records:
        raise ValueError("metrics file contains no records")
    invalid = [item.name for item in records if not math.isfinite(item.value)]
    if invalid:
        raise ValueError(f"metrics contain non-finite values: {sorted(set(invalid))}")


def _validate_metrics(records: list[ParsedMetric], expected: list[str]) -> None:
    test_records = [item for item in records if item.split == "test"]
    actual = {item.name for item in test_records}
    missing = set(expected) - actual
    if missing:
        raise ValueError(
            "runner did not emit planned test metrics: " f"{sorted(missing)}"
        )
    duplicates = sorted(
        name for name in actual if sum(item.name == name for item in test_records) > 1
    )
    if duplicates:
        raise ValueError(
            "runner emitted duplicate planned test metrics: " f"{duplicates}"
        )


def _validate_metric_units(
    records: list[ParsedMetric],
    definitions: dict[str, MetricDefinition],
) -> None:
    mismatches = [
        f"{item.name}: emitted={item.unit}, planned={definitions[item.name].unit}"
        for item in records
        if definitions[item.name].unit is not None
        and item.unit != definitions[item.name].unit
    ]
    if mismatches:
        raise ValueError("metric units do not match manifest: " + "; ".join(mismatches))


def _resolve_artifact_path(value: str, run_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
        root = run_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"metrics path escapes run directory: {value}")
        return resolved
    return safe_join(run_dir, value)


def _display_command(command: list[str], run_dir: Path, _dataset_path: Path) -> str:
    return portable_command(command, run_dir=run_dir)


def _failure_result(
    spec: ExecutionSpec,
    implementation: ImplementationDefinition,
    command: list[str],
    config_path: Path,
    log_path: Path,
    run_dir: Path,
    *,
    attempts: int,
    duration: float,
    error: str,
    dataset_path: Path | None = None,
) -> RuntimeRunResult:
    return RuntimeRunResult(
        run_record_id=spec.run_record_id,
        experiment_id=spec.experiment_id,
        method=spec.method,
        success=False,
        attempts=attempts,
        duration_seconds=duration,
        uses_gpu=implementation.uses_gpu,
        command=_display_command(command, run_dir, dataset_path or run_dir),
        config_path=relative_to_run(config_path, run_dir),
        log_path=relative_to_run(log_path, run_dir),
        metrics=[],
        error=error,
    )


def _append_log(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(content)


def _use_deployed_python(
    command: list[str],
    python_executable: str | None,
) -> list[str]:
    if not command or not python_executable:
        return command
    executable_name = Path(command[0]).name.casefold()
    current_name = Path(sys.executable).name.casefold()
    if executable_name in {"python", "python.exe", "python3", current_name}:
        return [python_executable, *command[1:]]
    return command


def _validate_live_run(
    config_path: Path,
    expected_config_digest: str,
    log_path: Path,
    validator: Callable[[], None] | None,
) -> None:
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("run config disappeared or became a symbolic link")
    if _file_digest(config_path) != expected_config_digest:
        raise ValueError("run config changed after process launch")
    if log_path.is_symlink() or not log_path.is_file():
        raise ValueError("run log disappeared or became a symbolic link")
    if validator is not None:
        validator()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _monitor_event(
    monitor: ExecutionMonitor | None,
    spec: ExecutionSpec,
    event: str,
    details: dict[str, Any],
) -> None:
    if monitor is not None:
        monitor.event(spec.run_record_id, event, details)
