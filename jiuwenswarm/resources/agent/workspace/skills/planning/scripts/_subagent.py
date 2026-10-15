# -*- coding: utf-8 -*-
"""
_subagent.py — Sub-agent 调起（persona 抽取 + facade.agent 包装 + JSON 解析）

替代旧 scripts/run_subagent.py（直接调 JiuwenSwarmChatClient）。新版本走
``openjiuwen.agent_teams.workflow.engine.facade.agent()`` 操作符，与 SwarmFlow
team 模式天然兼容（不需要 asyncio.run 绕开 event loop 冲突——这是旧实现
的根本问题）。

调用方（3 个 stage 脚本）:
    - ``iterate_method_design_async``       → method-designer / method-critic
    - ``plan_experiment_with_data_async``   → experiment-planner
    - ``downgrade_with_llm_async``          → experiment-planner (downgrade mode)

facade 全 API 文档见 [references/swarmflow-facade.md](../references/swarmflow-facade.md)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.workflow.engine.facade import agent, agent_session

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.subagent")


# LLM 调用心跳：每 30s 打一条 "仍等 X LLM, t=Ns"，避免上层 30+ 分钟看不到动静以为是死锁
# 2026-09-08 新增（paper-gen 接入 writing 阶段后用户反馈"日志太稀"）：
#   之前 _subagent 的 LLM call 完全 silent-in-flight—— start 有日志，end 有日志，
#   但中间 5-30 分钟没任何动静。火山 Agent Plan deepseek-v4-flash 慢的时候完全
#   没法区分"在跑"和"卡死"。本心跳 + 30s 报存活。
_LLM_HEARTBEAT_INTERVAL_S = 30.0


@asynccontextmanager
async def _llm_heartbeat(name: str, attempt: int = 0):
    """LLM 调用心跳 context manager。

    用法::

        async with _llm_heartbeat("method-designer"):
            text = await agent(prompt, ...)

    行为：
        - 进入时 log "调 X LLM start, prompt=... chars"
        - 每 30s log "仍等 X LLM, t=Ns"
        - 退出时 cancel 心跳 task（不 log "end"——caller 自己打 end 信息）
    """
    start_ts = time.monotonic()
    if attempt > 0:
        log.info(f"[_llm_heartbeat] {name} retry #{attempt} 启动 ...")
    else:
        log.info(f"[_llm_heartbeat] {name} LLM 启动 ...")

    async def _heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(_LLM_HEARTBEAT_INTERVAL_S)
                elapsed = time.monotonic() - start_ts
                log.info(
                    f"[_llm_heartbeat] {name} 仍在等 LLM, t={elapsed:.0f}s"
                )
        except asyncio.CancelledError:
            return

    hb_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        elapsed = time.monotonic() - start_ts
        log.info(f"[_llm_heartbeat] {name} LLM 结束, wall={elapsed:.1f}s")
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass

# 当前脚本所在目录的 references/ 路径（agent md 读这里）
REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"

# agent 名 → references/ 下文件名
_AGENT_MD_FILES: dict[str, str] = {
    "method-designer":     "method-designer.md",
    "method-critic":       "method-critic.md",
    "experiment-planner":  "experiment-planner.md",
    "experiment-critic":   "experiment-critic.md",  # 2026-08-29 新增：3 类反馈评审
    "planning-contract-critic": "planning-contract-critic.md",
}


# method-designer 曾在收到三轮逐字段反馈后仍持续返回旧形状。文字提示只能表达
# 语义约束，不能可靠承担机器接口；这里用 SwarmFlow 原生 structured output
# 约束最容易漂移的 MethodDesign 字段，后面的 validate_method_design 继续负责
# 更细的内容检查和跨字段规则。
_METHOD_DESIGN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    # 入口只保证顶层是对象。此前这里要求每个字段并把 technical_route 的
    # item 锁成 object，模型在修一项时漏掉另一项就触发 SDK 三连重试；每次还
    # 会把完整 schema/响应写进阶段日志。细粒度约束由 hard_gate_method_design
    # 在响应落地后统一给出可操作反馈，仍然 fail-closed。
    "properties": {
        "research_goal": {"type": "string", "minLength": 1},
        "core_mechanism": {"type": "string", "minLength": 1},
        "framework": {
            # 这里只是 LLM 传输边界：部分兼容模型会先把 framework 写成完整文字。
            # 允许文字进入现有迭代器，才能由 hard_gate_method_design 给出精确反馈
            # 并要求模型改成正式对象；最终产物仍必须通过严格对象契约，绝不降级。
            "type": ["object", "string"],
            # name 在后置 hard_gate_method_design 中仍是必填。入口不强制它，避免
            # 模型只漏一个名字就丢弃整份详细方案并进行多轮 schema 重试；这样缺口会
            # 作为可操作反馈返回给 planner。title/method_name/framework_name 是模型
            # 明确给出的同义键，可以无损复制；绝不从 overview 等散文猜名称。
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "title": {"type": ["string", "null"]},
                "method_name": {"type": ["string", "null"]},
                "framework_name": {"type": ["string", "null"]},
                "overview": {"type": ["string", "null"]},
                "stages": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": ["string", "object"]},
                },
            },
            "additionalProperties": True,
        },
        "components": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "function", "input_schema", "output_schema", "novelty_degree"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "function": {"type": "string", "minLength": 1},
                    # 模型会自然地把接口 shape 表达为 JSON object；接受该形态，
                    # 后续无损序列化回 planning 产物规定的 string。
                    "input_schema": {"type": ["string", "object"], "minLength": 1},
                    "output_schema": {"type": ["string", "object"], "minLength": 1},
                    "novelty_degree": {"type": "string", "minLength": 1},
                },
                "additionalProperties": True,
            },
        },
        # route 有时是 list[dict]，有时有分组 list；入口不裁掉信息，后置门禁
        # 决定其是否可接受。
        "technical_route": {"type": "array", "minItems": 1},
        "algorithm_reference": {"type": "array", "items": {"type": "object"}},
        "innovation_points": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["claim", "evidence_metric", "experiment_ref"],
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "evidence_metric": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            # 火山模型 round 0 稳定输出旧式 {metric_name,target/description}
                            # ——2026-09-10 16:47 实测确认这条仍然成立（我一度以为
                            # 模型已改吐新 shape，把 5 个字段设成 required，结果 round 0
                            # 直接 3 连败在 "'comparison_mode' is a required property"，
                            # 比不改还糟，已回退）。所以**不能 required**。
                            #
                            # 但可以卡另一头：这些键**一旦出现就不许是空值**。
                            # 16:32 那次的残留正是 {comparator:"", threshold:null,
                            # unit:""}——键在、值空，旧 schema 显式写了 "null" 放行它，
                            # 于是 3 轮修复都没补上还是漏到硬门禁。
                            # 去掉 null、加 minLength 后：
                            #   - 旧式 {metric_name,target}  -> 键不存在，放行（正是 round 0）
                            #   - 半填的 {comparator:""}     -> 生成侧就被拒，框架重试并回喂原因
                            # 不加 enum：comparator="gte"、unit="percentage_points"
                            # 这类别名归一化函数免费消化，写 enum 反而白花一次重试。
                            "required": ["metric_name"],
                            "properties": {
                                "metric_name": {"type": "string", "minLength": 1},
                                "comparison_mode": {"type": "string", "minLength": 1},
                                "comparator": {"type": "string", "minLength": 1},
                                "threshold": {"type": ["number", "string"]},
                                "unit": {"type": "string", "minLength": 1},
                                # vs 只在 comparison_mode=delta 时必填，
                                # 这条条件依赖交给硬门禁判，schema 不表达。
                                "vs": {"type": ["string", "null"]},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "experiment_ref": {"type": "string", "pattern": "^EXP-[A-Z0-9]+-[A-Z0-9_-]+$"},
                },
                "additionalProperties": True,
            },
        },
        "hypothesis_coverage": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                # experiment_ref 可从同一 hypothesis 唯一的 innovation ref 无损复制；
                # 多候选或无候选仍由 hard gate 拒绝，绝不猜测。
                "required": ["hypothesis_id", "mechanism"],
                "properties": {
                    "hypothesis_id": {"type": "string", "minLength": 1},
                    "mechanism": {"type": "string", "minLength": 1},
                    "experiment_ref": {"type": "string", "pattern": "^EXP-[A-Z0-9]+-[A-Z0-9_-]+$"},
                },
                "additionalProperties": True,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
        "implementable": {"type": "boolean"},
    },
    "additionalProperties": True,
}


def _output_schema(name: str) -> dict[str, Any] | None:
    """返回需要由模型入口强制执行的结构化输出 schema。"""
    return _METHOD_DESIGN_OUTPUT_SCHEMA if name == "method-designer" else None


def _normalize_structured_output(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """做只依赖同份产物唯一键的无损规范化。"""
    if name != "method-designer":
        return payload
    framework = payload.get("framework")
    if isinstance(framework, dict):
        if not (isinstance(framework.get("name"), str) and framework["name"].strip()):
            explicit_name = next(
                (
                    framework.get(key).strip()
                    for key in ("title", "method_name", "framework_name")
                    if isinstance(framework.get(key), str) and framework.get(key).strip()
                ),
                None,
            )
            if explicit_name:
                framework["name"] = explicit_name
                log.info("[structured-output] framework.%s 无损映射为 framework.name", "title/method_name/framework_name")
        if not framework.get("overview"):
            overview = next(
            (
                framework.get(key)
                for key in ("architecture", "architecture_diagram", "data_flow", "key_innovation")
                if isinstance(framework.get(key), str) and framework.get(key).strip()
            ),
            None,
        )
            if overview:
                framework["overview"] = overview
            log.info("[structured-output] framework.overview 从丰富架构描述复制")
    # 前缀匹配（"novel design" 也算）；run i 实测三轮反思都写 'incremental'
    # 而契约只认 adapted——同义词族归并，别再指望 LLM 自己改对枚举。
    novelty_aliases = {
        # novel
        "novel": "novel", "innovative": "novel", "original": "novel",
        "brand-new": "novel", "brand new": "novel", "new": "novel", "high": "novel",
        # adapted（增量改进型）
        "adapted": "adapted", "incremental": "adapted", "improved": "adapted",
        "modified": "adapted", "extended": "adapted", "customized": "adapted",
        "fine-tuned": "adapted", "fine tuned": "adapted",
        "medium": "adapted", "moderate": "adapted",
        # standard
        "standard": "standard", "existing": "standard", "conventional": "standard",
        "established": "standard", "baseline": "standard",
        "off-the-shelf": "standard", "off the shelf": "standard",
        "unchanged": "standard", "known": "standard", "low": "standard",
    }
    for component in payload.get("components") or []:
        if not isinstance(component, dict):
            continue
        for field in ("input_schema", "output_schema"):
            value = component.get(field)
            if isinstance(value, (dict, list)):
                component[field] = json.dumps(value, ensure_ascii=False, sort_keys=True)
                log.info(
                    "[structured-output] component %s.%s 从对象无损序列化为 JSON 字符串",
                    component.get("name", "?"), field,
                )
        raw_novelty = component.get("novelty_degree")
        if not isinstance(raw_novelty, str):
            continue
        lowered = raw_novelty.strip().lower()
        normalized_novelty = next(
            (target for prefix, target in novelty_aliases.items() if lowered.startswith(prefix)),
            None,
        )
        if normalized_novelty and raw_novelty != normalized_novelty:
            component["novelty_degree"] = normalized_novelty
            log.info(
                "[structured-output] component %s novelty_degree 规范化: %s -> %s",
                component.get("name", "?"),
                raw_novelty[:80],
                normalized_novelty,
            )
    _normalize_evidence_metrics(payload)
    # slug 中的下划线与连字符只是序列化差异；模块三只接受连字符。
    for field in ("innovation_points", "hypothesis_coverage"):
        for item in payload.get(field) or []:
            if not isinstance(item, dict):
                continue
            ref = item.get("experiment_ref")
            if isinstance(ref, str) and "_" in ref:
                item["experiment_ref"] = ref.replace("_", "-")
                log.info(
                    "[structured-output] %s.experiment_ref 规范化: %s -> %s",
                    field,
                    ref,
                    item["experiment_ref"],
                )
    refs_by_hypothesis: dict[str, set[str]] = {}
    for point in payload.get("innovation_points") or []:
        if not isinstance(point, dict):
            continue
        ref = point.get("experiment_ref")
        if not isinstance(ref, str):
            continue
        parts = ref.split("-", 2)
        if len(parts) == 3 and parts[0] == "EXP" and parts[1]:
            refs_by_hypothesis.setdefault(parts[1], set()).add(ref)
    for coverage in payload.get("hypothesis_coverage") or []:
        if not isinstance(coverage, dict) or coverage.get("experiment_ref"):
            continue
        hypothesis_id = coverage.get("hypothesis_id")
        candidates = refs_by_hypothesis.get(str(hypothesis_id), set())
        if len(candidates) == 1:
            coverage["experiment_ref"] = next(iter(candidates))
            log.info(
                "[structured-output] hypothesis_coverage[%s].experiment_ref "
                "从唯一 innovation ref 补为 %s",
                hypothesis_id,
                coverage["experiment_ref"],
            )
    return payload


# 越高越好型指标的命名关键词——判据散文里完全没方向词时（run i：threshold/unit
# 都填对了、comparator 空，三轮反思都不补），按指标名给 >= 默认。
# 只收无歧义词；error/loss/degradation/ppl 这类低为优绝不进。
_HIGHER_BETTER_WORDS = (
    "accuracy", "precision", "recall", "f1", "auc", "retention",
    "preservation", "coverage", "completeness", "correctness",
)


def _comparator_from_prose(text: str) -> str | None:
    """从 description 散文恢复比较方向（只返比较符，不碰 threshold/unit）。

    run i 实测模型把方向只写在描述里："must be >= 5.0"、"retain at least 90%"、
    "not more than 1% relative degradation"、"within -1%"。
    认不出返 None——调用方再决定是否按指标名兜底。
    """
    m = _TARGET_RE.search(text)
    if m:
        comp = _canonical_comparator(m.group(1))
        if comp is not None:
            return comp
    # "within -1%"（容忍下界）/ "within +0.5"（带上界? 极少见，按字面 >=）
    within = re.search(r"within\s+([+-])\s*\d", text, re.I)
    if within:
        return "<=" if within.group(1) == "-" else ">="
    return None


def _comparator_from_direction(value: Any) -> str | None:
    """把模型显式 ``direction`` 枚举投影到比较符。

    ``higher_is_better`` / ``lower_is_better`` 已经表达了方向，只是模型把它
    放在旧字段而没有复制到 ``comparator``。这里不碰阈值或指标含义。
    未识别的自由文本返回 ``None``，仍由门禁拒绝，避免从模糊措辞猜测。
    """
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"higher_is_better", "maximize", "maximise", "increasing"}:
        return ">="
    if token in {"lower_is_better", "minimize", "minimise", "decreasing"}:
        return "<="
    return None


def _recover_comparator_from_claim(entry: dict[str, Any], claim: Any) -> bool:
    """在同一创新点 claim 中恢复与现有阈值同值的比较关系。

    仅接受数值和单位都与 entry 已有值匹配的明示比较，或 ``within -1%``
    这类带符号容差。找不到一一对应关系时不改 entry，交给后续门禁处理。
    """
    if not isinstance(claim, str) or not claim.strip():
        return False
    threshold = entry.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return False
    unit = _canonical_unit(str(entry.get("unit") or ""))
    for match in _TARGET_RE.finditer(claim):
        marker = (match.group(3) or "").lower()
        candidate_unit = (
            "percent" if marker == "%" else
            "times" if marker in {"x", "×", "fold", "times"} else
            "points" if marker.startswith("point") or marker == "pp" else
            "ratio"
        )
        if candidate_unit != unit or abs(float(match.group(2)) - float(threshold)) > 1e-9:
            continue
        comparator = _canonical_comparator(match.group(1))
        if comparator is None:
            continue
        entry["comparator"] = comparator
        return True
    # 自然语言常插入量纲修饰词（"at least 0.3 judge accuracy points"），
    # 因此严格的 ``number + unit`` 形式匹配不到。只在整条 claim 中该数值
    # 唯一出现时接受裸的比较符+数值，避免把同一个 0.5 错配给另一条指标。
    loose = re.compile(
        r"(>=|<=|>|<|≥|≤|at\s+least|at\s+most|no\s+less\s+than|no\s+more\s+than)"
        r"\s*(-?\d+(?:\.\d+)?)",
        re.I,
    )
    candidates = [
        match for match in loose.finditer(claim)
        if abs(float(match.group(2)) - float(threshold)) <= 1e-9
    ]
    if len(candidates) == 1:
        comparator = _canonical_comparator(candidates[0].group(1))
        if comparator is not None:
            entry["comparator"] = comparator
            return True
    # “within -1% relative degradation” is a delta lower-bound after
    # normalization: (candidate - baseline) >= -1%.
    within = re.search(r"within\s+(-)\s*(\d+(?:\.\d+)?)\s*%", claim, re.I)
    if within and unit == "percent" and abs(float(within.group(2)) - float(threshold)) <= 1e-9:
        entry["comparison_mode"] = "delta"
        entry["comparator"] = ">="
        entry["threshold"] = -abs(float(threshold))
        return True
    return False


def coerce_feedback_text(value: Any) -> str:
    """critic.feedback 漂移归一：契约要 str，deepseek-v4-flash 偶返 list/dict。

    - list（run i 实测，如 ["...", "..."]）：逐项成行拼接（dict 元素 JSON 化）；
    - dict/其他：整体 JSON 化（与原尾部兜底一致）；
    - None → ""；str 原样返回。
    不修的话 list 形态在 _format_critic_structured_feedback 里被 ``isinstance(str)``
    静默丢弃——architect 下一轮根本看不到 critic 总体反馈。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif item is not None:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(p.strip() for p in parts if p.strip())
    return json.dumps(value, ensure_ascii=False)


# 操作符既认符号也认英文词——run h 里 target 散文出现 "at least 5x" 这类写法，
# 旧正则只认 >=/≤，拆不出 comparator/threshold，判据漏成空壳到门禁才被点名。
# 注意长词要排在短词前面（alternation 按从左到右尝试）。
_TARGET_RE = re.compile(
    r"(>=|<=|>|<|≥|≤"
    r"|greater\s+than\s+or\s+equal\s+to|less\s+than\s+or\s+equal\s+to"
    r"|no\s+less\s+than|no\s+more\s+than"
    r"|not\s+less\s+than|not\s+more\s+than"
    r"|at\s+least|at\s+most"
    r"|greater\s+than|more\s+than|exceeds?|less\s+than|below|under)"
    r"\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*(%|x|×|points?|pp|fold|times)?",
    re.I,
)
_VS_RE = re.compile(
    r"(?:compared\s+to|versus|\bvs\.?|\bover)\s+(.+?)(?:\s+at\s+the\s+same|[,;.]|$)",
    re.I,
)
_DERIVED_SUFFIXES = (
    "_relative_degradation", "_improvement", "_stability", "_gain",
    "_drop", "_change", "_diff", "_delta",
)

# 比较符别名（模型常把 "gte" 当 comparison_mode 吐，而不是填 comparator）
_COMPARATOR_ALIAS: dict[str, str] = {
    ">=": ">=", "≥": ">=", "gte": ">=", "ge": ">=",
    "<=": "<=", "≤": "<=", "lte": "<=", "le": "<=",
    ">": ">", "gt": ">",
    "<": "<", "lt": "<",
}

# 比较符的"正向/反向"词族——整词形态靠关键词归类，不再穷举别名：
# 2026-09-10 18:08 run f 模型直接吐 comparison_mode='greater_than_or_equal' /
# 'less_than_or_equal'，缩写别名表挡不住复合词（跟 unit 一样的打地鼠问题）。
_COMPARATOR_POSITIVE_WORDS = ("greater", "more", "higher", "above", "larger")
_COMPARATOR_NEGATIVE_WORDS = ("less", "lower", "fewer", "below", "smaller")


def _canonical_comparator(raw: str) -> str | None:
    """认比较符：符号、缩写（gte）、整词（greater_than_or_equal）都归到 >= > <= <。

    认不出来返回 None——调用方据此区分"这个槽位填的是比较符"和"填的是 mode 词"。
    """
    token = (raw or "").strip().lower()
    if not token:
        return None
    if token in _COMPARATOR_ALIAS:
        return _COMPARATOR_ALIAS[token]
    squashed = re.sub(r"[\s_\-]+", "", token)
    if squashed in {"gte", "ge"}:
        return ">="
    if squashed in {"lte", "le"}:
        return "<="
    if squashed == "gt":
        return ">"
    if squashed == "lt":
        return "<"
    # 边界整词（run h target 散文）：必须排在关键词子串扫描之前——
    # "no more than" 含 "more"，按正向词会被错判成 >；at least/at most 不含
    # greater/less 词族，不归化就会让调用方退回原文 "at least"。
    if squashed in {"atleast", "nolessthan", "notlessthan"}:
        return ">="
    if squashed in {"atmost", "nomorethan", "notmorethan"}:
        return "<="
    positive = any(word in squashed for word in _COMPARATOR_POSITIVE_WORDS)
    negative = any(word in squashed for word in _COMPARATOR_NEGATIVE_WORDS)
    if not positive and not negative:
        return None
    inclusive = "equal" in squashed
    if positive:
        return ">=" if inclusive else ">"
    return "<=" if inclusive else "<"

# unit 规范化用**规则**而不是穷举别名表：
# 之前枚举 accuracy_points/judge_accuracy_points 却漏了 percentage_points，
# 是打地鼠——模型能造的复合词无穷多，按后缀归类才收得住。
# 顺序重要：`percentage_points`（= 百分点差值）必须先命中 points，
# 否则会被 percentage 规则错判成 percent。
_UNIT_RULES: list[tuple[re.Pattern[str], str]] = [
    # 模型常把百分比单位直接写成符号；这和 ``percent`` 是同一个量纲，
    # 只做枚举序列化，不换算 threshold。
    (re.compile(r"^%$"), "percent"),
    (re.compile(r"(?:^|_)(?:pp|percentage_points?)$", re.I), "points"),
    (re.compile(r"points?$", re.I), "points"),
    # 下面是子串匹配：percent_relative / relative_percentage / pct_delta 都收。
    # 必须排在 points 规则之后——percentage_points 要先走 points，
    # 否则被 percent 规则错判（百分点差值 ≠ 百分比）。
    (re.compile(r"percent|pct", re.I), "percent"),
    # 2026-09-10 真实规划草案的 ``absolute_accuracy`` 表示 [0,1] 准确率
    # 标尺上的绝对差值（例如 0.05），不是百分点；只做单位名规范化，不换数值。
    (re.compile(r"^absolute_accuracy(?:_difference)?$", re.I), "ratio"),
    (re.compile(r"^(?:proportion|rate|fraction|ratio)$", re.I), "ratio"),
    (re.compile(r"fold|times|^x$|^×$", re.I), "times"),
    (re.compile(r"^(?:count|num|number)$", re.I), "count"),
]


def _canonical_unit(raw: str) -> str:
    """把模型自造的 unit 词归到 5 个合法值；认不出就原样返回让门禁报。"""
    value = raw.strip().lower()
    for pattern, canonical in _UNIT_RULES:
        if pattern.search(value):
            return canonical
    return value


def _entry_is_complete(entry: dict[str, Any]) -> bool:
    """5 个必填项是否**值**都合法——注意不能用 ``required.issubset(entry)``。

    dict 的 issubset 只查 key 存在。模型一旦把 5 个键都吐出来（哪怕
    ``comparator: ""`` / ``comparison_mode: "gte"``），key 检查就放行，
    整条 entry 原样穿过归一化直达硬门禁——2026-09-10 12:30 实测残留的 6 条
    错误全是这么漏的。所以这里必须逐个校验值。
    """
    if not isinstance(entry.get("metric_name"), str) or not entry["metric_name"].strip():
        return False
    if entry.get("comparison_mode") not in {"absolute", "delta"}:
        return False
    if entry.get("comparator") not in {">=", ">", "<=", "<"}:
        return False
    threshold = entry.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return False
    if entry.get("unit") not in {"ratio", "percent", "points", "times", "count"}:
        return False
    if entry.get("comparison_mode") == "delta" and not (
        isinstance(entry.get("vs"), str) and entry["vs"].strip()
    ):
        return False
    return True


def _recover_delta_vs_from_component(
    entry: dict[str, Any], claim: Any, components: Any,
) -> str | None:
    """只在文本显式点名唯一组件且写出对比关系时，恢复 delta 的消融对照。"""
    if entry.get("comparison_mode") != "delta" or str(entry.get("vs") or "").strip():
        return None
    text = " ".join(
        str(value) for value in (
            entry.get("description"), entry.get("metric_description"), claim,
        ) if isinstance(value, str)
    )
    lowered = text.casefold()
    # ``vs disabled`` 的空白本身是右边界；若把 ``\b`` 放在可选 ``.``
    # 之后，正则会优先吞掉 ``vs`` 后错误地检查到 ``.``/空白边界，漏掉它。
    if not re.search(r"(?:\bvs\.?\s|\bversus\b|\bcompared\s+to\b)", lowered):
        return None
    matches = {
        component.get("name").strip()
        for component in (components or [])
        if isinstance(component, dict)
        and isinstance(component.get("name"), str)
        and component["name"].strip()
        and component["name"].strip().casefold() in lowered
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _recover_unit_from_claim(entry: dict[str, Any], claim: Any) -> str | None:
    """从同一 claim 的同值、显式量纲恢复 unit；多义或缺失时不猜。"""
    if str(entry.get("unit") or "").strip() or not isinstance(claim, str):
        return None
    threshold = entry.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None
    candidates: set[str] = set()
    for match in re.finditer(
        r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*"
        r"(?P<unit>%|x\b|×|(?:[A-Za-z-]+\s+){0,3}(?:percentage\s+)?points?\b|pp\b|folds?\b|times\b)",
        claim,
        re.I,
    ):
        if abs(float(match.group("number")) - float(threshold)) > 1e-9:
            continue
        unit = _canonical_unit(match.group("unit"))
        if unit in {"ratio", "percent", "points", "times", "count"}:
            candidates.add(unit)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _normalize_evidence_metrics(payload: dict[str, Any]) -> None:
    """把模型旧式 ``target`` 拆成 EvidenceMetric；只解析显式信息。"""
    notes: list[str] = []
    for point_index, point in enumerate(payload.get("innovation_points") or []):
        if not isinstance(point, dict):
            continue
        raw_entries = point.get("evidence_metric") or []
        if not isinstance(raw_entries, list):
            continue
        normalized_entries: list[dict[str, Any]] = []
        unresolved_entries: list[dict[str, Any]] = []
        dropped: list[str] = []
        for metric_index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                normalized_entries.append(entry)
                continue
            required = {"metric_name", "comparison_mode", "comparator", "threshold", "unit"}
            metric_name = str(entry.get("metric_name") or "").strip()
            suffix = next((s for s in _DERIVED_SUFFIXES if metric_name.endswith(s)), None)
            if suffix:
                entry["metric_name"] = metric_name[: -len(suffix)]
            for threshold_alias in ("value", "target_value", "target_threshold", "expected_value"):
                if entry.get("threshold") is None and entry.get(threshold_alias) is not None:
                    entry["threshold"] = entry[threshold_alias]
                    break
            if not entry.get("comparison_mode"):
                entry["comparison_mode"] = (
                    "delta" if suffix or entry.get("vs") else "absolute"
                )
            mode_alias = {
                "relative": "delta", "difference": "delta", "change": "delta",
                "direct": "absolute", "raw": "absolute",
            }
            # ``or ""`` 防显式 null：str(None) 会得到字面量 "None"，
            # 后续任何别名表都认不出来，直接漏到门禁。
            mode = str(entry.get("comparison_mode") or "").strip().lower()
            comparator = str(entry.get("comparator") or "").strip()

            # 模型高频错位：把比较符塞进 comparison_mode 槽位、comparator 留空
            # （实测 'gte'，2026-09-10 18:08 run f 又出现整词
            # 'greater_than_or_equal'/'less_than_or_equal'）。把它搬回 comparator，
            # mode 则按 vs / 派生后缀 / unit 里的 relative 提示重新判定。
            mode_comparator = _canonical_comparator(mode)
            if mode_comparator is not None:
                if not comparator:
                    comparator = mode_comparator
                unit_raw_l = str(entry.get("unit") or "").lower()
                relative_hint = "relative" in unit_raw_l
                mode = "delta" if (suffix or entry.get("vs") or relative_hint) else "absolute"

            entry["comparison_mode"] = mode_alias.get(mode, mode)
            entry["unit"] = _canonical_unit(str(entry.get("unit", "")))
            comparator_canonical = _canonical_comparator(comparator)
            entry["comparator"] = (
                comparator_canonical if comparator_canonical is not None else comparator
            )
            if isinstance(entry.get("threshold"), str):
                raw_th = entry["threshold"].strip()
                # 带显式符号的裸数字（"+0.3"、"-1.0"）是 delta 语义：
                # 正数期望提升（>=），负数容忍退化（<=）。必须在 float() 吃掉
                # 符号之前截住，否则阈值丢了方向、comparator 还是空。
                bare_signed = re.fullmatch(
                    r"([+-])\s*(\d+(?:\.\d+)?)\s*%?", raw_th
                )
                if bare_signed and not comparator:
                    sign = bare_signed.group(1)
                    entry["threshold"] = abs(float(bare_signed.group(2)))
                    entry["comparator"] = ">=" if sign == "+" else "<="
                    # 裸带符号数（"+0.3"）本身就是相对变化量，必为 delta。
                    # 上面 line ~387 的推断可能已按"无后缀无 vs"填了 absolute，
                    # 这里要**覆盖**而不是 setdefault——那个推断在本形态下不成立。
                    entry["comparison_mode"] = "delta"
                    comparator = entry["comparator"]
                    if not str(entry.get("unit") or "").strip():
                        entry["unit"] = "percent" if raw_th.endswith("%") else "ratio"
                    entry["unit"] = _canonical_unit(entry["unit"])
                    # delta 必须有 vs；模型常把参照系写在 description 里
                    # （实测 "...with vs. without temporal preservation"）。
                    if not str(entry.get("vs") or "").strip():
                        desc = str(
                            entry.get("description")
                            or entry.get("metric_description") or ""
                        )
                        vm = _VS_RE.search(desc)
                        if vm:
                            entry["vs"] = vm.group(1).strip()
                else:
                    try:
                        entry["threshold"] = float(raw_th.rstrip("%"))
                    except ValueError:
                        pass
            recovered_unit = _recover_unit_from_claim(entry, point.get("claim"))
            if recovered_unit:
                entry["unit"] = recovered_unit
                notes.append(
                    f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                    f"从同值 claim 恢复 unit 为 {recovered_unit}"
                )
            # 模型有时已给出结构化数值，却把比较符留空且不保留 description。
            # compression ratio / accuracy 是本 schema 中单调“越大越好”的明确指标；
            # 负阈值的 relative degradation 也采用有符号差值 >= -x 表达。
            if not str(entry.get("comparator") or "").strip():
                name_lower = str(entry.get("metric_name") or "").lower()
                if "compression_ratio" in name_lower or "accuracy" in name_lower:
                    entry["comparator"] = ">="
                    notes.append(
                        f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                        "由单调 metric_name 恢复 comparator 为 >="
                    )
            if (
                suffix in {"_drop", "_relative_degradation"}
                and entry.get("comparator") in {"<=", "<"}
                and isinstance(entry.get("threshold"), (int, float))
            ):
                entry["comparator"] = ">=" if entry["comparator"] == "<=" else ">"
                entry["threshold"] = -abs(float(entry["threshold"]))
            if _entry_is_complete(entry):
                normalized_entries.append(entry)
                continue

            # 模型在新 schema 下的高频变体：把比较符塞进 threshold 字符串
            # （threshold:">=5.0"、"<=1.0"），comparator/unit 留空。
            # 16:53 实测每个判据都长这样。用 _TARGET_RE 拆掉操作符，然后重新校验。
            # 也兼容模型用 target_value 这个自造键名（round 0 实测）。
            # 注意：纯带符号数字 "+0.3"/"-1.0" 已在上面的 float 转换块里提前截住
            # （float() 会吃掉符号，必须赶在它之前保留方向），这里只管带比较符的。
            raw_threshold = entry.get("threshold")
            if not isinstance(raw_threshold, str):
                tv = entry.get("target_value")
                if isinstance(tv, str):
                    raw_threshold = tv
                    entry.setdefault("threshold", tv)
            if isinstance(raw_threshold, str):
                tm = _TARGET_RE.search(raw_threshold)
                if tm:
                    entry["threshold"] = float(tm.group(2))
                    if not str(entry.get("comparator") or "").strip():
                        entry["comparator"] = (
                            _canonical_comparator(tm.group(1)) or tm.group(1)
                        )
                    tmarker = (tm.group(3) or "").lower()
                    if not str(entry.get("unit") or "").strip():
                        if tmarker == "%":
                            entry["unit"] = "percent"
                        elif tmarker in {"x", "×", "fold", "times"}:
                            entry["unit"] = "times"
                        elif tmarker.startswith("point") or tmarker == "pp":
                            entry["unit"] = "points"
                        else:
                            entry["unit"] = "ratio"
                    # comparison_mode 缺失时按有无 vs/派生后缀推断
                    if not str(entry.get("comparison_mode") or "").strip():
                        mname = str(entry.get("metric_name") or "").lower()
                        has_suffix = any(mname.endswith(s) for s in _DERIVED_SUFFIXES)
                        entry["comparison_mode"] = (
                            "delta" if (has_suffix or entry.get("vs")) else "absolute"
                        )
            if _entry_is_complete(entry):
                normalized_entries.append(entry)
                continue

            # run i 实测：threshold/unit/mode 都填对了，唯独 comparator 空，
            # 方向只写在 description 散文里（"must be >= 5.0"、"retain at least
            # 90%"、"not more than 1% degradation"），三轮反思都不补。
            # 只补 comparator，绝不动数值槽位（0.9/ratio 与 "90%" 的换算模型已做）。
            if not str(entry.get("comparator") or "").strip():
                desc_text = str(
                    entry.get("description") or entry.get("metric_description") or ""
                )
                # ``direction`` 是模型明确给出的枚举，优先级高于散文；它仅
                # 序列化了方向，不会补造阈值。没有该枚举才从含比较词的描述取。
                recovered = _comparator_from_direction(entry.get("direction"))
                recovered_source = "direction" if recovered is not None else ""
                if recovered is None and desc_text:
                    recovered = _comparator_from_prose(desc_text)
                    recovered_source = "description" if recovered is not None else ""
                if recovered is None and entry.get("comparison_mode") == "absolute":
                    mname_l = str(entry.get("metric_name") or "").lower()
                    if any(w in mname_l for w in _HIGHER_BETTER_WORDS):
                        recovered = ">="
                        recovered_source = "metric_name"
                if recovered is not None:
                    dlow = desc_text.lower()
                    if (
                        recovered in {"<=", "<"}
                        and any(w in dlow for w in ("degradation", "decline", "drop"))
                    ):
                        # “degradation ≤ 1%”等价于“有符号差值 ≥ -1%”
                        recovered = ">=" if recovered == "<=" else ">"
                        thr = entry.get("threshold")
                        if isinstance(thr, (int, float)) and not isinstance(thr, bool):
                            entry["threshold"] = -abs(float(thr))
                    entry["comparator"] = recovered
                    notes.append(
                        f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                        f"comparator 由 {recovered_source} 恢复为 {recovered}"
                    )
            if _entry_is_complete(entry):
                normalized_entries.append(entry)
                continue

            if not str(entry.get("comparator") or "").strip() and _recover_comparator_from_claim(
                entry, point.get("claim")
            ):
                notes.append(
                    f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                    "comparator 由同一创新点 claim 的同值阈值恢复"
                )
            recovered_vs = _recover_delta_vs_from_component(
                entry, point.get("claim"), payload.get("components"),
            )
            if recovered_vs:
                entry["vs"] = recovered_vs
                notes.append(
                    f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                    f"从显式唯一组件名恢复 delta.vs 为 {recovered_vs}"
                )
            if _entry_is_complete(entry):
                normalized_entries.append(entry)
                continue

            # 走到这说明值还不合规。有 target/target_value 就去拆；没有时要分两种：
            target = entry.get("target")
            if not isinstance(target, str):
                target = entry.get("target_value")
            if not isinstance(target, str):
                # 模型经常把完整、可机械解析的 ``must be >= 0.3 points`` 写在
                # description，却漏填 target/threshold。只在 description 真含阈值
                # 语法时把它当 target；纯说明文字仍保留给门禁，绝不编造数值。
                description_target = str(
                    entry.get("description") or entry.get("metric_description") or ""
                )
                if _TARGET_RE.search(description_target) or re.search(
                    r"[+-]\s*\d+(?:\.\d+)?\s*(?:%|x|×|points?|pp|fold|times)?",
                    description_target,
                    re.I,
                ):
                    target = description_target
            if not isinstance(target, str):
                if required.issubset(entry):
                    # 结构字段齐全但仍无法证伪的条目先暂存。若同一创新点已有
                    # 完整主判据，稍后移到 auxiliary_evidence_metrics 并记日志；
                    # 若没有任何完整主判据，仍原样送门禁报错，绝不静默删除。
                    unresolved_entries.append(entry)
                    continue
                # 只有 metric_name/description 之类，压根没有阈值信息：
                # 这是说明性文字而非判据，按原有语义计入 dropped。
                dropped.append(str(entry.get("metric_name") or f"#{metric_index}"))
                continue
            match = _TARGET_RE.search(target)
            target_lower = target.lower()
            metric_name = str(entry.get("metric_name") or "").strip()
            suffix = next((s for s in _DERIVED_SUFFIXES if metric_name.endswith(s)), None)
            if not match:
                # _TARGET_RE 只认 >= <= > <；模型还会写裸带符号数
                # （实测 target:"+0.3 improvement"）。+ 期望提升(>=)，- 容忍退化(<=)。
                signed = re.search(
                    r"([+-])\s*(\d+(?:\.\d+)?)\s*(%|x|×|points?|pp|fold|times)?",
                    target, re.I,
                )
                if signed:
                    sign = signed.group(1)
                    comparator = ">=" if sign == "+" else "<="
                    threshold = float(signed.group(2))
                    marker = (signed.group(3) or "").lower()
                    # 裸带符号数本身就是相对变化量
                    signed_is_delta = True
                else:
                    unresolved_entries.append(entry)
                    continue
            else:
                comparator = _canonical_comparator(match.group(1)) or match.group(1)
                threshold = float(match.group(2))
                marker = (match.group(3) or "").lower()
                signed_is_delta = False
            is_delta = bool(
                signed_is_delta
                or suffix
                or any(word in target_lower for word in (
                    "improvement", "degradation", "relative", "compared", " over ", " vs ",
                ))
            )
            if suffix:
                metric_name = metric_name[: -len(suffix)]
            if marker == "%":
                unit = "percent"
            elif marker in {"x", "×", "fold", "times"}:
                unit = "times"
            elif marker.startswith("point") or marker == "pp" or "point" in target_lower:
                unit = "points"
            else:
                unit = "ratio"
            # “degradation <= 1%”等价于“基准差值 >= -1%”。
            if "degradation" in target_lower and comparator in {"<=", "<"}:
                comparator = ">=" if comparator == "<=" else ">"
                threshold = -abs(threshold)
            parsed: dict[str, Any] = {
                **entry,
                "metric_name": metric_name,
                "comparison_mode": "delta" if is_delta else "absolute",
                "comparator": comparator,
                "threshold": threshold,
                "unit": unit,
            }
            if is_delta and not parsed.get("vs"):
                # 参照系优先在 target 里找；模型也常写在 description 里。
                # 两处都没有就留空——硬门禁会精确报"delta 必填 vs"，
                # 这比 comparator/unit/threshold 三个字段的错误簇更可执行，
                # 修复轮更可能补上（绝不跨到 claim 里猜，vs 虽是自由文本也不猜）。
                vs_source = target
                desc = str(entry.get("description") or entry.get("metric_description") or "")
                if desc and not _VS_RE.search(vs_source):
                    vs_source = vs_source + " " + desc
                vs_match = _VS_RE.search(vs_source)
                if vs_match:
                    parsed["vs"] = vs_match.group(1).strip()
            normalized_entries.append(parsed)
            notes.append(
                f"innovation_points[{point_index}].evidence_metric[{metric_index}] "
                f"由 target 拆成结构化判据"
            )
        if unresolved_entries:
            if normalized_entries:
                # 这些字段不丢：rich stage-2 artifact 和 Writer 仍可审阅它们；
                # 只是它们不能冒充已经具有可执行阈值的 evidence_metric 主判据。
                point.setdefault("auxiliary_evidence_metrics", []).extend(unresolved_entries)
                names = ", ".join(
                    str(item.get("metric_name") or "?") for item in unresolved_entries
                )
                notes.append(
                    f"innovation_points[{point_index}] 将 {len(unresolved_entries)} 条"
                    f"无法从同值证据恢复方向的附带指标移至 auxiliary_evidence_metrics: {names}"
                )
            else:
                # 所有条目都不能形成可证伪主判据时必须 fail-closed；不能为跑通
                # 流程把整个 innovation point 掏空。
                normalized_entries.extend(unresolved_entries)
        if dropped and normalized_entries:
            notes.append(
                f"innovation_points[{point_index}] 移除无阈值说明性指标: {', '.join(dropped)}"
            )
        elif dropped:
            # 唯一判据不能静默删除，保留原条目让硬门禁报出具体缺字段。
            normalized_entries = [e for e in raw_entries if isinstance(e, dict)]
        point["evidence_metric"] = normalized_entries
    if notes:
        payload.setdefault("_normalization_notes", []).extend(notes)
        for note in notes:
            log.info("[structured-output] %s", note)
    # hypothesis_coverage accepts the same optional EvidenceMetric shape, but
    # older normalization only repaired innovation_points.  Real LLM runs then
    # exhausted every repair round on a missing delta.vs in coverage while the
    # identical innovation evidence had already been normalized.  Reuse the
    # same value-local parser through a shadow payload rather than maintaining a
    # second implementation.
    coverage = payload.get("hypothesis_coverage")
    if isinstance(coverage, list) and coverage:
        shadow: dict[str, Any] = {
            "innovation_points": coverage,
            "components": payload.get("components") or [],
        }
        _normalize_evidence_metrics(shadow)
        payload["hypothesis_coverage"] = shadow["innovation_points"]
        for index, item in enumerate(payload["hypothesis_coverage"]):
            if not isinstance(item, dict):
                continue
            for metric_index, metric in enumerate(item.get("evidence_metric") or []):
                if not isinstance(metric, dict):
                    continue
                if (
                    str(metric.get("metric_name") or "").strip().casefold() == "qa_token_f1"
                    and metric.get("comparison_mode") == "delta"
                    and not str(metric.get("vs") or "").strip()
                ):
                    # qa_token_f1 retention is defined by the registered
                    # memory-compression executor only against the frozen
                    # uncompressed baseline.  This is contract completion, not
                    # a scientific guess.
                    metric["vs"] = "uncompressed_context"
                    note = (
                        f"hypothesis_coverage[{index}].evidence_metric[{metric_index}] "
                        "按 qa_token_f1 执行合同补 delta.vs=uncompressed_context"
                    )
                    payload.setdefault("_normalization_notes", []).append(note)
                    log.info("[structured-output] %s", note)
        for note in shadow.get("_normalization_notes") or []:
            renamed = str(note).replace("innovation_points[", "hypothesis_coverage[")
            payload.setdefault("_normalization_notes", []).append(renamed)


def agent_md_path(name: str) -> Path:
    """
    拿 agent md 绝对路径。

    Args:
        name: agent 名 ∈ {'method-designer', 'method-critic', 'experiment-planner', 'experiment-critic'}。

    Returns:
        agent md 文件的绝对路径。
    """
    if name not in _AGENT_MD_FILES:
        raise ValueError(
            f"未知 agent 名: {name!r}，合法值: {sorted(_AGENT_MD_FILES)}"
        )
    return REFERENCES_DIR / _AGENT_MD_FILES[name]


# ════════════════════════════════════════════════════════════════
# Persona 提取
# ════════════════════════════════════════════════════════════════

# 匹配 "## Inline Persona for Teammate" 段（直到下一个 "## " 或文件末）
_PERSONA_SECTION_RE = re.compile(
    r"##\s+Inline Persona for Teammate\s*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 匹配 markdown ```...``` 代码块
_CODE_BLOCK_RE = re.compile(r"```(?:\w*\n)?(.*?)```", re.DOTALL)


def extract_persona(agent_md_path: Path) -> str:
    """
    从 agent md 抽 `## Inline Persona for Teammate` 段（去除外层 markdown ``` 包装）。

    agent md 格式约定::

        ## Inline Persona for Teammate

        ```
        ROLE: ...
        ...
        ```

    Args:
        agent_md_path: agent 定义 md 的路径。

    Returns:
        persona 文本（不含外层 markdown ``` 包装）。

    Raises:
        RuntimeError: agent md 找不到 '## Inline Persona for Teammate' 段。
    """
    text = Path(agent_md_path).read_text(encoding="utf-8")
    match = _PERSONA_SECTION_RE.search(text)
    if not match:
        raise RuntimeError(
            f"agent md {agent_md_path} 找不到 '## Inline Persona for Teammate' 段"
        )
    section = match.group(1).strip()

    # 抽代码块（```...```）—— persona 写在 markdown 代码块里
    code_match = _CODE_BLOCK_RE.search(section)
    if not code_match:
        # 没有代码块就用整段作为 persona（兜底）
        return section
    return code_match.group(1).strip()


# ════════════════════════════════════════════════════════════════
# Prompt 拼装
# ════════════════════════════════════════════════════════════════


def build_agent_prompt(persona: str, payload: dict) -> str:
    """
    拼 ``persona`` + payload JSON 成单条 prompt。

    facade.agent() 只接受单条 prompt（不拆 system/user），所以把 persona
    当作 instruction 前缀拼到 payload 前面。
    """
    return (
        f"{persona}\n\n"
        f"INPUT (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


# ════════════════════════════════════════════════════════════════
# 拉起 sub-agent（核心入口）
# ════════════════════════════════════════════════════════════════


async def call_agent_async(
    name: str,
    payload: dict,
    *,
    phase: str | None = None,
) -> dict:
    """
    拉起一个 sub-agent（async）。

    步骤:
        1. 读 agent md，抽 `## Inline Persona for Teammate` 段
        2. 拼 prompt = persona + payload JSON
        3. 调 facade.agent()（框架自动 json_repair 兜坏 JSON + 走 LLMConfig）
        4. ``json.loads`` 解析回 dict

    Args:
        name:   agent 名（'method-designer' / 'method-critic' / 'experiment-planner' / 'experiment-critic'）。
        payload:派发给 sub-agent 的数据 dict（context + 任务 + 上轮 feedback）。
        phase:  关联到 workflow 的 phase 名（可选；用于 trace 可观测性），
                通常传 ``planning_flow.py`` 里 ``phase("方法设计")`` 等同名标题。

    Returns:
        sub-agent 输出的 dict。
        - Architect: MethodDesign dict
        - Critic:    MethodReview dict
        - Planner:   ``{"experiment_plan": {...}, "data_plan": {...}}`` 包装 dict

    Raises:
        ValueError:  未知 agent 名。
        RuntimeError: agent md 缺 persona / LLM 响应空 / 响应非 JSON。

    Example:
        >>> method_draft = await call_agent_async(
        ...     "method-designer",
        ...     {"research_question": ..., "hypotheses": ...},
        ...     phase="方法设计",
        ... )
    """
    md_path = agent_md_path(name)
    persona = extract_persona(md_path)
    base_prompt = build_agent_prompt(persona, payload)
    output_schema = _output_schema(name)

    # 解析失败时 retry：把错误信息拼回 prompt 让 LLM 看到自己哪里错了。
    # deepseek-v4-flash 等模型对长 persona 偶尔会误解 schema（method-critic
    # 返过 ``[["blocker"], [0], ...]`` 这样的 list-of-pair 错例），retry
    # 通常第 2 次就纠正。max_retries=2 即最多 3 次总尝试。
    last_text = ""
    last_err = "未尝试"
    for attempt in range(1, 4):
        if attempt == 1:
            prompt = base_prompt
        else:
            prompt = (
                base_prompt
                + "\n\n[RETRY "
                + str(attempt - 1)
                + "] 你上一次的输出无法解析: "
                + last_err
                + "\n"
                + "你的上一次原始输出（前 500 字）:\n"
                + last_text[:500]
                + "\n\n请严格遵守上方 OUTPUT FORMAT —— "
                + "顶层必须是**单个 JSON object**（以 { 开头），"
                + "不要 markdown 标题、列表、代码块、解释文字。"
            )
        async with _llm_heartbeat(name, attempt - 1 if attempt > 1 else 0):
            response = await agent(
                prompt,
                label=name,
                phase=phase,
                schema=output_schema,
            )
        if output_schema is not None:
            if isinstance(response, dict):
                return _normalize_structured_output(name, response)
            # 结构化模式下模型仍可能回落到 markdown/纯文本。这里必须走下一轮带纠错
            # 提示的重试，而不是立刻放弃——否则上面的 for 循环形同虚设，一次
            # 偶发的格式漂移就会把整个 planning 阶段判死。
            last_text = response if isinstance(response, str) else ""
            last_err = f"schema 模式下返回 {type(response).__name__}（期望 JSON object）"
            continue
        text = response if isinstance(response, str) else ""
        if not text:
            last_text = ""
            last_err = "返回空响应"
            continue
        last_text = text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            last_err = f"非合法 JSON: {e}"
            continue
        # 部分 LLM（如 deepseek-v4-flash）会把 markdown 模板/列表/段落误当成
        # JSON 返（顶层是 list 而不是 dict）。在 stage 脚本 `.get()` 之前
        # 显式拦截，避免 AttributeError: 'list' object has no attribute 'get'
        # 这种下游不清晰的报错。
        if not isinstance(parsed, dict):
            last_err = (
                f"JSON 顶层是 {type(parsed).__name__}（期望 dict）"
            )
            continue
        return parsed

    raise RuntimeError(
        f"facade.agent({name}) 重试 3 次仍解析失败: {last_err}\n"
        f"--- 最后一次原始响应（前 500 字）---\n{last_text[:500]}"
    )


# ════════════════════════════════════════════════════════════════
# 拉起 sub-agent（session 化版本，2026-08-29 新增）
# ════════════════════════════════════════════════════════════════


async def call_session_async(
    name: str,
    payload: dict,
    *,
    phase: str | None = None,
    session_holder: dict | None = None,
    instructions: str | None = None,
) -> dict:
    """
    拉起一个 stateful sub-agent session（多轮 history 自动累积）。

    与 ``call_agent_async`` 的区别：
        - call_agent_async: 单次 LLM 调，每次重新拼完整 prompt（无 history）
        - call_session_async: 拉起 AgentSession，多次 send 自动累积 history

    用法（在反思循环里）：
        holder = {}  # 跨轮复用 session
        for round_idx in range(1, 4):
            payload = {...当前 plan + 上一轮 feedback...}
            result = await call_session_async(
                "method-designer",
                payload,
                phase="方法设计",
                session_holder=holder,  # ← 同一 session 跨轮复用
            )
        # holder 里的 session 仍开着（不主动关，session 退出时自动回收）

    Args:
        name:           agent 名（同 call_agent_async）。
        payload:        当前轮派发的数据 dict。
        phase:          workflow phase 名（用于 trace）。
        session_holder: dict（key 任意，value 是 AgentSession）；传 None 时本函数
                        新建一个 session 并放进 holder。**业务层**应在每阶段
                        （方法设计 / 实验规划）开始时新建 holder，结束时丢弃。
        instructions:   session-level instructions（如"只可见方法阶段人审反馈"）。

    Returns:
        sub-agent 当前轮输出的 dict（同 call_agent_async）。

    Raises:
        同 call_agent_async。
    """
    md_path = agent_md_path(name)
    persona = extract_persona(md_path)
    base_prompt = build_agent_prompt(persona, payload)
    output_schema = _output_schema(name)

    # session_holder 复用：业务层传 holder 时，第一次 send 后填入 session
    sess = session_holder.get("sess") if session_holder else None
    if sess is None:
        sess = agent_session(
            label=name,
            phase=phase,
            instructions=instructions,
        )
        if session_holder is not None:
            session_holder["sess"] = sess

    # 解析失败 retry（同 call_agent_async，但走 session.send）
    last_text = ""
    last_err = "未尝试"
    for attempt in range(1, 4):
        if attempt == 1:
            prompt = base_prompt
        else:
            prompt = (
                base_prompt
                + "\n\n[RETRY "
                + str(attempt - 1)
                + "] 你上一次的输出无法解析: "
                + last_err
                + "\n"
                + "你的上一次原始输出（前 500 字）:\n"
                + last_text[:500]
                + "\n\n请严格遵守上方 OUTPUT FORMAT —— "
                + "顶层必须是**单个 JSON object**（以 { 开头），"
                + "不要 markdown 标题、列表、代码块、解释文字。"
            )
        async with _llm_heartbeat(name, attempt - 1 if attempt > 1 else 0):
            response = await sess.send(prompt, schema=output_schema)
        if output_schema is not None:
            if isinstance(response, dict):
                return _normalize_structured_output(name, response)
            # 同 call_agent_async：结构化模式下的格式漂移要走纠错重试，不能直接放弃。
            last_text = response if isinstance(response, str) else ""
            last_err = f"schema 模式下返回 {type(response).__name__}（期望 JSON object）"
            continue
        text = response if isinstance(response, str) else ""
        if not text:
            last_text = ""
            last_err = "返回空响应"
            continue
        last_text = text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            last_err = f"非合法 JSON: {e}"
            continue
        if not isinstance(parsed, dict):
            last_err = (
                f"JSON 顶层是 {type(parsed).__name__}（期望 dict）"
            )
            continue
        return parsed

    raise RuntimeError(
        f"agent_session.send({name}) 重试 3 次仍解析失败: {last_err}\n"
        f"--- 最后一次原始响应（前 500 字）---\n{last_text[:500]}"
    )
