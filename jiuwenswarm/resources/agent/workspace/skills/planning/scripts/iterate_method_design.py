# -*- coding: utf-8 -*-
"""
iterate_method_design.py — 第 1 阶段：反复设计方法直到通过评审

按 [workflows/planning.md](../workflows/planning.md) Phase A 实现。
通过 [_subagent.py](_subagent.py) → facade.agent() 拉起 Architect 和 Critic 两个 sub-agent。
"""

from __future__ import annotations

import logging

from scripts._subagent import call_session_async, coerce_feedback_text
from scripts._hard_gates import (
    hard_gate_method_design,
    format_gaps_as_feedback,
)
from scripts.validate_plan import validate_method_design, validate_method_review

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.iterate_method_design")

# 评审门禁常量（见 [planning-schemas.md §3](../references/planning-schemas.md)）
# Phase 2 (Track B 反思合并)：从 3 降到 2，与 persona "modify 最多 2 轮" 对齐
# 节省：单 stage 反思 LLM call 从最多 6 砍到最多 4
DEFAULT_MAX_METHOD_ROUNDS = 2
ADEQUACY_PASS = 7.0


async def iterate_method_design_async(
    inputs: dict,
    max_rounds: int = DEFAULT_MAX_METHOD_ROUNDS,
    planning_feedback: dict | None = None,
    previous_feedback: str = "",
    *,
    contract_ledger: dict | None = None,
    run_legacy_critic: bool = True,
    method_designer_holder: dict | None = None,
    method_critic_holder: dict | None = None,
    debug_dump_path: str | None = None,
) -> tuple[dict, dict, int]:
    """
    反复设计方法，直到评审通过或达到最大轮数（异步版本）。

    步骤:
        1. 拉 architect 出方法初稿（payload = inputs + planning_feedback_suggested_changes + [previous_feedback]）
        2. 循环（最多 MAX_METHOD_ROUNDS 轮）:
            a. 拉 critic 评当前草稿
            b. 若 method_review.passed 且 adequacy_score ≥ ADEQUACY_PASS——跳出
            c. 否则把 method_review.feedback 拼 PlanningFeedback.suggested_changes 回 payload 调 architect 重写
        3. 兜底：检查 method.hypothesis_coverage 是否覆盖 inputs["hypotheses"][].id 全部
        4. 有漏——把缺失列表喂回 architect 补 1 轮

    REPLAN 路径（schemas §5「MethodReview → PlanningFeedback 闭环」）：
        当 planning_feedback 给定时：
            - 初稿 prompt 额外带 planning_feedback.reason / suggested_changes
            - 每轮 feedback 拼接：上一轮 MethodReview.feedback + PlanningFeedback.suggested_changes

    人审 modify 路径（[workflows/planning.md §Human Review](../workflows/planning.md)）：
        当 previous_feedback 非空时（main.py 收集 stdin 反馈）—— 把它拼在初稿 prompt 头部，
        architect 据此定向重写（与 critic_feedback 行为一致）。

    Session 化（2026-08-29 人审改造）：
        method-designer / method-critic 改成 ``call_session_async``，业务层在循环外
        持 session_holder（dict），跨轮复用同一 AgentSession。session history 自动累积，
        第 2 轮起 LLM 看到自己上轮的 draft / review，无需重塞完整 prompt。
        - ``method_designer_holder``: method-designer session 复用容器（外部传 None 时本函数内部新建）
        - ``method_critic_holder``:   method-critic session 复用容器（同上）

    Args:
        inputs:            8 个输入的 dict（load_inputs 返回的 payloads，含 references）。
        max_rounds:        反思最大轮数（默认 3，CLI 通过 --max-method-rounds 覆盖）。
        planning_feedback: 可选，模块三的 PlanningFeedback（REPLAN 入口用）。
        previous_feedback: 可选，人审 [m]odify 的反馈；非空时拼进初稿 prompt 头部。
        method_designer_holder: 跨轮复用 method-designer session 的 dict 容器（传 None
                                时本函数内部新建；外部 planning_flow.py 持有可让本阶段
                                的 session history 跨越 stage1/stage2 共享——但当前设计
                                stage1 跑完即关，所以一般是 None）。
        method_critic_holder:   跨轮复用 method-critic session 的 dict 容器（同上）。
        debug_dump_path:        末轮仍过硬门禁失败时，把失败草稿 JSON 落盘到此路径
                                供排查（不进正式产物）；None 则不落盘。

    Returns:
        (method_design, method_review, rounds_used):
            method_design:  最终的方法设计 dict（[planning-schemas.md §2.1](../references/planning-schemas.md)）。
            method_review:  最近一轮评审 dict（[planning-schemas.md §2.2](../references/planning-schemas.md)）。
            rounds_used:    实际跑的轮数（1-N+1）。
    """
    # 拼 REPLAN 上下文（schemas §5：把上一轮 MethodReview.feedback + PlanningFeedback.suggested_changes
    # 拼给 Architect，避免重复同样的错误）
    replan_context: dict = {}
    if planning_feedback:
        replan_context = {
            "planning_feedback_reason": planning_feedback.get("reason", ""),
            "planning_feedback_blockers": planning_feedback.get("blockers", []),
            "planning_feedback_suggested_changes": planning_feedback.get("suggested_changes", []),
            "planning_feedback_affected_experiment_ids": planning_feedback.get("affected_experiment_ids", []),
        }
        log.info(f"[iterate_method_design] REPLAN 模式：blocked={len(replan_context['planning_feedback_blockers'])} 条，"
              f"suggested={len(replan_context['planning_feedback_suggested_changes'])} 条")

    # 1. 拉 architect 出初稿（REPLAN 时把 planning_feedback 喂进 payload；人审反馈也拼进去）
    initial_payload = {**inputs, **replan_context}
    if contract_ledger:
        # 编排器预先分配；method-designer 只能采纳，不可自建另一套 experiment_ref。
        initial_payload["contract_ledger"] = contract_ledger
    if previous_feedback:
        initial_payload["previous_feedback"] = previous_feedback
        log.info(f"[iterate_method_design] 人审反馈模式：previous_feedback={len(previous_feedback)} chars")
    log.info("[iterate_method_design] round 0/initial: 调 method-designer LLM（可能 30-90s）...")
    method_draft = await call_session_async(
        "method-designer", initial_payload, phase="方法设计",
        session_holder=method_designer_holder,
        instructions="你是 method-designer 阶段会话：只设计方法；可见方法人审 modify 反馈；不可见实验阶段反馈。",
    )
    log.info(f"[iterate_method_design] round 0 完成: method_draft keys={list(method_draft.keys()) if isinstance(method_draft, dict) else type(method_draft).__name__}")
    # 校验初稿字段契约（不阻塞——LLM 反思循环会处理格式问题，校验仅作警告）
    draft_errs = validate_method_design(method_draft)
    for e in draft_errs:
        log.info(f"[iterate_method_design] method_draft 校验警告: {e}")

    # 2. Layer A 确定性门禁（Phase 2 / Track B，0 LLM 调用）
    # 把"让 LLM critic 评 hypothesis_coverage / 必填字段"这种反模式换成 Python 一行判
    # Layer A 命中 → 让 designer 补 1 轮（不进入反思循环），不再让 critic LLM 评确定性约束
    rounds_used = 0  # 初始化（含 Layer A 触发的 designer 计数）
    hard_gaps = hard_gate_method_design(method_draft, inputs)
    if hard_gaps:
        gap_feedback = format_gaps_as_feedback(hard_gaps, "method_design")
        print(
            f"[iterate_method_design] Layer A 命中 {len(hard_gaps)} 个 gap，"
            f"调 designer 补齐：{hard_gaps[:2]}{'...' if len(hard_gaps) > 2 else ''}"
        )
        method_draft = await call_session_async(
            "method-designer",
            {**initial_payload, "previous_feedback": gap_feedback},
            phase="方法设计",
            session_holder=method_designer_holder,
        )
        rounds_used += 1  # Layer A 触发的 designer 重写算 1 轮
        # 兜底：再跑一次 Layer A 校验
        # 但不强制再 1 轮 designer（gap 大多 1 轮就能修完；强行 2 轮浪费 token）
        remaining_gaps = hard_gate_method_design(method_draft, inputs)
        if remaining_gaps:
            print(
                f"[iterate_method_design] Layer A 二次校验仍剩 {len(remaining_gaps)} 个 gap，"
                f"继续进入反思循环让 critic 评"
            )

    # 新编排把语义评审延后到 method_design + experiment_plan 同时存在时，交给
    # planning-contract-critic。这里仅做可确定的局部字段修复，避免旧 critic
    # 触发整份方法设计反复改写。
    if not run_legacy_critic:
        for repair_idx in range(max_rounds):
            remaining_gaps = hard_gate_method_design(method_draft, inputs)
            if not remaining_gaps:
                return method_draft, {
                    "passed": True,
                    "adequacy_score": ADEQUACY_PASS,
                    "feedback": "基础结构门禁通过；跨产物语义审查由 planning-contract-critic 处理。",
                }, rounds_used
            method_draft = await call_session_async(
                "method-designer",
                {
                    **initial_payload,
                    "current_method_design": method_draft,
                    "previous_feedback": format_gaps_as_feedback(remaining_gaps, "method_design")
                    + "\n只修改列出的字段，保留 current_method_design 其余字段。",
                },
                phase="方法设计",
                session_holder=method_designer_holder,
            )
            rounds_used += 1
        final_gaps = hard_gate_method_design(method_draft, inputs)
        if final_gaps:
            raise RuntimeError(
                "method_design 定向结构修复耗尽后仍未通过确定性门禁：\n- "
                + "\n- ".join(final_gaps[:30])
            )
        return method_draft, {
            "passed": True,
            "adequacy_score": ADEQUACY_PASS,
            "feedback": "基础结构门禁通过；跨产物语义审查由 planning-contract-critic 处理。",
        }, rounds_used

    # 3. 反思循环
    method_review: dict = {}
    prev_method_review_for_critic: dict | None = None
    prev_method_draft_for_diff: dict | None = None  # 2026-08-29 新增：脚本 diff 兜底
    for round_idx in range(1, max_rounds + 1):
        rounds_used = round_idx
        log.info(f"[iterate_method_design] ━━━ round {round_idx}/{max_rounds} ━━━")

        # 2a. 拉 critic 评
        # 2026-08-29 新增：把本轮要评的 draft 暂存到 prev_method_draft_for_diff。
        # 下一轮 iter 开头会再次覆盖本变量（因为那时 method_draft 已经被 architect 重写）。
        # script_diff 在每轮 iter 末尾跑，对比 prev=本轮被评的 draft, curr=下一轮 architect 写的。
        # 但当前 iter 没法跑 diff（自己对自己 diff 没意义）— 唯一例外是首轮 prev=None。
        prev_method_draft_for_diff = method_draft  # 覆盖：本次 critic 看的版本
        # 2026-08-29 新增：payload 带 3 类反馈（previous_method_review / human_modify_feedback /
        # planning_feedback），让 critic 知道上轮评审、人审 modify、模块三 REPLAN 反馈
        # 都存在，并据其评"反馈采纳度"维度（method-critic persona 第 3 维度 0-2 分）。
        # 首轮 prev_method_review_for_critic=None；空 previous_feedback 传 "" 兼容 persona。
        method_review = await call_session_async(
            "method-critic",
            {
                "method_draft": method_draft,
                "gap_report": inputs["gap_report"],
                "previous_method_review": prev_method_review_for_critic,
                "human_modify_feedback": previous_feedback if previous_feedback else "",
                "planning_feedback": planning_feedback,
            },
            phase="方法设计",
            session_holder=method_critic_holder,
            instructions="你是 method-critic 阶段会话：只评审方法；可见方法人审 modify 反馈；不可见实验阶段反馈。",
        )
        # run i 实测：critic 把 feedback 返成 list（["...", ...]）。
        # 必须在喂给下一轮 architect 之前归一成 str——否则 formatter 的
        # isinstance(str) 分支静默跳过，反馈整段丢失（旧尾部兜底只在循环结束
        # 后跑，救不了轮内传递）。
        _fb = method_review.get("feedback")
        if _fb is not None and not isinstance(_fb, str):
            method_review["feedback"] = coerce_feedback_text(_fb)
            log.info(
                "[iterate_method_design] method_review.feedback 为 %s，已归一成 str",
                type(_fb).__name__,
            )
        # 校验评审字段契约（不阻塞——passed 字段由 critic 决定）
        review_errs = validate_method_review(method_review)
        for e in review_errs:
            log.info(f"[iterate_method_design] method_review 校验警告: {e}")

        # 2b. 通过则跳出——但 adequacy ≥ ADEQUACY_PASS 仍可能有 unaddressed 条目
        # 2026-08-29 新增：feedback_addressed 强制重写机制（C-③ 防走过场）
        passed = (
            method_review.get("passed")
            and method_review.get("adequacy_score", 0) >= ADEQUACY_PASS
        )
        unaddressed_count = _count_unaddressed(method_review)
        # 2026-08-29 新增：脚本 diff 硬兜底——LLM 自查说"已采纳"但值没变 → 视为未采纳
        script_diff_unchanged = _script_diff_unchanged_count(
            prev_method_draft_for_diff, method_draft, method_review,
        )
        total_unaddressed = unaddressed_count + script_diff_unchanged
        deterministic_gaps = hard_gate_method_design(method_draft, inputs)
        if passed and total_unaddressed == 0 and not deterministic_gaps:
            # 真正通过——跳出
            break
        if passed and total_unaddressed > 0:
            # adequacy ≥ 7 但有反馈未采纳——强制再写一轮（不能让 LLM 自查走过场）
            print(
                f"[iterate_method_design] adequacy ≥ ADEQUACY_PASS 但 "
                f"{total_unaddressed} 条反馈未采纳（LLM自查={unaddressed_count}, 脚本diff={script_diff_unchanged}），强制再写一轮"
            )
        # 保存本轮 review 供下轮 critic 当 previous_method_review
        prev_method_review_for_critic = dict(method_review) if method_review else None

        # 2c. 不通过——把 method_review 的**结构化字段**精确拼给下轮 architect。
        #     之前只拼了 feedback（str），critic 写的 feasibility_risks/missing_components/
        #     hypothesis_coverage 缺口这些没结构化传递——architect 看不到字段路径
        #     就自由发挥（典型症状：components 用 implementation 代替 function/architecture）。
        #     现在把每个结构化块按"先结构化、再原文"顺序拼，让 architect 知道改哪个字段。
        prev_feedback = _format_critic_structured_feedback(
            method_review,
            missing_hypotheses=set(),  # 第一轮兜底轮外传
        )
        if deterministic_gaps:
            prev_feedback = (
                f"{format_gaps_as_feedback(deterministic_gaps, 'method_design')}\n\n"
                f"{prev_feedback}"
            )
        # 2026-08-29 新增：把 feedback_addressed 中 unaddressed 项单独提块，让 architect
        # 知道上轮反馈哪条没改、当前值是啥、改哪个字段（field_path）
        unaddressed_block = _format_unaddressed_block(method_review)
        if unaddressed_block:
            prev_feedback = f"{prev_feedback}\n\n{unaddressed_block}"
        if planning_feedback:
            suggested = planning_feedback.get("suggested_changes") or []
            if suggested:
                # 标记 REPLAN 建议来自上游，避免 Architect 把它当成本轮 critic 反馈
                suggested_block = "\n".join(f"- {s}" for s in suggested)
                prev_feedback = (
                    f"{prev_feedback}\n\n"
                    f"[REPLAN 来自模块三 PlanningFeedback]\n{suggested_block}"
                )
        method_draft = await call_session_async(
            "method-designer",
            {**initial_payload, "previous_feedback": prev_feedback},
            phase="方法设计",
            session_holder=method_designer_holder,
        )
        # 注意：不要在这里覆盖 prev_method_draft_for_diff！
        # 它已经在 iter 开头被设成本轮 critic 看的版本（draft_vN），下一轮 iter 开头
        # architect 写完 draft_v{N+1} 后会再次覆盖（用 method_draft 的新值）。
        # 下一轮 iter 跑 script_diff(prev=本版 draft_vN, curr=method_draft=draft_v{N+1}, paths=本轮 review 的 field_path)

    # 3. 假设覆盖兜底
    # 兼容 deepseek 等 LLM 把 hypothesis_coverage 返成 dict（{H1: {...}, H2: {...}}）
    # 而不是 list of {hypothesis_id, ...} 的情况。统一规范成 list 后再迭代，
    # 避免 "for hc in dict" 迭代出 str 触发 AttributeError: 'str' has no attribute 'get'。
    hc_raw = method_draft.get("hypothesis_coverage", [])
    if isinstance(hc_raw, dict):
        method_draft["hypothesis_coverage"] = [
            {"hypothesis_id": k, **(v if isinstance(v, dict) else {})}
            for k, v in hc_raw.items()
        ]
        hc_raw = method_draft["hypothesis_coverage"]
    covered_ids = {
        hc.get("hypothesis_id")
        for hc in hc_raw
        if isinstance(hc, dict)
    }
    all_ids = {h.get("id") for h in inputs.get("hypotheses", [])}
    missing_ids = sorted(all_ids - covered_ids)

    # 4. 有漏——补 1 轮
    if missing_ids:
        method_draft = await call_session_async(
            "method-designer",
            {**initial_payload, "previous_feedback": f"补以下假设: {missing_ids}"},
            phase="方法设计",
            session_holder=method_designer_holder,
        )
        rounds_used += 1
        # 兜底轮不再拉 critic——直接用最后一轮 review

    # 5. 字段归一化兜底（防止下游 planning_flow.py:209 [:200] 切片炸）
    # deepseek-v4-flash 偶尔把 method_review.feedback 返成 dict
    # （例：{"strengths": [...], "weaknesses": [...]}）而不是 str。
    # planning_flow.py 把 feedback 拼进 human() prompt 文本时调 [:200] 切片，
    # dict[:200] 会 TypeError: unhashable type: 'slice'。统一强转成 str(JSON)
    # 保证下游 f-string 永远拿到 str。
    feedback = method_review.get("feedback", "")
    if not isinstance(feedback, str):
        method_review["feedback"] = coerce_feedback_text(feedback)

    final_gaps = hard_gate_method_design(method_draft, inputs)
    if final_gaps:
        # 失败现场必须留下来：deepseek-v4-flash 的漂移字面形态离线造不出来
        # （run h：target 拆出的判据 comparator 空/threshold null，但 target 原文
        # 不落盘就只能盲修）。写一份仅供排查的草稿，不进 Stage 5 正式产物。
        if debug_dump_path:
            try:
                import json as _json
                from pathlib import Path as _Path
                p = _Path(debug_dump_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    _json.dumps(method_draft, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info(f"[iterate_method_design] 失败草稿已落盘: {p}")
            except OSError as exc:
                log.warning(f"[iterate_method_design] 失败草稿落盘失败: {exc}")
        raise RuntimeError(
            "method_design 反思轮耗尽后仍未通过确定性门禁；禁止写成 complete：\n- "
            + "\n- ".join(final_gaps[:30])
        )

    return method_draft, method_review, rounds_used


# ════════════════════════════════════════════════════════════════
# 把 method_review 的结构化字段精确拼给下一轮 method-designer
# ════════════════════════════════════════════════════════════════

def _format_critic_structured_feedback(
    method_review: dict, missing_hypotheses: set[str] | None = None
) -> str:
    """把 critic 的结构化反馈按字段路径拼成单块文本。

    目的：让 architect 在下轮重写时**知道具体改哪个字段**（如
    ``components[2].function``），而不是只看 feedback 散文。结构化块
    按严重度分块（blocker / major / minor），缺假设列表单独提。

    Args:
        method_review: critic 返回的 dict（已通过 .feedback 归一化）。
        missing_hypotheses: 假设覆盖缺口集合（来自外部 #3 兜底轮），
            非空时也并入。

    Returns:
        拼好的 prev_feedback 文本（可能为空——critic 没结构化反馈时）。
    """
    blocks: list[str] = []

    # 1. 可行性风险（按 severity 分块）
    risks = method_review.get("feasibility_risks") or []
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
                blocks.append(f"[critic {sev} 级风险]\n{lines}")

    # 2. 缺失组件（指向 components[].name）
    missing_comps = method_review.get("missing_components") or []
    if isinstance(missing_comps, list) and missing_comps:
        comp_list = ", ".join(str(c) for c in missing_comps if c)
        if comp_list:
            blocks.append(
                f"[critic 缺失组件] {comp_list}\n"
                f"  → 必须在 components[] 补齐（fields: name/function/"
                f"input_schema/output_schema/novelty_degree）"
            )

    # 3. 撞车论文（提示差异化方向）
    overlap = method_review.get("overlap_papers") or []
    if isinstance(overlap, list) and overlap:
        ov_list = ", ".join(str(p) for p in overlap if p)
        if ov_list:
            blocks.append(
                f"[critic 撞车论文] {ov_list}\n"
                f"  → 在 innovation_points[].claim 里显式说明与上述工作的差异化"
            )

    # 4. 假设覆盖缺口（来自外部传参）
    if missing_hypotheses:
        miss_str = ", ".join(sorted(missing_hypotheses))
        blocks.append(
            f"[critic 假设覆盖缺口] {miss_str}\n"
            f"  → 在 hypothesis_coverage[] 补齐这些 hypothesis_id 的 mechanism+experiment_ref"
        )

    # 5. novelty_ok=False 时单独提示
    if method_review.get("novelty_ok") is False:
        blocks.append(
            "[critic 整体新颖性未通过] novelty_ok=false\n"
            "  → 必须重写至少 1 条 innovation_points[].claim 让差异化清晰可证伪"
        )

    # 6. feedback 原文（兜底——上面没结构化到的内容）
    #    list/dict 漂移先归一成 str，否则整块反馈被静默丢掉
    fb = coerce_feedback_text(method_review.get("feedback", ""))
    if fb.strip():
        blocks.append(f"[critic 总体反馈]\n{fb.strip()}")

    return "\n\n".join(b for b in blocks if b)


# ════════════════════════════════════════════════════════════════
# 脚本 diff 硬兜底（_experiment_diff.py，2026-08-29 新增）
# ════════════════════════════════════════════════════════════════


def _script_diff_unchanged_count(
    prev_draft: dict | None,
    curr_draft: dict,
    review: dict,
) -> int:
    """用 _experiment_diff.script_diff_check 硬兜底——
    即使 LLM 自查说"已采纳"，脚本对比值没变 → 强制重写。

    Args:
        prev_draft: 上一轮 method_draft（首轮为 None，跳过 diff）
        curr_draft: 本轮 method_draft
        review: method-critic 本轮的 review（用于抽 field_path）

    Returns:
        脚本检测到 unchanged + missing 的总条数。prev=None 时返回 0（首轮无从对比）。
    """
    if prev_draft is None:
        return 0
    from scripts._experiment_diff import (
        script_diff_check,
        extract_field_paths_from_addressed,
    )
    field_paths = extract_field_paths_from_addressed(review.get("feedback_addressed"))
    if not field_paths:
        return 0
    result = script_diff_check(prev_draft, curr_draft, field_paths)
    unchanged_n = len(result["unchanged"])
    missing_n = len(result["missing"])
    if unchanged_n or missing_n:
        print(
            f"[iterate_method_design] 脚本 diff 兜底：{unchanged_n} unchanged + "
            f"{missing_n} missing（field paths={field_paths}）"
        )
    return unchanged_n + missing_n


# ════════════════════════════════════════════════════════════════
# feedback_addressed 强制重写机制（C-③，2026-08-29 新增）
# ════════════════════════════════════════════════════════════════


def _count_unaddressed(method_review: dict) -> int:
    """统计 method_review.feedback_addressed 中 addressed=False 的条数。

    2026-08-29 新增：防止 LLM 自查走过场——即使 adequacy_score ≥ 7.0，
    只要有上轮反馈没真改，就**强制再写一轮**。

    Args:
        method_review: method-critic 本轮返回的 dict。

    Returns:
        未采纳条数。0 = 全部采纳；LLM 漂移（feedback_addressed 非 list）→ 0。
    """
    addressed = method_review.get("feedback_addressed")
    if not isinstance(addressed, list):
        return 0
    return sum(
        1 for a in addressed
        if isinstance(a, dict) and not a.get("addressed")
    )


def _format_unaddressed_block(method_review: dict) -> str:
    """把 unaddressed 项的 evidence + field_path 拼成单块文本。

    2026-08-29 新增：architect 在下轮重写时需要知道——
    1. 哪条上轮反馈没采纳（old_issue）
    2. 当前字段值是啥（evidence）
    3. 改哪个字段路径（field_path）

    比 _format_critic_structured_feedback 多了「field_path 定向 + 当前值」，
    专为 feedback_addressed 结构化追溯设计。

    Args:
        method_review: method-critic 本轮返回的 dict。

    Returns:
        拼好的 unaddressed 块文本；无 unaddressed → 空串。
    """
    addressed = method_review.get("feedback_addressed")
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
    return "[上轮反馈未采纳]\n" + "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 同步入口（保留给独立测试/单步调试用；正式流程走 planning_flow.run() 异步）
# ════════════════════════════════════════════════════════════════

import asyncio  # noqa: E402  同步包装在文件末尾，import 必须留在分隔注释之后


def iterate_method_design(
    inputs: dict,
    max_rounds: int = DEFAULT_MAX_METHOD_ROUNDS,
    planning_feedback: dict | None = None,
    previous_feedback: str = "",
    *,
    contract_ledger: dict | None = None,
    run_legacy_critic: bool = True,
    method_designer_holder: dict | None = None,
    method_critic_holder: dict | None = None,
    debug_dump_path: str | None = None,
) -> tuple[dict, dict, int]:
    """同步包装：``asyncio.run(iterate_method_design_async(...))``。

    注意：被 SwarmFlow workflow 调起时**不可用**（event loop 已在跑）——workflow
    调起路径请直接用 ``await iterate_method_design_async(...)``。
    """
    return asyncio.run(iterate_method_design_async(
        inputs, max_rounds, planning_feedback, previous_feedback,
        contract_ledger=contract_ledger,
        run_legacy_critic=run_legacy_critic,
        method_designer_holder=method_designer_holder,
        method_critic_holder=method_critic_holder,
        debug_dump_path=debug_dump_path,
    ))
