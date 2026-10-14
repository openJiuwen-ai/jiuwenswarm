# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DeepSearch Research Toolkit

提供文献调研工具 ``deepsearch_literature``：将 openJiuwen-DeepSearch
(https://atomgit.com/openJiuwen/deepsearch) 作为科研流水线 Ideation/Writing
模块的检索引擎, 以子进程方式调用其 CLI, 返回研究报告/检索结果的内容摘录
与落盘路径, 供后续 Rail 做引用溯源。

使用方式:
1. 在 config.yaml 的 ``research_tools`` 段配置 deepsearch.repo_dir 等参数
2. ``DeepSearchToolkit(...).get_tools()`` 获取工具列表并注册到 Runner

未配置或调用失败时返回结构化错误提示 (含降级建议), 不抛异常阻断流水线。
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

_DEFAULT_TIMEOUT_SEC = 900


class DeepSearchToolkit:
    """Wrap the openJiuwen-DeepSearch CLI as a member-callable tool."""

    def __init__(
        self,
        repo_dir: str = "",
        python_exe: str = "",
        extra_args: List[str] | None = None,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        workspace_dir: str = "",
    ) -> None:
        self.repo_dir = repo_dir or ""
        self.python_exe = python_exe or os.sys.executable
        self.extra_args = list(extra_args or [])
        self.timeout_sec = int(timeout_sec or _DEFAULT_TIMEOUT_SEC)
        self.workspace_dir = workspace_dir or os.getcwd()

    # ------------------------------------------------------------------
    def _run_query(self, query: str, top_k: int) -> dict[str, Any]:
        if not self.repo_dir:
            return {
                "ok": False,
                "error": "research_tools.deepsearch.repo_dir 未配置; "
                "请在 config.yaml research_tools 段配置 DeepSearch 仓库路径后重试, "
                "或改用 web_search 工具完成本轮文献检索。",
            }
        entry = Path(self.repo_dir) / "deepsearch" / "main.py"
        if not entry.exists():
            return {"ok": False, "error": f"DeepSearch 入口不存在: {entry}"}

        out_dir = Path(self.workspace_dir) / "deepsearch_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = str(int(__import__("time").time()))
        report_path = out_dir / f"ds_{stamp}.md"

        cmd = [
            self.python_exe,
            str(entry),
            "--query",
            query,
            "--output_dir",
            str(out_dir),
        ] + self.extra_args
        try:
            proc = subprocess.run(  # noqa: S603 - cmd 由配置白名单参数拼接
                cmd,
                cwd=str(Path(self.repo_dir) / "deepsearch"),
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"DeepSearch 超时 ({self.timeout_sec}s)"}

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": "DeepSearch 退出码 "
                + str(proc.returncode)
                + "; stderr 尾部: "
                + (proc.stderr or "")[-500:],
            }
        report_path.write_text(proc.stdout or "", encoding="utf-8", errors="ignore")
        digest = (proc.stdout or "")[:4000]
        return {
            "ok": True,
            "query": query,
            "report_path": str(report_path),
            "digest": digest,
            "note": "完整报告已落盘; 引用其内容时请标注 report_path 以便溯源核查。",
        }

    # ------------------------------------------------------------------
    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner."""

        def make_tool(name: str, description: str, input_params: dict, func) -> Tool:
            card = ToolCard(name=name, description=description, input_params=input_params)
            return LocalFunction(card=card, func=func)

        toolkit = self

        def deepsearch_literature(query: str, top_k: int = 10) -> str:
            """文献调研: 调用 DeepSearch 检索并返回报告摘录与落盘路径。"""
            result = toolkit._run_query(query, top_k)
            return json.dumps(result, ensure_ascii=False)

        return [
            make_tool(
                name="deepsearch_literature",
                description=(
                    "【深度文献检索工具】调用 openJiuwen-DeepSearch 引擎执行文献调研,"
                    "返回检索报告的内容摘录(digest)与完整报告落盘路径(report_path)。"
                    "适用于: 相关工作调研、研究假设的证据检索、论文引用核查。"
                    "参数: query 为检索问题(建议英文学术表述); top_k 预留。"
                    "失败时返回 ok=false 与降级建议(改用 web_search)。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "文献检索问题, 例如 'hierarchical memory mechanisms for LLM agents on LongMemEval'",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "期望保留的检索结果条数(预留参数, 默认 10)",
                        },
                    },
                    "required": ["query"],
                },
                func=deepsearch_literature,
            ),
        ]


__all__ = ["DeepSearchToolkit"]
