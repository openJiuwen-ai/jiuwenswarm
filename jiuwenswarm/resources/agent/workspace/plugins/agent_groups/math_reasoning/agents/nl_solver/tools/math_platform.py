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
                                    "/docker/zy/Mathematical_Reasoning_Platform"))
PLATFORM_PYTHON = os.environ.get("MATH_PLATFORM_PYTHON", "/usr/bin/python3")
BACKEND_TIMEOUT = {"nl": 900, "formal": 2500, "opt": 1300}
VALID_BACKENDS = ("nl", "formal", "opt", "paper")


def _run_dir_root() -> Path:
    return PLATFORM_ROOT / "platform_runs"


def _find_run_dir(backend: str, started_wall: float) -> Path | None:
    """找平台自建的、启动时间之后产生的、带 jsw_<backend> 标签的 run 目录。"""
    import time as _time
    root = _run_dir_root()
    pattern = f"*jsw_{backend}_single_1"
    candidates = []
    for d in root.glob(pattern):
        if d.is_dir() and d.stat().st_mtime >= started_wall - 5:
            candidates.append(d)
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _summarize_report(report_path: Path) -> dict:
    """解析平台统一报告 platform_report.json 为结构化摘要。"""
    try:
        record = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    dispatch = record.get("dispatch") or {}
    summary = dispatch.get("summary") or {}
    out = {
        "success": record.get("status") == "solved",
        "status": record.get("status"),
        "report": str(report_path),
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


def _find_result(run_dir: Path) -> dict:
    result = run_dir / "mma_workspace" / "result.json"
    if result.exists():
        try:
            return json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    report = run_dir / "platform_report.json"
    if report.exists():
        return _summarize_report(report)
    return {}


def _summarize(record: dict, run_dir: Path) -> dict:
    """把平台统一报告压缩成团队友好的结构化摘要。"""
    dispatch = (record.get("dispatch") or {}) if isinstance(record, dict) else {}
    summary = dispatch.get("summary") or {}
    out = {
        "success": record.get("status") in ("solved", "completed"),
        "status": record.get("status"),
        "run_dir": str(run_dir),
        "report": str(run_dir / "platform_report.json"),
    }
    project = (dispatch.get("project") or "")
    if "NL Solver" in project or summary.get("verdict"):
        out["verdict"] = summary.get("verdict")
        out["proof"] = (summary.get("proof") or "")[:2000]
    if "Opt Solver" in project or summary.get("pred_value") is not None:
        out["pred_value"] = summary.get("pred_value")
        out["solver_status"] = summary.get("solver_status")
    if "Paper Gen" in project or summary.get("pdf"):
        out["paper_status"] = summary.get("status")
        out["pdf"] = summary.get("pdf")
        out["pages"] = summary.get("pages")
    if record.get("error"):
        out["platform_error"] = str(record.get("error"))[:300]
    return out


class MathPlatformTool(Tool):
    """把题目转交数学推理平台：同步等待（nl/formal/opt）或异步两段式（paper）。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="math_platform_solve",
                name="math_platform_solve",
                description=(
                    "把一道数学题转交本机数学推理平台求解。"
                    "backend=nl 求一般数学题（快速）；backend=formal 证明命题（慢、结论机器验证）；"
                    "backend=opt 解优化建模（快速）；backend=paper 生成完整数模竞赛论文（很慢，"
                    "必须先用 action=start 启动，再用 action=status 轮询）。"
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
                            "description": "完整题目原文（含全部条件与要求）。",
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
                    "required": ["backend", "question"],
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
        if not question:
            return {"success": False, "error": "question 必填：请提供完整题目原文"}

        import time as _time
        started_wall = _time.time()
        problem_file = PLATFORM_ROOT / "platform_runs" / f"jsw_{backend}_input_{int(started_wall)}.txt"
        problem_file.parent.mkdir(parents=True, exist_ok=True)
        problem_file.write_text(question, encoding="utf-8")

        cmd = [
            PLATFORM_PYTHON, "run_platform.py", question,
            "--force", "solving" if backend in ("nl", "formal") else "modeling",
            "--tag", f"jsw_{backend}",
        ]
        if backend in ("nl", "formal"):
            cmd += ["--solver", backend]
        else:
            cmd += ["--modeling-backend", backend]

        env = os.environ.copy()

        if action == "start":
            detached = subprocess.Popen(
                cmd, cwd=str(PLATFORM_ROOT), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            import time as _time2
            time.sleep(3)  # 等平台创建自己的 run 目录
            run_dir = _find_run_dir(backend, started_wall)
            return {
                "success": True,
                "action": "start",
                "pid": detached.pid,
                "run_dir": str(run_dir) if run_dir else "(平台 run 目录创建中，稍后用 status 提供)",
                "result_file": (str(run_dir / "mma_workspace" / "result.json")
                                if run_dir else ""),
                "hint": "已后台启动。之后用 action=status 且 run_dir='"
                        + str(run_dir or "?") + "' 轮询；platform_report.json 出现且 "
                        "status=solved 即完成。",
            }

        if action == "status":
            rd = Path(str(inputs.get("run_dir") or ""))
            if not rd.is_dir():
                return {"success": False, "error": "run_dir 不存在", "run_dir": str(rd)}
            report = rd / "platform_report.json"
            record = _find_result(rd)
            if not record and not report.exists():
                return {"success": True, "status": "running", "run_dir": str(rd),
                        "hint": "任务仍在执行，请稍后再查。"}
            return {"success": True, "status": "done", "result": _summarize_report(report)}

        timeout = BACKEND_TIMEOUT.get(backend, 900)
        try:
            proc = subprocess.run(
                cmd, cwd=str(PLATFORM_ROOT), env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode not in (0, 1):
                return {"success": False,
                        "error": f"平台退出码 {proc.returncode}",
                        "stderr": proc.stderr[-500:]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"平台执行超时（>{timeout}s）"}

        run_dir = _find_run_dir(backend, started_wall)
        report = (run_dir / "platform_report.json") if run_dir else None
        if report and report.exists():
            return {"success": True, "backend": backend,
                    "result": _summarize_report(report), "run_dir": str(run_dir)}
        return {"success": False, "error": "平台未产出结果文件",
                "run_dir": str(run_dir or "")}

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)
