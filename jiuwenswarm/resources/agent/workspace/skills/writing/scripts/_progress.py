# -*- coding: utf-8 -*-
"""
_progress.py — writing-skill 实时进度文件写盘（paper-gen 协议，2026-09-07 引入）

writing-skill 子进程通过 facade.phase() 切换 8 个阶段时，main.py 的 progress_sink
监听到 PHASE 事件后写入 progress.json，供 paper-gen stage_runner 500ms mtime 轮询，
mtime 变化时把 phase chunk 推到 TUI 任务列表第 4 行（writing）。

设计：与 planning/scripts/_progress.py 同构（原子写盘 + 失败不 raise + schema 稳定）。

progress.json schema::

    {
        "current_phase": "读输入" | "初稿生成" | "Part2 评审"
                       | "Part1+2 修订" | "Part3 Abstract" | "Part4 References"
                       | "LaTeX 组装" | "PDF 编译" | "preflight" | "load_inputs",
        "index": int,             # current_phase 在 phase_order 里的位置
        "total": 8,               # phase_order 长度（PDF 编译也算 1 个）
        "phase_order": [str, ...],# 完整阶段顺序（前端用此渲染任务列表）
        "status": "running" | "complete" | "partial" | "aborted" | "error",
        "started_at": "<iso8601>",# 第一次写入的时间
        "updated_at": "<iso8601>",# 最近一次写入的时间
        "elapsed_seconds": float  # updated_at - started_at（粗略 wall-time）
    }

paper-gen stage_runner 读 progress.json：
    - 轮询 mtime，每 500ms 一次；变化时 read + 转发为 ``[writing progress] ...`` 日志
    - progress.json 不存在 → 阶段未开始（preflight 失败 / 子进程崩 / 老 skill 没装 sink）
    - 最后一帧 status="complete" + index=total-1 → 全部 8 phase 都跑完
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# 模块级 Python logger（writing.main 入口会 setup_writing_logging）
log = logging.getLogger("writing.progress")


# Evidence-first writing stages.  Visual planning/review are first-class
# resumable phases rather than hidden work inside prose generation.
PHASE_ORDER: tuple[str, ...] = (
    "输入清点", "图表能力清点", "图表规划", "图表生成", "图表审阅", "图表定点修订",
    "分节写作", "整体审阅", "局部修订", "标题与摘要", "受控渲染", "Tectonic 编译",
)

# 合法 status 值
_VALID_PROGRESS_STATUS = {"running", "complete", "partial", "aborted", "error"}


def write_progress_file(
    path: str | Path,
    current_phase: str,
    index: int,
    total: int,
    phase_order: list[str] | tuple[str, ...] | None = None,
    status: str = "running",
    started_at: str | None = None,
    updated_at: str | None = None,
    elapsed_seconds: float | None = None,
) -> bool:
    """把当前 phase 状态写到 progress.json（原子写盘）。

    Args:
        path:            progress.json 落盘路径（paper-gen 默认指向 <output_dir>/progress.json）
        current_phase:   当前 phase 名称（如 "初稿生成"）
        index:           current_phase 在 phase_order 里的位置（0-based）
        total:           phase_order 长度（通常 8）
        phase_order:     完整阶段顺序；None 时用本模块的 ``PHASE_ORDER`` 常量
        status:          "running" / "complete" / "partial" / "aborted" / "error"
        started_at:      ISO8601 字符串；None 时用当前时间
        updated_at:      ISO8601 字符串；None 时用当前时间
        elapsed_seconds: 写入时刻的 wall-time；None 时用 started_at/updated_at 推算

    Returns:
        True=写盘成功，False=写盘失败（不抛异常）。
    """
    try:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if started_at is None:
            started_at = _iso8601(now)
        if updated_at is None:
            updated_at = _iso8601(now)
        if elapsed_seconds is None:
            try:
                elapsed_seconds = round(
                    _parse_iso8601(updated_at) - _parse_iso8601(started_at), 3
                )
            except Exception:
                elapsed_seconds = 0.0
        order = list(phase_order) if phase_order is not None else list(PHASE_ORDER)
        # index 越界保护（防御性：阶段名拼错时不要让 paper-gen 读出 -1）
        safe_index = max(0, min(int(index), max(0, len(order) - 1))) if order else 0
        payload: dict[str, Any] = {
            "current_phase": str(current_phase),
            "index": safe_index,
            "total": len(order) or int(total),
            "phase_order": order,
            "status": status if status in _VALID_PROGRESS_STATUS else "running",
            "started_at": started_at,
            "updated_at": updated_at,
            "elapsed_seconds": round(float(elapsed_seconds), 3),
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
        # 写盘失败不杀 subprocess——返回 False，main.py 继续走
        log.warning(f"[_progress.write_progress_file] 写盘失败: {e}")
        return False


def read_progress_file(path: str | Path) -> dict | None:
    """读 progress.json（paper-gen stage_runner 用：本文件被 main.py 写，被 stage_runner 读）。

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


def progress_mtime(path: str | Path) -> float | None:
    """取 progress.json 的 mtime（paper-gen 轮询用——比读全文便宜）。

    Returns:
        mtime 浮点数；文件不存在 / stat 失败返 None。
    """
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


# ──────────────────── helpers ────────────────────


def _iso8601(t: float) -> str:
    """unix 时间戳 → ISO8601 字符串（带本地时区偏移，Z 结尾）。"""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat()


def _parse_iso8601(s: str) -> float:
    """ISO8601 字符串 → unix 时间戳（容忍 "Z" 结尾和时区偏移）。"""
    import datetime as _dt
    cleaned = s.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(cleaned).timestamp()
