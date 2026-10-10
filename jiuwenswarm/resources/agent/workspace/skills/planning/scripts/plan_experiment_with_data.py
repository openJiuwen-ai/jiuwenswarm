# -*- coding: utf-8 -*-
"""
plan_experiment_with_data.py — 第 2 阶段：规划实验方案 + 抽数据订单

按 [workflows/planning.md](../workflows/planning.md) Phase B 实现。
通过 [_subagent.py](_subagent.py) → facade.agent() 拉起 Planner sub-agent 出
``{"experiment_plan": ..., "data_plan": ...}`` 包装 dict；本脚本解包后，
data_plan 由 experiment_plan 确定性抽取（不是 LLM）。

REPLAN_DATA 路径（[planning-schemas.md §1.5.1](../references/planning-schemas.md)）：
    当 blockers 含 `data` 类时，带模块三 PlanningFeedback（blockers 原文 /
    suggested_changes / 失败 URL 黑名单）**重跑 experiment-planner** 重新规划
    数据源，随后照常走数据源自修复循环 + Layer A 门禁 + critic（1 轮）。
    2026-09-17 修复：此前该路径直接 ``dict(prev_experiment_plan)`` 逐字复用且
    ``skip_reflection=True`` 跳过全部自修复与门禁，数据源 URL 每轮一字不变 →
    同样的 [data] blocker 必然复现，REPLAN 永远无法自愈。

2026-08-29 新增反思循环（A-② adequacy<7 触发 / D-② 最多 3 轮）：
    每次 experiment-planner 调起后，调 experiment-critic 评 adequacy_score
    + feedback_addressed；通过 + 无 unaddressed → 跳出，否则重写（最多 3 轮）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from scripts._subagent import call_session_async, coerce_feedback_text
from scripts._hard_gates import (
    hard_gate_experiment_plan,
    format_gaps_as_feedback,
)
from scripts.validate_plan import validate_experiment_plan, validate_data_plan

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.plan_experiment_with_data")

# 评审门禁常量（与 iterate_method_design 对齐）
DEFAULT_MAX_REVIEWER_ROUNDS = 2
# REPLAN_DATA 下随 payload 下发给 planner 的「已验证候选直链」每数据集上限
MAX_RESOLVED_FILE_HINTS = 3
# Phase 2 (Track B 反思合并)：从 3 降到 2，与 method_design 一致
# 节省：单 stage 反思 LLM call 从最多 6 砍到最多 4
ADEQUACY_PASS = 7.0
# Layer A 定向补齐的最大重写次数（命中确定性 gap 才花 LLM call，最多多花 1 次）。
# 为什么不是 1 次：门禁 gap 可能同时含「重名 baseline」与「矩阵必须同步改名」两条
# 相互牵连的约束，LLM 偶有只改一半；而 run_legacy_critic=False 时剩余 gap 不进
# 反思循环，会一路流到模块三投影 fail-closed（顶层 aborted，无 planning_feedback）。
_LAYER_A_MAX_REWRITES = 2


def _infer_unique_task_type_from_metrics(metrics: Any) -> str | None:
    """仅在指标族唯一时确定性推导任务类型。

    这不是按数据集名称猜任务：``recall@10``/``ndcg@10`` 的计算契约本身就要求
    query、候选和相关性集合，因此唯一对应 retrieval；MAE/RMSE 唯一对应
    regression；macro/micro/weighted F1 唯一对应 classification。``accuracy``、
    ``recall`` 这类跨任务有歧义的名字不会触发推导，混合指标族也返回 None。
    """
    if not isinstance(metrics, list):
        return None
    families: set[str] = set()
    for raw in metrics:
        value = raw.get("name") if isinstance(raw, dict) else raw
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip().casefold().replace("-", "_").replace(" ", "_")
        name = name.replace("_at_", "@")
        if re.fullmatch(r"(?:recall|precision|hit_rate|ndcg)@\d+", name) or name == "mrr":
            families.add("retrieval")
        elif name in {"macro_f1", "micro_f1", "weighted_f1", "top_1_acc"}:
            families.add("classification")
        elif name in {"mae", "mse", "rmse", "r2"}:
            families.add("regression")
        elif name in {
            "compression_ratio", "qa_token_f1", "answer_exact_match",
            "original_token_count", "compressed_token_count",
        }:
            families.add("memory_compression")
        # accuracy/recall/precision 和时间、成本指标均不能唯一确定任务类型。
    return next(iter(families)) if len(families) == 1 else None


def _normalize_planner_output(
    combined: dict, method_design: dict | None = None
) -> dict:
    """把 LLM experiment-planner 的输出归一化成 ``{experiment_plan, data_plan}``。

    兼容 LLM schema 漂移（deepseek-v4-flash 偶尔漏外层包装）：
        1. 没 ``experiment_plan`` 键，但含 ``objectives`` + ``experiment_matrix``
           ——自动按平铺 dict 包成 ``{experiment_plan: <平铺 dict>, data_plan: {}}``
        2. 都缺——保持原样（让下游 raise，错误信息更准）

    plan_experiment_with_data 和 check_feasibility_and_downgrade 都用这个 helper，
    避免两处分别写兜底逻辑。method_design 传入时做跨产物指标名对账
    （见 _reconcile_metric_names）。
    """
    if "experiment_plan" in combined:
        experiment_plan = combined.get("experiment_plan")
        planner_data_plan = combined.get("data_plan")
        _normalize_dataset_fields(experiment_plan, planner_data_plan)
        _retain_planner_data_plan(experiment_plan, planner_data_plan)
        _normalize_metric_definitions_shape(experiment_plan)
        _reconcile_metric_names(combined["experiment_plan"], method_design)
        return combined
    if "objectives" in combined and "experiment_matrix" in combined:
        log.info(
            f"[plan_experiment_with_data] experiment-planner 漏了外层包装，"
            f"自动按平铺 keys={sorted(combined.keys())} 包裹"
        )
        wrapped = {"experiment_plan": combined, "data_plan": {}}
        _normalize_dataset_fields(combined, None)
        _normalize_metric_definitions_shape(combined)
        _reconcile_metric_names(combined, method_design)
        return wrapped
    return combined


def _normalize_metric_definitions_shape(experiment_plan: Any) -> None:
    """把 planner 常见的 ``{name: definition}`` 规范成契约列表。

    这里只搬运字段，不认可指标已验证，也不改变指标公式。复杂 dict 值会保留其
    已有字段并补 ``metric_name``；字符串值只转换成 ``definition``。
    """
    if not isinstance(experiment_plan, dict):
        return
    raw = experiment_plan.get("metric_definitions")
    if not isinstance(raw, dict):
        return
    normalized: list[dict[str, Any]] = []
    for metric_name, definition in raw.items():
        if not isinstance(metric_name, str) or not metric_name.strip():
            continue
        if isinstance(definition, str):
            normalized.append({
                "metric_name": metric_name.strip(),
                "definition": definition.strip(),
            })
        elif isinstance(definition, dict):
            item = dict(definition)
            item.setdefault("metric_name", metric_name.strip())
            normalized.append(item)
    experiment_plan["metric_definitions"] = normalized


def _retain_planner_data_plan(experiment_plan: Any, data_plan: Any) -> None:
    """把 planner 独立返回的 ``data_plan`` 保存在实验计划内供确定性抽取。

    ``plan_experiment_with_data_async`` 的公开约定是最终 data_plan 必须由
    :func:`_extract_data_order` 确定性生成，不能直接信任 LLM 对象。历史实现因此
    完全忽略了 planner 返回的 data_plan；副作用是其中已经明确写出的
    ``expected_size.task_type``、query/candidate/relevance 字段也被一起丢掉。

    这里仅保存原始声明，真正输出仍由 ``_extract_data_order`` 重建和补齐。若模型
    同时把 data_plan 嵌在 experiment_plan 中，外层规范 data_plan 覆盖同名键，
    ``expected_size`` 则按字段合并，避免一个响应里的有效信息互相覆盖。
    """
    if not isinstance(experiment_plan, dict) or not isinstance(data_plan, dict):
        return
    existing = experiment_plan.get("data_plan")
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in data_plan.items():
        if key == "expected_size" and isinstance(value, dict):
            current = merged.get("expected_size")
            expected = dict(current) if isinstance(current, dict) else {}
            expected.update(value)
            merged[key] = expected
        else:
            merged[key] = value
    experiment_plan["data_plan"] = merged


def _metric_squash(name: Any) -> str:
    """指标名归一：小写 + 去掉所有非字母数字（qa-accuracy / qa_accuracy 同名）。"""
    return re.sub(r"[^0-9a-z]+", "", str(name).lower())


def _reconcile_metric_names(
    experiment_plan: dict, method_design: dict | None
) -> None:
    """让 ``metrics`` 覆盖全部被引用指标名，就地改 experiment_plan + method_design。

    模块三两条硬规则（experiment/scripts/contracts.py）：
    ``baselines[].metric_name ∈ metrics``、创新点 evidence_metric 投影后
    也必须逐字符 ∈ metrics，否则 ExperimentModuleInput 直接拒收。
    run j 实测三方起名永远对不齐：baseline 报根名 ``qa_accuracy``，planner 的
    metrics 是 ``relative_qa_accuracy_degradation`` 等 4 个专名，designer 又用
    ``qa_accuracy_relative`` 等 7 个。两轮 planner+critic 反思都收敛不了。

    确定性两步：① squash 同名（大小写/下划线/连字符差异）把**引用处**改成 metrics
    里的规范拼写；② 仍对不上的引用名直接**并入 metrics**——被 baseline/判据
    引用的指标本来就必须真跑，加进去是让计划诚实，不是放宽契约。
    """
    metrics = experiment_plan.get("metrics")
    if not isinstance(metrics, list):
        return
    canonical: list[str] = []
    seen: set[str] = set()
    for m in metrics:
        if isinstance(m, str) and m.strip():
            name = m.strip()
            sq = _metric_squash(name)
            if sq not in seen:
                seen.add(sq)
                canonical.append(name)

    refs: list[tuple[dict, str]] = []  # (容器, 键)——直接改写引用处
    for bl in experiment_plan.get("baselines") or []:
        if isinstance(bl, dict) and isinstance(bl.get("metric_name"), str):
            refs.append((bl, "metric_name"))
    if isinstance(method_design, dict):
        for point in method_design.get("innovation_points") or []:
            if not isinstance(point, dict):
                continue
            em = point.get("evidence_metric")
            if isinstance(em, list):
                for entry in em:
                    if isinstance(entry, dict) and isinstance(
                        entry.get("metric_name"), str
                    ):
                        refs.append((entry, "metric_name"))
            elif isinstance(em, str) and em.strip():
                refs.append((point, "evidence_metric"))

    appended: list[str] = []
    renamed = 0
    for container, key in refs:
        raw = str(container[key]).strip()
        if not raw:
            continue
        sq = _metric_squash(raw)
        if sq in seen:
            canon = next(n for n in canonical if _metric_squash(n) == sq)
            if canon != raw:
                container[key] = canon
                renamed += 1
            continue
        seen.add(sq)
        canonical.append(raw)
        appended.append(raw)
        container[key] = raw
    experiment_plan["metrics"] = canonical
    if appended or renamed:
        log.info(
            f"[plan_experiment_with_data] 跨产物指标名对账: "
            f"并入 {len(appended)} 个被引用名 {appended[:6]}"
            f"{'…' if len(appended) > 6 else ''}；拼写归并 {renamed} 处"
        )


def _normalize_dataset_fields(experiment_plan: dict, data_plan: dict | None) -> None:
    """datasets[] 字段级归一化（就地改）。

    实测漂移（2026-09-10 run g）：模块三 DatasetSpec 硬要求非空 ``scale_estimate``，
    但 deepseek-v4-flash 把规模描述写进了自造键 ``size``
    （``"500 conversations, 2000 QA pairs"``），scale_estimate 留空——
    3 个数据集全中，模块三 pydantic 会直接拒收。内容明明给了，只是键名漂了，
    这里确定性搬过去，不必再烧一轮 planner（投影层原本只能塞占位符）。
    """
    for label, doc in (("experiment_plan", experiment_plan), ("data_plan", data_plan)):
        if not isinstance(doc, dict):
            continue
        for i, ds in enumerate(doc.get("datasets") or []):
            if not isinstance(ds, dict):
                continue
            scale = ds.get("scale_estimate")
            if isinstance(scale, str) and scale.strip():
                continue
            size = ds.get("size")
            if isinstance(size, str) and size.strip():
                ds["scale_estimate"] = size.strip()
                log.info(
                    f"[plan_experiment_with_data] {label}.datasets[{i}].size "
                    f"→ scale_estimate（{size.strip()[:50]!r}）"
                )


def _normalize_compute_estimate(experiment_plan: dict) -> None:
    """把 experiment_plan.compute_estimate 归一化成 number（防 float() 抛）。

    兼容 LLM 漂移：compute_estimate 偶尔返嵌套 dict
    ``{total_gpu_hours, tier, breakdown: {phase1, phase2, ...}}``。
    schema/validate_plan.py 期望 compute_estimate 是 number。**抽 total_gpu_hours**；
    抽不到就用 ``sum(breakdown.values())``；再抽不到给 0.0 占位。
    """
    ce = experiment_plan.get("compute_estimate")
    if isinstance(ce, (int, float)):
        return  # 已经合规
    if isinstance(ce, dict):
        total_h = ce.get("total_gpu_hours")
        if not isinstance(total_h, (int, float)):
            breakdown = ce.get("breakdown", {})
            if isinstance(breakdown, dict):
                total_h = sum(
                    v for v in breakdown.values()
                    if isinstance(v, (int, float))
                )
            else:
                total_h = 0.0
        experiment_plan["compute_estimate"] = float(total_h or 0.0)
        log.info(
            f"[plan_experiment_with_data] compute_estimate 嵌套 dict→抽 total 字段 "
            f"得 {experiment_plan['compute_estimate']} GPU-hours"
        )
        return
    # 缺失 / 异常类型 — 给 0 占位让下游能跑
    experiment_plan["compute_estimate"] = 0.0
    log.info(
        f"[plan_experiment_with_data] compute_estimate 异常类型 {type(ce).__name__}→0.0 兜底"
    )


# ════════════════════════════════════════════════════════════════
# ExperimentReview 归一化（2026-08-29 新增）
# ════════════════════════════════════════════════════════════════


def _normalize_critic_review(review: Any) -> dict:
    """把 experiment-critic LLM 的输出归一化成 ExperimentReview schema。

    兼容 deepseek-v4-flash 漂移：LLM 偶尔把整段反馈文本当 JSON 顶层 key 返
    （key 长度可达 3000+ 字符），导致 schema 字段（novelty_ok / adequacy_score /
    feedback_addressed / feasibility_risks）全缺失，仅留 `passed` 一个 bool。
    识别这种 "long-key 漂移" 后，从文本里抽结构化字段；抽不到的字段给兜底值
    （让 reflection loop 强制重写，不再因为 schema 字段缺失而误判通过）。

    兜底逻辑（让 reflection loop 不会因为 critic 输出坏而漏重写）：
    - novelty_ok: 抽不到 → False
    - adequacy_score: 抽不到 → 0.0（不通过 ADEQUACY_PASS 门禁）
    - feasibility_risks: 抽不到 → 至少 1 条 blocker 风险（强制重写）
    - feedback_addressed: 抽不到 → []（reflection loop 走 script_diff 兜底）
    - passed: 抽不到 → False（避免误判通过）

    Args:
        review: call_agent_async("experiment-critic", ...) 的返回值。

    Returns:
        归一化后的 ExperimentReview 风格 dict（schema 字段全在）。
    """
    if not isinstance(review, dict):
        log.info(
            f"[plan_experiment_with_data] experiment-critic 返非 dict 类型 "
            f"{type(review).__name__}→走 fallback"
        )
        return _fallback_critic_review(f"critic 返非 dict 类型: {type(review).__name__}")

    # 检测 long-key 漂移：LLM 把整段文本当 key 写进 JSON
    long_key_text = ""
    for k in list(review.keys()):
        if isinstance(k, str) and len(k) > 200:
            long_key_text = k
            del review[k]  # 移除异常 key
            break

    if not long_key_text:
        # 看起来结构正常——补齐缺失的必填字段
        return _fill_missing_critic_fields(review)

    # 从长文本里抽结构化字段
    log.info(
        f"[plan_experiment_with_data] experiment-critic 触发 long-key 漂移 "
        f"（key 长度={len(long_key_text)} chars）→ 文本抽结构化字段"
    )
    extracted = {
        "novelty_ok": _extract_novelty_ok(long_key_text),
        "feasibility_risks": _extract_feasibility_risks(long_key_text),
        "adequacy_score": _extract_adequacy_score(long_key_text),
        "feedback": long_key_text,
        "feedback_addressed": _extract_feedback_addressed(long_key_text),
        "passed": False,  # 下面根据 adequacy_score 重算
    }
    # passed 推导：adequacy ≥ ADEQUACY_PASS 且无 blocker 风险
    has_blocker = any(
        r.lower().startswith("blocker") for r in extracted["feasibility_risks"]
    )
    extracted["passed"] = (
        extracted["adequacy_score"] >= ADEQUACY_PASS and not has_blocker
    )
    return extracted


def _fill_missing_critic_fields(review: dict) -> dict:
    """critic 返 dict 但缺字段时补兜底值（防 reflection loop 误判通过）。"""
    if "novelty_ok" not in review:
        review["novelty_ok"] = False
    if "feasibility_risks" not in review or not review["feasibility_risks"]:
        review["feasibility_risks"] = [
            "blocker: critic 返 schema 缺 feasibility_risks（强制重写）"
        ]
    if "adequacy_score" not in review:
        review["adequacy_score"] = 0.0
    if "feedback" not in review:
        review["feedback"] = "（critic 返 schema 缺 feedback 字段）"
    elif not isinstance(review["feedback"], str):
        # run i：method-critic 实测把 feedback 返成 list；experiment 侧同族漂移
        review["feedback"] = coerce_feedback_text(review["feedback"])
    if "feedback_addressed" not in review:
        review["feedback_addressed"] = []
    if "passed" not in review:
        # 推导 passed
        has_blocker = any(
            r.lower().startswith("blocker")
            for r in review.get("feasibility_risks", [])
        )
        review["passed"] = (
            review["adequacy_score"] >= ADEQUACY_PASS and not has_blocker
        )
    return review


def _fallback_critic_review(reason: str) -> dict:
    """LLM 返非 dict / 极端漂移时返兜底 review（强制 reflection loop 重写）。"""
    return {
        "novelty_ok": False,
        "feasibility_risks": [f"blocker: critic LLM 输出解析失败：{reason}"],
        "adequacy_score": 0.0,
        "feedback": f"critic LLM 输出解析失败：{reason}；请重出",
        "feedback_addressed": [],
        "passed": False,
    }


def _extract_adequacy_score(text: str) -> float:
    """从 critic 文本里抽 adequacy_score（数字）。"""
    # 优先匹配 "adequacy_score: 7.0" / "adequacy score: 7.0" / "adequacy score=7.0"
    m = re.search(
        r"adequacy[_\s]*score\s*[:：=]\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE
    )
    if m:
        return float(m.group(1))
    # 中英文 "评分" / "分数"
    m = re.search(r"(?:评分|分数)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*/?\s*10?", text)
    if m:
        return float(m.group(1))
    return 0.0


def _extract_novelty_ok(text: str) -> bool:
    """从 critic 文本里抽 novelty_ok。"""
    m = re.search(
        r"novelty[_\s]*ok\s*[:：=]\s*(true|false|yes|no)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower() in ("true", "yes")
    # 中文："新颖性.*通过" / "整体新颖性: 是"
    if re.search(r"新颖性[^\n]{0,5}通过", text):
        return True
    if re.search(r"novelty.*?(true|通过)", text, re.IGNORECASE):
        return True
    return False


def _extract_feasibility_risks(text: str) -> list[str]:
    """从 critic 文本里抽 feasibility_risks（blocker/major/minor 条目）。

    兼容 4 种标记格式：
    - "[blocker] desc" / "【blocker】 desc"
    - "blocker: desc" / "blocker：desc"
    - "**风险** blocker: desc"（嵌套在标题下）
    """
    risks: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"[\[【]?\s*(blocker|major|minor)\s*[\]】:：]\s*([^\n]+)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        severity = m.group(1).lower()
        desc = m.group(2).strip()
        # 跳过表头 / 分隔符 / 标题类
        if not desc or desc.startswith("==") or desc.startswith("--"):
            continue
        # 截断防止超长（schema 期望 list[str]）
        if len(desc) > 200:
            desc = desc[:200] + "..."
        item = f"{severity}: {desc}"
        if item not in seen:
            seen.add(item)
            risks.append(item)
    return risks


def _extract_feedback_addressed(text: str) -> list:
    """从 critic 文本里抽 feedback_addressed 列表。

    启发式：找 "feedback_addressed: [...]" 段，能 parse 就 parse；parse 失败
    返空 list（reflection loop 会走 script_diff 兜底）。
    """
    # 启发式 1：显式 JSON-like 列表
    m = re.search(
        r"feedback_addressed\s*[:：]?\s*\[(.*?)\]",
        text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        try:
            items = json.loads("[" + m.group(1) + "]")
            if isinstance(items, list):
                return items
        except (json.JSONDecodeError, ValueError):
            pass
    return []


# 模块三 dataset_resolver 报落地页时的权威格式：
#   declared_source=declared URL is a paper/repository landing page, not a direct
#   dataset file: https://github.com/xiaowu0162/LongMemEval
# URL 主体只允许可打印 ASCII（``!``–``~``）：URL 里的非 ASCII 一律百分号编码，
# 所以一旦出现非 ASCII 就说明 URL 已经结束、后面是自然语言。blocker 是中英混排
# 的长句（``... https://parl.ai/projects/msc/（重复）``），用 ``\S+`` 会把「（重复）」
# 一起吃进来，剥离尾部标点也救不回夹在中间的汉字。
_URL_BODY = r"[!-~]+"
_DECLARED_SOURCE_URL_RE = re.compile(
    r"declared URL is a (?:paper/repository )?landing page[^:]*:\s*(https?://"
    + _URL_BODY
    + r")",
    re.IGNORECASE,
)
# 兜底：blocker 文本里出现的任意链接（suggested_changes 与检索错误里也可能带 URL）
_ANY_URL_RE = re.compile(r"https?://" + _URL_BODY)
# 从 URL 尾部剥掉的标点（blocker 是自然语言，URL 常被中英文标点包裹）。
# ⚠️ 不能剥 "/"——``parl.ai/projects/msc/`` 这类目录页 URL 会因此变形。
_URL_TRAILING_JUNK = ".,;:!?、。；：！？）)】]\"'"


def _extract_failed_source_urls(blockers: Any) -> list[str]:
    """从 planning_feedback.blockers 里抽出「已失败的数据源 URL」黑名单。

    模块三的 blocker 是自然语言字符串（``[data] 数据集X无法…``），URL 内嵌其中，
    没有结构化字段。这里做两级提取：优先匹配 dataset_resolver 的
    ``declared_source`` 权威格式，再兜底抓文本里所有 http(s) 链接。

    Returns:
        去重保序的 URL 列表；无 URL 或 blockers 非 list 时返回 ``[]``（无害降级，
        planner 仍会收到 blockers 原文）。
    """
    if not isinstance(blockers, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for blocker in blockers:
        if not isinstance(blocker, str) or not blocker:
            continue
        candidates = _DECLARED_SOURCE_URL_RE.findall(blocker)
        if not candidates:
            candidates = _ANY_URL_RE.findall(blocker)
        for raw in candidates:
            url = raw.rstrip(_URL_TRAILING_JUNK)
            key = url.casefold()
            if url and key not in seen:
                seen.add(key)
                found.append(url)
    return found


def _build_replan_data_feedback(planning_feedback: dict) -> tuple[str, list[str]]:
    """把模块三 PlanningFeedback 摊成喂给 experiment-planner 的 previous_feedback。

    REPLAN_DATA 从「逐字复用上一轮 plan」改为「带反馈重跑 planner」后，模块三的
    blockers / suggested_changes 是唯一权威问题描述，必须逐条原文透传（不截断、
    不改写）——它们决定 planner 该换哪个数据集。

    Returns:
        ``(feedback_text, forbidden_urls)``。forbidden_urls 同时作为独立 payload 键
        传给 planner（MUST 段要求禁止复用），并在文本里再列一遍做双保险。
    """
    reason = str(planning_feedback.get("reason") or "").strip()
    affected = planning_feedback.get("affected_experiment_ids") or []
    blockers = planning_feedback.get("blockers") or []
    suggested = planning_feedback.get("suggested_changes") or []
    forbidden_urls = _extract_failed_source_urls(blockers)

    lines: list[str] = [
        "[模块三 PlanningFeedback（权威，REPLAN_DATA 轮）]",
        f"原因: {reason or '(未提供)'}",
    ]
    if isinstance(affected, list) and affected:
        lines.append("受影响实验: " + ", ".join(str(x) for x in affected))
    lines.append("blockers（必须逐条回应，处理结论写进 feedback 字段）:")
    if isinstance(blockers, list) and blockers:
        lines.extend(f"- {b}" for b in blockers)
    else:
        lines.append("- (未提供)")
    lines.append("suggested_changes（模块三权威建议，逐条评估采纳与否并说明）:")
    if isinstance(suggested, list) and suggested:
        lines.extend(f"- {s}" for s in suggested)
    else:
        lines.append("- (未提供)")
    if forbidden_urls:
        lines.append("禁止复用以下失败 URL（出现在黑名单中的 source_url 一律不可再写）:")
        lines.extend(f"- {u}" for u in forbidden_urls)
    return "\n".join(lines), forbidden_urls


def _candidate_is_verified(candidate: dict) -> bool:
    """这条候选的 download_urls 是否真的被模块三验证过存在（见下）。

    判定顺序：
      1. 显式 ``verified`` 字段（模块三 2026-09-17 起写）：False 直接排除；
      2. 旧产物没有该字段 → 按 provider 兜底：``module2`` 是模块二声明的 URL
         **原样透传**，模块三从没联网查过它是否存在（不做存在性校验是有意设计），
         所以不能当"已验证"回喂。
    """
    if candidate.get("verified") is False:
        return False
    if "verified" in candidate:
        return True
    return str(candidate.get("provider") or "").strip().casefold() != "module2"


def load_resolved_download_hints(source_res_dir: Any) -> dict[str, list[str]] | None:
    """读取模块三 source-resolution 审计文件，抽**已验证存在**的候选下载直链。

    paper-gen 编排器在构造 REPLAN bundle 时尽力而为地拷入模块三
    ``run/data/source-resolution/*.json``（见 ``_build_replan_bundle``）。
    这里把 ``status ∈ {RESOLVED, SUBSTITUTE_AVAILABLE}`` 里的候选 URL 提成
    ``{数据集名: [url, ...]}`` 随 REPLAN_DATA payload 下发给 planner 参考。

    ⚠️ 只回喂 ``_candidate_is_verified`` 认过的候选。历史事故（2026-09-17）：
    ``provider="module2"`` 的透传候选也被当成"模块三已验证直链"回喂，于是
    planner 上轮**自己猜的**那条 404 地址被原样端回去、还附赠一句"已验证"——
    它自然继续照抄同类路径，三轮 REPLAN 全撞在同一个 404 上。没有验证过的话
    宁可不说，这比说错强。

    防御式设计：目录不存在 / 文件损坏 / 结构不符 → 返回 None，planner 照常工作。
    **不 import 模块三任何代码**，只按 JSON 结构读取，避免 skill 间硬耦合。

    Args:
        source_res_dir: ``.../source_resolution`` 目录（str 或 Path）；空值返回 None。

    Returns:
        ``{query: [download_url, ...]}``（每数据集最多 MAX_RESOLVED_FILE_HINTS 个）；
        无可用条目时返回 None。
    """
    if not source_res_dir:
        return None
    try:
        directory = Path(source_res_dir)
        if not directory.is_dir():
            return None
        hints: dict[str, list[str]] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status") or "").upper()
            if status not in {"RESOLVED", "SUBSTITUTE_AVAILABLE"}:
                continue
            query = str(payload.get("query") or "").strip()
            if not query:
                continue
            urls: list[str] = []
            for candidate in payload.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                if not _candidate_is_verified(candidate):
                    continue
                for url in candidate.get("download_urls") or []:
                    if isinstance(url, str) and url and url not in urls:
                        urls.append(url)
                if len(urls) >= MAX_RESOLVED_FILE_HINTS:
                    break
            if urls:
                hints[query] = urls[:MAX_RESOLVED_FILE_HINTS]
        return hints or None
    except OSError:
        return None


async def plan_experiment_with_data_async(
    inputs: dict,
    method_design: dict,
    method_review: dict,
    replan_data_only: bool = False,
    replan_only_stage2: bool = False,
    planning_feedback: dict | None = None,
    prev_experiment_plan: dict | None = None,
    previous_feedback: str = "",
    max_reviewer_rounds: int = DEFAULT_MAX_REVIEWER_ROUNDS,
    *,
    contract_ledger: dict | None = None,
    run_legacy_critic: bool = True,
    experiment_planner_holder: dict | None = None,
    experiment_critic_holder: dict | None = None,
    resolved_download_hints: dict | None = None,
) -> tuple[dict, dict, dict]:
    """
    规划实验方案，并从实验方案抽数据订单（异步版本）。

    步骤:
        1. 拼 planner payload（inputs + method_design + method_review + [planning_feedback] + [previous_feedback]）
        2. 拉 planner 出 ``{"experiment_plan": ..., "data_plan": ...}`` 包装 dict；
           解包取 ``experiment_plan``（REPLAN_DATA 时用 prev_experiment_plan，跳过 LLM）
        3. 字段契约校验（不阻塞——warn only）
        4. 归一化 compute_estimate
        5. **反思循环**（2026-08-29 新增）：调 experiment-critic 评 adequacy + feedback_addressed
           → 通过且无 unaddressed → 跳出；否则把反馈拼回 planner 重写（最多 max_reviewer_rounds 轮）
        6. 确定性从 experiment_plan 抽 data_plan（v2 重构：3 个 NLP 字段降为可选）:
            - datasets 照搬
            - usage_plan 由 datasets[].usage 拼接（v2 必填，新增）
            - split_strategy 仅在 datasets 暗示需要切分时包含（启发式关键词检测）
            - preprocessing_pipeline 仅在任一 dataset 有非空 preprocess_required 时包含
            - expected_size 仅在能合理估算时（rows>0 或 gpu>0）包含

    Args:
        inputs:              8 个输入的 dict（含完整 references）。
        method_design:       第 1 阶段产出的方法设计 dict；REPLAN_DATA 时可为空 {}。
        method_review:       第 1 阶段产出的评审 dict；REPLAN_DATA 时可为空 {}。
        replan_data_only:    True 且带 planning_feedback 时，带模块三反馈重跑 planner
                            重新规划数据源（schemas §1.5.1 REPLAN_DATA）；无 feedback 的
                            旧调用保持「逐字复用 prev_experiment_plan」兼容语义。
        planning_feedback:   可选，模块三的 PlanningFeedback。REPLAN_DATA 下它是重跑
                            planner 的触发条件与权威问题描述（blockers /
                            suggested_changes / 失败 URL 黑名单）。
        prev_experiment_plan:REPLAN 时传上一轮产物：REPLAN_DATA 下作为 planner 的参考
                            上下文（仍会被整体重写）；REPLAN_ONLY_STAGE2/ONLY_STAGE3
                            下直接复用。
        previous_feedback:   可选，人工审核反馈；非空时拼进 planner payload 让 LLM 定向重写
                            （与 iterate_method_design.previous_feedback 行为一致）。
        max_reviewer_rounds: experiment-critic 反思循环最大轮数（默认 2，与 method_design 一致）。
                            REPLAN_DATA 与 REPLAN_ONLY_STAGE2 下限制为 1 轮。
        resolved_download_hints: 可选，模块三 source-resolution 里已验证的候选直链
                            （``数据集名 -> [url, ...]``），随 REPLAN_DATA payload 下发给
                            planner 作参考。默认 None 表示无提示。

    Returns:
        (experiment_plan, data_plan, experiment_review):
            experiment_plan:  实验方案 dict（[planning-schemas.md §2.3](../references/planning-schemas.md)）。
            data_plan:        数据订单 dict（[planning-schemas.md §2.4](../references/planning-schemas.md)）。
            experiment_review: experiment-critic 反思循环的最终 review dict（feedback_addressed[] 等）。
                              REPLAN_DATA 模式或 max_reviewer_rounds=0 时返回空 dict。
    """
    # 1. 拼 planner payload
    planner_payload: dict = {
        **inputs,
        "method_design": method_design,
        "method_review": method_review,
    }
    if contract_ledger:
        # 不可行时由共同审查者裁决；planner 不得静默改变账本中的命名。
        planner_payload["contract_ledger"] = contract_ledger
    if planning_feedback:
        planner_payload["planning_feedback"] = planning_feedback
    if previous_feedback:
        planner_payload["previous_feedback"] = previous_feedback

    # 2. 拉 planner 出包装 dict。
    #    REPLAN_DATA：有模块三反馈时带反馈重跑 planner（见下）；无反馈时保持旧语义。
    #    REPLAN_ONLY_STAGE2：复用上一轮 plan + 仅 1 轮 critic。
    skip_reflection = False
    if replan_data_only:
        if not prev_experiment_plan:
            raise ValueError(
                "REPLAN_DATA 路径必须传 prev_experiment_plan（上一轮 experiment_plan.json）"
            )
        if not planning_feedback:
            # 兼容旧调用（无模块三反馈的 replan_data_only）：保持逐字复用语义。
            log.info(
                "[plan_experiment_with_data] REPLAN_DATA 模式：无 planning_feedback，"
                "逐字复用上一轮 experiment_plan"
            )
            experiment_plan = dict(prev_experiment_plan)
            skip_reflection = True
        else:
            # 有模块三反馈时必须重跑 planner。历史 bug：此处曾直接
            # ``dict(prev_experiment_plan)`` 逐字复用 + 跳过反思循环，导致
            # [data] blocker 里的数据源 URL 三轮 REPLAN 一字不变、同样的 blocker
            # 必然复现，自动重试永远无法自愈（白烧 max_experiment_replans 轮后
            # 顶层终态 waiting_for_experiment_replan）。
            replan_text, forbidden_urls = _build_replan_data_feedback(planning_feedback)
            blockers = planning_feedback.get("blockers") or []
            replan_payload = {
                **planner_payload,
                "previous_feedback": replan_text,
                # 上一轮完整方案仅作参考：让 planner 知道受影响实验与既有数据集契约，
                # 允许整体替换 datasets / source_url / 矩阵行。
                "previous_experiment_plan": prev_experiment_plan,
                # 独立键 + 文本双通道下发失败 URL 黑名单
                "forbidden_source_urls": forbidden_urls,
            }
            if resolved_download_hints:
                # 模块三已解析出的候选直链（若 bundle 携带），供 planner 优先复用
                replan_payload["resolved_download_hints"] = resolved_download_hints
            log.info(
                "[plan_experiment_with_data] REPLAN_DATA 模式：带模块三反馈重跑 planner"
                "（blockers=%d 条，禁止复用 URL=%d 个%s）",
                len(blockers) if isinstance(blockers, list) else 0,
                len(forbidden_urls),
                "，含已验证候选直链提示" if resolved_download_hints else "",
            )
            combined = await call_session_async(
                "experiment-planner", replan_payload,
                phase="实验规划-REPLAN_DATA",
                session_holder=experiment_planner_holder,
                instructions=(
                    "你是 experiment-planner 阶段会话：只设计实验 + 数据方案；"
                    "本轮需按模块三 REPLAN 反馈重选数据源；不可见方法阶段反馈。"
                ),
            )
            combined = _normalize_planner_output(combined, method_design)
            if "experiment_plan" in combined:
                experiment_plan = combined["experiment_plan"]
            else:
                # 兜底：LLM 返了 dict 但缺包装键（schema 漂移）。回退上一轮 plan
                # 而非 raise——回退后扩展过的门禁 1b 会继续触发数据源重选循环
                # （最多 3 次）与 Layer A 补写轮，比直接中断整轮 run 多几次补救机会。
                # 注：call_session_async 自身彻底失败会抛 RuntimeError 向上传播，
                # 那属不可恢复，显式失败不在此兜底。
                log.warning(
                    "[plan_experiment_with_data] REPLAN_DATA planner 返回缺 "
                    "'experiment_plan' 键（keys=%s），回退上一轮 plan；"
                    "后续门禁将继续触发数据源重选",
                    list(combined.keys()),
                )
                experiment_plan = dict(prev_experiment_plan)
            # 数据源自修复循环 + Layer A 照走；critic 限 1 轮（对齐 REPLAN_ONLY_STAGE2）
            skip_reflection = False
            max_reviewer_rounds = 1
    elif replan_only_stage2:
        # Phase 3 / Track C：method 类 REPLAN —— 复用 prev experiment_plan
        # 跳过 LLM planner，但**仍调 critic 1 次**评 adequacy
        # 防止新 method 跟旧 experiment 不适配（layer A schema gate 不够，需 critic 语义判断）
        if not prev_experiment_plan:
            raise ValueError(
                "REPLAN_ONLY_STAGE2 路径必须传 prev_experiment_plan（上一轮 experiment_plan.json）"
            )
        log.info(
            "[plan_experiment_with_data] REPLAN_ONLY_STAGE2 模式："
            "复用上一轮 experiment_plan + 仅 1 critic adequacy 评"
        )
        experiment_plan = dict(prev_experiment_plan)
        # 不跳反思循环——调 critic 1 次（max_reviewer_rounds=1）
        # 但仍跳过 Layer A（prev plan 已经过之前的 Layer A 校验，prev path 不会再触发）
        skip_reflection = False
        # 限制反思循环最多 1 轮
        max_reviewer_rounds = 1
    else:
        combined = await call_session_async(
            "experiment-planner", planner_payload, phase="实验规划",
            session_holder=experiment_planner_holder,
            instructions="你是 experiment-planner 阶段会话：只设计实验 + 数据方案；可见实验人审 modify 反馈；不可见方法阶段反馈。",
        )
        # 兼容 deepseek-v4-flash 偶尔漏外层包装——自动包一层。
        combined = _normalize_planner_output(combined, method_design)
        if "experiment_plan" not in combined:
            raise RuntimeError(
                f"experiment-planner 返回缺少 'experiment_plan' 键，实际 keys={list(combined.keys())}"
            )
        experiment_plan = combined["experiment_plan"]

    # 3. 字段契约校验（不阻塞——warn only；详细在校验脚本 validate_plan.py 跑）
    ep_errs = validate_experiment_plan(experiment_plan)
    for e in ep_errs:
        log.info(f"[plan_experiment_with_data] experiment_plan 校验警告: {e}")

    # 4. 字段归一化（防下游 check_feasibility_and_downgrade.py:84
    #    ``float(experiment_plan.get("compute_estimate", 0.0))`` 抛 TypeError）
    _normalize_compute_estimate(experiment_plan)

    # 5. 反思循环（2026-08-29 新增，最多 max_reviewer_rounds 轮）
    experiment_review: dict = {}
    prev_experiment_plan_for_diff: dict | None = None  # 2026-08-29 新增：脚本 diff 兜底
    planner_replan_count = 0  # Phase 2 / Track B：Layer A 触发的 planner 重写计数
    if not skip_reflection:
        # 数据源专项自修复：论文页/仓库落地页/失效占位不能流入模块三。最多连续重写
        # 3 次，每次都要求整体更新 dataset、matrix、baseline 与 success criteria，
        # 防止只换 URL 造成数据契约冒名。真实下载失败后的 PlanningFeedback 也会
        # 经同一 previous_feedback 路径回到这里。
        # 过滤条件只认 "source_url=" 前缀（门禁 1b 的 gap 专属），不再匹配
        # "论文落地页" 字面量——门禁 1b 扩展后文案已改为「论文/仓库落地页」，
        # 旧字面量会漏掉 GitHub 仓库页/目录页产生的 gap。其他 gap（paper_id
        # 幻觉、指标覆盖率等）不带该前缀，不会误触发数据源重选。
        for _data_round in range(3):
            source_gaps = [
                gap for gap in hard_gate_experiment_plan(
                    experiment_plan, inputs, method_design
                )
                if "source_url=" in gap
            ]
            if not source_gaps:
                break
            gap_feedback = format_gaps_as_feedback(
                source_gaps,
                f"experiment_plan 数据源重选 {_data_round + 1}/3",
            )
            combined = await call_session_async(
                "experiment-planner",
                {**planner_payload, "previous_feedback": gap_feedback},
                phase="实验规划-数据源重选",
                session_holder=experiment_planner_holder,
            )
            combined = _normalize_planner_output(combined, method_design)
            if "experiment_plan" not in combined:
                continue
            experiment_plan = combined["experiment_plan"]
            _normalize_compute_estimate(experiment_plan)
            planner_replan_count += 1

        # 5a. Layer A 确定性门禁（Phase 2 / Track B，0 LLM 调用）
        # 防 LLM 把 baseline.paper_id 编了假 / metric 覆盖率 < 70% / compute_estimate 返字符串
        # 命中 → 让 planner 补 1 轮（不进入反思循环）
        for _layer_a_round in range(_LAYER_A_MAX_REWRITES):
            hard_gaps = hard_gate_experiment_plan(experiment_plan, inputs, method_design)
            if not hard_gaps:
                break
            gap_feedback = format_gaps_as_feedback(
                hard_gaps,
                f"experiment_plan Layer A 补齐 {_layer_a_round + 1}/{_LAYER_A_MAX_REWRITES}",
            )
            log.info(
                f"[plan_experiment_with_data] Layer A 命中 {len(hard_gaps)} 个 gap，"
                f"调 planner 补齐（第 {_layer_a_round + 1}/{_LAYER_A_MAX_REWRITES} 次）："
                f"{hard_gaps[:2]}{'...' if len(hard_gaps) > 2 else ''}"
            )
            combined = await call_session_async(
                "experiment-planner",
                {**planner_payload, "previous_feedback": gap_feedback},
                phase="实验规划",
                session_holder=experiment_planner_holder,
            )
            combined = _normalize_planner_output(combined, method_design)
            if "experiment_plan" not in combined:
                log.warning(
                    "[plan_experiment_with_data] Layer A 补齐轮 planner 未返回 "
                    "experiment_plan，保留当前计划"
                )
                break
            experiment_plan = combined["experiment_plan"]
            _normalize_compute_estimate(experiment_plan)
            planner_replan_count += 1

        # 剩余 gap 在 run_legacy_critic=False 时不进反思循环，会一路流到模块三投影
        # fail-closed（status=invalid → 顶层 aborted，且无 planning_feedback 回流），
        # 所以这里要留 error 级痕迹，别让终态只有一个语焉不详的 aborted。
        remaining_gaps = hard_gate_experiment_plan(experiment_plan, inputs, method_design)
        if remaining_gaps:
            log.error(
                "[plan_experiment_with_data] Layer A 重写 %d 次后仍剩 %d 个 gap：%s",
                _LAYER_A_MAX_REWRITES, len(remaining_gaps), remaining_gaps[:3],
            )

        if not run_legacy_critic:
            # 跨产物语义由 flow 中唯一的 planning-contract-critic 负责；此处不再
            # 用 experiment-critic 触发整份方案重写。Layer A 已给 planner 一次定向补齐。
            max_reviewer_rounds = 0

        for round_idx in range(1, max_reviewer_rounds + 1):
            # 保存本轮要评的 plan 为下轮脚本 diff 的 prev（与 method_design 对称）
            prev_experiment_plan_for_diff = experiment_plan
            log.info(
                f"[plan_experiment_with_data] experiment-critic 评 {round_idx}/{max_reviewer_rounds} 轮"
            )
            critic_payload = {
                "experiment_plan": experiment_plan,
                # 2026-09-09：critic 要做 method_design ↔ experiment_plan 的**语义**一致性
                # 检查（planning-schemas.md §5.2 S1–S8），比如"创新点写 precision@5、
                # success_criteria 写 recall@5"这类同量纲偷换——两者都在 metrics 里，
                # 任何词表校验都放行，只有看得懂语义才抓得住。看不到 method_design 就查不了。
                "method_design": method_design,
                "previous_experiment_review": experiment_review if experiment_review else None,
                "human_modify_feedback": previous_feedback,
                "planning_feedback": planning_feedback,
            }
            experiment_review = await call_session_async(
                "experiment-critic",
                critic_payload,
                phase="实验规划",
                session_holder=experiment_critic_holder,
                instructions="你是 experiment-critic 阶段会话：只评审实验 + 数据方案；可见实验人审 modify 反馈；不可见方法阶段反馈。",
            )
            # 归一化 critic 输出（2026-08-29 新增，防 deepseek-v4-flash long-key 漂移
            # —— LLM 偶尔把整段反馈文本当 JSON 顶层 key 返，导致 schema 字段全缺失）
            experiment_review = _normalize_critic_review(experiment_review)
            # 解析 + 通过判定
            passed = (
                experiment_review.get("passed")
                and experiment_review.get("adequacy_score", 0) >= ADEQUACY_PASS
            )
            unaddressed_count = _count_unaddressed_experiment(experiment_review)
            # 2026-08-29 新增：脚本 diff 硬兜底（与 method_design 对称）
            script_diff_unchanged = _script_diff_unchanged_count_experiment(
                prev_experiment_plan_for_diff, experiment_plan, experiment_review,
            )
            total_unaddressed = unaddressed_count + script_diff_unchanged
            if passed and total_unaddressed == 0:
                log.info(
                    f"[plan_experiment_with_data] experiment-critic 通过 "
                    f"(adequacy={experiment_review.get('adequacy_score')}, "
                    f"unaddressed=0)"
                )
                break
            if passed and total_unaddressed > 0:
                # adequacy ≥ 7 但有反馈未采纳——强制再写一轮（防走过场）
                log.info(
                    f"[plan_experiment_with_data] adequacy ≥ ADEQUACY_PASS 但 "
                    f"{total_unaddressed} 条反馈未采纳（LLM自查={unaddressed_count}, "
                    f"脚本diff={script_diff_unchanged}），强制再写一轮"
                )
            else:
                # 不通过——调 planner 重写
                log.info(
                    f"[plan_experiment_with_data] experiment-critic 不通过 "
                    f"(adequacy={experiment_review.get('adequacy_score')}, "
                    f"unaddressed={total_unaddressed})，调 planner 重写"
                )
            # 拼反馈回 planner payload
            unaddressed_block = _format_experiment_unaddressed_block(experiment_review)
            structured_block = _format_experiment_critic_feedback(experiment_review)
            prev_feedback_text = structured_block
            if unaddressed_block:
                prev_feedback_text = f"{prev_feedback_text}\n\n{unaddressed_block}"
            if planning_feedback:
                suggested = planning_feedback.get("suggested_changes") or []
                if suggested:
                    suggested_block = "\n".join(f"- {s}" for s in suggested)
                    prev_feedback_text = (
                        f"{prev_feedback_text}\n\n"
                        f"[REPLAN 来自模块三 PlanningFeedback]\n{suggested_block}"
                    )
            if previous_feedback:
                prev_feedback_text = (
                    f"{prev_feedback_text}\n\n[人审 modify 反馈]\n{previous_feedback}"
                )
            # 调 planner 重写
            planner_payload_next = {**planner_payload, "previous_feedback": prev_feedback_text}
            combined = await call_session_async(
                "experiment-planner", planner_payload_next, phase="实验规划",
                session_holder=experiment_planner_holder,
            )
            combined = _normalize_planner_output(combined, method_design)
            if "experiment_plan" not in combined:
                log.info(
                    f"[plan_experiment_with_data] planner 重写返回缺 'experiment_plan'，"
                    f" 实际 keys={list(combined.keys())}——保留上轮 plan"
                )
                continue
            experiment_plan = combined["experiment_plan"]
            _normalize_compute_estimate(experiment_plan)
            # 注意：不要在这里覆盖 prev_experiment_plan_for_diff！
            # 它在 iter 开头已被设成本轮 critic 看的版本（plan_vN）。
            # 下一轮 iter 开头 architect/planner 写完 plan_v{N+1} 后会再次覆盖。
            # 下一轮 iter 跑 script_diff(prev=plan_vN, curr=plan_v{N+1}, paths=本轮 review 的 field_path)
        else:
            # 全部 max_reviewer_rounds 轮用完仍不通过
            log.info(
                f"[plan_experiment_with_data] experiment-critic 评 {max_reviewer_rounds} 轮仍不通过，"
                f" 保留最后一轮 plan（adequacy={experiment_review.get('adequacy_score')}）"
            )

    # 6. 确定性抽 data_plan（不是 LLM）
    data_plan = _extract_data_order(experiment_plan)
    dp_errs = validate_data_plan(data_plan)
    for e in dp_errs:
        log.info(f"[plan_experiment_with_data] data_plan 校验警告: {e}")

    return experiment_plan, data_plan, experiment_review


# ════════════════════════════════════════════════════════════════
# 同步入口（保留给独立测试/单步调试用）
# ════════════════════════════════════════════════════════════════


def plan_experiment_with_data(
    inputs: dict,
    method_design: dict,
    method_review: dict,
    replan_data_only: bool = False,
    replan_only_stage2: bool = False,
    planning_feedback: dict | None = None,
    prev_experiment_plan: dict | None = None,
    previous_feedback: str = "",
    max_reviewer_rounds: int = DEFAULT_MAX_REVIEWER_ROUNDS,
    *,
    contract_ledger: dict | None = None,
    run_legacy_critic: bool = True,
    experiment_planner_holder: dict | None = None,
    experiment_critic_holder: dict | None = None,
    resolved_download_hints: dict | None = None,
) -> tuple[dict, dict, dict]:
    """同步包装：``asyncio.run(plan_experiment_with_data_async(...))``。

    workflow 调起路径请直接用 ``await plan_experiment_with_data_async(...)``。
    """
    return asyncio.run(plan_experiment_with_data_async(
        inputs, method_design, method_review,
        replan_data_only, replan_only_stage2,
        planning_feedback, prev_experiment_plan,
        previous_feedback, max_reviewer_rounds,
        contract_ledger=contract_ledger,
        run_legacy_critic=run_legacy_critic,
        experiment_planner_holder=experiment_planner_holder,
        experiment_critic_holder=experiment_critic_holder,
        resolved_download_hints=resolved_download_hints,
    ))


def _extract_data_order(experiment_plan: dict) -> dict:
    """
    从 experiment_plan 确定性抽取 data_plan（2026-08-29 v2 重构）。

    映射规则：
        - datasets: 照搬 experiment_plan.datasets
        - usage_plan (v2 必填): 由 datasets[].usage 字段拼接推导
                      （v1 三大 NLP 字段降为可选，新增此字段承载「数据怎么用」语义）
                      兜底：datasets 全无 usage 字段时，列出 datasets[].name
        - split_strategy (v2 ⬜): **仅当 datasets 暗示需要 train/val/test 切分时**包含
                      （启发式：usage / preprocess_required 含 split/train/val/test 关键词）
                      若包含则用默认值 {method: "ratio_8_1_1", ...}
        - preprocessing_pipeline (v2 ⬜): **仅当任一 dataset 有非空 preprocess_required 时**包含
                      （合并去重保序）
        - expected_size (v2 ⬜): 保留 planner 已明确声明的任务语义字段（尤其
                      task_type 及 query/candidate/relevance 字段），再确定性补齐
                     可估算的 rows / disk_gb / gpu_estimate

    v2 设计动机：旧版强制 3 个 NLP 专属字段，对非 NLP 方向（CV、RL、control、仿真等）
    不友好——split_strategy 在仿真里没意义、preprocessing_pipeline 在 RL 里没意义、
    expected_size 在「调 API」类研究里算不出。
    v2：3 字段降为可选；新增 usage_plan 必填承载「数据怎么用」语义。
    对应 schema：[planning-schemas.md §2.4](../references/planning-schemas.md)

    Args:
        experiment_plan: 第 2 阶段产出的实验方案 dict。

    Returns:
        data_plan dict（datasets + usage_plan 必有；其余 3 字段按需出现）。
    """
    datasets = experiment_plan.get("datasets") or []
    declared_data_plan = experiment_plan.get("data_plan")
    if not isinstance(declared_data_plan, dict):
        declared_data_plan = {}

    # 1. datasets 照搬（保序）
    out_datasets = list(datasets)

    # 2. usage_plan (v2 必填)：拼接 datasets[].usage
    usage_parts: list[str] = []
    fallback_names: list[str] = []
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        name = ds.get("name") or "未命名数据集"
        usage = ds.get("usage")
        if isinstance(usage, str) and usage.strip():
            usage_parts.append(f"- {name}: {usage.strip()}")
        else:
            fallback_names.append(name)
    if usage_parts:
        usage_plan = "\n".join(usage_parts)
    elif fallback_names:
        # 兜底：datasets 全无 usage 字段 → 列出 name 让 LLM 后续 modify 时补 usage
        usage_plan = "数据用途待补充（per-dataset usage 字段缺失）：\n" + "\n".join(
            f"- {n}" for n in fallback_names
        )
    else:
        usage_plan = ""

    result: dict = {
        "datasets": out_datasets,
        "usage_plan": usage_plan,
    }

    # 3. split_strategy (v2 ⬜)：优先保留 planner 给出的结构化声明；缺失时才按
    # datasets 做启发式补齐。自然语言字符串不能直接冒充模块三要求的 dict。
    declared_split = declared_data_plan.get("split_strategy")
    if isinstance(declared_split, dict) and declared_split:
        result["split_strategy"] = dict(declared_split)
        if "seed" not in result["split_strategy"]:
            result["split_strategy"]["seed"] = 42
    elif _detect_needs_split(datasets):
        result["split_strategy"] = {
            "method": "ratio_8_1_1",
            "train": 0.8,
            "val": 0.1,
            "test": 0.1,
            "seed": 42,
            "kwargs": {},
        }

    # 4. preprocessing_pipeline (v2 ⬜)：先保留 planner 的结构化列表，再合并
    # datasets[].preprocess_required，去重保序。
    seen: set[str] = set()
    pipeline: list[str] = []
    declared_pipeline = declared_data_plan.get("preprocessing_pipeline")
    if isinstance(declared_pipeline, list):
        for step in declared_pipeline:
            if isinstance(step, str) and step.strip() and step.strip() not in seen:
                normalized_step = step.strip()
                seen.add(normalized_step)
                pipeline.append(normalized_step)
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        for step in ds.get("preprocess_required") or []:
            if not isinstance(step, str):
                continue
            normalized_step = step.strip()
            if normalized_step and normalized_step not in seen:
                seen.add(normalized_step)
                pipeline.append(normalized_step)
    if pipeline:
        result["preprocessing_pipeline"] = pipeline

    # 5. expected_size (v2 ⬜)：planner 明确声明的内容必须保留。这里不能只重建
    # rows/disk/gpu，否则 retrieval 等任务的 task_type 与字段映射会被静默删除。
    declared_expected = declared_data_plan.get("expected_size")
    expected_size = dict(declared_expected) if isinstance(declared_expected, dict) else {}

    # 兼容 planner 把 task_type/字段映射放在 datasets[] 的旧输出。仅当所有非空值
    # 完全一致时搬运；冲突时不猜，由下游契约明确 REPLAN。
    semantic_keys = (
        "task_type", "label_column",
        "query_field", "query_id_field",
        "candidate_field", "candidate_id_field", "corpus_field",
        "relevance_field", "ground_truth_field",
        "question_field", "answer_field",
    )
    for key in semantic_keys:
        if key in expected_size and expected_size[key] not in (None, ""):
            continue
        values = {
            str(ds[key]).strip()
            for ds in datasets
            if isinstance(ds, dict) and ds.get(key) not in (None, "")
        }
        if len(values) == 1:
            expected_size[key] = next(iter(values))

    # Planner 有时即使收到定向反馈仍漏掉 task_type。若整组指标只落入一个任务族，
    # 该值可由指标计算契约唯一确定；在这里补齐，避免同一 schema blocker 空转三轮。
    # 指标族混合或只有 accuracy 等歧义指标时保持缺失，继续由硬门禁/模块三 REPLAN。
    declared_dataset_tasks = {
        str(dataset.get("task_type")).strip().casefold()
        for dataset in datasets
        if isinstance(dataset, dict) and str(dataset.get("task_type") or "").strip()
    }
    if (
        not str(expected_size.get("task_type") or "").strip()
        and not declared_dataset_tasks
    ):
        inferred_task = _infer_unique_task_type_from_metrics(experiment_plan.get("metrics"))
        if inferred_task is not None:
            expected_size["task_type"] = inferred_task
            for dataset in out_datasets:
                if isinstance(dataset, dict) and not str(dataset.get("task_type") or "").strip():
                    dataset["task_type"] = inferred_task

    total_rows = 0
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        total_rows += _parse_scale_to_rows(str(ds.get("scale_estimate", "")))
    # 粗略：每行 ~1KB 文本数据（保守默认，CV/NLP 通用兜底）
    disk_gb = round(total_rows * 1024 / 1e9, 2)
    # gpu_estimate 跟 experiment_plan.compute_estimate 对齐（数据准备也要算力）
    gpu_estimate = float(experiment_plan.get("compute_estimate", 0.0) or 0.0)
    if total_rows > 0:
        expected_size.setdefault("rows", total_rows)
        expected_size.setdefault("disk_gb", disk_gb)
    if gpu_estimate > 0:
        expected_size.setdefault("gpu_estimate", gpu_estimate)
    if expected_size:
        result["expected_size"] = expected_size

    return result


def _detect_needs_split(datasets: list) -> bool:
    """启发式：datasets 是否暗示需要 train/val/test 切分（2026-08-29 v2 新增）。

    检查 usage / preprocess_required 字段中是否含 split/train/val/test 关键词
    （含中英文：`split` / `train` / `val` / `test` / `划分` / `切分` /
    `训练集` / `测试集` / `验证集`）。
    命中则视为该研究方向需要切分，data_plan 携带 split_strategy 字段。
    """
    keywords = (
        "split", "train", "val", "test",
        "划分", "切分", "训练集", "测试集", "验证集",
    )
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        text_blobs: list[str] = []
        usage = ds.get("usage")
        if isinstance(usage, str):
            text_blobs.append(usage)
        for step in ds.get("preprocess_required") or []:
            if isinstance(step, str):
                text_blobs.append(step)
        for blob in text_blobs:
            lower = blob.lower()
            if any(kw in lower for kw in keywords):
                return True
    return False


# 单位 → 乘数（用于 scale_estimate 解析）
_SCALE_UNIT_MULTIPLIER = {
    "k": 1_000,
    "m": 1_000_000,
    "万": 10_000,
    "亿": 100_000_000,
}
# 匹配模式：数字（可小数） + 可选单位（k/m/万/亿）
_SCALE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM万億亿]?)")


def _parse_scale_to_rows(scale: str) -> int:
    """
    解析 scale_estimate 字符串为估算行数。

    支持的格式（大小写不敏感）：
        - "10k" / "10K" → 10000
        - "1.5M" → 1500000
        - "2万" → 20000
        - "1000" → 1000
        - 其他（含中文、不可解析）→ 0

    Args:
        scale: 数据集规模字符串（来自 DatasetSpec.scale_estimate）。

    Returns:
        估算行数（int）。无法解析时返回 0。
    """
    if not scale:
        return 0
    m = _SCALE_RE.search(scale.strip().lower())
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    multiplier = _SCALE_UNIT_MULTIPLIER.get(unit, 1)
    return int(num * multiplier)


# ════════════════════════════════════════════════════════════════
# 反思循环 helpers（2026-08-29 新增，对称 experiment-critic / method-critic）
# ════════════════════════════════════════════════════════════════


def _count_unaddressed_experiment(experiment_review: dict) -> int:
    """统计 experiment-critic 返回的 feedback_addressed 中 addressed=False 的条数。

    对应 method-critic 的 _count_unaddressed。LLM 漂移（feedback_addressed 非 list）→ 0。
    """
    addressed = experiment_review.get("feedback_addressed")
    if not isinstance(addressed, list):
        return 0
    return sum(
        1 for a in addressed
        if isinstance(a, dict) and not a.get("addressed")
    )


def _format_experiment_unaddressed_block(experiment_review: dict) -> str:
    """把 unaddressed 项的 evidence + field_path 拼成单块文本。

    对应 method-critic 的 _format_unaddressed_block。architect 据此定向重写
    哪个字段、改成什么值。
    """
    addressed = experiment_review.get("feedback_addressed")
    if not isinstance(addressed, list):
        return ""
    unaddressed = [
        a for a in addressed
        if isinstance(a, dict) and not a.get("addressed")
    ]
    if not unaddressed:
        return ""
    lines: list[str] = []
    for a in unaddressed:
        issue = str(a.get("old_issue", "未指定 issue"))
        field_path = str(a.get("field_path", "未指定字段路径"))
        evidence = str(a.get("evidence", "未指定 evidence"))
        lines.append(f"  - 字段 `{field_path}`: {issue}\n    当前值: {evidence}")
    return "[experiment-critic 反馈未采纳]\n" + "\n".join(lines)


def _format_experiment_critic_feedback(experiment_review: dict) -> str:
    """把 experiment-critic 的结构化反馈（feasibility_risks / missing_*）拼成单块文本。

    仿 iterate_method_design._format_critic_structured_feedback 的逻辑，但适配
    experiment-critic 的字段（missing_baselines / missing_metrics / overlap_baselines）。
    """
    blocks: list[str] = []

    # 1. 可行性风险（按 severity 分块）
    risks = experiment_review.get("feasibility_risks") or []
    if isinstance(risks, list) and risks:
        by_sev: dict[str, list[str]] = {"blocker": [], "major": [], "minor": []}
        for r in risks:
            if not isinstance(r, str):
                continue
            r_strip = r.strip()
            for sev in by_sev:
                if r_strip.lower().startswith(sev + ":"):
                    by_sev[sev].append(r_strip)
                    break
        for sev in ("blocker", "major", "minor"):
            if by_sev[sev]:
                lines = "\n".join(f"  - {r}" for r in by_sev[sev])
                blocks.append(f"[experiment-critic {sev} 级风险]\n{lines}")

    # 2. 缺失 baseline / metric（指向 experiment_plan 字段路径）
    missing_bl = experiment_review.get("missing_baselines") or []
    if isinstance(missing_bl, list) and missing_bl:
        bl_list = ", ".join(str(b) for b in missing_bl if b)
        if bl_list:
            blocks.append(
                f"[experiment-critic 缺失 baseline] {bl_list}\n"
                f"  → 必须在 baselines[] 补齐（fields: name/paper_id/"
                f"metric_name/expected_performance）"
            )
    missing_m = experiment_review.get("missing_metrics") or []
    if isinstance(missing_m, list) and missing_m:
        m_list = ", ".join(str(m) for m in missing_m if m)
        if m_list:
            blocks.append(
                f"[experiment-critic 缺失 metric] {m_list}\n"
                f"  → 必须在 metrics[] 补齐，并在 success_criteria[] 给阈值"
            )

    # 3. 撞车 baseline
    overlap = experiment_review.get("overlap_baselines") or []
    if isinstance(overlap, list) and overlap:
        ov_list = ", ".join(str(p) for p in overlap if p)
        if ov_list:
            blocks.append(
                f"[experiment-critic 撞车 baseline] {ov_list}\n"
                f"  → 在 experiment_plan 中显式说明与上述 baseline 的差异化"
            )

    # 4. novelty_ok=False 单独提示
    if experiment_review.get("novelty_ok") is False:
        blocks.append(
            "[experiment-critic 整体新颖性未通过] novelty_ok=false\n"
            "  → 必须重写至少 1 条 baseline 选型让差异化清晰可证伪"
        )

    # 5. feedback 原文（兜底；list/dict 漂移先归一成 str，否则静默丢反馈）
    fb = coerce_feedback_text(experiment_review.get("feedback", ""))
    if fb.strip():
        blocks.append(f"[experiment-critic 总体反馈]\n{fb.strip()}")

    return "\n\n".join(b for b in blocks if b)


# ════════════════════════════════════════════════════════════════
# 脚本 diff 硬兜底（experiment 版本，与 iterate_method_design 对称）
# ════════════════════════════════════════════════════════════════


def _script_diff_unchanged_count_experiment(
    prev_plan: dict | None,
    curr_plan: dict,
    review: dict,
) -> int:
    """用 _experiment_diff.script_diff_check 硬兜底（experiment 版本）。

    与 iterate_method_design._script_diff_unchanged_count 完全对称：
    即使 LLM 自查说"已采纳"，脚本对比值没变 → 强制重写。

    Args:
        prev_plan: 上一轮 experiment_plan（首轮为 None，跳过 diff）
        curr_plan: 本轮 experiment_plan
        review: experiment-critic 本轮的 review（用于抽 field_path）

    Returns:
        脚本检测到 unchanged + missing 的总条数。prev=None 时返回 0（首轮无从对比）。
    """
    if prev_plan is None:
        return 0
    from scripts._experiment_diff import (
        script_diff_check,
        extract_field_paths_from_addressed,
    )
    field_paths = extract_field_paths_from_addressed(review.get("feedback_addressed"))
    if not field_paths:
        return 0
    result = script_diff_check(prev_plan, curr_plan, field_paths)
    unchanged_n = len(result["unchanged"])
    missing_n = len(result["missing"])
    if unchanged_n or missing_n:
        log.info(
            f"[plan_experiment_with_data] 脚本 diff 兜底：{unchanged_n} unchanged + "
            f"{missing_n} missing（field paths={field_paths}）"
        )
    return unchanged_n + missing_n
