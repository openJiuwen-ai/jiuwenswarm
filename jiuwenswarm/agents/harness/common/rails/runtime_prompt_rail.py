# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RuntimePromptRail — Assemble stable and dynamic runtime prompt state.

Stable environment rules and the conversation-start git snapshot stay in the
system prompt. Dynamic runtime state is managed as a prompt attachment.
Request date/time remains in the real user message's JSON envelope.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from contextvars import ContextVar
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
from jiuwenswarm.agents.harness.common.prompt.shell_environment import build_shell_environment_prompt
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_runtime_state_path,
    get_user_workspace_dir,
    logger,
)


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

        channel = (runtime_state.get("channel") or self._channel or "unknown").strip()

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
            priority=SystemPromptPriority.ENV,
        ))

        # ── Channel: directory and file-operation boundaries ──
        # Remove both the consolidated section and legacy sections first so
        # switching to a channel without local directory guidance clears it.
        self.system_prompt_builder.remove_section("directory_boundaries")
        self.system_prompt_builder.remove_section("tui_current_project_policy")
        self.system_prompt_builder.remove_section("trusted_dirs_policy")
        if self._channel in ("tui", "web", "ws_client", "process_cli"):
            # This agent's own workspace. Team members each own one; without
            # it (single-agent runs) the process-wide agent workspace is the
            # same directory anyway.
            agent_workspace_dir = self._existing_dir(self._workspace_dir) or str(get_agent_workspace_dir())
            config_dir = str(get_user_workspace_dir() / "config")
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
                    f"- 智能体内部数据目录：`{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm 启动配置目录：`{config_dir}`\n"
                    "- `IDENTITY.md`、`memory/`、`skills/`、`todo/` 和运行状态属于智能体内部数据。\n"
                    f"- 技能执行产生的内部技能资产放在 `{agent_workspace_dir}/skills/{{skill_name}}/`。\n"
                    "- 不要把普通任务产物写入智能体内部目录或启动配置目录。\n"
                    "- 用户任务中的 `config/`、`memory/`、`skills/`、`todo/` 或 `workspace/` 不自动映射到 JiuwenSwarm 内部目录。"
                )
            else:
                directory_content += (
                    "- When the user has provided an explicit path, use it directly without asking again.\n"
                    "- Ask for a project path only when the task truly requires a "
                    "project and its location cannot be determined from the existing "
                    "context.\n\n"
                    "## JiuwenSwarm Internal Directories\n\n"
                    f"- Agent internal data directory: `{agent_workspace_dir}`\n"
                    f"- JiuwenSwarm startup configuration directory: `{config_dir}`\n"
                    "- `IDENTITY.md`, `memory/`, `skills/`, `todo/`, and runtime state are Agent internal data.\n"
                    f"- Internal skill assets produced by skill execution belong in "
                    f"`{agent_workspace_dir}/skills/{{skill_name}}/`.\n"
                    "- Do not write ordinary task artifacts to the Agent internal data "
                    "directory or startup configuration directory.\n"
                    "- `config/`, `memory/`, `skills/`, `todo/`, or `workspace/` in a "
                    "user task do not automatically refer to JiuwenSwarm internal "
                    "directories."
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

    async def _sync_a4p_intent_authorization_attachment(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        section = "a4p.intent_authorization"
        if self._channel != "web" or not self._a4p_authorizer_available:
            await self._clear_prompt_attachment(ctx, section=section)
            return
        try:
            from jiuwenswarm.agents.harness.common.a4p_runtime import is_a4p_enabled
            from jiuwenswarm.agents.harness.common.tools.a4p_tools import (
                get_a4p_supported_action_names,
            )
            from jiuwenswarm.common.config import get_config

            enabled = is_a4p_enabled(get_config())
            action_names = get_a4p_supported_action_names()
        except Exception as exc:
            logger.warning("Failed to resolve A4P intent authorization prompt: %s", exc)
            enabled = False
            action_names = []
        if not enabled:
            await self._clear_prompt_attachment(ctx, section=section)
            return

        actions = "、".join(f"`{name}`" for name in action_names)
        if self._force_english or self._language != "cn":
            actions = ", ".join(f"`{name}`" for name in action_names)
            content = (
                "# A4P Intent Authorization\n\n"
                "## Authorization boundaries\n"
                f"- Web: when using {actions}, call `request_a4p_intent_authorization` before the first protected tool "
                "call. Every protected call must match a valid approved scope, including read-only operations. "
                "Authorization permits execution; it does not execute actions.\n"
                "- Unprotected tools may be used first to read necessary information, discover tool capabilities, and "
                "determine parameters. One authorization may include multiple `actions` covering currently known "
                "scopes. Use real tool names and security-critical parameters; ordinary parameters not used for "
                "authorization matching need not be known in advance.\n"
                "- For Shell tools, authorize each complete exact command by default. Use a narrowly scoped wildcard "
                "only when one dynamic argument cannot be known in advance and the wildcard cannot absorb whitespace, "
                "Shell operators, redirections, or extra arguments.\n"
                "- For file tools, use a concrete file path or a filename glob under a fixed directory. Never use a "
                "bare `*` or substitute a directory path for a file scope. Use dedicated file tools for file writes.\n"
                "\n"
                "## Planning and convergence\n"
                "- Choose a feasible plan that satisfies the user request and has explicit authorization scopes. "
                "Do not "
                "search for an optimal plan or prove that equivalent alternatives are worse.\n"
                "- Plan once: identify necessary operations and dependencies; determine tools and scopes for protected "
                "operations; check requirement coverage, parameter dependencies, and authorization scopes. Once these "
                "checks pass, immediately make the next tool call.\n"
                "- Without new information, do not reconsider selected tools, operation grouping, data sources, or "
                "execution order. Do not restructure a feasible plan to reduce the number of actions.\n"
                "- Revise only affected steps and their dependencies when requirements change, new facts appear, a "
                "tool "
                "returns an error, or a concrete requirement or authorization violation is discovered. Hypothetical "
                "concerns alone do not justify restarting planning.\n"
                "\n"
                "## Unknown information and dynamic parameters\n"
                "- Use facts already established by context and tool documentation without repeatedly verifying them. "
                "If missing information affects execution or authorization, obtain it through appropriate tools rather "
                "than repeatedly guessing tool behavior.\n"
                "- For values produced at runtime, specify how they will be obtained and used in later parameters; "
                "their actual values need not be known in advance.\n"
                "- Plans may describe variables and substitution rules. Authorization requests must use valid concrete "
                "scopes or supported patterns; actual tool calls must use resolved values, never unresolved "
                "placeholders.\n"
                "- Only unknowns affecting feasibility, authorization scope, or correct execution block progress. For "
                "other implementation choices, select a reasonable option and proceed. If necessary information cannot "
                "be obtained and no feasible plan with explicit scopes exists, report the specific missing "
                "information; "
                "do not guess parameters or broaden authorization to compensate.\n"
                "\n"
                "## Interactive sessions and Cron\n"
                "- Interactive sessions may use staged authorization: authorize and execute a known stage, then "
                "request "
                "another authorization only if the later stage is not covered by an approved scope. A runtime value "
                "becoming known does not itself require another authorization.\n"
                "- Cron authorization cannot be staged or depend on interactive approval during execution. Runtime "
                "values may vary, but their construction and allowed scopes must be expressible in advance; otherwise, "
                "do not enable the job.\n"
                "- When the user only requests creating a scheduled job, put task execution and necessary preparation "
                "in the future plan. Execute operations in the current session only if creation or authorization "
                "actually depends on their results, or the user explicitly requests immediate execution. Otherwise, do "
                "not trial-run the task or request additional session-level authorization.\n"
                "- Generate the job `description` and authorization `actions` from the same execution plan. The "
                "description records execution steps, selected call parameters, and dynamic parameter resolution; "
                "actions cover corresponding protected calls. Future execution must follow that plan without selecting "
                "equivalent implementations again.\n"
                "- Use the system-designated default Cron creation tool. Execute in order: create disabled -> "
                "authorize "
                "the complete future scope using the returned `cronJobId` -> enable after `ok=true`. When the "
                "preceding "
                "step succeeds, proceed directly to the next step. If authorization fails, is denied, or is "
                "insufficient, keep the job disabled and handle the actual result; do not repeatedly submit the same "
                "request.\n"
            )
        else:
            content = (
                "# A4P 意图授权\n\n"
                "## 授权边界\n"
                f"- Web：涉及 {actions} 时，在第一个受保护的工具调用前，调用 `request_a4p_intent_authorization` "
                "获取授权。每个受保护调用必须匹配有效的已批准范围，只读操作也不例外。授权只允许执行，不代表操作已经完成。\n"
                "- 可先使用受保护列表外的工具读取必要信息、发现工具能力和确定参数。一次授权可通过多个 `actions` 覆盖当前已确定的范围。使用真实工具名及其安全关键参数；无需提前确定不参与授权匹配的普通参数。\n"
                "- 对 Shell 工具，默认分别授权完整精确命令。只有单个动态参数无法提前确定，且通配不会覆盖空白、Shell 运算符、重定向或额外参数时，才使用窄范围通配。\n"
                "- 对文件工具，使用具体文件路径，或固定目录下的文件名 glob。不得使用裸 * 或以目录路径代替文件范围。使用专用文件工具完成文件写入。\n"
                "\n"
                "## 规划与收敛\n"
                "- 选择满足用户需求、权限范围明确且能够执行的方案。无需寻找最优方案，也无需证明其他等价方案更差。\n"
                "- 完成一次规划：确定必要操作和依赖；确定受保护操作的工具和授权范围；检查需求覆盖、参数依赖和权限范围。三项检查通过后，立即进入下一步工具调用。\n"
                "- 没有新增信息时，不重新比较已经选定的工具、操作拆分、数据来源或执行顺序。不要为了减少 actions 数量重组已经可行的方案。\n"
                "- 只有用户需求变化、新获得的事实、工具返回错误，或发现具体的需求或授权约束不满足时，才修改受影响的步骤及其依赖。假设性问题本身不构成重新规划的理由。\n"
                "\n"
                "## 未知信息与动态参数\n"
                "- 直接使用上下文和工具说明中已明确的事实，不重复验证。缺失信息若影响执行或授权，使用必要的信息获取工具确认，不反复猜测工具行为。\n"
                "- 对运行时产生的信息，在计划中说明获取方式及其如何构成后续参数；无需提前取得实际值。\n"
                "- 计划可以描述变量和替换关系；提交授权时必须使用有效的具体范围或受支持的模式；实际调用时必须使用已解析的真实参数，不得提交未解析的占位符。\n"
                "- 只有影响任务可行性、授权范围或正确执行的未知信息需要阻塞下一步。"
                "其他实现选择选定一种合理方案后继续。"
                "若必要信息无法获取，且不存在权限范围明确、能够执行的方案，说明具体缺失信息；"
                "不得以扩大授权或猜测参数代替解决问题。"
                "\n"
                "\n"
                "## 当前会话与定时任务\n"
                "- 当前交互会话允许分阶段授权：先授权并执行参数已明确的阶段；仅当后续阶段不在已批准范围内时再申请授权。运行时值变为已知，本身不意味着需要再次授权。\n"
                "- Cron 授权不能分阶段，也不能依赖触发时的交互审批。运行时参数值可以变化，但其构成方式及允许范围必须能够预先表达；无法表达时，不启用任务。\n"
                "- 用户只要求创建定时任务时，将任务执行及其必要准备操作纳入未来执行计划。"
                "只有创建或授权本身确实依赖当前操作的结果，或用户明确要求立即执行时，才在当前会话执行相应操作；"
                "否则不试运行任务或申请额外的会话级授权。"
                "\n"
                "- 从同一份执行计划生成任务 description 和授权 actions：description 记录执行步骤、已确定的调用参数及动态参数解析关系；actions "
                "表达对应的受保护调用范围。未来执行应遵循该计划，不重新选择等价实现。\n"
                "- 使用系统指定的默认 Cron 创建工具，顺序执行：create disabled -> 使用返回的 cronJobId 一次性授权完整未来范围 -> ok=true 后 "
                "enable。前一步成功时直接进入下一步。授权失败、被拒绝或范围不足时保持任务禁用，并根据实际结果处理，不反复提交相同请求。\n"
            )
        await self._upsert_prompt_attachment(
            ctx,
            section=section,
            content=content,
            kind=PromptAttachmentKind.RUNTIME,
            priority=94,
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
            from jiuwenswarm.agents.harness.common.a4p_runtime import (
                get_a4p_runtime,
                is_a4p_enabled,
            )
            from jiuwenswarm.common.config import get_config

            summary = (
                get_a4p_runtime().cron_intent_authorization_summary(job_id)
                if is_a4p_enabled(get_config())
                else None
            )
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
