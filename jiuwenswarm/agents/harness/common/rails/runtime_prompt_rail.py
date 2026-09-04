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
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.workspace_content.workspace_header import (
    IMPORTANT_FILES,
)

from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.agents.harness.common.prompt.shell_environment import build_shell_environment_prompt
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import _runtime_env_message_rules_text
from jiuwenswarm.common.config import get_sandbox_runtime
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
            self.system_prompt_builder.remove_section("runtime.binary_context")
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

        # ── Managed binary runtime paths ──
        # Desktop packaged builds inject these values before spawning AgentServer.
        # Keep this as a dynamic prompt attachment so upgrades/user data roots are
        # reflected without hard-coding a machine-specific path in templates.
        binary_vars = (
            ("Python", "CLAW_PYTHON_EXE"),
            ("Node.js", "CLAW_NODE_EXE"),
            ("npm", "CLAW_NPM_CMD"),
            ("npx", "CLAW_NPX_CMD"),
            ("Git", "CLAW_GIT_EXE"),
            ("Git Bash", "CLAW_GIT_BASH_EXE"),
        )
        binary_lines = []
        for label, env_name in binary_vars:
            value = (os.environ.get(env_name) or "").strip()
            if value:
                binary_lines.append(f"- {label}: `{value}`")
        runtime_source = (os.environ.get("CLAW_RUNTIME_SOURCE") or "unknown").strip()
        if env_language == "cn":
            binary_content = (
                "## 受管工具链\n\n"
                f"- 来源：`{runtime_source}`\n"
                + ("\n".join(binary_lines) if binary_lines else "- 未注入受管工具路径")
                + "\n\n"
                "使用规则：优先使用上面列出的绝对路径或对应的 `CLAW_*` 路径变量；"
                "不要用本机的 python/node/npx/git 裸命令覆盖已提供的受管运行时。"
            )
        else:
            binary_content = (
                "## Managed Toolchain\n\n"
                f"- Source: `{runtime_source}`\n"
                + ("\n".join(binary_lines) if binary_lines else "- No managed runtime paths injected")
                + "\n\n"
                "Usage: prefer the absolute paths above or the corresponding `CLAW_*` path variables; "
                "do not replace an injected managed runtime with bare python/node/npx/git commands."
            )
        await self._clear_prompt_attachment(ctx, section="runtime.binary_context")
        await self._upsert_prompt_attachment(
            ctx,
            section="runtime.binary_context",
            content=binary_content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=95,
        )

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

        if not self._force_english and self._language == "cn":
            env_content = (
                "# 运行环境\n\n"
                "## 平台与 Shell\n\n"
                f"- 当前运行平台：`{os_type}`\n"
                f"- OS 版本：{os_version}\n"
                f"- Shell：{shell_name}\n\n"
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
                f"或使用 cmd 分步创建 `mkdir parent && mkdir parent\\child`.{sandbox_perm_cn}\n\n"
                "## 编码兼容性\n\n"
                "- 代码将在 GBK 控制台或仅支持 GBK 的工具中运行时，避免直接使用 GBK 无法编码的 Emoji 和特殊字符。\n"
                "- 必须使用这些字符时，选择明确支持 UTF-8 的执行工具或显式配置 UTF-8 编码。\n\n"
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
                f"or cmd with step-by-step creation `mkdir parent && mkdir parent\\\\child`.{sandbox_perm_en}\n\n"
                "## Encoding Compatibility\n\n"
                "- When code will run in a GBK console or a tool that supports only GBK, avoid Emoji and "
                "special characters that GBK cannot encode.\n"
                "- If those characters are required, use a tool that explicitly supports UTF-8 or "
                "configure UTF-8 encoding.\n\n"
            "## Current Channel\n\n"
            f"- Current channel: `{channel}`"
        )

        # ── Input Instructions / Output Rules / Subagent Usage Rules ──
        # Shared across office / code / design / team profiles. Appended after
        # the CN/EN branch so both language paths receive the same content
        # (English-only, mirroring the office/code/design all-English policy).
        env_content += "\n\n" + _runtime_env_message_rules_text()

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

        # ── Channel: directory and runtime context (consolidated, last) ──
        # Merge what used to be separate sections — internal data dirs, workspace
        # important files, working-directory strategy, runtime directory context —
        # into ONE section placed at the very end (priority 96 > runtime.setting 95,
        # env 89) so the static KV-cache prefix stays stable and all user/directory-
        # dependent dynamic text lives together at the tail.
        # Remove the consolidated section, legacy sections, and the openjiuwen
        # WORKSPACE section first so switching channels cannot leave stale guidance
        # and so the important-files table is not duplicated.
        self.system_prompt_builder.remove_section("directory_boundaries")
        self.system_prompt_builder.remove_section("tui_current_project_policy")
        self.system_prompt_builder.remove_section("trusted_dirs_policy")
        self.system_prompt_builder.remove_section(SectionName.WORKSPACE)
        if self._channel in ("tui", "web", "desktop", "ws_client", "__cron__", "cron"):
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

            is_cn = not self._force_english and self._language == "cn"
            lang_key = "cn" if is_cn else "en"
            important_files = IMPORTANT_FILES.get(lang_key, IMPORTANT_FILES["cn"])
            project_label = (
                f"`{project_dir}`" if has_project
                else ("未设置" if is_cn else "not set")
            )

            if is_cn:
                directory_content = (
                    "# 目录与运行时上下文\n\n"
                    "## 工作目录\n\n"
                    "- 项目目录是你当前的工作空间，"
                    f"当前项目目录是：`{prompt_project_dir}`\n\n"
                    f"{important_files}\n\n"
                    "## 项目目录规则\n\n"
                    "- 用户任务中的相对路径必须相对于当前项目目录路径去解析。\n"
                    "- 命令工具未显式传入 `workdir` 时，默认在当前项目目录执行。\n"
                    "- 用户已经提供明确路径时直接使用，不要重复询问。\n"
                    "- 只有任务确实需要操作某个项目、且现有上下文无法确定项目位置时，才询问项目路径。\n"
                    "- 用户明确指定保存位置时，优先使用用户指定位置；否则，项目代码、测试、配置、构建文件、项目文档、报告、导出文件、图片和数据文件等放在当前工作目录的合理位置。\n\n"
                    "## 小艺 work 内部数据目录\n\n"
                    "小艺 work 使用独立的内部数据目录保存启动配置、Agent 身份、记忆、技能、待办和运行状态。"
                    "这些内部数据目录不等于用户当前处理的项目目录，也不是用户任务中相对路径的默认含义。\n\n"
                    "| 路径 | 类型 | 用途 | 操作建议 |\n"
                    "|------|------|------|----------|\n"
                    f"| `{config_dir}` | 小艺 work 启动配置目录 | 保存 `config.yaml` 和 `.env` | 只有用户明确要求修改 小艺 work 自身配置时才访问 |\n"
                    f"| `{agent_workspace_dir}` | Agent 内部数据目录 | 保存身份、记忆、技能、待办和运行状态 | 不要在其中搜索或运行用户项目文件 |\n"
                    f"| `{agent_workspace_dir}/memory` | Agent 记忆目录 | 保存持久化记忆 | 将其视为 Agent 记忆的一部分 |\n"
                    f"| `{agent_workspace_dir}/skills` | Agent 技能目录 | 保存和读取技能 | 可以读取和调用，不要作为项目目录使用 |\n"
                    f"| `{agent_workspace_dir}/todo` | Agent 待办目录 | 保存任务和待办状态 | 用于任务状态管理 |\n\n"
                    "以下资源由 小艺 work 提供，路径相对于运行时给出的智能体内部数据目录：\n\n"
                    "- `IDENTITY.md`：用户为智能体指定的身份信息。\n"
                    "- `skills/`：当前已安装并启用的技能。\n"
                    "- `todo/`：任务和待办状态。\n\n"
                    "目录规则：\n\n"
                    "- 智能体内部数据目录只保存智能体自身数据，不是用户项目目录。\n"
                    "- 智能体身份、记忆、技能、待办和运行状态只能保存在对应的内部数据目录。\n"
                    f"- 技能执行产生的内部技能资产放在 `{agent_workspace_dir}/skills/{{skill_name}}/`。\n"
                    "- 小艺 work 启动配置目录不得用于保存普通任务产物。\n"
                    "- 用户任务中的 `config/`、`memory/`、`skills/`、`todo/` 或 `workspace/` 不自动映射到 小艺 work 内部目录。\n\n"
                    "## 运行时目录上下文\n\n"
                    f"- 当前项目目录：{project_label}\n"
                    f"- 当前工作目录（cwd，也是命令工具默认执行目录）：`{runtime_cwd}`\n"
                    f"- Agent 内部数据目录：`{agent_workspace_dir}`\n"
                    f"- 小艺 work 启动配置目录：`{config_dir}`"
                )
            else:
                directory_content = (
                    "# Directory and Runtime Context\n\n"
                    "## Working Directory\n\n"
                    "- The project directory is your current workspace; "
                    f"the current project directory is: `{prompt_project_dir}`\n\n"
                    f"{important_files}\n\n"
                    "## Project Directory Rules\n\n"
                    "- Resolve relative paths in user tasks against the current project directory.\n"
                    "- When a command tool is called without an explicit `workdir`, run it in the current "
                    "project directory.\n"
                    "- When the user has provided an explicit path, use it directly without asking again.\n"
                    "- Ask for a project path only when the task truly requires a project and its location "
                    "cannot be determined from the existing context.\n"
                    "- Prefer a user-specified save location. Otherwise, place project code, tests, "
                    "configuration, build files, project documentation, reports, exports, images, and data "
                    "files in an appropriate location under the current working directory.\n\n"
                    "## 小艺 work Internal Data Directories\n\n"
                    "小艺 work keeps its startup configuration, Agent identity, memory, skills, "
                    "todos, and runtime state in dedicated internal data directories. These are not the "
                    "same as the project directory the user is currently working on, nor are they the "
                    "default meaning of relative paths in user tasks.\n\n"
                    "| Path | Type | Purpose | Suggested use |\n"
                    "|------|------|---------|---------------|\n"
                    f"| `{config_dir}` | 小艺 work startup config directory | stores `config.yaml` and `.env` | access only when the user explicitly asks to change 小艺 work's own config |\n"
                    f"| `{agent_workspace_dir}` | Agent internal data directory | stores identity, memory, skills, todos, runtime state | do not search or run user project files inside it |\n"
                    f"| `{agent_workspace_dir}/memory` | Agent memory directory | stores persistent memory | treat as part of the Agent's memory |\n"
                    f"| `{agent_workspace_dir}/skills` | Agent skills directory | stores and reads skills | may read and invoke; do not use as a project directory |\n"
                    f"| `{agent_workspace_dir}/todo` | Agent todo directory | stores task and todo state | used for task state management |\n\n"
                    "The following resources are provided by 小艺 work. Their paths are relative to the "
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
                    "- Do not use the 小艺 work startup configuration directory for ordinary task deliverables.\n"
                    "- `config/`, `memory/`, `skills/`, `todo/`, or `workspace/` in a user task do not "
                    "automatically refer to 小艺 work internal directories.\n\n"
                    "## Runtime Directory Context\n\n"
                    f"- Current project directory: {project_label}\n"
                    f"- Current working directory (cwd, also the default command-tool execution directory): `{runtime_cwd}`\n"
                    f"- Agent internal data directory: `{agent_workspace_dir}`\n"
                    f"- 小艺 work startup configuration directory: `{config_dir}`"
                )
            trusted_dirs = [
                path for path in self._existing_dirs(self._trusted_dirs)
                if not self._same_path(path, prompt_project_dir)
                and not self._same_path(path, runtime_cwd)
            ]
            if trusted_dirs:
                trusted_dirs_display = ", ".join(f"`{path}`" for path in trusted_dirs)
                if is_cn:
                    directory_content += (
                        "\n\n## 其他已授权目录\n\n"
                        f"- 可读写但不属于当前项目目录的其他目录：{trusted_dirs_display}\n"
                        "- 对当前项目目录和上述已授权目录之外的路径进行操作前，先向用户确认。"
                    )
                else:
                    directory_content += (
                        "\n\n## Other Authorized Directories\n\n"
                        f"- Other readable and writable directories that are not the current project: {trusted_dirs_display}\n"
                        "- Ask the user before operating outside the current project and these authorized directories."
                    )
            self.system_prompt_builder.add_section(PromptSection(
                name="directory_boundaries",
                content={"cn": directory_content, "en": directory_content},
                priority=96,
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
