# -*- coding: utf-8 -*-
"""
_status.py — planning-skill 状态文件写盘（plan-supervisor 协议）

2026-08-31 引入：planning-skill 子进程收尾时把运行状态写到 ``--status-file`` 指定的路径，
供 agent 端的 `call_planning_skill` tool 读取（替代旧版"按退出码猜状态"）。

设计要点：
    - **原子写盘**（tmp → os.replace），避免 agent 读到半截文件
    - **schema 稳定**：agent 端 fallback 路径也认这个 schema
    - **不阻塞主流程**：写盘失败时返 warning 而不是 raise（不杀 subprocess）
    - **向后兼容**：旧 main() 退出码映射也保留在 main.py，agent 端 fallback 仍可用

status.json schema (TODO §5.4)：
    {
        "status": "complete" | "error" | "preflight_failed" | "load_inputs_failed"
                  | "replan_schema_blocked" | "aborted" (不再用，保留字段以防 agent 端读到旧值)
        "artifacts": [str, ...],                 // 写盘的产物路径（绝对路径）
        "errors": [str, ...],
        "warnings": [str, ...],
        "check_feasibility": {                   // 门禁校验结果
            "passed": bool,
            "downgraded_to": "TIER_FULL" | "TIER_TRIM" | "TIER_CORE" | None,
            "issues": [str, ...]
        },
        "method_rounds_used": int,               // method_designer ↔ method_critic 实际轮数
        "wall_time_seconds": float,              // 子进程总耗时
        "tier": 0 | 1 | 2,                       // 算力档位（与 check_feasibility.downgraded_to 联动）
        "checkpoint_status": {                   // 4 checkpoint 状态（agent 端展示用）
            "method_design":     "passed" | "skipped" | "needs_replan",
            "experiment_plan":   "passed" | "skipped" | "needs_replan",
            "tier_downgrade":    "passed" | "skipped" | "needs_replan",
            "execution_config":  "passed" | "skipped" | "needs_replan"
        }
    }

agent 端用 `call_planning_skill` 读 status.json：
    - status == "complete" + 4 checkpoint 全 passed → 4 stage 跑完，可做 4 人审
    - status == "complete" + 部分 checkpoint "skipped" → REPLAN_DATA 路径
    - status == "error" / "preflight_failed" / ... → 让用户看 errors 决定 retry / abort
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


# status 合法值（与 main.py 退出码 + planning_flow.py 返回值对齐）
_VALID_STATUS = {
    "complete",                  # 4 stage 全跑完且 4 checkpoint 都过（agent 模式下 checkpoint 都在 agent 端）
    "error",                     # 通用错误
    "preflight_failed",          # preflight critical 失败
    "preflight_error",           # preflight 异常
    "load_inputs_failed",        # load_inputs 校验失败
    "replan_schema_blocked",     # REPLAN 反馈的 category=schema 触发阻塞
    "aborted",                   # 旧协议，agent 模式不再用（保留以防 agent 端读到旧值兜底）
}

# checkpoint 合法值
_VALID_CHECKPOINT = {"passed", "skipped", "needs_replan"}

# 算力档位（与 scripts/estimate_compute.py 镜像）
_TIER_NAMES = {0: "TIER_FULL", 1: "TIER_TRIM", 2: "TIER_CORE"}


def write_status_file(
    path: str | Path,
    status: str,
    *,
    artifacts: list[str] | None = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    check_feasibility: dict | None = None,
    method_rounds_used: int = 0,
    wall_time_seconds: float | None = None,
    tier: int = 0,
    checkpoint_status: dict | None = None,
) -> bool:
    """把 planning-skill 状态写到 status.json（原子写盘）。

    Args:
        path:                 status.json 落盘路径（agent 默认指向 <output_dir>/status.json）
        status:               主状态枚举（见 _VALID_STATUS）
        artifacts:            写盘的产物绝对路径列表
        errors:               错误信息列表
        warnings:             警告信息列表
        check_feasibility:    门禁校验结果 dict（含 passed / downgraded_to / issues）
        method_rounds_used:   method_designer ↔ method_critic 反思实际轮数
        wall_time_seconds:    子进程总耗时（None 时用当前 monotonic 计算）
        tier:                 算力档位 0/1/2
        checkpoint_status:    4 checkpoint 状态 dict

    Returns:
        True=写盘成功，False=写盘失败（不抛异常）。
    """
    try:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if wall_time_seconds is None:
            # 兜底：写盘时算一次
            try:
                import time as _t
                wall_time_seconds = round(_t.monotonic(), 2)
            except Exception:
                wall_time_seconds = 0.0
        payload: dict[str, Any] = {
            "status": status if status in _VALID_STATUS else "error",
            "artifacts": list(artifacts or []),
            "errors": list(errors or []),
            "warnings": list(warnings or []),
            "check_feasibility": _normalize_check_feasibility(check_feasibility, tier),
            "method_rounds_used": int(method_rounds_used or 0),
            "wall_time_seconds": round(float(wall_time_seconds), 2),
            "tier": int(tier),
            "checkpoint_status": _normalize_checkpoint_status(checkpoint_status),
        }
        # 原子写盘（tmp + os.replace）
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(out_path))
        return True
    except Exception as e:  # noqa: BLE001
        # 写盘失败不杀 subprocess——返回 False，main.py 走 fallback 退出码路径
        import sys
        print(f"[_status.write_status_file] 写盘失败: {e}", file=sys.stderr)
        return False


def _normalize_check_feasibility(cf: dict | None, tier: int) -> dict:
    """归一化 check_feasibility 字段。"""
    cf = cf or {}
    downgraded_to = cf.get("downgraded_to")
    if downgraded_to is None and tier in _TIER_NAMES:
        downgraded_to = _TIER_NAMES[tier] if tier > 0 else None
    return {
        "passed": bool(cf.get("passed", tier == 0)),
        "downgraded_to": downgraded_to,
        "issues": list(cf.get("issues") or []),
    }


def _normalize_checkpoint_status(cps: dict | None) -> dict:
    """归一化 4 checkpoint 状态。"""
    cps = cps or {}
    return {
        "method_design":    cps.get("method_design", "skipped") if cps.get("method_design") in _VALID_CHECKPOINT else "skipped",
        "experiment_plan":  cps.get("experiment_plan", "skipped") if cps.get("experiment_plan") in _VALID_CHECKPOINT else "skipped",
        "tier_downgrade":   cps.get("tier_downgrade", "skipped") if cps.get("tier_downgrade") in _VALID_CHECKPOINT else "skipped",
        "execution_config": cps.get("execution_config", "skipped") if cps.get("execution_config") in _VALID_CHECKPOINT else "skipped",
    }


def read_status_file(path: str | Path) -> dict | None:
    """读 status.json（agent 端 fallback 用——本文件主要被 main.py 写、agent tool 读）。

    Returns:
        解析后的 dict；读不到 / 解析失败返 None。
    """
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
                    