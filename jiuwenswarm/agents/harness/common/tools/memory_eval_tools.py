# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Memory Eval Toolkit

提供 ``run_memory_eval`` 工具: 将 LongMemEval 记忆策略评测 harness 封装为
JiuwenSwarm 成员可调度的执行能力。科研流水线 (SwarmFlow) 的 Experiment
模块通过本工具发起评测, 结果流水落盘 (results.jsonl), 每条记录含
token 用量与延迟, 供资源报告从框架侧聚合与追溯。

使用方式:
1. config.yaml ``research_tools.memory_eval`` 段配置 harness_dir
2. ``MemoryEvalToolkit(...).get_tools()`` 注册到 Runner
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, List

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 7200


class MemoryEvalToolkit:
    """Run the LongMemEval memory-policy benchmark as a member tool."""

    def __init__(
        self,
        harness_dir: str = "",
        python_exe: str = "",
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.harness_dir = harness_dir or ""
        self.python_exe = python_exe or os.sys.executable
        self.timeout_sec = int(timeout_sec or _DEFAULT_TIMEOUT_SEC)

    # ------------------------------------------------------------------
    def _run(self, mode: str, policies: str, n: int, budgets: str) -> dict[str, Any]:
        if not self.harness_dir:
            return {
                "ok": False,
                "error": "research_tools.memory_eval.harness_dir 未配置; "
                "请在 config.yaml 指向 paper-lab/experiments/longmemeval 后重试。",
            }
        script = Path(self.harness_dir) / "run.py"
        if not script.exists():
            return {"ok": False, "error": f"评测入口不存在: {script}"}

        cmd = [self.python_exe, str(script)]
        if mode == "full":
            cmd.append("--full")
        else:
            cmd += ["--n", str(max(1, int(n)))]
        if policies:
            cmd += ["--policies", policies]
        try:
            proc = subprocess.run(  # noqa: S603 - 参数经白名单校验后拼接
                cmd,
                cwd=str(self.harness_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"评测超时 ({self.timeout_sec}s)"}
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": "评测退出码 "
                + str(proc.returncode)
                + "; stderr 尾部: "
                + (proc.stderr or "")[-800:],
            }

        # 从 harness 输出目录读取增量结果流水 (含逐题 token/延迟)
        results_jsonl = (
            Path(self.harness_dir).resolve().parents[2]
            / "output" / "longmemeval" / "results.jsonl"
        )
        records: List[dict] = []
        if results_jsonl.exists():
            lines = results_jsonl.read_text(encoding="utf-8").splitlines()
            records = [json.loads(x) for x in lines if x.strip()]
        total_pt = sum(r.get("prompt_tokens", 0) for r in records)
        total_ct = sum(r.get("completion_tokens", 0) for r in records)
        summary_lines = [
            x for x in (proc.stdout or "").splitlines()
            if x.startswith(("sliding", "retrieval", "full", "compression", "hierarchical"))
        ]
        return {
            "ok": True,
            "mode": mode,
            "policies": policies or "all",
            "records": len(records),
            "total_prompt_tokens": total_pt,
            "total_completion_tokens": total_ct,
            "summary": summary_lines[:30],
            "results_path": str(results_jsonl),
        }

    # ------------------------------------------------------------------
    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner."""

        def make_tool(name: str, description: str, input_params: dict, func) -> Tool:
            card = ToolCard(name=name, description=description, input_params=input_params)
            return LocalFunction(card=card, func=func)

        toolkit = self

        def run_memory_eval(
            mode: str = "smoke",
            policies: str = "",
            n_questions: int = 10,
            budgets: str = "",
        ) -> str:
            """执行记忆策略评测并返回汇总。"""
            result = toolkit._run(mode, policies, n_questions, budgets)
            return json.dumps(result, ensure_ascii=False)

        return [
            make_tool(
                name="run_memory_eval",
                description=(
                    "【记忆策略评测工具】在 LongMemEval 上执行预算受控的 LLM Agent "
                    "记忆策略评测 (6 策略 x 512/1024/2048/4096 token 预算), "
                    "返回 EM/F1 汇总、逐题结果路径与 token 用量。"
                    "参数: mode='smoke'(小样本) 或 'full'(120 题全量); "
                    "policies 逗号分隔策略子集(空=全部): "
                    "full_context,sliding_window,retrieval,retrieval_recency,compression,hierarchical; "
                    "n_questions 为 smoke 模式题数。"
                    "结果流水含逐题 token/延迟, 支持断点续跑。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "description": "smoke 或 full",
                        },
                        "policies": {
                            "type": "string",
                            "description": "逗号分隔的策略子集, 空串表示全部",
                        },
                        "n_questions": {
                            "type": "integer",
                            "description": "smoke 模式的题目数 (默认 10)",
                        },
                        "budgets": {
                            "type": "string",
                            "description": "逗号分隔的预算子集(cl100k token), 空串表示全部档位",
                        },
                    },
                },
                func=run_memory_eval,
            ),
        ]


__all__ = ["MemoryEvalToolkit"]
