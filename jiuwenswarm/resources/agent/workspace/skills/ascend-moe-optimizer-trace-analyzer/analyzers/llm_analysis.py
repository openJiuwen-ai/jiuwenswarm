from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class LLMPromptInput:
    overview: dict
    diagnosis: dict
    phase_summary: List[dict]
    category_summary: List[dict]
    core_group_summary: List[dict]
    phase_core_group_summary: List[dict]
    category_core_group_summary: List[dict]
    name_summary: List[dict]
    overlap_summary: List[dict]
    bubble_summary: List[dict]
    top_n: int = 12


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if abs(value) >= 100:
            return f"{value:.3f}".rstrip("0").rstrip(".")
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _table(rows: List[dict], columns: List[str], limit: int = 12) -> str:
    if not rows:
        return "No data."
    selected = rows[:limit]
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_fmt(row.get(column)) for column in columns) + " |"
        for row in selected
    ]
    return "\n".join([header, sep] + body)


def build_llm_prompt(inp: LLMPromptInput) -> str:
    overview = inp.overview
    diagnosis = inp.diagnosis
    phase_summary = inp.phase_summary
    category_summary = inp.category_summary
    core_group_summary = inp.core_group_summary
    phase_core_group_summary = inp.phase_core_group_summary
    category_core_group_summary = inp.category_core_group_summary
    name_summary = inp.name_summary
    overlap_summary = inp.overlap_summary
    bubble_summary = inp.bubble_summary
    top_n = inp.top_n
    findings = diagnosis.get("findings", [])
    finding_lines = []
    for finding in findings:
        finding_lines.append(
            f"- [{finding.get('severity')}] {finding.get('title')}: "
            f"{finding.get('detail')} Action: {finding.get('recommendation')}"
        )

    return "\n\n".join(
        [
            "# Task",
            (
                "你是算子 trace 性能分析专家。请基于下面统计表写一段 LLM Analysis，"
                "用于追加到 report.md。请不要复述所有表格，重点给出 3-5 条有证据的判断和下一步排查建议。"
            ),
            "# Rules",
            "\n".join(
                [
                    "- 只能依据给出的统计数据，不要虚构 trace 中没有的信息。",
                    "- 明确区分 union_us 和 total_us：union_us 更接近 wall time 覆盖，total_us 会重复累计并行核。",
                    "- 关注 core group、tid、phase、category 和 raw name 的分布；如果出现 cube/vector_recv/vector_send，请按核组解释。",
                    "- 如果 phase/category 语义明显依赖某个算子配置，请说明适用边界。",
                    "- 如果结论不确定，请写“可能”并说明需要继续看哪张表或哪部分源码。",
                    "- 输出中文，使用简洁小标题和项目符号。",
                ]
            ),
            "# Overview",
            _table(
                [overview],
                [
                    "num_instances",
                    "num_phases",
                    "num_names",
                    "num_pids",
                    "num_tids",
                    "num_core_groups",
                    "core_groups",
                    "total_wall_us",
                ],
                1,
            ),
            "# Deterministic Findings",
            "\n".join(finding_lines) if finding_lines else "No deterministic findings.",
            "# Core Group Summary",
            _table(
                core_group_summary,
                ["core_group", "observed_core_count", "union_us", "ratio_to_total_wall", "total_us"],
                top_n,
            ),
            "# Category By Core Group",
            _table(
                category_core_group_summary,
                [
                    "core_group",
                    "category",
                    "union_us",
                    "ratio_to_core_group_wall",
                    "ratio_to_total_wall",
                    "total_us",
                ],
                top_n * 3,
            ),
            "# Phase By Core Group",
            _table(
                phase_core_group_summary,
                [
                    "core_group",
                    "phase",
                    "category",
                    "union_us",
                    "ratio_to_core_group_wall",
                    "total_us",
                ],
                top_n * 3,
            ),
            "# Phase Summary",
            _table(
                phase_summary,
                ["phase", "category", "count", "union_us", "ratio_to_total_wall", "total_us"],
                top_n,
            ),
            "# Category Summary",
            _table(
                category_summary,
                ["category", "count", "union_us", "ratio_to_total_wall", "total_us"],
                top_n,
            ),
            "# Top Raw Names",
            _table(
                name_summary,
                ["name", "phase", "category", "count", "total_us", "union_us", "tid_nunique"],
                top_n,
            ),
            "# High Overlap Pairs",
            _table(
                overlap_summary,
                ["phase_a", "phase_b", "overlap_us", "overlap_ratio_a", "overlap_ratio_b"],
                top_n,
            ),
            "# Bubble Summary",
            _table(
                bubble_summary,
                ["parent_phase", "bubble_us", "bubble_ratio", "gap_count", "max_gap_us"],
                top_n,
            ),
        ]
    )


def run_llm_command(prompt: str, command: str, timeout_s: int = 120) -> dict:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {
            "status": "error",
            "analysis": "",
            "error": f"Invalid LLM command: {exc}",
        }

    if not argv:
        return {
            "status": "not_configured",
            "analysis": "",
            "error": "Empty LLM command.",
        }

    try:
        result = subprocess.run(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "analysis": "",
            "error": f"LLM command not found: {exc}",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "analysis": "",
            "error": f"LLM command timed out after {timeout_s}s.",
        }

    if result.returncode != 0:
        return {
            "status": "error",
            "analysis": "",
            "error": result.stderr.strip() or f"LLM command failed with code {result.returncode}.",
        }

    return {
        "status": "ok",
        "analysis": result.stdout.strip(),
        "error": "",
    }


def generate_llm_analysis(
    prompt: str,
    enabled: bool = False,
    command: Optional[str] = None,
    timeout_s: int = 120,
) -> dict:
    command = command or os.environ.get("TRACE_ANALYSIS_LLM_CMD")
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "analysis": "",
            "error": "",
        }

    if not command:
        return {
            "enabled": True,
            "status": "not_configured",
            "analysis": "",
            "error": "Set --llm-command or TRACE_ANALYSIS_LLM_CMD to enable LLM analysis.",
        }

    result = run_llm_command(prompt, command, timeout_s=timeout_s)
    result["enabled"] = True
    result["command"] = command
    return result
