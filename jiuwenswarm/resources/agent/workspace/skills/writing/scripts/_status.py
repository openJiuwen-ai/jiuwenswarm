# -*- coding: utf-8 -*-
"""
_status.py — writing-skill 状态文件写盘（paper-gen 协议，2026-09-07 引入）

writing-skill 子进程收尾时把运行状态写到 ``--status-file`` 指定的路径，
供 paper-gen stage_runner 读 status.json 决定终态（complete / partial / error / aborted）。

设计要点：
    - **原子写盘**（tmp → os.replace），避免 paper-gen 读到半截文件
    - **schema 稳定**：status 值集固定，paper-gen 读不到合法值会 fallback
    - **不阻塞主流程**：写盘失败时返 warning 而不是 raise（不杀 subprocess）
    - **writing 自己的 status 值**（success/partial/failed）由 main.py 写盘前映射到
      paper-gen 期望的 _VALID_STATUS 集

status.json schema::

    {
        "status": "complete" | "partial" | "error"
                  | "preflight_failed" | "load_inputs_failed" | "aborted",
        "artifacts": [str, ...],                 // 写盘的产物绝对路径（paper.pdf/.tex/.json 等）
        "errors": [str, ...],
        "warnings": [str, ...],
        "wall_time_seconds": float,              // 子进程总耗时
        "revision_rounds_used": int,             // 反思循环实际轮数（0 = 一次过）
        "pdf_path": str,                         // 最终 paper.pdf 绝对路径（partial 时可能为空）
        "verdict": "pass" | "revise" | "revise_exhausted" | "aborted",
                                                // 反思循环终态（前端可读）
    }

paper-gen stage_runner 读 status.json 后还会重新执行正式发布门禁：
    - 只有 status == "complete"、verdict == "pass"，且 PDF、证据图审计、
      四项专项审查、终稿审查均通过时，顶层 run_summary 才标 complete
    - status == "partial" 只保留诊断/修订产物；顶层映射为
      blocked_writing_release，退出码 1，不构成正式论文交付
    - status in ("error", "preflight_failed", "load_inputs_failed", "aborted")
      → 整个 paper-gen 标 aborted_writing，退出码 1
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# 模块级 Python logger（writing.main 入口会 setup_writing_logging）
log = logging.getLogger("writing.status")


# status 合法值（与 main.py 退出码 + writing_flow.run() 返回值对齐）
# 注意：writing 自己的 success/partial/failed 在 main.py 写盘前已映射成 complete/partial/error
_VALID_STATUS = {
    "complete",                  # 4 sub-agent 反思循环通过 + paper.pdf 成功落盘
    "partial",                   # 反思循环 exhausted 或 LaTeX 编译三档全败但 .tex 落盘
    "error",                     # 通用错误
    "preflight_failed",          # preflight critical 失败
    "preflight_error",           # preflight 异常
    "load_inputs_failed",        # stage1_load_inputs 校验失败（m1/m2/m3 schema 不符）
    "aborted",                   # human() 检查点选 abort
}

# verdict 合法值（writing_flow.py:WRITING_PASS / WRITING_REVISE / 等常量映射）
_VALID_VERDICT = {
    "pass", "revise", "revise_exhausted", "aborted", "unknown",
    "invalid_execution_evidence", "controlled_audit_failed",
    "frontmatter_layout_failed", "pdf_validation_failed",
    "revision_no_progress", "resume_specialist_review_missing",
    "section_quality_exhausted", "writer_protocol_failed",
}


def write_status_file(
    path: str | Path,
    status: str,
    *,
    artifacts: list[str] | None = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    wall_time_seconds: float | None = None,
    revision_rounds_used: int = 0,
    pdf_path: str | None = None,
    verdict: str | None = None,
    llm_token_usage: dict | None = None,
) -> bool:
    """把 writing-skill 状态写到 status.json（原子写盘）。

    Args:
        path:                 status.json 落盘路径（paper-gen 默认指向 <output_dir>/status.json）
        status:               主状态枚举（见 _VALID_STATUS）
        artifacts:            写盘的产物绝对路径列表
        errors:               错误信息列表
        warnings:             警告信息列表
        wall_time_seconds:    子进程总耗时（None 时用当前 monotonic 计算）
        revision_rounds_used: 反思循环实际轮数（0 = reviewer 一次过，无需 revise）
        pdf_path:             最终 paper.pdf 绝对路径（partial 时可能为 None 或空串）
        verdict:              反思循环终态 verdict（"pass" / "revise_exhausted" / ...）

    Returns:
        True=写盘成功，False=写盘失败（不抛异常）。
    """
    try:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if wall_time_seconds is None:
            # 兜底：写盘时算一次
            try:
                wall_time_seconds = round(time.monotonic(), 2)
            except Exception:
                wall_time_seconds = 0.0
        normalized_status = status if status in _VALID_STATUS else "error"
        normalized_verdict = verdict if verdict in _VALID_VERDICT else "unknown"
        payload: dict[str, Any] = {
            "status": normalized_status,
            "artifacts": list(artifacts or []),
            "errors": list(errors or []),
            "warnings": list(warnings or []),
            "wall_time_seconds": round(float(wall_time_seconds), 2),
            "revision_rounds_used": int(revision_rounds_used or 0),
            "pdf_path": str(pdf_path) if pdf_path else "",
            "verdict": normalized_verdict,
            "llm_token_usage": dict(llm_token_usage or {}),
        }
        # 原子写盘（tmp + os.replace）
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(out_path))
        log.info(f"[_status.write_status_file] status.json 写盘成功: "
                 f"status={normalized_status}, verdict={normalized_verdict}, "
                 f"artifacts={len(payload['artifacts'])}, "
                 f"wall={payload['wall_time_seconds']}s")
        return True
    except Exception as e:  # noqa: BLE001
        # 写盘失败不杀 subprocess——返回 False，main.py 走 fallback 退出码路径
        log.warning(f"[_status.write_status_file] 写盘失败: {e}")
        return False


def read_status_file(path: str | Path) -> dict | None:
    """读 status.json（paper-gen stage_runner 用——本文件被 main.py 写、stage_runner 读）。

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
