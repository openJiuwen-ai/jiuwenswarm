# -*- coding: utf-8 -*-
"""
validate_plan.py — 规划模块输出契约束校验（实现）

按 [references/planning-schemas.md](../references/planning-schemas.md) §2 校验 5 个产物的字段契约：
    1. MethodDesign
    2. MethodReview
    3. ExperimentPlan
    4. DataPlan
    5. ExecutionConfig

校验粒度：必填 + 枚举 + 嵌套实体字段 + **跨产物交叉引用**。

跨产物校验（2026-09-09 新增 `validate_cross_refs()`，见 planning-schemas.md §5.1）：
规则照模块三 `experiment/scripts/contracts.py::validate_cross_references` **镜像**——
契约知识零复制，同事契约演进自动跟随。违反这些 = 模块三 fail-closed 拒收整个请求，
所以必须在模块二内部就抓出来（那里还有 critic 反思循环能改），而不是拖到 stage2/stage3 边界。

历史说明：本文件原先明确"不校验跨产物引用"，把 cross-ref 推给
[check_feasibility_and_downgrade.py](check_feasibility_and_downgrade.py)；而后者又推给 iterate 阶段
兜底，结果整条链路上 method_design ↔ experiment_plan 的一致性**无人校验**。2026-09-09 收口到本文件。

设计依据：规划模块设计方案 §7 A 档 6 个函数（validate_experiment_matrix / validate_data_plan / 等）的契约束校验部分。

示例:
    python scripts/validate_plan.py --type method_design --input path/to/method_design.json
    python scripts/validate_plan.py --type experiment_plan --input path/to/experiment_plan.json
    python scripts/validate_plan.py --type cross_refs --planning-dir path/to/stage2_planning
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.validate_plan")


# ════════════════════════════════════════════════════════════════
# 常量（与 planning-schemas.md §2/§3 对齐）
# ════════════════════════════════════════════════════════════════

# --- MethodDesign（§2.1） ---
_METHOD_DESIGN_REQUIRED: set[str] = {
    "research_goal", "core_mechanism", "framework", "components",
    "technical_route", "innovation_points", "hypothesis_coverage",
    "limitations", "implementable",
}
# algorithm_reference 是 ⬜ 可选

_NOVELTY_DEGREE_ALLOWED: set[str] = {"novel", "adapted", "standard"}

# 嵌套实体的必填字段
_COMPONENT_REQUIRED: set[str] = {"name", "function", "input_schema", "output_schema", "novelty_degree"}
_INNOVATION_POINT_REQUIRED: set[str] = {"claim", "experiment_ref"}
_HYPOTHESIS_COVERAGE_REQUIRED: set[str] = {"hypothesis_id", "mechanism", "experiment_ref"}

# --- EvidenceMetric（§2.1.4，2026-09-09 由自由字符串改为结构化） ---
_EVIDENCE_METRIC_REQUIRED: set[str] = {
    "metric_name", "comparison_mode", "comparator", "threshold", "unit",
}
_COMPARISON_MODE_ALLOWED: set[str] = {"absolute", "delta"}
_COMPARATOR_ALLOWED: set[str] = {">=", ">", "<=", "<"}
_UNIT_ALLOWED: set[str] = {"ratio", "percent", "points", "times", "count"}
# 派生后缀：方向/基准应由 comparison_mode + comparator + vs 表达，不得糊在指标名里。
# 否则 metric_name 无法逐字符命中 experiment_plan.metrics（模块三 cross-ref 硬校验）。
_DERIVED_METRIC_SUFFIXES: tuple[str, ...] = (
    "_delta", "_improvement", "_stability", "_gain", "_drop", "_change", "_diff",
)

# `experiment_ref` 格式：EXP-<HYPOTHESIS_ID>-<SLUG>（见 §2.1.2）。
# 消融实验单独用 EXP-ABL-<COMPONENT>。
_EXPERIMENT_REF_PATTERN = re.compile(r"^EXP-[A-Z0-9]+-[A-Z0-9-]+$")

# --- MethodReview（§2.2） ---
_METHOD_REVIEW_REQUIRED: set[str] = {
    "novelty_ok", "feasibility_risks", "adequacy_score", "feedback", "passed",
}
# overlap_papers / missing_components 是 ⬜ 可选

_ADEQUACY_PASS: float = 7.0
_BLOCKER_PREFIX: str = "blocker:"

# --- ExperimentPlan（§2.3） ---
_EXPERIMENT_PLAN_REQUIRED: set[str] = {
    "objectives", "datasets", "baselines", "metrics", "primary_experiments",
    "experiment_matrix", "ablation_plan", "expected_results",
    "success_criteria", "compute_estimate",
}

_READINESS_ALLOWED: set[str] = {"available", "download", "apply"}
_DATASET_REQUIRED: set[str] = {"name", "source_url", "scale_estimate", "readiness"}
_BASELINE_REQUIRED: set[str] = {"name", "paper_id", "metric_name"}
_ABLATION_REQUIRED: set[str] = {"component", "removed_by"}

# --- DataPlan（§2.4，2026-08-29 v2） ---
# v2 重构：3 个 NLP 专属字段（split_strategy / preprocessing_pipeline / expected_size）降为可选；
# 新增 usage_plan 必填承载「数据怎么用」语义。
_DATA_PLAN_REQUIRED: set[str] = {
    "datasets", "usage_plan",
}

# split_strategy / preprocessing_pipeline / expected_size 是 ⬜ 可选
# 若存在仍校验内容，但不强制
_SPLIT_STRATEGY_REQUIRED_KEYS: set[str] = {"method", "seed"}
_EXPECTED_SIZE_REQUIRED_KEYS: set[str] = {"rows", "disk_gb", "gpu_estimate"}

# --- ExecutionConfig（§2.5） ---
_EXECUTION_CONFIG_REQUIRED: set[str] = {
    "run_dir", "result_dir", "seeds", "max_retries", "dry_run",
}
# timeout_seconds 是 ⬜ 可选（int / null）


# ════════════════════════════════════════════════════════════════
# 错误表示
# ════════════════════════════════════════════════════════════════


class ValidationError:
    """单条校验错误。

    `severity` 2026-09-09 新增：跨产物校验里有一类"前置条件不成立所以结论不可信"的场景
    （见 `validate_cross_refs` 的列语义自检），报成 error 会误伤，完全不报又漏掉信号，
    所以降级为 warning——`_print_report` 分开计数，退出码只看 error。
    """

    def __init__(self, path: str, message: str, severity: str = "error"):
        self.path = path        # 出错字段的定位路径，如 "innovation_points[0].experiment_ref"
        self.message = message  # 人类可读的错误描述
        self.severity = severity  # "error" | "warning"

    @property
    def is_warning(self) -> bool:
        return self.severity == "warning"

    def __str__(self) -> str:
        tag = "WARN " if self.is_warning else ""
        return f"{tag}[{self.path}] {self.message}"

    def __repr__(self) -> str:
        return f"ValidationError({self.path!r}, {self.message!r}, severity={self.severity!r})"


# ════════════════════════════════════════════════════════════════
# 通用校验原语
# ════════════════════════════════════════════════════════════════


def _err(path: str, message: str) -> ValidationError:
    return ValidationError(path, message)


def _warn(path: str, message: str) -> ValidationError:
    return ValidationError(path, message, severity="warning")


def _is_empty(value: Any) -> bool:
    """空值判定：None / 空字符串 / 空列表 / 空字典。"""
    return value is None or value == "" or value == [] or value == {}


def _check_required_keys(payload: dict, required: set[str], base_path: str = "") -> list[ValidationError]:
    """校验 payload 的必填字段是否齐全。"""
    errs: list[ValidationError] = []
    if not isinstance(payload, dict):
        return [_err(base_path or "<root>", f"期望 dict，实际为 {type(payload).__name__}")]
    for key in required:
        if key not in payload or _is_empty(payload[key]):
            errs.append(_err(_join_path(base_path, key), "必填字段缺失或为空"))
    return errs


def _check_in(value: Any, allowed: set[str], path: str) -> ValidationError | None:
    """校验 value 是否在 allowed 集合内（用于枚举值）。"""
    if value not in allowed:
        return _err(path, f"值 {value!r} 不在允许集合 {sorted(allowed)} 内")
    return None


def _check_nonempty_str(value: Any, path: str) -> ValidationError | None:
    """校验 value 是非空字符串。"""
    if not isinstance(value, str):
        return _err(path, f"期望 str，实际为 {type(value).__name__}")
    if not value.strip():
        return _err(path, "字符串为空")
    return None


def _check_nonempty_list(value: Any, path: str) -> ValidationError | None:
    """校验 value 是非空列表。"""
    if not isinstance(value, list):
        return _err(path, f"期望 list，实际为 {type(value).__name__}")
    if not value:
        return _err(path, "列表为空")
    return None


def _check_bool(value: Any, path: str) -> ValidationError | None:
    """校验 value 是 bool。"""
    if not isinstance(value, bool):
        return _err(path, f"期望 bool，实际为 {type(value).__name__}")
    return None


def _check_number_range(value: Any, lo: float, hi: float, path: str) -> ValidationError | None:
    """校验 value 是 int/float 且在 [lo, hi] 范围内。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _err(path, f"期望 number，实际为 {type(value).__name__}")
    if value < lo or value > hi:
        return _err(path, f"值 {value} 超出范围 [{lo}, {hi}]")
    return None


def _check_nonneg_number(value: Any, path: str) -> ValidationError | None:
    """校验 value 是 ≥ 0 的 int/float。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _err(path, f"期望 number，实际为 {type(value).__name__}")
    if value < 0:
        return _err(path, f"值 {value} 不能为负")
    return None


def _check_dict_keys(payload: Any, required_keys: set[str], path: str) -> list[ValidationError]:
    """校验 dict 包含全部必填 key（允许其他额外 key）。"""
    if not isinstance(payload, dict):
        return [_err(path, f"期望 dict，实际为 {type(payload).__name__}")]
    errs: list[ValidationError] = []
    for k in required_keys:
        if k not in payload:
            errs.append(_err(_join_path(path, k), "必填 key 缺失"))
    return errs


def _join_path(base: str, key: str | int) -> str:
    """拼接字段路径。base 为空时直接返回 key。"""
    if not base:
        return str(key)
    return f"{base}.{key}"


def _index_path(base: str, index: int) -> str:
    """拼接列表索引路径。base 为空时直接返回 [index]。"""
    if not base:
        return f"[{index}]"
    return f"{base}[{index}]"


# ════════════════════════════════════════════════════════════════
# 嵌套实体校验
# ════════════════════════════════════════════════════════════════


def _check_component(comp: Any, path: str) -> list[ValidationError]:
    """校验 Component（MethodDesign.components[] 元素）。"""
    if not isinstance(comp, dict):
        return [_err(path, f"期望 dict，实际为 {type(comp).__name__}")]
    errs: list[ValidationError] = []
    for key in ("name", "function", "input_schema", "output_schema"):
        if e := _check_nonempty_str(comp.get(key), _join_path(path, key)):
            errs.append(e)
    if (nd := comp.get("novelty_degree")) is not None:
        if e := _check_in(nd, _NOVELTY_DEGREE_ALLOWED, _join_path(path, "novelty_degree")):
            errs.append(e)
    return errs


def _check_evidence_metric(em: Any, path: str) -> list[ValidationError]:
    """校验单条 EvidenceMetric（§2.1.4，2026-09-09 结构化）。

    结构化的收益：`metric_name` 能逐字符与 `experiment_plan.metrics` 比对（见
    `validate_cross_refs` X12），把原先"指标压根不存在"这类只能靠语义发现的问题
    降级为确定性可查。
    """
    if not isinstance(em, dict):
        return [_err(path, f"期望 dict（结构化判据，见 §2.1.4），实际为 {type(em).__name__}；"
                           f"2026-09-09 起 evidence_metric 不再接受自由字符串")]
    errs: list[ValidationError] = []

    for key in ("metric_name", "comparison_mode", "unit"):
        if e := _check_nonempty_str(em.get(key), _join_path(path, key)):
            errs.append(e)

    # 模型常把阈值写在 target/description 散文里、结构化字段留空（run h 实测：
    # target 拆出的判据 comparator 空/threshold null）。只报"字符串为空"它不知道
    # 去哪改——把原文和操作指引直接拼进给 planner/designer 的报错里。
    raw_target = em.get("target")
    raw_desc = em.get("description") or em.get("metric_description")
    _hint_parts = []
    if isinstance(raw_target, str) and raw_target.strip():
        _hint_parts.append(f"target 原文={raw_target.strip()[:160]!r}")
    if isinstance(raw_desc, str) and raw_desc.strip():
        _hint_parts.append(f"description 原文={raw_desc.strip()[:160]!r}")
    target_hint = (
        "；" + "，".join(_hint_parts)
        + "——请从上面原文提取：数值填 threshold（number）、比较符填 comparator "
          "（>= / <= / > / <），delta 还必须填 vs；不要只留 target 散文"
    ) if _hint_parts else ""

    comparator_value = em.get("comparator")
    if not (isinstance(comparator_value, str) and comparator_value.strip()):
        errs.append(_err(_join_path(path, "comparator"), "字符串为空" + target_hint))

    metric_name = em.get("metric_name")
    if isinstance(metric_name, str) and metric_name.strip():
        lowered = metric_name.strip().casefold()
        for suffix in _DERIVED_METRIC_SUFFIXES:
            if lowered.endswith(suffix):
                errs.append(_err(
                    _join_path(path, "metric_name"),
                    f"不得使用派生后缀 {suffix!r}（实际 {metric_name!r}）；"
                    f"方向与基准请用 comparison_mode / comparator / vs 表达。"
                    f"带后缀的名字无法逐字符命中 experiment_plan.metrics",
                ))
                break

    mode = em.get("comparison_mode")
    if isinstance(mode, str) and mode.strip() and mode not in _COMPARISON_MODE_ALLOWED:
        errs.append(_err(_join_path(path, "comparison_mode"),
                         f"必须是 {sorted(_COMPARISON_MODE_ALLOWED)} 之一，实际 {mode!r}"))

    comparator = em.get("comparator")
    if isinstance(comparator, str) and comparator.strip() and comparator not in _COMPARATOR_ALLOWED:
        errs.append(_err(_join_path(path, "comparator"),
                         f"必须是 {sorted(_COMPARATOR_ALLOWED)} 之一，实际 {comparator!r}"))

    unit = em.get("unit")
    if isinstance(unit, str) and unit.strip() and unit not in _UNIT_ALLOWED:
        errs.append(_err(_join_path(path, "unit"),
                         f"必须是 {sorted(_UNIT_ALLOWED)} 之一，实际 {unit!r}"))

    # threshold 必须是数值（bool 是 int 子类，要排掉）
    threshold = em.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        errs.append(_err(_join_path(path, "threshold"),
                         f"期望数值，实际 {threshold!r}（{type(threshold).__name__}）"
                         + target_hint))

    # delta 模式必须说明"相对谁"，否则阈值无意义
    if mode == "delta" and not (isinstance(em.get("vs"), str) and em["vs"].strip()):
        errs.append(_err(_join_path(path, "vs"),
                         "comparison_mode='delta' 时必填（相对哪个 baseline / 消融组件）"))

    return errs


def _check_evidence_metric_list(
    value: Any,
    path: str,
    *,
    required: bool,
) -> list[ValidationError]:
    """校验 `evidence_metric` 字段（list[EvidenceMetric]，至少 1 条）。"""
    if value is None and not required:
        return []
    if isinstance(value, str):
        return [_err(path, f"期望 list[dict]（结构化判据，见 §2.1.4），实际为自由字符串 {value[:80]!r}；"
                           f"2026-09-09 改造：一条判据一个 dict，多个判据写多条，"
                           f"不要挤进一个字符串（原先挤在一起会被投影层静默丢掉除第一个以外的全部）")]
    if not isinstance(value, list):
        return [_err(path, f"期望 list[dict]，实际为 {type(value).__name__}")]
    if not value:
        return [_err(path, "至少 1 条判据（空列表 = 创新点无法被证伪）")]
    errs: list[ValidationError] = []
    for i, em in enumerate(value):
        errs.extend(_check_evidence_metric(em, f"{path}[{i}]"))
    return errs


def _check_innovation_point(ip: Any, path: str) -> list[ValidationError]:
    """校验 InnovationPoint（MethodDesign.innovation_points[] 元素）。"""
    if not isinstance(ip, dict):
        return [_err(path, f"期望 dict，实际为 {type(ip).__name__}")]
    errs: list[ValidationError] = []
    for key in _INNOVATION_POINT_REQUIRED:
        if e := _check_nonempty_str(ip.get(key), _join_path(path, key)):
            errs.append(e)
    errs.extend(_check_evidence_metric_list(
        ip.get("evidence_metric"), _join_path(path, "evidence_metric"), required=True,
    ))
    errs.extend(_check_experiment_ref_format(
        ip.get("experiment_ref"), _join_path(path, "experiment_ref"),
    ))
    return errs


def _check_experiment_ref_format(value: Any, path: str) -> list[ValidationError]:
    """`experiment_ref` 必须是 `EXP-<HYP>-<SLUG>` 格式（§2.1.2）。

    为什么锁格式：method-designer 跑在 experiment-planner **之前**，写 ref 时实验 id 还不存在。
    锁定格式 + 要求 planner 原样采纳，两边才能对上（否则模块三 `unplanned experiment
    references` 直接拒收整个请求）。snake_case 描述式名字（`exp_end_to_end_evaluation`）
    是历史漂移的根源，明确拒绝。
    """
    if not isinstance(value, str) or not value.strip():
        return []  # 非空性由调用方的 _check_nonempty_str 负责，不重复报
    raw = value.strip()
    if _EXPERIMENT_REF_PATTERN.match(raw):
        return []
    return [_err(path, f"格式必须是 `EXP-<hypothesis_id 大写>-<SLUG>`（如 `EXP-H1-ENDTOEND`），"
                       f"实际 {raw!r}；experiment-planner 会原样采纳此 id，"
                       f"snake_case 描述式命名会导致模块三 unplanned experiment references 拒收")]


def _check_hypothesis_coverage(hc: Any, path: str) -> list[ValidationError]:
    """校验 HypothesisCoverage（MethodDesign.hypothesis_coverage[] 元素）。"""
    if not isinstance(hc, dict):
        return [_err(path, f"期望 dict，实际为 {type(hc).__name__}")]
    errs: list[ValidationError] = []
    for key in _HYPOTHESIS_COVERAGE_REQUIRED:
        if e := _check_nonempty_str(hc.get(key), _join_path(path, key)):
            errs.append(e)
    # evidence_metric 在这里是 ⬜ 可选（模块三 HypothesisCoverage 无此字段，投影层会裁掉；
    # 保留供模块四 Writer 与 critic 使用）
    errs.extend(_check_evidence_metric_list(
        hc.get("evidence_metric"), _join_path(path, "evidence_metric"), required=False,
    ))
    errs.extend(_check_experiment_ref_format(
        hc.get("experiment_ref"), _join_path(path, "experiment_ref"),
    ))
    return errs


def _check_dataset_spec(ds: Any, path: str, required_readiness: bool = True) -> list[ValidationError]:
    """校验 DatasetSpec（ExperimentPlan/DataPlan.datasets[] 元素）。"""
    if not isinstance(ds, dict):
        return [_err(path, f"期望 dict，实际为 {type(ds).__name__}")]
    errs: list[ValidationError] = []
    for key in _DATASET_REQUIRED:
        if key == "readiness" and not required_readiness:
            continue
        if e := _check_nonempty_str(ds.get(key), _join_path(path, key)):
            if key == "scale_estimate":
                # planner 两轮不补时无法凭空造规模；paper-gen 投影层会塞
                # _SCALE_PLACEHOLDER 过模块三契约，降 warning（能从自造键 size
                # 搬的已在 _normalize_dataset_fields 里确定性搬掉）。
                errs.append(_warn(
                    _join_path(path, key),
                    f"{e.message}（投影层将以占位符补齐模块三契约）",
                ))
            else:
                errs.append(e)
    return errs


def _check_baseline_spec(bl: Any, path: str) -> list[ValidationError]:
    """校验 BaselineSpec（ExperimentPlan.baselines[] 元素）。"""
    if not isinstance(bl, dict):
        return [_err(path, f"期望 dict，实际为 {type(bl).__name__}")]
    errs: list[ValidationError] = []
    for key in _BASELINE_REQUIRED:
        if e := _check_nonempty_str(bl.get(key), _join_path(path, key)):
            errs.append(e)
    return errs


def _check_ablation(ab: Any, path: str) -> list[ValidationError]:
    """校验 Ablation（ExperimentPlan.ablation_plan[] 元素）。"""
    if not isinstance(ab, dict):
        return [_err(path, f"期望 dict，实际为 {type(ab).__name__}")]
    errs: list[ValidationError] = []
    for key in _ABLATION_REQUIRED:
        if e := _check_nonempty_str(ab.get(key), _join_path(path, key)):
            errs.append(e)
    return errs


# ════════════════════════════════════════════════════════════════
# 4 个主校验函数（public，阶段三可 import）
# ════════════════════════════════════════════════════════════════


def validate_method_design(payload: Any) -> list[ValidationError]:
    """校验 MethodDesign 全部字段契约（planning-schemas.md §2.1）。"""
    errs: list[ValidationError] = _check_required_keys(payload, _METHOD_DESIGN_REQUIRED)
    if not isinstance(payload, dict):
        return errs

    # implementable 是 bool
    if "implementable" in payload:
        if e := _check_bool(payload.get("implementable"), "implementable"):
            errs.append(e)

    # components[]
    components = payload.get("components", [])
    if isinstance(components, list):
        for i, comp in enumerate(components):
            errs.extend(_check_component(comp, _index_path("components", i)))

    # innovation_points[]
    ip_list = payload.get("innovation_points", [])
    if isinstance(ip_list, list):
        for i, ip in enumerate(ip_list):
            errs.extend(_check_innovation_point(ip, _index_path("innovation_points", i)))

    # hypothesis_coverage[]
    hc_list = payload.get("hypothesis_coverage", [])
    if isinstance(hc_list, list):
        for i, hc in enumerate(hc_list):
            errs.extend(_check_hypothesis_coverage(hc, _index_path("hypothesis_coverage", i)))

    return errs


def validate_method_review(payload: Any) -> list[ValidationError]:
    """校验 MethodReview 全部字段契约（planning-schemas.md §2.2）。

    注：方法评审的"passed=true ⇒ score≥7 且无 blocker 风险"是**内容性**判断
    （评分与评语是否自洽），由 method-critic 的 Self-Reflection Checklist 自查，
    不在代码层强制——LLM 自己说 passed 它就要自己保证评分与评语一致。
    """
    errs: list[ValidationError] = _check_required_keys(payload, _METHOD_REVIEW_REQUIRED)
    if not isinstance(payload, dict):
        return errs

    # bool
    for bool_key in ("novelty_ok", "passed"):
        if bool_key in payload:
            if e := _check_bool(payload.get(bool_key), bool_key):
                errs.append(e)

    # adequacy_score: number, 0-10
    if "adequacy_score" in payload:
        if e := _check_number_range(payload.get("adequacy_score"), 0.0, 10.0, "adequacy_score"):
            errs.append(e)

    # feedback: 非空字符串
    if e := _check_nonempty_str(payload.get("feedback"), "feedback"):
        errs.append(e)

    # feasibility_risks: list[str]，每条非空
    fr = payload.get("feasibility_risks")
    if isinstance(fr, list):
        for i, item in enumerate(fr):
            if not isinstance(item, str) or not item.strip():
                errs.append(_err(_index_path("feasibility_risks", i), "期望非空字符串"))

    return errs


def validate_experiment_plan(payload: Any) -> list[ValidationError]:
    """校验 ExperimentPlan 全部字段契约（planning-schemas.md §2.3）。"""
    errs: list[ValidationError] = _check_required_keys(payload, _EXPERIMENT_PLAN_REQUIRED)
    if not isinstance(payload, dict):
        return errs

    # 列表非空字段（必填同时要求非空）
    for list_key in (
        "objectives", "datasets", "baselines", "metrics", "primary_experiments",
        "experiment_matrix", "ablation_plan", "expected_results", "success_criteria",
    ):
        if list_key in payload:
            if e := _check_nonempty_list(payload.get(list_key), list_key):
                errs.append(e)

    # compute_estimate: number ≥ 0
    if "compute_estimate" in payload:
        if e := _check_nonneg_number(payload.get("compute_estimate"), "compute_estimate"):
            errs.append(e)

    # datasets[].readiness 枚举
    datasets = payload.get("datasets", [])
    if isinstance(datasets, list):
        for i, ds in enumerate(datasets):
            errs.extend(_check_dataset_spec(ds, _index_path("datasets", i)))
            if isinstance(ds, dict):
                readiness = ds.get("readiness")
                if readiness is not None:
                    if e := _check_in(readiness, _READINESS_ALLOWED, _index_path("datasets", i) + ".readiness"):
                        errs.append(e)

    # baselines[]
    baselines = payload.get("baselines", [])
    if isinstance(baselines, list):
        for i, bl in enumerate(baselines):
            errs.extend(_check_baseline_spec(bl, _index_path("baselines", i)))

    # ablation_plan[]
    ablations = payload.get("ablation_plan", [])
    if isinstance(ablations, list):
        for i, ab in enumerate(ablations):
            errs.extend(_check_ablation(ab, _index_path("ablation_plan", i)))

    # experiment_matrix: 模块三标准行格式（planning-schemas.md §2.3.4，
    # 对齐模块三 contracts.py / experiment-schemas.md §2.2.2 的硬校验）
    errs.extend(_check_experiment_matrix(payload))

    return errs


def _check_experiment_matrix(payload: dict) -> list[ValidationError]:
    """校验 experiment_matrix 符合模块三标准行格式。

    硬规则（模块三违反即 REPLAN，本模块 fail-fast 提前拦截）：
        1. 每行 ≥ 4 列，前 4 列非空字符串
        2. 首列 ∈ primary_experiments 或以 ``EXP-ABL-`` 前缀（消融约定 id）
        3. 第二列与 datasets[].name 逐字符一致
        4. 每个 primary id 至少在首列出现一次
        5. 每个 baseline 至少在第三列起的实现名中出现一次
        6. 行尾 key=value 参数：key 非空且同一行内唯一

    2026-09-09：规则 3/5 是**按列号**的校验，前置依赖"列语义正确"。列整体错位时
    （首列写了数据集名、实现名挤到 row[1]），规则 3 会报"数据集 'BM25' 未声明"
    ——错误正确但归因错误，让人去查数据集而真问题是列排错。故列语义自检不过时
    这两条降级为 warning，只留规则 2 的"首列不对"作为唯一 error，把注意力压到根因上。
    """
    errs: list[ValidationError] = []
    matrix = payload.get("experiment_matrix", [])
    if not isinstance(matrix, list):
        return errs

    dataset_names = {
        ds.get("name")
        for ds in payload.get("datasets", [])
        if isinstance(ds, dict) and isinstance(ds.get("name"), str)
    }
    baseline_names = {
        bl.get("name")
        for bl in payload.get("baselines", [])
        if isinstance(bl, dict) and isinstance(bl.get("name"), str)
    }
    primary_ids = {
        pid for pid in payload.get("primary_experiments", [])
        if isinstance(pid, str)
    }

    first_col = {
        row[0].strip()
        for row in matrix
        if isinstance(row, list) and row and isinstance(row[0], str) and row[0].strip()
    }
    columns_ok = _matrix_columns_trustworthy(payload, first_col)
    # 列语义可信时按列号校验才报 error，否则降级（见 docstring）
    mark = _err if columns_ok else _warn

    matrix_ids: set[str] = set()
    matrix_methods: set[str] = set()
    for i, row in enumerate(matrix):
        row_path = _index_path("experiment_matrix", i)
        if not isinstance(row, list) or not row:
            errs.append(_err(row_path, "期望非空 list"))
            continue
        for j, cell in enumerate(row):
            if not isinstance(cell, str) or not cell.strip():
                errs.append(_err(f"{row_path}[{j}]", "单元格期望非空字符串"))
        if len(row) < 4:
            errs.append(_err(
                row_path,
                "行格式需 ≥4 列: [experiment_id, dataset_name, "
                "method_or_baseline, variant/参数, ...]",
            ))
            continue
        # 规则 2：首列 experiment_id
        exp_id = row[0]
        if isinstance(exp_id, str) and exp_id.strip():
            matrix_ids.add(exp_id)
            if exp_id not in primary_ids and not exp_id.startswith("EXP-ABL-"):
                errs.append(_err(
                    f"{row_path}[0]",
                    f"首列 {exp_id!r} 不在 primary_experiments 内，"
                    "也非 EXP-ABL- 前缀消融 id",
                ))
        # 规则 3：第二列 dataset_name 精确匹配（按列号 → 受列语义自检管辖）
        if isinstance(row[1], str) and row[1].strip() and row[1] not in dataset_names:
            errs.append(mark(
                f"{row_path}[1]",
                f"数据集 {row[1]!r} 未在 datasets[].name 中声明"
                "（禁止未声明别名，须逐字符一致）",
            ))
        # 规则 5/6：实现名 token + key=value 参数
        param_keys: list[str] = []
        for j, token in enumerate(row[2:], start=2):
            if not isinstance(token, str) or not token.strip():
                continue
            if "=" in token:
                param_keys.append(token.split("=", 1)[0].strip())
            else:
                matrix_methods.add(token)
        if any(not k for k in param_keys) or len(param_keys) != len(set(param_keys)):
            errs.append(_err(
                row_path,
                f"key=value 参数 key 重复或为空: {param_keys}",
            ))

    # 规则 4：每个 primary id 至少在首列出现一次
    for missing in sorted(primary_ids - matrix_ids):
        errs.append(_err(
            "experiment_matrix",
            f"主实验 {missing!r} 未出现在矩阵首列（模块三硬校验）",
        ))
    # 规则 5：每个 baseline 至少在实现名中出现一次（按列号 → 受列语义自检管辖）
    for missing in sorted(baseline_names - matrix_methods):
        errs.append(mark(
            "experiment_matrix",
            f"基线 {missing!r} 未出现在矩阵实现名中（模块三硬校验）",
        ))
    return errs


def validate_data_plan(payload: Any) -> list[ValidationError]:
    """校验 DataPlan 全部字段契约（planning-schemas.md §2.4，2026-08-29 v2）。

    v2 重构：
    - 必填：datasets（强校验 source_url）+ usage_plan（新增）
    - 可选：split_strategy / preprocessing_pipeline / expected_size（若存在仍校验内容）
    """
    errs: list[ValidationError] = _check_required_keys(payload, _DATA_PLAN_REQUIRED)
    if not isinstance(payload, dict):
        return errs

    # usage_plan: 必填非空字符串（v2 新增）
    if e := _check_nonempty_str(payload.get("usage_plan"), "usage_plan"):
        errs.append(e)

    # datasets[]: 非空 + 每个含 source_url（强校验规则 #1）
    if e := _check_nonempty_list(payload.get("datasets"), "datasets"):
        errs.append(e)
    datasets = payload.get("datasets", [])
    if isinstance(datasets, list):
        for i, ds in enumerate(datasets):
            errs.extend(_check_dataset_spec(ds, _index_path("datasets", i)))
            if isinstance(ds, dict) and not ds.get("source_url"):
                errs.append(_err(
                    _index_path("datasets", i) + ".source_url",
                    "强校验失败：source_url 不能为空（防虚构数据）",
                ))

    # preprocessing_pipeline[]: 可选（v2）；若存在则校验类型（list[str]）
    if "preprocessing_pipeline" in payload and payload["preprocessing_pipeline"] is not None:
        pp = payload["preprocessing_pipeline"]
        if not isinstance(pp, list):
            errs.append(_err("preprocessing_pipeline", f"期望 list，实际为 {type(pp).__name__}"))
        else:
            for i, step in enumerate(pp):
                if not isinstance(step, str) or not step.strip():
                    errs.append(_err(_index_path("preprocessing_pipeline", i), "期望非空字符串"))

    # split_strategy: 可选（v2）；若存在则校验 method / seed
    if "split_strategy" in payload and payload["split_strategy"] is not None:
        errs.extend(_check_dict_keys(
            payload.get("split_strategy"),
            _SPLIT_STRATEGY_REQUIRED_KEYS,
            "split_strategy",
        ))

    # expected_size: 可选（v2）；若存在则校验 rows / disk_gb / gpu_estimate
    if "expected_size" in payload and payload["expected_size"] is not None:
        errs.extend(_check_dict_keys(
            payload.get("expected_size"),
            _EXPECTED_SIZE_REQUIRED_KEYS,
            "expected_size",
        ))

    return errs


def validate_execution_config(payload: Any) -> list[ValidationError]:
    """校验 ExecutionConfig 全部字段契约（planning-schemas.md §2.5）。

    强校验规则（来自 schemas §2.5）:
        1. run_dir 非空 str
        2. result_dir 非空 str
        3. seeds 非空 list[int]
        4. max_retries 非负 int
        5. dry_run 是 bool
        6. timeout_seconds 若存在则是 int 或 None
        7. result_dir 解析后必须位于 run_dir 内或其子目录（防止交付产物写到 run_dir 外）
    """
    errs: list[ValidationError] = _check_required_keys(payload, _EXECUTION_CONFIG_REQUIRED)
    if not isinstance(payload, dict):
        return errs

    # 1. run_dir
    if "run_dir" in payload:
        if e := _check_nonempty_str(payload.get("run_dir"), "run_dir"):
            errs.append(e)

    # 2. result_dir
    if "result_dir" in payload:
        if e := _check_nonempty_str(payload.get("result_dir"), "result_dir"):
            errs.append(e)

    # 3. seeds: 非空 list[int]
    seeds = payload.get("seeds")
    if "seeds" in payload:
        if e := _check_nonempty_list(seeds, "seeds"):
            errs.append(e)
        elif isinstance(seeds, list):
            for i, s in enumerate(seeds):
                if isinstance(s, bool) or not isinstance(s, int):
                    errs.append(_err(
                        _index_path("seeds", i),
                        f"期望 int，实际为 {type(s).__name__}",
                    ))

    # 4. max_retries: int >= 0
    if "max_retries" in payload:
        if e := _check_nonneg_number(payload.get("max_retries"), "max_retries"):
            errs.append(e)
        # 顺便校验是 int（不接 float）
        mr = payload.get("max_retries")
        if mr is not None and isinstance(mr, bool):
            errs.append(_err("max_retries", "期望 int，实际为 bool"))
        elif mr is not None and not isinstance(mr, int):
            errs.append(_err("max_retries", f"期望 int，实际为 {type(mr).__name__}"))

    # 5. dry_run: bool
    if "dry_run" in payload:
        if e := _check_bool(payload.get("dry_run"), "dry_run"):
            errs.append(e)

    # 6. timeout_seconds: int 或 None（不接 float）
    if "timeout_seconds" in payload:
        ts = payload.get("timeout_seconds")
        if ts is not None:
            if isinstance(ts, bool) or not isinstance(ts, int):
                errs.append(_err(
                    "timeout_seconds",
                    f"期望 int 或 null，实际为 {type(ts).__name__}",
                ))

    # 7. result_dir 必须位于 run_dir 内或其子目录
    run_dir = payload.get("run_dir")
    result_dir = payload.get("result_dir")
    if (isinstance(run_dir, str) and run_dir.strip()
            and isinstance(result_dir, str) and result_dir.strip()):
        from pathlib import Path
        try:
            run_abs = Path(run_dir).resolve()
            # result_dir 若为绝对路径直接用；相对则拼到 run_dir 下
            result_path = Path(result_dir)
            if not result_path.is_absolute():
                result_path = run_abs / result_path
            result_abs = result_path.resolve()
            # 校验：result_abs 是 run_abs 自身或其子路径
            try:
                result_abs.relative_to(run_abs)
            except ValueError:
                errs.append(_err(
                    "result_dir",
                    f"强校验失败：result_dir={result_dir!r} 解析后不在 run_dir={run_dir!r} 内",
                ))
        except (OSError, RuntimeError) as e:
            # 路径解析异常（如含 NUL 等）——当作字段问题报
            errs.append(_err("result_dir", f"路径解析失败: {e}"))

    return errs


# ════════════════════════════════════════════════════════════════
# 跨产物交叉引用校验（planning-schemas.md §5.1，X1–X12）
# ════════════════════════════════════════════════════════════════
#
# 为什么要在模块二做：模块三 `contracts.py::validate_cross_references` 是 fail-closed
# ——违反任一条它 raise ValueError 拒收**整个请求**，不会"猜该听谁的"。在模块二内部抓出来，
# 反思循环还能改；拖到 stage2/stage3 边界才炸，就只能整条重跑。
#
# 根因是流水线顺序：method-designer 跑在 experiment-planner **之前**，写 experiment_ref
# 时那批实验 id 还不存在。治本靠 prompt 侧的"后跑者原样采纳先跑者标识符"（见
# experiment-planner.md 的采纳义务），本函数是兜底探测。


_ARXIV_ID_PATTERN = re.compile(r"\b\d{4}\.\d{4,5}\b")
_DATASET_ALIAS_PATTERN = re.compile(r"\bDS\d+\b")
_SEED_COUNT_PATTERN = re.compile(r"(\d+)\s*(?:个)?\s*(?:random\s+)?seeds?\b", re.IGNORECASE)

# X10 比对数据集时用的字段（与模块三 DatasetSpec 对齐）
_DATASET_IDENTITY_KEYS: tuple[str, ...] = ("name", "source_url", "scale_estimate", "readiness")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _str_set(values: Any) -> set[str]:
    """从任意可迭代里提出非空字符串集合（去首尾空白）。"""
    return {v.strip() for v in _as_list(values) if isinstance(v, str) and v.strip()}


def _field_set(items: Any, key: str) -> set[str]:
    """从 list[dict] 里提出某字段的非空字符串集合。"""
    out: set[str] = set()
    for item in _as_list(items):
        if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip():
            out.add(item[key].strip())
    return out


def _metric_vocabulary(experiment_plan: dict) -> set[str]:
    """`metrics[]` 的合法指标名集合。

    容忍两种形状：list[str]（schema 规定）与 list[dict]（LLM 偶发漂移，取 name/metric_name）。
    形状本身由 validate_experiment_plan 管，这里只求别因形状漂移把 X12 变成一片假阳性。
    """
    vocab: set[str] = set()
    for m in _as_list(experiment_plan.get("metrics")):
        if isinstance(m, str) and m.strip():
            vocab.add(m.strip())
        elif isinstance(m, dict):
            for k in ("name", "metric_name"):
                if isinstance(m.get(k), str) and m[k].strip():
                    vocab.add(m[k].strip())
                    break
    return vocab


def _matrix_ids_and_methods(experiment_plan: dict) -> tuple[set[str], set[str], list[tuple[int, dict]]]:
    """扫一遍矩阵，返回 (首列 id 集合, 实现名 token 集合, [(行号, key=value 参数 dict)])。"""
    ids: set[str] = set()
    methods: set[str] = set()
    params: list[tuple[int, dict]] = []
    for i, row in enumerate(_as_list(experiment_plan.get("experiment_matrix"))):
        if not isinstance(row, list) or not row:
            continue
        if isinstance(row[0], str) and row[0].strip():
            ids.add(row[0].strip())
        row_params: dict[str, str] = {}
        for token in row[2:]:
            if not isinstance(token, str) or not token.strip():
                continue
            if "=" in token:
                k, v = token.split("=", 1)
                row_params[k.strip()] = v.strip()
            else:
                methods.add(token.strip())
        params.append((i, row_params))
    return ids, methods, params


def _matrix_columns_trustworthy(experiment_plan: dict, matrix_ids: set[str]) -> bool:
    """列语义前置自检（planning-schemas.md §5.1 前置自检）。

    按列号做校验（X5 实现名在 row[2:]、X7 ablation 参数、X11 数据集在 row[1]）之前，
    必须先确认首列真的是 experiment_id——能在 `primary_experiments ∪ EXP-ABL-*` 命中。

    为什么：列语义整体错位时（首列写了数据集别名、实现名挤到 row[1]），按列号校验会报出
    **"错误正确但归因错误"**的信息——比如"数据集 'BM25' 未声明"，让人去查数据集，
    而真问题是列排错了。这种误导比不报错更难排查，所以整块降级为 warning。
    """
    if not matrix_ids:
        return False
    primary = _str_set(experiment_plan.get("primary_experiments"))
    recognized = {mid for mid in matrix_ids if mid in primary or mid.startswith("EXP-ABL-")}
    # 过半首列能认出来才认为列语义正确（容忍个别 id 拼错，不容忍整体错位）
    return len(recognized) * 2 > len(matrix_ids)


def _iter_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """递归收集 (路径, 字符串) 对，用于 X9 正文引文扫描。"""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((path or "<root>", value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_iter_strings(v, _join_path(path, k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_iter_strings(v, _index_path(path, i)))
    return out


def _all_evidence_metrics(method_design: dict) -> list[tuple[str, dict]]:
    """收集 (路径, EvidenceMetric dict)，覆盖 innovation_points 与 hypothesis_coverage。"""
    out: list[tuple[str, dict]] = []
    for field in ("innovation_points", "hypothesis_coverage"):
        for i, item in enumerate(_as_list(method_design.get(field))):
            if not isinstance(item, dict):
                continue
            base = f"{_index_path(field, i)}.evidence_metric"
            for j, em in enumerate(_as_list(item.get("evidence_metric"))):
                if isinstance(em, dict):
                    out.append((f"{base}[{j}]", em))
    return out


def validate_cross_refs(
    method_design: Any,
    experiment_plan: Any,
    data_plan: Any = None,
    *,
    execution_config: Any = None,
    hypotheses: Any = None,
    key_papers: Any = None,
) -> list[ValidationError]:
    """校验 method_design ↔ experiment_plan ↔ data_plan 的交叉引用（§5.1 X1–X12）。

    规则照模块三 `experiment/scripts/contracts.py::validate_cross_references` 镜像。

    Args:
        method_design: MethodDesign dict。
        experiment_plan: ExperimentPlan dict。
        data_plan: DataPlan dict；None 时跳过 X10 的数据集一致性。
        execution_config: ExecutionConfig dict；None 时跳过 X11 的 seed 计数。
        hypotheses: 上游 `ResearchQuestion.hypotheses`（list[dict]，含 `id`）；
            None 时 X4 退化为"coverage 与 objectives 两方相等"。
        key_papers: 上游 `key_papers`（list[dict]，含 `id`）；
            None 时跳过 X8/X9——没有上游白名单时判"编造引文"必然假阳性。

    Returns:
        ValidationError 列表（可能含 severity="warning" 的降级项）。空列表表示通过。
    """
    md = _as_dict(method_design)
    ep = _as_dict(experiment_plan)
    dp = _as_dict(data_plan)
    errs: list[ValidationError] = []

    matrix_ids, matrix_methods, matrix_params = _matrix_ids_and_methods(ep)
    columns_ok = _matrix_columns_trustworthy(ep, matrix_ids)
    if matrix_ids and not columns_ok:
        errs.append(_warn(
            "experiment_matrix",
            "首列多数不在 primary_experiments 内也非 EXP-ABL- 前缀，判定列语义错位；"
            "X5/X7/X11 这类按列号的校验已整块降级为 warning（避免报出"
            "归因错误的误导信息）。请先修正矩阵列顺序再看下面的 warning",
        ))
    mark = _warn if not columns_ok else _err

    # ---- X1 / X2：experiment_ref 必须是已规划的实验 ----
    # 镜像 contracts.py: planned_ids = set(primary_experiments) | 矩阵首列 id
    planned_ids = _str_set(ep.get("primary_experiments")) | matrix_ids
    for field in ("innovation_points", "hypothesis_coverage"):
        for i, item in enumerate(_as_list(md.get(field))):
            ref = _as_dict(item).get("experiment_ref")
            if not isinstance(ref, str) or not ref.strip():
                continue
            if ref.strip() not in planned_ids:
                errs.append(_err(
                    f"{_index_path(field, i)}.experiment_ref",
                    f"{ref!r} 不在已规划实验内（primary_experiments ∪ 矩阵首列）；"
                    f"模块三会以 `unplanned experiment references` 拒收整个请求。"
                    f"修法：experiment-planner 原样采纳该 id 写进 primary_experiments，"
                    f"而不是另起一个描述式名字。已规划: {sorted(planned_ids)}",
                ))

    # ---- X3：按 hypothesis_id join，coverage 的 ref 必须落在该假设的 objectives 里 ----
    objectives = _as_list(ep.get("objectives"))
    obj_by_hyp: dict[str, set[str]] = {}
    for obj in objectives:
        o = _as_dict(obj)
        hid = o.get("hypothesis_id")
        if isinstance(hid, str) and hid.strip():
            obj_by_hyp.setdefault(hid.strip(), set()).update(_str_set(o.get("experiment_ids")))
    for i, item in enumerate(_as_list(md.get("hypothesis_coverage"))):
        hc = _as_dict(item)
        hid = hc.get("hypothesis_id")
        ref = hc.get("experiment_ref")
        if not (isinstance(hid, str) and hid.strip() and isinstance(ref, str) and ref.strip()):
            continue
        bound = obj_by_hyp.get(hid.strip())
        if bound is None or not bound:
            continue  # 该假设无 objectives 绑定，由 X4 报"假设未承接"
        if ref.strip() not in bound:
            errs.append(_err(
                f"{_index_path('hypothesis_coverage', i)}.experiment_ref",
                f"假设 {hid!r} 的验证实验 {ref!r} 不在 "
                f"objectives[hypothesis_id={hid!r}].experiment_ids={sorted(bound)} 内"
                f"——同一个假设，方法侧和实验侧指向了不同实验",
            ))

    # ---- X4：hypothesis_id 三方集合相等 ----
    cov_ids = _field_set(md.get("hypothesis_coverage"), "hypothesis_id")
    obj_ids = set(obj_by_hyp)
    if cov_ids or obj_ids:
        if missing := cov_ids - obj_ids:
            errs.append(_err("objectives", f"以下假设有方法侧覆盖但无实验目标承接: {sorted(missing)}"))
        if extra := obj_ids - cov_ids:
            errs.append(_err("hypothesis_coverage", f"以下假设有实验目标但无方法侧覆盖: {sorted(extra)}"))
    if hypotheses is not None:
        up_ids = _field_set(hypotheses, "id")
        if up_ids:
            if uncovered := up_ids - cov_ids:
                errs.append(_err("hypothesis_coverage",
                                 f"上游假设未被方法设计承接: {sorted(uncovered)}（§5.3 要求全覆盖）"))
            if invented := cov_ids - up_ids:
                errs.append(_err("hypothesis_coverage",
                                 f"出现上游不存在的 hypothesis_id: {sorted(invented)}"))

    # ---- X5：framework.name 必须出现在矩阵实现名里（否则"本方法"根本没被跑） ----
    # 论文命名惯例是 "Full Name (ABBR)"，矩阵里天然只写缩写——2026-09-10 run g：
    # framework.name='...Memory Compression with Temporal Preservation (SHiC-TP)'、
    # 矩阵 9 行写 'SHiC-TP'，本方法明明被安排跑了，旧检查仍误报并把 planner 拖进
    # 两轮无谓反思。所以全名、括号缩写，任一命中即可。
    framework = md.get("framework")
    fw_name = framework.get("name") if isinstance(framework, dict) else framework
    if isinstance(fw_name, str) and fw_name.strip() and matrix_methods:
        fw_aliases = {fw_name.strip()}
        fw_aliases.update({
            token.strip()
            for token in re.findall(r"[（(]([^（）()]{1,40})[)）]", fw_name)
            if token.strip()
        })
        if not (fw_aliases & matrix_methods):
            errs.append(mark(
                "experiment_matrix",
                f"framework.name={fw_name.strip()!r}（含括号缩写 {sorted(fw_aliases - {fw_name.strip()})}）"
                f"未出现在矩阵实现名中 {sorted(matrix_methods)}"
                f"——矩阵里只有基线，本方法没被安排跑",
            ))

    # ---- X6：ablation_plan[].component ⊆ components[].name ----
    comp_names = _field_set(md.get("components"), "name")
    for i, ab in enumerate(_as_list(ep.get("ablation_plan"))):
        comp = _as_dict(ab).get("component")
        if not (isinstance(comp, str) and comp.strip()) or not comp_names:
            continue
        if comp.strip() not in comp_names:
            # 模块三 Ablation.component 只要非空 str，无名称一致性硬规则；
            # snake_case slug vs 全名（run j：'temporal_expression_preservation'
            # vs 'Temporal Expression Preservation Module'）两轮反思收敛不了，
            # 也不挡 stage3——降 warning，语义质量留给 S 类 critic。
            errs.append(_warn(
                f"{_index_path('ablation_plan', i)}.component",
                f"{comp!r} 不是 method_design.components[].name 中的任何一个 "
                f"{sorted(comp_names)}——消融组件名与方法组件名不一致（不挡模块三）",
            ))

    # ---- X7：矩阵消融行的 ablation=<component> 参数 ----
    matrix_rows = _as_list(ep.get("experiment_matrix"))
    for row_idx, params in matrix_params:
        row = matrix_rows[row_idx] if row_idx < len(matrix_rows) else None
        exp_id = row[0].strip() if isinstance(row, list) and row and isinstance(row[0], str) else ""
        if not exp_id.startswith("EXP-ABL-"):
            continue
        ablated = params.get("ablation")
        if not ablated:
            errs.append(mark(
                f"{_index_path('experiment_matrix', row_idx)}",
                f"消融行 {exp_id!r} 缺 `ablation=<component>` 参数——无法确定拆的是哪个组件",
            ))
        elif comp_names and ablated not in comp_names:
            errs.append(mark(
                f"{_index_path('experiment_matrix', row_idx)}",
                f"`ablation={ablated}` 的值不在 components[].name {sorted(comp_names)} 内",
            ))

    # ---- X8：paper_id 白名单（防编造文献） ----
    baseline_papers = _field_set(ep.get("baselines"), "paper_id")
    algo_papers = _field_set(md.get("algorithm_reference"), "paper_id")
    if key_papers is not None:
        upstream_papers = _field_set(key_papers, "id")
        if upstream_papers:
            if fake := baseline_papers - upstream_papers:
                errs.append(_err("baselines", f"paper_id 不在上游 key_papers 内: {sorted(fake)}"))
            if fake := algo_papers - upstream_papers - baseline_papers:
                errs.append(_err("algorithm_reference",
                                 f"paper_id 不在上游 key_papers 也不在 baselines 内: {sorted(fake)}"))

    # ---- X9：正文内联 arXiv 引文 ⊆ 已声明论文 id ----
    if key_papers is not None:
        declared = _field_set(key_papers, "id") | baseline_papers | algo_papers
        if declared:
            for path, text in _iter_strings(md):
                for cited in _ARXIV_ID_PATTERN.findall(text):
                    if cited not in declared:
                        errs.append(_err(path, f"正文引用了未声明的论文 id {cited!r}（疑似编造引文）"))

    # ---- X10：experiment_plan.datasets == data_plan.datasets + 算力口径 ----
    if dp:
        def _identity(specs: Any) -> set[tuple]:
            out: set[tuple] = set()
            for ds in _as_list(specs):
                d = _as_dict(ds)
                out.add(tuple(
                    d[k].strip() if isinstance(d.get(k), str) else d.get(k)
                    for k in _DATASET_IDENTITY_KEYS
                ))
            return out

        ep_ds, dp_ds = _identity(ep.get("datasets")), _identity(dp.get("datasets"))
        if ep_ds != dp_ds:
            only_ep = sorted(str(t) for t in ep_ds - dp_ds)
            only_dp = sorted(str(t) for t in dp_ds - ep_ds)
            errs.append(_err(
                "data_plan.datasets",
                f"与 experiment_plan.datasets 不一致（模块三逐字段比对后拒收）。"
                f"仅出现在 experiment_plan: {only_ep}；仅出现在 data_plan: {only_dp}。"
                f"DataPlan 应由 ExperimentPlan 确定性抽取，不该二次改写",
            ))

        expected_size = _as_dict(dp.get("expected_size"))
        gpu_est = expected_size.get("gpu_estimate")
        compute_est = ep.get("compute_estimate")
        if (isinstance(gpu_est, (int, float)) and not isinstance(gpu_est, bool)
                and isinstance(compute_est, (int, float)) and not isinstance(compute_est, bool)
                and float(gpu_est) != float(compute_est)):
            errs.append(_err(
                "data_plan.expected_size.gpu_estimate",
                f"{gpu_est} != experiment_plan.compute_estimate {compute_est}"
                f"——同一笔算力两处声明且不一致",
            ))
        rows = expected_size.get("rows")
        # v2：expected_size 整体可选（见 _DATA_PLAN_REQUIRED 注释）；rows=0 只是
        # 占位、信息量低，不再是契约违反——投影层也不依赖它。降 warning 保留可见性。
        if isinstance(rows, (int, float)) and not isinstance(rows, bool):
            has_datasets = bool(_as_list(dp.get("datasets")))
            if has_datasets and rows <= 0:
                errs.append(_warn("data_plan.expected_size.rows",
                                  f"声明了 {len(_as_list(dp.get('datasets')))} 个数据集但 rows={rows}"))
            if not has_datasets and rows > 0:
                errs.append(_warn("data_plan.expected_size.rows",
                                  f"rows={rows} 但 datasets 为空"))

    # ---- X11：数据集别名禁令 + seed 计数 ----
    # 按别名聚合（不是按出现位置逐条报）：同一个 DS1 在矩阵里出现 17 次是**一个**问题，
    # 报 17 条只会挤爆反思 prompt 的预算，让真正不同的问题被淹掉。
    dataset_names = _field_set(ep.get("datasets"), "name")
    alias_hits: dict[str, list[str]] = {}
    alias_scan: list[tuple[str, str]] = []
    for i, sc in enumerate(_as_list(ep.get("success_criteria"))):
        if isinstance(sc, str):
            alias_scan.append((_index_path("success_criteria", i), sc))
    for row_idx, row in enumerate(matrix_rows):
        for j, cell in enumerate(_as_list(row)):
            if isinstance(cell, str):
                alias_scan.append((f"{_index_path('experiment_matrix', row_idx)}[{j}]", cell))
    for path, text in alias_scan:
        for alias in set(_DATASET_ALIAS_PATTERN.findall(text)):
            if alias not in dataset_names:
                alias_hits.setdefault(alias, []).append(path)
    for alias, paths in sorted(alias_hits.items()):
        shown = paths[:5]
        more = f" 等 {len(paths)} 处" if len(paths) > len(shown) else ""
        errs.append(_err(
            "experiment_plan",
            f"出现数据集别名 {alias!r}（{', '.join(shown)}{more}）"
            f"——必须逐字符写 datasets[].name {sorted(dataset_names)}"
            f"（模块三按名字精确匹配，别名会被当成未声明数据集）",
        ))

    if execution_config is not None:
        seeds = _as_list(_as_dict(execution_config).get("seeds"))
        declared_seed_count = len(seeds)
        notes = ep.get("notes")
        note_texts = [notes] if isinstance(notes, str) else [
            n for n in _as_list(notes) if isinstance(n, str)
        ]
        for text in note_texts:
            for raw in _SEED_COUNT_PATTERN.findall(text):
                claimed = int(raw)
                if declared_seed_count and claimed != declared_seed_count:
                    errs.append(_err(
                        "experiment_plan.notes",
                        f"声明跑 {claimed} 个 seed，但 ExecutionConfig.seeds 只有 "
                        f"{declared_seed_count} 个 {seeds}——方差声明与实际执行不符",
                    ))

    # ---- X12：所有 metric_name 逐字符 ∈ metrics[] ----
    vocab = _metric_vocabulary(ep)
    if vocab:
        for path, em in _all_evidence_metrics(md):
            name = em.get("metric_name")
            if isinstance(name, str) and name.strip() and name.strip() not in vocab:
                errs.append(_err(
                    _join_path(path, "metric_name"),
                    f"{name.strip()!r} 不在 experiment_plan.metrics {sorted(vocab)} 内；"
                    f"模块三会以 `innovation metrics not in plan` 拒收。修法："
                    f"experiment-planner 把方法侧用到的指标全部采纳进 metrics[]，"
                    f"或方法侧改用已有指标——不要造派生名",
                ))
        # baselines[].metric_name 同属指标词表，漂了同样会让"跟基线比"无从对齐
        for i, bl in enumerate(_as_list(ep.get("baselines"))):
            name = _as_dict(bl).get("metric_name")
            if isinstance(name, str) and name.strip() and name.strip() not in vocab:
                errs.append(_err(
                    f"{_index_path('baselines', i)}.metric_name",
                    f"{name.strip()!r} 不在 metrics {sorted(vocab)} 内"
                    f"——基线报的指标不在评测指标里，无法与本方法对比",
                ))

    # ---- 附加：方法依赖验证集但 data_plan 没有切分方案（§5.3） ----
    if dp and not _as_dict(dp.get("split_strategy")):
        needle_paths = [
            p for p, text in _iter_strings(md.get("technical_route"))
            if re.search(r"validation|dev(?:elopment)?\s+set|验证集|开发集", text, re.IGNORECASE)
        ]
        needle_paths += [
            p for p, text in _iter_strings(md.get("components"))
            if re.search(r"validation|dev(?:elopment)?\s+set|验证集|开发集", text, re.IGNORECASE)
        ]
        if needle_paths:
            # v2 起 split_strategy 可选：切分方式与 seed 由投影层按 ExecutionConfig.seeds
            # 确定性合成（predefined_splits+seed），缺它不阻塞模块三。降 warning 保留可见性。
            errs.append(_warn(
                "data_plan.split_strategy",
                f"方法依赖验证集（见 method_design 的 {needle_paths[:3]}）"
                f"但 data_plan.split_strategy 缺失——v2 可选字段，投影层会合成 seed，"
                f"warning 仅提示产物富字段不足",
            ))

    return errs


# ════════════════════════════════════════════════════════════════
# Dispatch 入口
# ════════════════════════════════════════════════════════════════


_VALIDATORS = {
    "method_design": validate_method_design,
    "method_review": validate_method_review,
    "experiment_plan": validate_experiment_plan,
    "data_plan": validate_data_plan,
    "execution_config": validate_execution_config,
}


def validate_type(schema_type: str, payload: Any) -> list[ValidationError]:
    """按 schema_type 分发到对应校验器。

    Args:
        schema_type: 产物类型名（"method_design" / "method_review" / "experiment_plan" / "data_plan"）。
        payload: 产物 dict。

    Returns:
        ValidationError 列表。空列表表示通过。

    Raises:
        ValueError: schema_type 不在 4 个合法值内。
    """
    if schema_type not in _VALIDATORS:
        raise ValueError(
            f"未知 schema_type: {schema_type!r}，"
            f"合法值: {sorted(_VALIDATORS)}"
        )
    return _VALIDATORS[schema_type](payload)


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def _load_input(path: str) -> Any:
    """加载输入文件（仅支持 .json）。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _print_report(errors: list[ValidationError]) -> None:
    """按稳定格式输出校验报告（warning 与 error 分开计数）。"""
    hard = [e for e in errors if not e.is_warning]
    soft = [e for e in errors if e.is_warning]
    if not errors:
        log.info("VALIDATION OK: 契约全部满足。")
        return
    if hard:
        log.error(f"VALIDATION FAILED: {len(hard)} 处违反契约"
                  + (f"（另有 {len(soft)} 条 warning）" if soft else ""))
    else:
        log.warning(f"VALIDATION OK（带 {len(soft)} 条 warning）")
    for e in hard:
        log.error(f"  - {e}")
    for e in soft:
        log.warning(f"  - {e}")


def _load_planning_dir(planning_dir: str) -> dict[str, Any]:
    """从 stage2 产物目录读取跨产物校验所需的 4 个 json（缺的返回 None）。"""
    from pathlib import Path

    base = Path(planning_dir)
    out: dict[str, Any] = {}
    for key, filename in (
        ("method_design", "method_design.json"),
        ("experiment_plan", "experiment_plan.json"),
        ("data_plan", "data_plan.json"),
        ("execution_config", "execution_config.json"),
    ):
        path = base / filename
        out[key] = _load_input(str(path)) if path.is_file() else None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规划模块输出契约束校验")
    parser.add_argument(
        "--type",
        required=True,
        choices=[
            "method_design", "method_review", "experiment_plan", "data_plan",
            "execution_config", "cross_refs",
        ],
        help="待校验的产物类型；cross_refs 走跨产物交叉引用校验（§5.1）",
    )
    parser.add_argument("--input", help="产物文件路径（.json）；--type cross_refs 时不用")
    parser.add_argument(
        "--planning-dir",
        help="stage2 产物目录（含 method_design.json / experiment_plan.json / "
             "data_plan.json / execution_config.json）；仅 --type cross_refs 用",
    )

    args = parser.parse_args(argv)

    if args.type == "cross_refs":
        if not args.planning_dir:
            parser.error("--type cross_refs 需要 --planning-dir")
        loaded = _load_planning_dir(args.planning_dir)
        if loaded["method_design"] is None or loaded["experiment_plan"] is None:
            parser.error(
                f"{args.planning_dir} 下缺 method_design.json 或 experiment_plan.json，"
                f"无法做跨产物校验"
            )
        errors = validate_cross_refs(
            loaded["method_design"],
            loaded["experiment_plan"],
            loaded["data_plan"],
            execution_config=loaded["execution_config"],
        )
    else:
        if not args.input:
            parser.error(f"--type {args.type} 需要 --input")
        payload = _load_input(args.input)
        errors = validate_type(args.type, payload)

    _print_report(errors)
    return 0 if not any(not e.is_warning for e in errors) else 1


if __name__ == "__main__":
    sys.exit(main())
