"""Persist validated module output, environment metadata and a human summary."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path

from contracts import ExperimentModuleInput, ExperimentModuleOutput
from io_utils import (
    portable_arguments,
    relative_to_run,
    write_json_atomic,
    write_text_atomic,
)
from runtime_models import ImplementationManifest, RuntimeRunResult


def snapshot_inputs(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> tuple[Path, Path]:
    request_path = run_dir / "inputs" / "experiment-request.json"
    manifest_path = run_dir / "inputs" / "implementation-manifest.json"
    write_json_atomic(request_path, request.model_dump(mode="json"))
    manifest_payload = portable_manifest_payload(manifest, run_dir)
    write_json_atomic(manifest_path, manifest_payload)
    return request_path, manifest_path


def portable_manifest_payload(
    manifest: ImplementationManifest,
    run_dir: Path,
) -> dict[str, object]:
    manifest_payload = manifest.model_dump(mode="json")
    for implementation in manifest_payload["implementations"].values():
        implementation["command"] = portable_arguments(
            implementation["command"], run_dir=run_dir
        )
        implementation["smoke_test_command"] = portable_arguments(
            implementation["smoke_test_command"], run_dir=run_dir
        )
        implementation["env"] = {
            name: portable_arguments([value], run_dir=run_dir)[0]
            for name, value in implementation["env"].items()
        }
    for preparation in manifest_payload["dataset_preparations"].values():
        preparation["command"] = portable_arguments(
            preparation["command"], run_dir=run_dir
        )
        preparation["env"] = {
            name: portable_arguments([value], run_dir=run_dir)[0]
            for name, value in preparation["env"].items()
        }
    for extension in manifest_payload["analysis_extensions"].values():
        extension["command"] = portable_arguments(
            extension["command"], run_dir=run_dir
        )
        extension["env"] = {
            name: portable_arguments([value], run_dir=run_dir)[0]
            for name, value in extension["env"].items()
        }
    for metric in manifest_payload["metrics"].values():
        metric["implementation"] = portable_arguments(
            [metric["implementation"]], run_dir=run_dir
        )[0]
    return manifest_payload


def write_environment(
    run_dir: Path,
    manifest: ImplementationManifest,
) -> tuple[Path, list[str]]:
    packages = sorted(
        {
            (distribution.metadata.get("Name") or distribution.name): distribution.version
            for distribution in importlib.metadata.distributions()
        }.items(),
        key=lambda item: item[0].lower(),
    )
    path = run_dir / "environment" / "environment.json"
    implementation_runtime: list[dict[str, object]] = []
    warnings: list[str] = []
    python_names = {
        Path(sys.executable).name.lower(),
        "python",
        "python3",
        "python.exe",
    }
    for name, definition in sorted(manifest.implementations.items()):
        executable = definition.command[0]
        uses_coordinator_python = Path(executable).name.lower() in python_names
        implementation_runtime.append(
            {
                "name": name,
                "declared_executable": portable_arguments(
                    [executable], run_dir=run_dir
                )[0],
                "uses_coordinator_python": uses_coordinator_python,
                "revision": definition.revision,
                "implementation_url": (
                    str(definition.implementation_url)
                    if definition.implementation_url is not None
                    else None
                ),
                "uses_gpu": definition.uses_gpu,
            }
        )
        if not uses_coordinator_python:
            warnings.append(
                f"实现{name}使用外部运行时{Path(executable).name}；"
                "environment.json无法自动枚举其完整依赖"
            )
        if definition.revision is None:
            warnings.append(f"实现{name}未声明源码或镜像revision")
    write_json_atomic(
        path,
        {
            "scope": "coordinator_and_declared_implementation_runtime",
            "python": sys.version,
            "executable_name": Path(sys.executable).name,
            "platform": platform.platform(),
            "packages": [
                {"name": name, "version": version} for name, version in packages
            ],
            "implementations": implementation_runtime,
            "limitations": [
                "packages记录协调器Python环境；外部解释器、容器或远程环境必须由实现revision和notes补充"
            ],
        },
    )
    return path, warnings


def write_runtime_results(
    results: list[RuntimeRunResult],
    run_dir: Path,
) -> Path:
    path = run_dir / "outputs" / "runtime-results.json"
    write_json_atomic(path, [item.model_dump(mode="json") for item in results])
    return path


def write_module_output(
    output: ExperimentModuleOutput,
    run_dir: Path,
) -> Path:
    path = run_dir / "outputs" / "experiment-module-output.json"
    write_json_atomic(path, output.model_dump(mode="json"))
    write_text_atomic(run_dir / "outputs" / "summary.md", _summary(output))
    return path


def reproducibility_command() -> str:
    return (
        "python {experiment_skill_dir}/scripts/main.py run "
        "--input {run_dir}/inputs/experiment-request.json "
        "--manifest {run_dir}/inputs/implementation-manifest.json"
    )


def relative_environment_path(path: Path, run_dir: Path) -> str:
    return relative_to_run(path, run_dir)


def _summary(output: ExperimentModuleOutput) -> str:
    lines = [
        "# Experiment Module Summary",
        "",
        f"- Run ID: `{output.run_id}`",
        f"- Status: `{output.status}`",
        f"- Warnings: {len(output.warnings)}",
        f"- Errors: {len(output.errors)}",
    ]
    if output.experiment_results is not None:
        results = output.experiment_results
        lines.extend(
            [
                f"- Successful/recorded runs: {len(results.experiment_runs)}",
                f"- Metric records: {len(results.metric_records)}",
                f"- Visualization candidates: {len(results.visualization_candidates)}",
                "",
                "## Key findings",
                "",
            ]
        )
        lines.extend(f"- {finding}" for finding in results.key_findings)
    if output.planning_feedback is not None:
        lines.extend(
            [
                "",
                "## Planning feedback",
                "",
                output.planning_feedback.reason,
                "",
            ]
        )
        lines.extend(
            f"- {blocker}" for blocker in output.planning_feedback.blockers
        )
    if output.errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in output.errors)
    return "\n".join(lines) + "\n"
