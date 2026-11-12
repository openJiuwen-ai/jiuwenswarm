"""
openJiuwen-DeepSearch 主脚本

使用 uv 执行：
    uv run scripts/main.py --query "研究题目"

依赖：
    - openjiuwen-deepsearch==0.1.1
    - python-dotenv
"""
import argparse
import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
import shutil
from typing import Any, ClassVar, Dict, List

from dotenv import load_dotenv
from openjiuwen_deepsearch.config.config import Config
from openjiuwen_deepsearch.config.method import ExecutionMethod
from openjiuwen_deepsearch.framework.openjiuwen.agent.agent_factory import AgentFactory
from openjiuwen_deepsearch.utils.debug_utils.result_exporter import ResultExporter
from openjiuwen_deepsearch.framework.openjiuwen.agent.workflow import parse_endnode_content
from openjiuwen_deepsearch.utils.log_utils.log_manager import LogManager

from convert_docx import convert_md_to_docx
from convert_html import convert_md_to_html

CHECKED_CITATION_RE = re.compile(
    r"\[\s*checked_citation:\s*\d+\s*\](\[\[\d+\]\]\((?:[^()]|\([^()]*\))*\))"
)
LEGACY_CITATION_RE = re.compile(r"\[\s*citation:\s*\d+\s*\]")

# 获取技能根目录，优先使用 SKILL_ROOT 环境变量，否则自动检测
SKILL_ROOT = Path(os.getenv("SKILL_ROOT", Path(__file__).parent.parent))

# 加载 .env 文件
env_path = SKILL_ROOT / ".env"
load_dotenv(env_path)

os.environ["LLM_SSL_VERIFY"] = "false"
os.environ["TOOL_SSL_VERIFY"] = "false"

# 初始化日志管理器
log_dir = SKILL_ROOT / "output" / "logs"
LogManager.init(
    log_dir=str(log_dir),
    max_bytes=100 * 1024 * 1024,
    backup_count=20,
    level="INFO",
    is_sensitive=False
)

# 初始化结果导出器
results_dir = SKILL_ROOT / "output" / "results"
ResultExporter.init(
    results_dir=str(results_dir)
)

logger = logging.getLogger(__name__)


async def run_jiuwen_workflow(query: str, agent_config: dict):
    """
    执行 openJiuwen-DeepSearch 工作流

    Args:
        query: 用户查询字符串
        agent_config: Agent 配置字典

    Returns:
        最终研究报告内容
    """
    agent_factory = AgentFactory()
    agent = agent_factory.create_agent(agent_config)

    full_report = ""

    async for chunk in agent.run(
            message=query,
            conversation_id=str(uuid.uuid4()),
            report_template="",
            interrupt_feedback="",
            agent_config=agent_config
    ):
        logger.debug("[Stream message from node: %s]", chunk)
        chunk_content = json.loads(chunk)
        report_result = parse_endnode_content(chunk_content)
        if report_result:
            logger.debug("[Final Report is: %s]", report_result)
            if not full_report:
                full_report = report_result.get("response_content", "")
                full_report = CHECKED_CITATION_RE.sub(r"\1", full_report)
                full_report = LEGACY_CITATION_RE.sub("", full_report)

    output_md = f"{query}.md"
    output_html = f"{query}.html"
    output_docx = f"{query}.docx"

    output_dir = SKILL_ROOT / "output" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_md_path = output_dir / output_md
    output_html_path = output_dir / output_html
    output_docx_path = output_dir / output_docx

    try:
        with open(output_md, "w", encoding="utf-8") as f:
            f.write(full_report)
        convert_md_to_html(output_md, output_html)
        convert_md_to_docx(output_md, output_docx)
        
        shutil.move(output_md, output_md_path)
        shutil.move(output_html, output_html_path)
        shutil.move(output_docx, output_docx_path)
    except OSError as e:
        with open(output_md, "w", encoding="utf-8") as f:
            f.write(full_report)

    return full_report


def load_agent_config() -> dict:
    """
    从环境变量加载 Agent 配置

    Returns:
        Agent 配置字典

    Raises:
        ValueError: 缺少必需的环境变量
    """
    # 从环境变量读取配置
    required_env_vars = {
        "LLM_MODEL_NAME": "LLM 模型名称",
        "LLM_MODEL_TYPE": "LLM 模型类型",
        "LLM_BASE_URL": "LLM API 地址",
        "LLM_API_KEY": "LLM API Key",
        "WEB_SEARCH_ENGINE_NAME": "搜索引擎名称",
        "WEB_SEARCH_API_KEY": "搜索引擎 API Key",
        "WEB_SEARCH_URL": "搜索引擎 API 地址",
    }

    # 检查必需的环境变量
    missing_vars = [
        var_name for var_name, desc in required_env_vars.items()
        if not os.getenv(var_name)
    ]

    if missing_vars:
        raise ValueError(
            f"缺少必需的环境变量: {', '.join(missing_vars)}\n"
            f"请在 .env 文件中配置这些变量。"
        )

    # 加载配置
    config = Config().agent_config.model_dump()

    # LLM 配置
    config["llm_config"]["general"] = {
        "model_name": os.getenv("LLM_MODEL_NAME"),
        "model_type": os.getenv("LLM_MODEL_TYPE"),
        "base_url": os.getenv("LLM_BASE_URL"),
        "api_key": bytearray(os.getenv("LLM_API_KEY", ""), encoding="utf-8"),
        "verify_ssl": False,
    }

    # 搜索引擎配置
    config["web_search_engine_config"] = {
        "search_engine_name": os.getenv("WEB_SEARCH_ENGINE_NAME"),
        "search_api_key": bytearray(os.getenv("WEB_SEARCH_API_KEY", ""), encoding="utf-8"),
        "search_url": os.getenv("WEB_SEARCH_URL"),
        "max_web_search_results": int(os.getenv("MAX_WEB_SEARCH_RESULTS", "5")),
    }

    # 工作流配置
    config["workflow_human_in_the_loop"] = False
    config["search_mode"] = "research"
    config["outliner_max_section_num"] = 5
    config["outline_interaction_enabled"] = False
    config["source_tracer_infer_switch"] = False

    # 执行方式
    execution_method = os.getenv("EXECUTION_METHOD", "parallel")
    if execution_method == ExecutionMethod.DEPENDENCY_DRIVING.value:
        config["execution_method"] = ExecutionMethod.DEPENDENCY_DRIVING.value
    else:
        config["execution_method"] = ExecutionMethod.PARALLEL.value

    return config


def execute_deep_search(query: str) -> str | None:
    """
    执行深度研究（供 Agent 调用）

    Args:
        query: 研究题目

    Returns:
        研究报告内容，失败返回 None
    """
    try:
        # 加载 Agent 配置
        agent_config = load_agent_config()

        # 执行工作流
        logger.info("开始执行深度研究: %s", query)
        result = asyncio.run(run_jiuwen_workflow(query, agent_config))

        if result:
            logger.info("研究报告生成完成")
            return result
        else:
            logger.warning("未生成研究报告")
            return None

    except ValueError as e:
        logger.error("配置错误: %s", e)
        return None
    except Exception as e:
        logger.exception("执行失败: %s", e)
        return None


def run_background():
    """在后台运行当前脚本"""
    # 检查是否已经在后台进程中（防止无限循环）
    if os.getenv("JIUWEN_BACKGROUND_MODE") == "1":
        return

    script_path = Path(__file__).resolve()
    python_executable = sys.executable

    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            python_executable = str(pyw)

    cwd = Path.cwd()  # 当前工作目录

    # 获取当前命令行参数（移除 --background 标志）
    cmd_args = [arg for arg in sys.argv[1:] if arg != "--background"]

    # 跨平台后台执行
    env = os.environ.copy()
    env["JIUWEN_BACKGROUND_MODE"] = "1"  # 标记已进入后台模式

    # 确保 SKILL_ROOT 环境变量传递给子进程
    if "SKILL_ROOT" not in env:
        env["SKILL_ROOT"] = str(SKILL_ROOT)

    if sys.platform == "win32":
        detached_process = 0x00000008
        create_no_window = 0x08000000

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        proc = subprocess.Popen(
            [python_executable, str(script_path)] + cmd_args,
            creationflags=detached_process | subprocess.CREATE_NO_WINDOW,
            cwd=str(cwd),
            env=env,
            startupinfo=startupinfo
        )
    else:
        # Linux/macOS: 使用 start_new_session 创建新会话
        proc = subprocess.Popen(
            [python_executable, str(script_path)] + cmd_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(cwd),
            env=env
        )

    with open(f"PID.info", "w", encoding="utf-8") as f:
        f.write(f"启动成功，子进程 PID={proc.pid}")
    query_text = ' '.join(cmd_args[cmd_args.index('--query') + 1:]) if '--query' in cmd_args else 'default'
    logger.info("任务已在后台启动，查询: %s", query_text)
    sys.exit(0)


def main():
    """主函数（命令行入口）"""
    parser = argparse.ArgumentParser(
        description="openJiuwen-DeepSearch - 知识增强型深度检索与研究引擎"
    )
    parser.add_argument(
        "--mode",
        default="query",
        choices=["query"],
        help="运行模式（当前仅支持 query）"
    )
    parser.add_argument(
        "--query",
        nargs="*",
        default=["AI手机研究报告"],
        help="研究题目（支持空格）"
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="在后台运行（默认行为）"
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="在前台运行"
    )

    args = parser.parse_args()

    # 默认后台执行模式，除非指定 --foreground
    if not args.foreground:
        run_background()

    query = " ".join(args.query)

    result = execute_deep_search(query)

    if result:
        logger.info("研究报告生成完成")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()