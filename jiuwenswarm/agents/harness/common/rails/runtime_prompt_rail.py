# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Assemble stable and dynamic runtime prompt state.

Stable environment rules and the conversation-start git snapshot stay in the
system prompt. Dynamic runtime state is managed as a prompt attachment.
Request date/time remains in the real user message's JSON envelope.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation.cwd import init_cwd
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
)

from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.agents.harness.common.prompt.priority_registry import (
    SystemPromptPriority,
)
from jiuwenswarm.agents.harness.common.prompt.shell_environment import (
    build_shell_environment_prompt,
    get_shell_capabilities,
)
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_runtime_state_path,
    get_user_workspace_dir,
    logger,
)


A4P_PROMPT_VERSION = "2026-10-19.1"


class RuntimePromptRail(DeepAgentRail):
    """Keep stable system context separate from dynamic prompt attachments."""

    priority = 5  # 高优先级，确保早于其他 rail 执行

    def __init__(
        self,
        language: str = "cn",
        channel: str = "web",
        timezone_offset: int = 8,
    ) -> None:
        super().__init__()
        self._agent = None
        self.system_prompt_builder = None
        self.attachment_manager = None
        self._language = language
        self._channel = channel
        self._trusted_dirs: list[str] | None = None
        self._cwd: str | None = None
        self._project_dir: str | None = None
        self._workspace_dir: str | None = None
        self._task_workspace_root: str | None = None
        self._task_work_dir: str | None = None
        self._task_outputs_dir: str | None = None
        self._execution_cwd: str | None = None
        self._execution_project_root: str | None = None
        self._execution_workspace: str | None = None
        self._execution_paths_revision = 0
        self._bound_execution_paths_revision: ContextVar[int] = ContextVar(
            "runtime_prompt_bound_execution_paths_revision",
            default=-1,
        )
        self._model_name: str = ""
        self._mode: str = ""
        self._session_id: str | None = None
        self._force_english: bool = False
        self._request_metadata: dict[str, Any] = {}
        self._a4p_authorizer_available: bool = False

    def init(self, agent) -> None:
        """从 agent 获取 system_prompt_builder 引用。"""
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self.attachment_manager = getattr(agent, "prompt_attachment_manager", None)

    def uninit(self, agent) -> None:
        """清理注入的 section 并释放引用。"""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("video_duplex.voice_receipt")
            self.system_prompt_builder.remove_section("time")
            self.system_prompt_builder.remove_section("runtime.model_answer_policy")
            self.system_prompt_builder.remove_section("language_output")
            self.system_prompt_builder.remove_section("env")
            self.system_prompt_builder.remove_section("directory_boundaries")
            self.system_prompt_builder.remove_section("tui_current_project_policy")
            self.system_prompt_builder.remove_section("trusted_dirs_policy")
            self.system_prompt_builder.remove_section("git_status")
        self._agent = None
        self.system_prompt_builder = None
        self.attachment_manager = None
        self._task_workspace_root = None
        self._task_work_dir = None
        self._task_outputs_dir = None
        self._execution_cwd = None
        self._execution_project_root = None
        self._execution_workspace = None

    def set_language(self, language: str) -> None:
        """per-request 更新语言。"""
        self._language = language

    def set_channel(self, channel: str) -> None:
        """per-request 更新频道。"""
        self._channel = channel

    def set_a4p_authorizer_available(self, available: bool) -> None:
        """Record whether this request has an interactive Web authorizer route."""
        self._a4p_authorizer_available = bool(available)

    def set_trusted_dirs(self, trusted_dirs: list[str] | None) -> None:
        """per-request 更新可信目录。"""
        self._trusted_dirs = trusted_dirs

    def set_runtime_paths(
        self,
        *,
        cwd: str | None = None,
        project_dir: str | None = None,
        workspace_dir: str | None = None,
        task_workspace_root: str | None = None,
        task_work_dir: str | None = None,
        task_outputs_dir: str | None = None,
    ) -> None:
        """Per-request project identity, task paths, cwd, and own workspace.

        Args:
            cwd: Working directory shell runs in and relative paths resolve against.
            project_dir: Project root, when the request is bound to one.
            workspace_dir: This agent's own workspace (artifacts, memory, skills
                view). Team members each have their own; falls back to the
                process-wide agent workspace when unset.
            task_workspace_root: Root of an automatically allocated projectless
                task workspace.
            task_work_dir: Temporary/intermediate files directory.
            task_outputs_dir: Final deliverables directory.
        """
        self._cwd = cwd.strip() if isinstance(cwd, str) and cwd.strip() else None
        self._project_dir = (
            project_dir.strip()
            if isinstance(project_dir, str) and project_dir.strip()
            else None
        )
        self._workspace_dir = (
            workspace_dir.strip()
            if isinstance(workspace_dir, str) and workspace_dir.strip()
            else None
        )
        self._task_workspace_root = (
            task_workspace_root.strip()
            if isinstance(task_workspace_root, str) and task_workspace_root.strip()
            else None
        )
        self._task_work_dir = (
            task_work_dir.strip()
            if isinstance(task_work_dir, str) and task_work_dir.strip()
            else None
        )
        self._task_outputs_dir = (
            task_outputs_dir.strip()
            if isinstance(task_outputs_dir, str) and task_outputs_dir.strip()
            else None
        )

    def set_model_name(self, model_name: str) -> None:
        """per-request 更新模型名称，作为文件读取失败时的兜底。"""
        self._model_name = model_name or ""

    def set_execution_paths(
        self,
        *,
        cwd: str,
        project_root: str,
        workspace: str,
    ) -> None:
        """Bind Agent/Code paths inside the task that executes the round."""
        execution_paths = (cwd, project_root, workspace)
        if execution_paths == (
            self._execution_cwd,
            self._execution_project_root,
            self._execution_workspace,
        ):
            return
        self._execution_cwd = cwd
        self._execution_project_root = project_root
        self._execution_workspace = workspace
        # A session normally keeps one fixed operational directory. Increment
        # only when those paths actually change, so repeated requests in the
        # same session do not overwrite a cwd selected by runtime tools (for
        # example, a Code worktree).
        self._execution_paths_revision += 1

    def set_mode(self, mode: str) -> None:
        """per-request 更新运行模式，作为文件读取失败时的兜底。"""
        self._mode = mode or ""

    def set_session_id(self, session_id: str | None) -> None:
        """per-request 更新 session id，用于读取按 session 隔离的 runtime_state 文件。"""
        self._session_id = (
            session_id.strip()
            if isinstance(session_id, str) and session_id.strip()
            else None
        )

    def set_request_metadata(self, metadata: dict[str, Any] | None) -> None:
        """Store request metadata used by channel-specific attachments."""
        self._request_metadata = dict(metadata or {})

    def set_force_english(self, force: bool) -> None:
        """Force English for runtime scaffolding in code mode."""
        self._force_english = force

    def _bind_execution_paths(self) -> None:
        """Bind request paths in the asyncio task that runs the current round."""
        if (
            self._execution_cwd is None
            or self._execution_project_root is None
            or self._execution_workspace is None
        ):
            return
        if (
            self._bound_execution_paths_revision.get()
            == self._execution_paths_revision
        ):
            return
        init_cwd(
            self._execution_cwd,
            project_root=self._execution_project_root,
            workspace=self._execution_workspace,
        )
        self._bound_execution_paths_revision.set(self._execution_paths_revision)

    def _uses_round_execution_paths(self) -> bool:
        """Return whether Agent/Code rounds need request path rebinding."""
        mode_root = str(self._mode or "").strip().lower().split(".", 1)[0]
        return mode_root in {"agent", "code"}

    @staticmethod
    def _existing_dirs(paths: list[str] | None) -> list[str]:
        """Return normalized existing directories, preserving order."""
        result: list[str] = []
        seen: set[str] = set()
        for item in paths or []:
            if not isinstance(item, str) or not item.strip():
                continue
            path = os.path.abspath(os.path.expanduser(item.strip()))
            key = os.path.normcase(path)
            if key in seen or not os.path.isdir(path):
                continue
            seen.add(key)
            result.append(path)
        return result

    @staticmethod
    def _existing_dir(path: str | None) -> str | None:
        if not isinstance(path, str) or not path.strip():
            return None
        resolved = os.path.abspath(os.path.expanduser(path.strip()))
        return resolved if os.path.isdir(resolved) else None

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))

    @staticmethod
    def _configured_model_names() -> list[str]:
        """Read configured model names from config.yaml as a runtime fallback."""
        try:
            from jiuwenswarm.common.config import get_model_names

            return [
                str(name).strip()
                for name in get_model_names()
                if str(name).strip()
            ]
        except Exception as exc:
            logger.debug("Failed to read configured model names: %s", exc)
            return []

    def _resolve_current_mode(
        self,
        ctx: AgentCallbackContext,
        configured_mode: str,
    ) -> str:
        """用 DeepAgent session state 覆盖 code 模式的请求初始快照。"""
        # 只对单 agent 的 code profile 走 live 覆盖（返回的 legacy 串
        # "code.plan"/"code.normal" 只携带单 agent code 语义）。保留旧
        # {"code", "code.normal", "code.plan"} 语义，并补上新三段命名单 agent
        # code canonical agent.code.*；code.team / team.code.* / team.plan.code
        # 仍按原样返回，避免把 team 系覆盖成单 agent 串。
        if configured_mode not in {
            "code", "code.normal", "code.plan",
            "agent.code.normal", "agent.code.plan",
        }:
            return configured_mode

        agent = self._agent or ctx.agent
        load_state = getattr(agent, "load_state", None)
        if not callable(load_state) or ctx.session is None:
            return configured_mode

        try:
            state = load_state(ctx.session)
            plan_state = getattr(state, "plan_mode", None)
            if isinstance(plan_state, dict):
                plan_mode = plan_state.get("mode")
            else:
                plan_mode = getattr(plan_state, "mode", None)
        except Exception as exc:
            logger.debug(
                "[RuntimePromptRail] Failed to resolve live agent mode: %s",
                exc,
            )
            return configured_mode

        normalized = str(plan_mode or "").strip().lower()
        if normalized == "plan":
            return "code.plan"
        if normalized in {"normal", "auto"}:
            return "code.normal"
        return configured_mode

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Prepare conversation-start context before the first model call."""
        self._bind_execution_paths()
        runtime_state = await self._refresh_dynamic_attachments(ctx)
        await self._sync_git_system_context(ctx, runtime_state)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        # Long-lived Agent/Code interactions execute rounds in a supervisor task
        # created before the request arrives. Rebind inside that task so its
        # subsequently spawned Bash/file tool tasks inherit the request cwd,
        # rather than the Agent's internal data workspace captured at startup.
        if self._uses_round_execution_paths():
            self._bind_execution_paths()
        runtime_state = await self._refresh_dynamic_attachments(ctx)
        if not self.system_prompt_builder:
            return

        for name in (
            "time",
            "runtime.model_answer_policy",
            "language_output",
            "env",
            "tui_current_project_policy",
            "trusted_dirs_policy"):
            self.system_prompt_builder.remove_section(name)

        from jiuwenswarm.extensions.video_duplex.backend.session_receipts import apply_summary_policy

        apply_summary_policy(self.system_prompt_builder, self._session_id, ctx)

        channel = (runtime_state.get("channel") or self._channel or "unknown").strip()

        # ── Platform, shell, encoding, time-query and channel rules ──
        os_type = sys.platform
        shell_capabilities = await get_shell_capabilities(os_type)
        shell_path = os.environ.get("SHELL", "")
        if os_type.startswith("win"):
            # Windows normally has no SHELL variable. Prefer the actual
            # PowerShell executable so the prompt does not report unknown.
            shell_path = shell_capabilities.powershell or ""
            shell_name = "PowerShell" if shell_path else "unknown"
        else:
            shell_name = os.path.basename(shell_path) if shell_path else "unknown"
        import platform as plat
        os_version = f"{plat.system()} {plat.release()}"
        env_language = "cn" if not self._force_english and self._language == "cn" else "en"
        shell_env_prompt = build_shell_environment_prompt(
            env_language, os_type, capabilities=shell_capabilities
        )

        if not self._force_english and self._language == "cn":
            env_content = (
                "# 运行环境\n\n"
                "## 平台与 Shell\n\n"
                f"- 当前运行平台：`{os_type}`\n"
                f"- OS 版本：{os_version}\n"
                f"- Shell：{shell_name}\n\n"
                f"{shell_env_prompt}\n\n"
                "## 编码兼容性\n\n"
                "- 代码将在 GBK 控制台或仅支持 GBK 的工具中运行时，避免直接使用 GBK 无法编码的 Emoji 和特殊字符。\n"
                "- 必须使用这些字符时，选择明确支持 UTF-8 的执行工具或显式配置 UTF-8 编码。\n\n"
                "## 时间相关查询\n\n"
                "- 用户询问“最新、当前、今年、实时、近期”等信息并需要搜索时，搜索 query 应优先包含当前年份或日期。\n\n"
                "## 当前渠道\n\n"
                f"- 当前渠道：`{channel}`"
            )
        else:
            env_content = (
                "# Runtime Environment\n\n"
                "## Platform and Shell\n\n"
                f"- Current platform: `{os_type}`\n"
                f"- OS version: {os_version}\n"
                f"- Shell: {shell_name}\n\n"
                f"{shell_env_prompt}\n\n"
                "## Encoding Compatibility\n\n"
                "- When code will run in a GBK console or a tool that supports only GBK, avoid Emoji and "
                "special characters that GBK cannot encode.\n"
                "- If those characters are required, use a tool that explicitly supports UTF-8 or "
                "configure UTF-8 encoding.\n\n"
                "## Time-sensitive Queries\n\n"
                "- When the user asks for the latest, current, this year's, real-time, or recent information "
                "and search is needed, prefer including the current year or date in the query.\n\n"
                "## Current Channel\n\n"
                f"- Current channel: `{channel}`"
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="env",
            content={"cn": env_content, "en": env_content},
            priority=SystemPromptPriority.ENV,
        ))

        # ── Channel: directory and file-operation boundaries ──
        # Remove both the consolidated section and legacy sections first so
        # switching to a channel without local directory guidance clears it.
        self.system_prompt_builder.remove_section("directory_boundaries")
        self.system_prompt_builder.remove_section("tui_current_project_policy")
        self.system_prompt_builder.remove_section("trusted_dirs_policy")
        if self._channel in ("tui", "web", "ws_client", "process_cli"):
            installed_workspace_dir = Path(get_agent_workspace_dir())
            user_workspace_dir = Path(get_user_workspace_dir())
            # This agent's own workspace. Team members each own one; without
            # it (single-agent runs) the process-wide agent workspace is the
            # same directory anyway.
            agent_workspace_dir = self._existing_dir(self._workspace_dir) or str(
                installed_workspace_dir
            )
            installed_skills_dir = installed_workspace_dir / "skills" / "{skill_name}"
            installed_agent_templates_dir = (
                installed_workspace_dir / "plugins" / "agent_templates" / "local"
            )
            installed_agent_groups_dir = (
                user_workspace_dir / ".agent_teams" / "agent_groups" / "local"
            )
            installed_plugin_packages_dir = (
                installed_workspace_dir / "plugins" / "plugin_packages" / "local"
            )
            installed_mcp_hub_dir = installed_workspace_dir / "mcp" / "mcp_hub"
            config_dir = str(user_workspace_dir / "config")
            project_dir = self._existing_dir(self._project_dir)
            task_workspace_root = self._existing_dir(self._task_workspace_root)
            task_work_dir = self._existing_dir(self._task_work_dir)
            task_outputs_dir = self._existing_dir(self._task_outputs_dir)
            runtime_cwd = (
                self._existing_dir(self._cwd)
                or task_work_dir
                or project_dir
                or agent_workspace_dir
            )
            has_projectless_task = (
                project_dir is None
                and task_workspace_root is not None
                and task_work_dir is not None
                and task_outputs_dir is not None
            )
            has_project = project_dir is not None
            prompt_project_dir = project_dir or runtime_cwd
            has_distinct_cwd = bool(
                has_project
                and not self._same_path(project_dir or "", runtime_cwd)
            )

            if not self._force_english and self._language == "cn":
                if has_projectless_task:
                    active_directory_content = (
                        "## 当前任务目录\n\n"
                        f"- 当前任务根目录：`{task_workspace_root}`\n"
                        f"- 临时工作目录：`{task_work_dir}`\n"
                        f"- 最终产物目录：`{task_outputs_dir}`\n\n"
                        "- 相对路径以临时工作目录为基准。\n"
                        "- Bash 未显式传入 `workdir` 时，默认在临时工作目录执行。\n"
                        "- 中间文件、缓存和临时脚本放在临时工作目录。\n"
                        "- 报告、导出文件、图片、数据等最终产物放在最终产物目录。\n\n"
                    )
                    directory_content = (
                        "# 目录与文件操作边界\n\n"
                        f"{active_directory_content}"
                        "## 通用目录规则\n\n"
                    )
                elif not has_project:
                    active_directory_content = (
                        "## 当前项目目录\n\n"
                        f"- 项目目录是当前运行时工作空间：`{prompt_project_dir}`\n\n"
                        "- 相对路径以项目目录为基准。\n"
                        "- Bash 未显式传入 `workdir` 时，默认在项目目录执行。\n"
                        "- 未指定保存位置时，任务产物放在项目目录内的合理位置。\n\n"
                    )
                    directory_content = (
                        "# 目录与文件操作边界\n\n"
                        f"{active_directory_content}"
                        "## 通用目录规则\n\n"
                    )
                else:
                    project_description = (
                        "- 项目目录是当前项目的根目录与项目上下文边界，"
                        if has_distinct_cwd
                        else "- 项目目录是你当前的工作空间，"
                    )
                    cwd_description = (
                        f"- 当前工作目录（cwd、相对路径基准及 Bash 默认目录）是：`{runtime_cwd}`\n\n"
                        if has_distinct_cwd
                        else "\n"
                    )
                    separation_rule = (
                        "- 项目目录与当前工作目录是两个独立概念，不得互相替换。\n"
                        if has_distinct_cwd
                        else ""
                    )
                    operation_directory = "当前工作目录" if has_distinct_cwd else "当前项目目录"
                    directory_content = (
                        "# 目录与文件操作边界\n\n"
                        "## 项目目录\n\n"
                        "### 项目目录说明\n\n"
                        f"{project_description}"
                        f"当前项目目录是：`{prompt_project_dir}`\n"
                        f"{cwd_description}"
                        "### 项目目录规则\n\n"
                        f"{separation_rule}"
                        f"- 用户任务中的相对路径必须相对于{operation_directory}路径去解析。\n"
                        f"- Bash 未显式传入 `workdir` 时，默认在{operation_directory}执行。\n"
                    )
            else:
                if has_projectless_task:
                    active_directory_content = (
                        "## Current Task Directories\n\n"
                        f"- Current task root: `{task_workspace_root}`\n"
                        f"- Temporary working directory: `{task_work_dir}`\n"
                        f"- Final deliverables directory: `{task_outputs_dir}`\n\n"
                        "- Resolve relative paths against the temporary working directory.\n"
                        "- When Bash is called without an explicit `workdir`, "
                        "run it in the temporary working directory.\n"
                        "- Put intermediate files, caches, and temporary scripts in the temporary working directory.\n"
                        "- Put reports, exports, images, data, and other final "
                        "deliverables in the final deliverables directory.\n\n"
                    )
                    directory_content = (
                        "# Directory and File-Operation Boundaries\n\n"
                        f"{active_directory_content}"
                        "## General Directory Rules\n\n"
                    )
                elif not has_project:
                    active_directory_content = (
                        "## Current Project Directory\n\n"
                        f"- The project directory is the current runtime workspace: `{prompt_project_dir}`\n\n"
                        "- Resolve relative paths against the project directory.\n"
                        "- When Bash is called without an explicit `workdir`, run it in the project directory.\n"
                        "- When no save location is specified, put task artifacts in "
                        "an appropriate location under the project directory.\n\n"
                    )
                    directory_content = (
                        "# Directory and File-Operation Boundaries\n\n"
                        f"{active_directory_content}"
                        "## General Directory Rules\n\n"
                    )
                else:
                    project_description = (
                        "- The project directory is the project root and project-context boundary; "
                        if has_distinct_cwd
                        else "- The project directory is your current workspace; "
                    )
                    cwd_description = (
                        f"- The current working directory (cwd, relative-path base, and Bash default) is: "
                        f"`{runtime_cwd}`\n\n"
                        if has_distinct_cwd
                        else "\n"
                    )
                    separation_rule = (
                        "- The project directory and current working directory are independent concepts; do not "
                        "substitute one for the other.\n"
                        if has_distinct_cwd
                        else ""
                    )
                    operation_directory = (
                        "current working directory" if has_distinct_cwd else "current project directory"
                    )
                    directory_content = (
                        "# Directory and File-Operation Boundaries\n\n"
                        "## Project Directory\n\n"
                        "### Project Directory Description\n\n"
                        f"{project_description}"
                        f"the current project directory is: `{prompt_project_dir}`\n"
                        f"{cwd_description}"
                        "### Project Directory Rules\n\n"
                        f"{separation_rule}"
                        f"- Resolve relative paths in user tasks against the {operation_directory}.\n"
                        f"- When Bash is called without an explicit `workdir`, run it in the {operation_directory}.\n"
                    )
            if not self._force_english and self._language == "cn":
                directory_content += (
                    "- 用户已经提供明确路径时直接使用，不要重复询问。\n"
                    "- 只有任务确实需要操作某个项目、且现有上下文无法确定项目位置时，才询问项目路径。\n\n"
                    "## JiuwenSwarm 内部目录\n\n"
                    "### 核心内部数据\n\n"
                    f"- 智能体内部数据目录：`{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm 启动配置目录：`{config_dir}`\n"
                    "- `IDENTITY.md`、`memory/`、`todo/` 和运行状态属于智能体内部数据。\n\n"
                    "### 已安装扩展资产\n\n"
                    f"- 已安装的 Skill 根目录：`{installed_skills_dir}`\n"
                    f"- 已安装的专家模板存放在：`{installed_agent_templates_dir}`\n"
                    f"- 已安装的专家团存放在：`{installed_agent_groups_dir}`\n"
                    f"- 已安装的插件包存放在：`{installed_plugin_packages_dir}`\n"
                    f"- 已安装的 MCP 存放在：`{installed_mcp_hub_dir}`\n\n"
                    "### 目录使用规则\n\n"
                    "- 不要把普通任务产物写入智能体内部目录或启动配置目录。\n"
                    "- 仅当用户明确要求创建、安装、更新或管理 JiuwenSwarm 内部资产时，才写入对应目录。\n"
                    "- 修改或删除已有内部资产前，确认目标及操作范围，避免影响其他智能体或会话。\n"
                    "- 用户任务中的 `config/`、`memory/`、`skills/`、`todo/`、`plugins/`、`mcp/` "
                    "或 `workspace/` 不自动映射到 JiuwenSwarm 内部目录。\n"
                    "- 目录中存在某项资产，不代表当前会话可以直接调用；实际可用性以当前技能、工具和 MCP 注册状态为准。"
                )
            else:
                directory_content += (
                    "- When the user has provided an explicit path, use it directly without asking again.\n"
                    "- Ask for a project path only when the task truly requires a "
                    "project and its location cannot be determined from the existing "
                    "context.\n\n"
                    "## JiuwenSwarm Internal Directories\n\n"
                    "### Core Internal Data\n\n"
                    f"- Agent internal data directory: `{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm startup configuration directory: `{config_dir}`\n"
                    "- `IDENTITY.md`, `memory/`, `todo/`, and runtime state are Agent internal data.\n\n"
                    "### Installed Extension Assets\n\n"
                    f"- Installed Skill root: `{installed_skills_dir}`\n"
                    f"- Installed Agent templates: `{installed_agent_templates_dir}`\n"
                    f"- Installed Agent groups: `{installed_agent_groups_dir}`\n"
                    f"- Installed plugin packages: `{installed_plugin_packages_dir}`\n"
                    f"- Installed MCPs: `{installed_mcp_hub_dir}`\n\n"
                    "### Directory Usage Rules\n\n"
                    "- Do not write ordinary task artifacts to the Agent internal data "
                    "directory or startup configuration directory.\n"
                    "- Write to these directories only when the user explicitly asks to "
                    "create, install, update, or manage JiuwenSwarm internal assets.\n"
                    "- Before modifying or deleting an existing internal asset, confirm the "
                    "target and operation scope to avoid affecting other Agents or sessions.\n"
                    "- `config/`, `memory/`, `skills/`, `todo/`, `plugins/`, `mcp/`, or "
                    "`workspace/` in a "
                    "user task do not automatically refer to JiuwenSwarm internal "
                    "directories.\n"
                    "- An asset's presence in one of these directories does not make it "
                    "directly callable in the current session; actual availability follows "
                    "the current Skill, tool, and MCP registrations."
                )
            self.system_prompt_builder.add_section(PromptSection(
                name="directory_boundaries",
                content={"cn": directory_content, "en": directory_content},
                priority=SystemPromptPriority.DIRECTORY_BOUNDARIES,
            ))

    async def _refresh_dynamic_attachments(
        self,
        ctx: AgentCallbackContext,
    ) -> dict[str, Any]:
        """Refresh runtime and git sections without touching the system prefix."""
        runtime_state: dict[str, Any] = {}
        state_path = get_runtime_state_path(self._session_id)
        try:
            with open(state_path, encoding="utf-8") as f:
                loaded_state = yaml.safe_load(f) or {}
                if isinstance(loaded_state, dict):
                    runtime_state = loaded_state
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Failed to read runtime state file %s: %s", state_path, exc)

        configured_models: list[str] = []
        raw_available_models = runtime_state.get("available_models") or []
        available_models: list[str] = [
            str(item).strip()
            for item in raw_available_models
            if str(item).strip()
        ] if isinstance(raw_available_models, list) else []
        if not available_models or not runtime_state.get("model"):
            configured_models = self._configured_model_names()
        if not available_models:
            available_models = configured_models
        fallback_model = configured_models[0] if configured_models else ""
        model = str(
            runtime_state.get("model")
            or self._model_name
            or fallback_model
            or "unknown"
        ).strip()
        available_models_str = ", ".join(available_models) if available_models else model
        configured_mode = str(
            self._mode or runtime_state.get("mode") or "unknown"
        ).strip()
        mode = self._resolve_current_mode(ctx, configured_mode)
        language_val = (
            self._language
            or runtime_state.get("language")
            or "unknown"
        ).strip()
        from jiuwenswarm.extensions.video_duplex.backend.session_receipts import apply_summary_policy

        apply_summary_policy(self.system_prompt_builder, self._session_id, ctx)

        channel = (runtime_state.get("channel") or self._channel or "unknown").strip()

        if not self._force_english and self._language == "cn":
            runtime_content = (
                "# 运行时状态\n\n"
                f"- 当前模型：{model}\n"
                f"- 可用模型：{available_models_str}\n"
                f"- 当前模式：{mode}\n"
                f"- 当前语言：{language_val}\n"
                f"- 当前渠道：{channel}"
            )
        else:
            runtime_content = (
                "# Runtime State\n\n"
                f"- Current model: {model}\n"
                f"- Available models: {available_models_str}\n"
                f"- Current mode: {mode}\n"
                f"- Current language: {language_val}\n"
                f"- Current channel: {channel}"
            )
        await self._upsert_prompt_attachment(
            ctx,
            section="runtime.setting",
            content=runtime_content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=95,
        )
        await self._sync_a4p_intent_authorization_attachment(ctx)
        await self._sync_a4p_cron_authorization_scope_attachment(ctx)

        return runtime_state

    def _log_a4p_prompt_injection(
        self,
        ctx: AgentCallbackContext,
        *,
        injected: bool,
        reason: str,
        content: str = "",
    ) -> None:
        """Record attachment registration, not proof of provider delivery or compliance."""
        from jiuwenswarm.agents.harness.common.a4p.execution_context import (
            AUTHORIZATION_EXECUTION_CONTEXTS,
        )

        session = getattr(ctx, "session", None)
        session_getter = getattr(session, "get_session_id", None)
        session_id = (session_getter() if callable(session_getter) else "") or self._session_id or ""
        execution = AUTHORIZATION_EXECUTION_CONTEXTS.get(session_id)
        extra = ctx.extra if isinstance(ctx.extra, dict) else {}
        event = {
            "event": "a4p.prompt.injection",
            "version": A4P_PROMPT_VERSION,
            "session_id": session_id,
            "request_id": execution.request_id if execution else extra.get("request_id", ""),
            "model": self._model_name,
            "channel": self._channel,
            "language": "en" if self._force_english or self._language != "cn" else "cn",
            "injected": injected,
            "reason": reason,
            "content_chars": len(content),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
        }
        logger.info("[RuntimePromptRail] %s", json.dumps(event, ensure_ascii=False))

    async def _sync_a4p_intent_authorization_attachment(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        section = "a4p.intent_authorization"
        if self._channel != "web" or not self._a4p_authorizer_available:
            await self._clear_prompt_attachment(ctx, section=section)
            self._log_a4p_prompt_injection(
                ctx, injected=False,
                reason="non_web_channel" if self._channel != "web" else "authorizer_unavailable",
            )
            return
        resolution_error = False
        try:
            from jiuwenswarm.agents.harness.common.a4p.runtime import is_a4p_preauthorization_active
            from jiuwenswarm.agents.harness.common.tools.a4p_tools import (
                get_a4p_supported_action_names,
            )
            from jiuwenswarm.common.config import get_config

            enabled = is_a4p_preauthorization_active(get_config())
            action_names = get_a4p_supported_action_names()
        except Exception as exc:
            logger.warning("Failed to resolve A4P intent authorization prompt: %s", exc)
            resolution_error = True
            enabled = False
            action_names = []
        if not enabled:
            await self._clear_prompt_attachment(ctx, section=section)
            self._log_a4p_prompt_injection(
                ctx, injected=False, reason="config_error" if resolution_error else "a4p_disabled",
            )
            return

        actions = "、".join(f"`{name}`" for name in action_names)
        if self._force_english or self._language != "cn":
            actions = ", ".join(f"`{name}`" for name in action_names)
            content = (
                "# A4P Intent Authorization\n\n"
                "- FIRST: before reading, listing, searching, writing, or running Shell commands with "
                f"{actions}, call `request_a4p_intent_authorization` with the known candidate scopes. "
                "Read-only operations also require this preparation. Do not read or explore first to prepare "
                "authorization. A user-supplied path is not permission. Only tools outside this list may be "
                "used first to discover necessary information.\n"
                "- Submit multiple actions together when input/output paths are known. The backend filters "
                "policy-allowed and valid-token-covered concrete calls; do not guess which calls need approval. "
                "Inspect permittedActions, unapprovedActions and deniedActions. Execute only permittedActions; "
                "unapprovedActions still lack approval and deniedActions require revising the plan. "
                "ok=true with authorizationRequired=false means no new approval was needed. Authorization "
                "does not execute actions; actual calls still recheck permissions.\n"
                "- Session authorization may be staged: authorize discovery first when needed, then newly "
                "discovered read/write scopes. Reuse approved scopes; resolved runtime values alone do not "
                "require another request. Do not repeat rejected or failed requests unchanged.\n"
                "- Use real tool names and security-critical parameters. Shell scopes default to complete exact "
                "commands; a narrow wildcard for one unknown argument must not absorb whitespace, Shell "
                "operators, redirections, or extra arguments. File scopes use a concrete file or a filename "
                "glob under a fixed directory; never a bare * or a directory in place of a file. Use dedicated "
                "file tools for writes. Unknown/pattern scopes remain in the approval request.\n"
                "- read_file/list_files/glob/grep require explicit absolute paths without '..' in requests and "
                "execution. list_files/glob authorize an exact directory; grep an exact file or directory. "
                "Filename discovery does not authorize read_file; reading does not authorize writing. "
                "Search keywords and result limits may vary within the root.\n"
                "- Choose a feasible plan once and proceed; revise only for new facts, changed requirements, "
                "or actual errors. Ordinary parameters need not be known for authorization; express dynamic "
                "values through bounded scopes, then execute with resolved values, never placeholders. "
                "Do not broaden scopes or guess missing information.\n"
                "- Cron: use the system-designated default creation tool: create disabled -> authorize the "
                "complete future scope using the returned cronJobId -> enable after ok=true only if every "
                "planned action is permitted. Keep the job disabled on failure, rejection, or insufficient "
                "scope. Cron authorization cannot be staged or rely on interactive approval during execution; "
                "current session permissions/tokens do not filter cron requests. Generate description and "
                "actions from the same plan, including dynamic argument resolution. When only scheduling is "
                "requested, put execution/preparation in the future plan; do not trial-run now unless setup "
                "depends on its results or the user requests immediate execution.\n"
            )
        else:
            content = (
                "# A4P 意图授权\n\n"
                f"- 第一步：使用 {actions} 读取、浏览、搜索、写入或执行命令前，必须先调用 "
                "`request_a4p_intent_authorization` 提交已知候选范围。只读操作也必须先提交；"
                "不得先读取或搜索文件来准备授权。用户提供路径不等于已授权。只有列表外的工具可先用于获取必要信息。\n"
                "- 输入输出路径已知时，合并提交多个 actions。后端过滤规则允许或已有有效 token 覆盖的具体操作，"
                "不要自行猜测哪些操作需要审批。检查 permittedActions、unapprovedActions 和 deniedActions；"
                "只能执行 permittedActions 中的操作，unapprovedActions 仍缺授权，deniedActions 需要调整计划。"
                "ok=true 且 authorizationRequired=false 表示无需新增授权。授权不执行操作，实际调用仍重新校验权限。\n"
                "- 会话允许分阶段授权：必要时先授权浏览或搜索，再补充发现的读取和写入范围。复用已批准范围；"
                "运行时值变为已知本身不要求重新授权。拒绝或失败后不要原样重复请求。\n"
                "- 使用真实工具名和安全关键参数。Shell 默认授权完整精确命令；单个未知参数仅允许窄范围通配，"
                "不得覆盖空白、Shell 运算符、重定向或额外参数。文件使用具体文件或固定目录下的文件名 glob，"
                "不得使用裸 * 或用目录代替文件范围。使用专用文件工具写入。通配范围或无法预检的操作仍保留申请。\n"
                "- read_file/list_files/glob/grep 在申请和执行时必须显式使用绝对路径且不含 '..'。"
                "list_files/glob 授权具体目录，grep 授权具体文件或目录；文件名搜索不包含 read_file 权限，"
                "读取不包含写入权限。范围内搜索词和结果条数可变化。\n"
                "- 选定可行计划后执行，仅因新事实、需求变化或实际错误调整。无需提前确定普通参数；"
                "动态参数用有界范围表达，实际执行使用真实值而非占位符。不扩大范围或猜测缺失信息。\n"
                "- 定时任务：使用系统指定的默认创建工具，create disabled -> 使用返回的 cronJobId "
                "一次性授权完整未来范围 -> ok=true 且计划内全部操作获准后 enable。失败、拒绝或范围不足时保持禁用。"
                "Cron 不能分阶段授权或依赖执行时交互审批，也不按当前会话权限/token 过滤。"
                "同一计划生成 description 和 actions，包含动态参数解析方式。用户仅要求定时时，准备和执行放入未来计划；"
                "只有设置依赖当前执行结果或用户要求立即执行时才现在执行，不额外试运行。\n"
            )
        try:
            injected = await self._upsert_prompt_attachment(
                ctx,
                section=section,
                content=content,
                kind=PromptAttachmentKind.RUNTIME,
                priority=94,
            )
        except Exception:
            self._log_a4p_prompt_injection(
                ctx, injected=False, reason="attachment_error", content=content,
            )
            raise
        self._log_a4p_prompt_injection(
            ctx, injected=injected,
            reason="attachment_registered" if injected else "attachment_unavailable",
            content=content,
        )

    async def _sync_a4p_cron_authorization_scope_attachment(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        section = "a4p.cron_authorization_scope"
        cron = self._request_metadata.get("cron")
        cron = cron if isinstance(cron, dict) else {}
        job_id = str(cron.get("job_id") or cron.get("jobId") or "").strip()
        if self._channel not in {"cron", "__cron__"} or not job_id:
            await self._clear_prompt_attachment(ctx, section=section)
            return
        try:
            from jiuwenswarm.agents.harness.common.a4p.runtime import (
                get_a4p_runtime,
                is_a4p_preauthorization_active,
            )
            from jiuwenswarm.common.config import get_config

            if not is_a4p_preauthorization_active(get_config()):
                await self._clear_prompt_attachment(ctx, section=section)
                return
            summary = get_a4p_runtime().cron_intent_authorization_summary(job_id)
        except Exception as exc:
            logger.warning("Failed to resolve A4P cron authorization scope: %s", exc)
            summary = None

        english = self._force_english or self._language != "cn"
        if summary is None:
            content = (
                f"# A4P Cron Authorization Scope\n\n- Current cron job: `{job_id}`.\n"
                "- No active A4P intent authorization is available. Protected calls fail closed because cron "
                "cannot approve interactively."
                if english
                else f"# A4P 定时任务授权范围\n\n- 当前定时任务：`{job_id}`。\n"
                "- 当前任务没有可用的 A4P intent 授权。定时执行无法交互审批，受保护调用将 fail-closed。"
            )
        else:
            display_actions = [
                {key: value for key, value in action.items() if key != "allowExtraParams"}
                for action in summary.get("actions") or []
                if isinstance(action, dict)
            ]
            actions_json = json.dumps(display_actions, ensure_ascii=False, indent=2, sort_keys=True)
            if english:
                content = (
                    f"# A4P Cron Authorization Scope\n\n- Current cron job: `{job_id}`.\n"
                    f"- Expires at: `{summary.get('expireAt') or 'unknown'}`.\n"
                    f"- Authorized actions:\n```json\n{actions_json}\n```\n"
                    "Only invoke protected tools inside this scope; out-of-scope, expired, or identity-mismatched "
                    "calls fail closed."
                )
            else:
                content = (
                    f"# A4P 定时任务授权范围\n\n- 当前定时任务：`{job_id}`。\n"
                    f"- 到期时间：`{summary.get('expireAt') or 'unknown'}`。\n"
                    f"- 已授权 actions：\n```json\n{actions_json}\n```\n"
                    "仅调用 scope 内的受保护工具；scope 外、过期或 identity 不匹配时 fail-closed。"
                )
        await self._upsert_prompt_attachment(
            ctx,
            section=section,
            content=content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=94,
        )

    async def _sync_git_system_context(
        self,
        ctx: AgentCallbackContext,
        runtime_state: dict[str, Any],
    ) -> None:
        """Install the conversation git snapshot in the cacheable system prefix."""
        # Clear the legacy per-model-call attachment when upgrading a live agent.
        await self._clear_prompt_attachment(ctx, section="git_status")
        if self.system_prompt_builder is None:
            return

        self.system_prompt_builder.remove_section("git_status")
        git_branch = str(runtime_state.get("git_branch") or "").strip()
        if git_branch and git_branch != "N/A":
            git_main_branch = str(runtime_state.get("git_main_branch") or "").strip()
            git_status_text = str(runtime_state.get("git_status") or "").strip()
            git_recent_commits = str(runtime_state.get("git_recent_commits") or "").strip()
            git_user = str(runtime_state.get("git_user") or "").strip()
            git_lines = [
                "This is the git status at the start of the conversation. "
                "Note that this status is a snapshot in time, and will not update during the conversation. "
                "Run git yourself when you need the current state — for example before staging or "
                "committing, or after anything may have changed the working tree.",
                f"Current branch: {git_branch}",
            ]
            if git_main_branch:
                git_lines.append(
                    f"Main branch (you will usually use this for PRs): {git_main_branch}"
                )
            if git_user:
                git_lines.append(f"Git user: {git_user}")
            git_lines.append(f"Status:\n{git_status_text or '(clean)'}")
            git_lines.append(f"Recent commits:\n{git_recent_commits or '(none)'}")
            git_content = "\n\n".join(git_lines)
            self.system_prompt_builder.add_section(PromptSection(
                name="git_status",
                content={"cn": git_content, "en": git_content},
                priority=SystemPromptPriority.GIT_STATUS,
            ))

    async def _upsert_prompt_attachment(
        self,
        ctx: AgentCallbackContext,
        *,
        section: str,
        content: str,
        kind: PromptAttachmentKind,
        priority: int,
    ) -> bool:
        if self.attachment_manager is None:
            logger.warning(
                "[RuntimePromptRail] prompt attachment manager unavailable; skip dynamic section=%s",
                section,
            )
            return False
        try:
            writer = self.attachment_manager.bind_context(ctx)
            await writer.add_section(
                section,
                content,
                kind,
                "jiuwenswarm.runtime_prompt_rail",
                priority=priority,
                content_kind="text/markdown",
            )
            return True
        except ValueError as exc:
            logger.warning("[RuntimePromptRail] skip prompt attachment section=%s: %s", section, exc)
            return False

    async def _clear_prompt_attachment(
        self,
        ctx: AgentCallbackContext,
        *,
        section: str,
    ) -> None:
        if self.attachment_manager is None:
            return
        try:
            await self.attachment_manager.bind_context(ctx).clear_section(section)
        except ValueError as exc:
            logger.warning("[RuntimePromptRail] skip clearing prompt attachment section=%s: %s", section, exc)
