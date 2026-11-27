# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Inject dynamic time/channel/runtime info per model call.

Dynamic content (time, channel, agent, model, language) is decoupled from the
static identity prompt and refreshed on every model call via before_model_call().
"""
from __future__ import annotations

import platform
import subprocess
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from shutil import which
from typing import Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.utils import (
    get_user_workspace_dir,
    get_agent_memory_dir,
    get_agent_registered_skill_dirs,
    get_shared_agent_skills_dirs,
    get_agent_workspace_dir,
    get_deepagent_todo_dir,
    get_deepagent_soul_md_path,
    get_multi_tenant_user_workspace_dir,
)

_CN_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

logger = getLogger(__name__)


class RuntimePromptRail(DeepAgentRail):
    """在 before_model_call 中注入运行时动态 section（时间、运行时信息）。"""

    priority = 5  # 高优先级，确保早于其他 rail 执行

    def __init__(
        self,
        language: str = "cn",
        channel: str = "web",
        timezone_offset: int = 8,
        agent_name: str = "main_agent",
        model_name: str = "gpt-4",
        workspace_dir: Optional[str] = None,
        agent_id: Optional[str] = None,
        service_id: Optional[str] = None,
        custom_home_dir: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._language = language
        self._channel = channel
        self._tz = timezone(timedelta(hours=timezone_offset))
        self._agent_name = agent_name
        self._model_name = model_name
        self._workspace_dir = workspace_dir
        self._agent_id = agent_id
        self._service_id = service_id
        self._request_system_prompt: str = ""
        self._custom_home_dir = custom_home_dir

    def init(self, agent) -> None:
        """从 agent 获取 system_prompt_builder 引用。"""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        """清理注入的 section 并释放引用。"""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("time")
            self.system_prompt_builder.remove_section("runtime")
            self.system_prompt_builder.remove_section("workspace")
            self.system_prompt_builder.remove_section("soul")
            self.system_prompt_builder.remove_section("request_system_prompt")
        self.system_prompt_builder = None

    def set_language(self, language: str) -> None:
        """per-request 更新语言。"""
        self._language = language

    def set_channel(self, channel: str) -> None:
        """per-request 更新频道。"""
        self._channel = channel

    def set_custom_home_dir(self, custom_home_dir: str | None) -> None:
        """更新自定义 home 目录（用于 SOUL.md 加载）。

        Args:
            custom_home_dir: 自定义 home 目录路径
        """
        self._custom_home_dir = custom_home_dir

    def set_workspace_dir(self, workspace_dir: Optional[str]) -> None:
        """per-request 更新工作空间目录。"""
        self._workspace_dir = workspace_dir

    def set_request_system_prompt(self, prompt: Optional[str]) -> None:
        """per-request 更新 system prompt 追加内容。"""
        value = prompt.strip() if isinstance(prompt, str) else ""
        self._request_system_prompt = value

    def _read_file(self, file_path: str) -> Optional[str]:
        """读取文件内容，失败返回 None"""
        if not file_path:
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return content if content else None
        except Exception as e:
            logger.debug("[RuntimePromptRail] file read failed: %s: %s", file_path, e)
            return None

    def _load_soul_md(self) -> Optional[str]:
        """加载 SOUL.md 内容，优先从定制 home_dir 加载"""
        if self._custom_home_dir:
            custom_path = Path(self._custom_home_dir) / "SOUL.md"
            content = self._read_file(str(custom_path))
            if content:
                logger.info("[RuntimePromptRail] SOUL.md loaded from custom: %s", custom_path)
                return content
        
        default_path = get_deepagent_soul_md_path()
        content = self._read_file(str(default_path))
        if content:
            logger.debug("[RuntimePromptRail] SOUL.md loaded from default: %s", default_path)
        return content

    @staticmethod
    def _get_git_branch() -> str:
        """获取当前 git 分支名，失败时返回 N/A。"""
        git_bin = which("git")
        if not git_bin:
            return "N/A"

        try:
            result = subprocess.run(
                [git_bin, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            return "N/A"

        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "N/A"

    def _get_workspace_dirs(self) -> dict[str, str]:
        """获取工作空间目录路径，支持多租户。"""
        if self._agent_id and self._service_id:
            # 多租户模式
            base_workspace = get_multi_tenant_user_workspace_dir(self._service_id, self._agent_id)
            if base_workspace:
                workspace_root = base_workspace / "jiuwenclaw_workspace"
                return {
                    "config": str(base_workspace / "config"),
                    "workspace": self._workspace_dir or str(workspace_root), # 优先使用请求中的 workspace_dir
                    "memory": str(workspace_root / "memory"),
                    "skills": ", ".join(str(d) for d in get_shared_agent_skills_dirs())
                    if get_shared_agent_skills_dirs() else str(workspace_root / "skills"),
                    "todo": str(workspace_root / "todo"),
                }
        
        # 单租户模式
        return {
            "config": str(get_user_workspace_dir() / "config"),
            "workspace": self._workspace_dir or str(get_agent_workspace_dir()),
            "memory": str(get_agent_memory_dir()),
            "skills": ", ".join(str(d) for d in get_agent_registered_skill_dirs()),
            "todo": str(get_deepagent_todo_dir()),
        }

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """每次 model call 注入最新的时间和运行时信息。"""
        if not self.system_prompt_builder:
            return

        now = datetime.now(tz=self._tz)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        current_year = now.strftime("%Y")

        if self._language == "cn":
            time_content = (
                f"# 当前日期与时间\n\n"
                f"- 当前时间：{now_str}\n"
                f"- 当前年份：{current_year}\n"
                "- 当用户询问“最新、当前、今年、本年、实时、近期”等信息并需要搜索时，"
                "搜索 query 必须优先使用当前年份或日期"
            )
        else:
            time_content = (
                f"# Current Date & Time\n\n"
                f"- Current time: {now_str}\n"
                f"- Current year: {current_year}\n"
                "- When the user asks for latest/current/this-year/recent information and search is needed, "
                "search queries must prefer the current year or date."
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="time",
            content={"cn": time_content, "en": time_content},
            priority=92,
        ))

        plat = f"{platform.system()} {platform.machine()}"
        python_ver = platform.python_version()
        git_branch = self._get_git_branch()

        if self._language == "cn":
            runtime_content = (
                "# 运行时\n\n"
                f"- 平台：{plat}\n"
                f"- Python：{python_ver}\n"
                f"- 模型：{self._model_name}\n"
                f"- Git 分支：{git_branch}\n"
                f"- Agent：{self._agent_name}\n"
                f"- 频道：{self._channel}\n"
                f"- 语言：{self._language}"
            )
        else:
            runtime_content = (
                "# Runtime\n\n"
                f"- Platform: {plat}\n"
                f"- Python: {python_ver}\n"
                f"- Model: {self._model_name}\n"
                f"- Git branch: {git_branch}\n"
                f"- Agent: {self._agent_name}\n"
                f"- Channel: {self._channel}\n"
                f"- Language: {self._language}"
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="runtime",
            content={"cn": runtime_content, "en": runtime_content},
            priority=95,
        ))

        # 使用多租户感知的方法获取路径
        dirs = self._get_workspace_dirs()
        config_dir = dirs["config"]
        resolved_workspace = dirs["workspace"]
        memory_dir = dirs["memory"]
        skills_dir = dirs["skills"]
        todo_dir = dirs["todo"]

        if self._language == "cn":
            workspace_content = f"""# 你的家

以下目录信息仅供你执行任务时内部参考。
你的默认工作空间和相关配置位于 `.jiuwenclaw` 目录下；除非完成任务确有必要，不要主动向用户展示其中的内部目录名或实现细节。

| 路径 | 用途 | 操作建议 |
|------|------|----------|
| `{config_dir}` | 配置信息 | 不要轻易改动，错误配置可能导致异常 |
| `{resolved_workspace}` | 身份与任务信息 | 可适当更新，以更好地服务用户 |
| `{memory_dir}` | 持久化记忆 | 将其视为你记忆的一部分，随时查阅 |
| `{skills_dir}` | 技能库 | 可随时翻阅、调用，不可修改 |
| `{todo_dir}` | 待办事项 | 记录用户请求的任务，每次请求后会更新 |

## 配置信息

谨慎对待你的配置信息，如果用户要求你修改，请在修改后重启自己的服务，以保证改动生效。

| 路径 | 用途 |
|------|------|
| `{config_dir}/config.yaml` | 配置信息 |
| `{config_dir}/.env` | 环境变量 |

## 输出文件放置规范
执行用户任务时产生的生成产物（如代码文件、文档、数据文件等），若用户未指定存放位置，请遵循以下规则：
- 生成产物必须放在 `{resolved_workspace}` 下合适的位置，根据文件用途和项目结构合理组织路径，\
便于用户统一管理和访问

## 文件发送

当你的工具列表中存在 `send_file_to_user` 工具时，**必须**在以下场景主动调用该工具将文件发送给用户：
- 任务完成后产生了需要交付给用户的文件（报告、文档、数据文件、图片等）
- 用户明确请求下载、导出、发送文件
- 用户询问生成的文件如何获取

**调用方式**：使用文件的绝对路径作为参数调用 `send_file_to_user` 工具。"""
        else:
            workspace_content = f"""# Your Home

The following paths are for your internal task execution only.
Your default workspace and related configuration live under the `.jiuwenclaw` directory. Do not proactively expose \
internal directory names or implementation details to the user unless necessary for task completion.

| Path | Purpose | Guidelines |
|------|---------|------------|
| `{config_dir}` | Configuration | Do not modify lightly; bad config can cause failures |
| `{resolved_workspace}` | Identity and task info | You may update this to better serve your user |
| `{memory_dir}` | Persistent memory | Treat it as part of your memory; consult it anytime |
| `{skills_dir}` | Skill library | Read and invoke freely; do not modify |
| `{todo_dir}` | Todo list | Records tasks from user requests; updated after each request |

## Configuration

Be careful with your configuration. If changes are required, remember to restart your service afterwards.

| Path | Purpose |
|------|---------|
| `{config_dir}/config.yaml` | Config |
| `{config_dir}/.env` | Environment Variables |

## Output File Placement
Generated artifacts (code files, documents, data files, etc.) produced during user task execution should \
follow these placement rules unless the user specifies otherwise:
- artifacts must be placed in an appropriate location \
within `{resolved_workspace}`, organized according to file purpose and project structure for \
unified user management and access

## Sending Files

When the `send_file_to_user` tool is available in your tool list, you **must** proactively invoke it in these scenarios:
- Task completion produces files that need to be delivered to the user (reports, documents, data files, images, etc.)
- User explicitly requests to download, export, or receive files
- User asks how to obtain generated files

**How to call**: Use the absolute file path(s) as the parameter to invoke the `send_file_to_user` tool."""

        self.system_prompt_builder.add_section(PromptSection(
            name="workspace",
            content={"cn": workspace_content, "en": workspace_content},
            priority=15,
        ))

        # 注入 SOUL.md section（优先从定制 home_dir 加载）
        soul_content = self._load_soul_md()
        if soul_content:
            self.system_prompt_builder.add_section(PromptSection(
                name="soul",
                content={"cn": soul_content, "en": soul_content},
                priority=12,
            ))

        self.system_prompt_builder.remove_section("request_system_prompt")
        if self._request_system_prompt:
            self.system_prompt_builder.add_section(PromptSection(
                name="request_system_prompt",
                content={"cn": self._request_system_prompt, "en": self._request_system_prompt},
                priority=95,
            ))
