#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日报生成器辅助脚本

功能：
1. 收集今日记忆文件内容
2. 解析待办事项状态
3. 生成格式化日报

使用方式：
    python report_helper.py --date 2026-03-06
    python report_helper.py  # 默认使用今天日期
"""

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 尝试从 jiuwenswarm.utils 导入，如果失败则使用环境变量或硬编码路径
try:
    from jiuwenswarm.common.utils import get_agent_root_dir
    _has_jiuwenswarm = True
except ImportError:
    _has_jiuwenswarm = False

# 修复 Windows 控制台编码问题
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def get_workspace_dir() -> Path:
    """获取 Agent 根目录（含 memory/、sessions/、skills/）。

    优先级：
    1. jiuwenswarm.utils.get_agent_root_dir()（如果可用）
    2. JIUWENSWARM_DATA_DIR 环境变量 / agent
    3. JIUWENSWARM_AGENT_ROOT 环境变量
    4. 默认 ~/.jiuwenswarm/agent
    """
    if _has_jiuwenswarm:
        return get_agent_root_dir()

    # 检查多实例环境变量
    env_workspace = os.getenv("JIUWENSWARM_DATA_DIR")
    if env_workspace:
        return Path(env_workspace) / "agent"

    # 检查旧的 AGENT_ROOT 环境变量
    if "JIUWENSWARM_AGENT_ROOT" in os.environ:
        return Path(os.environ["JIUWENSWARM_AGENT_ROOT"])

    # 默认路径
    home_agent = Path.home() / ".jiuwenswarm" / "agent"
    if home_agent.is_dir():
        return home_agent

    # 开发模式：包内 resources/agent
    script_dir = Path(__file__).resolve()
    pkg_agent = script_dir.parent.parent.parent  # daily-report -> skills -> agent
    if (pkg_agent / "memory").is_dir() or (pkg_agent / "skills").is_dir():
        return pkg_agent

    return home_agent


def read_file_safe(file_path: Path) -> str:
    """安全读取文件内容"""
    if not file_path.exists():
        return ""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"[读取文件失败: {e}]"


def parse_todo_status(content: str) -> dict[str, list[dict]]:
    """解析待办事项状态

    支持两种格式：
    1. Markdown checkbox: - [x] 任务 / - [ ] 任务
    2. YAML frontmatter: status: completed/running/waiting
    """
    result = {
        "completed": [],
        "running": [],
        "waiting": [],
        "cancelled": []
    }

    if not content:
        return result

    lines = content.split("\n")

    # 解析 Markdown checkbox 格式
    checkbox_pattern = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+)$")

    # 解析带状态标记的格式 (如: 1. [status:completed] 任务描述)
    status_pattern = re.compile(r"^\s*\d+\.\s*\[status:(\w+)\]\s*(.+)$", re.IGNORECASE)

    # 解析 YAML 格式的状态
    yaml_status_pattern = re.compile(r"status:\s*(\w+)", re.IGNORECASE)

    current_task = None
    current_status = "waiting"

    for line in lines:
        # 尝试 checkbox 格式
        checkbox_match = checkbox_pattern.match(line)
        if checkbox_match:
            checked = checkbox_match.group(1).lower() == "x"
            task_desc = checkbox_match.group(2).strip()
            status = "completed" if checked else "waiting"
            result[status].append({
                "description": task_desc,
                "status": status
            })
            continue

        # 尝试状态标记格式
        status_match = status_pattern.match(line)
        if status_match:
            status = status_match.group(1).lower()
            task_desc = status_match.group(2).strip()
            if status in result:
                result[status].append({
                    "description": task_desc,
                    "status": status
                })
            continue

        # 尝试提取 YAML 状态
        yaml_match = yaml_status_pattern.search(line)
        if yaml_match:
            current_status = yaml_match.group(1).lower()

        # 解析普通任务行 (如: 1. 任务描述)
        task_match = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if task_match:
            task_desc = task_match.group(1).strip()
            if task_desc and current_status in result:
                result[current_status].append({
                    "description": task_desc,
                    "status": current_status
                })
                current_status = "waiting"  # 重置状态

    return result


def extract_work_summary(content: str) -> list[str]:
    """从记忆文件中提取工作摘要

    提取要点：
    - 以 - 或 * 开头的列表项
    - 以 ## 标题分隔的内容
    """
    summaries = []

    if not content:
        return summaries

    lines = content.split("\n")
    in_section = False
    current_section = []

    for line in lines:
        stripped = line.strip()

        # 跳过空行和注释
        if not stripped or stripped.startswith("<!--"):
            continue

        # 检测标题
        if stripped.startswith("##"):
            if in_section and current_section:
                summaries.append("\n".join(current_section))
                current_section = []
            in_section = True
            continue

        # 收集列表项
        if stripped.startswith("-") or stripped.startswith("*"):
            item = stripped.lstrip("-* ").strip()
            if item:
                current_section.append(f"- {item}")

    # 添加最后一个 section
    if current_section:
        summaries.append("\n".join(current_section))

    return summaries


def find_latest_todo_file(workspace_dir: Path) -> Path | None:
    """查找最新的 todo.md 文件"""
    session_dir = workspace_dir / "session"

    if not session_dir.exists():
        return None

    todo_files = list(session_dir.rglob("todo.md"))

    if not todo_files:
        return None

    # 按修改时间排序，返回最新的
    todo_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return todo_files[0]


def generate_report(
    date_str: str,
    memory_content: str,
    todo_data: dict[str, list[dict]],
    long_term_memory: str = ""
) -> dict[str, Any]:
    """生成日报数据"""

    # 统计任务数量
    completed_count = len(todo_data["completed"])
    running_count = len(todo_data["running"])
    waiting_count = len(todo_data["waiting"])

    # 提取工作摘要
    work_summaries = extract_work_summary(memory_content)

    # 生成日报
    report = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(),
        "statistics": {
            "completed": completed_count,
            "running": running_count,
            "waiting": waiting_count
        },
        "tasks": {
            "completed": [t["description"] for t in todo_data["completed"]],
            "running": [t["description"] for t in todo_data["running"]],
            "waiting": [t["description"] for t in todo_data["waiting"]]
        },
        "work_summary": work_summaries,
        "long_term_context": long_term_memory[:500] if long_term_memory else ""  # 截取前500字符
    }

    return report


def format_report_markdown(report: dict[str, Any]) -> str:
    """将日报数据格式化为 Markdown"""

    lines = [
        f"# 📋 工作日报 - {report['date']}",
        "",
        "## 📊 今日概览",
        f"- 完成任务: {report['statistics']['completed']} 项",
        f"- 进行中: {report['statistics']['running']} 项",
        f"- 待处理: {report['statistics']['waiting']} 项",
        ""
    ]

    # 已完成任务
    if report["tasks"]["completed"]:
        lines.append("## ✅ 已完成任务")
        for task in report["tasks"]["completed"]:
            lines.append(f"- {task}")
        lines.append("")

    # 进行中任务
    if report["tasks"]["running"]:
        lines.append("## 🔄 进行中任务")
        for task in report["tasks"]["running"]:
            lines.append(f"- {task}")
        lines.append("")

    # 今日工作记录
    lines.append("## 📝 今日工作记录")
    if report["work_summary"]:
        for summary in report["work_summary"]:
            lines.append(summary)
    else:
        lines.append("- 暂无工作记录")
    lines.append("")

    # 明日计划
    lines.append("## 🔜 明日计划")
    if report["tasks"]["waiting"]:
        for task in report["tasks"]["waiting"]:
            lines.append(f"- {task}")
    else:
        lines.append("- 待补充")
    lines.append("")

    return "\n".join(lines)
