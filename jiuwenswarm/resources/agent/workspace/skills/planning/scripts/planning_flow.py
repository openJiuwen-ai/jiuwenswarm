"""planning_flow.py — 规划模块业务编排层（2026-08-31 plan-supervisor 接入改造）

执行流（4 个阶段，**不**含 4 个人审检查点）::

    phase("方法设计")       → iterate_method_design_async
    phase("实验规划")       → plan_experiment_with_data_async
    phase("门禁校验")       → check_feasibility_and_downgrade
    phase("执行配置")       → produce_execution_config
    phase("写产物")         → write_outputs

设计要点（Phase 3 plan-supervisor 接入）：
      skill 内部不再持 main session 句柄——主 session 由 agent 拥有。
    - **session 化子 agent（4 个）保留**：method-designer / method-critic /
      experiment-planner / experiment-critic 走 ``call_session_async``，跨反思
      循环复用 session_holder，history 自动累积。详见 iterate_method_design.py /
      plan_experiment_with_data.py。
    - **REPLAN 入口换为 args["feedback_file"]**（agent 协议）；旧 args["replan_from"]
      保留兼容（自动转换）。同一份 load_planning_feedback 校验。
    - **返回 rich dict**：status / artifacts(=paths) / errors / warnings /
      check_feasibility / method_rounds_used / tier / checkpoint_status，供 main.py
      写 status.json。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from openjiuwen.agent_teams.workflow.engine.facade import (
    log as _sf_log,
    phase,
)

# 模块级 Python logger（与 facade log 区分）
# planning.main 入口的 setup_planning_logging 会配置 handler
plog = logging.getLogger("planning.flow")

# 算力档位常量（与 scripts/estimate_compute.py 镜像）
TIER_FULL = 0
TIER_TRIM = 1
TIER_CORE = 2


# ════════════════════════════════════════════════════════════════
# REPLAN 路由决策（Phase 3 / Track C，4 类局部路由）
# ════════════════════════════════════════════════════════════════


def compute_replan_route(cats: set[str]) -> dict:
    """根据 blocker categories 决定 REPLAN 走哪条局部路由。

    refactor 设计：plan-supervisor/docs/plan-supervisor-refactor-2026-09-01.md §4 Track C。

    路由表（取最严子集，混合 category 按表匹配）：

    | category                                   | 行为                                       |
    |--------------------------------------------|--------------------------------------------|
    | ``schema``                                 | 直接返回 replan_schema_blocked（caller 处理）|
    | ``data`` only                              | REPLAN_DATA（data 重抽）                   |
    | ``method``                                 | stage 1 重跑 + stage 2 复用                |
    | ``experiment`` / ``metric`` / ``baseline`` / ``compute`` | stage 1 复用 + stage 2 重跑 |
    | ``budget``                                 | stage 1+2 复用 + stage 3 gate 重算         |
    | 混合（data + 其他）                        | data 走 REPLAN_DATA + 其余按上表           |

    Args:
        cats: 从 feedback_file.blockers[] 解析出的 category 集合。

    Returns:
        包含 ``replan_skip_stage1`` / ``replan_only_stage2`` /
        ``replan_only_stage3`` / ``replan_data_only`` 4 个 bool 的 dict。
    """
    route = {
        "replan_skip_stage1": False,
        "replan_only_stage2": False,
        "replan_only_stage3": False,
        "replan_data_only": False,
    }
    if "schema" in cats:
        # caller 看到 schema 直接 return replan_schema_blocked，这里保持默认
        return route
    if "data" in cats and not (cats - {"data"}):
        # 纯 data 类别
        route["replan_data_only"] = True
        route["replan_skip_stage1"] = True
        return route
    if "data" in cats:
        # data + 其他类混合 → data 走 REPLAN_DATA，其余按下面规则
        route["replan_data_only"] = True
        route["replan_skip_stage1"] = True
    if "method" in cats:
        # method 重跑 stage 1，stage 2 复用
        route["replan_skip_stage1"] = False
        route["replan_only_stage2"] = True
    elif cats & {"experiment", "metric", "baseline", "compute"}:
        # 这些都改 experiment_plan 字段 → stage 1 复用 + stage 2 重跑
        route["replan_skip_stage1"] = True
        route["replan_only_stage2"] = True
    elif "budget" in cats:
        # 只重算 gate，stage 1+2 全复用
        route["replan_skip_stage1"] = True
        route["replan_only_stage3"] = True
    return route


def extract_blocker_categories(planning_feedback: dict | None) -> set[str]:
    """从 planning_feedback.blockers[] 抽 category 集合。

    blockers 每条以 ``[category]`` 开头，例 ``[method] xxx`` → category="method"。
    空 category（``[] xxx``）和非字符串项被忽略。
    """
    cats: set[str] = set()
    for b in (planning_feedback or {}).get("blockers") or []:
        if isinstance(b, str) and b.startswith("[") and "]" in b:
            cat = b[1:b.index("]")].strip()
            if cat:  # 跳过空 category（"[] xxx" malformed）
                cats.add(cat)
    return cats


# ════════════════════════════════════════════════════════════════
# 业务编排（4 阶段，**无 4 个检查点**）
# ════════════════════════════════════════════════════════════════


async def planning_main_session(sess, args) -> dict:
    """主业务编排：跑 4 个 stage，返回 rich dict 给 main.py 写 status.json。

    本函数**不调 LLM**——主 session ``sess`` 仅为历史接口兼容保留（agent 模式传 None）。
    真正干活的是 4 个子 session（method-* / experiment-*）；子 session 由 iterate_method_design
    / plan_experiment_with_data 内部拉起。

    Args:
        sess: 历史参数（已 deprecated，agent 模式传 None；保留仅作 API 兼容）。
        args: CLI dict，键：
            input_dir, output_dir, max_method_rounds, strict, replan_from（旧）,
            feedback_file（新）, status_file, seeds, max_retries, timeout_seconds,
            dry_run, no_human_review（agent 模式不传）.

    Returns:
        ``rich dict`` 供 main.py 调 _status.write_status_file()：
        - status: complete | error | preflight_failed | preflight_error |
                  load_inputs_failed | unsupported_execution_capability |
                  replan_schema_blocked
        - output_dir: str
        - tier: 0 | 1 | 2
        - paths / artifacts: list[str] 写盘的产物绝对路径
        - errors: list[str]
        - warnings: list[str]
        - check_feasibility: {passed, downgraded_to, issues}
        - method_rounds_used: int
        - checkpoint_status: {method_design, experiment_plan, tier_downgrade, execution_config}
        - reason: str  （preflight / load_inputs / replan_schema 失败时）
    """
    _ = sess  # 历史参数；agent 模式传 None，本函数内部不调 sess.send()
    args = args if isinstance(args, dict) else {}
    _sf_log(f"planning_main_session args={list(args.keys())}")
    plog.info("planning_main_session 启动")

    # sys.path bootstrap（让 from scripts.* 解析到本目录）
    _SCRIPTS_PARENT = str(Path(__file__).resolve().parent.parent)
    if _SCRIPTS_PARENT not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_PARENT))
    _SCRIPTS_DIR = str(Path(__file__).resolve().parent)
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))

    # ── 子 session 容器；共享 critic 会话贯穿方法/实验的共同审查 ──
    method_designer_holder: dict = {}
    method_critic_holder: dict = {}
    experiment_planner_holder: dict = {}
    experiment_critic_holder: dict = {}
    contract_critic_holder: dict = {}

    # 4 checkpoint 状态（agent 端人审做；skill 端只标 "skipped"）
    checkpoint_status: dict[str, str] = {
        "method_design":    "skipped",
        "experiment_plan":  "skipped",
        "tier_downgrade":   "skipped",
        "execution_config": "skipped",
    }

    # REPLAN 反馈来源：args["feedback_file"]（新）优先，args["replan_from"]（旧）兜底
    feedback_file = args.get("feedback_file") or args.get("replan_from")

    try:
        # ────────────── Pre-flight ──────────────
        try:
            from scripts.main import _preflight  # type: ignore
            plog.info("跑 preflight ...")
            report = _preflight()
            plog.info(f"preflight 完成: passed={report['passed']}, critical={len(report.get('critical', []))}, warn={len(report.get('warning', []))}")
            if not report["passed"]:
                _sf_log("preflight critical 失败，环境不可用")
                return {
                    "status": "preflight_failed",
                    "errors": report["critical"],
                    "warnings": report.get("warning", []),
                    "checkpoint_status": checkpoint_status,
                }
        except Exception as exc:  # noqa: BLE001
            _sf_log(f"preflight 异常: {exc}")
            return {
                "status": "preflight_error",
                "errors": [str(exc)],
                "checkpoint_status": checkpoint_status,
            }

        # ────────────── Load inputs ──────────────
        phase("方法设计")
        from scripts.load_inputs import load_inputs, load_planning_feedback  # noqa: E402
        plog.info(f"跑 load_inputs(input_dir={args['input_dir']}) ...")
        inputs, errors = load_inputs(args["input_dir"])
        # 统计每个 entity 是否成功
        loaded = {k: (v is not None and v != [] and not (isinstance(v, list) and len(v) == 0)) for k, v in inputs.items()}
        plog.info(f"load_inputs 完成: payloads={loaded}, errors={len(errors)}")
        for k, v in inputs.items():
            if v is None or (isinstance(v, list) and len(v) == 0):
                plog.warning(f"  {k} 为空/None")
        if errors:
            for e in errors:
                _sf_log(f"[load_inputs] {e}")
            if args.get("strict"):
                return {
                    "status": "load_inputs_failed",
                    "errors": errors,
                    "checkpoint_status": checkpoint_status,
                }

        # ────────────── Execution capability handshake ──────────────
        # This is a deterministic module-two → module-three boundary, not an
        # LLM semantic review.  It prevents a known unsupported research
        # profile from burning method-designer repair rounds and then being
        # misreported as an invalid metric name.
        from scripts.execution_capability_preflight import (  # noqa: E402
            assess_execution_capability,
            format_capability_errors,
        )
        capability_report = assess_execution_capability(inputs)
        if not capability_report["passed"]:
            output_path = Path(args["output_dir"])
            output_path.mkdir(parents=True, exist_ok=True)
            report_path = output_path / "execution_capability_report.json"
            report_path.write_text(
                json.dumps(capability_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            errors = format_capability_errors(capability_report)
            plog.error("模块三能力握手阻断规划: %s", errors[0])
            return {
                "status": "unsupported_execution_capability",
                "output_dir": args["output_dir"],
                "artifacts": [str(report_path.resolve())],
                "errors": errors,
                "warnings": [],
                "capability_report": capability_report,
                "check_feasibility": {
                    "passed": False,
                    "downgraded_to": None,
                    "issues": errors,
                },
                "checkpoint_status": checkpoint_status,
            }
        if capability_report.get("available_profiles"):
            inputs = {**inputs, "execution_capability": {
                "instruction": "The following module-three contracts are executable boundaries. Use their task_type, primary_method_id, baseline_method_ids, method_ids, metric_names, and planning_constraints exactly; do not invent unsupported alternatives or list the primary method as a baseline.",
                "profiles": capability_report["available_profiles"],
            }}

        from scripts.contract_ledger import build_contract_ledger, update_contract_ledger, write_contract_ledger  # noqa: E402
        contract_ledger = build_contract_ledger(inputs)
        plog.info("共享约定账本已初始化: hypotheses=%d", len(contract_ledger["hypotheses"]))

        # ── REPLAN 路由（Phase 3 / Track C，4 类局部路由） ──
        # refactor 设计：plan-supervisor/docs/plan-supervisor-refactor-2026-09-01.md §4 Track C
        planning_feedback: dict | None = None
        if feedback_file:
            planning_feedback, fb_errs = load_planning_feedback(feedback_file)
            if fb_errs:
                for e in fb_errs:
                    _sf_log(f"[load_planning_feedback] {e}")
            cats = extract_blocker_categories(planning_feedback)
            if "schema" in cats:
                return {
                    "status": "replan_schema_blocked",
                    "errors": [f"categories={sorted(cats)}"],
                    "categories": sorted(cats),
                    "checkpoint_status": checkpoint_status,
                }
            route = compute_replan_route(cats)
            replan_data_only = route["replan_data_only"]
            replan_skip_stage1 = route["replan_skip_stage1"]
            replan_only_stage2 = route["replan_only_stage2"]
            replan_only_stage3 = route["replan_only_stage3"]
            _sf_log(
                f"[REPLAN 路由] cats={sorted(cats)} → "
                f"replan_skip_stage1={replan_skip_stage1} "
                f"replan_only_stage2={replan_only_stage2} "
                f"replan_only_stage3={replan_only_stage3} "
                f"replan_data_only={replan_data_only}"
            )
        else:
            replan_data_only = False
            replan_skip_stage1 = False
            replan_only_stage2 = False
            replan_only_stage3 = False

        # ────────────── Stage 1: 方法设计 ──────────────
        from scripts.iterate_method_design import iterate_method_design_async  # noqa: E402
        if replan_skip_stage1:
            # 局部 REPLAN 不重跑方法设计，但不能把已验证的方法清空。
            # feedback_file 的父目录由编排器保存上一轮规划快照。
            prev_dir = Path(feedback_file).parent if feedback_file else None
            prev_md_path = prev_dir / "method_design.json" if prev_dir else None
            prev_mr_path = prev_dir / "method_review.json" if prev_dir else None
            if prev_md_path is None or not prev_md_path.is_file():
                return {
                    "status": "replan_missing_previous_method",
                    "errors": ["REPLAN 跳过方法设计时缺少上一轮 method_design.json"],
                    "checkpoint_status": checkpoint_status,
                }
            method_design = json.loads(prev_md_path.read_text(encoding="utf-8"))
            method_review = (
                json.loads(prev_mr_path.read_text(encoding="utf-8"))
                if prev_mr_path is not None and prev_mr_path.is_file()
                else {}
            )
            method_rounds_used = 0
            contract_ledger = update_contract_ledger(contract_ledger, method_design=method_design)
            plog.info("Stage 1 方法设计: REPLAN 跳过（已加载上一轮 method_design）")
        else:
            plog.info(f"Stage 1 方法设计 启动 (max_rounds={args.get('max_method_rounds', 2)})")
            _debug_dump = (
                str(Path(args["output_dir"]) / "_debug_failed_method_draft.json")
                if args.get("output_dir") else None
            )
            method_design, method_review, method_rounds_used = await iterate_method_design_async(
                inputs,
                max_rounds=int(args.get("max_method_rounds", 2)),
                planning_feedback=planning_feedback,
                contract_ledger=contract_ledger,
                run_legacy_critic=False,
                method_designer_holder=method_designer_holder,
                method_critic_holder=method_critic_holder,
                debug_dump_path=_debug_dump,
            )
            contract_ledger = update_contract_ledger(contract_ledger, method_design=method_design)
            plog.info("共享账本已接收方法侧指标: metrics=%d", len(contract_ledger.get("metric_names", [])))
            plog.info(f"Stage 1 方法设计 完成: rounds_used={method_rounds_used}, has_method_design={bool(method_design)}, has_method_review={bool(method_review)}")
        checkpoint_status["method_design"] = "passed"  # stage 跑完即标 passed；人审归 agent

        # ────────────── Stage 2: 实验规划 ──────────────
        phase("实验规划")
        plog.info("Stage 2 实验规划 启动")
        prev_experiment_plan: dict | None = None
        prev_data_plan: dict | None = None
        if (replan_data_only or replan_only_stage2 or replan_only_stage3) and feedback_file:
            prev_path = Path(feedback_file)
            prev_dir = prev_path if prev_path.is_dir() else prev_path.parent
            prev_ep_path = prev_dir / "experiment_plan.json"
            if prev_ep_path.is_file():
                prev_experiment_plan = json.loads(prev_ep_path.read_text(encoding="utf-8"))
                _sf_log(f"REPLAN 加载上一轮 experiment_plan: {prev_ep_path}")
            prev_dp_path = prev_dir / "data_plan.json"
            if prev_dp_path.is_file():
                prev_data_plan = json.loads(prev_dp_path.read_text(encoding="utf-8"))
                _sf_log(f"REPLAN 加载上一轮 data_plan: {prev_dp_path}")

        # 模块三已验证候选直链（bundle 携带时）——REPLAN_DATA 下随 payload 喂给 planner
        from scripts.plan_experiment_with_data import (  # noqa: E402
            plan_experiment_with_data_async,
            load_resolved_download_hints,
        )
        # 目录布局：bundle 里 feedback_file 与 source_resolution/ 同级（也可能直接给目录）
        resolved_download_hints: dict | None = None
        if feedback_file:
            _fb_path = Path(feedback_file)
            _hints_dir = _fb_path if _fb_path.is_dir() else _fb_path.parent
            resolved_download_hints = load_resolved_download_hints(
                _hints_dir / "source_resolution"
            )
            if resolved_download_hints:
                _sf_log(
                    "REPLAN 加载模块三已验证候选直链: "
                    f"{sorted(resolved_download_hints)}"
                )
        if replan_only_stage3:
            # budget 类：stage 1+2 全复用，只重算 stage 3 gate
            # 不调 plan_experiment_with_data（避免 LLM 重跑）
            assert prev_experiment_plan is not None, "replan_only_stage3 必传 prev"
            experiment_plan = prev_experiment_plan
            data_plan = prev_data_plan or {}
            experiment_review: dict = {}
            _sf_log("REPLAN 路由 budget：跳过 stage 2，调 stage 3 gate 重算")
        else:
            experiment_plan, data_plan, experiment_review = await plan_experiment_with_data_async(
                inputs, method_design, method_review,
                replan_data_only=replan_data_only,
                replan_only_stage2=replan_only_stage2,
                planning_feedback=planning_feedback,
                prev_experiment_plan=prev_experiment_plan,
                contract_ledger=contract_ledger,
                run_legacy_critic=False,
                experiment_planner_holder=experiment_planner_holder,
                experiment_critic_holder=experiment_critic_holder,
                resolved_download_hints=resolved_download_hints,
            )
        checkpoint_status["experiment_plan"] = "passed"

        # 两份产物同时存在后，才由同一个会话审查跨产物语义；它可把问题归给任一方。
        from scripts._subagent import call_session_async  # noqa: E402
        from scripts.contract_review import (  # noqa: E402
            deterministic_findings, feedback_for_owner, merge_findings, normalize_contract_review,
        )
        contract_ledger = update_contract_ledger(
            contract_ledger, method_design=method_design, experiment_plan=experiment_plan,
        )
        deterministic = deterministic_findings(method_design, experiment_plan, data_plan, inputs)
        raw_contract_review = await call_session_async(
            "planning-contract-critic",
            {
                "contract_ledger": contract_ledger,
                "method_design": method_design,
                "experiment_plan": experiment_plan,
                "data_plan": data_plan,
                "deterministic_findings": deterministic,
                "previous_contract_review": None,
            },
            phase="共享约定审查",
            session_holder=contract_critic_holder,
            instructions="你是共同审查会话：审查双方，不偏袒任一方；按 owner/path 给出定向修复意见。",
        )
        contract_review = merge_findings(normalize_contract_review(raw_contract_review), deterministic)
        contract_ledger = update_contract_ledger(contract_ledger, decisions=contract_review.get("decisions"))
        plog.info("共享约定审查完成: feedback=%d", len(contract_review["feedback"]))

        # 共同审查并非只出报告：每位作者只拿到属于自己的 path 清单，最多各修一次，
        # 再由同一个 critic session 复核。这样不会因一条实验意见重写整份方法设计。
        method_patch = feedback_for_owner(contract_review, "method-design")
        experiment_patch = feedback_for_owner(contract_review, "experiment-plan")
        if method_patch or experiment_patch:
            from scripts._hard_gates import hard_gate_experiment_plan, hard_gate_method_design  # noqa: E402
            from scripts.plan_experiment_with_data import _extract_data_order, _normalize_planner_output  # noqa: E402
            if method_patch:
                candidate_method = await call_session_async(
                    "method-designer",
                    {
                        **inputs,
                        "contract_ledger": contract_ledger,
                        "current_method_design": method_design,
                        "previous_feedback": method_patch + "\n只修改列出的字段，保留 current_method_design 的其余字段。",
                    },
                    phase="共享约定修复",
                    session_holder=method_designer_holder,
                )
                gaps = hard_gate_method_design(candidate_method, inputs)
                if gaps:
                    plog.warning("方法侧定向修复未通过基础门禁，保留旧版本: %s", gaps[:3])
                else:
                    method_design = candidate_method
                    contract_ledger = update_contract_ledger(contract_ledger, method_design=method_design)
                    plog.info("共同审查的 method-design 定向修复已采纳")
            if experiment_patch:
                candidate = await call_session_async(
                    "experiment-planner",
                    {
                        **inputs,
                        "method_design": method_design,
                        "method_review": method_review,
                        "contract_ledger": contract_ledger,
                        "current_experiment_plan": experiment_plan,
                        "previous_feedback": experiment_patch + "\n只修改列出的字段，保留 current_experiment_plan 的其余字段。",
                    },
                    phase="共享约定修复",
                    session_holder=experiment_planner_holder,
                )
                candidate = _normalize_planner_output(candidate, method_design)
                candidate_plan = candidate.get("experiment_plan") if isinstance(candidate, dict) else None
                if not isinstance(candidate_plan, dict):
                    plog.warning("实验侧定向修复未返回 experiment_plan，保留旧版本")
                else:
                    candidate_data = _extract_data_order(candidate_plan)
                    gaps = hard_gate_experiment_plan(candidate_plan, inputs, method_design)
                    if gaps:
                        plog.warning("实验侧定向修复未通过基础门禁，保留旧版本: %s", gaps[:3])
                    else:
                        experiment_plan, data_plan = candidate_plan, candidate_data
                        contract_ledger = update_contract_ledger(
                            contract_ledger, method_design=method_design, experiment_plan=experiment_plan,
                        )
                        plog.info("共同审查的 experiment-plan 定向修复已采纳")
            deterministic = deterministic_findings(method_design, experiment_plan, data_plan, inputs)
            raw_contract_review = await call_session_async(
                "planning-contract-critic",
                {
                    "contract_ledger": contract_ledger,
                    "method_design": method_design,
                    "experiment_plan": experiment_plan,
                    "data_plan": data_plan,
                    "deterministic_findings": deterministic,
                    "previous_contract_review": contract_review,
                },
                phase="共享约定复核",
                session_holder=contract_critic_holder,
            )
            contract_review = merge_findings(normalize_contract_review(raw_contract_review), deterministic)
            contract_ledger = update_contract_ledger(contract_ledger, decisions=contract_review.get("decisions"))

        # ────────────── Stage 3: 门禁校验 ──────────────
        phase("门禁校验")
        plog.info("Stage 3 门禁校验 启动")
        from scripts.check_feasibility_and_downgrade import (  # noqa: E402
            check_feasibility_and_downgrade,
            produce_execution_config,
        )
        experiment_plan, data_plan, tier, _results = check_feasibility_and_downgrade(
            experiment_plan, data_plan, inputs["resource_constraints"],
        )
        plog.info(f"Stage 3 门禁校验 完成: tier={tier}, issues={len(_results.get('issues', []))}")

        # 算力档位门禁：tier > 0 表明超预算
        # agent 端会读 status.json 的 check_feasibility / tier 决定是否走 checkpoint-3
        # skill 端在 --no-human-review / AUTO 模式下不自动降级（交给 agent 决策）
        if tier > TIER_FULL:
            checkpoint_status["tier_downgrade"] = "needs_replan"
            # 注意：skill 不在此处自动调 downgrade_with_llm_async——
            # 那是 checkpoint 3 人审决策（downgrade / manual / abort）后才做的事。
            # agent 端读 status.json 看到 tier > 0 后，会渲染 budget UI 让人审决定；
            # 选了 downgrade 才调 call_planning_skill（带 feedback_file）重跑。

        # ────────────── Stage 4: 执行配置 ──────────────
        phase("执行配置")
        execution_config = produce_execution_config(
            output_dir=args["output_dir"],
            user_seeds=args.get("seeds"),
            user_max_retries=args.get("max_retries"),
            user_timeout_seconds=args.get("timeout_seconds"),
            user_dry_run=args.get("dry_run"),
        )
        _sf_log(f"ExecutionConfig: run_dir={execution_config['run_dir']} "
                f"result_dir={execution_config['result_dir']}")
        checkpoint_status["execution_config"] = "passed"

        # ────────────── Stage 5: 写产物 ──────────────
        phase("写产物")
        plog.info(f"Stage 5 写产物 启动: output_dir={args['output_dir']}")
        from scripts.write_outputs import write_outputs  # noqa: E402
        paths = write_outputs(
            method_design=method_design,
            method_review=method_review,
            experiment_plan=experiment_plan,
            data_plan=data_plan,
            execution_config=execution_config,
            experiment_review=experiment_review,
            output_dir=args["output_dir"],
        )
        review_path = Path(args["output_dir"]) / "contract_review.json"
        review_path.write_text(json.dumps(contract_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.extend([write_contract_ledger(args["output_dir"], contract_ledger), str(review_path.resolve())])
        plog.info(f"Stage 5 写产物 完成: 写入 {len(paths)} 个产物到 {args['output_dir']}")
        _sf_log(f"产物已写入: {list(paths)[:5]}...")

        return {
            "status": "complete",
            "output_dir": args["output_dir"],
            "tier": tier,
            "paths": list(paths),
            "artifacts": list(paths),
            "errors": [],
            "warnings": [],
            "check_feasibility": {
                "passed": tier == TIER_FULL,
                "downgraded_to": None if tier == TIER_FULL else (
                    "TIER_TRIM" if tier == TIER_TRIM else "TIER_CORE"
                ),
                "issues": _results.get("issues", []),
            },
            "method_rounds_used": method_rounds_used,
            "checkpoint_status": checkpoint_status,
        }
    finally:
        # 关闭 4 个子 session holder（aclose 是幂等的，未开 session 也不报错）
        # 注意：human_holder 已删除（人审归 agent 端）
        for holder in (
            method_designer_holder,
            method_critic_holder,
            experiment_planner_holder,
            experiment_critic_holder,
            contract_critic_holder,
        ):
            sess_obj = holder.get("sess")
            if sess_obj is not None:
                try:
                    await sess_obj.aclose()
                except Exception as exc:  # noqa: BLE001
                    _sf_log(f"[planning_main_session] session.aclose 失败: {exc}")
