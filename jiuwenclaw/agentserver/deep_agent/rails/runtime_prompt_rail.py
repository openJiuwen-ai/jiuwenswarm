# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Inject dynamic time/channel/runtime info per model call.

Dynamic content (time, channel, agent, model, language) is decoupled from the
static identity prompt and refreshed on every model call via before_model_call().
Supports `sections` gating: team mode passes ("time",) to inject only the date
(without touching system_prompt_builder or workspace paths).
"""
from __future__ import annotations

import platform
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from shutil import which
from typing import Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.config import get_sandbox_runtime
from jiuwenclaw.utils import (
    get_multi_tenant_user_workspace_dir,
    normalize_tenant_scope_id,
    resolve_agent_registered_skill_dirs,
)

_CN_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

logger = getLogger(__name__)


class RuntimePromptRail(DeepAgentRail):
    """在 before_model_call 中注入运行时动态 section（时间、运行时信息）。

    支持 `sections` 门控：team 模式传 ("time",)，只注入日期（不依赖
    system_prompt_builder，也不带 workspace 路径段）。
    """

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
        request_identify: str = "",
        request_soul: str = "",
        sections: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._language = language
        self._channel = channel
        self._tz = timezone(timedelta(hours=timezone_offset))
        self._agent_name = agent_name
        self._model_name = model_name
        self._mode: str = ""
        self._session_id: str | None = None
        self._workspace_dir = workspace_dir
        self._agent_id = agent_id
        self._service_id = service_id
        self._request_system_prompt: str = ""
        self._request_identify: str = request_identify.strip() if request_identify else ""
        self._request_soul: str = request_soul.strip() if request_soul else ""
        self._registered_skill_dirs: list[str] | None = None
        # None → 全部段落（普通模式默认，行为与历史一致）；
        # team 模式传 ("time",)，只注入日期，不碰 builder、不带路径。
        self._sections = ("time", "runtime", "workspace") if sections is None else tuple(sections)

    def set_registered_skill_dirs(self, dirs: list[str] | None) -> None:
        """Use adapter snapshot (reload session isolation); None falls back to resolve."""
        if dirs is None:
            self._registered_skill_dirs = None
            return
        self._registered_skill_dirs = [str(d) for d in dirs if str(d).strip()]

    def set_model_name(self, model_name: str) -> None:
        """Per-request model name used in the runtime prompt section."""
        self._model_name = model_name or ""

    def set_mode(self, mode: str) -> None:
        """Per-request mode label (team/plan/…); stored for prompt/runtime context."""
        self._mode = mode or ""

    def set_session_id(self, session_id: str | None) -> None:
        """Per-request session id for session-scoped runtime context."""
        self._session_id = (
            session_id.strip()
            if isinstance(session_id, str) and session_id.strip()
            else None
        )

    def _skills_dirs_display(self, workspace_root: Path) -> str:
        if self._registered_skill_dirs:
            return ", ".join(self._registered_skill_dirs)
        resolved = resolve_agent_registered_skill_dirs()
        if resolved:
            return ", ".join(str(p) for p in resolved)
        return str(workspace_root / "skills")

    def init(self, agent) -> None:
        """从 agent 获取 system_prompt_builder 引用。"""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        """清理注入的 section 并释放引用。"""
        # 热重载后 agent.system_prompt_builder 可能已是新引用，退休清理前先同步缓存，
        # 确保 remove_section 落到当前生效的 builder 上。
        _builder = getattr(agent, "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("runtime")
            self.system_prompt_builder.remove_section("workspace")
            self.system_prompt_builder.remove_section("request_system_prompt")
        self.system_prompt_builder = None

    def set_language(self, language: str) -> None:
        """per-request 更新语言。"""
        self._language = language

    def set_channel(self, channel: str) -> None:
        """per-request 更新频道。"""
        self._channel = channel

    def set_workspace_dir(self, workspace_dir: Optional[str]) -> None:
        """per-request 更新工作空间目录。"""
        self._workspace_dir = workspace_dir

    def set_request_system_prompt(self, prompt: Optional[str]) -> None:
        """per-request 更新 system prompt 追加内容。"""
        value = prompt.strip() if isinstance(prompt, str) else ""
        self._request_system_prompt = value

    def set_request_identify(self, identify: Optional[str]) -> None:
        """per-request 更新身份信息（覆盖 IDENTITY.md）。"""
        value = identify.strip() if identify else ""
        self._request_identify = value

    def set_request_soul(self, soul: Optional[str]) -> None:
        """per-request 更新灵魂/性格描述（覆盖 SOUL.md）。"""
        value = soul.strip() if soul else ""
        self._request_soul = value

    def _get_workspace_dirs(self) -> dict[str, str]:
        """获取工作空间目录路径（始终按租户树；缺省为 default/default）。"""
        service_id = normalize_tenant_scope_id(self._service_id)
        agent_id = normalize_tenant_scope_id(self._agent_id)
        base_workspace = get_multi_tenant_user_workspace_dir(service_id, agent_id)
        if base_workspace is None:
            # normalize 后两侧均非空，理论上不会走到；防御性回退 default/default。
            base_workspace = get_multi_tenant_user_workspace_dir("default", "default")
        if base_workspace is None:
            raise RuntimeError(
                "failed to resolve multi-tenant workspace for RuntimePromptRail "
                f"(service_id={service_id!r}, agent_id={agent_id!r})"
            )
        workspace_root = base_workspace / "agent" / "jiuwenclaw_workspace"
        return {
            "config": str(base_workspace / "config"),
            "workspace": self._workspace_dir or str(workspace_root),
            "memory": str(workspace_root / "memory"),
            "daily_memory": str(workspace_root / "memory" / "daily_memory"),
            "skills": self._skills_dirs_display(workspace_root),
            "todo": str(workspace_root / "todo"),
        }

    def _add_runtime_section(self) -> None:
        """构建并注入 runtime 段（平台/Python/命令语法规范）。"""
        plat = f"{platform.system()} {platform.machine()}"
        python_ver = platform.python_version()
        os_type = platform.system().lower()

        # 沙箱权限提示词仅在 sandbox.enabled=True 时注入; 关闭沙箱时命令可自由访问
        # 全盘, 不应让 LLM 误以为工作区外一律无权限而在真实 PermissionError 上过早放弃.
        sandbox_enabled = bool(get_sandbox_runtime().get("enabled"))
        sandbox_perm_cn = ""
        sandbox_perm_en = ""
        if sandbox_enabled:
            sandbox_perm_cn = (
                "\n\n## 命令执行环境与权限\n\n"
                "- 你的命令在一个**受限沙箱**中执行，只能读写你自己的工作区目录，"
                "工作区之外的路径（例如 `C:\\` 系统盘、其他用户目录、桌面）很可能**没有访问权限**。\n"
                "- 一旦命令返回**权限拒绝**类错误（如 `拒绝访问` / `PermissionError` / `WinError 5` / "
                "`Access is denied`），**立即停止**对该路径的进一步尝试。"
                "**不要**换一种命令（改 `dir`/`powershell`/`wsl`/换路径写法）反复重试同一目标——"
                "权限是按路径授予的，换命令语法不会改变结果，只会浪费轮次。\n"
                "- 正确做法：将该路径视为不可达，向用户说明权限受限并给出替代方案"
                "（例如请用户把文件放进工作区，或在工作区内完成等效任务）。\n"
            )
            sandbox_perm_en = (
                "\n\n## Command Execution Environment and Permissions\n\n"
                "- Your commands run inside a **restricted sandbox**. You can read/write your own "
                "workspace directory, but paths outside it (e.g. `C:\\` system drive, other user "
                "directories, the Desktop) very likely have **no access**.\n"
                "- As soon as a command returns a **permission-denied** error "
                "(`PermissionError` / `WinError 5` / `Access is denied`), **stop immediately**. "
                "Do NOT retry the same target with a different command (switching "
                "`dir`/`powershell`/`wsl`/path syntax) — permissions are granted per-path, so "
                "changing command syntax will not change the result; it only wastes turns.\n"
                "- Correct action: treat that path as unreachable, tell the user about the "
                "restriction, and offer an alternative (e.g. ask the user to place the file in "
                "the workspace, or complete the equivalent task within the workspace).\n"
            )

        if self._language == "cn":
            runtime_content = (
                "# 运行时\n\n"
                f"- 平台：{plat}\n"
                f"- Python：{python_ver}\n"
                f"- 模型：{self._model_name}（本字段为身份来源：被问及当前模型时须据此回答，不得沿用对话历史中先前的自我声明）\n"
                f"- Agent：{self._agent_name}\n"
                f"- 频道：{self._channel}\n"
                f"- 语言：{self._language}\n"
                "\n## 命令语法规范\n"
                f"当前运行平台：`{os_type}`\n\n"
                "**重要提示**：必须严格使用与当前平台匹配的命令语法，切勿使用其他平台的命令格式。\n\n"
                "常见命令差异对照：\n\n"
                "| 操作 | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |\n"
                "|------|---------------------------|-------------------------------|\n"
                "| 创建目录 | `mkdir folder` 或 PowerShell "
                "`New-Item -ItemType Directory -Path folder` | `mkdir -p folder` |\n"
                "| 查看文件 | `type file.txt` 或 PowerShell `Get-Content file.txt` | `cat file.txt` |\n"
                "| 列出文件 | `dir` 或 PowerShell `Get-ChildItem` | `ls -la` |\n"
                "| 删除文件 | `del file.txt` 或 PowerShell `Remove-Item file.txt` | `rm file.txt` |\n"
                "| 删除目录 | `rmdir folder` 或 PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |\n"
                "| 查找文件 | `dir /s pattern` 或 PowerShell "
                "`Get-ChildItem -Recurse -Filter pattern` | `find . -name pattern` |\n\n"
                "**特别注意**：Windows 的 `mkdir` 不支持 `-p` 参数！"
                "在 Windows 上使用 `mkdir -p folder` 会错误创建名为 `-p` 的目录。"
                "如需创建嵌套目录，请使用 PowerShell `New-Item -ItemType Directory -Path \"parent/child\" -Force`，"
                f"或使用 cmd 分步创建 `mkdir parent && mkdir parent\\child`.{sandbox_perm_cn}"
            )
        else:
            runtime_content = (
                "# Runtime\n\n"
                f"- Platform: {plat}\n"
                f"- Python: {python_ver}\n"
                f"- Model: {self._model_name} "
                "(authoritative: when asked your current model, answer from this field, "
                "not prior conversation history)\n"
                f"- Agent: {self._agent_name}\n"
                f"- Channel: {self._channel}\n"
                f"- Language: {self._language}\n"
                "\n## Command Syntax\n"
                f"Current platform: `{os_type}`\n\n"
                "**Important**: You MUST strictly use command syntax matching the current platform. "
                "Never use command formats from other platforms.\n\n"
                "Common command differences:\n\n"
                "| Operation | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |\n"
                "|-----------|---------------------------|-------------------------------|\n"
                "| Create directory | `mkdir folder` or PowerShell "
                "`New-Item -ItemType Directory -Path folder` | `mkdir -p folder` |\n"
                "| View file | `type file.txt` or PowerShell `Get-Content file.txt` | `cat file.txt` |\n"
                "| List files | `dir` or PowerShell `Get-ChildItem` | `ls -la` |\n"
                "| Delete file | `del file.txt` or PowerShell `Remove-Item file.txt` | `rm file.txt` |\n"
                "| Delete directory | `rmdir folder` or PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |\n"
                "| Find file | `dir /s pattern` or PowerShell "
                "`Get-ChildItem -Recurse -Filter pattern` | `find . -name pattern` |\n\n"
                "**WARNING**: Windows `mkdir` does NOT support the `-p` flag! "
                "Using `mkdir -p folder` on Windows will incorrectly create a directory named `-p`. "
                'To create nested directories on Windows, use either PowerShell '
                '`New-Item -ItemType Directory -Path "parent/child" -Force` '
                f"or cmd with step-by-step creation `mkdir parent && mkdir parent\\child`.{sandbox_perm_en}"
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="runtime",
            content={"cn": runtime_content, "en": runtime_content},
            priority=95,
        ))
        logger.info("[RuntimePromptRail] runtime section 已注入")

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """每次 model call 注入最新的时间和运行时信息。"""
        logger.info(
            "[RuntimePromptRail] before_model_call 开始执行: language=%s channel=%s agent=%s model=%s",
            self._language, self._channel, self._agent_name, self._model_name
        )

        # 日期注入不依赖 system_prompt_builder：团队模式成员可能没有 builder，
        # 日期也必须注入（environment_context 由 ReActAgent 消费）。
        if "time" in self._sections:
            now = datetime.now(tz=self._tz)
            now_str = now.strftime("%Y-%m-%d")
            current_year = now.strftime("%Y")

            if self._language == "cn":
                time_content = (
                    f"# 当前日期\n\n"
                    f"- 当前日期：{now_str}\n"
                    f"- 当前年份：{current_year}\n"
                    '- 当用户询问"最新、当前、今年、本年、实时、近期"等信息并需要搜索时，'
                    '搜索 query 必须优先使用当前年份或日期'
                )
            else:
                time_content = (
                    f"# Current Date\n\n"
                    f"- Current date: {now_str}\n"
                    f"- Current year: {current_year}\n"
                    "- When the user asks for latest/current/this-year/recent information and search is needed, "
                    "search queries must prefer the current year or date."
                )

            ctx.extra.setdefault("environment_context", []).append({
                "content": time_content,
                "source": "time_rail",
            })

        # 热重载（DeepAgent._hot_reload_system_prompt）会新建 SystemPromptBuilder 并替换
        # agent.system_prompt_builder，但保留型 rail 不会重新 init()，缓存的
        # self.system_prompt_builder 可能指向旧 builder。这里每次从 ctx.agent 现取最新
        # builder 并刷新缓存，使后续 add_section 都落到当前生效的 builder 上。
        _builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if _builder is not None:
            self.system_prompt_builder = _builder
        if not self.system_prompt_builder:
            if "runtime" in self._sections or "workspace" in self._sections:
                logger.warning("[RuntimePromptRail] system_prompt_builder 未初始化，无法注入 section")
            return

        if "runtime" in self._sections:
            self._add_runtime_section()

        # soul / identify 由 JiuClawContextEngineeringRail 写入 context 段（## SOUL / ## IDENTITY）

        # 使用多租户感知的方法获取路径
        if "workspace" in self._sections:
            dirs = self._get_workspace_dirs()
            config_dir = dirs["config"]
            resolved_workspace = dirs["workspace"]
            memory_dir = dirs["memory"]
            daily_memory_dir = dirs["daily_memory"]
            skills_dir = dirs["skills"]
            todo_dir = dirs["todo"]

            #  关键诊断：检查 output_dir ContextVar 的实际值
            from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import get_effective_request_output_dir
            actual_output_dir = get_effective_request_output_dir()
            logger.info(
                "[RuntimePromptRail]  workspace paths 已获取: "
                "resolved_workspace=%s | output_dir_from_ContextVar=%s | workspace_dir_from_request=%s",
                resolved_workspace,
                actual_output_dir,
                self._workspace_dir
            )

            # ⭐ 方案B：根据 actual_output_dir 是否有值，生成不同的指导内容
            # 直接给出路径值，不指导 Agent 调用 API（与方案A形成双重保障）
            if actual_output_dir:
                # 有值：直接给出 output_dir 路径
                output_dir_guidance_cn = f"当前会话的 output_dir 路径：`{actual_output_dir}`"
                output_dir_example_cn = f"{actual_output_dir}/filename.ext"
                output_dir_guidance_en = f"Current session output_dir path: `{actual_output_dir}`"
                output_dir_example_en = f"{actual_output_dir}/filename.ext"
                logger.info(
                    "[RuntimePromptRail] ⭐ output_dir 有值，直接注入路径: %s",
                    actual_output_dir
                )
            else:
                # 无值：明确告知未设置，使用 resolved_workspace
                output_dir_guidance_cn = f"output_dir 未设置，使用 resolved_workspace：`{resolved_workspace}`"
                output_dir_example_cn = f"{resolved_workspace}/filename.ext"
                output_dir_guidance_en = f"output_dir not set, use resolved_workspace: `{resolved_workspace}`"
                output_dir_example_en = f"{resolved_workspace}/filename.ext"
                logger.info(
                    "[RuntimePromptRail] ⭐ output_dir 为 None，使用 resolved_workspace 备选: %s",
                    resolved_workspace
                )

            if self._language == "cn":
                workspace_content = f"""# 你的家
                以下目录信息仅供你执行任务时内部参考。
                你的默认工作空间和相关配置位于 `.jiuwenclaw` 目录下；除非完成任务确有必要，不要主动向用户展示其中的内部目录名或实现细节。

                | 路径 | 用途 | 操作建议 |
                |------|------|----------|
                | `{config_dir}` | 配置信息 | 不要轻易改动，错误配置可能导致异常 |
                | `{resolved_workspace}` | 身份与任务信息 | 可适当更新，以更好地服务用户 |
                | `{memory_dir}` | 持久化记忆（含 USER.md、MEMORY.md） | 将其视为你记忆的一部分，随时查阅 |
| `{daily_memory_dir}` | 每日记忆文件（YYYY-MM-DD.md） | 每天的记忆记录存放在此，新增/编辑后需调用 memory_index 索引 |
                | `{skills_dir}` | 技能库 | 可随时翻阅、调用，不可修改 |
                | `{todo_dir}` | 待办事项 | 记录用户请求的任务，每次请求后会更新 |

                ## 配置信息

                谨慎对待你的配置信息，如果用户要求你修改，请在修改后重启自己的服务，以保证改动生效。

                | 路径 | 用途 |
                |------|------|
                | `{config_dir}/config.yaml` | 配置信息 |
                | `{config_dir}/.env` | 环境变量 |

                ## 文件输出与发送规范

                执行用户任务时产生的生成产物（如代码文件、文档、数据文件等），按以下规则选择存放位置：

                ### 需要交付给用户的文件

                {output_dir_guidance_cn}

                直接使用上述路径创建文件，例如：`{output_dir_example_cn}`

                ### 项目文件编辑或内部文件

                使用 resolved_workspace：`{resolved_workspace}`

                **建议**：
                - 需要交付给用户的文件：使用上述 output_dir 路径
                - 项目文件编辑或内部文件：使用 resolved_workspace

                ## 文件发送

                当你的工具列表中存在 `send_file_to_user` 工具时，**必须**在以下场景主动调用该工具将文件发送给用户：
                - 任务完成后产生了需要交付给用户的文件（报告、文档、数据文件、图片等）
                - 用户明确请求下载、导出、发送文件
                - 用户询问生成的文件如何获取
                - 用户主动调用`write_file`、`write_text_file`这类文件生成/修改工具后

                **调用方式**：使用文件的绝对路径作为参数调用 `send_file_to_user` 工具。"""
            else:
                workspace_content = f"""# Your Home
                The following paths are for your internal task execution only.
Your default workspace and related configuration live under the `.jiuwenclaw` directory. Do not proactively expose
                internal directory names or implementation details to the user unless necessary for task completion.

                | Path | Purpose | Guidelines |
                |------|---------|------------|
                | `{config_dir}` | Configuration | Do not modify lightly; bad config can cause failures |
                | `{resolved_workspace}` | Identity and task info | You may update this to better serve your user |
                | `{memory_dir}` | Persistent memory (USER.md, MEMORY.md) | Treat it as part of your memory;
                consult it anytime |
                | `{daily_memory_dir}` | Daily memory files (YYYY-MM-DD.md) | Daily memory records;
                call memory_index after creating/editing |
                | `{skills_dir}` | Skill library | Read and invoke freely; do not modify |
                | `{todo_dir}` | Todo list | Records tasks from user requests; updated after each request |

                ## Configuration

                Be careful with your configuration. If changes are required, remember to restart your service
                afterwards.

                | Path | Purpose |
                |------|---------|
                | `{config_dir}/config.yaml` | Config |
                | `{config_dir}/.env` | Environment Variables |

                ## File Output and Sending Guidelines

                Generated artifacts (code files, documents, data files, etc.) produced during user task execution
                should be stored according to the following rules:

                ### Files to Deliver to User

                {output_dir_guidance_en}

                Directly use the above path to create files, e.g.: `{output_dir_example_en}`

                ### Project File Editing or Internal Files

                Use resolved_workspace: `{resolved_workspace}`

                **Recommendations**:
                - Files to deliver to user: Use the output_dir path above
                - Project file editing or internal files: Use resolved_workspace

                ## Sending Files

                When the `send_file_to_user` tool is available in your tool list, you **must** proactively invoke it
                in these scenarios:
                - Task completion produces files that need to be delivered to the user (reports, documents,
                data files, images, etc.)
                - User explicitly requests to download, export, or receive files
                - User asks how to obtain generated files
                - After the user actively invokes file generation/modification tools such as
                  `write_file` and `write_text_file`

                **How to call**: Use the absolute file path(s) as the parameter to invoke the `send_file_to_user`
                tool."""

            self.system_prompt_builder.add_section(PromptSection(
                name="workspace",
                content={"cn": workspace_content, "en": workspace_content},
                priority=15,
            ))
            logger.info(
                "[RuntimePromptRail]  workspace section 已注入（包含 output_dir 指导）"
                " | resolved_workspace=%s | output_dir_API返回值=%s",
                resolved_workspace,
                actual_output_dir
            )

            self.system_prompt_builder.remove_section("request_system_prompt")
            if self._request_system_prompt:
                self.system_prompt_builder.add_section(PromptSection(
                    name="request_system_prompt",
                    content={"cn": self._request_system_prompt, "en": self._request_system_prompt},
                    priority=95,
                ))
