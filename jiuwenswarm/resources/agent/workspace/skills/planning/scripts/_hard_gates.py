# -*- coding: utf-8 -*-
"""确定性门禁——0 LLM 调用，stage 1/2 复用。

refactor 设计：plan-supervisor/docs/plan-supervisor-refactor-2026-09-01.md §4 Track B。

设计原则
--------
能用一行 Python 判的事，就不让 LLM 评：
- 减少 LLM 漂移（memory: deepseek-v4-flash-planner-drift 记的 4 类 schema 漂移
  ——feedback_addressed 偶返 dict / compute_estimate 偶返嵌套 /
  feasibility_risks 偶返 list[dict] / feedback_addressed.field_path 偶现空索引）
- 节省 LLM token + 等待时间（每轮反思约 30s × 2 件事）
- 抗 hallucination（baseline.paper_id 真实性等纯确定性约束）

调用方
------
- iterate_method_design.iterate_method_design_async 反思循环开头
- plan_experiment_with_data.plan_experiment_with_data_async 反思循环开头

返回 list[str]：空列表 = 通过；非空 = gap 列表，调用方把 gap 列表塞
previous_feedback 让 designer/planner 重写 1 次（条件触发）。
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from scripts.validate_plan import validate_cross_refs, validate_method_design

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.hard_gates")


# ════════════════════════════════════════════════════════════════
# Stage 1: method_design 5 个硬门禁
# ════════════════════════════════════════════════════════════════


def hard_gate_method_design(
    method_design: dict,
    inputs: dict,
) -> list[str]:
    """method_design 阶段 Layer A：返回 gap 列表（空列表 = 通过）。

    检查项（共 7 条）：
      1. 必填字段非空（research_goal / core_mechanism）
      2. framework 是 dict 且 framework.name 非空
      3. components[]（**顶层字段**）的 name + function 双字段（缺一即 gap）
      4. hypothesis_coverage 100% 覆盖 inputs.hypotheses[].id
      5. innovation_points[] 至少 1 个
      6. 完整 MethodDesign schema（validate_method_design 的全部错误）
      7. evidence_metric[].metric_name ∈ 模块三标准指标表（2026-09-17 新增，
         见 _method_design_metric_gaps）

    ⚠️ 门禁 7 是这条约束的**唯一载体**，不要把它写回 method-designer 的 persona：
    实测（2026-09-17）加 12 行指标名清单后，该模型在此处 5/5 丢失顶层结构、
    规划阶段稳定失败。详见 _standard_metric_gap_text 的 docstring。

    Args:
        method_design: designer LLM 返回的方法设计 dict。
        inputs:        8 个输入的 dict（含 hypotheses / references）。

    Returns:
        gap 列表；[] = 通过。
    """
    # 兜底：deepseek-v4-flash 等模型偶尔返 markdown 字符串（refactor 前由 reflection
    # loop + critic 兜底；refactor 后 Layer A 是无条件首道关，必须能消化非 dict）。
    # 返一个 gap 让 reflection loop 把字符串作为 feedback 喂回 designer 重写 1 轮。
    if not isinstance(method_design, dict):
        return [f"method_design 不是 dict（type={type(method_design).__name__}，"
                f"可能 LLM 返了 markdown 字符串）；请严格返单个 JSON object"]

    gaps: list[str] = []

    # 门禁 1: 必填顶层字段非空
    for field in ("research_goal", "core_mechanism"):
        if not str(method_design.get(field, "")).strip():
            gaps.append(f"缺少顶层字段或为空: {field}")

    # 门禁 2: framework.name 非空
    # framework 的形状是 {name, overview, stages}（schema §2.1）。
    # 兜底：deepseek-v4-flash 等模型偶发把 framework 返成 string（如 "Goal-directed search"），
    # 不是 dict。必须在所有 .get() 前 isinstance 检查，否则 AttributeError 直接崩 reflection loop。
    framework = method_design.get("framework")
    if not isinstance(framework, dict):
        return [f"framework 必须是 dict（实际 type={type(framework).__name__}）；"
                f"请改成 {{\"name\": \"...\", \"overview\": \"...\", \"stages\": [...]}}。"
                f"注意 components / hypothesis_coverage / innovation_points 都是 "
                f"method_design 的**顶层字段**，不要嵌进 framework 里"]
    if not str(framework.get("name", "")).strip():
        gaps.append("framework.name 为空")

    # 门禁 3: components[].name + function 双字段
    # 2026-09-09 修 bug：原实现读 framework.get("components")，但 components 是 **顶层字段**
    # （schema §2.1 + 模块三 contracts.py MethodDesign.components 都是顶层），
    # framework 实际形状是 {name, overview, stages} —— 里面从来没有 components。
    # 结果：每轮都往 reflection loop 塞一条假 gap"framework.components[] 为空"，
    # 逼 designer 去修一个不存在的问题，白烧一轮反思。
    comps = method_design.get("components") or []
    if not comps:
        gaps.append("components[] 为空（至少 1 个 component；注意是顶层字段，不在 framework 内）")
    else:
        for i, c in enumerate(comps):
            if not (isinstance(c, dict) and c.get("name") and c.get("function")):
                name = c.get("name") if isinstance(c, dict) else None
                gaps.append(
                    f"components[{i}] 缺 name 或 function（name={name!r}）"
                )

    # 门禁 4: hypothesis_coverage 100%
    # 兼容 deepseek 等 LLM 把 hypothesis_coverage 返成 dict 的情况
    hc_raw = method_design.get("hypothesis_coverage") or []
    if isinstance(hc_raw, dict):
        covered = {k for k in hc_raw.keys() if k}
    else:
        covered = {
            hc.get("hypothesis_id")
            for hc in hc_raw
            if isinstance(hc, dict) and hc.get("hypothesis_id")
        }
    all_ids = {
        h.get("id") for h in (inputs.get("hypotheses") or []) if h.get("id")
    }
    missing = sorted(all_ids - covered)
    if missing:
        gaps.append(
            f"假设未覆盖: {missing}（请在 hypothesis_coverage[] 补全 mechanism + experiment_ref）"
        )

    # 门禁 5: 创新点至少 1 个
    innov = method_design.get("innovation_points") or []
    if not innov:
        gaps.append("innovation_points[] 为空（至少 1 个创新点）")

    # 门禁 7: evidence_metric[].metric_name 必须是模块三的标准指标名。
    # 必须拦在**源头**：planner 有「逐字符照抄 evidence_metric」的硬性采纳义务，
    # 只拦 stage 2 会让它夹在两条互斥要求之间死锁（详见 _method_design_metric_gaps）。
    gaps.extend(_method_design_metric_gaps(method_design, inputs))

    # 门禁 6: 完整 MethodDesign schema。前 5 条用于给常见错误短反馈；这里负责
    # evidence_metric、experiment_ref、hypothesis_coverage 子字段等深层约束。
    # 过去这些只记 warning，导致反思轮耗尽后的坏产物仍被标成 complete。
    for error in validate_method_design(method_design):
        rendered = str(error)
        if rendered not in gaps:
            gaps.append(rendered)

    return gaps


# ════════════════════════════════════════════════════════════════
# Stage 2: experiment_plan 5 个硬门禁
# ════════════════════════════════════════════════════════════════


def _is_dataset_landing_page(source_url: str) -> bool:
    """判断 URL 是否为论文/仓库落地页（不是可下载的数据直链）。

    ⚠️ 契约源：模块三 ``experiment/scripts/dataset_resolver._is_dataset_landing_page``
    （同一份规则）。模块三解析阶段对落地页 URL 直接跳过显式解析、只按数据集名检索，
    检索不到就返回 NOT_FOUND blocker 烧掉一整轮实验。模块二这里多拦一个 = 少烧
    一整轮。**若模块三改规则，此处必须同步**。

    判定为落地页：
    - arxiv / doi / openreview / aclanthology 的论文页；
    - ``github.com/org/repo``（路径段 ≤2 的裸仓库主页）与 ``/blob`` ``/tree`` 浏览页；
      ``releases/download``、``raw`` 等文件直链不算；
    - 任意以 ``/`` 或 ``/abs`` 结尾的目录页（如 ``parl.ai/projects/msc/``）。
    """
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host in {"arxiv.org", "www.arxiv.org", "doi.org", "dx.doi.org"}:
        return True
    if host in {
        "openreview.net", "www.openreview.net",
        "aclanthology.org", "www.aclanthology.org",
    }:
        return True
    if host in {"github.com", "www.github.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        return len(parts) <= 2 or (len(parts) >= 3 and parts[2] in {"blob", "tree"})
    return path.endswith(("/", "/abs"))


def _duplicated(values: list[str]) -> list[str]:
    """返回重复出现的取值（按首次出现顺序，结果去重）。"""
    seen: set[str] = set()
    dups: list[str] = []
    for value in values:
        if value in seen and value not in dups:
            dups.append(value)
        seen.add(value)
    return dups


def _identifier_contract_gaps(experiment_plan: dict) -> list[str]:
    """门禁 1c：标识符唯一性 —— 镜像模块三 ExperimentPlan.identifiers_are_unique。

    ⚠️ 契约源：模块三 ``experiment/scripts/contracts.py::ExperimentPlan.identifiers_are_unique``
    （同一份规则，**模块三改规则时此处必须同步**）。那是 pydantic ``model_validator``，
    模块二产物一进投影层（``_experiment_bootstrap.write_stage3_inputs``）就 raise →
    ``projection_report.status=invalid`` → 顶层 ``aborted_experiment_replan``。这条路径
    **不产生 planning_feedback**（模块三还没开跑），planner 收不到任何回馈，所以必须在
    Layer A 就拦住，借补写轮让它重生成。

    历史事故（2026-09-17）：REPLAN 重生成的计划里出现两个同名 baseline
    ``Naive-Vector-RAG``（只有 metric_name 不同）。主路径恰好没触发，REPLAN 一重生成
    就漏过去，投影报 ``baselines names must be unique`` → 顶层直接 aborted。
    注意**不能**由门禁自动去重/改名：模块三按名字反查实现清单（``planning_adapter``
    要求矩阵里的实现名与 baselines 一致），自动改名会把 blocker 换个地方爆。

    gap 文案**不得包含 "source_url=" 子串**：数据源专项自修复循环用
    ``"source_url=" in gap`` 过滤，混进去会让命名问题被当成数据源问题白烧 3 轮。
    """
    gaps: list[str] = []

    for field, label in (("datasets", "数据集名"), ("baselines", "基线名")):
        names = [
            item["name"].strip()
            for item in (experiment_plan.get(field) or [])
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].strip()
        ]
        if dups := _duplicated(names):
            gaps.append(
                f"{field}[].name 重复：{dups}（{label}必须唯一）。模块三 "
                f"ExperimentPlan.identifiers_are_unique 会拒收整份计划"
                f"（experiment_plan.{field} names must be unique），投影层报 invalid 后"
                f"顶层 aborted，planner 拿不到反馈。修法：给重名项改成互不相同的名字，"
                f"并**同步更新 experiment_matrix 中引用它的每一行**——模块三按名字反查"
                f"实现清单，只改这里会让矩阵引用落空；也不要靠删掉其中一条来消重。"
            )

    # metrics 兼容 list[str] 与 list[dict]{"name":...} 两种形态（见门禁 3 注释）
    metric_names = [
        str(m.get("name") if isinstance(m, dict) else m).strip()
        for m in (experiment_plan.get("metrics") or [])
        if (isinstance(m, dict) and m.get("name")) or isinstance(m, str)
    ]
    metric_names = [name for name in metric_names if name]
    if dups := _duplicated(metric_names):
        gaps.append(
            f"metrics 重复：{dups}（指标名必须唯一）。模块三 "
            f"ExperimentPlan.identifiers_are_unique 会拒收整份计划"
            f"（experiment_plan.metrics must be unique）；同一指标只登记一次。"
        )

    primary_ids = [
        str(e.get("id") if isinstance(e, dict) else e).strip()
        for e in (experiment_plan.get("primary_experiments") or [])
    ]
    primary_ids = [item for item in primary_ids if item]
    if dups := _duplicated(primary_ids):
        gaps.append(
            f"primary_experiments 重复：{dups}（主实验 id 必须唯一）。模块三 "
            f"ExperimentPlan.identifiers_are_unique 会拒收整份计划"
            f"（experiment_plan.primary_experiments must be unique）；"
            f"同一实验拆成多条请改用 experiment_matrix 的变体列表达。"
        )

    known_metrics = set(metric_names)
    unknown_metrics = sorted({
        item["metric_name"].strip()
        for item in (experiment_plan.get("baselines") or [])
        if isinstance(item, dict)
        and isinstance(item.get("metric_name"), str)
        and item["metric_name"].strip()
        and item["metric_name"].strip() not in known_metrics
    })
    if unknown_metrics:
        gaps.append(
            f"baselines[].metric_name 不在 metrics 内：{unknown_metrics}。模块三会拒收"
            f"（baseline metric_name values must be planned metrics）；请把这些指标补进 "
            f"metrics（并保证 success_criteria 覆盖），或把基线改绑一个已规划的指标。"
        )

    return gaps


def _matrix_implementation_names(experiment_plan: dict) -> list[str]:
    """矩阵第 3 列起**不含 `=`** 的 token —— 模块三眼里的"实现名"。

    ⚠️ 契约源：模块三 ``plan_execution.planned_method_names``（去重前的同一套取法）。
    ``implementation_builder`` 拿它判定 ``ablation_plan[].removed_by`` 是否
    "已精确登记为实验矩阵实现"。**模块三改取法时此处必须同步**。
    """
    names: list[str] = []
    for row in experiment_plan.get("experiment_matrix") or []:
        if not isinstance(row, list):
            continue
        for token in row[2:]:
            if not isinstance(token, str):
                continue
            cleaned = token.strip()
            if cleaned and "=" not in cleaned and cleaned not in names:
                names.append(cleaned)
    return names


def _row_parameter_pairs(row: list) -> list[tuple[str, str]]:
    """把一行的 ``key=value`` token 拆成 **pair 列表**（不是 dict）。

    用 dict 会让重复键互相覆盖——2026-09-17 事故里 planner 把两个消融压进同一行，
    ``ablation=EvidenceGateWriter`` / ``ablation=ConsistencyReranker`` 在 dict 里只剩
    后一个，重复就此隐身（模块三按原始 token 列表校验，直接拒收整份请求）。
    需要唯一性判定时用 :func:`_matrix_contract_gaps`，别在这里靠 dict 兜。
    """
    pairs: list[tuple[str, str]] = []
    for token in row[2:]:
        if not isinstance(token, str):
            continue
        cleaned = token.strip()
        if "=" not in cleaned:
            continue
        key, _, value = cleaned.partition("=")
        pairs.append((key.strip(), value.strip()))
    return pairs


def _ablation_row_implementation_names(
    experiment_plan: dict, component: str
) -> list[str]:
    """找出该组件对应的消融行（``EXP-ABL-*`` + ``ablation=<component>``）用的实现名。

    用来给 gap 做**定向**建议：告诉 planner「你这条消融行的第 3 列现在写的是什么」，
    它把 removed_by 改成同一个名字即可，不必重新设计矩阵。
    """
    names: list[str] = []
    for row in experiment_plan.get("experiment_matrix") or []:
        if not isinstance(row, list) or not row:
            continue
        if not (isinstance(row[0], str) and row[0].strip().upper().startswith("EXP-ABL-")):
            continue
        # pair 列表 + 任一次匹配：同一行写了多个 ablation= 时也能找对（dict 会只剩最后一个）
        ablated_components = {
            value for key, value in _row_parameter_pairs(row) if key == "ablation"
        }
        row_names: list[str] = []
        for token in row[2:]:
            if not isinstance(token, str):
                continue
            cleaned = token.strip()
            if cleaned and "=" not in cleaned and cleaned not in row_names:
                row_names.append(cleaned)
        if component and component not in ablated_components:
            continue
        for name in row_names:
            if name not in names:
                names.append(name)
    return names


def _matrix_contract_gaps(experiment_plan: dict) -> list[str]:
    """门禁 1e：矩阵的**行形状 / 参数键 / 引用完整性**规则 —— 模块三
    ExperimentModuleInput 会在投影层 fail-closed 拒收整份请求。

    ⚠️ 契约源：模块三 ``contracts.py::ExperimentModuleInput.validate_cross_references``
    —— 行 ≥4 列 / 前 4 列非空 / key=value 参数键非空且行内唯一 /
    ``primary_experiments ⊆ 矩阵首列`` / 矩阵第二列 ∈ ``datasets[].name``。
    **模块三改规则时此处必须同步**。

    这五条是同一类死路：pydantic 在投影层 raise → ``projection_report.status=invalid``
    → 顶层 ``aborted_experiment_replan``，**全程不产生 planning_feedback**，planner
    连"哪里错了"都收不到。所以只能在这里拦。只挑模块三真有的规则，不加严——规划侧
    更严会白烧 planner 重写轮。

    为什么要在这里重写一遍：`validate_plan._check_experiment_matrix` **已经**实现了
    这套规则（规则 1/6），但它在 `plan_experiment_with_data.py` 里是 **warn-only**——
    只写一行 log，永远进不了 `previous_feedback`，planner 看不到、也就不会改。
    而模块三这条失败发生在**投影层 pydantic 校验**（``<root>: experiment_matrix
    parameters require unique non-empty keys``），走的是
    ``projection_report.status=invalid`` → 顶层 ``aborted_experiment_replan``，
    **不产生 planning_feedback**——和门禁 1c 是同一条死路，只能在 Layer A 拦。

    历史事故（2026-09-17）：planner 想把两个消融压进同一行，写成了
    ``[… "GWCR-Mem", "ablation=EvidenceGateWriter", "ablation=ConsistencyReranker", …]``
    （第 8、10 行），模块三直接拒收；规划侧既没门禁、`validate_experiment_plan`
    又只是 warning，跑了三轮都没人告诉 planner 错在哪。
    """
    matrix = experiment_plan.get("experiment_matrix")
    if not isinstance(matrix, list):
        return []

    malformed: list[int] = []
    blank_required: list[int] = []
    duplicate_keys: list[tuple[int, str, list[str]]] = []
    matrix_ids: set[str] = set()
    matrix_datasets: set[str] = set()
    for index, row in enumerate(matrix):
        if not isinstance(row, list):
            malformed.append(index)
            continue
        if len(row) < 4:
            malformed.append(index)
            continue
        if isinstance(row[0], str) and row[0].strip():
            matrix_ids.add(row[0].strip())
        if isinstance(row[1], str) and row[1].strip():
            matrix_datasets.add(row[1].strip())
        if any(not isinstance(item, str) or not item.strip() for item in row[:4]):
            blank_required.append(index)
        keys = [key for key, _ in _row_parameter_pairs(row)]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            first = row[0].strip() if isinstance(row[0], str) else ""
            duplicate_keys.append((index, first, keys))

    # primary_experiments 兼容 list[str] 与 list[dict]{"id":...} 两种形态（LLM 漂移）
    primary_ids = {
        str(item.get("id") if isinstance(item, dict) else item).strip()
        for item in (experiment_plan.get("primary_experiments") or [])
    }
    primary_ids.discard("")
    missing_primary = sorted(primary_ids - matrix_ids)
    # datasets 同理：只认 names（模块三按 datasets[].name 比对 row[1]）
    declared_datasets = {
        str(item.get("name") if isinstance(item, dict) else "").strip()
        for item in (experiment_plan.get("datasets") or [])
    }
    declared_datasets.discard("")
    unknown_datasets = sorted(matrix_datasets - declared_datasets) if declared_datasets else []

    gaps: list[str] = []
    if malformed:
        gaps.append(
            f"experiment_matrix 行格式需 ≥4 列 [experiment_id, dataset_name, "
            f"method_or_baseline, variant/参数, ...]；不合格行号: {malformed}。"
            f"模块三会拒收整份请求（experiment_matrix rows must follow …）。"
        )
    if blank_required:
        gaps.append(
            f"experiment_matrix 前 4 列不得为空；不合格行号: {blank_required}。"
            f"模块三会拒收整份请求（first four values cannot be blank）。"
        )
    for index, first_cell, keys in duplicate_keys:
        # 定向建议：重复的是 ablation= 时，根因几乎总是"想在一行里表达多个消融"。
        # 但**消融行**与**主实验行**的正确修法不同，别混为一谈：
        #   - EXP-ABL-* 行：模块三约定一条消融一行（X7 按行只取一个 ablation 值）
        #   - 主实验行：组合变体应该走 key=value 参数（egw=off / car=off 这类），
        #     ablation= 是 EXP-ABL- 行专用的
        dupes = _duplicated(keys)
        if not all(keys):
            dupes = sorted({*dupes, ""})
        hint = ""
        if "ablation" in keys:
            if first_cell.upper().startswith("EXP-ABL-"):
                hint = (
                    "  ⚠️ 重复的是 `ablation=`：消融行必须**一条消融一行**"
                    "（首列 EXP-ABL-{COMPONENT}，行尾 ablation=<component>），"
                    "不能把两个消融压进同一行——模块三的 X7 校验按行只取一个 "
                    "ablation 值，压行会让第二个组件失去登记。请把该行拆成两行。"
                )
            else:
                hint = (
                    f"  ⚠️ 重复的是 `ablation=`，而这是主实验行（首列 {first_cell!r}，"
                    "非 EXP-ABL- 前缀）：`ablation=<component>` 是消融行专用参数。"
                    "主实验行要表达组合变体时请用 key=value 参数（如 `egw=off` / "
                    "`car=off`），或把该组合拆成独立的 EXP-ABL-{COMPONENT} 行。"
                )
        gaps.append(
            f"experiment_matrix[{index}] 的 key=value 参数 key 重复或为空：{keys}。"
            f"模块三 ExperimentModuleInput 会在**投影层 pydantic 校验**直接拒收整份"
            f"请求（experiment_matrix parameters require unique non-empty keys），"
            f"该路径**不产生 planning_feedback**，planner 收不到任何回馈，"
            f"只能在 Layer A 这里拦住。重复/空的 key: {dupes}。"
            f"同一行内每个参数 key 只能出现一次。{hint}"
        )
    if missing_primary:
        gaps.append(
            f"primary_experiments 里的 {missing_primary} 未出现在 experiment_matrix "
            f"首列。模块三会在投影层拒收（primary experiments missing from "
            f"experiment_matrix），该路径不产生 planning_feedback。"
            f"每个主实验 id 至少要有一行首列是它。当前矩阵首列: {sorted(matrix_ids)}。"
        )
    if unknown_datasets:
        gaps.append(
            f"experiment_matrix 第二列用了未声明的数据集 {unknown_datasets}。模块三会在"
            f"投影层拒收（experiment_matrix uses unknown datasets），该路径不产生 "
            f"planning_feedback。必须与 datasets[].name 逐字符一致；"
            f"当前已声明: {sorted(declared_datasets)}。"
        )
    return gaps


def _ablation_contract_gaps(experiment_plan: dict) -> list[str]:
    """门禁 1d：``ablation_plan[].removed_by`` 必须是矩阵里的实现名。

    ⚠️ 契约源：模块三 ``implementation_builder.build_implementations`` ——
    ``if ablation.removed_by not in methods`` 直接 fail-closed 拒收，类别 ``[experiment]``，
    整轮 REPLAN 打回重做。而模块三自己的建议写得很明确：「为每个消融提供独立实验ID和
    可执行实现名；**不得从自然语言猜测消融代码**」——所以修法在规划侧把 removed_by
    约束成实现名，**不是**让模块三放宽去解析自然语言。

    历史事故（2026-09-17）：planner 把 removed_by 写成「将 egw 开关置为 off，所有候选
    记忆无条件写入…」这类散文。实测当时**所有**真实计划的每一条 removed_by 都是散文
    （无一命中矩阵实现名），两轮 REPLAN 因此完全复现同一 blocker；且该 blocker 含
    "实现"字样被自动归类成 ``[method]``，把 REPLAN 路由去重跑 method-designer，
    后者结构化输出重试耗尽 → planning error → 顶层 ``aborted_planning_replan``。

    注意 ``component`` 名本身**不算**实现名：矩阵里它是 ``ablation=<component>`` 参数
    （含 ``=``），模块三的取法会跳过它。所以这条 gap 不能靠「把 removed_by 改成组件名」
    修好——必须指向矩阵第 3 列起那个裸实现名。
    """
    ablations = experiment_plan.get("ablation_plan")
    if not isinstance(ablations, list) or not ablations:
        return []
    methods = _matrix_implementation_names(experiment_plan)
    method_set = set(methods)
    offenders: list[tuple[int, str, str, list[str]]] = []
    for index, ablation in enumerate(ablations):
        if not isinstance(ablation, dict):
            continue
        removed_by = ablation.get("removed_by")
        if not isinstance(removed_by, str) or not removed_by.strip():
            continue  # 缺字段/空串由 validate_experiment_plan 管，不在这里重复报
        removed_by = removed_by.strip()
        if removed_by in method_set:
            continue
        component = str(ablation.get("component") or "").strip()
        offenders.append((
            index, removed_by, component,
            _ablation_row_implementation_names(experiment_plan, component),
        ))
    if not offenders:
        return []

    # 规则说明只写一次（4 条消融命中时逐条重复同一段说明会把 feedback 撑成一片噪声，
    # 反而盖住真正要改的那几个字段名）。
    gaps = [
        f"ablation_plan[].removed_by 必须是**实验矩阵里的实现名**（矩阵第3列起不含 "
        f"`=` 的 token）。模块三 implementation_builder 对此 fail-closed 拒收整份方案"
        f"（「未精确登记为实验矩阵实现」），类别 [experiment]，整轮 REPLAN 打回重做。"
        f"**禁止**写「将 xx 开关置为 off」这类自然语言——模块三不会从自然语言猜消融"
        f"代码，它自己的建议原文就是「为每个消融提供独立实验ID和可执行实现名」。"
        f"注意组件名**不算**实现名：它在矩阵里是 `ablation=<component>` 参数（含 `=`），"
        f"会被跳过；必须指向第3列那个裸实现名。"
        f"当前矩阵中的实现名: {methods}。"
    ]
    for index, removed_by, component, row_names in offenders:
        if row_names:
            fix = (
                f"该组件对应的消融行（EXP-ABL-* + ablation={component}）第3列现在是 "
                f"{row_names}；把 removed_by 改成其中那个实现名即可。确实需要独立变体"
                f"时，另给它起一个实现名并**同时**写进消融行第3列（只改一处会被"
                f"「矩阵未包含该实现名」打回）。"
            )
        else:
            fix = (
                f"该组件（{component!r}）没有对应的消融行（EXP-ABL-* 首列 + "
                f"ablation=<component> 参数）；先补这一行，再让 removed_by 指向它的"
                f"第3列实现名。"
            )
        gaps.append(
            f"ablation_plan[{index}].removed_by={removed_by!r} 不是矩阵实现名。{fix}"
        )
    return gaps


#: 模块三**标准指标表**（镜像 `experiment/scripts/implementation_builder.py` 的
#: `_CLASSIFICATION_METRICS` / `_REGRESSION_METRICS` 与
#: `retrieval_metrics.RETRIEVAL_METRICS`）。**模块三改表时此处必须同步**。
#:
#: 为什么门禁要拦：`manifest.metrics[].verified` 只对标准指标置 True，而
#: `review_contracts` 对任何未验证指标直接报 error。planner 自造指标名（`key_recall`
#: 这类）**没有任何途径**通过——整个 stage3 会白跑一轮再被打回。在这里拦住，
#: 让它在 stage 2 内部就改成标准名，代价从"一整轮实验"降到"一次 planner 重写"。
_STANDARD_METRIC_NAMES: frozenset[str] = frozenset({
    # classification
    "accuracy", "top-1_acc", "macro_f1", "micro_f1", "weighted_f1",
    "train_time_seconds", "inference_time_seconds", "total_time_seconds",
    # regression
    "mae", "mse", "rmse", "r2",
    # retrieval / ranking（不带 k 的写法；带 k 见 _K_SUFFIX_RE）
    "recall_at_k", "precision_at_k", "hit_rate_at_k", "mrr", "ndcg_at_k",
    # fixed-revision memory-compression QA executor
    "compression_ratio", "qa_token_f1", "answer_exact_match",
    "original_token_count", "compressed_token_count",
})

#: `recall@10` / `ndcg@5` 这类带 k 的写法（模块三归一后是 `<name>_at_k`）。
_METRIC_K_SUFFIX_RE = re.compile(r"(?:@|_at_)(\d+)$")


def _normalize_metric_name(metric: str) -> str:
    """与模块三 `_split_metric_k` 同一套归一：小写、去空格、连字符转下划线、
    ``@k`` / ``_at_<n>`` 折成 ``_at_k``。"""
    normalized = str(metric).strip().casefold().replace("-", "_").replace(" ", "")
    match = _METRIC_K_SUFFIX_RE.search(normalized)
    return normalized[: match.start()] + "_at_k" if match else normalized


#: 归一化后的标准名集合。两侧都用同一套归一，避免 `top-1_acc` vs `top_1_acc`
#: 这种"看起来一样但字符串不等"的假阳性。
_STANDARD_METRIC_NAMES_NORMALIZED: frozenset[str] = frozenset(
    _normalize_metric_name(name) for name in _STANDARD_METRIC_NAMES
)


def _is_standard_metric(metric: str) -> bool:
    """该指标名能否被模块三认成标准指标（与模块三同一套归一规则）。"""
    return _normalize_metric_name(metric) in _STANDARD_METRIC_NAMES_NORMALIZED


#: 标准指标名的**唯一权威清单**。方法设计、实验规划与模块三三处共用，
#: 改表时只改这里 + `experiment/scripts/retrieval_metrics.py`。
def _standard_metric_gap_text(unknown: list[str], *, owner: str) -> str:
    """标准指标门禁的 gap 文案。

    ``owner`` 决定"同步更新"那句点哪些字段——方法设计师改不到
    ``success_criteria`` / ``baselines``，那些是 planner 的地盘；对它说这些只会
    让反馈变模糊、降低一次改对的概率。

    ⚠️ 这条约束**只写在门禁里，不写进 method-designer 的 persona**。2026-09-17
    实测：往 persona 里加 12 行指标名清单后，deepseek-v4-flash-vision-exp 在
    method-designer 上 **5/5 丢失顶层结构**（把 innovation_points 的值平铺成根级
    数组，框架 3 次 schema 重试后放弃，`aborted_planning`）；同一输入只换回旧
    persona 就通过。模型其实**照做了**指标名要求（输出里全是 recall@10 / ndcg@10），
    代价是结构化输出崩掉——那段把注意力全压在 evidence_metric 这个深层字段上。
    靠门禁拦 + 一轮定向重写同样能达成约束，且不碰脆弱的结构化输出。
    """
    available = sorted(_STANDARD_METRIC_NAMES)
    sync_clause = (
        "并**同步更新**所有引用它的地方（evidence_metric[].metric_name、"
        "claim 里对它的描述、hypothesis_coverage 的 mechanism）"
        if owner == "method_design"
        else "并**同步更新**所有引用它的地方（success_criteria、"
        "baselines[].metric_name、expected_results）"
    )
    return (
        f"{owner} 里有模块三不认识的指标名：{unknown}。模块三只对**标准指标**"
        f"（公式公认、可确定性计算）置 verified=true，其余一律拒收——自造指标名"
        f"没有任何途径变成已验证，整轮实验会白跑。可用指标名：{available}"
        f"（带 k 的检索指标写成 `recall@10` / `precision@10` / `ndcg@10` / "
        f"`hit_rate@10` 这类形式，k 按你的实验设定）。"
        f"请把自造名替换成上表中的标准名，{sync_clause}。"
        f"研究结论要建立在这些标准指标上，而不是新造一个名字——"
        f"想表达「无关记忆更少」，用 `precision@10`（无关项变多时查准率自然下降）。"
    )


def _execution_profile(inputs: dict) -> dict | None:
    profiles = ((inputs.get("execution_capability") or {}).get("profiles") or [])
    return profiles[0] if len(profiles) == 1 and isinstance(profiles[0], dict) else None


def _method_design_metric_gaps(method_design: dict, inputs: dict | None = None) -> list[str]:
    """门禁 7（method_design）：``evidence_metric[].metric_name`` 必须是标准指标名。

    **为什么必须拦在源头**：`evidence_metric[].metric_name` 是指标名的**出处**——
    experiment-planner 的 persona 有硬性「采纳义务」，要求把 method_design 里的
    `evidence_metric[].metric_name` **逐字符照抄**进 `metrics[]`。所以：
      - 只拦 stage 2 → planner 被夹在「必须照抄」和「必须是标准名」之间，**死锁**；
      - 拦在 stage 1 → method-designer 改成标准名，planner 照抄即通过。
    """
    unknown: set[str] = set()
    for field in ("innovation_points", "hypothesis_coverage"):
        for item in method_design.get(field) or []:
            if not isinstance(item, dict):
                continue
            # ⚠️ 兼容两种形态：schema 要求 `list[dict]`（取 metric_name），
            # 但**真实产物里是纯字符串**（method-designer 漂移，见
            # test/run/stage3_experiment_replan_2 的 `"evidence_metric": "irrelevant_ratio"`）。
            # 只认 list[dict] 会让这条门禁在真实产物上完全看不见东西。
            raw = item.get("evidence_metric")
            candidates: list[str] = []
            if isinstance(raw, str):
                candidates.append(raw)
            elif isinstance(raw, list):
                for evidence in raw:
                    if isinstance(evidence, dict) and isinstance(evidence.get("metric_name"), str):
                        candidates.append(evidence["metric_name"])
            for name in candidates:
                if name.strip() and not _is_standard_metric(name):
                    unknown.add(name.strip())
    gaps = [_standard_metric_gap_text(sorted(unknown), owner="method_design")] if unknown else []
    profile = _execution_profile(inputs or {})
    allowed = {str(item) for item in (profile or {}).get("metric_names") or []}
    if allowed:
        declared = {
            str(evidence.get("metric_name") or "").strip()
            for field in ("innovation_points", "hypothesis_coverage")
            for item in method_design.get(field) or [] if isinstance(item, dict)
            for evidence in (item.get("evidence_metric") or []) if isinstance(evidence, dict)
            if str(evidence.get("metric_name") or "").strip()
        }
        outside = sorted(declared - allowed)
        if outside:
            gaps.append(
                f"method_design 指标 {outside} 超出已匹配执行能力 {profile.get('profile_id')!r}；"
                f"只能使用 {sorted(allowed)}。用 qa_token_f1 对 uncompressed_context 做比较表达 QA 保真，"
                "不要改用通用 classification/retrieval 指标。"
            )
    unsupported_claims = [
        str(value).strip()
        for value in (profile or {}).get("unsupported_method_claims") or []
        if str(value).strip()
    ]
    if unsupported_claims:
        corpus = json.dumps(method_design, ensure_ascii=False).casefold()
        found = sorted({claim for claim in unsupported_claims if claim.casefold() in corpus})
        if found:
            gaps.append(
                f"method_design 声明了当前注册执行器不会执行的机制 {found}。"
                f"请严格按执行能力 {profile.get('profile_id')!r} 的 planning_constraints "
                "重写方法机制、组件、技术路线与创新点；这些机制不能留到 writing 再降级。"
            )
    return gaps


def _execution_capability_gaps(experiment_plan: dict, inputs: dict) -> list[str]:
    profile = _execution_profile(inputs)
    if not profile:
        return []
    gaps: list[str] = []
    task_type = str(profile.get("task_type") or "").strip()
    allowed_metrics = {str(value) for value in profile.get("metric_names") or []}
    allowed_methods = {str(value) for value in profile.get("method_ids") or []}
    primary_method = str(profile.get("primary_method_id") or "").strip()
    allowed_baselines = {
        str(value).strip() for value in profile.get("baseline_method_ids") or []
        if str(value).strip()
    }
    metrics = {
        str(item.get("name") if isinstance(item, dict) else item).strip()
        for item in experiment_plan.get("metrics") or []
        if str(item.get("name") if isinstance(item, dict) else item).strip()
    }
    if allowed_metrics and metrics - allowed_metrics:
        gaps.append(
            f"experiment_plan 指标 {sorted(metrics - allowed_metrics)} 超出执行能力 {profile.get('profile_id')!r}；"
            f"只能使用 {sorted(allowed_metrics)}。"
        )
    wrong_task = [
        {"dataset": item.get("name"), "task_type": item.get("task_type")}
        for item in experiment_plan.get("datasets") or [] if isinstance(item, dict)
        if str(item.get("task_type") or "").strip() != task_type
    ]
    if task_type and wrong_task:
        gaps.append(f"datasets[].task_type 必须逐项为 {task_type!r}，当前不一致：{wrong_task}")
    if allowed_methods:
        baseline_methods = {str(item.get("name") or "").strip() for item in experiment_plan.get("baselines") or [] if isinstance(item, dict)}
        matrix_methods = {str(row[2] or "").strip() for row in experiment_plan.get("experiment_matrix") or [] if isinstance(row, list) and len(row) >= 3}
        outside = sorted((baseline_methods | matrix_methods) - allowed_methods - {""})
        if outside:
            gaps.append(
                f"实验实现名 {outside} 未登记；只能使用固定方法 {sorted(allowed_methods)}，"
                "并同步修改 baselines[].name、experiment_matrix 与 ablation_plan[].removed_by。"
            )
        if primary_method and primary_method in baseline_methods:
            gaps.append(
                f"主方法 {primary_method!r} 不能出现在 baselines[].name；"
                f"基线只能使用 {sorted(allowed_baselines)}。"
            )
        outside_baselines = sorted(baseline_methods - allowed_baselines) if allowed_baselines else []
        if outside_baselines:
            gaps.append(
                f"baselines[].name 包含非基线方法 {outside_baselines}；"
                f"执行能力只登记了基线 {sorted(allowed_baselines)}。"
            )
        if primary_method:
            methods_by_experiment: dict[str, set[str]] = {}
            for row in experiment_plan.get("experiment_matrix") or []:
                if isinstance(row, list) and len(row) >= 3:
                    methods_by_experiment.setdefault(str(row[0]), set()).add(str(row[2]).strip())
            missing_primary = sorted(
                str(experiment_id)
                for experiment_id in experiment_plan.get("primary_experiments") or []
                if primary_method not in methods_by_experiment.get(str(experiment_id), set())
            )
            if missing_primary:
                gaps.append(
                    f"主实验 {missing_primary} 缺少注册主方法 {primary_method!r} 的矩阵行。"
                )
    preferred = [
        item for item in profile.get("preferred_datasets") or []
        if isinstance(item, dict)
    ]
    if preferred:
        actual = [
            {
                "name": str(item.get("name") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "license": str(item.get("license") or "").strip(),
                "task_type": str(item.get("task_type") or "").strip(),
            }
            for item in experiment_plan.get("datasets") or []
            if isinstance(item, dict)
        ]
        expected = [
            {
                "name": str(item.get("name") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "license": str(item.get("license") or "").strip(),
                "task_type": str(item.get("task_type") or task_type).strip(),
            }
            for item in preferred
        ]
        if actual != expected:
            gaps.append(
                f"本次已注册执行能力要求 datasets 逐字采用 {expected}，当前为 {actual}。"
                "请同步更新 experiment_matrix、success_criteria 和数据规模说明；"
                "不要添加未经执行器验证的第二数据集。"
            )
    return gaps


def _standard_metric_gaps(experiment_plan: dict) -> list[str]:
    """门禁 1f：``metrics[]`` 必须是模块三认得的**标准指标名**。

    ⚠️ 契约源：模块三 ``implementation_builder._standard_metric_definition`` ——
    只对标准指标返回 ``verified=True``，其余一律 ``None``；而
    ``review_contracts.deterministic_static_review`` 对 ``verified=False`` 的指标
    直接报 error，整轮 stage3 白跑。

    历史事故（2026-09-17 静态审查 F-02/F-03）：planner 为检索任务自造了
    ``key_recall`` / ``irrelevant_ratio`` / ``contradiction_ratio``，三者都不在标准表里，
    模块三无从验证——**它们没有任何途径变成"已验证"**，任务必然停在实现阶段。
    """
    metrics = experiment_plan.get("metrics")
    if not isinstance(metrics, list):
        return []
    names: list[str] = []
    for item in metrics:  # 兼容 list[str] 与 list[dict]{"name":...} 两种形态
        value = item.get("name") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    unknown = sorted({name for name in names if not _is_standard_metric(name)})
    return [_standard_metric_gap_text(unknown, owner="experiment_plan")] if unknown else []


def _task_metric_compatibility_gaps(experiment_plan: dict) -> list[str]:
    """拦截“名字标准、放到当前任务却没有定义”的指标组合。

    ``accuracy`` 对分类是标准指标，但对 retrieval 没有唯一含义。旧门禁只做全局
    名称检查，导致 ``recall@10 + accuracy`` 被放行，直到模块三才 REPLAN。
    这里不猜 accuracy 是回答正确率还是 hit@1，只要求规划把任务和定义说清楚。
    """
    raw_metrics = experiment_plan.get("metrics")
    if not isinstance(raw_metrics, list):
        return []
    metrics = {
        _normalize_metric_name(item.get("name") if isinstance(item, dict) else item)
        for item in raw_metrics
        if isinstance(item.get("name") if isinstance(item, dict) else item, str)
    }
    datasets = experiment_plan.get("datasets")
    task_types = {
        str(item.get("task_type")).strip().casefold()
        for item in datasets or []
        if isinstance(item, dict) and str(item.get("task_type") or "").strip()
    }
    retrieval_metrics = {
        name for name in metrics
        if name.startswith(("recall@", "precision@", "hit_rate@", "ndcg@"))
        or name in {"mrr", "recall_at_k", "precision_at_k", "hit_rate_at_k", "ndcg_at_k"}
    }
    classification_only = metrics & {
        "accuracy", "top_1_acc", "macro_f1", "micro_f1", "weighted_f1"
    }
    retrieval_declared = bool(task_types & {"retrieval", "ranking"})
    mixed_without_explicit_hybrid = bool(retrieval_metrics and classification_only)
    if not retrieval_declared and not mixed_without_explicit_hybrid:
        return []
    if not classification_only:
        return []
    return [
        "指标与任务类型不兼容或含义不唯一：检索/排序实验同时声明了 "
        f"{sorted(classification_only)}。accuracy/F1 不能自动解释成 hit@1 或检索命中率；"
        "当前模块三只为 retrieval 提供已验证的 recall@k、precision@k、hit_rate@k、"
        "mrr、ndcg@k；generated-implementation 只生成实验方法实现，不会把自然语言"
        "metric_definitions 变成受信任的新指标执行器。请从 metrics、success_criteria、"
        "baselines[].metric_name 和创新点 evidence_metric 中删除 accuracy/F1，改用上述"
        "已实现检索指标。不要改成 QA、hybrid_retrieval_qa 或其他当前未注册的任务类型。"
    ]


def hard_gate_experiment_plan(
    experiment_plan: dict,
    inputs: dict,
    method_design: dict | None = None,
) -> list[str]:
    """experiment_plan 阶段 Layer A：返回 gap 列表。

    检查项（共 9 条）：
      1.  必填顶层非空（primary_experiments / baselines / metrics / datasets）
      1b. datasets[].source_url 不是论文/仓库落地页（要数据直链）
      1c. 标识符唯一性 + baseline.metric_name ∈ metrics（2026-09-17 新增，
          镜像模块三 ExperimentPlan.identifiers_are_unique —— 见 _identifier_contract_gaps）
      1d. ablation_plan[].removed_by ∈ 矩阵实现名（2026-09-17 新增，
          镜像模块三 implementation_builder 的 fail-closed 判定 —— 见 _ablation_contract_gaps）
      1e. 矩阵行形状（≥4 列/前 4 列非空）+ key=value 参数键非空且行内唯一
          （2026-09-17 新增，镜像模块三 ExperimentModuleInput 的投影层校验
          —— 见 _matrix_contract_gaps）
      2.  baseline.paper_id ∈ key_papers.id（防 hallucination）
      3.  metric × success_criteria 覆盖率 ≥ 70%
      4.  compute_estimate 是 number
      5.  primary_experiments[].id 非空
      6.  与 method_design 的跨产物交叉引用（2026-09-09 新增，见 planning-schemas.md §5.1）

    门禁 6 为什么放这里：这是**唯一**能让 planner 拿到定向反馈重写一轮的位置。
    这些不一致再往后走就是模块三 fail-closed 拒收整个请求（`unplanned experiment
    references` / `innovation metrics not in plan`），到那时只能整条重跑。
    根因是流水线顺序——method-designer 跑在 planner 之前，planner 必须**采纳**
    它已经写下的 experiment_ref 与指标名，而不是另起一套。

    Args:
        experiment_plan: planner LLM 返回的实验方案 dict。
        inputs:          8 个输入的 dict（含 key_papers / references / research_question）。
        method_design:   第 1 阶段的方法设计 dict；None 时跳过门禁 6。

    Returns:
        gap 列表；[] = 通过。
    """
    # 兜底：见 hard_gate_method_design 同款注释（防 LLM 返 markdown 字符串炸 reflection）
    if not isinstance(experiment_plan, dict):
        return [f"experiment_plan 不是 dict（type={type(experiment_plan).__name__}，"
                f"可能 LLM 返了 markdown 字符串）；请严格返单个 JSON object"]

    gaps: list[str] = []

    # 门禁 1: 必填顶层字段非空
    for field in ("primary_experiments", "baselines", "metrics", "datasets"):
        if not experiment_plan.get(field):
            gaps.append(f"缺少顶层字段或为空: {field}")

    # 门禁 1b: 数据来源必须是数据文件直链，论文页/仓库页不能冒充下载地址。
    # 这里只判 URL 的确定性类别，不联网猜数据是否存在。命中后把反馈交回
    # experiment-planner，让它同步替换 dataset、实验矩阵、基线与成功标准。
    # 判定规则与模块三 dataset_resolver._is_dataset_landing_page 对齐（见上方函数
    # docstring）——历史事故：只拦论文页时，``github.com/org/repo`` 这类裸仓库页
    # 一路放行到模块三，被模块三判为落地页 → 数据 NOT_FOUND → 白烧一轮 REPLAN。
    for i, dataset in enumerate(experiment_plan.get("datasets") or []):
        if not isinstance(dataset, dict):
            continue
        source_url = dataset.get("source_url")
        if not isinstance(source_url, str) or not source_url.strip():
            continue
        if _is_dataset_landing_page(source_url.strip()):
            dataset_name = dataset.get("name") or f"datasets[{i}]"
            gaps.append(
                f"datasets[{i}].source_url={source_url!r} 是论文/仓库落地页，"
                f"不是可下载数据直链；请替换数据集 {dataset_name!r}：优先 "
                "Hugging Face datasets 文件页/raw 直链、Zenodo 文件页、"
                "GitHub releases/download 等官方下载端点，并同步更新 "
                "experiment_matrix、相关 baseline、success_criteria 与 usage。"
                "候选必须按当前任务类型、必需字段、许可证和指标可计算性逐项核验；"
                "不得只改 URL 而保留不兼容的数据集名。"
            )

    # 门禁 1c: 标识符唯一性 + baseline.metric_name ∈ metrics。这些是模块三
    # ExperimentPlan 的 pydantic model_validator，投影层一校验就 raise → 顶层
    # aborted_experiment_replan，且**没有** planning_feedback 回流给 planner，
    # 只能靠这里拦住（详见 _identifier_contract_gaps docstring）。
    gaps.extend(_identifier_contract_gaps(experiment_plan))

    # 门禁 1d: ablation_plan[].removed_by 必须是矩阵里的实现名。模块三
    # implementation_builder 对此 fail-closed 拒收，且该 blocker 会把 REPLAN
    # 路由去重跑 method-designer（详见 _ablation_contract_gaps docstring）。
    gaps.extend(_ablation_contract_gaps(experiment_plan))

    # 门禁 1e: 矩阵行形状 + key=value 参数键唯一性。模块三在**投影层 pydantic
    # 校验**就拒收整份请求，且该路径不产生 planning_feedback（详见
    # _matrix_contract_gaps docstring）。
    gaps.extend(_matrix_contract_gaps(experiment_plan))

    # 门禁 1f: metrics[] 必须是模块三认得的**标准指标名**。自造名无法被确定性验证，
    # 会白烧一整轮 stage3（详见 _standard_metric_gaps docstring）。
    gaps.extend(_standard_metric_gaps(experiment_plan))
    # 门禁 1g: 标准名称也必须和任务语义兼容（例如 retrieval 下的 accuracy 无唯一
    # 定义）。只报告歧义，不把 accuracy 擅自换算成 hit@1。
    gaps.extend(_task_metric_compatibility_gaps(experiment_plan))
    # 门禁 1h: 已匹配专用执行能力时，task/method/metric 必须逐字落在发布合同内。
    gaps.extend(_execution_capability_gaps(experiment_plan, inputs))

    # 门禁 2: baseline.paper_id 真实性（防 LLM 编 paper_id）
    key_paper_ids = {
        p.get("id")
        for field in ("key_papers", "references")
        for p in (inputs.get(field) or [])
        if isinstance(p, dict) and p.get("id")
    }
    for i, b in enumerate(experiment_plan.get("baselines") or []):
        if not isinstance(b, dict):
            continue
        bp = b.get("paper_id")
        if bp not in key_paper_ids:
            gaps.append(
                f"baselines[{i}].paper_id={bp!r} 不在 key_papers/references 的已核验 id 内（防 hallucination）"
            )

    # 门禁 3: metric × success_criteria 覆盖率 ≥ 70%
    # 兼容三种输入形态（e2e 2026-09-04 实测）：
    #   - list[dict]: [{"name": "ROUGE-L"}, ...]  (schema 期望)
    #   - list[str]:  ["ROUGE-L", "BERTScore"]    (LLM 偶返)
    #   - str:        "方法在三类评测上..."          (mock 数据用纯文本 / LLM 自由描述)
    # str 形态代表"自由目标描述"，跳过精确名称覆盖率检查（无法做元素级匹配）。
    sc_raw = (inputs.get("research_question") or {}).get("success_criteria")
    if isinstance(sc_raw, str):
        sc_names: set = set()  # str 形态：跳过覆盖率门禁
    elif isinstance(sc_raw, list):
        sc_names = {
            (s.get("name") if isinstance(s, dict) else s)
            for s in sc_raw
            if (isinstance(s, dict) and s.get("name")) or isinstance(s, str)
        }
    else:
        sc_names = set()
    metric_names = {
        (m.get("name") if isinstance(m, dict) else m)
        for m in (experiment_plan.get("metrics") or [])
        if (isinstance(m, dict) and m.get("name")) or isinstance(m, str)
    }
    if sc_names and metric_names:
        covered = sc_names & metric_names
        coverage = len(covered) / max(len(sc_names), 1)
        if coverage < 0.7:
            gaps.append(
                f"metrics 与 success_criteria 覆盖率 {coverage:.0%} < 70%："
                f"missing={sorted(sc_names - metric_names)[:5]}"
            )

    # 门禁 4: compute_estimate 是 number（防 LLM 返字符串"30 GPUh"）
    ce = experiment_plan.get("compute_estimate")
    if not isinstance(ce, (int, float)):
        gaps.append(
            f"compute_estimate 应为 number，实际 {type(ce).__name__}: {ce!r}"
        )

    # 门禁 5: primary_experiments[].id 非空
    for i, e in enumerate(experiment_plan.get("primary_experiments") or []):
        if isinstance(e, dict) and not e.get("id"):
            gaps.append(f"primary_experiments[{i}].id 为空")

    # 门禁 6: 跨产物交叉引用（2026-09-09 新增，planning-schemas.md §5.1 X1–X12）
    # 只报 error 级；warning（列语义自检不过时的降级项）不进 reflection——
    # 那些结论本身不可信，塞给 LLM 只会让它去改一个可能不存在的问题。
    if isinstance(method_design, dict) and method_design:
        cross_errs = validate_cross_refs(
            method_design,
            experiment_plan,
            key_papers=inputs.get("key_papers"),
            hypotheses=(inputs.get("research_question") or {}).get("hypotheses"),
        )
        for ce_err in cross_errs:
            # experiment-planner 只能修 experiment_plan。方法正文/组件里的问题交给
            # 共同 critic 路由回 method-designer，不能让 planner 无效重写。
            method_owned = ce_err.path.startswith((
                "innovation_points", "hypothesis_coverage", "components",
                "framework", "algorithm_reference", "technical_route",
            ))
            if not ce_err.is_warning and not method_owned:
                gaps.append(f"[跨产物] {ce_err}")

    return gaps


# ════════════════════════════════════════════════════════════════
# 拼 LLM 可读的 prev_feedback
# ════════════════════════════════════════════════════════════════


def format_gaps_as_feedback(gaps: list[str], phase: str) -> str:
    """把 gap 列表拼成 LLM 可读的 prev_feedback 文本。

    Args:
        gaps:  hard_gate_* 返回的 gap 列表。
        phase: 阶段名（method_design / experiment_plan），用于标签前缀。

    Returns:
        拼好的 prev_feedback 文本；gaps 为空时返回空串。
    """
    if not gaps:
        return ""
    lines = "\n".join(f"- {g}" for g in gaps)
    return (
        f"[{phase} 确定性门禁 gap 列表]\n"
        f"以下是 Python 静态检查出的问题（不是 LLM 评的）：\n"
        f"{lines}\n"
        f"请针对每条 gap 修复，不要改其他字段。"
    )
