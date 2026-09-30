"""Run optional domain-specific analysis and visualization extension commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import Field, model_validator

from contracts import (
    AnalysisRecord,
    ExperimentModuleInput,
    FigureArtifact,
    FindingRecord,
    MetricRecord,
    TableArtifact,
    VisualizationCandidate,
    VisualizationDataAsset,
)
from io_utils import (
    build_subprocess_environment,
    expand_placeholders,
    portable_command,
    read_json,
    relative_to_run,
    safe_join,
    slugify,
    write_json_atomic,
    write_text_atomic,
)
from runtime_models import (
    AggregateRecord,
    AnalysisExtensionDefinition,
    ImplementationManifest,
    RuntimeModel,
)


class AnalysisExtensionOutput(RuntimeModel):
    analysis_records: list[AnalysisRecord] = Field(default_factory=list)
    visualization_data: list[VisualizationDataAsset] = Field(default_factory=list)
    visualization_candidates: list[VisualizationCandidate] = Field(default_factory=list)
    tables: dict[str, str] = Field(default_factory=dict)
    figures: dict[str, str] = Field(default_factory=dict)
    table_artifacts: list[TableArtifact] = Field(default_factory=list)
    figure_artifacts: list[FigureArtifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def compatibility_artifacts_match(self) -> "AnalysisExtensionOutput":
        if set(self.tables) != {item.name for item in self.table_artifacts}:
            raise ValueError("extension tables keys must match table_artifacts names")
        if set(self.figures) != {item.name for item in self.figure_artifacts}:
            raise ValueError("extension figures keys must match figure_artifacts names")
        return self


def write_analysis_context(
    run_dir: Path,
    request: ExperimentModuleInput,
    metric_records: list[MetricRecord],
    aggregates: list[AggregateRecord],
    findings: list[FindingRecord],
    analyses: list[AnalysisRecord],
    visualization_data: list[VisualizationDataAsset],
    visualization_candidates: list[VisualizationCandidate],
) -> Path:
    path = run_dir / "analysis" / "extension-context.json"
    write_json_atomic(
        path,
        {
            "domain": request.domain.model_dump(mode="json"),
            "method_design": request.method_design.model_dump(mode="json"),
            "experiment_plan": request.experiment_plan.model_dump(mode="json"),
            "metric_records": [item.model_dump(mode="json") for item in metric_records],
            "aggregates": [item.model_dump(mode="json") for item in aggregates],
            "finding_records": [item.model_dump(mode="json") for item in findings],
            "analysis_records": [item.model_dump(mode="json") for item in analyses],
            "visualization_data": [
                item.model_dump(mode="json") for item in visualization_data
            ],
            "visualization_candidates": [
                item.model_dump(mode="json") for item in visualization_candidates
            ],
        },
    )
    return path


def run_analysis_extensions(
    manifest: ImplementationManifest,
    run_dir: Path,
    context_path: Path,
    *,
    timeout_seconds: int | None,
    reserved_table_names: set[str] | None = None,
    reserved_figure_names: set[str] | None = None,
) -> tuple[AnalysisExtensionOutput, list[str], list[str]]:
    combined = AnalysisExtensionOutput()
    warnings: list[str] = []
    required_failures: list[str] = []
    table_names = set(reserved_table_names or set())
    figure_names = set(reserved_figure_names or set())
    context = read_json(context_path)
    known_metric_ids = {item["record_id"] for item in context["metric_records"]}
    known_experiment_ids = {
        item["experiment_id"] for item in context["metric_records"]
    }
    known_finding_ids = {item["finding_id"] for item in context["finding_records"]}
    known_analysis_ids = {item["analysis_id"] for item in context["analysis_records"]}
    known_data_ids = {item["data_id"] for item in context["visualization_data"]}
    known_candidate_ids = {
        item["candidate_id"] for item in context["visualization_candidates"]
    }
    for name, definition in manifest.analysis_extensions.items():
        if not definition.ready:
            continue
        missing_environment = [
            variable for variable in definition.required_env if variable not in os.environ
        ]
        if missing_environment:
            message = (
                f"领域分析扩展{name}缺少环境变量: "
                + ", ".join(missing_environment)
            )
            (required_failures if definition.required else warnings).append(message)
            continue
        try:
            output = _run_one_extension(
                name,
                definition,
                run_dir,
                context_path,
                timeout_seconds=timeout_seconds,
            )
            table_collisions = table_names & set(output.tables)
            figure_collisions = figure_names & set(output.figures)
            if table_collisions or figure_collisions:
                raise ValueError(
                    "artifact names collide with an earlier candidate: "
                    f"tables={sorted(table_collisions)}, "
                    f"figures={sorted(figure_collisions)}"
                )
            _validate_references(
                output,
                known_metric_ids=known_metric_ids,
                known_experiment_ids=known_experiment_ids,
                known_finding_ids=known_finding_ids,
                known_analysis_ids=known_analysis_ids,
                known_data_ids=known_data_ids,
                known_candidate_ids=known_candidate_ids,
            )
            _merge_outputs(combined, output)
            table_names.update(output.tables)
            figure_names.update(output.figures)
            warnings.extend(output.warnings)
        except Exception as exc:
            message = f"领域分析扩展{name}失败: {exc}"
            (required_failures if definition.required else warnings).append(message)
    return combined, warnings, required_failures


def _validate_references(
    output: AnalysisExtensionOutput,
    *,
    known_metric_ids: set[str],
    known_experiment_ids: set[str],
    known_finding_ids: set[str],
    known_analysis_ids: set[str],
    known_data_ids: set[str],
    known_candidate_ids: set[str],
) -> None:
    incoming_analysis_ids = [item.analysis_id for item in output.analysis_records]
    incoming_data_ids = [item.data_id for item in output.visualization_data]
    incoming_candidate_ids = [
        item.candidate_id for item in output.visualization_candidates
    ]
    _require_new_unique_ids(
        "analysis_id", incoming_analysis_ids, known_analysis_ids
    )
    _require_new_unique_ids("data_id", incoming_data_ids, known_data_ids)
    _require_new_unique_ids(
        "candidate_id", incoming_candidate_ids, known_candidate_ids
    )

    analysis_metric_refs = {
        record_id
        for item in output.analysis_records
        for record_id in item.source_metric_record_ids
    }
    data_metric_refs = {
        record_id
        for item in output.visualization_data
        for record_id in item.source_metric_record_ids
    }
    unknown_metric_refs = (analysis_metric_refs | data_metric_refs) - known_metric_ids
    if unknown_metric_refs:
        raise ValueError(
            f"extension references unknown metric records: {sorted(unknown_metric_refs)}"
        )

    referenced_experiments = {
        experiment_id
        for item in output.analysis_records
        for experiment_id in item.source_experiment_ids
    }
    referenced_experiments.update(
        experiment_id
        for item in output.visualization_data
        for experiment_id in item.source_experiment_ids
    )
    referenced_experiments.update(
        experiment_id
        for item in output.visualization_candidates
        for experiment_id in item.source_experiment_ids
    )
    referenced_experiments.update(
        experiment_id
        for item in output.table_artifacts
        for experiment_id in item.source_experiment_ids
    )
    referenced_experiments.update(
        experiment_id
        for item in output.figure_artifacts
        for experiment_id in item.source_experiment_ids
    )
    unknown_experiments = referenced_experiments - known_experiment_ids
    if unknown_experiments:
        raise ValueError(
            f"extension references unknown experiments: {sorted(unknown_experiments)}"
        )

    available_analysis_ids = known_analysis_ids | set(incoming_analysis_ids)
    available_data_ids = known_data_ids | set(incoming_data_ids)
    candidate_analysis_refs = {
        analysis_id
        for item in output.visualization_candidates
        for analysis_id in item.analysis_ids
    }
    candidate_data_refs = {
        data_id
        for item in output.visualization_candidates
        for data_id in item.source_data_ids
    }
    candidate_finding_refs = {
        finding_id
        for item in output.visualization_candidates
        for finding_id in item.related_finding_ids
    }
    if unknown := candidate_analysis_refs - available_analysis_ids:
        raise ValueError(f"extension candidates reference unknown analyses: {sorted(unknown)}")
    if unknown := candidate_data_refs - available_data_ids:
        raise ValueError(f"extension candidates reference unknown data: {sorted(unknown)}")
    if unknown := candidate_finding_refs - known_finding_ids:
        raise ValueError(f"extension candidates reference unknown findings: {sorted(unknown)}")

    known_analysis_ids.update(incoming_analysis_ids)
    known_data_ids.update(incoming_data_ids)
    known_candidate_ids.update(incoming_candidate_ids)


def _require_new_unique_ids(
    label: str,
    values: list[str],
    existing: set[str],
) -> None:
    duplicates = {value for value in values if values.count(value) > 1}
    collisions = set(values) & existing
    if duplicates or collisions:
        raise ValueError(
            f"extension {label} values are duplicate or already used: "
            f"{sorted(duplicates | collisions)}"
        )


def _run_one_extension(
    name: str,
    definition: AnalysisExtensionDefinition,
    run_dir: Path,
    context_path: Path,
    *,
    timeout_seconds: int | None,
) -> AnalysisExtensionOutput:
    variables = {
        "extension": slugify(name),
        "run_dir": str(run_dir),
        "context_path": str(context_path),
        "raw_metrics_path": str(run_dir / "visualization" / "raw_metrics.csv"),
        "aggregated_metrics_path": str(
            run_dir / "visualization" / "aggregated_metrics.csv"
        ),
    }
    output_path = _resolve_output_path(
        expand_placeholders(definition.output_path, variables),
        run_dir,
    )
    variables["output_path"] = str(output_path)
    command = [
        expand_placeholders(argument, variables) for argument in definition.command
    ]
    cwd = safe_join(run_dir, definition.cwd)
    if not cwd.is_dir():
        raise ValueError(f"extension cwd does not exist: {definition.cwd}")
    if output_path.is_file():
        output_path.unlink()
    elif output_path.exists():
        raise ValueError(f"extension output path is not a file: {definition.output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = build_subprocess_environment(
        definition.env,
        definition.required_env,
    )
    log_path = safe_join(run_dir, f"logs/analysis-{slugify(name)}.log")
    write_text_atomic(
        log_path,
        "command: "
        + _display_command(command, run_dir)
        + "\n--- combined output ---\n",
    )
    with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        log_file.write(f"\n--- returncode: {completed.returncode} ---\n")
    if completed.returncode != 0:
        raise ValueError(
            f"command exited with code {completed.returncode}; "
            f"see {relative_to_run(log_path, run_dir)}"
        )
    if not output_path.is_file():
        raise ValueError("command succeeded but did not produce output JSON")
    output = AnalysisExtensionOutput.model_validate(read_json(output_path))
    _validate_artifact_paths(output, run_dir)
    return output


def _merge_outputs(
    combined: AnalysisExtensionOutput,
    incoming: AnalysisExtensionOutput,
) -> None:
    table_collisions = set(combined.tables) & set(incoming.tables)
    figure_collisions = set(combined.figures) & set(incoming.figures)
    if table_collisions or figure_collisions:
        raise ValueError(
            "extension compatibility artifact names collide: "
            f"tables={sorted(table_collisions)}, figures={sorted(figure_collisions)}"
        )
    combined.analysis_records.extend(incoming.analysis_records)
    combined.visualization_data.extend(incoming.visualization_data)
    combined.visualization_candidates.extend(incoming.visualization_candidates)
    combined.tables.update(incoming.tables)
    combined.figures.update(incoming.figures)
    combined.table_artifacts.extend(incoming.table_artifacts)
    combined.figure_artifacts.extend(incoming.figure_artifacts)
    combined.warnings.extend(incoming.warnings)


def _validate_artifact_paths(output: AnalysisExtensionOutput, run_dir: Path) -> None:
    required_paths: list[str] = [item.path for item in output.visualization_data]
    required_paths.extend(item.path for item in output.table_artifacts)
    required_paths.extend(item.path for item in output.figure_artifacts)
    required_paths.extend(
        item.preview_path
        for item in output.visualization_candidates
        if item.preview_path is not None
    )
    required_paths.extend(
        item.editable_spec_path
        for item in output.visualization_candidates
        if item.editable_spec_path is not None
    )
    missing = [path for path in required_paths if not safe_join(run_dir, path).is_file()]
    if missing:
        raise ValueError(f"extension references missing artifact files: {missing}")


def _resolve_output_path(value: str, run_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        root = run_dir.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"extension output escapes run directory: {value}")
        return resolved
    return safe_join(run_dir, value)


def _display_command(command: list[str], run_dir: Path) -> str:
    return portable_command(command, run_dir=run_dir)
