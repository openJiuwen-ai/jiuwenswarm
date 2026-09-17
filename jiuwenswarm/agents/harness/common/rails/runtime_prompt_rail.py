# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Inject dynamic time/runtime info per model call.

Time and runtime state (model, mode, language, etc.) are injected fresh on
every model call by reading runtime_state.yaml in Python, so the LLM always
sees the current values without needing to call any tool.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any

import yaml

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
)

from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.agents.harness.common.prompt.shell_environment import build_shell_environment_prompt
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_runtime_state_path,
    get_user_workspace_dir,
    logger,
)


class RuntimePromptRail(DeepAgentRail):
    """在 before_model_call 中注入时间及运行时状态文件路径。"""

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
        self._model_name: str = ""
        self._mode: str = ""
        self._session_id: str | None = None
        self._force_english: bool = False

    def init(self, agent) -> None:
        """从 agent 获取 system_prompt_builder 引用。"""
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self.attachment_manager = getattr(agent, "prompt_attachment_manager", None)

    def uninit(self, agent) -> None:
        """清理注入的 section 并释放引用。"""
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("time")
            self.system_prompt_builder.remove_section("runtime.model_answer_policy")
            self.system_prompt_builder.remove_section("language_output")
            self.system_prompt_builder.remove_section("env")
            self.system_prompt_builder.remove_section("directory_boundaries")
            self.system_prompt_builder.remove_section("tui_current_project_policy")
            self.system_prompt_builder.remove_section("trusted_dirs_policy")
        self._agent = None
        self.system_prompt_builder = None
        self.attachment_manager = None

    def set_language(self, language: str) -> None:
        """per-request 更新语言。"""
        self._language = language

    def set_channel(self, channel: str) -> None:
        """per-request 更新频道。"""
        self._channel = channel

    def set_trusted_dirs(self, trusted_dirs: list[str] | None) -> None:
        """per-request 更新可信目录。"""
        self._trusted_dirs = trusted_dirs

    def set_runtime_paths(
        self,
        *,
        cwd: str | None = None,
        project_dir: str | None = None,
        workspace_dir: str | None = None,
    ) -> None:
        """Per-request stable project identity, dynamic cwd and own workspace.

        Args:
            cwd: Working directory shell runs in and relative paths resolve against.
            project_dir: Project root, when the request is bound to one.
            workspace_dir: This agent's own workspace (artifacts, memory, skills
                view). Team members each have their own; falls back to the
                process-wide agent workspace when unset.
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

    def set_model_name(self, model_name: str) -> None:
        """per-request 更新模型名称，作为文件读取失败时的兜底。"""
        self._model_name = model_name or ""

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

    def set_force_english(self, force: bool) -> None:
        """Force English for runtime scaffolding in code mode."""
        self._force_english = force

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
        if configured_mode not in {"code", "code.normal", "code.plan"}:
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

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
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

        # ── runtime ──
        runtime_state: dict[str, Any] = {}
        state_path = get_runtime_state_path(self._session_id)
        try:
            with open(state_path, encoding="utf-8") as f:
                runtime_state = yaml.safe_load(f) or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Failed to read runtime state file %s: %s", state_path, e)

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
            runtime_state.get("mode") or self._mode or "unknown"
        ).strip()
        mode = self._resolve_current_mode(ctx, configured_mode)
        language_val = (
            self._language
            or runtime_state.get("language")
            or "unknown"
        ).strip()
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
        await self._clear_prompt_attachment(ctx, section="runtime.setting")
        await self._upsert_prompt_attachment(
            ctx,
            section="runtime.setting",
            content=runtime_content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=95,
        )

        # ── Platform, shell, encoding, time-query and channel rules ──
        os_type = sys.platform
        shell_path = os.environ.get("SHELL", "")
        if os_type.startswith("win"):
            # Windows normally has no SHELL variable. Prefer the actual
            # PowerShell executable so the prompt does not report unknown.
            shell_path = shutil.which("pwsh") or shutil.which("powershell") or ""
            shell_name = "PowerShell" if shell_path else "unknown"
        else:
            shell_name = os.path.basename(shell_path) if shell_path else "unknown"
        import platform as plat
        os_version = f"{plat.system()} {plat.release()}"
        env_language = "cn" if not self._force_english and self._language == "cn" else "en"
        shell_env_prompt = build_shell_environment_prompt(env_language, os_type)

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
            priority=89,
        ))

        # ── Git status section ──
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

            await self._upsert_prompt_attachment(
                ctx,
                section="git_status",
                content=git_content,
                kind=PromptAttachmentKind.WORKSPACE_DELTA,
                priority=87,
            )
        else:
            await self._clear_prompt_attachment(
                ctx,
                section="git_status",
            )

        # ── Channel: directory and file-operation boundaries ──
        # Remove both the consolidated section and legacy sections first so
        # switching away from TUI/Web cannot leave stale directory guidance.
        self.system_prompt_builder.remove_section("directory_boundaries")
        self.system_prompt_builder.remove_section("tui_current_project_policy")
        self.system_prompt_builder.remove_section("trusted_dirs_policy")
        if self._channel in ("tui", "web", "ws_client"):
            # This agent's own workspace. Team members each own one; without
            # it (single-agent runs) the process-wide agent workspace is the
            # same directory anyway.
            agent_workspace_dir = self._existing_dir(self._workspace_dir) or str(get_agent_workspace_dir())
            config_dir = str(get_user_workspace_dir() / "config")
            project_dir = self._existing_dir(self._project_dir)
            runtime_cwd = (
                self._existing_dir(self._cwd)
                or project_dir
                or agent_workspace_dir
            )
            has_project = project_dir is not None
            # The prompt consistently calls the active path the project
            # directory. When no explicit project is bound, cwd is the
            # project directory shown to the model.
            prompt_project_dir = project_dir or runtime_cwd

            if not self._force_english and self._language == "cn":
                if has_project:
                    project_path = project_dir
                else:
                    project_path = runtime_cwd
                directory_content = (
                    "# 目录与文件操作边界\n\n"
                    "## 项目目录\n\n"
                    "### 项目目录说明\n\n"
                    "- 项目目录是你当前的工作空间，"
                    f"当前项目目录是：`{project_path}`\n\n"
                    "### 项目目录规则\n\n"
                    "- 用户任务中的相对路径必须相对于当前项目目录路径去解析。\n"
                    "- Bash 未显式传入 `workdir` 时，默认在当前项目目录执行。\n"
                    "- 用户已经提供明确路径时直接使用，不要重复询问。\n"
                    "- 只有任务确实需要操作某个项目、且现有上下文无法确定项目位置时，才询问项目路径。\n"
                    "- 用户明确指定保存位置时，优先使用用户指定位置；否则，项目代码、测试、配置、构建文件、项目文档、报告、导出文件、图片和数据文件等放在当前工作目录的合理位置。\n\n"
                    "## JiuwenSwarm 内部目录\n\n"
                    f"- 智能体内部数据目录：`{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm 启动配置目录：`{config_dir}`\n\n"
                    "以下资源由 JiuwenSwarm 提供，路径相对于运行时给出的智能体内部数据目录：\n\n"
                    "- `IDENTITY.md`：用户为智能体指定的身份信息。\n"
                    "- `skills/`：当前已安装并启用的技能。\n"
                    "- `todo/`：任务和待办状态。\n\n"
                    "目录规则：\n\n"
                    "- 智能体内部数据目录只保存智能体自身数据，不是用户项目目录。\n"
                    "- 智能体身份、记忆、技能、待办和运行状态只能保存在对应的内部数据目录。\n"
                    f"- 技能执行产生的内部技能资产放在 `{agent_workspace_dir}/skills/{{skill_name}}/`。\n"
                    "- JiuwenSwarm 启动配置目录不得用于保存普通任务产物。\n"
                    "- 用户任务中的 `config/`、`memory/`、`skills/`、`todo/` 或 `workspace/` 不自动映射到 JiuwenSwarm 内部目录。"
                )
            else:
                directory_content = (
                    "# Directory and File-Operation Boundaries\n\n"
                    "## Project Directory\n\n"
                    "### Project Directory Description\n\n"
                    "- The project directory is your current workspace; "
                    f"the current project directory is: `{prompt_project_dir}`\n\n"
                    "### Project Directory Rules\n\n"
                    "- Resolve relative paths in user tasks against the current project directory.\n"
                    "- When Bash is called without an explicit `workdir`, run it in the current project "
                    "directory.\n"
                    "- When the user has provided an explicit path, use it directly without asking again.\n"
                    "- Ask for a project path only when the task truly requires a project and its location "
                    "cannot be determined from the existing context.\n"
                    "- Prefer a user-specified save location. Otherwise, place project code, tests, "
                    "configuration, build files, project documentation, reports, exports, images, and data "
                    "files in an appropriate location under the current working directory.\n\n"
                    "## JiuwenSwarm Internal Directories\n\n"
                    f"- Agent internal data directory: `{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm startup configuration directory: `{config_dir}`\n\n"
                    "The following resources are provided by JiuwenSwarm. Their paths are relative to the "
                    "Agent internal data directory supplied at runtime:\n\n"
                    "- `IDENTITY.md`: identity information assigned to the Agent by the user.\n"
                    "- `skills/`: currently installed and enabled skills.\n"
                    "- `todo/`: task and to-do state.\n\n"
                    "Directory rules:\n\n"
                    "- The Agent internal data directory stores only the Agent's own data; it is not a user "
                    "project directory.\n"
                    "- Agent identity, memory, skills, to-dos, and runtime state must be stored only in their "
                    "corresponding internal data directories.\n"
                    f"- Internal skill assets produced by skill execution belong in "
                    f"`{agent_workspace_dir}/skills/{{skill_name}}/`.\n"
                    "- Do not use the JiuwenSwarm startup configuration directory for ordinary task deliverables.\n"
                    "- `config/`, `memory/`, `skills/`, `todo/`, or `workspace/` in a user task do not "
                    "automatically refer to JiuwenSwarm internal directories."
                )
            self.system_prompt_builder.add_section(PromptSection(
                name="directory_boundaries",
                content={"cn": directory_content, "en": directory_content},
                priority=89,
            ))

    async def _upsert_prompt_attachment(
        self,
        ctx: AgentCallbackContext,
        *,
        section: str,
        content: str,
        kind: PromptAttachmentKind,
        priority: int,
    ) -> None:
        if self.attachment_manager is None:
            logger.warning(
                "[RuntimePromptRail] prompt attachment manager unavailable; skip dynamic section=%s",
                section,
            )
            return
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
        except ValueError as exc:
            logger.warning("[RuntimePromptRail] skip prompt attachment section=%s: %s", section, exc)

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
