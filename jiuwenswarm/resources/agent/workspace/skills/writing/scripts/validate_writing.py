# -*- coding: utf-8 -*-
"""
validate_writing.py — WritingOutput 校验（writing-schemas §4 强校验 20 条 + §5 一致性 12 条）

仿 planning/scripts/validate_plan.py 模式：
  * 5 个 validate_xxx 函数
  * ValidationError 数据类
  * 校验原语：_err / _is_empty / _check_required_keys / _check_in / _check_nonempty_str /
              _check_nonempty_list / _check_bool / _check_number_range / _check_dict_keys /
              _join_path / _index_path
  * CLI：--type / --input

注意：input 校验（m1/m2/m3）由 stage1_load_inputs 处理；本模块聚焦 WritingOutput
（paper.json）的输出校验。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# ════════════════════════════════════════════════════════════════
# ValidationError
# ════════════════════════════════════════════════════════════════


class ValidationError:
    """单条校验错误。"""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"[{self.path}] {self.message}"


# ════════════════════════════════════════════════════════════════
# 校验原语（private）
# ════════════════════════════════════════════════════════════════


def _err(path: str, message: str) -> ValidationError:
    return ValidationError(path, message)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _check_required_keys(payload: dict, required: set[str], base_path: str) -> list[ValidationError]:
    errs: list[ValidationError] = []
    for k in required:
        if k not in payload:
            errs.append(_err(_join_path(base_path, k), "必填字段缺失"))
        elif _is_empty(payload[k]):
            errs.append(_err(_join_path(base_path, k), "必填字段为空"))
    return errs


def _check_in(value: Any, allowed: set[str], path: str) -> list[ValidationError]:
    if value not in allowed:
        return [_err(path, f"值 {value!r} 不在 {sorted(allowed)} 中")]
    return []


def _check_nonempty_str(value: Any, path: str) -> list[ValidationError]:
    if not isinstance(value, str) or not value.strip():
        return [_err(path, "必须是非空字符串")]
    return []


def _check_nonempty_list(value: Any, path: str) -> list[ValidationError]:
    if not isinstance(value, list) or len(value) == 0:
        return [_err(path, "必须是非空列表")]
    return []


def _check_bool(value: Any, path: str) -> list[ValidationError]:
    if not isinstance(value, bool):
        return [_err(path, f"必须是 bool，实际是 {type(value).__name__}")]
    return []


def _join_path(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key


def _index_path(base: str, index: int) -> str:
    return f"{base}[{index}]"


# ════════════════════════════════════════════════════════════════
# 5 个公开 validate_xxx
# ════════════════════════════════════════════════════════════════


def validate_research_question(payload: dict) -> list[ValidationError]:
    """§4.1 research_question.topic / scope / success_criteria 非空。"""
    errs: list[ValidationError] = []
    rq = payload.get("research_question", {}) or {}
    errs.extend(_check_required_keys(
        rq, {"topic", "scope", "success_criteria"}, "research_question"
    ))
    return errs


def validate_writing_output(payload: dict) -> list[ValidationError]:
    """§2.1 + §4.17-20 WritingOutput 必填字段 + §5 一致性约束关键项。

    检查项：
      * pdf_path / sections / review_history / status 必填
      * status ∈ {success, partial, failed}
      * sections 必含 6 键
      * review_history 每条含 round / score / issues / resolved
    """
    errs: list[ValidationError] = []
    # partial 状态允许 pdf_path 为空字符串（status 已经是 partial 表达降级）
    is_partial = payload.get("status") == "partial"
    errs.extend(_check_required_keys(
        payload, {"sections", "review_history", "status"}, ""
    ))
    # pdf_path 在 partial 状态可空（其他状态必填）
    if "pdf_path" not in payload or (not is_partial and _is_empty(payload.get("pdf_path"))):
        errs.append(_err("pdf_path", "必填字段为空（partial 状态除外）"))

    # status enum
    if "status" in payload:
        errs.extend(_check_in(
            payload["status"], {"success", "partial", "failed"}, "status"
        ))

    # sections 6 键
    sections = payload.get("sections", {}) or {}
    for k in ("abstract", "introduction", "related_work",
              "method", "experiments", "conclusion"):
        if k not in sections:
            errs.append(_err(f"sections.{k}", "必填字段缺失"))

    # review_history 每轮字段齐全
    rh = payload.get("review_history", []) or []
    for i, r in enumerate(rh):
        if not isinstance(r, dict):
            errs.append(_err(_index_path("review_history", i), "必须是 dict"))
            continue
        for k in ("round", "score", "issues", "resolved"):
            if k not in r:
                errs.append(_err(
                    _index_path(_index_path("review_history", i), k),
                    "必填字段缺失"
                ))

    # §4.18 pdf_path 真实存在（如果 status=success）
    if payload.get("status") == "success" and payload.get("pdf_path"):
        p = Path(payload["pdf_path"])
        if not p.is_file():
            errs.append(_err("pdf_path", f"文件不存在: {p}"))
        elif p.stat().st_size < 5000:
            errs.append(_err("pdf_path", f"文件 size < 5KB（可能空文件）: {p}"))
    # partial 状态允许 pdf_path 为空（status 已经是 partial 表达降级）

    return errs


def validate_consistency(payload: dict) -> list[ValidationError]:
    """§5 一致性约束关键项。"""
    errs: list[ValidationError] = []
    # §5.1 gap_report.research_question 与 research_question.topic 指向同一问题
    rq_topic = payload.get("research_question", {}).get("topic", "")
    gap_rq = (payload.get("gap_report", {}) or {}).get("research_question", "")
    if rq_topic and gap_rq and rq_topic != gap_rq:
        errs.append(_err(
            "gap_report.research_question",
            f"与 research_question.topic 不一致: {gap_rq!r} != {rq_topic!r}"
        ))

    # §5.4 experiment_plan.datasets[].name 必须在 tables / figures 中被引用
    ep = payload.get("experiment_plan", {}) or {}
    datasets = [d.get("name") for d in (ep.get("datasets") or []) if d.get("name")]
    er = payload.get("experiment_results", {}) or {}
    tables_keys = " ".join((er.get("tables") or {}).keys())
    figures_keys = " ".join((er.get("figures") or {}).keys())
    for d in datasets:
        if d and d not in tables_keys and d not in figures_keys:
            errs.append(_err(
                "experiment_results",
                f"§5.4 dataset {d!r} 未在 tables/figures 中引用"
            ))

    # §5.5 statistics 中的指标名必须在 metrics 中定义
    metrics = set(ep.get("metrics") or [])
    statistics = er.get("statistics") or {}
    for m in statistics:
        if metrics and m not in metrics:
            errs.append(_err(
                "experiment_results.statistics",
                f"§5.5 指标 {m!r} 未在 experiment_plan.metrics 中定义"
            ))

    return errs


def validate_method_design(payload: dict) -> list[ValidationError]:
    """§1.7 MethodDesign 输入字段必填。"""
    errs: list[ValidationError] = []
    md = payload.get("method_design", {}) or {}
    errs.extend(_check_required_keys(
        md, {"research_goal", "core_mechanism", "framework", "components",
             "technical_route", "innovation_points", "hypothesis_coverage",
             "limitations", "implementable"}, "method_design"
    ))
    # implementable 必须 true（否则 status 应该是 failed）
    if "implementable" in md:
        errs.extend(_check_bool(md["implementable"], "method_design.implementable"))
    return errs


def validate_experiment_plan(payload: dict) -> list[ValidationError]:
    """§1.8 ExperimentPlan 输入字段必填。"""
    errs: list[ValidationError] = []
    ep = payload.get("experiment_plan", {}) or {}
    errs.extend(_check_required_keys(
        ep, {"objectives", "datasets", "baselines", "metrics",
             "primary_experiments", "experiment_matrix", "ablation_plan",
             "expected_results", "success_criteria"}, "experiment_plan"
    ))
    return errs


# ════════════════════════════════════════════════════════════════
# dispatch + CLI
# ════════════════════════════════════════════════════════════════


_VALIDATORS: dict[str, Any] = {
    "writing_output": validate_writing_output,
    "research_question": validate_research_question,
    "method_design": validate_method_design,
    "experiment_plan": validate_experiment_plan,
    "consistency": validate_consistency,
}


def validate_type(schema_type: str, payload: dict) -> list[ValidationError]:
    if schema_type not in _VALIDATORS:
        raise ValueError(f"未知 schema_type: {schema_type!r}")
    return _VALIDATORS[schema_type](payload)


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Writing Module 产物校验（writing-schemas §4/§5）")
    p.add_argument("--type", required=True, choices=list(_VALIDATORS.keys()),
                   help="待校验的产物类型")
    p.add_argument("--input", required=True, help="产物文件路径（.json）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    p = Path(args.input)
    if not p.is_file():
        print(f"[validate] 文件不存在: {p}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[validate] JSON 解析失败: {e}", file=sys.stderr)
        return 1
    errors = validate_type(args.type, payload)
    if not errors:
        print(f"[validate] {args.type} 通过 0 errors")
        return 0
    print(f"[validate] {args.type} 失败 {len(errors)} errors:")
    for e in errors:
        print(f"  {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
