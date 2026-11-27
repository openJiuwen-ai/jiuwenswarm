# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List

from openjiuwen_deepsearch.config.config import Config
from openjiuwen_deepsearch.config.method import ExecutionMethod
from openjiuwen_deepsearch.framework.openjiuwen.agent.agent_factory import AgentFactory
from openjiuwen_deepsearch.framework.openjiuwen.agent.workflow import parse_endnode_content
from openjiuwen_deepsearch.utils.log_utils.log_common import session_id_ctx
from openjiuwen_deepsearch.utils.log_utils.log_manager import LogManager

from jiuwenclaw.agentserver.gateway_push import GatewayPushTransport, WebSocketGatewayPushTransport
from jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_docx_offline import convert_md_to_docx
from jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline import convert_md_to_html
from jiuwenclaw.agentserver.tools.deepresearch_plugin.report_bundle import build_report_bundle
from jiuwenclaw.utils import format_session_log, get_agent_workspace_dir, get_logs_dir

logger = logging.getLogger(__name__)
SAVE_REPORT_PATH = "workspace/reports"
INFERENCE_LINK_RE = re.compile(r"\[([^\]]+)\]\(#inference:(\d+)\)")


class TaskStatus(str, Enum):
    """任务状态枚举."""
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class DeepResearchTask:
    """DeepResearch 任务数据类."""
    task_id: str
    query: str
    file_name: str
    status: TaskStatus
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    result: str = ""
    error: str = ""
    session_id: str = ""
    channel_id: str = ""
    request_id: str = ""
    # 协作取消事件
    cancel_event: threading.Event | None = None

    def __post_init__(self):
        """初始化取消事件."""
        if self.cancel_event is None:
            self.cancel_event = threading.Event()

    @staticmethod
    def format_timestamp(timestamp: float | None) -> str | None:
        """将时间戳转换为本地时区的格式化时间字符串（YYYY-MM-DD HH:MM:SS）."""
        if timestamp is None:
            return None
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class DeepResearchTaskManager:
    """DeepResearch 任务池管理器（全局单例）.

    管理所有 DeepResearch 任务的创建、执行、状态查询、取消等功能。
    """

    _instance: DeepResearchTaskManager | None = None
    _lock = asyncio.Lock()

    # 资源上限配置
    MAX_ACTIVE_TASKS = 10  # 最大活跃任务数
    MAX_TOTAL_TASKS = 100  # 最大保留任务数（包括已完成）
    MAX_TASKS_PER_SESSION = 5  # 每个会话最大任务数
    MAX_RESPONSE_CONTENT_BYTES = 10 * 1024 * 1024  # 最大报告内容大小（10MB）
    MAX_INFER_MESSAGES = 20  # 最大推理图数量
    MAX_SINGLE_HTML_BASE64_BYTES = 5 * 1024 * 1024  # 单个推理图最大大小（5MB）

    def __new__(cls):
        """实现单例模式."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化任务池管理器."""
        if hasattr(self, '_initialized'):
            return

        self._tasks: Dict[str, DeepResearchTask] = {}
        self._task_handles: Dict[str, asyncio.Task] = {}
        self._task_semaphore = asyncio.Semaphore(DeepResearchTaskManager.MAX_ACTIVE_TASKS)
        self._initialized = True
        self._gateway_push: GatewayPushTransport = WebSocketGatewayPushTransport()
        logger.info("[DeepResearchTaskManager] 初始化完成（全局单例）")

    @classmethod
    async def get_instance(cls) -> DeepResearchTaskManager:
        """获取全局单例实例（线程安全）."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _resolve_petal_search_url() -> str:
        """Build Petal Search URL from LLM API_BASE: strip trailing /v2, append /v1/ai-tools/web-search."""
        api_base = (
            os.environ.get("API_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or ""
        ).strip()
        if not api_base:
            return ""
        trimmed = api_base.rstrip("/")
        if trimmed.lower().endswith("/v2"):
            trimmed = trimmed[:-3]
        trimmed = trimmed.rstrip("/")
        return f"{trimmed}/v1/ai-tools/web-search"

    @staticmethod
    def _load_config() -> Dict[str, str]:
        """从环境变量加载 DeepSearch 配置.

        策略：
        - 大模型相关配置：先从 DeepSearch 专属环境变量获取，为空则 fallback 到项目全局环境变量
        - 其他配置：直接从 DeepSearch 专属环境变量获取

        环境变量映射（与 app_web_handlers.py _CONFIG_SET_ENV_MAP 保持一致）：
        - DeepSearch 专属：LLM_MODEL_NAME, LLM_MODEL_TYPE, LLM_BASE_URL, LLM_API_KEY,
          WEB_SEARCH_ENGINE_NAME, WEB_SEARCH_API_KEY, WEB_SEARCH_URL, EXECUTION_METHOD
        - 项目全局：MODEL_NAME, MODEL_PROVIDER, API_BASE, API_KEY
        """
        # 大模型相关配置：DeepSearch 专属优先，fallback 到项目全局
        llm_model_name = (
                os.environ.get("LLM_MODEL_NAME", "").strip()
                or os.environ.get("MODEL_NAME", "").strip()
        )
        llm_model_type = (
                os.environ.get("LLM_MODEL_TYPE", "").strip().lower()
                or os.environ.get("MODEL_PROVIDER", "").strip().lower()
        )
        llm_base_url = (
                os.environ.get("LLM_BASE_URL", "").strip()
                or os.environ.get("API_BASE", "").strip()
        )
        llm_api_key = (
                os.environ.get("LLM_API_KEY", "").strip()
                or os.environ.get("API_KEY", "").strip()
        )

        web_search_engine_name = os.environ.get("WEB_SEARCH_ENGINE_NAME", "petal").strip()
        web_search_api_key = (os.environ.get("WEB_SEARCH_API_KEY")
                              or os.environ.get("OPENAI_DEFAULT_HEADERS")
                              or os.environ.get("default_headers", "")
                              ).strip()
        web_search_url = (os.environ.get("WEB_SEARCH_URL")
                          or DeepResearchTaskManager._resolve_petal_search_url()
                          ).strip()
        execution_method = os.environ.get("EXECUTION_METHOD", "parallel").strip()

        config = {
            "LLM_MODEL_NAME": llm_model_name,
            "LLM_MODEL_TYPE": llm_model_type,
            "LLM_BASE_URL": llm_base_url,
            "LLM_API_KEY": llm_api_key,
            "WEB_SEARCH_ENGINE_NAME": web_search_engine_name,
            "WEB_SEARCH_API_KEY": web_search_api_key,
            "WEB_SEARCH_URL": web_search_url,
            "MAX_WEB_SEARCH_RESULTS": "5",
            "EXECUTION_METHOD": execution_method,
            "OUTLINER_MAX_SECTION_NUM": "5",
            "WORKFLOW_HUMAN_IN_THE_LOOP": "False",
            "OUTLINE_INTERACTION_ENABLED": "False",
            "SOURCE_TRACER_INFER_SWITCHES": "True",
            "VLM_CHART_GENERATOR_ENABLE": "True",
            "VLM_CHART_GENERATOR_MAX_ITERATIONS": 0,
        }
        return config

    @staticmethod
    def _validate_config(config: Dict[str, str]) -> tuple[bool, str]:
        """验证 DeepResearch 配置."""
        if not config["LLM_API_KEY"]:
            return False, (
                "DeepResearch 缺少 LLM_API_KEY。\n"
                "请在 ~/.jiuwenclaw/config/.env 中设置以下任一环境变量：\n"
                "  - LLM_API_KEY（DeepSearch 专属配置，优先使用）\n"
                "  - API_KEY（项目全局配置，作为 fallback）"
            )

        if not config["WEB_SEARCH_API_KEY"]:
            return False, (
                "DeepResearch 缺少 WEB_SEARCH_API_KEY。\n"
                "请在 ~/.jiuwenclaw/config/.env 中设置 WEB_SEARCH_API_KEY"
            )

        return True, "配置验证通过"

    @staticmethod
    def _validate_and_sanitize_filename(file_name: str) -> str:
        """校验并净化文件名，防止路径穿越.

        Args:
            file_name: 原始文件名

        Returns:
            安全的文件名组件

        Raises:
            ValueError: 如果文件名包含非法字符或路径穿越模式
        """
        # 移除已知后缀
        base_name = DeepResearchTaskManager._strip_known_suffix(file_name)

        # 检查空文件名
        if not base_name or not base_name.strip():
            raise ValueError("file_name 不能为空")

        # 检查路径分隔符和路径穿越模式
        dangerous_patterns = ['/', '\\', '..', '~', '\x00']
        for pattern in dangerous_patterns:
            if pattern in base_name:
                raise ValueError(f"file_name 包含非法字符或路径穿越模式: '{pattern}'")

        # 检查绝对路径（Windows 和 Unix）
        if base_name.startswith('/') or (len(base_name) > 1 and base_name[1] == ':'):
            raise ValueError("file_name 不能是绝对路径")

        # 只允许安全字符：字母、数字、下划线、短横线、中文、空格（替换为下划线）
        import re as filename_re
        safe_name = filename_re.sub(r'[^\w\u4e00-\u9fff\-]', '_', base_name)
        safe_name = filename_re.sub(r'_+', '_', safe_name).strip('_')

        if not safe_name:
            raise ValueError("file_name 经净化后为空，请使用有效的文件名")

        # 限制文件名长度
        max_length = 200
        if len(safe_name) > max_length:
            safe_name = safe_name[:max_length]

        return safe_name

    @staticmethod
    def _verify_path_containment(output_dir: str, target_path: str) -> bool:
        """校验目标路径是否在输出目录范围内.

        Args:
            output_dir: 输出目录路径
            target_path: 目标文件路径

        Returns:
            目标路径是否在输出目录内
        """
        try:
            output_dir_resolved = Path(output_dir).resolve()
            target_resolved = Path(target_path).resolve()
            # 检查目标路径是否在输出目录下
            return str(target_resolved).startswith(str(output_dir_resolved))
        except Exception:
            return False

    @staticmethod
    def _strip_known_suffix(file_name: str) -> str:
        """移除已知后缀，避免重复拼接扩展名."""
        base_name = file_name
        for suffix in [".md", ".html", ".docx", ".txt"]:
            if base_name.lower().endswith(suffix.lower()):
                return base_name[:-len(suffix)]
        return base_name

    @staticmethod
    def _collect_inference_html(infer_messages: Any) -> dict[str, str]:
        """解析 infer_messages，返回可写入文件的 HTML 映射（带资源边界检查）."""
        if not isinstance(infer_messages, list):
            return {}

        html_map: dict[str, str] = {}
        for item in infer_messages:
            # 检查推理图数量上限
            if len(html_map) >= DeepResearchTaskManager.MAX_INFER_MESSAGES:
                logger.warning(
                    "[DeepResearchTaskManager] 推理图数量达到上限 (%d)，跳过后续推理图",
                    DeepResearchTaskManager.MAX_INFER_MESSAGES,
                )
                break

            if not isinstance(item, dict):
                continue

            infer_id = str(item.get("id", "")).strip()
            html_base64 = item.get("html_base64", "")
            if not infer_id or not html_base64:
                continue

            # 检查单个推理图大小上限
            if len(html_base64) > DeepResearchTaskManager.MAX_SINGLE_HTML_BASE64_BYTES:
                logger.warning(
                    "[DeepResearchTaskManager] 推理图 base64 超过大小上限 (%d bytes)，跳过 infer_id=%s",
                    DeepResearchTaskManager.MAX_SINGLE_HTML_BASE64_BYTES,
                    infer_id,
                )
                continue

            try:
                html_content = base64.b64decode(html_base64).decode("utf-8")
            except Exception as exc:
                logger.warning(
                    "[DeepResearchTaskManager] Failed to decode inference html. infer_id=%s error=%s",
                    infer_id,
                    exc,
                )
                continue

            if html_content.strip():
                html_map[infer_id] = html_content
        return html_map

    @staticmethod
    def _sanitize_html(html_content: str) -> str:
        """净化HTML内容，移除危险元素和属性.

        Args:
            html_content: 原始HTML内容

        Returns:
            安全的HTML内容
        """
        import re as html_re

        # 移除 script 标签及其内容
        html_content = html_re.sub(
            r'<script[^>]*>.*?</script>',
            '',
            html_content,
            flags=html_re.IGNORECASE | html_re.DOTALL
        )

        # 移除危险标签: iframe, object, embed, applet, form
        dangerous_tags = ['iframe', 'object', 'embed', 'applet', 'form', 'meta', 'link', 'base']
        for tag in dangerous_tags:
            html_content = html_re.sub(
                rf'<{tag}[^>]*>.*?</{tag}>',
                '',
                html_content,
                flags=html_re.IGNORECASE | html_re.DOTALL
            )
            html_content = html_re.sub(
                rf'<{tag}[^>]*>',
                '',
                html_content,
                flags=html_re.IGNORECASE
            )

        html_content = html_re.sub(
            r'\s+on[a-z]+\s*=\s*["\'][^"\']*["\']',
            '',
            html_content,
            flags=html_re.IGNORECASE
        )
        html_content = html_re.sub(
            r'\s+on[a-z]+\s*=\s*[^\s>]+',
            '',
            html_content,
            flags=html_re.IGNORECASE
        )

        dangerous_schemes = ['javascript:', 'vbscript:', 'data:text/html']
        url_attrs_pattern = 'href|src|action|formaction|data|poster'
        for scheme in dangerous_schemes:
            # 带引号的 URL
            html_content = html_re.sub(
                rf'({url_attrs_pattern})\s*=\s*["\']\s*{scheme}[^"\']*["\']',
                '',
                html_content,
                flags=html_re.IGNORECASE
            )
            # 不带引号的 URL
            html_content = html_re.sub(
                rf'({url_attrs_pattern})\s*=\s*{scheme}[^\s>]*',
                '',
                html_content,
                flags=html_re.IGNORECASE
            )

        return html_content

    @staticmethod
    def _build_report_content(data: Any, report_file: str) -> tuple[str, str | None, str | None]:
        """根据 DeepResearch 结果构建最终落盘的报告内容（带资源边界检查）."""
        infer_dir = None
        chart_dir = None
        if isinstance(data, dict) and "response_content" in data:
            report_content = data.get("response_content", "")
            if report_content == "":
                raise ValueError("response_content is empty")

            # 检查报告内容大小上限
            data_for_bundle = data
            content_bytes = len(report_content.encode("utf-8"))
            if content_bytes > DeepResearchTaskManager.MAX_RESPONSE_CONTENT_BYTES:
                logger.warning(
                    "[DeepResearchTaskManager] 报告内容超过大小上限 (%d bytes > %d bytes)，将截断",
                    content_bytes,
                    DeepResearchTaskManager.MAX_RESPONSE_CONTENT_BYTES,
                )
                # 截断而不是拒绝，避免整个任务失败
                data_for_bundle = dict(data)
                data_for_bundle["response_content"] = report_content[
                    : DeepResearchTaskManager.MAX_RESPONSE_CONTENT_BYTES // 2
                ]

            bundle = build_report_bundle(
                data_for_bundle,
                report_file,
                max_infer_messages=DeepResearchTaskManager.MAX_INFER_MESSAGES,
                max_single_html_base64_bytes=DeepResearchTaskManager.MAX_SINGLE_HTML_BASE64_BYTES,
            )
            return bundle.markdown_text, bundle.infer_dir, bundle.chart_dir

        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, indent=2), infer_dir, chart_dir
        return str(data), infer_dir, chart_dir

    @staticmethod
    def _write_report_artifacts(
        data: Any,
        file_name: str,
        output_dir: str = SAVE_REPORT_PATH,
        *,
        task_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> dict[str, str]:
        """写出 markdown/html/docx 报告及推理图目录（支持协作取消）."""
        # 检查取消状态
        if cancel_event and cancel_event.is_set():
            logger.info("[DeepResearchTaskManager] 任务已取消，跳过报告写出 task_id=%s", task_id)
            return {}

        os.makedirs(output_dir, exist_ok=True)

        # 文件名安全校验和净化
        try:
            safe_base_name = DeepResearchTaskManager._validate_and_sanitize_filename(file_name)
        except ValueError as exc:
            logger.warning(
                "[DeepResearchTaskManager] 文件名校验失败，使用默认文件名。file_name=%s error=%s",
                file_name,
                exc,
            )
            safe_base_name = f"report_{task_id or 'default'}"


        report_file = os.path.join(output_dir, 
                                   f"report_{safe_base_name}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        report_file = report_file.replace("\\", "/")

        # 路径 containment 校验
        if not DeepResearchTaskManager._verify_path_containment(output_dir, report_file):
            raise ValueError(f"生成的报告路径逃逸出输出目录: {report_file}")

        report_file_md = f"{report_file}.md"
        report_file_html = f"{report_file}.html"
        report_file_docx = f"{report_file}.docx"

        report_content, infer_dir, chart_dir = DeepResearchTaskManager._build_report_content(data, report_file)

        # 再次检查取消状态
        if cancel_event and cancel_event.is_set():
            logger.info("[DeepResearchTaskManager] 任务已取消，跳过报告文件写入 task_id=%s", task_id)
            return {}

        with open(report_file_md, "w", encoding="utf-8") as f:
            f.write(report_content)

        artifacts = {"md": report_file_md}
        if infer_dir:
            artifacts["infer_dir"] = infer_dir
        if chart_dir:
            artifacts["chart_dir"] = chart_dir

        # HTML转换前检查取消状态
        if cancel_event and cancel_event.is_set():
            logger.info("[DeepResearchTaskManager] 任务已取消，跳过HTML转换 task_id=%s", task_id)
            return artifacts

        try:
            convert_md_to_html(report_file_md, report_file_html)
        except Exception as exc:
            logger.warning(
                "[DeepResearchTaskManager] Optional html report generation failed. "
                "task_id=%s output=%s error=%s",
                task_id,
                report_file_html,
                exc,
            )
        else:
            artifacts["html"] = report_file_html

        # DOCX转换前检查取消状态
        if cancel_event and cancel_event.is_set():
            logger.info("[DeepResearchTaskManager] 任务已取消，跳过DOCX转换 task_id=%s", task_id)
            return artifacts

        try:
            convert_md_to_docx(report_file_md, report_file_docx)
        except Exception as exc:
            logger.warning(
                "[DeepResearchTaskManager] Optional docx report generation failed. "
                "task_id=%s output=%s error=%s",
                task_id,
                report_file_docx,
                exc,
            )
        else:
            artifacts["docx"] = report_file_docx

        return artifacts

    @staticmethod
    def _format_report_result(report_paths: dict[str, str]) -> str:
        """生成格式化后的报告结果路径字符串."""
        parts = [f"markdown报告已保存到{report_paths['md']}\n"]
        if report_paths.get("html"):
            parts.append(f"html报告已保存到{report_paths['html']}\n")
        if report_paths.get("docx"):
            parts.append(f"docx报告已保存到{report_paths['docx']}\n")
        if report_paths.get("infer_dir"):
            parts.append(f"溯源推理图已保存到{report_paths['infer_dir']}\n")
        if report_paths.get("chart_dir"):
            parts.append(f"图表图片已保存到{report_paths['chart_dir']}\n")
        return "".join(parts)

    async def _run_jiuwen_workflow(
            self,
            query: str,
            agent_config: Dict,
            report_template: str,
            task_id: str = "",
            log_output_dir: str = "",
    ) -> Any:
        """运行 openJiuwen-DeepResearch 工作流，捕获执行日志到任务目录.

        Args:
            query: 用户查询字符串
            agent_config: Agent 配置字典
            report_template: 报告模板
            task_id: 任务 ID（用于日志文件命名）
            log_output_dir: 日志输出目录路径（默认为项目日志目录下的 DeepResearch 子文件夹）

        Returns:
            最终研究报告内容
        """
        # === 日志捕获设置 ===
        log_capture_context = self._setup_log_capture(task_id, log_output_dir)

        try:
            agent_factory = AgentFactory()
            agent = agent_factory.create_agent(agent_config)

            last_report = None
            chunk_count = 0

            async for chunk in agent.run(
                    message=query,
                    conversation_id=str(secrets.token_hex(16)),
                    report_template=report_template,
                    interrupt_feedback="",
                    agent_config=agent_config
            ):
                chunk_count += 1
                logger.debug("[DeepResearchTaskManager] Stream chunk #%d from node", chunk_count)
                chunk_content = json.loads(chunk)
                report_result = parse_endnode_content(chunk_content)
                if report_result:
                    last_report = report_result
                    logger.info(
                        "[DeepResearchTaskManager] Final report received at chunk #%d",
                        chunk_count,
                        extra={'user_visible': 'critical'}
                    )

            logger.info(
                "[DeepResearchTaskManager] Workflow completed. Total chunks: %d",
                chunk_count
            )

            return last_report

        finally:
            # === 清理日志捕获 ===
            self._teardown_log_capture(log_capture_context)

    @staticmethod
    def _setup_log_capture(
            task_id: str,
            log_output_dir: str = "",
    ) -> Dict[str, Any]:
        """设置日志捕获机制，使用 openjiuwen_deepsearch 内部 LogManager 初始化日志系统.

        Args:
            task_id: 任务 ID（用于关联日志）
            log_output_dir: 日志输出目录路径（默认为项目日志目录下的 DeepResearch 子文件夹）

        Returns:
            清理上下文字典，包含：
            - session_id_token: session_id_ctx.reset 所需的 token
            - original_safe_base: 原 LogManager._SAFE_BASE 值
            - log_dir: 日志输出目录

        日志输出结构（由 LogManager.init() 创建）：
            {log_output_dir}/
            ├── common/
            │   ├── common.log          # 通用日志
            │   └── common_warning.log  # 警告及以上日志
            ├── metrics/
            │   └── metrics.log         # 性能打点日志
            └── interface/
            │   └── deepsearch_interface.log  # 接口日志
        """
        context: Dict[str, Any] = {
            "session_id_token": None,
            "original_safe_base": None,
            "log_dir": None,
        }

        # 默认使用项目日志目录下的 DeepResearch 子文件夹
        if not log_output_dir:
            log_output_dir = str(get_logs_dir() / "DeepResearch")
        if not task_id:
            task_id = f"anonymous_{secrets.token_hex(4)}"

        # 1. 设置 session_id_ctx（让 openjiuwen_deepsearch 内部日志关联任务）
        session_id_token = session_id_ctx.set(task_id)
        context["session_id_token"] = session_id_token

        logger.info(
            "[DeepResearchTaskManager] Task execution session_id: %s",
            task_id,
            extra={'user_visible': 'critical'}
        )

        # 2. 创建任务专属日志目录
        log_dir = Path(log_output_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        context["log_dir"] = str(log_dir)

        # 3. 处理 LogManager 单例和路径限制
        # 保存原始 _SAFE_BASE 值以便恢复
        context["original_safe_base"] = LogManager._SAFE_BASE

        # 动态修改 _SAFE_BASE 为任务目录（绕过路径安全限制）
        LogManager._SAFE_BASE = str(log_dir)

        # 重置 _initialized 以允许重新初始化
        LogManager._initialized = False

        # 4. 清理之前配置的 handlers（避免重复）
        # LogManager.init() 会清理 root logger handlers，但不会清理 metrics/interface
        metrics_logger = logging.getLogger("metrics")
        interface_logger = logging.getLogger("deepsearch_interface")

        for handler in list(metrics_logger.handlers):
            handler.flush()
            handler.close()
        metrics_logger.handlers.clear()

        for handler in list(interface_logger.handlers):
            handler.flush()
            handler.close()
        interface_logger.handlers.clear()

        # 5. 使用 LogManager.init() 初始化完整日志系统
        LogManager.init(
            log_dir=str(log_dir),
            level="INFO",
            max_bytes=10 * 1024 * 1024,  # 10MB
            backup_count=5,
            is_sensitive=False,
        )

        logger.info(
            "[DeepResearchTaskManager] LogManager initialized with log_dir=%s level=INFO",
            log_dir
        )

        return context

    @staticmethod
    def _teardown_log_capture(context: Dict[str, Any]) -> None:
        """清理日志捕获机制，恢复原日志配置.

        Args:
            context: _setup_log_capture 返回的清理上下文
        """
        # 1. 重置 session_id_ctx
        session_id_token = context.get("session_id_token")
        if session_id_token is not None:
            session_id_ctx.reset(session_id_token)
            logger.debug("[DeepResearchTaskManager] Reset session_id_ctx")

        # 2. 重置 LogManager 状态（允许下次任务重新初始化）
        LogManager._initialized = False

        # 3. 恢复原始 _SAFE_BASE
        original_safe_base = context.get("original_safe_base")
        if original_safe_base is not None:
            LogManager._SAFE_BASE = original_safe_base

        # 4. 清理所有 handlers（root logger、metrics、interface）
        # 清理 root logger handlers（由 setup_common_logger 创建）
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            handler.flush()
            handler.close()
        root_logger.handlers.clear()
        root_logger.setLevel(logging.WARNING)

        # 清理 metrics_logger handlers（由 setup_metrics_logger 创建）
        metrics_logger = logging.getLogger("metrics")
        for handler in list(metrics_logger.handlers):
            handler.flush()
            handler.close()
        metrics_logger.handlers.clear()

        # 清理 interface_logger handlers（由 setup_interface_logger 创建）
        interface_logger = logging.getLogger("deepsearch_interface")
        for handler in list(interface_logger.handlers):
            handler.flush()
            handler.close()
        interface_logger.handlers.clear()

        logger.debug(
            "[DeepResearchTaskManager] Cleaned up all LogManager handlers"
        )

    async def _execute_task(
        self,
        task_id: str,
        query: str,
        file_name: str,
    ) -> None:
        """执行 DeepResearch 任务（后台协程）."""
        task = self._tasks[task_id]
        task.started_at = time.time()
        task.status = TaskStatus.RUNNING

        logger.debug(
            format_session_log(
                task.session_id,
                f"[DeepResearchTaskManager] 任务路由信息 task_id={task_id} "
                f"channel_id={task.channel_id} request_id={task.request_id}",
            )
        )

        logger.info(
            format_session_log(
                task.session_id,
                "[DeepResearchTaskManager] 开始执行任务 task_id=%s query=%s",
            ),
            task_id,
            query[:80] + "..." if len(query) > 80 else query,
            extra={'user_visible': 'critical'}
        )

        try:
            # 1. 加载配置
            config = DeepResearchTaskManager._load_config()

            # 2. 验证配置
            config_valid, config_msg = DeepResearchTaskManager._validate_config(config)
            if not config_valid:
                raise ValueError(config_msg)

            # 3. 设置 SSL 配置
            os.environ["LLM_SSL_VERIFY"] = "false"
            os.environ["LLM_SSL_CERT"] = ""
            os.environ["TOOL_SSL_VERIFY"] = "false"
            os.environ["TOOL_SSL_CERT"] = ""

            config_extension = {
                "extra_body": {
                    "thinking": {
                        "type": "disabled"
                    }
                }
            }

            # 4. 解析 LLM 配置
            current_agent_config = Config().agent_config.model_dump()
            current_agent_config["llm_config"]["general"] = {}
            current_agent_config["llm_config"]["general"]["model_name"] = config["LLM_MODEL_NAME"]
            current_agent_config["llm_config"]["general"]["model_type"] = config["LLM_MODEL_TYPE"]
            current_agent_config["llm_config"]["general"]["base_url"] = config["LLM_BASE_URL"]
            current_agent_config["llm_config"]["general"]["extension"] = config_extension
            current_agent_config["llm_config"]["general"]["api_key"] = bytearray(config["LLM_API_KEY"],
                                                                                 encoding="utf-8")
            current_agent_config["llm_config"]["general"]["verify_ssl"] = False

            # 5. 解析搜索引擎配置
            current_agent_config["web_search_engine_config"]["search_engine_name"] = config["WEB_SEARCH_ENGINE_NAME"]
            current_agent_config["web_search_engine_config"]["search_api_key"] = bytearray(
                config["WEB_SEARCH_API_KEY"], encoding="utf-8"
            )
            current_agent_config["web_search_engine_config"]["search_url"] = config["WEB_SEARCH_URL"]
            current_agent_config["web_search_engine_config"]["max_web_search_results"] = (
                config["MAX_WEB_SEARCH_RESULTS"]
            )
            current_agent_config["outliner_max_section_num"] = int(config["OUTLINER_MAX_SECTION_NUM"])

            current_agent_config["workflow_human_in_the_loop"] = config["WORKFLOW_HUMAN_IN_THE_LOOP"]
            current_agent_config["outline_interaction_enabled"] = config["OUTLINE_INTERACTION_ENABLED"]
            current_agent_config["source_tracer_infer_switch"] = config["SOURCE_TRACER_INFER_SWITCHES"]
            current_agent_config["vlm_chart_generator_enable"] = config["VLM_CHART_GENERATOR_ENABLE"]
            current_agent_config["vlm_chart_generator_max_iterations"] = config["VLM_CHART_GENERATOR_MAX_ITERATIONS"]
            if config["EXECUTION_METHOD"] == ExecutionMethod.DEPENDENCY_DRIVING.value:
                current_agent_config["execution_method"] = ExecutionMethod.DEPENDENCY_DRIVING.value
            else:
                current_agent_config["execution_method"] = ExecutionMethod.PARALLEL.value

            # 6. 执行工作流（带日志捕获）
            # 报告目录：用于保存报告文件
            report_dir = os.path.join(get_agent_workspace_dir(), "reports")
            # 日志目录：使用项目日志目录下的 DeepResearch 子文件夹（默认行为）
            data = await self._run_jiuwen_workflow(
                query,
                current_agent_config,
                "",
                task_id=task_id,
                log_output_dir="",  # 使用默认日志目录 get_logs_dir() / "DeepResearch"
            )

            if data:
                report_paths = await asyncio.to_thread(
                    self._write_report_artifacts,
                    data,
                    file_name,
                    report_dir,
                    task_id=task_id,
                    cancel_event=task.cancel_event,
                )
                result = self._format_report_result(report_paths)

                """
                    result = (
                    f"markdown报告已保存到{report_paths['md']}; "
                    f"html报告已保存到{report_paths['html']}; "
                    f"docx报告已保存到{report_paths['docx']}"
                )
                    if report_paths["infer_dir"]:
                    result += f"; 溯源推理图已保存到{report_paths['infer_dir']}"

                """
                task.status = TaskStatus.COMPLETED
                task.result = result
                logger.info(
                    format_session_log(
                        task.session_id,
                        "[DeepResearchTaskManager] 任务完成 task_id=%s result=%s",
                    ),
                    task_id,
                    result,
                    extra={'user_visible': 'critical'}
                )
            else:
                raise ValueError("DeepResearch 返回空结果")

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            task.error = "任务已取消"
            logger.info(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 任务已取消 task_id=%s",
                ),
                task_id,
                extra={'user_visible': 'critical'}
            )

        except Exception as exc:
            task.status = TaskStatus.ERROR
            task.error = str(exc)
            logger.exception(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 任务异常 task_id=%s error=%s",
                ),
                task_id,
                exc,
                extra={'user_visible': 'critical'}
            )

        finally:
            task.completed_at = time.time()
            self._task_handles.pop(task_id, None)

            # 发送 WebSocket 通知

            await self._notify_completion(task)

    async def _notify_completion(self, task: DeepResearchTask) -> None:
        """通过 WebSocket 发送任务完成通知."""
        if task.status == TaskStatus.COMPLETED:
            payload_status = "completed"
            message = task.result
        elif task.status == TaskStatus.CANCELLED:
            payload_status = "cancelled"
            message = "任务已取消"
        else:
            payload_status = "error"
            message = task.error

        payload = {
            "event_type": "deepresearch.task_completed",
            "task_id": task.task_id,
            "query": task.query,
            "status": payload_status,
            "result": message,
            "created_at": DeepResearchTask.format_timestamp(task.created_at) if task.created_at else None,
            "started_at": DeepResearchTask.format_timestamp(task.started_at) if task.started_at else None,
            "completed_at": DeepResearchTask.format_timestamp(task.completed_at) if task.completed_at else None,
        }

        msg = {
            "request_id": task.request_id,
            "channel_id": task.channel_id,
            "session_id": task.session_id,
            "payload": payload,
            "is_complete": False,
        }

        logger.debug(
            format_session_log(
                task.session_id,
                "[DeepResearchTaskManager] 发送任务完成通知 task_id=%s status=%s",
            ),
            task.task_id,
            payload_status,
        )

        try:
            await self._gateway_push.send_push(msg)
        except Exception as exc:
            logger.warning(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 发送 WebSocket 通知失败 task_id=%s error=%s",
                ),
                task.task_id,
                exc,
            )

    async def create_task(
        self,
        query: str,
        file_name: str,
        session_id: str = "",
        channel_id: str = "",
        request_id: str = "",
    ) -> str:
        """创建并启动 DeepResearch 任务.

        Args:
            query: 研究查询
            file_name: 报告文件名，不带后缀
            session_id: 会话 ID（用于通知）
            channel_id: 渠道 ID（用于通知）
            request_id: 请求 ID（用于通知）

        Returns:
            任务 ID

        Raises:
            RuntimeError: 当达到任务数量上限时
        """
        # 检查全局任务数量上限
        running_count = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
        if running_count >= DeepResearchTaskManager.MAX_ACTIVE_TASKS:
            raise RuntimeError(
                f"活跃任务数已达上限 ({DeepResearchTaskManager.MAX_ACTIVE_TASKS})，"
                f"请等待现有任务完成后再创建新任务"
            )

        # 检查保留任务数量上限
        if len(self._tasks) >= DeepResearchTaskManager.MAX_TOTAL_TASKS:
            # 清理已完成的旧任务
            completed_tasks = [
                tid for tid, t in self._tasks.items()
                if t.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.ERROR)
            ]
            for tid in completed_tasks[: len(self._tasks) - DeepResearchTaskManager.MAX_TOTAL_TASKS + 1]:
                self._tasks.pop(tid, None)
                logger.info("[DeepResearchTaskManager] 清理已完成旧任务 task_id=%s", tid)

        effective_session_id = session_id or "__anonymous__"
        session_tasks = sum(
            1 for t in self._tasks.values()
            if
            (t.session_id == effective_session_id or (t.session_id == "" and effective_session_id == "__anonymous__"))
            and t.status == TaskStatus.RUNNING
        )
        if session_tasks >= DeepResearchTaskManager.MAX_TASKS_PER_SESSION:
            raise RuntimeError(
                f"当前会话 ({session_id or '匿名'}) 活跃任务数已达上限 "
                f"({DeepResearchTaskManager.MAX_TASKS_PER_SESSION})"
            )

        task_id = f"dr_{time.monotonic_ns()}_{secrets.token_hex(4)}"

        task = DeepResearchTask(
            task_id=task_id,
            query=query,
            file_name=file_name,
            status=TaskStatus.RUNNING,
            created_at=time.time(),
            session_id=session_id,
            channel_id=channel_id,
            request_id=request_id,
        )

        self._tasks[task_id] = task

        # 创建后台协程，显式传递路由参数
        coro = self._execute_task(
            task_id=task_id,
            query=query,
            file_name=file_name,
        )
        task_handle = asyncio.create_task(coro)
        self._task_handles[task_id] = task_handle

        logger.info(
            format_session_log(
                session_id,
                "[DeepResearchTaskManager] 创建深度研究任务：%s task_id=%s query=%s channel_id=%s running_count=%d/%d",
            ),
            file_name,
            task_id,
            query[:80] + "..." if len(query) > 80 else query,
            channel_id,
            running_count + 1,
            DeepResearchTaskManager.MAX_ACTIVE_TASKS,
            extra={'user_visible': 'critical'}
        )

        return task_id

    async def get_task_status(
        self,
        task_id: str,
        caller_session_id: str = "",
        caller_channel_id: str = "",
    ) -> Dict[str, Any] | None:
        """获取任务状态.

        Args:
            task_id: 任务 ID
            caller_session_id: 调用者会话 ID（用于授权校验）
            caller_channel_id: 调用者渠道 ID（用于授权校验）

        Returns:
            任务信息字典，如果任务不存在或无权访问则返回 None
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None

        # 授权校验：只允许任务所有者访问
        if not self._check_task_ownership(task, caller_session_id, caller_channel_id):
            return None

        return {
            "task_id": task.task_id,
            "query": task.query,
            "file_name": task.file_name,
            "status": task.status.value,
            "created_at": DeepResearchTask.format_timestamp(task.created_at) if task.created_at else None,
            "started_at": DeepResearchTask.format_timestamp(task.started_at) if task.started_at else None,
            "completed_at": DeepResearchTask.format_timestamp(task.completed_at) if task.completed_at else None,
            "result": task.result if task.status == TaskStatus.COMPLETED else "",
            "error": task.error if task.status == TaskStatus.ERROR else "",
        }

    @staticmethod
    def _check_task_ownership(
            task: DeepResearchTask,
            caller_session_id: str,
            caller_channel_id: str,
    ) -> bool:
        """校验任务所有权（仅基于 session_id）.

        安全修复：移除 channel_id 独立授权逻辑，防止同频道跨会话越权访问。
        channel_id 仅用于日志记录和审计，不参与授权决策。

        Args:
            task: 任务对象
            caller_session_id: 调用者会话 ID（唯一授权依据）
            caller_channel_id: 调用者渠道 ID（仅用于日志审计）

        Returns:
            是否有权限访问该任务
        """
        # 安全策略：仅以 session_id 作为授权依据
        # 1. 任务必须有明确的 session_id（匿名任务拒绝访问）
        if not task.session_id:
            logger.warning(
                "[DeepResearchTaskManager] 拒绝访问无归属任务 task_id=%s "
                "(安全策略：匿名任务不允许访问)",
                task.task_id,
            )
            return False

        # 2. 调用者必须提供 session_id
        if not caller_session_id:
            logger.warning(
                "[DeepResearchTaskManager] 拒绝访问：调用者未提供 session_id task_id=%s",
                task.task_id,
            )
            return False

        # 3. 仅匹配 session_id（移除 channel_id 独立授权）
        if task.session_id == caller_session_id:
            return True

        # 4. session_id 不匹配，记录审计日志（包含 channel_id 信息）
        logger.warning(
            format_session_log(
                task.session_id,
                "[DeepResearchTaskManager] 越权访问被阻止 task_id=%s "
                "task_session=%s caller_session=%s caller_channel=%s",
            ),
            task.task_id,
            task.session_id,
            caller_session_id,
            caller_channel_id,
        )
        return False

    async def list_tasks(
        self,
        status_filter: str | None = None,
        caller_session_id: str = "",
        caller_channel_id: str = "",
    ) -> List[Dict[str, Any]]:
        """列出任务（仅返回调用者拥有的任务）.

        Args:
            status_filter: 可选的状态过滤器（running/completed/cancelled/error）
            caller_session_id: 调用者会话 ID（用于授权校验）
            caller_channel_id: 调用者渠道 ID（用于授权校验）

        Returns:
            任务信息列表
        """
        tasks = []
        for task in self._tasks.values():
            # 授权校验：只返回调用者拥有的任务
            if not self._check_task_ownership(task, caller_session_id, caller_channel_id):
                continue

            if status_filter and task.status.value != status_filter:
                continue

            tasks.append({
                "task_id": task.task_id,
                "query": task.query,
                "file_name": task.file_name,
                "status": task.status.value,
                "created_at": DeepResearchTask.format_timestamp(task.created_at) if task.created_at else None,
                "started_at": DeepResearchTask.format_timestamp(task.started_at) if task.started_at else None,
                "completed_at": DeepResearchTask.format_timestamp(task.completed_at) if task.completed_at else None,
                "result": task.result if task.status == TaskStatus.COMPLETED else "",
                "error": task.error if task.status == TaskStatus.ERROR else "",
            })

        # 按创建时间倒序排列
        tasks.sort(key=lambda x: x["created_at"], reverse=True)
        return tasks

    async def cancel_task(
        self,
        task_id: str,
        caller_session_id: str = "",
        caller_channel_id: str = "",
    ) -> bool:
        """取消任务（仅允许任务所有者取消）.

        Args:
            task_id: 任务 ID
            caller_session_id: 调用者会话 ID（用于授权校验）
            caller_channel_id: 调用者渠道 ID（用于授权校验）

        Returns:
            是否成功取消
        """
        task = self._tasks.get(task_id)
        if task is None:
            logger.warning(
                "[DeepResearchTaskManager] 取消任务失败：任务不存在 task_id=%s" % task_id
            )
            return False

        # 授权校验：只允许任务所有者取消
        if not self._check_task_ownership(task, caller_session_id, caller_channel_id):
            logger.warning(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 取消任务失败：无权访问 task_id=%s",
                ),
                task_id,
            )
            return False

        task_handle = self._task_handles.get(task_id)
        if task_handle is None:
            logger.info(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 任务已结束，无需取消 task_id=%s",
                ),
                task_id,
            )
            return False

        if task_handle.done():
            logger.info(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 任务已完成，无需取消 task_id=%s",
                ),
                task_id,
            )
            return False

        # 设置取消事件，通知线程中的报告转换停止
        if task.cancel_event:
            task.cancel_event.set()
            logger.info(
                format_session_log(
                    task.session_id,
                    "[DeepResearchTaskManager] 已设置取消事件 task_id=%s",
                ),
                task_id,
            )

        task_handle.cancel()
        try:
            await asyncio.gather(task_handle, return_exceptions=True)
        except asyncio.CancelledError:
            pass

        logger.info(
            format_session_log(
                task.session_id,
                "[DeepResearchTaskManager] 已取消任务 task_id=%s",
            ),
            task_id,
            extra={'user_visible': 'critical'}
        )
        return True

    async def get_task_result(
        self,
        task_id: str,
        caller_session_id: str = "",
        caller_channel_id: str = "",
    ) -> str | None:
        """获取任务结果（仅允许任务所有者获取）.

        Args:
            task_id: 任务 ID
            caller_session_id: 调用者会话 ID（用于授权校验）
            caller_channel_id: 调用者渠道 ID（用于授权校验）

        Returns:
            任务结果字符串，如果任务未完成或无权访问则返回 None
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None

        # 授权校验：只允许任务所有者获取结果
        if not self._check_task_ownership(task, caller_session_id, caller_channel_id):
            return None

        if task.status != TaskStatus.COMPLETED:
            return None

        return task.result

    async def run_task_and_wait(
        self,
        query: str,
        file_name: str,
        session_id: str = "",
        channel_id: str = "",
        request_id: str = "",
    ) -> Dict[str, Any]:
        """创建任务并等待执行结束，适合 CLI 或脚本入口直接调用."""
        task_id = await self.create_task(
            query=query,
            file_name=file_name,
            session_id=session_id,
            channel_id=channel_id,
            request_id=request_id,
        )

        task_handle = self._task_handles.get(task_id)
        if task_handle is not None:
            await asyncio.gather(task_handle, return_exceptions=True)

        task_info = await self.get_task_status(task_id)
        if task_info is None:
            raise RuntimeError(f"DeepResearch task not found after execution: {task_id}")
        return task_info

    async def run_task_direct(
        self,
        query: str,
        file_name: str,
        session_id: str = "",
        channel_id: str = "",
        request_id: str = "",
    ) -> str:
        """直接执行深度研究任务并阻塞等待完成，不提交到任务池.

        与 create_task 的区别：
        - 不创建 DeepResearchTask 对象
        - 不占用任务池资源（_tasks、_task_handles）
        - 不受 MAX_ACTIVE_TASKS、MAX_TASKS_PER_SESSION 等限制
        - 不发送 WebSocket 完成通知
        - 直接在当前协程中执行，阻塞等待完成

        适用场景：
        - Agent 会话中需要即时获取结果
        - 不需要异步后台执行的场景

        Args:
            query: 研究查询
            file_name: 报告文件名，不带后缀
            session_id: 会话 ID（仅用于日志关联）
            channel_id: 渠道 ID（仅用于日志关联）
            request_id: 请求 ID（仅用于日志关联）

        Returns:
            报告保存路径信息字符串

        Raises:
            ValueError: 配置验证失败或执行结果为空
            Exception: 工作流执行过程中的其他异常
        """
        # 生成临时任务 ID（仅用于日志和报告文件命名）
        temp_task_id = f"dr_blocking_{time.monotonic_ns()}_{secrets.token_hex(4)}"

        logger.info(
            format_session_log(
                session_id,
                "[DeepResearchTaskManager] 开始阻塞执行深度研究任务 temp_task_id=%s query=%s channel_id=%s",
            ),
            temp_task_id,
            query[:80] + "..." if len(query) > 80 else query,
            channel_id,
            extra={'user_visible': 'critical'}
        )

        # 1. 加载配置
        config = DeepResearchTaskManager._load_config()

        # 2. 验证配置
        config_valid, config_msg = DeepResearchTaskManager._validate_config(config)
        if not config_valid:
            raise ValueError(config_msg)

        # 3. 设置 SSL 配置
        os.environ["LLM_SSL_VERIFY"] = "false"
        os.environ["LLM_SSL_CERT"] = ""
        os.environ["TOOL_SSL_VERIFY"] = "false"
        os.environ["TOOL_SSL_CERT"] = ""

        config_extension = {
            "extra_body": {
                "thinking": {
                    "type": "disabled"
                }
            }
        }

        # 4. 解析 LLM 配置
        current_agent_config = Config().agent_config.model_dump()
        current_agent_config["llm_config"]["general"] = {}
        current_agent_config["llm_config"]["general"]["model_name"] = config["LLM_MODEL_NAME"]
        current_agent_config["llm_config"]["general"]["model_type"] = config["LLM_MODEL_TYPE"]
        current_agent_config["llm_config"]["general"]["base_url"] = config["LLM_BASE_URL"]
        current_agent_config["llm_config"]["general"]["extension"] = config_extension
        current_agent_config["llm_config"]["general"]["api_key"] = bytearray(config["LLM_API_KEY"],
                                                                              encoding="utf-8")
        current_agent_config["llm_config"]["general"]["verify_ssl"] = False

        # 5. 解析搜索引擎配置
        current_agent_config["web_search_engine_config"]["search_engine_name"] = config["WEB_SEARCH_ENGINE_NAME"]
        current_agent_config["web_search_engine_config"]["search_api_key"] = bytearray(
            config["WEB_SEARCH_API_KEY"], encoding="utf-8"
        )
        current_agent_config["web_search_engine_config"]["search_url"] = config["WEB_SEARCH_URL"]
        current_agent_config["web_search_engine_config"]["max_web_search_results"] = (
            config["MAX_WEB_SEARCH_RESULTS"]
        )
        current_agent_config["outliner_max_section_num"] = int(config["OUTLINER_MAX_SECTION_NUM"])

        current_agent_config["workflow_human_in_the_loop"] = config["WORKFLOW_HUMAN_IN_THE_LOOP"]
        current_agent_config["outline_interaction_enabled"] = config["OUTLINE_INTERACTION_ENABLED"]
        current_agent_config["source_tracer_infer_switch"] = config["SOURCE_TRACER_INFER_SWITCHES"]
        current_agent_config["vlm_chart_generator_enable"] = config["VLM_CHART_GENERATOR_ENABLE"]
        current_agent_config["vlm_chart_generator_max_iterations"] = config["VLM_CHART_GENERATOR_MAX_ITERATIONS"]
        if config["EXECUTION_METHOD"] == ExecutionMethod.DEPENDENCY_DRIVING.value:
            current_agent_config["execution_method"] = ExecutionMethod.DEPENDENCY_DRIVING.value
        else:
            current_agent_config["execution_method"] = ExecutionMethod.PARALLEL.value

        # 6. 直接执行工作流（阻塞等待）
        report_dir = os.path.join(get_agent_workspace_dir(), "reports")
        data = await self._run_jiuwen_workflow(
            query,
            current_agent_config,
            "",
            task_id=temp_task_id,
            log_output_dir="",  # 使用默认日志目录
        )

        if not data:
            raise ValueError("DeepResearch 返回空结果")

        # 7. 写出报告文件
        report_paths = await asyncio.to_thread(
            self._write_report_artifacts,
            data,
            file_name,
            report_dir,
            task_id=temp_task_id,
            cancel_event=None,  # 阻塞执行不支持取消
        )

        result = self._format_report_result(report_paths)

        logger.info(
            "[DeepResearchTaskManager] 阻塞执行任务完成 temp_task_id=%s result=%s",
            temp_task_id,
            result,
            extra={'user_visible': 'critical'}
        )

        return result


__all__ = [
    "DeepResearchTaskManager",
    "TaskStatus",
    "DeepResearchTask",
]
