# -*- coding: utf-8 -*-
"""
_experiment_diff.py — 反馈采纳度硬兜底（脚本 diff，2026-08-29 新增）

C-③ 决策：反馈改进检查 = LLM diff + 脚本 diff 都做。
- LLM diff：method-critic / experiment-critic 在 feedback_addressed[] 自查
- 脚本 diff：本文件提供的 script_diff_check，**防止 LLM 自查走过场**——
  即使 LLM 自查说"已采纳"，脚本对比值没变 → 强制重写。

设计要点：
1. field_path 语法：点号 + 方括号索引（与 LLM 输出对齐）
   - "baselines" → 顶层 list
   - "baselines[2]" → 顶层 list 第 3 个元素
   - "baselines[2].paper_id" → 嵌套字段
2. 比对规则：
   - 上一轮 prev 值 == 本轮 curr 值 → unchanged（视为未采纳）
   - 本轮 curr 值为 None/空 → missing（视为未补）
   - 本轮 curr 值与 prev 不同 → addressed
3. 用途：
   - iterate_method_design.py — 检查 method_design 反思循环
   - plan_experiment_with_data.py — 检查 experiment_plan 反思循环
"""

from __future__ import annotations

import re
from typing import Any


def _get_field_value(payload: Any, field_path: str) -> Any:
    """按路径取 payload 字段值。

    支持语法:
        - "baselines"                 → 顶层 list / dict
        - "baselines[2]"              → 顶层 list 第 3 个元素
        - "baselines[2].paper_id"     → 嵌套字段
        - "components[0].function"    → 同上

    Args:
        payload: dict / list（顶层若是 list 则先取 [0] 再走路径）
        field_path: 字段路径字符串

    Returns:
        字段值；路径不通 → None
    """
    if payload is None:
        return None
    # 拆 path: 用 \w+|\[\d+\] 分段
    parts = re.findall(r"\w+|\[\d+\]", field_path)
    cur: Any = payload
    for part in parts:
        if cur is None:
            return None
        if part.startswith("["):
            idx = int(part[1:-1])
            if isinstance(cur, list) and -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
    return cur


def script_diff_check(
    previous: dict | None,
    current: dict | None,
    expected_field_paths: list[str],
) -> dict:
    """硬兜底：检查 expected_field_paths 字段在 previous vs current 中是否真变化。

    即使 LLM 自查说"已采纳"，脚本对比值没变 → 强制重写（防走过场）。

    Args:
        previous: 上一轮产物 dict；None 时所有 path 标 missing（首轮）
        current: 本轮产物 dict；None 时所有 path 标 missing（异常）
        expected_field_paths: 待检查的字段路径列表（如 ["baselines", "baselines[2]"]）

    Returns:
        {
            "addressed": bool,           # 全部采纳
            "unchanged": [str, ...],     # prev == curr 的 path 列表
            "missing": [str, ...],       # curr 为 None / 异常的 path 列表
        }
    """
    if not expected_field_paths:
        return {"addressed": True, "unchanged": [], "missing": []}
    if current is None:
        return {
            "addressed": False,
            "unchanged": [],
            "missing": [f"{p} 当前值为 None" for p in expected_field_paths],
        }
    unchanged: list[str] = []
    missing: list[str] = []
    for path in expected_field_paths:
        prev_val = _get_field_value(previous or {}, path)
        curr_val = _get_field_value(current, path)
        if curr_val is None or curr_val == "" or curr_val == [] or curr_val == {}:
            missing.append(f"{path} 当前为空（prev={prev_val!r}, curr={curr_val!r}）")
        elif previous is None:
            # 首轮：prev 缺失，curr 非空 → 当作采纳（无从对比）
            continue
        elif prev_val == curr_val:
            unchanged.append(f"{path} 未变化（prev == curr == {curr_val!r}）")
    return {
        "addressed": not unchanged and not missing,
        "unchanged": unchanged,
        "missing": missing,
    }


def extract_field_paths_from_addressed(feedback_addressed: list[dict] | None) -> list[str]:
    """从 feedback_addressed[] 抽 field_path 列表（去 None + 去重保序）。

    用途：把 LLM 自查的 field_path 喂给 script_diff_check 做硬兜底。

    Args:
        feedback_addressed: critic 返回的 feedback_addressed 列表

    Returns:
        字段路径列表（保序，已去重）
    """
    if not isinstance(feedback_addressed, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in feedback_addressed:
        if not isinstance(item, dict):
            continue
        fp = item.get("field_path")
        if not isinstance(fp, str) or not fp.strip():
            continue
        if fp in seen:
            continue
        seen.add(fp)
        out.append(fp)
    return out
