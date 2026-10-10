# -*- coding: utf-8 -*-
"""
write_outputs.py — 写盘规划模块全部输出（实现）

按 [references/planning-schemas.md §2 输出侧](../references/planning-schemas.md) 把 5 个产物写到 output_dir：
    1. MethodDesign
    2. MethodReview
    3. ExperimentPlan
    4. DataPlan
    5. ExecutionConfig

每个产物同时写 2 份：
    - .json  —— 机器读，给模块三 ExperimentExecutor / DataPreparer / 模块四 Writer 消费
    - .md    —— 人读，便于人工检查

约定输出目录结构:
    output_dir/
    ├── method_design.json
    ├── method_design.md
    ├── method_review.json
    ├── method_review.md
    ├── experiment_plan.json
    ├── experiment_plan.md
    ├── data_plan.json
    ├── data_plan.md
    ├── execution_config.json
    └── execution_config.md

示例:
    python scripts/write_outputs.py \\
        --method-design outputs/method_design.json \\
        --method-review outputs/method_review.json \\
        --experiment-plan outputs/experiment_plan.json \\
        --data-plan outputs/data_plan.json \\
        --execution-config outputs/execution_config.json \\
        --output-dir outputs/
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.write_outputs")


# ════════════════════════════════════════════════════════════════
# 字段顺序与必填/可选（与 planning-schemas.md §2 表格对齐）
# ════════════════════════════════════════════════════════════════

# MethodDesign（§2.1）字段顺序 + 必填集合
_METHOD_DESIGN_ORDER = [
    "research_goal", "core_mechanism", "framework", "components",
    "technical_route", "algorithm_reference", "innovation_points",
    "hypothesis_coverage", "limitations", "implementable",
]
_METHOD_DESIGN_REQUIRED = {
    "research_goal", "core_mechanism", "framework", "components",
    "technical_route", "innovation_points", "hypothesis_coverage",
    "limitations", "implementable",
}
# algorithm_reference 是 ⬜ 可选

# MethodDesign 嵌套实体的必填字段
_NESTED_COMPONENT_REQUIRED = {"name", "function", "input_schema", "output_schema", "novelty_degree"}
# evidence_metric 2026-09-09 由自由字符串改为 list[EvidenceMetric]（§2.1.4）——
# 这里只管"字段必须在"，形状校验在 validate_plan._check_evidence_metric_list。
_NESTED_INNOVATION_REQUIRED = {"claim", "evidence_metric", "experiment_ref"}
_NESTED_HYPOTHESIS_COVERAGE_REQUIRED = {"hypothesis_id", "mechanism", "experiment_ref"}
# EvidenceMetric（§2.1.4）子字段：vs 仅 comparison_mode='delta' 时必填，故不列入
_NESTED_EVIDENCE_METRIC_REQUIRED = {
    "metric_name", "comparison_mode", "comparator", "threshold", "unit",
}

# MethodReview（§2.2）
_METHOD_REVIEW_ORDER = [
    "novelty_ok", "overlap_papers", "feasibility_risks", "missing_components",
    "adequacy_score", "feedback", "passed",
]
_METHOD_REVIEW_REQUIRED = {
    "novelty_ok", "feasibility_risks", "adequacy_score", "feedback", "passed",
}
# overlap_papers / missing_components 是 ⬜ 可选

# ExperimentPlan（§2.3）
_EXPERIMENT_PLAN_ORDER = [
    "objectives", "datasets", "baselines", "metrics", "primary_experiments",
    "experiment_matrix", "ablation_plan", "expected_results",
    "success_criteria", "compute_estimate",
]
_EXPERIMENT_PLAN_REQUIRED = set(_EXPERIMENT_PLAN_ORDER)  # 全部必填

# ExperimentPlan 嵌套实体的必填字段
_NESTED_DATASET_REQUIRED = {"name", "source_url", "scale_estimate", "readiness"}
_NESTED_BASELINE_REQUIRED = {"name", "paper_id", "metric_name"}
_NESTED_ABLATION_REQUIRED = {"component", "removed_by"}

# DataPlan（§2.4，2026-08-29 v2）
# 必填：datasets + usage_plan（新增）
# 可选：split_strategy / preprocessing_pipeline / expected_size（v2 降为可选）
_DATA_PLAN_ORDER = [
    "datasets", "usage_plan",
    "split_strategy", "preprocessing_pipeline", "expected_size",
]
_DATA_PLAN_REQUIRED = {"datasets", "usage_plan"}

# ExecutionConfig（§2.5）—— 模块二的下游输出
_EXECUTION_CONFIG_ORDER = [
    "run_dir", "result_dir", "seeds", "max_retries", "timeout_seconds", "dry_run",
]
_EXECUTION_CONFIG_REQUIRED = {
    "run_dir", "result_dir", "seeds", "max_retries", "dry_run",
}
# timeout_seconds 是 ⬜ 可选（int / null）

# ExperimentReview（§2.6，2026-08-29 新增）—— experiment-critic 反思循环的最终输出
_EXPERIMENT_REVIEW_ORDER = [
    "novelty_ok", "overlap_baselines", "feasibility_risks",
    "missing_baselines", "missing_metrics",
    "adequacy_score", "feedback", "feedback_addressed", "passed",
]
_EXPERIMENT_REVIEW_REQUIRED = {
    "novelty_ok", "feasibility_risks", "adequacy_score", "feedback", "passed",
}
# overlap_baselines / missing_baselines / missing_metrics / feedback_addressed 是 ⬜ 可选

# 实体名 → (字段顺序, 必填集合) 映射
_ENTITY_FIELD_MAP: dict[str, tuple[list[str], set[str]]] = {
    "MethodDesign": (_METHOD_DESIGN_ORDER, _METHOD_DESIGN_REQUIRED),
    "MethodReview": (_METHOD_REVIEW_ORDER, _METHOD_REVIEW_REQUIRED),
    "ExperimentPlan": (_EXPERIMENT_PLAN_ORDER, _EXPERIMENT_PLAN_REQUIRED),
    "DataPlan": (_DATA_PLAN_ORDER, _DATA_PLAN_REQUIRED),
    "ExecutionConfig": (_EXECUTION_CONFIG_ORDER, _EXECUTION_CONFIG_REQUIRED),
    "ExperimentReview": (_EXPERIMENT_REVIEW_ORDER, _EXPERIMENT_REVIEW_REQUIRED),  # 2026-08-29 新增
}

# 嵌套实体的必填字段映射（按外层字段名）
_NESTED_REQUIRED_MAP: dict[str, set[str]] = {
    "components": _NESTED_COMPONENT_REQUIRED,
    "innovation_points": _NESTED_INNOVATION_REQUIRED,
    "hypothesis_coverage": _NESTED_HYPOTHESIS_COVERAGE_REQUIRED,
    "evidence_metric": _NESTED_EVIDENCE_METRIC_REQUIRED,  # 2026-09-09 结构化后新增
    "datasets": _NESTED_DATASET_REQUIRED,  # 同时用于 ExperimentPlan 和 DataPlan
    "baselines": _NESTED_BASELINE_REQUIRED,
    "ablation_plan": _NESTED_ABLATION_REQUIRED,
}


# ════════════════════════════════════════════════════════════════
# 总入口
# ════════════════════════════════════════════════════════════════


def write_outputs(
    method_design: dict,
    method_review: dict,
    experiment_plan: dict,
    data_plan: dict,
    output_dir: str,
    execution_config: dict | None = None,
    experiment_review: dict | None = None,
) -> list[str]:
    """
    把产物写到 output_dir，每个写 .json + .md 双格式。

    Args:
        method_design:    MethodDesign 全字段 dict（planning-schemas.md §2.1）。
        method_review:    MethodReview 全字段 dict（planning-schemas.md §2.2）。
        experiment_plan:  ExperimentPlan 全字段 dict（planning-schemas.md §2.3）。
        data_plan:        DataPlan 全字段 dict（planning-schemas.md §2.4）。
        output_dir:       输出目录路径（不存在则创建）。
        execution_config: ExecutionConfig 全字段 dict（planning-schemas.md §2.5）；
                          为 None 时跳过（向后兼容：旧版 4 产物脚本不传也不报错）。
        experiment_review:ExperimentReview 全字段 dict（planning-schemas.md §2.6，2026-08-29 新增）；
                          为 None 或空 dict 时跳过（向后兼容：旧调用方不传也不报错；
                          REPLAN_DATA 模式也走空 dict 路径）。

    Returns:
        写出的文件路径列表（绝对路径），共 8/10/12 个：4/5/6 个 .json + 4/5/6 个 .md。
        顺序：method_design.{json,md} → method_review.{json,md} →
              experiment_plan.{json,md} → data_plan.{json,md} →
              [execution_config.{json,md}] → [experiment_review.{json,md}]。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    paths.extend(_write_method_design(method_design, str(out)))
    paths.extend(_write_method_review(method_review, str(out)))
    paths.extend(_write_experiment_plan(experiment_plan, str(out)))
    paths.extend(_write_data_plan(data_plan, str(out)))
    if execution_config is not None:
        paths.extend(_write_execution_config(execution_config, str(out)))
    # 2026-08-29 新增：写 experiment_review（experiment-critic 反思循环产物）
    if experiment_review:
        paths.extend(_write_experiment_review(experiment_review, str(out)))
    return [str(p) for p in paths]


# ════════════════════════════════════════════════════════════════
# 4 个产物的写盘函数
# ════════════════════════════════════════════════════════════════


def _write_json(payload: dict, json_path: Path) -> None:
    """写 .json（indent=2, ensure_ascii=False，不 sort_keys）。"""
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def _write_method_design(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 method_design.json + method_design.md。"""
    json_path = Path(output_dir) / "method_design.json"
    md_path = Path(output_dir) / "method_design.md"
    _write_json(payload, json_path)
    md_path.write_text(_to_markdown("MethodDesign", payload), encoding="utf-8")
    return json_path, md_path


def _write_method_review(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 method_review.json + method_review.md。"""
    json_path = Path(output_dir) / "method_review.json"
    md_path = Path(output_dir) / "method_review.md"
    _write_json(payload, json_path)
    md_path.write_text(_to_markdown("MethodReview", payload), encoding="utf-8")
    return json_path, md_path


def _write_experiment_plan(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 experiment_plan.json + experiment_plan.md。"""
    json_path = Path(output_dir) / "experiment_plan.json"
    md_path = Path(output_dir) / "experiment_plan.md"
    _write_json(payload, json_path)
    # experiment_matrix 用专用表格渲染，其他用通用 _to_markdown
    md_path.write_text(
        _to_markdown(
            "ExperimentPlan",
            payload,
            special_handlers={"experiment_matrix": _render_experiment_matrix_table},
        ),
        encoding="utf-8",
    )
    return json_path, md_path


def _write_data_plan(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 data_plan.json + data_plan.md。"""
    json_path = Path(output_dir) / "data_plan.json"
    md_path = Path(output_dir) / "data_plan.md"
    _write_json(payload, json_path)
    md_path.write_text(_to_markdown("DataPlan", payload), encoding="utf-8")
    return json_path, md_path


def _write_execution_config(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 execution_config.json + execution_config.md（§2.5）。"""
    json_path = Path(output_dir) / "execution_config.json"
    md_path = Path(output_dir) / "execution_config.md"
    _write_json(payload, json_path)
    md_path.write_text(_to_markdown("ExecutionConfig", payload), encoding="utf-8")
    return json_path, md_path


def _write_experiment_review(payload: dict, output_dir: str) -> tuple[Path, Path]:
    """写 experiment_review.json + experiment_review.md（§2.6，2026-08-29 新增）。"""
    json_path = Path(output_dir) / "experiment_review.json"
    md_path = Path(output_dir) / "experiment_review.md"
    _write_json(payload, json_path)
    md_path.write_text(_to_markdown("ExperimentReview", payload), encoding="utf-8")
    return json_path, md_path


# ════════════════════════════════════════════════════════════════
# 渲染工具
# ════════════════════════════════════════════════════════════════


def _to_markdown(
    entity_name: str,
    payload: dict,
    special_handlers: dict[str, Any] | None = None,
) -> str:
    """
    把产物的 dict 渲染为 Markdown 字符串。

    渲染规则:
        - 顶层标题: `# {entity_name}`
        - 顶层字段按 planning-schemas.md 表格顺序（见 _ENTITY_FIELD_MAP）
        - 必填字段为空 → 显式标 `_(无)_`
        - 可选字段为空 → 整行省略
        - 嵌套实体（list[dict]）→ `- *#{i}*` 索引 + 子字段缩进列表
        - 字典（如 split_strategy）→ Markdown 表格
        - 列表（标量）→ `- item` 列表
        - experiment_matrix 等特殊字段可通过 special_handlers 覆盖

    Args:
        entity_name: 实体名（如 "MethodDesign"），用于顶层标题和字段顺序查询。
        payload: 产物的 dict。
        special_handlers: 字段名 → 渲染函数的覆盖映射。

    Returns:
        Markdown 字符串。
    """
    if entity_name not in _ENTITY_FIELD_MAP:
        raise ValueError(f"unknown entity_name: {entity_name}")

    order, required_set = _ENTITY_FIELD_MAP[entity_name]
    special_handlers = special_handlers or {}

    parts: list[str] = [f"# {entity_name}\n\n"]
    for key in order:
        if key in special_handlers:
            rendered = special_handlers[key](payload.get(key))
        else:
            value = payload.get(key)
            required = key in required_set
            rendered = _render_field_md(key, value, required, indent=0)
        parts.append(rendered)
        parts.append("\n\n")  # 字段间空行
    return "".join(parts).rstrip() + "\n"


def _render_field_md(key: str, value: Any, required: bool = True, indent: int = 0) -> str:
    """
    渲染单个字段为 Markdown 行（无 trailing newline）。

    缺失值约定（与 [planning-schemas.md §0](../references/planning-schemas.md) 对齐）：
        - `value is None`       → 标 `_(null)_`（显式缺失，JSON 用 `null`）
        - `value == ""` / `[]` / `{}` → 标 `_(无)_`（防御性兜底；合法输入不应出现）
        - optional 字段为空时    → 整行省略（不渲染）

    Args:
        key: 字段名。
        value: 字段值。
        required: 是否必填。True 时显式标 `_(null)_` / `_(无)_`；False 时空值返回空串（整行省略）。
        indent: 缩进级别（0/1/2...）。

    Returns:
        Markdown 字符串片段。
    """
    prefix = "  " * indent
    # 区分 None（显式缺失）vs 空集合（不应出现，防御性兜底）
    if value is None:
        if required:
            return f"{prefix}- **{key}**: _(null)_"
        return ""
    is_empty = value == "" or value == [] or value == {}
    if is_empty:
        if required:
            return f"{prefix}- **{key}**: _(无)_"
        return ""

    # bool 要在 int 之前判断（bool 是 int 的子类）
    if isinstance(value, bool):
        return f"{prefix}- **{key}**: {str(value).lower()}"

    if isinstance(value, (str, int, float)):
        return f"{prefix}- **{key}**: {value}"

    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return _render_dict_list(key, value, required, indent)
        return _render_scalar_list(key, value, required, indent)

    if isinstance(value, dict):
        return _render_dict_table(key, value, indent)

    return f"{prefix}- **{key}**: {value}"


def _render_scalar_list(key: str, value: list, required: bool, indent: int) -> str:
    """渲染 list[str | int | float]（如 metrics / expected_results / success_criteria）。"""
    prefix = "  " * indent
    lines = [f"{prefix}- **{key}**:"]
    for item in value:
        lines.append(f"{prefix}  - {item}")
    return "\n".join(lines)


def _render_dict_list(key: str, value: list[dict], required: bool, indent: int) -> str:
    """
    渲染 list[dict]（如 components / innovation_points / hypothesis_coverage /
    datasets / baselines / ablation_plan）。

    嵌套实体的必填字段从 _NESTED_REQUIRED_MAP 取；空 dict 不强制占位。
    """
    prefix = "  " * indent
    nested_required = _NESTED_REQUIRED_MAP.get(key, set())
    lines = [f"{prefix}- **{key}**:"]
    for i, item in enumerate(value, 1):
        lines.append(f"{prefix}  - *#{i}*")
        if not isinstance(item, dict):
            lines.append(f"{prefix}    - {item}")
            continue
        for sub_key, sub_value in item.items():
            sub_required = sub_key in nested_required
            sub_rendered = _render_field_md(sub_key, sub_value, sub_required, indent + 2)
            if sub_rendered:  # 跳过可选空字段
                lines.append(sub_rendered)
    return "\n".join(lines)


def _render_dict_table(key: str, value: dict, indent: int) -> str:
    """
    渲染 dict 为 Markdown 表格（如 split_strategy / expected_size）。
    """
    prefix = "  " * indent
    lines = [
        f"{prefix}- **{key}**:",
        f"{prefix}  | key | value |",
        f"{prefix}  | --- | --- |",
    ]
    for k, v in value.items():
        if isinstance(v, bool):
            v_str = str(v).lower()
        else:
            v_str = str(v)
        v_str = v_str.replace("|", "\\|")  # 转义管道符避免破坏表格
        lines.append(f"{prefix}  | {k} | {v_str} |")
    return "\n".join(lines)


def _render_experiment_matrix_table(matrix: Any) -> str:
    """
    渲染 experiment_matrix 为固定 3 列 Markdown 表格：| dataset | baseline | variable |。

    规则：
        - 不足 3 列填 `—`
        - 多余列拼接到最后一列（逗号分隔）
        - 空矩阵 → `_(无)_`
    """
    if not matrix or not isinstance(matrix, list):
        return "- **experiment_matrix**: _(无)_"
    lines = [
        "- **experiment_matrix**:",
        "  | dataset | baseline | variable |",
        "  | --- | --- | --- |",
    ]
    for row in matrix:
        if not isinstance(row, list):
            lines.append(f"  | {row} | — | — |")
            continue
        cells = [str(c).replace("|", "\\|") for c in row]
        if len(cells) < 3:
            cells = cells + ["—"] * (3 - len(cells))
        elif len(cells) > 3:
            cells = cells[:2] + [", ".join(cells[2:])]
        lines.append(f"  | {cells[0]} | {cells[1]} | {cells[2]} |")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """
    CLI 入口:
        python scripts/write_outputs.py \\
            --method-design <path> --method-review <path> \\
            --experiment-plan <path> --data-plan <path> \\
            [--execution-config <path>] \\
            --output-dir <path>

    每个 --xxx 参数指向已经存盘的 JSON 产物文件，脚本读取后写 .json + .md 双格式。
    --execution-config 可选；不传时 ExecutionConfig 不写出。
    """
    import argparse

    parser = argparse.ArgumentParser(description="规划模块产物写盘（.json + .md 双格式）")
    parser.add_argument("--method-design", required=True, help="method_design.json 路径")
    parser.add_argument("--method-review", required=True, help="method_review.json 路径")
    parser.add_argument("--experiment-plan", required=True, help="experiment_plan.json 路径")
    parser.add_argument("--data-plan", required=True, help="data_plan.json 路径")
    parser.add_argument("--execution-config", required=False, default=None,
                        help="execution_config.json 路径（可选）")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    args = parser.parse_args(argv)

    def _load(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    paths = write_outputs(
        method_design=_load(args.method_design),
        method_review=_load(args.method_review),
        experiment_plan=_load(args.experiment_plan),
        data_plan=_load(args.data_plan),
        execution_config=_load(args.execution_config) if args.execution_config else None,
        output_dir=args.output_dir,
    )
    for p in paths:
        log.info(p)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
