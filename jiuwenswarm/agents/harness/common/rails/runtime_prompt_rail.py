# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Inject dynamic time/runtime info per model call.

Time and runtime state (model, mode, language, etc.) are injected fresh on
every model call by reading runtime_state.yaml in Python, so the LLM always
sees the current values without needing to call any tool.
"""
from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

_LANGUAGE_NAMES = {"cn": "Chinese (Simplified)", "zh": "Chinese (Simplified)", "en": "English"}


class RuntimePromptRail(DeepAgentRail):
    """在 before_model_call 中注入时间及运行时状态文件路径。"""

    priority = 5  # 高优先级，确保早于其他 rail 执行

    def __init__(
        self,
        language: str = "cn",
        channel: str = "web",
        timezone_offset: int = 8,
        agent_id: str | None = None,
        service_id: str | None = None,
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
        self._request_system_prompt: str = ""
        self._agent_id = agent_id
        self._service_id = service_id

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
            self.system_prompt_builder.remove_section("browser_tool_policy")
            self.system_prompt_builder.remove_section("tui_current_project_policy")
            self.system_prompt_builder.remove_section("trusted_dirs_policy")
            self.system_prompt_builder.remove_section("request_system_prompt")
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
        """Force English for scaffolding sections (time/runtime/env) in code mode.

        Does NOT affect the Language section (response language), which always
        follows the user's preferred language set via :meth:`set_language`.
        """
        self._force_english = force

    def set_request_system_prompt(self, prompt: str | None) -> None:
        """per-request 更新 system prompt 追加内容。"""
        value = prompt.strip() if isinstance(prompt, str) else ""
        self._request_system_prompt = value

    def _resolve_agent_workspace_and_config(self) -> tuple[str, str]:
        """Resolve agent workspace / config dirs; enterprise uses workspace_key paths."""
        if self._workspace_dir and is_enterprise():
            workspace_root = Path(self._workspace_dir)
            base_workspace = workspace_root.parent.parent
            return str(workspace_root), str(base_workspace / "config")
        if self._workspace_dir:
            return (
                str(Path(self._workspace_dir)),
                str(get_user_workspace_dir() / "config"),
            )
        return (
            str(get_agent_workspace_dir()),
            str(get_user_workspace_dir() / "config"),
        )

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
            "browser_tool_policy",
            "tui_current_project_policy",
            "trusted_dirs_policy",
            "request_system_prompt",
        ):
            self.system_prompt_builder.remove_section(name)

        # ── time ──
        # Inject real date values (day-level precision only, not HH:MM) to avoid
        # repeated date derivations in reasoning.  Priority 130 places this after
        # FINAL_VISIBLE_REPLY(120) so the dynamic suffix does not break prefix
        # caching of the static sections above.
        now = datetime.now(timezone.utc).astimezone()
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        weekday_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][now.weekday()]
        week_start = now - timedelta(days=now.weekday())
        week_end = now + timedelta(days=6 - now.weekday())

        if not self._force_english and self._language == "cn":
            time_content = (
                "# 时间说明\n\n"
                f"当前日期：{now.strftime('%Y-%m-%d')}（{weekday_cn}）\n"
                f"本周日期范围：{week_start.strftime('%Y-%m-%d')} 至 {week_end.strftime('%Y-%m-%d')}\n"
                "- 当用户询问“最新、当前、今年、本年、实时、近期”等信息并需要搜索时，"
                "搜索 query 必须优先使用当前年份或日期"
            )
        else:
            time_content = (
                "# Time Description\n\n"
                f"Current date: {now.strftime('%Y-%m-%d')} ({weekday_en})\n"
                f"This week: {week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}\n"
                "- When the user asks for latest/current/this-year/recent information and search is needed, "
                "search queries must prefer the current year or date."
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="time",
            content={"cn": time_content, "en": time_content},
            priority=130,
        ))

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
        # Language section controls the model's *response* language and must
        # follow the user's preferred language.  ``_force_english`` only
        # affects system-prompt scaffolding (time / runtime / env sections),
        # NOT the response language, so that code mode (which sets
        # _force_english=True for English scaffolding) still replies in the
        # user's chosen language.
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
            model_answer_policy = (
                "# 模型名称回答策略\n\n"
                "- 当用户询问「你是什么模型」「当前用的是哪个模型」等问题时，"
                "直接使用 `runtime.setting` 中的当前模型值回答，只说模型名称，不要介绍身份或列出能力。\n"
                "- 当用户询问「你支持/配置了哪些模型」「有哪些模型可用」等问题时，"
                "直接使用 `runtime.setting` 中的可用模型列表回答，列出所有已配置的模型名称。"
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
            model_answer_policy = (
                "# Model Name Answer Policy\n\n"
                "- When the user asks what model you are using, answer with only the current model "
                "value from `runtime.setting`. Do not introduce yourself or list capabilities.\n"
                "- When the user asks what models are available or configured, list all available "
                "models from the `runtime.setting` available_models list."
            )

        self.system_prompt_builder.add_section(PromptSection(
            name="runtime.model_answer_policy",
            content={"cn": model_answer_policy, "en": model_answer_policy},
            priority=95,
        ))

        await self._clear_prompt_attachment(ctx, section="runtime.setting")
        await self._upsert_prompt_attachment(
            ctx,
            section="runtime.setting",
            content=runtime_content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=95,
        )

        # ── Language output constraint (injected near end) ──
        language_name = _LANGUAGE_NAMES.get(language_val, language_val)
        language_output_content = (
            "# Language\n\n"
            f"Always respond in {language_name}, regardless of the language "
            f"used in the user's message. Even if the user writes in another "
            f"language, you must still respond in {language_name}. "
            f"Use {language_name} for all explanations, comments, "
            f"and communications with the user. "
            f"Technical terms and code identifiers should remain "
            f"in their original form."
        )
        self.system_prompt_builder.add_section(PromptSection(
            name="language_output",
            content={"cn": language_output_content, "en": language_output_content},
            priority=93,
        ))

        # ── Platform / OS environment section ──
        os_type = sys.platform
        shell_path = os.environ.get("SHELL", "")
        shell_name = os.path.basename(shell_path) if shell_path else "unknown"
        import platform as plat
        os_version = f"{plat.system()} {plat.release()}"
        env_language = "cn" if not self._force_english and self._language == "cn" else "en"
        shell_env_prompt = build_shell_environment_prompt(env_language, os_type)

        if not self._force_english and self._language == "cn":
            env_content = (
                "# 运行环境\n\n"
                f"- 当前运行平台：`{os_type}`\n"
                f"- Shell：{shell_name}\n"
                f"- OS 版本：{os_version}\n\n"
                f"{shell_env_prompt}\n\n"
                "## 平台命令差异（仅在必须使用 shell 时参考）\n\n"
                "以下命令差异仅适用于测试、构建、git、包管理、运行脚本等必须调用 shell 的场景。"
                "文件读取、编辑、搜索仍应优先使用专用工具。\n\n"
                "| 操作 | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |\n"
                "|------|---------------------------|-------------------------------|\n"
                "| 创建目录 | `mkdir folder` 或 PowerShell "
                "`New-Item -ItemType Directory -Path folder` "
                "| `mkdir -p folder` |\n"
                "| 删除文件 | `del file.txt` 或 PowerShell `Remove-Item file.txt` | `rm file.txt` |\n"
                "| 删除目录 | `rmdir folder` 或 PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |\n"
                "| 查找文件 | `dir /s pattern` 或 PowerShell "
                "`Get-ChildItem -Recurse -Filter pattern` "
                "| `find . -name pattern` |\n\n"
                "**特别注意**：Windows 的 cmd/PowerShell `mkdir` 不支持 `-p` 参数；"
                "只有在 Shell 能力显示 Git Bash/PATH bash 可用且实际使用 bash/Git Bash 时，"
                "`mkdir -p` 才是合适的。"
                "如需在 cmd/PowerShell 中创建嵌套目录，请使用 PowerShell "
                "`New-Item -ItemType Directory -Path \"parent/child\" -Force`，"
                "或使用 cmd 分步创建 `mkdir parent && mkdir parent\\child`。"
            )
        else:
            env_content = (
                "# Environment\n\n"
                f"- Current platform: `{os_type}`\n"
                f"- Shell: {shell_name}\n"
                f"- OS Version: {os_version}\n\n"
                f"{shell_env_prompt}\n\n"
                "## Platform Command Differences (only when shell is required)\n\n"
                "The following command differences apply only to scenarios where shell execution is required "
                "(testing, builds, git, package management, running scripts). "
                "File reading, editing, and searching should still prefer dedicated tools.\n\n"
                "| Operation | Windows (`win32`/`win64`) | Linux/macOS (`linux`/`darwin`) |\n"
                "|-----------|---------------------------|-------------------------------|\n"
                "| Create directory | `mkdir folder` or PowerShell "
                "`New-Item -ItemType Directory -Path folder` "
                "| `mkdir -p folder` |\n"
                "| Delete file | `del file.txt` or PowerShell `Remove-Item file.txt` | `rm file.txt` |\n"
                "| Delete directory | `rmdir folder` or PowerShell `Remove-Item -Recurse folder` | `rm -rf folder` |\n"
                "| Find file | `dir /s pattern` or PowerShell "
                "`Get-ChildItem -Recurse -Filter pattern` "
                "| `find . -name pattern` |\n\n"
                "**WARNING**: Windows cmd/PowerShell `mkdir` does NOT support the `-p` flag; "
                "`mkdir -p` is appropriate only when Shell capabilities show Git Bash/PATH bash "
                "is available and you are actually using bash/Git Bash. "
                "To create nested directories in cmd/PowerShell, use either PowerShell "
                "`New-Item -ItemType Directory -Path \"parent/child\" -Force` "
                "or cmd with step-by-step creation `mkdir parent && mkdir parent\\\\child`."
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

        # ── Channel: browser_tool_policy or trusted_dirs_policy──
        if self._channel == "web":
            browser_tool_policy = (
                "# Browser Tool Policy\n\n"
                "- For browser tasks such as opening pages, navigation, clicking, typing, login, screenshots, "
                "page inspection, or extracting data from a live website, use `task_tool` with "
                '`subagent_type` set to `"browser_agent"` and put the full browser objective in '
                "`task_description`.\n"
                "- Do not use bash, execute_code, subprocess, shell commands, or direct Chrome/Edge launches "
                "for browser automation.\n"
                "- If `task_tool` or `browser_agent` is unavailable, say that the browser "
                "subagent is unavailable before trying to start a browser through commands."
            )
            self.system_prompt_builder.add_section(PromptSection(
                name="browser_tool_policy",
                content={"cn": browser_tool_policy, "en": browser_tool_policy},
                priority=98,
            ))

        # Directory / trusted-dirs policy: needed for any interactive channel that
        # may bind project_dir (officeclaw included). Previously limited to tui/web,
        # so officeclaw agents never saw the relay project path and wrote absolute
        # paths under the agent_default workspace instead.
        if (
            self._channel in ("tui", "web", "officeclaw")
            or self._project_dir
            or self._cwd
        ):
            trusted_dirs = self._existing_dirs(self._trusted_dirs)
            # Prefer explicit per-agent workspace; enterprise uses multi-tenant paths.
            resolved_ws, config_dir = self._resolve_agent_workspace_and_config()
            agent_workspace_dir = self._existing_dir(self._workspace_dir) or resolved_ws
            project_dir = self._existing_dir(self._project_dir)
            runtime_cwd = (
                self._existing_dir(self._cwd)
                or project_dir
                or agent_workspace_dir
            )
            has_project = project_dir is not None
            cwd_is_agent_workspace = self._same_path(runtime_cwd, agent_workspace_dir)
            workspace_boundary = project_dir or runtime_cwd
            other_dirs = []
            for path in trusted_dirs:
                if self._same_path(path, workspace_boundary):
                    continue
                if self._same_path(path, runtime_cwd):
                    continue
                other_dirs.append(path)
            cn_dirs_display = ", ".join(other_dirs) if other_dirs else "无"
            en_dirs_display = ", ".join(other_dirs) if other_dirs else "none"
            logger.info(
                "[RuntimePromptRail] directory context: channel=%s project_dir=%s "
                "cwd=%s agent_workspace=%s has_project=%s",
                self._channel,
                project_dir,
                runtime_cwd,
                agent_workspace_dir,
                has_project,
            )

            if not self._force_english and self._language == "cn":
                if has_project:
                    current_project_policy = (
                        "# 运行时目录上下文\n\n"
                        f"- 当前项目目录（项目根目录，也是本次任务的 workspace 边界）：{project_dir}\n"
                        f"- 当前工作目录（cwd，也是 Bash 默认执行目录）：{runtime_cwd}\n"
                        f"- Agent 内部数据目录：{agent_workspace_dir}\n"
                        f"- JiuwenSwarm 启动配置目录：{config_dir}\n\n"
                        "目录含义：\n"
                        "- 当前项目目录是用户正在处理的项目根目录，也是本次任务文件操作的主要边界。\n"
                        "- 当前工作目录是 Bash 未显式传入 `workdir` 时的默认执行目录，也是相对路径的解析基准。\n"
                        "- 当前工作目录可以等于项目目录，也可以是项目目录中的子目录。\n"
                        "- Agent 内部数据目录只保存身份、记忆、技能、待办和运行状态，不属于用户项目。\n"
                        "- JiuwenSwarm 启动配置目录只保存 JiuwenSwarm 自身配置，不属于用户项目中的 `config/`。\n\n"
                        "回答规则：\n"
                        "- 用户询问“当前项目”“项目目录”“项目工作空间”或未加限定的 workspace 时，回答当前项目目录。\n"
                        "- 用户询问“当前工作目录”“cwd”或“Bash 在哪里执行”时，回答当前工作目录。\n"
                        "- 用户询问“Agent 工作空间”“Agent 数据目录”时，回答 Agent 内部数据目录。\n"
                        "- 用户询问“JiuwenSwarm 配置目录”时，回答 JiuwenSwarm 启动配置目录。\n"
                    )
                    trusted_dirs_content = (
                        "# 工作目录策略\n\n"
                        f"- 当前项目目录：{project_dir}\n"
                        f"- 当前工作目录：{runtime_cwd}\n"
                        f"- 其他可访问目录（可读写其中的资源，但不是当前项目目录）：{cn_dirs_display}\n\n"
                        "重要规则：\n"
                        "- 项目文件搜索、代码读取与编辑、测试、构建和 Git 操作应限制在当前项目目录内。\n"
                        "- 用户任务中的相对路径相对于当前工作目录解析。\n"
                        "- Bash 工具未显式传入 `workdir` 时，默认在当前工作目录中执行。\n"
                        "- Bash 工具显式传入 `workdir` 时，本次命令在指定目录中执行。\n"
                        "- 不要依赖一次 Bash 调用中的 `cd` 改变后续工具调用的工作目录。\n"
                        "- 不要在 Agent 内部数据目录中搜索、运行或修改用户项目文件。\n"
                        "- 不要将项目中的 `config/` 自动解释为 JiuwenSwarm 启动配置目录。\n"
                        "- 若操作涉及当前项目目录和其他已授权目录之外的路径，应先向用户确认。\n"
                    )
                elif cwd_is_agent_workspace:
                    current_project_policy = (
                        "# 运行时目录上下文\n\n"
                        "- 当前项目目录：未设置\n"
                        f"- 当前工作目录（cwd，也是 Bash 默认执行目录）：{runtime_cwd}\n"
                        f"- Agent 内部数据目录：{agent_workspace_dir}\n"
                        f"- JiuwenSwarm 启动配置目录：{config_dir}\n\n"
                        "当前没有绑定用户项目，也没有传入独立的任务工作路径。"
                        "当前工作目录暂时回退到 Agent 内部数据目录。\n\n"
                        "重要规则：\n"
                        "- 当前目录虽然是 Bash 默认执行目录，但它仍然是 Agent 内部数据目录，不是用户项目。\n"
                        "- 不要假设其中存在项目代码、Git 仓库或用户项目配置。\n"
                        "- 可以在其中执行普通聊天、记忆、待办和非项目型文件任务。\n"
                        "- 用户要求搜索、编辑、测试或构建项目时，应先获得明确的项目路径。\n"
                        "- 任务中出现 `config/` 时，不要自动将其解释为 JiuwenSwarm 启动配置目录。\n"
                    )
                    trusted_dirs_content = (
                        "# 工作目录策略\n\n"
                        f"- 当前工作目录：{runtime_cwd}\n"
                        f"- 其他可访问目录：{cn_dirs_display}\n\n"
                        "重要规则：\n"
                        "- 当前没有项目目录，不要把当前工作目录称为项目目录。\n"
                        "- Bash 工具未显式传入 `workdir` 时，默认在当前工作目录中执行。\n"
                        "- 用户任务中的相对路径相对于当前工作目录解析。\n"
                        "- 用户要求处理项目时，应先获得明确的项目路径。\n"
                    )
                else:
                    current_project_policy = (
                        "# 运行时目录上下文\n\n"
                        "- 当前项目目录：未设置\n"
                        f"- 当前工作目录（cwd，也是 Bash 默认执行目录）：{runtime_cwd}\n"
                        f"- Agent 内部数据目录：{agent_workspace_dir}\n"
                        f"- JiuwenSwarm 启动配置目录：{config_dir}\n\n"
                        "当前没有绑定用户项目。\n\n"
                        "目录含义：\n"
                        "- 当前工作目录是本次任务的文件操作目录和相对路径解析基准，但不要称其为项目目录。\n"
                        "- Bash 未显式传入 `workdir` 时，默认在当前工作目录中执行。\n"
                        "- 不要假设当前工作目录是 Git 仓库或代码项目。\n"
                        "- Agent 内部数据目录只保存 Agent 自身数据。\n"
                        "- 用户要求处理某个项目时，应先获得明确的项目路径。\n"
                        "- 任务中出现 `config/` 时，不要自动将其解释为 JiuwenSwarm 启动配置目录。\n"
                    )
                    trusted_dirs_content = (
                        "# 工作目录策略\n\n"
                        f"- 当前工作目录：{runtime_cwd}\n"
                        f"- 其他可访问目录：{cn_dirs_display}\n\n"
                        "重要规则：\n"
                        "- 当前工作目录是本次任务的主要文件操作范围，但不是项目目录。\n"
                        "- Bash 工具未显式传入 `workdir` 时，默认在当前工作目录中执行。\n"
                        "- 用户任务中的相对路径相对于当前工作目录解析。\n"
                        "- 用户要求处理项目时，应先获得明确的项目路径。\n"
                    )
            else:
                if has_project:
                    current_project_policy = (
                        "# Runtime Directory Context\n\n"
                        f"- Current project directory (project root and workspace boundary): {project_dir}\n"
                        f"- Current working directory (cwd and Bash default directory): {runtime_cwd}\n"
                        f"- Agent internal data directory: {agent_workspace_dir}\n"
                        f"- JiuwenSwarm startup configuration directory: {config_dir}\n\n"
                        "Directory semantics:\n"
                        "- The current project directory is the user project's root "
                        "and the main file-operation boundary.\n"
                        "- The current working directory is Bash's default directory when `workdir` is omitted "
                        "and the base for relative paths.\n"
                        "- The current working directory may equal the project root or be a subdirectory inside it.\n"
                        "- The Agent internal data directory stores identity, memory, skills, todos, "
                        "and runtime state; it is not part of the user project.\n"
                        "- The JiuwenSwarm startup configuration directory stores JiuwenSwarm's own "
                        "configuration; it is not the project's `config/` directory.\n\n"
                        "Answering rules:\n"
                        "- For the current project, project directory, project workspace, or an unqualified "
                        "workspace, answer with the current project directory.\n"
                        "- For the current working directory, cwd, or Bash execution directory, "
                        "answer with the current working directory.\n"
                        "- For the Agent workspace or Agent data directory, "
                        "answer with the Agent internal data directory.\n"
                        "- For the JiuwenSwarm configuration directory, "
                        "answer with the JiuwenSwarm startup configuration directory.\n"
                    )
                    trusted_dirs_content = (
                        "# Working Directory Policy\n\n"
                        f"- Current project directory: {project_dir}\n"
                        f"- Current working directory: {runtime_cwd}\n"
                        f"- Other accessible directories: {en_dirs_display}\n\n"
                        "Important rules:\n"
                        "- Project searches, code reads and edits, tests, builds, and Git operations "
                        "must stay within the current project directory.\n"
                        "- Relative paths in user tasks are resolved against the current working directory.\n"
                        "- Bash runs in the current working directory when `workdir` is omitted.\n"
                        "- When Bash is given an explicit `workdir`, that directory applies to that command.\n"
                        "- Do not rely on `cd` in one Bash call to change the working directory of later tool calls.\n"
                        "- Do not search for, run, or modify user project files in the Agent internal data directory.\n"
                        "- Do not map the project's relative `config/` path to the JiuwenSwarm startup "
                        "configuration directory.\n"
                        "- Ask the user before operating outside the current project "
                        "and other authorized directories.\n"
                    )
                elif cwd_is_agent_workspace:
                    current_project_policy = (
                        "# Runtime Directory Context\n\n"
                        "- Current project directory: not set\n"
                        f"- Current working directory (cwd and Bash default directory): {runtime_cwd}\n"
                        f"- Agent internal data directory: {agent_workspace_dir}\n"
                        f"- JiuwenSwarm startup configuration directory: {config_dir}\n\n"
                        "No user project or independent task path is currently bound. "
                        "The current working directory has temporarily fallen back to "
                        "the Agent internal data directory.\n\n"
                        "Important rules:\n"
                        "- Although this is Bash's default directory, it remains the Agent internal data directory "
                        "and is not a user project.\n"
                        "- Do not assume that it contains project code, a Git repository, "
                        "or user project configuration.\n"
                        "- Ordinary conversation, memory, todo, and non-project file tasks may be performed here.\n"
                        "- Obtain an explicit project path before searching, editing, testing, or building a project.\n"
                        "- Do not map a task's relative `config/` path to the JiuwenSwarm startup "
                        "configuration directory.\n"
                    )
                    trusted_dirs_content = (
                        "# Working Directory Policy\n\n"
                        f"- Current working directory: {runtime_cwd}\n"
                        f"- Other accessible directories: {en_dirs_display}\n\n"
                        "Important rules:\n"
                        "- No project directory is set; do not call the current working directory "
                        "a project directory.\n"
                        "- Bash runs in the current working directory when `workdir` is omitted.\n"
                        "- Relative paths in user tasks are resolved against the current working directory.\n"
                        "- Obtain an explicit project path before performing project work.\n"
                    )
                else:
                    current_project_policy = (
                        "# Runtime Directory Context\n\n"
                        "- Current project directory: not set\n"
                        f"- Current working directory (cwd and Bash default directory): {runtime_cwd}\n"
                        f"- Agent internal data directory: {agent_workspace_dir}\n"
                        f"- JiuwenSwarm startup configuration directory: {config_dir}\n\n"
                        "No user project is currently bound.\n\n"
                        "Directory semantics:\n"
                        "- The current working directory is this task's file-operation directory "
                        "and relative-path base, but it is not a project directory.\n"
                        "- Bash runs in the current working directory when `workdir` is omitted.\n"
                        "- Do not assume that the current working directory is a Git repository or code project.\n"
                        "- The Agent internal data directory stores only the Agent's own data.\n"
                        "- Obtain an explicit project path before performing project work.\n"
                        "- Do not map a task's relative `config/` path to the JiuwenSwarm startup "
                        "configuration directory.\n"
                    )
                    trusted_dirs_content = (
                        "# Working Directory Policy\n\n"
                        f"- Current working directory: {runtime_cwd}\n"
                        f"- Other accessible directories: {en_dirs_display}\n\n"
                        "Important rules:\n"
                        "- The current working directory is this task's main file-operation scope, "
                        "but it is not a project directory.\n"
                        "- Bash runs in the current working directory when `workdir` is omitted.\n"
                        "- Relative paths in user tasks are resolved against the current working directory.\n"
                        "- Obtain an explicit project path before performing project work.\n"
                    )

            self.system_prompt_builder.add_section(PromptSection(
                name="tui_current_project_policy",
                content={"cn": current_project_policy, "en": current_project_policy},
                priority=99,
            ))
            self.system_prompt_builder.add_section(PromptSection(
                name="trusted_dirs_policy",
                content={"cn": trusted_dirs_content, "en": trusted_dirs_content},
                priority=90,
            ))

        if self._request_system_prompt:
            # priority 需高于 RuntimePromptRail 其它 section（最高 99），保证拼在系统提示词末尾
            self.system_prompt_builder.add_section(PromptSection(
                name="request_system_prompt",
                content={"cn": self._request_system_prompt, "en": self._request_system_prompt},
                priority=110,
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
