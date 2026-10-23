# -*- coding: utf-8 -*-
"""数学推理平台桥接工具：把题目转交本机部署的数学推理平台执行并回传结构化结果。

平台四模块（backend 决定走哪一个）：
- nl      自然语言求解（快，分钟内）
- formal  Lean4 形式化证明（准，5-30 分钟）
- opt     优化建模求解（快，分钟级）
- paper   数模竞赛论文全流程（完整，小时级，异步两段式）

环境变量（可选，默认值适配本机部署）：
- MATH_PLATFORM_ROOT   平台根目录
- MATH_PLATFORM_PYTHON 平台解释器（平台代码按 Python 3.10 编写）
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    from openjiuwen.core.foundation.tool.base import Tool, ToolCard
except ImportError:  # 独立调试（无 jiuwenswarm 运行时）时的最小桩
    class ToolCard:  # type: ignore
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Tool:  # type: ignore

        def __init__(self, card):
            self.card = card

PLATFORM_ROOT = Path(os.environ.get("MATH_PLATFORM_ROOT",
                                    "/docker/zy/Mathematical_Reasoning_Platform")).resolve()
PLATFORM_PYTHON = os.environ.get("MATH_PLATFORM_PYTHON", "/usr/bin/python3")
RUNS_ROOT = (PLATFORM_ROOT / "platform_runs").resolve()
BACKEND_TIMEOUT = {"nl": 900, "formal": 2500, "opt": 1300}
VALID_BACKENDS = ("nl", "formal", "opt", "paper")


def _find_run_dir(backend: str, started_wall: float) -> Path | None:
    """找平台自建的、启动时间之后产生的、带 jsw_<backend> 标签的 run 目录。"""
    import time as time_mod

    candidates = [
        d for d in RUNS_ROOT.glob(f"*jsw_{backend}_single_1")
        if d.is_dir() and d.stat().st_mtime >= started_wall - 5
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _read_result_file(path: Path) -> dict | None:
    """读取并解析结果 JSON；缺失、损坏或未写完（无 status 字段）返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("status"):
        return data
    return None


def _summarize_record(record: dict, run_dir: Path) -> dict:
    """把平台统一报告压缩成团队友好的结构化摘要。"""
    dispatch = record.get("dispatch") or {}
    summary = dispatch.get("summary") or record.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    out = {
        "success": record.get("status") == "solved",
        "status": record.get("status"),
        "run_dir": str(run_dir),
        "report": str(run_dir / "platform_report.json"),
        "project": dispatch.get("project"),
    }
    if dispatch.get("project") == "NL Solver" or summary.get("verdict"):
        out["verdict"] = summary.get("verdict")
        out["proof"] = (summary.get("proof") or "")[:2000]
    if dispatch.get("project") == "Opt Solver" or summary.get("pred_value") is not None:
        out["pred_value"] = summary.get("pred_value")
        out["solver_status"] = summary.get("solver_status")
    if dispatch.get("project") == "Paper Gen" or summary.get("pdf"):
        out["paper_status"] = summary.get("status")
        out["pdf"] = summary.get("pdf")
        out["pages"] = summary.get("pages")
    if record.get("error"):
        out["platform_error"] = str(record.get("error"))[:300]
    return out


def _resolve_status(rd: Path) -> dict:
    """状态查询：优先 mma_workspace/result.json（paper），其次统一报告。

    两类文件任一缺失、损坏或未写完（无 status 字段）都视为仍在执行，
    不返回 done + 空结果的假完成。
    """
    report = rd / "platform_report.json"
    record = _read_result_file(report)
    source = "platform_report.json"
    if record is None:
        result_file = rd / "mma_workspace" / "result.json"
        record = _read_result_file(result_file)
        source = "result.json"
    if record is None:
        return {"success": True, "phase_status": "running", "run_dir": str(rd),
                "hint": "任务仍在执行或结果尚未写完，请稍后再查。"}
    out = _summarize_record(record, rd)
    out["source"] = source
    return {"success": True, "phase_status": "done", **out}


class MathPlatformTool(Tool):
    """把题目转交数学推理平台：同步等待（nl/formal/opt）或异步两段式（paper）。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="math_platform_solve",
                name="math_platform_solve",
                description=(
                    "把一道数学题转交本机数学推理平台求解。"
                    "backend=nl 求一般数学题（快速）；backend=formal 证明命题（慢、"
                    "结论机器验证）；backend=opt 解优化建模（快速）；backend=paper "
                    "生成完整数模竞赛论文（很慢，必须先用 action=start 启动，"
                    "再用 action=status 且携带返回的 run_dir 轮询，此时无需 question）。"
                    "question 必须是完整题目的原文。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "backend": {
                            "type": "string",
                            "enum": list(VALID_BACKENDS),
                            "description": "选择平台模块：nl/formal/opt/paper。",
                        },
                        "question": {
                            "type": "string",
                            "description": "完整题目原文（action=run 或 start 时必填）。",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["run", "start", "status"],
                            "description": (
                                "run=同步执行直到出结果（nl/formal/opt 用）；"
                                "start/status=paper 专用两段式：先 start 启动，"
                                "再拿返回的 run_dir 用 status 轮询。"
                            ),
                        },
                        "run_dir": {
                            "type": "string",
                            "description": "action=status 时填 start 返回的 run_dir。",
                        },
                    },
                    "required": ["backend", "action"],
                    "additionalProperties": False,
                },
                parallel_safe=False,
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict:
        inputs = inputs or {}
        backend = str(inputs.get("backend") or "").strip()
        question = str(inputs.get("question") or "").strip()
        action = str(inputs.get("action") or ("start" if backend == "paper" else "run")).strip()
        if backend not in VALID_BACKENDS:
            return {"success": False, "error": f"backend 必须是 {VALID_BACKENDS} 之一"}
        if action == "status":
            return self._status(inputs)
        if action not in ("run", "start"):
            return {"success": False, "error": f"未知 action: {action!r}"}
        if not question:
            return {"success": False, "error": "question 必填：请提供完整题目原文"}

        import time as time_mod

        started_wall = time_mod.time()
        problem_file = RUNS_ROOT / f"jsw_{backend}_input_{int(started_wall)}.txt"
        problem_file.parent.mkdir(parents=True, exist_ok=True)
        problem_file.write_text(question, encoding="utf-8")

        cmd = [
            PLATFORM_PYTHON, "run_platform.py", question,
            "--force", "solving" if backend in ("nl", "formal") else "modeling",
            "--tag", f"jsw_{backend}",
        ]
        env = os.environ.copy()

        if action == "start":
            try:
                detached = subprocess.Popen(
                    cmd, cwd=str(PLATFORM_ROOT), env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                return {"success": False, "error": f"启动失败: {exc}"}
            time_mod.sleep(3)  # 等平台创建自己的 run 目录
            run_dir = _find_run_dir(backend, started_wall)
            return {
                "success": True,
                "action": "start",
                "pid": detached.pid,
                "run_dir": str(run_dir) if run_dir else "",
                "result_file": (str(run_dir / "platform_report.json")
                                if run_dir else ""),
                "hint": ("已后台启动。之后用 action=status 且 run_dir='"
                         + str(run_dir or "?") + "' 轮询，直到返回 done。"),
            }

        # action == "run"：放到工作线程执行，避免阻塞事件循环
        timeout = BACKEND_TIMEOUT.get(backend, 900)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd, cwd=str(PLATFORM_ROOT), env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode not in (0, 1):
                return {"success": False,
                        "error": f"平台退出码 {proc.returncode}",
                        "stderr": (proc.stderr or "")[-500:]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"平台执行超时（>{timeout}s）"}
        except OSError as exc:
            return {"success": False, "error": f"平台启动失败: {exc}"}

        run_dir = _find_run_dir(backend, started_wall)
        if run_dir:
            report = run_dir / "platform_report.json"
            record = _read_result_file(report)
            if record is not None:
                return {"success": True, "backend": backend, "run_dir": str(run_dir),
                        "result": _summarize_record(record, run_dir)}
        return {"success": False, "error": "平台未产出可解析的结果文件",
                "run_dir": str(run_dir or "")}

    def _status(self, inputs: dict) -> dict:
        run_dir_raw = str(inputs.get("run_dir") or "").strip()
        if not run_dir_raw:
            return {"success": False, "error": "action=status 需要 run_dir", "run_dir": ""}
        try:
            rd = Path(run_dir_raw).resolve()
            rd.relative_to(RUNS_ROOT)
        except (ValueError, OSError):
            return {"success": False, "error": "run_dir 不在平台产物目录内",
                    "run_dir": run_dir_raw}
        if not rd.is_dir():
            return {"success": False, "error": "run_dir 不存在", "run_dir": run_dir_raw}
        return _resolve_status(rd)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)
