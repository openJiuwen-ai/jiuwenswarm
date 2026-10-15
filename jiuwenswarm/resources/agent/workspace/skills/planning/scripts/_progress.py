# -*- coding: utf-8 -*-
"""
_progress.py — planning-skill 实时进度文件写盘（2026-09-05 plan-supervisor 接入）

背景：plan-supervisor 调 planning-skill 子进程时，4 阶段（方法设计 / 实验规划 / 门禁校验
/ 执行配置 / 写产物）共 7+ 分钟无任何输出——用户看不到当前在哪个 stage。planning_flow.py
的 5 个 ``phase(...)`` 调用已经按顺序就位，但 main.py:163 把 ``progress_sink`` 设为 no-op
（``lambda evt: None``），phase 事件发到空气。

设计：新建 ``progress.json``（与 ``status.json`` 同协议：原子写盘 + 失败不 raise + schema
稳定），由 main.py 的 progress_sink 监听到 PHASE 事件后写入；agent 端 ``call_planning_skill``
tool 轮询 progress.json mtime，变化时向 framework yield phase chunk，框架推到 web UI
的 PhaseProgress 组件，渲染成"4 行任务列表 + 状态图标"。

protocol 约定（2026-09-05 plan-supervisor Phase 4 引入）：

    progress.json schema::

        {
            "current_phase": "方法设计" | "实验规划" | "门禁校验" |
                             "执行配置" | "写产物" | "preflight" | "load_inputs",
            "index": int,             # current_phase 在 phase_order 里的位置
            "total": 5,               # phase_order 长度（写产物也算 1 个）
            "phase_order": [str, ...],# 完整阶段顺序（前端用此渲染任务列表）
            "status": "running" | "complete" | "error",
            "started_at": "<iso8601>",# 第一次写入的时间
            "updated_at": "<iso8601>",# 最近一次写入的时间
            "elapsed_seconds": float  # updated_at - started_at（粗略 wall-time）
        }

agent 端用 ``call_planning_skill`` 读 progress.json：
    - 轮询 mtime，每 500ms 一次；变化时 read + 转发为 ``{type: "phase", ...}`` chunk
    - progress.json 不存在 → 阶段未开始（preflight 失败 / 子进程崩 / 老 skill 没装 sink）
    - 最后一帧 status="complete" + index=total-1 → 全部 4 stage + 写产物都跑完
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# 模块级 Python logger（planning.main 入口会 setup_planning_logging）
log = logging.getLogger("planning.progress")


# planning-skill 4 阶段（含收尾的"写产物"，与 planning_flow.py 的 5 个 phase() 调用顺序对齐）
PHASE_ORDER: tuple[str, ...] = (
    "方法设计",
    "实验规划",
    "门禁校验",
    "执行配置",
    "写产物",
)

# 合法 status 值
_VALID_PROGRESS_STATUS = {"running", "complete", "error"}


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
        path:            progress.json 落盘路径（agent 默认指向 <output_dir>/progress.json）
        current_phase:   当前 phase 名称（如 "方法设计"）
        index:           current_phase 在 phase_order 里的位置（0-based）
        total:           phase_order 长度（通常 5）
        phase_order:     完整阶段顺序；None 时用本模块的 ``PHASE_ORDER`` 常量
        status:          "running" / "complete" / "error"
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
        # index 越界保护（防御性：阶段名拼错时不要让 agent 读出 -1）
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
    """读 progress.json（agent 端用：本文件被 main.py 写，被 call_planning_skill 读）。

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
    """取 progress.json 的 mtime（agent 端轮询用——比读全文便宜）。

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
