"""Generate a non-runnable manifest draft from a validated planning request."""

from __future__ import annotations

from pathlib import Path

from contracts import ExperimentModuleInput
from io_utils import slugify, write_json_atomic
from plan_execution import planned_method_names


def scaffold_manifest(
    request: ExperimentModuleInput,
    output_path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"manifest already exists: {output_path}; use explicit overwrite to replace it"
        )
    methods = sorted(planned_method_names(request))
    payload = {
        "schema_version": "1.0.0",
        "execution_approved": False,
        "approval_digest": None,
        "approved_by": None,
        "approved_at_utc": None,
        "approval_type": None,
        "reviewer_model": None,
        "review_digest": None,
        "reviewed_code_digest": None,
        "reviewed_smoke_digest": None,
        "datasets": {
            dataset.name: f"data/{slugify(dataset.name)}"
            for dataset in request.data_plan.datasets
        },
        "dataset_preparations": {
            dataset.name: {
                "ready": False,
                "planning_aliases": [],
                "experiment_ids": [],
                "command": [
                    "python",
                    f"implementations/data/prepare-{slugify(dataset.name)}.py",
                    "--dataset",
                    "{dataset_path}",
                    "--metadata",
                    "{metadata_path}",
                ],
                "cwd": ".",
                "env": {},
                "required_env": [],
                "marker_path": f"data/{slugify(dataset.name)}/.prepared.json",
                "notes": "未就绪：请实现DataPlan中的预处理步骤并完成冒烟测试",
            }
            for dataset in request.data_plan.datasets
            if _requires_preprocessing(request.data_plan.preprocessing_pipeline)
        },
        "analysis_extensions": {},
        "implementations": {
            method: {
                "ready": False,
                "command": [
                    "python",
                    f"implementations/{slugify(method)}/main.py",
                    "--config",
                    "{config_path}",
                    "--metrics",
                    "{metrics_path}",
                ],
                "cwd": ".",
                "env": {},
                "required_env": [],
                "metrics_path": "raw_results/{run_record_id}/metrics.json",
                "uses_gpu": False,
                "implementation_url": None,
                "revision": None,
                "source_kind": "EXISTING",
                "license": None,
                "dependency_plan": [],
                "smoke_test_command": [],
                "smoke_test_passed": False,
                "notes": "未就绪：请模块三Agent实现并冒烟测试后填写真实说明",
            }
            for method in methods
        },
        "metrics": {
            metric: {
                "verified": False,
                "definition_used": "未就绪：请填写本次采用的准确指标定义",
                "implementation": "未就绪：请填写评测脚本或函数入口",
                "parameters": {},
                "library": None,
                "library_version": None,
                "direction": "MAXIMIZE",
                "unit": None,
                "aggregation": "未就绪：请填写runner内部的样本级聚合方式",
                "seed_aggregation": None,
            }
            for metric in request.experiment_plan.metrics
        },
    }
    write_json_atomic(output_path, payload)
    return output_path


def _requires_preprocessing(steps: list[str]) -> bool:
    no_op_values = {"identity", "none", "noop", "no-op", "无需预处理"}
    return any(step.strip().lower() not in no_op_values for step in steps)
