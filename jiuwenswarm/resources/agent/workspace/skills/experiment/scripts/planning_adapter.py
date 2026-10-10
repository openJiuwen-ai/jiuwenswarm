"""Fail-closed adapter for Module 2's split planning artifacts.

The adapter is deliberately the only place where Module 2 compatibility rules
are applied.  The canonical ExperimentModuleInput remains strict and keeps
``extra='forbid'`` semantics.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from contracts import ExperimentModuleInput, PlanningFeedback
from io_utils import read_json
from load_inputs import load_manifest
from runtime_models import ImplementationManifest


MODULE_TWO_FILES = {
    "method_design": "method_design.json",
    "experiment_plan": "experiment_plan.json",
    "data_plan": "data_plan.json",
    "execution_config": "execution_config.json",
    "domain": "domain.json",
    "resource_constraints": "resource_constraints.json",
}


class AdapterStatus(str, Enum):
    READY = "READY"
    REPLAN = "REPLAN"


class PlanningAdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    adapter_version: str = "1.0.0"
    status: AdapterStatus
    run_id: str = Field(min_length=1)
    request: ExperimentModuleInput | None = None
    planning_feedback: PlanningFeedback | None = None
    source_files: dict[str, str]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_payload_matches(self) -> "PlanningAdapterResult":
        if self.status == AdapterStatus.READY.value:
            if self.request is None or self.planning_feedback is not None:
                raise ValueError("READY requires request and forbids planning_feedback")
        elif self.request is not None or self.planning_feedback is None:
            raise ValueError("REPLAN requires planning_feedback and forbids request")
        return self


def adapt_planning_bundle(
    planning_dir: Path,
    *,
    workspace_root: Path,
    manifest_path: Path | None = None,
    domain_path: Path | None = None,
    resource_constraints_path: Path | None = None,
) -> PlanningAdapterResult:
    """Assemble Module 2 artifacts into a canonical strict request.

    Paths are accepted only inside ``workspace_root``.  A three-column matrix
    is converted only when its method and experiment identity can be proven by
    the implementation manifest; ambiguity produces structured REPLAN output.
    """

    root = workspace_root.resolve()
    source_dir = _trusted_path(planning_dir, root, must_exist=True)
    if not source_dir.is_dir():
        raise ValueError("planning_dir must be a directory")
    paths = {
        name: _trusted_path(source_dir / filename, root, must_exist=True)
        for name, filename in MODULE_TWO_FILES.items()
        if name not in {"domain", "resource_constraints"}
    }
    paths["domain"] = _trusted_path(
        domain_path or source_dir / MODULE_TWO_FILES["domain"],
        root,
        must_exist=True,
    )
    paths["resource_constraints"] = _trusted_path(
        resource_constraints_path
        or source_dir / MODULE_TWO_FILES["resource_constraints"],
        root,
        must_exist=True,
    )
    payloads = {name: read_json(path) for name, path in paths.items()}
    source_files = {
        name: path.relative_to(root).as_posix() for name, path in paths.items()
    }

    blockers: list[str] = []
    suggestions: list[str] = []
    affected = _affected_ids(payloads.get("experiment_plan"))
    execution_config = payloads.get("execution_config")
    if not isinstance(execution_config, dict):
        blockers.append("execution_config.json必须包含JSON对象")
        suggestions.append("让模块二重新输出完整ExecutionConfig对象")
        execution_config = {}
    else:
        execution_config = dict(execution_config)
    # Module 2 owns this projection only.  No other unknown field is filtered.
    execution_config.pop("result_dir", None)
    try:
        execution_config["run_dir"] = _normalize_run_dir(
            execution_config.get("run_dir"), root
        )
    except (TypeError, ValueError) as exc:
        blockers.append(f"execution_config.run_dir无法安全转换: {exc}")
        suggestions.append(
            "把run_dir设置为可信工作区内的目录；不得使用工作区外绝对路径或.."
        )

    data_plan = payloads.get("data_plan")
    if isinstance(data_plan, dict):
        data_plan = dict(data_plan)
        preprocessing = data_plan.get("preprocessing_pipeline")
        if preprocessing is None or preprocessing == []:
            data_plan["preprocessing_pipeline"] = ["identity"]

    raw_for_id = {
        "method_design": payloads.get("method_design"),
        "experiment_plan": payloads.get("experiment_plan"),
        "data_plan": data_plan,
        "domain": payloads.get("domain"),
        "resource_constraints": payloads.get("resource_constraints"),
        "execution_config": execution_config,
    }
    run_id = _stable_run_id(raw_for_id)

    manifest: ImplementationManifest | None = None
    if not blockers:
        raw_manifest_path = manifest_path
        if raw_manifest_path is None:
            run_path = root / str(execution_config.get("run_dir", ""))
            raw_manifest_path = run_path / "implementation-manifest.json"
        try:
            resolved_manifest = _trusted_path(
                raw_manifest_path, root, must_exist=True
            )
            manifest = load_manifest(resolved_manifest)
            source_files["implementation_manifest"] = (
                resolved_manifest.relative_to(root).as_posix()
            )
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            blockers.append(f"implementation manifest不可用: {exc}")
            suggestions.append(
                "先由模块三Implementation Builder创建或补全实现清单，"
                "并显式登记规划别名与实验ID"
            )

    experiment_plan = payloads.get("experiment_plan")
    if isinstance(experiment_plan, dict):
        experiment_plan = dict(experiment_plan)
        if manifest is not None:
            matrix, matrix_blockers, matrix_suggestions = _convert_matrix(
                experiment_plan, manifest
            )
            if matrix is not None:
                experiment_plan["experiment_matrix"] = matrix
            blockers.extend(matrix_blockers)
            suggestions.extend(matrix_suggestions)
    else:
        blockers.append("experiment_plan.json必须包含JSON对象")
        suggestions.append("让模块二重新输出完整ExperimentPlan对象")

    if blockers:
        return _replan(
            run_id,
            affected,
            blockers,
            suggestions,
            source_files,
        )

    canonical = {
        "schema_version": "2.0.0",
        "run_id": run_id,
        "method_design": payloads.get("method_design"),
        "experiment_plan": experiment_plan,
        "data_plan": data_plan,
        "domain": payloads.get("domain"),
        "resource_constraints": payloads.get("resource_constraints"),
        "execution_config": execution_config,
    }
    try:
        request = ExperimentModuleInput.model_validate(canonical)
    except ValidationError as exc:
        field_errors = [
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        ]
        return _replan(
            run_id,
            affected,
            field_errors,
            ["按列出的精确字段修正模块二输出；模块三不会删除其他未知字段"],
            source_files,
        )
    return PlanningAdapterResult(
        status="READY",
        run_id=run_id,
        request=request,
        planning_feedback=None,
        source_files=source_files,
        warnings=[],
    )


def _convert_matrix(
    experiment_plan: dict[str, Any],
    manifest: ImplementationManifest,
) -> tuple[list[list[str]] | None, list[str], list[str]]:
    matrix = experiment_plan.get("experiment_matrix")
    if not isinstance(matrix, list) or not matrix:
        return None, ["experiment_plan.experiment_matrix必须是非空数组"], [
            "重新生成非空实验矩阵"
        ]
    if any(not isinstance(row, list) for row in matrix):
        return None, ["experiment_matrix每一行必须是数组"], [
            "每行使用字符串数组"
        ]
    lengths = {len(row) for row in matrix}
    if 3 in lengths and lengths != {3}:
        return None, ["三列兼容矩阵不能与模块三标准矩阵混用"], [
            "统一输出三列模块二格式，或全部升级为模块三标准格式"
        ]
    datasets = {
        item.get("name")
        for item in experiment_plan.get("datasets", [])
        if isinstance(item, dict)
    }
    baselines = {
        item.get("name")
        for item in experiment_plan.get("baselines", [])
        if isinstance(item, dict)
    }
    primary_ids = [
        item
        for item in experiment_plan.get("primary_experiments", [])
        if isinstance(item, str) and item.strip()
    ]
    blockers: list[str] = []
    suggestions: list[str] = []
    converted: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for index, row in enumerate(matrix):
        if any(not isinstance(item, str) or not item.strip() for item in row):
            blockers.append(f"experiment_matrix[{index}]包含空值或非字符串")
            continue
        cleaned = [item.strip() for item in row]
        if len(cleaned) == 3:
            dataset, baseline, variable = cleaned
            if dataset not in datasets:
                blockers.append(
                    f"experiment_matrix[{index}].dataset={dataset!r}未在datasets中定义"
                )
            if baseline not in baselines:
                blockers.append(
                    f"experiment_matrix[{index}].baseline={baseline!r}未在baselines中定义"
                )
            if baseline not in manifest.implementations:
                blockers.append(
                    f"experiment_matrix[{index}]的基线{baseline!r}没有实现登记"
                )
            method = _resolve_method(variable, manifest)
            if method is None:
                blockers.append(
                    f"experiment_matrix[{index}].variable={variable!r}无法唯一映射到真实方法"
                )
                suggestions.append(
                    f"在唯一实现的planning_aliases中显式登记{variable!r}，"
                    "不要用含义不明的消融描述代替方法名"
                )
                continue
            experiment_id = _resolve_experiment_id(method, primary_ids, manifest)
            if experiment_id is None:
                blockers.append(
                    f"experiment_matrix[{index}]无法为方法{method!r}唯一确定experiment_id"
                )
                suggestions.append(
                    f"在implementations.{method}.experiment_ids中登记一个对应的主实验ID"
                )
                continue
            normalized = [experiment_id, dataset, baseline, method]
        elif len(cleaned) >= 4:
            normalized = cleaned
            experiment_id, dataset = normalized[:2]
            if experiment_id not in set(primary_ids) | {
                item for item in (row[0] for row in matrix) if isinstance(item, str)
            }:
                blockers.append(
                    f"experiment_matrix[{index}].experiment_id={experiment_id!r}没有规划引用"
                )
            if dataset not in datasets:
                blockers.append(
                    f"experiment_matrix[{index}].dataset={dataset!r}未在datasets中定义"
                )
            for token_index, token in enumerate(normalized[2:], start=2):
                if "=" in token:
                    continue
                method = _resolve_method(token, manifest)
                if method is None:
                    blockers.append(
                        f"experiment_matrix[{index}][{token_index}]={token!r}"
                        "无法唯一映射到实现清单"
                    )
                else:
                    normalized[token_index] = method
        else:
            blockers.append(
                f"experiment_matrix[{index}]有{len(cleaned)}列；只接受三列兼容格式或至少四列标准格式"
            )
            continue
        signature = tuple(normalized)
        if signature in seen:
            blockers.append(f"experiment_matrix[{index}]转换后与另一行完全重复")
        seen.add(signature)
        converted.append(normalized)

    if blockers:
        suggestions.append(
            "保证数据集、基线、方法和实验ID都能在相应清单中精确交叉引用"
        )
        return None, blockers, list(dict.fromkeys(suggestions))
    return converted, [], []


def _resolve_method(
    value: str, manifest: ImplementationManifest
) -> str | None:
    normalized = value.strip().casefold()
    matches = [
        name
        for name, definition in manifest.implementations.items()
        if normalized
        in {
            name.strip().casefold(),
            *(alias.strip().casefold() for alias in definition.planning_aliases),
        }
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_experiment_id(
    method: str,
    primary_ids: list[str],
    manifest: ImplementationManifest,
) -> str | None:
    if len(primary_ids) == 1:
        return primary_ids[0]
    declared = [
        item
        for item in manifest.implementations[method].experiment_ids
        if item in primary_ids
    ]
    return declared[0] if len(declared) == 1 else None


def _normalize_run_dir(value: Any, root: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError("run_dir必须是非空字符串")
    path = Path(value.strip())
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"{value!r}位于可信工作区之外")
    relative = resolved.relative_to(root).as_posix()
    if relative in {"", "."}:
        raise ValueError("run_dir不能等于整个可信工作区")
    return relative


def _trusted_path(value: Path, root: Path, *, must_exist: bool) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve(strict=must_exist)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path is outside the trusted workspace: {value}")
    return resolved


def _stable_run_id(payload: dict[str, Any]) -> str:
    # ``run_dir`` is a storage address, not part of the scientific run
    # identity.  Including it makes relocation change run_id and, worse,
    # prevents callers from safely using ``runs/<run_id>`` without a circular
    # dependency.  Work on a JSON-shaped copy so the caller's contract is not
    # mutated.
    identity = dict(payload)
    execution = identity.get("execution_config")
    if isinstance(execution, dict):
        execution = dict(execution)
        execution.pop("run_dir", None)
        identity["execution_config"] = execution
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "experiment-" + hashlib.sha256(encoded).hexdigest()[:20]


def _affected_ids(experiment_plan: Any) -> list[str]:
    if isinstance(experiment_plan, dict):
        values = experiment_plan.get("primary_experiments")
        if isinstance(values, list):
            ids = [item for item in values if isinstance(item, str) and item.strip()]
            if ids:
                return list(dict.fromkeys(ids))
    return ["unresolved"]


def _replan(
    run_id: str,
    affected: list[str],
    blockers: list[str],
    suggestions: list[str],
    source_files: dict[str, str],
) -> PlanningAdapterResult:
    return PlanningAdapterResult(
        status="REPLAN",
        run_id=run_id,
        request=None,
        planning_feedback=PlanningFeedback(
            reason="模块二兼容输入无法安全组装为模块三正式契约",
            affected_experiment_ids=affected or ["unresolved"],
            blockers=list(dict.fromkeys(blockers)),
            suggested_changes=list(dict.fromkeys(suggestions))
            or ["修正阻断字段后使用相同规划重新适配"],
        ),
        source_files=source_files,
        warnings=[],
    )
