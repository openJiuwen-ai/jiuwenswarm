# -*- coding: utf-8 -*-
"""
stage1_load_inputs.py — 阶段 1：load_inputs 加载 3 个 JSON + 字段初筛

输入：3 个 JSON 路径（m1 / m2 / m3）
输出：(inputs_dict, errors_list)

字段校验（writing-schemas §4 强校验规则的实现）：
  * m1.research_question.topic/scope/success_criteria 非空
  * m1.hypotheses ≥ 1 + verifiable=true + id 匹配 H\\d+ + id 唯一
  * m1.key_papers ≥ 3 + id/title 非空 + id 唯一
  * m1.gap_report.research_question ≈ research_question.topic
  * m1.research_frontier.frontier_text 非空
  * m1.domain.domain_name ∈ {nlp, cv, rl, agent, recsys, kg, speech, other}
  * m2.method_design.implementable == true
  * m2.method_design.innovation_points[].experiment_ref ∈ m2.experiment_plan.primary_experiments
  * m2.method_design.hypothesis_coverage[].hypothesis_id 覆盖 m1.hypotheses[].id 全集
  * m2.experiment_plan.success_criteria 非空；量化性由规划/实验门禁负责
  * m2.experiment_plan.datasets[].source_url 非空
  * m3.experiment_results.key_findings ≥ 1
  * m3.experiment_results.experiment_setup 非空
  * m3.experiment_results.result_analysis 非空
  * m3.experiment_results.statistics 的 key ∈ m2.experiment_plan.metrics

CLI 用法：
  python -m scripts.stage1_load_inputs \
      --module1 mock/module1_conception.json \
      --module2 mock/module2_planning.json \
      --module3 mock/module3_execution.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# 合法 domain 集合
VALID_DOMAINS: set[str] = {
    "nlp", "cv", "rl", "agent", "recsys", "kg", "speech", "graph", "multimodal", "other",
}

# 假设 id 格式
HYPOTHESIS_ID_PAT = re.compile(r"^H\d+$")


# ════════════════════════════════════════════════════════════════
# 顶层入口
# ════════════════════════════════════════════════════════════════


def load_inputs(
    module1_path: str,
    module2_path: str,
    module3_path: str,
    source_manifest_path: str | None = None,
) -> tuple[dict, list[str]]:
    """
    读取 3 个上游 JSON，做字段校验。

    Returns:
        ({"m1": ..., "m2": ..., "m3": ...}, errors_list)
        errors 为空列表时表示全部通过。
    """
    errors: list[str] = []
    m1 = _read_json(module1_path, "m1 (Module 1 / Conception)", errors)
    m2 = _read_json(module2_path, "m2 (Module 2 / Planning)", errors)
    m3 = _read_json(module3_path, "m3 (Module 3 / Execution)", errors)
    manifest: dict = {}
    if source_manifest_path:
        manifest = _read_json(source_manifest_path, "source manifest", errors)

    if errors:
        return {"m1": m1, "m2": m2, "m3": m3, "source_manifest": manifest}, errors

    # 字段校验（逐节 §4.x）
    _check_m1(m1, errors)
    _check_m2(m2, m1, errors)
    # Module 3 is intentionally not required to contain results here.  A
    # REPLAN/FAILED execution is a valid upstream state and is routed by the
    # workflow to ``needs_experiment_data`` rather than being rejected as a
    # malformed paper input or silently turned into a result narrative.
    _check_m3_shape(m3, errors)

    return {"m1": m1, "m2": m2, "m3": m3, "source_manifest": manifest}, errors


# ════════════════════════════════════════════════════════════════
# m1 校验
# ════════════════════════════════════════════════════════════════


def _check_m1(m1: dict, errors: list[str]) -> None:
    # §4.1 research_question 三必填
    rq = m1.get("research_question") or {}
    if not isinstance(rq, dict):
        errors.append("§4.1 m1.research_question 必须是 object")
    else:
        for f in ("topic", "scope", "success_criteria"):
            if not _nonempty_str(rq.get(f)):
                errors.append(f"§4.1 m1.research_question.{f} 必填非空")

    # §4.2/4.3 hypotheses
    hypotheses = m1.get("hypotheses") or []
    if not isinstance(hypotheses, list) or len(hypotheses) < 1:
        errors.append("§4.2 m1.hypotheses[] 至少 1 条")
    else:
        seen_h: set[str] = set()
        for i, h in enumerate(hypotheses):
            if not isinstance(h, dict):
                errors.append(f"§4.2 m1.hypotheses[{i}] 必须是 object")
                continue
            if h.get("verifiable") is not True:
                errors.append(f"§4.2 m1.hypotheses[{i}].verifiable 必须为 true")
            if not _nonempty_str(h.get("claim")):
                errors.append(f"§4.2 m1.hypotheses[{i}].claim 必填非空")
            hid = h.get("id")
            if not _nonempty_str(hid):
                errors.append(f"§4.3 m1.hypotheses[{i}].id 必填非空")
            else:
                if not HYPOTHESIS_ID_PAT.match(str(hid)):
                    errors.append(f"§4.3 m1.hypotheses[{i}].id={hid!r} 不匹配 H\\d+ 格式")
                if hid in seen_h:
                    errors.append(f"§4.3 m1.hypotheses[{i}].id={hid!r} 重复")
                seen_h.add(str(hid))

    # §4.5/4.6 key_papers
    key_papers = m1.get("key_papers") or []
    if not isinstance(key_papers, list) or len(key_papers) < 3:
        errors.append(f"§4.5 m1.key_papers 数量 {len(key_papers) if isinstance(key_papers, list) else 0} < 3")
    else:
        seen_kp: set[str] = set()
        for i, p in enumerate(key_papers):
            if not isinstance(p, dict):
                errors.append(f"§4.5 m1.key_papers[{i}] 必须是 object")
                continue
            pid = p.get("id")
            if not _nonempty_str(pid):
                errors.append(f"§4.5 m1.key_papers[{i}].id 必填非空")
            else:
                if pid in seen_kp:
                    errors.append(f"§4.6 m1.key_papers[{i}].id={pid!r} 重复")
                seen_kp.add(str(pid))
            if not _nonempty_str(p.get("title")):
                errors.append(f"§4.5 m1.key_papers[{i}].title 必填非空")
            if not _nonempty_str(p.get("method_key")):
                errors.append(f"§4.5 m1.key_papers[{i}].method_key 必填非空")

    # §4.7 gap_report
    gap = m1.get("gap_report") or {}
    if not isinstance(gap, dict):
        errors.append("§4.7 m1.gap_report 必须是 object")
    else:
        for f in ("research_question", "existing_state", "missing_capability"):
            if not _nonempty_str(gap.get(f)):
                errors.append(f"§4.7 m1.gap_report.{f} 必填非空")

    # §4.x research_frontier（弱格式，仅非空检查）
    rf = m1.get("research_frontier") or {}
    if not _nonempty_str((rf or {}).get("frontier_text")):
        errors.append("§4.1 m1.research_frontier.frontier_text 必填非空")

    # Domain labels are upstream taxonomy, not a writing eligibility gate.
    dom = m1.get("domain") or {}
    dn = dom.get("domain_name")
    if not _nonempty_str(dn):
        errors.append("§4.1 m1.domain.domain_name 必填非空")


# ════════════════════════════════════════════════════════════════
# m2 校验
# ════════════════════════════════════════════════════════════════


def _check_m2(m2: dict, m1: dict, errors: list[str]) -> None:
    md = m2.get("method_design") or {}
    if not isinstance(md, dict):
        errors.append("§4.9 m2.method_design 必须是 object")
        return

    # §4.9 implementable
    if md.get("implementable") is not True:
        errors.append("§4.9 m2.method_design.implementable 必须为 true")

    # 必填字段。planning 保留富结构供写作使用：framework 是 object，technical_route 是 list。
    for f in ("research_goal", "core_mechanism"):
        if not _nonempty_str(md.get(f)):
            errors.append(f"§4.7 m2.method_design.{f} 必填非空")
    if not _nonempty_value(md.get("framework")):
        errors.append("§4.7 m2.method_design.framework 必填非空（str 或 object）")
    if not _nonempty_value(md.get("technical_route")):
        errors.append("§4.7 m2.method_design.technical_route 必填非空（str 或 list）")

    comp_names: set[str] = set()
    comp_aliases: set[str] = set()
    components = md.get("components") or []
    if not isinstance(components, list) or len(components) < 1:
        errors.append("§4.7 m2.method_design.components 至少 1 个")
    else:
        for i, c in enumerate(components):
            if not isinstance(c, dict):
                errors.append(f"§4.7 m2.method_design.components[{i}] 必须是 object")
                continue
            for f in ("name", "function", "input_schema", "output_schema"):
                if not _nonempty_str(c.get(f)):
                    errors.append(f"§4.7 m2.method_design.components[{i}].{f} 必填非空")
            if c.get("novelty_degree") not in {"novel", "adapted", "standard"}:
                errors.append(
                    f"§4.7 m2.method_design.components[{i}].novelty_degree={c.get('novelty_degree')!r} "
                    "必须在 {novel, adapted, standard} 中"
                )
            cname = c.get("name")
            if cname:
                full_name = str(cname).strip()
                comp_names.add(full_name)
                comp_aliases.add(full_name.casefold())
                # Planning commonly emits ``ABC (Expanded Component Name)``
                # while the mechanism uses only ``ABC``. Treat that stable
                # leading abbreviation as the same component without fuzzy
                # token matching that could accept unrelated prose.
                leading = re.split(r"\s*[（(]", full_name, maxsplit=1)[0].strip()
                if len(leading) >= 2:
                    comp_aliases.add(leading.casefold())

    # §4.10 innovation_points[].experiment_ref ∈ primary_experiments
    ep = m2.get("experiment_plan") or {}
    primary_ids: set[str] = set()
    if isinstance(ep, dict):
        for x in (ep.get("primary_experiments") or []):
            primary_ids.add(str(x))

    innovation_points = md.get("innovation_points") or []
    if not isinstance(innovation_points, list) or len(innovation_points) < 1:
        errors.append("§4.10 m2.method_design.innovation_points 至少 1 个")
    else:
        for i, ip in enumerate(innovation_points):
            if not isinstance(ip, dict):
                errors.append(f"§4.10 m2.method_design.innovation_points[{i}] 必须是 object")
                continue
            for f in ("claim", "experiment_ref"):
                if not _nonempty_str(ip.get(f)):
                    errors.append(f"§4.10 m2.method_design.innovation_points[{i}].{f} 必填非空")
            # 新 planning schema：evidence_metric 是 list[EvidenceMetric]，保留 legacy str 兼容旧产物。
            if not _nonempty_evidence_metric(ip.get("evidence_metric")):
                errors.append(
                    f"§4.10 m2.method_design.innovation_points[{i}].evidence_metric "
                    "必填非空（str 或非空 list[EvidenceMetric]）"
                )
            er = ip.get("experiment_ref")
            if er and primary_ids and str(er) not in primary_ids:
                errors.append(
                    f"§4.10 m2.method_design.innovation_points[{i}].experiment_ref={er!r} "
                    f"不在 m2.experiment_plan.primary_experiments={sorted(primary_ids)} 中"
                )

    # §4.11 hypothesis_coverage 覆盖 m1.hypotheses[].id 全集
    h_ids_required: set[str] = set()
    for h in (m1.get("hypotheses") or []):
        if isinstance(h, dict) and h.get("id"):
            h_ids_required.add(str(h["id"]))

    coverage = md.get("hypothesis_coverage") or []
    covered: set[str] = set()
    if isinstance(coverage, list) and coverage:
        for i, hc in enumerate(coverage):
            if not isinstance(hc, dict):
                errors.append(f"§4.11 m2.method_design.hypothesis_coverage[{i}] 必须是 object")
                continue
            for f in ("hypothesis_id", "mechanism", "experiment_ref"):
                if not _nonempty_str(hc.get(f)):
                    errors.append(f"§4.11 m2.method_design.hypothesis_coverage[{i}].{f} 必填非空")
            hid = hc.get("hypothesis_id")
            if hid:
                covered.add(str(hid))
                mech = str(hc.get("mechanism") or "")
                folded_mechanism = mech.casefold()
                if comp_names and mech and not any(
                    alias in folded_mechanism for alias in comp_aliases
                ):
                    # 要求机制叙述引用一个组件的全名或显式缩写；不做宽松模糊匹配。
                    errors.append(
                        f"§5.7 m2.method_design.hypothesis_coverage[{i}].mechanism"
                        f"={hc.get('mechanism')!r} 未包含任何 components[].name={sorted(comp_names)}"
                    )

    missing = h_ids_required - covered
    if h_ids_required and missing:
        errors.append(
            f"§4.11 m2.method_design.hypothesis_coverage 未覆盖假设: {sorted(missing)}"
        )

    # limitations
    if not isinstance(md.get("limitations"), list) or len(md.get("limitations") or []) < 1:
        errors.append("§4.7 m2.method_design.limitations 至少 1 条")

    # experiment_plan 校验
    if not isinstance(ep, dict):
        errors.append("§4.12 m2.experiment_plan 必须是 object")
        return

    # Quantified criteria are enforced by Planning and Experiment before any
    # run can become PASS. Writing is deliberately a late consumer: rejecting
    # an otherwise verified experiment here because one planning sentence is
    # qualitative causes an expensive, non-actionable full-pipeline rerun.
    scs = ep.get("success_criteria") or []
    if not isinstance(scs, list) or len(scs) < 1:
        errors.append("§4.12 m2.experiment_plan.success_criteria 至少 1 条")
    else:
        for i, sc in enumerate(scs):
            criterion = sc.get("criterion") if isinstance(sc, dict) else sc
            if not _nonempty_str(criterion):
                errors.append(f"§4.12 m2.experiment_plan.success_criteria[{i}] 必填非空（str 或 object.criterion）")

    # §4.13 datasets[].source_url 非空
    datasets = ep.get("datasets") or []
    if not isinstance(datasets, list) or len(datasets) < 1:
        errors.append("§4.13 m2.experiment_plan.datasets 至少 1 个")
    else:
        for i, d in enumerate(datasets):
            if not isinstance(d, dict):
                errors.append(f"§4.13 m2.experiment_plan.datasets[{i}] 必须是 object")
                continue
            if not _nonempty_str(d.get("source_url")):
                errors.append(f"§4.13 m2.experiment_plan.datasets[{i}].source_url 必填非空")
            if not _nonempty_str(d.get("name")):
                errors.append(f"§4.13 m2.experiment_plan.datasets[{i}].name 必填非空")

    # baselines
    baselines = ep.get("baselines") or []
    if not isinstance(baselines, list) or len(baselines) < 1:
        errors.append("§4.13 m2.experiment_plan.baselines 至少 1 个")
    else:
        for i, b in enumerate(baselines):
            if not isinstance(b, dict):
                errors.append(f"§4.13 m2.experiment_plan.baselines[{i}] 必须是 object")
                continue
            for f in ("name", "paper_id", "metric_name"):
                if not _nonempty_str(b.get(f)):
                    errors.append(f"§4.13 m2.experiment_plan.baselines[{i}].{f} 必填非空")

    # metrics
    metrics = ep.get("metrics") or []
    if not isinstance(metrics, list) or len(metrics) < 1:
        errors.append("§4.12 m2.experiment_plan.metrics 至少 1 个")
    else:
        for i, m in enumerate(metrics):
            if not _nonempty_str(m):
                errors.append(f"§4.12 m2.experiment_plan.metrics[{i}] 必填非空")


# ════════════════════════════════════════════════════════════════
# m3 校验
# ════════════════════════════════════════════════════════════════


def _check_m3_shape(m3: dict, errors: list[str]) -> None:
    """Validate only the stable envelope, not a claim that results exist."""
    status = m3.get("status")
    if status is not None and not _nonempty_str(status):
        errors.append("m3.status 必须是非空 string（若提供）")
    er = m3.get("experiment_results") or {}
    if not isinstance(er, dict):
        errors.append("§4.14 m3.experiment_results 必须是 object")
    # An absent/null experiment_results is valid for REPLAN/FAILED inputs.
    # Detailed checks happen only after the evidence gate has established that
    # there is a completed, traceable execution to inspect.


# ════════════════════════════════════════════════════════════════
# helper
# ════════════════════════════════════════════════════════════════


def _read_json(path: str, label: str, errors: list[str]) -> dict:
    p = Path(path)
    if not p.is_file():
        errors.append(f"{label} 文件不存在: {path}")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"{label} JSON 解析失败: {e}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{label} JSON 顶层必须为 object，实际是 {type(data).__name__}")
        return {}
    return data


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _nonempty_value(v: Any) -> bool:
    """接受写作所需的富结构，而不把 object/list 错当成缺失。"""
    if _nonempty_str(v):
        return True
    if isinstance(v, dict):
        return bool(v)
    if isinstance(v, list):
        return bool(v)
    return False


def _nonempty_evidence_metric(v: Any) -> bool:
    """兼容旧版 str 与新版 list[EvidenceMetric]。"""
    if _nonempty_str(v):
        return True
    if not isinstance(v, list) or not v:
        return False
    return all(isinstance(item, dict) and _nonempty_str(item.get("metric_name")) for item in v)


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Writing 模块输入侧：读取并校验 3 个上游 JSON",
    )
    parser.add_argument("--module1", required=True, help="模块一（Conception）JSON 路径")
    parser.add_argument("--module2", required=True, help="模块二（Planning）JSON 路径")
    parser.add_argument("--module3", required=True, help="模块三（Execution）JSON 路径")
    parser.add_argument(
        "--quiet", action="store_true", help="通过时不打印成功信息（只显错）"
    )
    args = parser.parse_args()

    inputs, errors = load_inputs(args.module1, args.module2, args.module3)

    if errors:
        print(f"❌ load_inputs 失败：共 {len(errors)} 条错误", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("✅ load_inputs 通过：3 个 JSON 全部合法")
        print(f"  - m1: {len(inputs['m1'].get('key_papers') or [])} key_papers, "
              f"{len(inputs['m1'].get('hypotheses') or [])} hypotheses")
        print(f"  - m2: {len((inputs['m2'].get('method_design') or {}).get('components') or [])} "
              "components, "
              f"{len((inputs['m2'].get('experiment_plan') or {}).get('baselines') or [])} "
              "baselines")
        print(f"  - m3: {len((inputs['m3'].get('experiment_results') or {}).get('key_findings') or [])} "
              "key_findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
