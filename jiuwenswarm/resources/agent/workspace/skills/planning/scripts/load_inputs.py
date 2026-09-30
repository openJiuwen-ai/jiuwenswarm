# -*- coding: utf-8 -*-
"""
load_inputs.py — 解析模块一全部输入

按 [references/planning-schemas.md §1 输入侧](../references/planning-schemas.md) 解析模块一传过来的 8 个产物：
    1. ResearchQuestion
    2. Hypothesis（hypotheses[]）
    3. GapReport
    4. PaperRef（key_papers[]）
    5. Reference（references[]，新版模块一提供；兼容旧产物时可缺省）
    6. ResourceConstraints
    7. ResearchFrontier（弱格式）
    8. Domain

约定输入目录结构:
    plan_dir/
    ├── research_question.json
    ├── hypotheses.json
    ├── gap_report.json
    ├── key_papers.json
    ├── references.json
    ├── resource_constraints.json
    ├── research_frontier.json
    └── domain.json

每个 _load_xxx 负责解析一个实体的文件路径，校验必填字段，返回 (payload, errors)。
errors 是空列表表示通过；非空表示校验失败/缺文件。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.load_inputs")


# 8 个文件名常量（与上文约定一一对应）
_INPUT_FILES = {
    "research_question":    "research_question.json",
    "hypotheses":           "hypotheses.json",
    "gap_report":           "gap_report.json",
    "key_papers":           "key_papers.json",
    "references":           "references.json",
    "resource_constraints": "resource_constraints.json",
    "research_frontier":    "research_frontier.json",
    "domain":               "domain.json",
}

# PlanningFeedback 合法 category 集合（schemas §1.5.1）
_FEEDBACK_CATEGORIES: set[str] = {"data", "compute", "baseline", "metric", "schema", "method",
                                  "experiment", "budget"}


# ════════════════════════════════════════════════════════════════
# 总入口
# ════════════════════════════════════════════════════════════════


def load_inputs(plan_dir: str) -> tuple[dict, list[str]]:
    """
    加载模块一全部 8 个输入文件，按 planning-schemas.md §1 逐项解析与校验。

    Args:
        plan_dir: 模块一产物目录的路径。

    Returns:
        (payloads, errors):
            payloads: {
                "research_question":     {...},
                "hypotheses":            [...],
                "gap_report":            {...},
                "key_papers":            [...],
                "references":            [...],
                "resource_constraints":  {...},
                "research_frontier":     {...},
                "domain":                {...},
            }
            errors: 错误信息清单（每条形如 "[research_question] 缺必填字段: topic"）。
                    空列表表示全部通过。

    Raises:
        FileNotFoundError: plan_dir 不存在时。
    """
    plan_path = Path(plan_dir)
    if not plan_path.is_dir():
        raise FileNotFoundError(f"plan_dir 不存在: {plan_dir}")

    payloads: dict = {}
    errors: list[str] = []

    for key, filename in _INPUT_FILES.items():
        file_path = plan_path / filename
        if not file_path.is_file():
            # 兼容升级前的模块一缓存；最新版 conception 一定输出该文件。
            # references 是补充证据，关键论文仍由 key_papers 提供最小规划输入。
            if key == "references":
                log.warning("旧版模块一输入缺少 references.json；继续使用 key_papers")
                payloads[key] = []
                continue
            errors.append(f"[{key}] 缺失文件: {file_path}")
            payloads[key] = None if not key.endswith("es") and not key.endswith("rs") else []
            # 列表型 key 缺文件也补空列表，单体型补 None
            if key in ("hypotheses", "key_papers"):
                payloads[key] = []
            else:
                payloads[key] = None
            continue

        if key == "hypotheses":
            payload, errs = _load_hypotheses(str(file_path))
        elif key == "key_papers":
            payload, errs = _load_key_papers(str(file_path))
        elif key == "references":
            payload, errs = _load_references(str(file_path))
        elif key == "research_question":
            payload, errs = _load_research_question(str(file_path))
        elif key == "gap_report":
            payload, errs = _load_gap_report(str(file_path))
        elif key == "resource_constraints":
            payload, errs = _load_resource_constraints(str(file_path))
        elif key == "research_frontier":
            payload, errs = _load_research_frontier(str(file_path))
        elif key == "domain":
            payload, errs = _load_domain(str(file_path))
        else:
            payload, errs = None, [f"[{key}] 未识别的输入实体"]

        payloads[key] = payload
        for e in errs:
            errors.append(f"[{key}] {e}")

    # 新版模块一同时交付完整 references；这里只做确定性的 id/元数据关系校验，
    # 不做研究内容是否合理之类的语义判断。
    references = payloads.get("references") or []
    if references:
        reference_index = {
            item.get("id"): item for item in references
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        hypothesis_ids = {
            item.get("id") for item in payloads.get("hypotheses") or []
            if isinstance(item, dict)
        }
        for index, reference in enumerate(references):
            if not isinstance(reference, dict):
                continue
            unknown = set(reference.get("related_hypothesis_ids") or []) - hypothesis_ids
            if unknown:
                errors.append(
                    f"[references] references[{index}].related_hypothesis_ids "
                    f"含不存在的假设 id: {sorted(unknown)}"
                )
        for index, paper in enumerate(payloads.get("key_papers") or []):
            if not isinstance(paper, dict):
                continue
            reference = reference_index.get(paper.get("id"))
            if reference is None:
                errors.append(f"[key_papers] key_papers[{index}] 不在 references 中")
                continue
            for field in ("title", "source", "url"):
                if field in paper and paper.get(field) != reference.get(field):
                    errors.append(
                        f"[key_papers] key_papers[{index}].{field} 与 references 不一致"
                    )

    # 注：跨字段语义一致性检查（gap.rq ≈ rq.topic / baseline ⊆ missing）已删除——
    # 这类**内容性 / 语义性**判断由 method-designer / method-critic 的 Self-Reflection
    # Checklist 在生成 + 评审时自查；详见 references/{method-designer,method-critic}.md。

    return payloads, errors


# ════════════════════════════════════════════════════════════════
# 8 个输入实体的解析函数
# ════════════════════════════════════════════════════════════════
#
# 注：每个 _load_xxx 内部只做**结构性**校验（类型/格式/必填/范围/唯一性），
# 不做**内容性**判断（如"假设是否真可验证"、"两段文本是否讲同一研究问题"）。
# 内容性判断由 3 个 persona 的 Self-Reflection Checklist 在 LLM 生成 + 评审时自查。
# ════════════════════════════════════════════════════════════════


def _load_research_question(path: str) -> tuple[dict, list[str]]:
    """
    解析 ResearchQuestion（planning-schemas.md §1.1 / conception-schemas.md §2.1）。

    必填字段: topic / scope / success_criteria（conception §2.1 字段表）。
    全部为非空 str（conception §5 强校验规则 1：三者非空且语义不矛盾）。
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `research_question` 子项）。

    Returns:
        (payload, errors)。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors = _validate_required_fields(
        "research_question", payload, ["topic", "scope", "success_criteria"],
    )
    # 类型 + 非空检查（conception §5 强校验规则 1）
    for field in ("topic", "scope", "success_criteria"):
        v = payload.get(field)
        if v is None:
            continue  # 必填已由 _validate_required_fields 报缺
        if not isinstance(v, str):
            errors.append(f"字段 {field} 不是 str（实际 {type(v).__name__}）")
        elif not v.strip():
            errors.append(f"字段 {field} 是空字符串（conception §5 强校验规则 1）")
    return payload, errors


def _load_hypotheses(path: str) -> tuple[list[dict], list[str]]:
    """
    解析 hypotheses[]（planning-schemas.md §1.2 / conception-schemas.md §2.2）。

    每条必填: id / claim / verifiable（conception §2.2 字段表）。
    结构性校验（conception §5 规则 2/3）：
        - 至少 1 条假设
        - id 格式 `H\\d+`（如 `H1` / `H2`）
        - id 在当前任务内必须唯一
    返回值示例见 conception-schemas.md §7（`hypotheses[]` 数组）。

    内容性检查（"verifiable 是否真为 true" / "claim 是否含可观察预期变化"）
    已删除——由 method-designer 的 Self-Reflection Checklist 在生成时自查。

    Returns:
        (hypotheses_list, errors)。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return [], [f"读文件失败: {e}"]

    if not isinstance(payload, list):
        return [], [f"顶层不是 list（实际 {type(payload).__name__}）"]

    errors: list[str] = []
    seen_ids: set[str] = set()
    import re as _re
    _H_ID_RE = _re.compile(r"^H\d+$")
    for i, hyp in enumerate(payload):
        if not isinstance(hyp, dict):
            errors.append(f"hypotheses[{i}] 不是 dict")
            continue
        # 必填字段
        for f in ("id", "claim", "verifiable"):
            if f not in hyp:
                errors.append(f"hypotheses[{i}] 缺必填字段: {f}")
        # id 格式（conception §5 规则 3）
        hid = hyp.get("id")
        if hid is not None:
            if not isinstance(hid, str):
                errors.append(f"hypotheses[{i}].id 不是 str（实际 {type(hid).__name__}）")
            elif not hid.strip():
                errors.append(f"hypotheses[{i}].id 是空字符串")
            elif not _H_ID_RE.match(hid):
                errors.append(
                    f"hypotheses[{i}].id={hid!r} 格式不符（期望 H\\d+，如 H1/H2；conception §5 规则 3）"
                )
            elif hid in seen_ids:
                errors.append(
                    f"hypotheses[{i}].id={hid!r} 与之前重复（conception §5 规则 3：id 任务内必须唯一）"
                )
            else:
                seen_ids.add(hid)
        # claim 非空（结构性）
        claim = hyp.get("claim")
        if claim is not None:
            if not isinstance(claim, str):
                errors.append(f"hypotheses[{i}].claim 不是 str")
            elif not claim.strip():
                errors.append(f"hypotheses[{i}].claim 是空字符串")
        # verifiable 是 bool（结构性）
        v = hyp.get("verifiable")
        if v is not None and not isinstance(v, bool):
            errors.append(
                f"hypotheses[{i}].verifiable 不是 bool（实际 {type(v).__name__}）"
            )

    if not payload:
        errors.append("hypotheses 列表为空（conception §5 规则 2：至少 1 条假设）")

    return payload, errors


def _load_gap_report(path: str) -> tuple[dict, list[str]]:
    """
    解析 GapReport（planning-schemas.md §1.3 / conception-schemas.md §2.3）。

    必填字段: research_question / existing_state / missing_capability（conception §2.3）。
    可选: opportunities（list[str]）。
    强校验（conception §5 规则 7/8）：
        - existing_state / missing_capability 非空（防凭空生成研究空白）
        - missing_capability 应位于 research_question.scope 内（conception §6 一致性）
          → 跨字段一致性检查由 load_inputs() 在汇总阶段执行（见 _cross_validate）
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `gap_report` 子项）。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors = _validate_required_fields(
        "gap_report", payload, ["research_question", "existing_state", "missing_capability"],
    )
    # 必填 str 字段：类型 + 非空
    for field in ("research_question", "existing_state", "missing_capability"):
        v = payload.get(field)
        if v is None:
            continue
        if not isinstance(v, str):
            errors.append(f"字段 {field} 不是 str（实际 {type(v).__name__}）")
        elif not v.strip():
            errors.append(
                f"字段 {field} 是空字符串（conception §5 规则 7/8：不得凭空生成研究空白）"
            )
    # opportunities 必须是 list[str]
    opps = payload.get("opportunities")
    if opps is not None:
        if not isinstance(opps, list):
            errors.append(f"opportunities 不是 list（实际 {type(opps).__name__}）")
        else:
            for i, x in enumerate(opps):
                if not isinstance(x, str):
                    errors.append(f"opportunities[{i}] 不是 str")
                elif not x.strip():
                    errors.append(f"opportunities[{i}] 是空字符串")
    return payload, errors


def _load_key_papers(path: str) -> tuple[list[dict], list[str]]:
    """
    解析 key_papers[]（planning-schemas.md §1.4 / conception-schemas.md §2.4）。

    每条必填: id / title / method_key（conception §2.4 字段表）。
    可选: is_baseline（bool）。
    结构性校验（conception §5 规则 5/6）：
        - 至少 1 条
        - id 非空且任务内唯一
        - title / method_key 非空
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `key_papers[]` 数组）。

    注：内容性检查（"key_papers 数量是否 ≥ 3 算充足"）已删除——
    由 method-designer 的 Self-Reflection Checklist 在生成时自查。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return [], [f"读文件失败: {e}"]

    if not isinstance(payload, list):
        return [], [f"顶层不是 list（实际 {type(payload).__name__}）"]

    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, paper in enumerate(payload):
        if not isinstance(paper, dict):
            errors.append(f"key_papers[{i}] 不是 dict")
            continue
        # 必填字段
        for f in ("id", "title", "method_key"):
            if f not in paper:
                errors.append(f"key_papers[{i}] 缺必填字段: {f}")
        # id 非空 + 唯一（conception §5 规则 6）
        pid = paper.get("id")
        if pid is not None:
            if not isinstance(pid, str):
                errors.append(f"key_papers[{i}].id 不是 str（实际 {type(pid).__name__}）")
            elif not pid.strip():
                errors.append(f"key_papers[{i}].id 是空字符串（conception §5 规则 5：id 必须能核验）")
            elif pid in seen_ids:
                errors.append(
                    f"key_papers[{i}].id={pid!r} 与之前重复（conception §5 规则 6：id 任务内必须唯一）"
                )
            else:
                seen_ids.add(pid)
        # title 非空
        title = paper.get("title")
        if title is not None and isinstance(title, str) and not title.strip():
            errors.append(f"key_papers[{i}].title 是空字符串")
        # method_key 非空
        mk = paper.get("method_key")
        if mk is not None and isinstance(mk, str) and not mk.strip():
            errors.append(f"key_papers[{i}].method_key 是空字符串")
        # is_baseline 必须是 bool
        bl = paper.get("is_baseline")
        if bl is not None and not isinstance(bl, bool):
            errors.append(f"key_papers[{i}].is_baseline 不是 bool")

    if not payload:
        errors.append("key_papers 列表为空（conception §5 规则 5：至少 1 条）")

    return payload, errors


def _load_references(path: str) -> tuple[list[dict], list[str]]:
    """Load Module-1's verified bibliography without changing its metadata.

    The planning agents may use these records for evidence and baseline
    provenance, while ``key_papers`` remains the smaller method-design subset.
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"读文件失败: {exc}"]
    if not isinstance(payload, list):
        return [], [f"顶层不是 list（实际 {type(payload).__name__}）"]

    errors: list[str] = []
    if len(payload) != 10:
        errors.append(f"references 必须恰好包含 10 篇（实际 {len(payload)}）")
    seen_ids: set[str] = set()
    allowed_sources = {"arxiv", "semantic_scholar", "openalex", "crossref"}
    for index, reference in enumerate(payload):
        prefix = f"references[{index}]"
        if not isinstance(reference, dict):
            errors.append(f"{prefix} 不是 dict")
            continue
        for field in ("id", "title", "authors", "year", "source", "url", "citation_text", "relevance"):
            if field not in reference:
                errors.append(f"{prefix} 缺必填字段: {field}")
        paper_id = reference.get("id")
        if not isinstance(paper_id, str) or not paper_id.strip():
            errors.append(f"{prefix}.id 必须是非空字符串")
        elif paper_id in seen_ids:
            errors.append(f"{prefix}.id={paper_id!r} 与之前重复")
        else:
            seen_ids.add(paper_id)
        for field in ("title", "url", "citation_text", "relevance"):
            value = reference.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{field} 必须是非空字符串")
        authors = reference.get("authors")
        if not isinstance(authors, list) or not authors or not all(
            isinstance(author, str) and author.strip() for author in authors
        ):
            errors.append(f"{prefix}.authors 必须是非空字符串列表")
        year = reference.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            errors.append(f"{prefix}.year 必须是整数")
        if reference.get("source") not in allowed_sources:
            errors.append(f"{prefix}.source 不在允许枚举中")
        related = reference.get("related_hypothesis_ids", [])
        if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
            errors.append(f"{prefix}.related_hypothesis_ids 必须是字符串列表")
    return payload, errors


def _load_resource_constraints(path: str) -> tuple[dict, list[str]]:
    """
    解析 ResourceConstraints（planning-schemas.md §1.5 / conception-schemas.md §1.3）。

    必填字段: gpu_hours / memory_gb / time_budget_days（conception §1.3）。
    可选: gpu_type (str / null) / budget (float / null)。

    数值范围（与 conception-schemas.md §1.3 强校验对齐）：
        - gpu_hours ≥ 0
        - memory_gb > 0
        - time_budget_days > 0
        - budget ≥ 0 或 null（null 走"无算力门禁"分支）

    注: gpu_hours / memory_gb / time_budget_days 在 JSON 中读为 int；budget 读为 number。
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `resource_constraints` 子项）。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors = _validate_required_fields(
        "resource_constraints",
        payload,
        ["gpu_hours", "memory_gb", "time_budget_days"],
    )
    # int 字段（必填 + 范围）
    for f, lo in (("gpu_hours", 0), ("memory_gb", 1), ("time_budget_days", 1)):
        v = payload.get(f)
        if v is None:
            continue  # 必填已由 _validate_required_fields 报缺
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(
                f"resource_constraints.{f} 不是 int（实际 {type(v).__name__}）"
            )
        elif v < lo:
            errors.append(
                f"resource_constraints.{f}={v} 不在合法范围 [≥{lo if lo > 0 else 0}] 内"
            )
    # 可选 gpu_type str
    gpu_type = payload.get("gpu_type")
    if gpu_type is not None and not isinstance(gpu_type, str):
        errors.append(f"resource_constraints.gpu_type 不是 str（实际 {type(gpu_type).__name__}）")
    # 可选 budget number（≥ 0 或 null）
    budget = payload.get("budget")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            errors.append(
                f"resource_constraints.budget 不是 number（实际 {type(budget).__name__}）"
            )
        elif budget < 0:
            errors.append(
                f"resource_constraints.budget={budget} 不在合法范围 [≥0] 内"
            )
    return payload, errors


def _load_research_frontier(path: str) -> tuple[dict, list[str]]:
    """
    解析 ResearchFrontier（planning-schemas.md §1.6 / conception-schemas.md §2.5）。

    必填字段: frontier_text（conception §2.5）。
    弱格式：只检查字段非空，不校验段落内部结构（conception §2.5 注）。
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `research_frontier` 子项）。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors = _validate_required_fields("research_frontier", payload, ["frontier_text"])
    ft = payload.get("frontier_text")
    if ft is not None and not isinstance(ft, str):
        errors.append("frontier_text 不是 str")
    elif isinstance(ft, str) and not ft.strip():
        errors.append("frontier_text 为空字符串（弱格式：必须非空）")
    return payload, errors


def _load_domain(path: str) -> tuple[dict, list[str]]:
    """
    解析 Domain（planning-schemas.md §1.7 / conception-schemas.md §1.2）。

    必填字段: domain_name（conception §1.2）。
    强校验：domain_name 非空 str（conception §5 规则 10：透传过程中不得修改）。
    返回值示例见 conception-schemas.md §7（顶层 keys 的 `domain` 子项）。
    """
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors = _validate_required_fields("domain", payload, ["domain_name"])
    dn = payload.get("domain_name")
    if dn is not None:
        if not isinstance(dn, str):
            errors.append(f"domain_name 不是 str（实际 {type(dn).__name__}）")
        elif not dn.strip():
            errors.append(
                "domain_name 是空字符串（conception §5 规则 10：domain 必须可识别）"
            )
    return payload, errors


# ════════════════════════════════════════════════════════════════
# PlanningFeedback 加载（§1.5.1，REPLAN 入口）
# ════════════════════════════════════════════════════════════════


def load_planning_feedback(path: str) -> tuple[dict | None, list[str]]:
    """
    加载模块三返回的 PlanningFeedback（planning-schemas.md §1.5.1）。

    该函数与 load_inputs() 并列，不依赖 7 个模块一输入——是 REPLAN 路径的独立入口。
    main.py 通过 --replan-from <path> 传入，路径既可指向 JSON 文件，也可指向包含
    `planning_feedback.json` 的目录。

    强校验（schemas §1.5.1）：
        1. 顶层是 dict
        2. reason / affected_experiment_ids / blockers / suggested_changes 必填
        3. affected_experiment_ids 非空 list[str]
        4. blockers 非空 list[str]，每条形如 `[category] description`，
           category ∈ _FEEDBACK_CATEGORIES（8 类别：data, compute, baseline, metric,
           schema, method, experiment, budget）
        5. suggested_changes 非空 list[str]

    Args:
        path: PlanningFeedback 文件路径（.json）或目录（含 planning_feedback.json）。

    Returns:
        (payload, errors):
            payload: PlanningFeedback dict；失败时为 None。
            errors:  错误信息列表；空列表表示通过。
    """
    p = Path(path)
    if p.is_dir():
        p = p / "planning_feedback.json"
    if not p.is_file():
        return None, [f"文件不存在: {p}"]

    try:
        payload = _read_json(str(p))
    except (json.JSONDecodeError, OSError) as e:
        return None, [f"读文件失败: {e}"]

    if not isinstance(payload, dict):
        return None, [f"顶层不是 dict（实际 {type(payload).__name__}）"]

    errors: list[str] = []

    # 必填字段
    errors.extend(_validate_required_fields(
        "planning_feedback",
        payload,
        ["reason", "affected_experiment_ids", "blockers", "suggested_changes"],
    ))

    # reason: 非空 str
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        errors.append(f"reason 不是 str（实际 {type(reason).__name__}）")
    elif isinstance(reason, str) and not reason.strip():
        errors.append("reason 为空字符串")

    # affected_experiment_ids: 非空 list[str]
    aei = payload.get("affected_experiment_ids")
    if aei is not None:
        if not isinstance(aei, list):
            errors.append(f"affected_experiment_ids 不是 list（实际 {type(aei).__name__}）")
        elif not aei:
            errors.append("affected_experiment_ids 为空（status=REPLAN 必须有具体受影响实验）")
        else:
            for i, x in enumerate(aei):
                if not isinstance(x, str) or not x.strip():
                    errors.append(f"affected_experiment_ids[{i}] 不是非空字符串")

    # blockers: 非空 list[str]，每条 `[category] description`
    blockers = payload.get("blockers")
    if blockers is not None:
        if not isinstance(blockers, list):
            errors.append(f"blockers 不是 list（实际 {type(blockers).__name__}）")
        elif not blockers:
            errors.append("blockers 为空（REPLAN 不得只报问题不分类）")
        else:
            for i, b in enumerate(blockers):
                if not isinstance(b, str) or not b.strip():
                    errors.append(f"blockers[{i}] 不是非空字符串")
                    continue
                # 格式校验：[category] description
                if not (b.startswith("[") and "]" in b):
                    errors.append(
                        f"blockers[{i}] 格式不符（期望 '[category] description'）：{b!r}"
                    )
                    continue
                cat = b[1:b.index("]")].strip()
                if cat not in _FEEDBACK_CATEGORIES:
                    errors.append(
                        f"blockers[{i}] category={cat!r} 不在合法集合 {sorted(_FEEDBACK_CATEGORIES)} 内"
                    )
                desc = b[b.index("]") + 1:].strip()
                if not desc:
                    errors.append(f"blockers[{i}] description 为空（[category] 后需有 description）")

    # suggested_changes: 非空 list[str]
    sc = payload.get("suggested_changes")
    if sc is not None:
        if not isinstance(sc, list):
            errors.append(f"suggested_changes 不是 list（实际 {type(sc).__name__}）")
        elif not sc:
            errors.append("suggested_changes 为空（模块三不得只报问题不给建议）")
        else:
            for i, x in enumerate(sc):
                if not isinstance(x, str) or not x.strip():
                    errors.append(f"suggested_changes[{i}] 不是非空字符串")

    return payload, errors


# ════════════════════════════════════════════════════════════════
# 校验工具（被 7 个 _load_xxx 内部调用）
# ════════════════════════════════════════════════════════════════


def _read_json(path: str) -> Any:
    """读 JSON 文件，返回解析后的对象。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_required_fields(
    entity_name: str,
    payload: dict,
    required: list[str],
) -> list[str]:
    """
    校验 payload 的必填字段是否齐全。

    Args:
        entity_name: 实体名（用于错误信息定位，如 "research_question"）。
        payload: 待校验 dict。
        required: 必填字段名列表。

    Returns:
        错误信息列表。空列表表示通过。
        每条形如: "[research_question] 缺必填字段: topic"
    """
    errors: list[str] = []
    for f in required:
        if f not in payload:
            errors.append(f"缺必填字段: {f}")
        elif payload[f] is None:
            errors.append(f"必填字段为 None: {f}")
    return errors


def _validate_enum(
    entity_name: str,
    field_path: str,
    value: Any,
    allowed: list[str],
) -> str | None:
    """
    校验 value 是否在 allowed 枚举值内。

    Returns:
        错误信息字符串；通过返回 None。
    """
    if value not in allowed:
        return (
            f"[{entity_name}] {field_path}={value!r} 不在枚举 {allowed} 内"
        )
    return None


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """
    CLI 入口：
        python scripts/load_inputs.py --input-dir <path> [--strict]

    --strict: 任一错误即退出码 1；否则 0（带警告通过）。
    """
    parser = argparse.ArgumentParser(description="加载并校验模块一 8 个输入")
    parser.add_argument("--input-dir", required=True, help="模块一产物目录")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任一错误即退出码 1；否则 0",
    )
    args = parser.parse_args(argv)

    try:
        payloads, errors = load_inputs(args.input_dir)
    except FileNotFoundError as e:
        log.error(f"[load_inputs] {e}")
        return 1

    if errors:
        for e in errors:
            log.error(f"[load_inputs] {e}")
        if args.strict:
            return 1
        return 0
    log.info(f"[load_inputs] 8 个输入全部校验通过: {list(payloads.keys())}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
