# -*- coding: utf-8 -*-
"""_experiment_bootstrap.py — stage2 planning 产物 → 模块三严格输入的确定性投影。

## 为什么存在

模块二（planning）的 LLM 产物是"富字段"的：`framework` 是 dict、`objectives` 是 list[dict]、
datasets 带 `id`/`notes`。模块三（experiment）的 `contracts.py` 全部 `extra="forbid"` 且字段
形状严格。两边直接对接会炸 40+ 个字段级错误。

原先走模块三的 `adapt-planning` CLI，但它 **fail-closed 强制要求已存在的 implementation
manifest**，而 manifest 又要由已验证的 request 来 scaffold —— 死结。模块三 SKILL.md 执行步骤 1
允许"标准输入直接校验"，所以这里直接产出标准 `ExperimentModuleInput`，绕开 adapt。

## 红线

1. **不改 `experiment/`（同事的模块三）一行**——planning 向 experiment 对齐，投影层放 paper-gen。
2. **不改 stage2 原产物**——模块四 writing 还要消费富字段版；投影只写到 stage3 目录。
3. **契约知识零复制**——白名单从模块三 pydantic `model_fields` 动态取；manifest 直接复用同事的
   `scaffold_manifest()`；矩阵规则复用模块二 `validate_plan._check_experiment_matrix`。
   同事契约演进时这里自动跟随，不需要人肉同步。

## 投影 vs 猜测

只做**确定性形状归一**（dict→str、白名单裁剪、缺省填充）和**可证明的引用修正**（casefold /
唯一模糊匹配）。矩阵行内容、无法唯一映射的引用 **不猜**——记为 error 让上层暴露漂移，
对齐模块三"不得静默猜测"的原则。所有动作写进 `projection_report.json` 供审计。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("paper_gen.experiment_bootstrap")

SCHEMA_VERSION = "2.0.0"
MANIFEST_NAME = "implementation-manifest.json"

# 6 个源文件（与模块三 planning_adapter.MODULE_TWO_FILES 对齐）。
# domain / resource_constraints 是流水线输入，由 _adapt_conception_to_planning 写到
# stage2_planning/_input/ 下，不是 planning 的产物 —— 所以单独指路。
_PLANNING_ARTIFACTS = {
    "method_design": "method_design.json",
    "experiment_plan": "experiment_plan.json",
    "data_plan": "data_plan.json",
    "execution_config": "execution_config.json",
}
_PIPELINE_INPUTS = {
    "domain": "domain.json",
    "resource_constraints": "resource_constraints.json",
}

# planning 的 novelty_degree 词表 → 模块三 Component.novelty_degree 的 novel|adapted|standard
_NOVELTY_MAP = {
    "high": "novel",
    "medium": "adapted",
    "low": "standard",
    "novel": "novel",
    "adapted": "adapted",
    "standard": "standard",
}

_SCALE_PLACEHOLDER = "unknown (planner omitted)"


# ══════════════════════════════════════════════════════════════════
# 模块三 / 模块二 的动态加载
# ══════════════════════════════════════════════════════════════════


class _Module3:
    """模块三 scripts 的按需加载句柄。

    模块三内部用扁平 import（`from contracts import ...`），所以必须把它的 scripts 目录
    塞进 sys.path 才能加载。副作用：`contracts` / `io_utils` 这类通用名字会进 sys.modules，
    调用方别在同一进程里加载同名模块。
    """

    def __init__(self, scripts_dir: Path) -> None:
        self.scripts_dir = scripts_dir
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import contracts
        import planning_adapter
        from scaffold_manifest import scaffold_manifest

        self.contracts = contracts
        self.request_model = contracts.ExperimentModuleInput
        self.stable_run_id = planning_adapter._stable_run_id
        self.scaffold_manifest = scaffold_manifest


def _resolve_skill_scripts(skill: str) -> Path:
    """定位某个 skill 的 scripts 目录（复用 _stage_runner 的 walk-up + editable 兜底）。

    延迟 import _stage_runner 避免循环：_stage_runner 在模块级 import 本模块，
    而本模块只在运行时才回头拿它的 helper。
    """
    # 已安装的 workspace 中，各 skill 与 paper-gen 是同级目录。先走这个与
    # 当前工作目录无关的确定性路径，避免从仓库根直接运行测试/CLI 时，
    # _stage_runner 的向上查找看不到嵌套在 jiuwenswarm/resources 下的 skills。
    sibling_main = Path(__file__).resolve().parents[2] / skill / "scripts" / "main.py"
    if sibling_main.is_file():
        return sibling_main.parent

    from _stage_runner import _resolve_skill_main

    return _resolve_skill_main(skill).parent


def _load_module3() -> _Module3:
    return _Module3(_resolve_skill_scripts("experiment"))


def _load_matrix_checker():
    """拿模块二的矩阵校验器，用于一次性枚举全部矩阵违规。

    模块三的 `model_validator(mode="after")` 遇到第一条 cross-ref 错误就抛，
    调试时只能一条条挤。模块二 `_check_experiment_matrix` 实现的是同一套硬规则
    （planning-schemas.md §2.3.4 ←→ experiment-schemas.md §2.2.2），且一次返回全部错误。

    返回 None 表示拿不到——降级为只报模块三的第一条错，不是致命问题。
    """
    try:
        scripts_dir = _resolve_skill_scripts("planning")
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from validate_plan import _check_experiment_matrix

        return _check_experiment_matrix
    except (ImportError, OSError, FileNotFoundError, AttributeError) as exc:
        log.warning("模块二矩阵校验器不可用，降级为单条报错: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════
# 投影审计报告
# ══════════════════════════════════════════════════════════════════


class ProjectionReport:
    """记录每一次形状归一 / 引用修正 / 无法修复的漂移。

    warn  = 已确定性修好，模块三能吃
    error = 修不了（需要模块二重新生成或人工介入）
    """

    def __init__(self) -> None:
        self.notes: list[dict[str, str]] = []

    def warn(self, field_path: str, action: str, detail: str = "") -> None:
        self.notes.append({
            "severity": "warn",
            "field_path": field_path,
            "action": action,
            "detail": detail,
        })

    def error(self, field_path: str, action: str, detail: str = "") -> None:
        self.notes.append({
            "severity": "error",
            "field_path": field_path,
            "action": action,
            "detail": detail,
        })

    @property
    def errors(self) -> list[dict[str, str]]:
        return [n for n in self.notes if n["severity"] == "error"]

    @property
    def warnings(self) -> list[dict[str, str]]:
        return [n for n in self.notes if n["severity"] == "warn"]

    def summary_lines(self, limit: int = 40) -> list[str]:
        return [
            f"[{n['severity']}] {n['field_path']}: {n['action']}"
            + (f" ({n['detail']})" if n["detail"] else "")
            for n in self.notes[:limit]
        ]


# ══════════════════════════════════════════════════════════════════
# 通用投影 helper
# ══════════════════════════════════════════════════════════════════


def _allowed_fields(model: Any) -> set[str]:
    """从模块三 pydantic 模型动态取合法字段名（契约零复制的关键）。"""
    return set(model.model_fields)


def _note_line(note: dict[str, str], detail_limit: int = 160) -> str:
    """审计条目 → 单行摘要（带 detail，方便直接进 stage error 列表）。"""
    line = f"{note['field_path']}: {note['action']}"
    detail = note.get("detail", "")
    if detail:
        clipped = detail if len(detail) <= detail_limit else detail[:detail_limit] + "..."
        line = f"{line} — {clipped}"
    return line


def _whitelist(
    payload: Any,
    model: Any,
    path: str,
    report: ProjectionReport,
) -> dict[str, Any]:
    """按模块三模型字段裁剪 dict；被裁字段记 warn。"""
    if not isinstance(payload, dict):
        report.error(path, "期望 JSON 对象", f"实际 {type(payload).__name__}")
        return {}
    allowed = _allowed_fields(model)
    kept = {k: v for k, v in payload.items() if k in allowed}
    dropped = sorted(set(payload) - allowed)
    if dropped:
        report.warn(path, f"裁掉模块三不接受的字段: {', '.join(dropped)}")
    return kept


def _as_text(value: Any) -> str:
    """把任意标量/结构压成单行文本（只在已确认要降维时用）。"""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _infer_unique_task_type_from_metrics(metrics: Any) -> str | None:
    """仅从唯一标准指标族推导任务类型；歧义或混合指标返回 None。"""
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
    return next(iter(families)) if len(families) == 1 else None


def _coerce_int(value: Any, path: str, report: ProjectionReport) -> Any:
    """int 字段的宽松收窄（42.0 → 42）；不可无损转换时原样保留让模块三报错。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        report.warn(path, f"float→int: {value} → {int(value)}")
        return int(value)
    return value


# ══════════════════════════════════════════════════════════════════
# method_design 投影
# ══════════════════════════════════════════════════════════════════


def _project_framework(value: Any, report: ProjectionReport) -> str:
    """`framework` dict → "name: overview" 单行文本。"""
    path = "method_design.framework"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = _as_text(value.get("name"))
        overview = _as_text(value.get("overview"))
        parts = [p for p in (name, overview) if p]
        if not parts:
            report.error(path, "dict 里既没有 name 也没有 overview", _as_text(value)[:200])
            return ""
        text = ": ".join(parts)
        report.warn(path, "dict→str", "取 name: overview（stages 明细留在 stage2 原产物）")
        return text
    report.error(path, "既不是 str 也不是 dict", f"实际 {type(value).__name__}")
    return _as_text(value)


def _project_technical_route(value: Any, report: ProjectionReport) -> str:
    """把模块二的结构化技术路线无损降维成模块三可读文本。

    ``step`` 在真实规划里只是序号；旧实现只取它，导致完整路线变成
    ``1) 1 / 2) 2``，Implementation Agent 因而无法生成实现。
    """
    path = "method_design.technical_route"
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        report.error(path, "既不是 str 也不是 list", f"实际 {type(value).__name__}")
        return _as_text(value)
    steps: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw_step = _as_text(item.get("step"))
            number = raw_step if raw_step.isdigit() else ""
            name = _as_text(item.get("name"))
            description = _as_text(item.get("description"))
            duration = _as_text(
                item.get("duration_estimate") or item.get("duration_hours")
            )
            body = " — ".join(part for part in (name, description) if part)
            if not body and raw_step and not number:
                body = raw_step
            if duration:
                body = f"{body} [{duration}]" if body else f"耗时 {duration}"
            if number and body:
                steps.append(f"{number}) {body}")
            elif body:
                steps.append(body)
            else:
                steps.append(number or _as_text(item))
        else:
            steps.append(_as_text(item))
    steps = [s for s in steps if s]
    if not steps:
        report.error(path, "list 为空或全是空步骤")
        return ""
    report.warn(path, "list→str", f"{len(steps)} 步编号拼接")
    return "\n".join(
        step if step.lstrip().split(")", 1)[0].isdigit() and ")" in step
        else f"{i}) {step}"
        for i, step in enumerate(steps, start=1)
    )


def _project_algorithm_reference(value: Any, report: ProjectionReport) -> list[str]:
    """`algorithm_reference` list[dict] → list[str]。"""
    path = "method_design.algorithm_reference"
    if not isinstance(value, list):
        if value is None:
            return []
        report.error(path, "期望 list", f"实际 {type(value).__name__}")
        return []
    out: list[str] = []
    converted = False
    for item in value:
        if isinstance(item, str):
            out.append(item.strip())
            continue
        if isinstance(item, dict):
            converted = True
            head = _as_text(item.get("paper_id"))
            algo = _as_text(item.get("algorithm"))
            adapt = _as_text(item.get("adaptation") or item.get("usage"))
            text = ": ".join(p for p in (head, algo) if p)
            if adapt:
                text = f"{text} — {adapt}" if text else adapt
            out.append(text or _as_text(item))
            continue
        converted = True
        out.append(_as_text(item))
    if converted:
        report.warn(
            path,
            "list[dict]→list[str]",
            "格式 'paper_id: algorithm — adaptation/usage'，保留论文在本方法中的用途",
        )
    return [s for s in out if s]


def _project_novelty(value: Any, path: str, report: ProjectionReport) -> Any:
    """planning 的 high/medium/low → 模块三 novel|adapted|standard。"""
    if not isinstance(value, str) or not value.strip():
        report.error(path, "期望非空字符串", f"实际 {value!r}")
        return value
    raw = value.strip()
    key = raw.casefold()
    if key in _NOVELTY_MAP:
        mapped = _NOVELTY_MAP[key]
        if mapped != raw:
            report.warn(path, f"枚举映射 {raw!r} → {mapped!r}")
        return mapped
    for needle, mapped in (("novel", "novel"), ("adapt", "adapted"), ("standard", "standard")):
        if needle in key:
            report.warn(path, f"子串匹配 {raw!r} → {mapped!r}")
            return mapped
    report.warn(path, f"无法识别 {raw!r}，兜底 'adapted'", "建议模块二收紧 novelty_degree 词表")
    return "adapted"


def _project_evidence_metric(
    value: Any,
    metrics: list[str],
    path: str,
    report: ProjectionReport,
) -> Any:
    """把模块二的 `evidence_metric` 降档成模块三要的单个 str（逐字符 ∈ metrics）。

    模块二侧（2026-09-09 起）是 **list[EvidenceMetric]** 结构化对象
    （planning-schemas.md §2.1.4），模块三 `contracts.InnovationPoint.evidence_metric`
    是单个 str，所以这里是有损降档：

    - 取 ``[0].metric_name``，剩下的判据 **warn 列出来**（不静默丢）。
      为什么必须列：一个 claim 常有多条判据（既提升效果、又不增开销），只搬第一条
      等于让模块三只验一半，而"只验了一半"这件事本身不能悄悄发生。
    - 阈值/方向/基准（comparator / threshold / unit / vs）在模块三这个字段里
      **无法表达**，一并 warn 记账；它们仍完整保留在模块二原始产物里供模块四写作。

    legacy 兼容：旧产物是自由字符串（"metric_x >= 0.9; metric_y >= 5x"）。仍接受，
    但走"扫表达式取最早出现、并列取最长"的启发式并 warn——那个启发式**会静默丢掉
    第一个之外的全部判据**，正是把这个字段结构化的直接原因。
    """
    # --- 新形状：list[EvidenceMetric] ---
    if isinstance(value, list):
        if not value:
            report.error(path, "evidence_metric 为空列表", "至少 1 条判据")
            return ""
        entries = [em for em in value if isinstance(em, dict)]
        if not entries:
            report.error(path, "evidence_metric 列表内没有 dict 判据", f"实际 {value!r}")
            return ""
        first = entries[0]
        name = first.get("metric_name")
        if not isinstance(name, str) or not name.strip():
            report.error(f"{path}[0].metric_name", "缺失或为空")
            return ""
        name = name.strip()
        if metrics and name not in metrics:
            report.error(
                f"{path}[0].metric_name",
                "不在 experiment_plan.metrics 内（模块三 cross-ref 会拒收）",
                f"实际 {name!r}；可选 {metrics}",
            )
        # 判据里的阈值语义在模块三这个 str 字段里放不下——记账，不静默丢
        dropped = _describe_evidence_metric(first)
        if dropped:
            report.warn(path, f"阈值语义降档丢失: {dropped}",
                        "模块三 evidence_metric 只是指标名；完整判据见模块二原始产物")
        if len(entries) > 1:
            rest = "; ".join(_describe_evidence_metric(em, with_name=True) for em in entries[1:])
            report.warn(
                path,
                f"该创新点有 {len(entries)} 条判据，模块三只能承载 1 条",
                f"未传递: {rest}",
            )
        return name

    # --- legacy：自由字符串 ---
    if not isinstance(value, str) or not value.strip():
        report.error(path, "期望 list[EvidenceMetric]（§2.1.4）", f"实际 {value!r}")
        return value
    raw = value.strip()
    report.warn(path, "evidence_metric 是 legacy 自由字符串",
                "2026-09-09 起应为 list[EvidenceMetric]；字符串走启发式收敛，可能丢判据")
    if raw in metrics:
        return raw
    for candidates, label in ((metrics, "精确子串"), (metrics, "忽略大小写子串")):
        haystack = raw if label == "精确子串" else raw.casefold()
        hits = []
        for metric in candidates:
            needle = metric if label == "精确子串" else metric.casefold()
            position = haystack.find(needle)
            if position >= 0:
                hits.append((position, -len(metric), metric))
        if hits:
            hits.sort()
            chosen = hits[0][2]
            report.warn(
                path,
                f"表达式收敛到计划指标 {chosen!r}（{label}）",
                f"原值 {raw[:120]!r}"
                + (f"；表达式里另有 {len(hits) - 1} 个指标被丢弃: "
                   f"{[h[2] for h in hits[1:]]}" if len(hits) > 1 else ""),
            )
            return chosen
    report.error(
        path,
        "无法映射到 experiment_plan.metrics 中任一指标",
        f"原值 {raw[:120]!r}；可选 {metrics}",
    )
    return raw


def _describe_evidence_metric(em: dict, with_name: bool = False) -> str:
    """把 EvidenceMetric 的阈值语义压成一行人类可读文本（用于 warn 记账）。"""
    parts: list[str] = []
    if with_name and isinstance(em.get("metric_name"), str):
        parts.append(str(em["metric_name"]))
    comparator = em.get("comparator")
    threshold = em.get("threshold")
    if comparator is not None and threshold is not None:
        parts.append(f"{comparator} {threshold}{em.get('unit') or ''}")
    if em.get("comparison_mode") == "delta" and em.get("vs"):
        parts.append(f"vs {em['vs']}")
    return " ".join(parts)


def _project_experiment_ref(
    value: Any,
    known_ids: list[str],
    path: str,
    report: ProjectionReport,
) -> Any:
    """`experiment_ref` 必须 ∈ primary_experiments ∪ 矩阵首列。

    精确 → 原样；casefold 命中 → 修正；difflib 唯一近似（cutoff 0.6）→ 修正；
    否则 error（保留原值，让模块三 REPLAN）。
    """
    if not isinstance(value, str) or not value.strip():
        report.error(path, "期望非空字符串", f"实际 {value!r}")
        return value
    raw = value.strip()
    if raw in known_ids:
        return raw
    folded = {i.casefold(): i for i in known_ids}
    if raw.casefold() in folded:
        fixed = folded[raw.casefold()]
        report.warn(path, f"大小写归一 {raw!r} → {fixed!r}")
        return fixed
    close = difflib.get_close_matches(raw.casefold(), list(folded), n=2, cutoff=0.6)
    if len(close) == 1:
        fixed = folded[close[0]]
        report.warn(path, f"唯一近似匹配 {raw!r} → {fixed!r}", "difflib cutoff=0.6")
        return fixed
    report.error(
        path,
        "无法唯一映射到 primary_experiments 或矩阵首列",
        f"原值 {raw!r}；候选 {known_ids}"
        + (f"；近似歧义 {close}" if close else ""),
    )
    return raw


def _project_method_design(
    payload: Any,
    module3: _Module3,
    metrics: list[str],
    known_ids: list[str],
    report: ProjectionReport,
) -> dict[str, Any]:
    contracts = module3.contracts
    out = _whitelist(payload, contracts.MethodDesign, "method_design", report)
    if not out:
        return out

    out["research_goal"] = _as_text(out.get("research_goal"))
    out["core_mechanism"] = _as_text(out.get("core_mechanism"))
    out["framework"] = _project_framework(out.get("framework"), report)
    out["technical_route"] = _project_technical_route(out.get("technical_route"), report)
    out["algorithm_reference"] = _project_algorithm_reference(
        out.get("algorithm_reference"), report
    )

    components = out.get("components")
    if isinstance(components, list):
        projected = []
        for index, item in enumerate(components):
            path = f"method_design.components[{index}]"
            entry = _whitelist(item, contracts.Component, path, report)
            for key in ("name", "function", "input_schema", "output_schema"):
                if key in entry:
                    entry[key] = _as_text(entry[key])
            if "novelty_degree" in entry:
                entry["novelty_degree"] = _project_novelty(
                    entry["novelty_degree"], f"{path}.novelty_degree", report
                )
            projected.append(entry)
        out["components"] = projected

    points = out.get("innovation_points")
    if isinstance(points, list):
        projected = []
        for index, item in enumerate(points):
            path = f"method_design.innovation_points[{index}]"
            entry = _whitelist(item, contracts.InnovationPoint, path, report)
            if "claim" in entry:
                entry["claim"] = _as_text(entry["claim"])
            if "evidence_metric" in entry:
                entry["evidence_metric"] = _project_evidence_metric(
                    entry["evidence_metric"], metrics, f"{path}.evidence_metric", report
                )
            if "experiment_ref" in entry:
                entry["experiment_ref"] = _project_experiment_ref(
                    entry["experiment_ref"], known_ids, f"{path}.experiment_ref", report
                )
            projected.append(entry)
        out["innovation_points"] = projected

    coverage = out.get("hypothesis_coverage")
    if isinstance(coverage, list):
        projected = []
        for index, item in enumerate(coverage):
            path = f"method_design.hypothesis_coverage[{index}]"
            # 模块三 HypothesisCoverage 没有 evidence_metric —— _whitelist 会裁掉
            entry = _whitelist(item, contracts.HypothesisCoverage, path, report)
            for key in ("hypothesis_id", "mechanism"):
                if key in entry:
                    entry[key] = _as_text(entry[key])
            if "experiment_ref" in entry:
                entry["experiment_ref"] = _project_experiment_ref(
                    entry["experiment_ref"], known_ids, f"{path}.experiment_ref", report
                )
            projected.append(entry)
        out["hypothesis_coverage"] = projected

    limitations = out.get("limitations")
    if isinstance(limitations, list):
        out["limitations"] = [_as_text(i) for i in limitations if _as_text(i)]

    if out.get("implementable") is not True:
        report.error(
            "method_design.implementable",
            "模块三要求必须为 true",
            f"实际 {out.get('implementable')!r}",
        )
    return out


# ══════════════════════════════════════════════════════════════════
# experiment_plan / data_plan 投影
# ══════════════════════════════════════════════════════════════════


def _project_datasets(
    payload: Any,
    module3: _Module3,
    path: str,
    report: ProjectionReport,
) -> list[dict[str, Any]]:
    """Project datasets while preserving an optional Module 2 source URL."""
    if not isinstance(payload, list):
        report.error(path, "期望 list", f"实际 {type(payload).__name__}")
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        item_path = f"{path}[{index}]"
        normalized_item = dict(item) if isinstance(item, dict) else item
        if isinstance(normalized_item, dict):
            required_fields = normalized_item.get("required_fields")
            if isinstance(required_fields, dict):
                aliases = {
                    "question_field": required_fields.get("question"),
                    "answer_field": required_fields.get("answer"),
                    "ground_truth_field": required_fields.get("answer"),
                }
                for target, value in aliases.items():
                    if _as_text(value) and not _as_text(normalized_item.get(target)):
                        normalized_item[target] = _as_text(value)
                report.warn(
                    f"{item_path}.required_fields",
                    "映射为模块三 question_field/answer_field/ground_truth_field",
                    "context 字段由 memory_compression 固定执行器按 context/history/memory 字段族解析",
                )
        entry = _whitelist(normalized_item, module3.contracts.DatasetSpec, item_path, report)
        # 数据集条目在模块三运行时契约里不允许携带 task_type / label_column
        # （部分部署副本的 DatasetSpec 仍是旧版，extra=forbid 会直接拒收整轮请求）。
        # 这两个键的语义已由 data_plan.expected_size 承载，投影层在此逐字上移并裁掉。
        for lifted in ("task_type", "label_column"):
            if lifted in entry:
                entry.pop(lifted)
                report.warn(
                    f"{item_path}.{lifted}",
                    f"上移到 data_plan.expected_size（数据集条目不得携带 {lifted}）",
                )
        if "name" in entry:
            entry["name"] = _as_text(entry["name"])
        raw_url = entry.get("source_url")
        url = _as_text(raw_url)
        if url and not url.startswith(("http://", "https://")):
            report.error(
                f"{item_path}.source_url",
                "提供地址时必须为 HttpUrl（http/https）；可留空由模块三按名称检索",
                f"实际 {url[:120]!r}",
            )
        entry["source_url"] = url or None
        if not _as_text(entry.get("scale_estimate")):
            entry["scale_estimate"] = _SCALE_PLACEHOLDER
            report.warn(
                f"{item_path}.scale_estimate",
                f"缺失，填占位 {_SCALE_PLACEHOLDER!r}",
                "模块三必填；建议模块二补真实规模估计",
            )
        readiness = _as_text(entry.get("readiness"))
        if readiness not in {"available", "download", "apply"}:
            report.error(
                f"{item_path}.readiness",
                "期望 available|download|apply",
                f"实际 {readiness!r}",
            )
        else:
            entry["readiness"] = readiness
        if not isinstance(entry.get("preprocess_required"), list):
            entry.pop("preprocess_required", None)
        out.append(entry)
    return out


def _project_experiment_plan(
    payload: Any,
    module3: _Module3,
    datasets: list[dict[str, Any]],
    report: ProjectionReport,
) -> dict[str, Any]:
    contracts = module3.contracts
    out = _whitelist(payload, contracts.ExperimentPlan, "experiment_plan", report)
    if not out:
        return out

    out["datasets"] = datasets

    metric_definitions = out.get("metric_definitions")
    if isinstance(metric_definitions, dict):
        normalized_definitions: list[dict[str, Any]] = []
        for metric_name, definition in metric_definitions.items():
            if not isinstance(metric_name, str) or not metric_name.strip():
                report.error(
                    "experiment_plan.metric_definitions",
                    "字典键必须是非空指标名",
                    f"实际 {metric_name!r}",
                )
                continue
            if isinstance(definition, str) and definition.strip():
                normalized_definitions.append({
                    "metric_name": metric_name.strip(),
                    "definition": definition.strip(),
                })
            elif isinstance(definition, dict):
                item = dict(definition)
                item.setdefault("metric_name", metric_name.strip())
                normalized_definitions.append(item)
            else:
                report.error(
                    f"experiment_plan.metric_definitions.{metric_name}",
                    "定义必须是非空字符串或对象",
                    f"实际 {type(definition).__name__}",
                )
        out["metric_definitions"] = normalized_definitions
        report.warn(
            "experiment_plan.metric_definitions",
            "dict→list[MetricDefinitionSpec]",
            "只做形状转换；模块二定义仍是提案，不会因此获得 verified=true",
        )

    objectives = out.get("objectives")
    if isinstance(objectives, list):
        converted = False
        flattened: list[str] = []
        for item in objectives:
            if isinstance(item, dict):
                converted = True
                flattened.append(_as_text(item.get("description")) or _as_text(item))
            else:
                flattened.append(_as_text(item))
        if converted:
            report.warn(
                "experiment_plan.objectives",
                "list[dict]→list[str]",
                "取 description（hypothesis_id/experiment_ids 留在 stage2 原产物）",
            )
        out["objectives"] = [s for s in flattened if s]

    baselines = out.get("baselines")
    if isinstance(baselines, list):
        projected = []
        for index, item in enumerate(baselines):
            path = f"experiment_plan.baselines[{index}]"
            entry = _whitelist(item, contracts.BaselineSpec, path, report)
            for key in ("name", "paper_id", "metric_name"):
                if key in entry:
                    entry[key] = _as_text(entry[key])
            projected.append(entry)
        roles = _registered_method_roles(module3, out.get("experiment_matrix"))
        primary_method = roles.get("primary_method_id")
        allowed_baselines = set(roles.get("baseline_method_ids") or [])
        if primary_method or allowed_baselines:
            corrected = [
                entry for entry in projected
                if entry.get("name") != primary_method
                and (not allowed_baselines or entry.get("name") in allowed_baselines)
            ]
            removed = [
                entry.get("name") for entry in projected if entry not in corrected
            ]
            if removed:
                report.warn(
                    "experiment_plan.baselines",
                    f"按注册执行能力移除非基线方法 {removed}",
                    f"primary_method_id={primary_method!r}; baseline_method_ids={sorted(allowed_baselines)}",
                )
            projected = corrected
        out["baselines"] = projected

    ablation = out.get("ablation_plan")
    if isinstance(ablation, list):
        projected = []
        for index, item in enumerate(ablation):
            path = f"experiment_plan.ablation_plan[{index}]"
            entry = _whitelist(item, contracts.Ablation, path, report)
            for key in ("component", "removed_by", "expected_impact"):
                if key in entry and entry[key] is not None:
                    entry[key] = _as_text(entry[key])
            projected.append(entry)
        out["ablation_plan"] = projected

    for key in ("metrics", "primary_experiments", "expected_results", "success_criteria"):
        value = out.get(key)
        if isinstance(value, list):
            out[key] = [_as_text(i) for i in value if _as_text(i)]

    matrix = out.get("experiment_matrix")
    if isinstance(matrix, list):
        out["experiment_matrix"] = [
            [_as_text(cell) for cell in row] if isinstance(row, list) else row
            for row in matrix
        ]

    if isinstance(out.get("compute_estimate"), int) and not isinstance(
        out.get("compute_estimate"), bool
    ):
        out["compute_estimate"] = float(out["compute_estimate"])
    return out


def _registered_method_roles(module3: _Module3, matrix: Any) -> dict[str, Any]:
    """Resolve primary/baseline roles from module three's published contract."""
    path = module3.scripts_dir.parent / "references" / "execution-capabilities.json"
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    matrix_methods = {
        _as_text(row[2])
        for row in matrix or []
        if isinstance(row, list) and len(row) >= 3 and _as_text(row[2])
    }
    matches = []
    for profile in contract.get("available_requirement_profiles") or []:
        if not isinstance(profile, dict):
            continue
        allowed = {_as_text(value) for value in profile.get("method_ids") or []}
        if matrix_methods and matrix_methods <= allowed:
            matches.append(profile)
    return matches[0] if len(matches) == 1 else {}


def _project_data_plan(
    payload: Any,
    module3: _Module3,
    datasets: list[dict[str, Any]],
    seeds: list[int],
    report: ProjectionReport,
    *,
    raw_plan_datasets: Any = None,
    plan_metrics: Any = None,
) -> dict[str, Any]:
    """data_plan 投影。

    关键：`datasets` **直接复用 experiment_plan 投影后的同一份对象**——模块三
    `validate_cross_references` 要求两边 `model_dump(mode="json")` 完全相等，
    分别投影会因为占位值/裁剪顺序不同而不等。
    """
    contracts = module3.contracts
    out = _whitelist(payload, contracts.DataPlan, "data_plan", report)
    if not out:
        out = {}
    out["datasets"] = datasets

    split = out.get("split_strategy")
    fallback_seed = seeds[0] if seeds else 42
    if not isinstance(split, dict) or not split:
        out["split_strategy"] = {"method": "predefined_splits", "seed": fallback_seed}
        report.warn(
            "data_plan.split_strategy",
            f"缺失，填 predefined_splits + seed={fallback_seed}",
            "模块三必填且必须含 seed；建议模块二显式输出切分方案",
        )
    elif "seed" not in split:
        out["split_strategy"] = {**split, "seed": fallback_seed}
        report.warn(
            "data_plan.split_strategy.seed",
            f"缺失，补 execution_config.seeds[0]={fallback_seed}",
        )

    pipeline = out.get("preprocessing_pipeline")
    if not isinstance(pipeline, list) or not [_as_text(i) for i in pipeline if _as_text(i)]:
        out["preprocessing_pipeline"] = ["identity"]
        report.warn(
            "data_plan.preprocessing_pipeline",
            "缺失/为空，填 ['identity']",
            "与模块三 adapt 的同名兜底一致",
        )
    else:
        out["preprocessing_pipeline"] = [_as_text(i) for i in pipeline if _as_text(i)]

    if not isinstance(out.get("expected_size"), dict):
        out["expected_size"] = {}
        report.warn("data_plan.expected_size", "缺失/非对象，填 {}")

    # 模块二当前把任务类型和标签列放在 datasets[]，模块三的数据准备器则从
    # expected_size 读取。这里只做同一数据集内的逐字迁移，不从名称或领域猜测。
    # 单数据集时含义唯一；多数据集只有所有非空值完全一致时才可安全迁移。
    expected_size = out["expected_size"]
    raw_datasets = raw_plan_datasets if isinstance(raw_plan_datasets, list) else []
    for key in ("task_type", "label_column"):
        if _as_text(expected_size.get(key)):
            continue
        values = {
            _as_text(item.get(key))
            for item in raw_datasets
            if isinstance(item, dict) and _as_text(item.get(key))
        }
        if len(values) == 1:
            value = next(iter(values))
            expected_size[key] = value
            report.warn(
                f"data_plan.expected_size.{key}",
                f"从 experiment_plan.datasets[] 无损迁移 {value!r}",
                "模块二与模块三字段位置不同；未进行语义推断",
            )

    # 最后一层兼容保险：旧版/漂移版模块二可能同时漏掉 expected_size.task_type
    # 与 datasets[].task_type。只有当标准指标族能唯一决定任务时才补齐；accuracy、
    # 普通 recall 等歧义指标或混合任务保持为空并让模块三 fail-closed。
    declared_dataset_tasks = {
        _as_text(item.get("task_type")).casefold()
        for item in raw_datasets
        if isinstance(item, dict) and _as_text(item.get("task_type"))
    }
    if not _as_text(expected_size.get("task_type")) and not declared_dataset_tasks:
        inferred_task = _infer_unique_task_type_from_metrics(plan_metrics)
        if inferred_task is not None:
            expected_size["task_type"] = inferred_task
            for dataset in out["datasets"]:
                if isinstance(dataset, dict) and not _as_text(dataset.get("task_type")):
                    dataset["task_type"] = inferred_task
            report.warn(
                "data_plan.expected_size.task_type",
                f"由唯一标准指标族确定性补齐 {inferred_task!r}",
                "未使用数据集名称或领域猜测；歧义/混合指标不会走此兜底",
            )
    return out


# ══════════════════════════════════════════════════════════════════
# domain / resource_constraints / execution_config 投影
# ══════════════════════════════════════════════════════════════════


def _project_domain(payload: Any, module3: _Module3, report: ProjectionReport) -> dict[str, Any]:
    out = _whitelist(payload, module3.contracts.Domain, "domain", report)
    if "domain_name" in out:
        out["domain_name"] = _as_text(out["domain_name"])
    return out


def _project_resource_constraints(
    payload: Any, module3: _Module3, report: ProjectionReport
) -> dict[str, Any]:
    out = _whitelist(payload, module3.contracts.ResourceConstraints, "resource_constraints", report)
    for key in ("gpu_hours", "memory_gb", "time_budget_days"):
        if key in out:
            out[key] = _coerce_int(out[key], f"resource_constraints.{key}", report)
    if isinstance(out.get("budget"), int) and not isinstance(out.get("budget"), bool):
        out["budget"] = float(out["budget"])
    if "gpu_type" in out and out["gpu_type"] is not None:
        out["gpu_type"] = _as_text(out["gpu_type"])
    return out


def _project_execution_config(
    payload: Any,
    module3: _Module3,
    run_dir: str,
    report: ProjectionReport,
) -> dict[str, Any]:
    """execution_config 投影。

    `run_dir` 由调用方算好并强制改写：模块二写的是 stage2 目录下的**绝对路径**，
    而模块三 `ExecutionConfig.run_dir` 有 `_validate_relative_path` —— 绝对路径、盘符、
    `..` 全部拒绝，且运行时按 `resolve_run_dir(run_dir, base_dir)` 解析。
    """
    out = _whitelist(payload, module3.contracts.ExecutionConfig, "execution_config", report)
    original = _as_text(out.get("run_dir"))
    out["run_dir"] = run_dir
    if original != run_dir:
        report.warn(
            "execution_config.run_dir",
            f"改写为 stage3 相对路径 {run_dir!r}",
            f"模块二原值 {original[:120]!r}（绝对路径，模块三只接受相对路径）",
        )
    seeds = out.get("seeds")
    if isinstance(seeds, list):
        out["seeds"] = [_coerce_int(s, "execution_config.seeds[]", report) for s in seeds]
    if "max_retries" in out:
        out["max_retries"] = _coerce_int(out["max_retries"], "execution_config.max_retries", report)
    return out


# ══════════════════════════════════════════════════════════════════
# 编排
# ══════════════════════════════════════════════════════════════════


def _read_sources(planning_dir: Path) -> dict[str, Any]:
    """读 6 个源文件。planning 产物在 planning_dir 根下，流水线输入在 _input/ 下。"""
    payloads: dict[str, Any] = {}
    missing: list[str] = []
    for name, filename in _PLANNING_ARTIFACTS.items():
        path = planning_dir / filename
        if not path.is_file():
            missing.append(str(path))
            continue
        payloads[name] = json.loads(path.read_text(encoding="utf-8"))
    for name, filename in _PIPELINE_INPUTS.items():
        # _input/ 优先（真实来源），planning_dir 根下兜底
        for candidate in (planning_dir / "_input" / filename, planning_dir / filename):
            if candidate.is_file():
                payloads[name] = json.loads(candidate.read_text(encoding="utf-8"))
                break
        else:
            missing.append(str(planning_dir / "_input" / filename))
    # research_question 不是模块三契约字段，但其中可能有模块一明确写出的
    # “X（主方法）”角色声明。把它作为可选审计依据读入，不能因缺失而阻塞旧用例。
    research_question = planning_dir / "_input" / "research_question.json"
    if research_question.is_file():
        payloads["research_question"] = json.loads(
            research_question.read_text(encoding="utf-8")
        )
    if missing:
        raise FileNotFoundError("stage2 产物缺失: " + "; ".join(missing))
    return payloads


_EXPLICIT_PRIMARY_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9_.-]*)\s*[（(]\s*主方法\s*[）)]"
)


def _reconcile_explicit_primary_and_seeds(
    canonical: dict[str, Any],
    *,
    research_question: Any,
    raw_method_design: Any,
    report: ProjectionReport,
) -> None:
    """按上游的明确声明修正角色，并消除模块二展开种子造成的重复。

    这里不做语义猜测：只有 research_question 明文包含 ``Name（主方法）``，且
    恰好得到一个主方法时才调整；框架名也必须与 method_design.framework.name
    精确相等才替换。所有改变进入 projection_report。
    """
    text = json.dumps(research_question, ensure_ascii=False, sort_keys=True)
    primary_names = sorted(set(_EXPLICIT_PRIMARY_RE.findall(text)))
    plan = canonical.get("experiment_plan")
    if not isinstance(plan, dict):
        return

    if len(primary_names) == 1:
        primary = primary_names[0]
        baselines = plan.get("baselines")
        if isinstance(baselines, list):
            kept = [
                item for item in baselines
                if not (isinstance(item, dict) and item.get("name") == primary)
            ]
            if len(kept) != len(baselines):
                plan["baselines"] = kept
                report.warn(
                    "experiment_plan.baselines",
                    f"移除被上游明确标注为主方法的 {primary!r}",
                    "依据 research_question 中的（主方法）声明",
                )

        framework_name = ""
        if isinstance(raw_method_design, dict):
            framework = raw_method_design.get("framework")
            if isinstance(framework, dict):
                framework_name = _as_text(framework.get("name"))
        matrix = plan.get("experiment_matrix")
        if framework_name and isinstance(matrix, list):
            replaced = 0
            for row in matrix:
                if not isinstance(row, list):
                    continue
                for index in range(2, len(row)):
                    if row[index] == framework_name:
                        row[index] = primary
                        replaced += 1
            if replaced:
                report.warn(
                    "experiment_plan.experiment_matrix",
                    f"将 {replaced} 个精确框架名替换为明确主方法 {primary!r}",
                    f"framework.name={framework_name!r}；依据（主方法）声明",
                )

    # 模块二有时会把“关闭数据溯源/减少种子/不导出产物”列为消融。这些条目既没有
    # 独立实现标识，又会破坏模块三强制证据链。若上游研究问题没有要求消融，且这些
    # 行有明确的 EXP-ABL 标识，则只从模块三的可执行投影中排除；stage2 原文件不改，
    # projection_report 完整记录，避免静默篡改科学目标。
    has_upstream_question = isinstance(research_question, dict) and bool(research_question)
    requested_ablation = "消融" in text
    matrix = plan.get("experiment_matrix")
    ablations = plan.get("ablation_plan")
    if (
        has_upstream_question
        and not requested_ablation
        and isinstance(matrix, list)
        and isinstance(ablations, list)
    ):
        kept_rows: list[Any] = []
        removed_ids: list[str] = []
        for row in matrix:
            exp_id = _as_text(row[0]) if isinstance(row, list) and row else ""
            if exp_id.upper().startswith("EXP-ABL"):
                removed_ids.append(exp_id)
            else:
                kept_rows.append(row)
        if removed_ids:
            plan["experiment_matrix"] = kept_rows
            plan["ablation_plan"] = []
            report.warn(
                "experiment_plan.ablation_plan",
                f"从可执行投影排除 {len(set(removed_ids))} 个未被选题要求的流程型消融",
                "这些条目会关闭溯源/多种子/聚合/产物导出且无独立实现；原始 stage2 规划保留不变；"
                f"experiment_ids={sorted(set(removed_ids))}",
            )

    # 模块二可能把 seed 展开成多行，模块三则通过 execution_config.seeds 统一展开。
    # 收集显式 seed 后从矩阵移除该参数并去重，避免 N×N 重复；没有明文 seed 就不动。
    matrix = plan.get("experiment_matrix")
    if not isinstance(matrix, list):
        return
    found_seeds: set[int] = set()
    normalized_rows: list[Any] = []
    seen: set[str] = set()
    removed_seed_tokens = 0
    duplicate_rows = 0
    for row in matrix:
        if not isinstance(row, list):
            normalized_rows.append(row)
            continue
        cleaned: list[Any] = []
        # 至少保留四个标准位置；四列行里的第 4 格即使长得像 seed= 也交给
        # 原契约判断，避免适配器把本来可诊断的行缩成另一种错误。
        may_extract_seed = len(row) >= 5
        for cell in row:
            value = _as_text(cell)
            if may_extract_seed and value.casefold().startswith("seed="):
                raw_seed = value.split("=", 1)[1].strip()
                try:
                    found_seeds.add(int(raw_seed))
                    removed_seed_tokens += 1
                    continue
                except ValueError:
                    pass
            cleaned.append(cell)
        signature = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
        if signature in seen:
            duplicate_rows += 1
            continue
        seen.add(signature)
        normalized_rows.append(cleaned)
    if found_seeds:
        plan["experiment_matrix"] = normalized_rows
        execution = canonical.get("execution_config")
        if isinstance(execution, dict):
            execution["seeds"] = sorted(found_seeds)
        report.warn(
            "experiment_plan.experiment_matrix",
            f"提取 {removed_seed_tokens} 个 seed 参数并去重 {duplicate_rows} 行",
            f"execution_config.seeds={sorted(found_seeds)}；由模块三统一展开",
        )


_AGGREGATE_METRIC_ALIASES = {
    "macro_f1_mean": "macro_f1",
    "macro_f1_std": "macro_f1",
    "macro_f1_range": "macro_f1",
    "accuracy_mean": "accuracy",
    "accuracy_std": "accuracy",
    "accuracy_range": "accuracy",
    "train_time_mean": "train_time_seconds",
    "inference_time_mean": "inference_time_seconds",
    "total_time_mean": "total_time_seconds",
}
_MATRIX_CONTROL_PARAMETERS = {"aggregate", "stratify", "test_size", "timer"}


def _normalize_runtime_metrics_and_controls(
    canonical: dict[str, Any], report: ProjectionReport
) -> None:
    """把模块二的聚合展示名投影为模块三逐次运行指标。

    模块三会在真实多种子运行后统一计算 mean/std/min/max/CI，因此运行器必须输出
    每次运行的基础指标，不能让单次运行伪装成 ``*_std``。矩阵中的切分、计时和
    聚合控制词也不应作为 sklearn 模型构造参数传入。
    """
    plan = canonical.get("experiment_plan")
    if not isinstance(plan, dict):
        return

    metrics = plan.get("metrics")
    if isinstance(metrics, list):
        normalized: list[str] = []
        changes: list[str] = []
        for item in metrics:
            original = _as_text(item)
            target = _AGGREGATE_METRIC_ALIASES.get(original.casefold(), original)
            if target != original:
                changes.append(f"{original}->{target}")
            if target and target not in normalized:
                normalized.append(target)
        plan["metrics"] = normalized
        if changes:
            report.warn(
                "experiment_plan.metrics",
                "聚合展示名投影为逐次运行指标并去重: " + ", ".join(changes),
                "mean/std/range 由模块三在多种子真实结果上统一计算",
            )

    def replace_metric_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        result = value
        for source, target in sorted(
            _AGGREGATE_METRIC_ALIASES.items(), key=lambda item: -len(item[0])
        ):
            result = result.replace(source, target)
        return result

    for baseline in plan.get("baselines", []):
        if isinstance(baseline, dict):
            baseline["metric_name"] = replace_metric_text(baseline.get("metric_name"))
    for key in ("objectives", "expected_results", "success_criteria"):
        if isinstance(plan.get(key), list):
            plan[key] = [replace_metric_text(item) for item in plan[key]]

    method = canonical.get("method_design")
    if isinstance(method, dict):
        for item in method.get("innovation_points", []):
            if isinstance(item, dict):
                item["evidence_metric"] = replace_metric_text(item.get("evidence_metric"))

    matrix = plan.get("experiment_matrix")
    if not isinstance(matrix, list):
        return
    test_sizes: set[float] = set()
    removed: dict[str, int] = {}
    cleaned_matrix: list[Any] = []
    for row in matrix:
        if not isinstance(row, list):
            cleaned_matrix.append(row)
            continue
        cleaned = []
        for cell in row:
            text = _as_text(cell)
            if "=" not in text:
                cleaned.append(cell)
                continue
            key, raw_value = text.split("=", 1)
            key = key.strip().casefold()
            if key not in _MATRIX_CONTROL_PARAMETERS:
                cleaned.append(cell)
                continue
            removed[key] = removed.get(key, 0) + 1
            if key == "test_size":
                try:
                    value = float(raw_value.strip())
                    if 0.0 < value < 1.0:
                        test_sizes.add(value)
                except ValueError:
                    pass
        if len(cleaned) == 3:
            cleaned.append("variant=standard")
            report.warn(
                "experiment_plan.experiment_matrix",
                "移除控制参数后补充 variant=standard 保持模块三列以上契约",
            )
        cleaned_matrix.append(cleaned)
    plan["experiment_matrix"] = cleaned_matrix
    if removed:
        report.warn(
            "experiment_plan.experiment_matrix",
            "移除运行控制参数，避免误传给模型构造器: "
            + ", ".join(f"{key}={count}项" for key, count in sorted(removed.items())),
            "聚合/计时由模块三负责；分类执行器依据明确 label 自动分层",
        )
    if len(test_sizes) == 1:
        data_plan = canonical.get("data_plan")
        if isinstance(data_plan, dict):
            split = data_plan.get("split_strategy")
            if isinstance(split, dict) and "test" not in split and "test_size" not in split:
                value = next(iter(test_sizes))
                split["test"] = value
                report.warn(
                    "data_plan.split_strategy.test",
                    f"从矩阵唯一明确的 test_size 无损迁移 {value}",
                )


def compute_run_dir(
    stage3_dir: Path,
    base_dir: Path,
    run_id: str | None = None,
) -> str:
    """算出模块三要的相对 run_dir。

    模块三 `resolve_run_dir(run_dir, base_dir)` 把相对路径按 base_dir 解析，而
    `_validate_relative_path` 禁止绝对路径 / 盘符 / `..`。所以 stage3 目录必须在
    base_dir 之内——C2 走 SDK 时 base_dir 就是 agent 进程的 cwd（项目根）。
    """
    # New requests are content-addressed.  Keeping the legacy default when no
    # run_id is supplied preserves callers that only use this helper for path
    # validation, while production always passes the projected run_id.
    target = stage3_dir / "runs" / run_id if run_id else stage3_dir / "run"
    relative = Path(os.path.relpath(target.resolve(), base_dir.resolve()))
    posix = relative.as_posix()
    if posix.startswith("..") or relative.is_absolute() or relative.drive:
        raise ValueError(
            f"stage3 运行目录必须落在可信工作区内: {stage3_dir} 不在 {base_dir} 之下"
            f"（算出的相对路径 {posix!r} 会被模块三 _validate_relative_path 拒绝）"
        )
    return posix


def project_payloads(
    planning_dir: Path,
    *,
    run_dir: str,
    module3: _Module3 | None = None,
) -> tuple[dict[str, Any], ProjectionReport]:
    """stage2 六份产物 → 模块三 canonical request dict（未校验）+ 审计报告。"""
    module3 = module3 or _load_module3()
    report = ProjectionReport()
    sources = _read_sources(planning_dir)

    raw_plan = sources.get("experiment_plan")
    raw_plan = raw_plan if isinstance(raw_plan, dict) else {}
    metrics = [
        _as_text(m) for m in raw_plan.get("metrics", []) if _as_text(m)
    ]
    # 模块三 planned_ids = primary_experiments ∪ 矩阵首列
    known_ids = [_as_text(i) for i in raw_plan.get("primary_experiments", []) if _as_text(i)]
    for row in raw_plan.get("experiment_matrix", []):
        if isinstance(row, list) and row and _as_text(row[0]) not in known_ids:
            known_ids.append(_as_text(row[0]))

    execution_config = _project_execution_config(
        sources.get("execution_config"), module3, run_dir, report
    )
    seeds = [s for s in execution_config.get("seeds", []) if isinstance(s, int)]

    datasets = _project_datasets(
        raw_plan.get("datasets"), module3, "experiment_plan.datasets", report
    )

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "method_design": _project_method_design(
            sources.get("method_design"), module3, metrics, known_ids, report
        ),
        "experiment_plan": _project_experiment_plan(raw_plan, module3, datasets, report),
        "data_plan": _project_data_plan(
            sources.get("data_plan"),
            module3,
            datasets,
            seeds,
            report,
            raw_plan_datasets=raw_plan.get("datasets"),
            plan_metrics=raw_plan.get("metrics"),
        ),
        "domain": _project_domain(sources.get("domain"), module3, report),
        "resource_constraints": _project_resource_constraints(
            sources.get("resource_constraints"), module3, report
        ),
        "execution_config": execution_config,
    }
    _reconcile_explicit_primary_and_seeds(
        canonical,
        research_question=sources.get("research_question"),
        raw_method_design=sources.get("method_design"),
        report=report,
    )
    _normalize_runtime_metrics_and_controls(canonical, report)
    # run_id 用模块三同一套 sha256 算法，但对**投影后**的 payload 重算
    canonical["run_id"] = module3.stable_run_id({
        "method_design": canonical["method_design"],
        "experiment_plan": canonical["experiment_plan"],
        "data_plan": canonical["data_plan"],
        "domain": canonical["domain"],
        "resource_constraints": canonical["resource_constraints"],
        "execution_config": canonical["execution_config"],
    })
    return canonical, report


def _sweep_matrix(canonical: dict[str, Any], report: ProjectionReport) -> None:
    """一次性枚举全部矩阵违规（模块三只会抛第一条 cross-ref 错）。"""
    checker = _load_matrix_checker()
    if checker is None:
        return
    plan = canonical.get("experiment_plan")
    if not isinstance(plan, dict):
        return
    for err in checker(plan):
        # 模块二 validate_plan.ValidationError: .path / .message
        field_path = getattr(err, "path", None) or "experiment_matrix"
        message = getattr(err, "message", None) or str(err)
        report.error(f"experiment_plan.{field_path}", "矩阵不符合模块三行格式", message)


def write_stage3_inputs(
    planning_dir: Path,
    stage3_dir: Path,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """把 stage2 产物投影成模块三严格输入，落盘 request.json + manifest + 审计报告。

    Returns:
        dict: ``{"status": "ready"|"invalid", "request_path", "manifest_path",
                 "run_dir", "run_id", "errors", "warnings", "report_path"}``

    status == "invalid" 时 request.json 仍会落盘（方便人工比对/修补），但不 scaffold
    manifest —— 因为 `scaffold_manifest()` 吃的是已验证的 request model。
    """
    planning_dir = planning_dir.resolve()
    stage3_dir = stage3_dir.resolve()
    base_dir = (base_dir or Path.cwd()).resolve()
    stage3_dir.mkdir(parents=True, exist_ok=True)

    module3 = _load_module3()
    # First projection uses a harmless placeholder.  The module-three identity
    # hash deliberately excludes execution_config.run_dir, so after deriving
    # the run_id we can give this run its own stable directory without a hash
    # cycle.  Same content -> same run_id/path (resume); changed content -> a
    # different path (no collision with the previous state).
    canonical, report = project_payloads(
        planning_dir,
        run_dir=compute_run_dir(stage3_dir, base_dir),
        module3=module3,
    )
    run_id = str(canonical["run_id"])
    run_dir_rel = compute_run_dir(stage3_dir, base_dir, run_id=run_id)
    canonical["execution_config"]["run_dir"] = run_dir_rel
    verified_run_id = module3.stable_run_id({
        "method_design": canonical["method_design"],
        "experiment_plan": canonical["experiment_plan"],
        "data_plan": canonical["data_plan"],
        "domain": canonical["domain"],
        "resource_constraints": canonical["resource_constraints"],
        "execution_config": canonical["execution_config"],
    })
    if verified_run_id != run_id:
        raise RuntimeError("run_id must not depend on execution_config.run_dir")

    request_path = stage3_dir / "request.json"
    request_path.write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 矩阵漂移一次性摊开（在严格校验之前，保证即使 model_validate 早退也有完整清单）
    _sweep_matrix(canonical, report)

    manifest_path = base_dir / run_dir_rel / MANIFEST_NAME
    status = "invalid"
    try:
        request = module3.request_model.model_validate(canonical)
    except Exception as exc:  # pydantic ValidationError + model_validator 里的 ValueError
        errors = getattr(exc, "errors", None)
        if callable(errors):
            for item in exc.errors(include_url=False):
                loc = ".".join(str(p) for p in item["loc"])
                report.error(loc or "<root>", "模块三契约校验失败", item["msg"])
        else:
            report.error("<root>", "模块三契约校验失败", str(exc))
    else:
        status = "ready"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        module3.scaffold_manifest(request, manifest_path, overwrite=True)

    report_payload = {
        "status": status,
        "planning_dir": str(planning_dir),
        "stage3_dir": str(stage3_dir),
        "base_dir": str(base_dir),
        "run_dir": run_dir_rel,
        "run_id": canonical.get("run_id"),
        "request_path": str(request_path),
        "manifest_path": str(manifest_path) if status == "ready" else None,
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "notes": report.notes,
    }
    report_path = stage3_dir / "projection_report.json"
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.info(
        "stage3 投影 %s: %d warn / %d error → %s",
        status, len(report.warnings), len(report.errors), request_path,
    )
    for line in report.summary_lines():
        (log.error if line.startswith("[error]") else log.info)("  %s", line)

    return {
        "status": status,
        "request_path": str(request_path),
        "manifest_path": str(manifest_path) if status == "ready" else None,
        "report_path": str(report_path),
        "run_dir": run_dir_rel,
        "run_id": canonical.get("run_id"),
        "errors": [_note_line(n) for n in report.errors],
        "warnings": [_note_line(n) for n in report.warnings],
    }


# ── CLI（确定性验证用；不接 LLM）──


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="stage2 planning 产物 → 模块三严格输入投影")
    parser.add_argument("--planning-dir", required=True, type=Path)
    parser.add_argument("--stage3-dir", required=True, type=Path)
    parser.add_argument(
        "--base-dir", type=Path, default=None,
        help="可信工作区根（run_dir 相对于它解析）；默认 cwd",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = write_stage3_inputs(
        args.planning_dir, args.stage3_dir, base_dir=args.base_dir
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
