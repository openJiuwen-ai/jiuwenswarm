# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""编码引擎实现：jiuwen-coding / claude-code / codex.

设计要点（"架构合理、不拆太散"）：

- 一个抽象基类 ``CodingEngine`` 定义统一契约；
- 一个 ``CliCodingEngine`` 收敛所有"外部 CLI"引擎的共性（查找可执行文件、
  准备工作区、缺失即安装、``<cli> ... <prompt>`` 运行任务）；
- ``ClaudeCodeEngine`` / ``CodexEngine`` 仅声明各自的命令行与工作区细节；
- ``JiuwenEngine`` 表示原生后端，不外挂 CLI；
- 一个 ``get_coding_engine(kind)`` 注册表，按名称返回单例引擎。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from shutil import which

logger = logging.getLogger(__name__)

_ENTERPRISE_USER_ENV_MAP = {
    "email_address": "EMAIL_ADDRESS",
    "email_token": "EMAIL_TOKEN",
    "jina_api_key": "JINA_API_KEY",
    "bocha_api_key": "BOCHA_API_KEY",
    "serper_api_key": "SERPER_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "github_token": "GITHUB_TOKEN",
    "gitcode_token": "GITCODE_TOKEN",
    "teamskills_user_token": "TEAM_SKILLS_HUB_USER_TOKEN",
    "free_search_ddg_enabled": "FREE_SEARCH_DDG_ENABLED",
    "free_search_bing_enabled": "FREE_SEARCH_BING_ENABLED",
    "evolution_auto_scan": "EVOLUTION_AUTO_SCAN",
    "skill_create": "SKILL_CREATE",
}

# 当前请求绑定的分身（用于按分身隔离 CLI 引擎工作区，避免多分身产物互相覆盖）。
# 用 ContextVar 而非引擎实例属性：引擎是模块级单例，ContextVar 天然按 async 上下文隔离。
_WORKSPACE_AVATAR: ContextVar[str] = ContextVar("coding_workspace_avatar", default="")


def _safe_workspace_segment(name: str) -> str:
    """把 avatar_id 规整为安全的目录名（防路径穿越/异常字符）."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned[:64]


def set_workspace_avatar(avatar_id: str | None) -> None:
    """设置当前请求的分身，使 CLI 引擎工作区按分身隔离."""
    _WORKSPACE_AVATAR.set(_safe_workspace_segment(avatar_id or ""))


def clear_workspace_avatar() -> None:
    """清除当前请求的分身绑定（回退到共享工作区）."""
    _WORKSPACE_AVATAR.set("")


# ---------------------------------------------------------------------------
# 引擎种类常量
# ---------------------------------------------------------------------------

CODING_ENGINE_JIUWEN = "jiuwen-coding"
CODING_ENGINE_CLAUDE_CODE = "claude-code"
CODING_ENGINE_CODEX = "codex"

DEFAULT_CODING_ENGINE = CODING_ENGINE_JIUWEN

_DEFAULT_TIMEOUT_S = float(os.getenv("CODING_CLI_TIMEOUT", "1800"))
_MAX_OUTPUT_CHARS = int(os.getenv("CODING_CLI_MAX_OUTPUT_CHARS", "120000"))


@dataclass(frozen=True)
class EngineStatus:
    """引擎就绪状态（用于日志、前端诊断、提示注入）."""

    kind: str
    display_name: str
    is_cli: bool
    available: bool
    executable: str = ""
    workspace: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class CodingEngine(ABC):
    """编码后端统一契约."""

    kind: str = ""
    display_name: str = ""
    is_cli: bool = False

    # -- 能力查询 ----------------------------------------------------------

    def is_available(self) -> bool:
        """引擎当前是否可用（原生引擎恒为 True；CLI 引擎需可执行文件存在）."""
        return True

    def is_credentials_configured(self) -> bool:
        """凭据是否已配置（原生引擎恒为 True；CLI 引擎需平台内专用 Key）."""
        return True

    def provides_tool(self) -> bool:
        """是否需要为 Leader 注册 ``coding_task`` 工具（仅 CLI 引擎需要）."""
        return self.is_cli

    @abstractmethod
    def ensure_ready(self, skills_root: Path, *, auto_install: bool = True) -> EngineStatus:
        """准备引擎运行所需的一切（工作区、可执行文件等），返回就绪状态."""

    @abstractmethod
    async def run_task(self, message: str, *, cwd: str | None = None) -> str:
        """执行一次编码任务并返回文本结果（原生引擎不应被调用）."""

    @abstractmethod
    def prompt_section(self, *, skills_root: str, language: str) -> str:
        """返回注入给 Leader 的编排提示（指导如何使用本引擎）."""


# ---------------------------------------------------------------------------
# 原生引擎：jiuwen-coding
# ---------------------------------------------------------------------------


class JiuwenEngine(CodingEngine):
    """原生 DeepAgent：Leader 直接用 skills + bash 完成编码任务，无外部 CLI."""

    kind = CODING_ENGINE_JIUWEN
    display_name = "Jiuwen Coding"
    is_cli = False

    def ensure_ready(self, skills_root: Path, *, auto_install: bool = True) -> EngineStatus:
        return EngineStatus(
            kind=self.kind,
            display_name=self.display_name,
            is_cli=False,
            available=True,
            workspace=str(skills_root),
            detail="native DeepAgent backend",
        )

    async def run_task(self, message: str, *, cwd: str | None = None) -> str:
        return (
            "[coding_task] 当前分身使用原生 jiuwen-coding 后端，无需外部 CLI。"
            "请直接使用已加载的 Skill 与 bash 完成本次编码/检视任务。"
        )

    def prompt_section(self, *, skills_root: str, language: str) -> str:
        if language == "cn":
            return (
                "【编码后端 — jiuwen-coding（原生）】\n"
                "- 你就是编码引擎：直接用已加载的 Skill 脚本与 bash 完成检视/开发/测试\n"
                f"- skills 根目录：{skills_root}\n"
                "- 不要调用 coding_task / 外部 CLI；按 AIDLC 流程逐步执行并产出结果"
            )
        return (
            "[Coding backend — jiuwen-coding (native)]\n"
            "- You ARE the coding engine: use the loaded skill scripts and bash directly.\n"
            f"- skills root: {skills_root}\n"
            "- Do NOT call coding_task / external CLI; follow the AIDLC workflow step by step."
        )


# ---------------------------------------------------------------------------
# CLI 引擎共性
# ---------------------------------------------------------------------------


def _truncate_output(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    half = _MAX_OUTPUT_CHARS // 2
    return (
        text[:half]
        + f"\n\n... [truncated, total {len(text)} chars] ...\n\n"
        + text[-half:]
    )


def _non_interactive_task_prompt(
    *,
    engine_name: str,
    message: str,
    workspace: Path,
    skills_root: Path,
) -> str:
    return (
        f"你正在作为 {engine_name} 非交互执行 AIDLC 编码/检视任务。\n"
        "必须自主完成，不要向用户或 Leader 反问，不要要求补充已可推断的信息。\n"
        "\n"
        "【已知运行环境】\n"
        f"- 当前工作目录：{workspace}\n"
        f"- skills 目录：{workspace / 'skills'}（软链到 {skills_root}）\n"
        "- GitCode 凭据：如需访问 GitCode，请直接使用环境变量 GITCODE_TOKEN；不要打印、泄露或要求用户粘贴 token。\n"
        "- 若任务包含 PR/MR URL，请把该 URL 作为权威输入；可用 dev-reviewer 的 collect 流程拉取 diff。\n"
        "- 你收到本任务意味着 Leader 已判定需要外部 CodingAgent 处理；请完整接管检视分析。\n"
        "- 若需要本地仓库且当前目录不是业务仓，请优先使用 GitCode PR diff / 已配置的 gitcode-repo 信息自行获取上下文；仍不足时产出基于可得证据的检视结果并注明限制，不要反问。\n"
        "- 如果某个工具命令失败，必须尝试替代路径：GitCode API、git clone/fetch、dev-reviewer 脚本、直接读取 PR diff，或基于已获取片段完成保守检视。\n"
        "\n"
        "【执行要求】\n"
        "1. 先阅读 ./skills/dev-reviewer/SKILL.md；需要 GitCode 仓库信息时可阅读 ./skills/gitcode-repo/SKILL.md 和 gitcode-repo.json。\n"
        "2. 对代码检视任务，按 dev-reviewer 流程收集 diff、分析风险、输出可执行发现；collect 后若 context.json 的 \
            project_context.repo_review_standards 非空，必须先读取列出的仓内检视标准再检视（被本 PR 修改的按 PR base 版本读取）。\n"
        "3. 你可以运行允许的 bash/python/git 命令；所有失败都要自行降级重试或记录限制，不要停下来问问题。\n"
        "4. 最终必须给出实质结果：至少包含执行摘要、已检查的文件/证据、Must Fix/Should Fix/可不评论项；没有发现问题也要说明已检查依据。\n"
        "5. 不要输出请提供仓库路径/Token/PR信息等请求，除非原始任务完全没有目标。\n"
        "\n"
        "【⚠️ 行评行号硬性规则（必须遵守）⚠️】\n"
        "代码检视意见 **必须** 附带精确行号，这是强制规则，不可豁免：\n"
        "- 每条 finding 的 location 字段 **必填**，格式为：文件相对路径:行号（如 src/foo.py:42 或 pkg/bar.ts:156-160）\n"
        "- **禁止** 产出 location 为空、unknown、N/A、多处、见下文等模糊值的 finding\n"
        "- 如果问题跨多行，使用范围格式：path:start-end（如 src/utils.py:10-25）\n"
        "- 只有架构/流程/文档类问题（无法对应到任何具体代码行）才允许在 location 中写 (architecture) 或 (documentation)，并在 issue 字段说明原因\n"
        "- 检视结果写入 result.json 时，**必须** 逐条校验 location 合法性，缺失或格式错误的 finding 需立即补全\n"
        "- Leader 用 resolve-positions 从 location 解析 path 和 position；**格式不正确的 finding 将无法提交行评**\n"
        "\n"
        "【原始任务】\n"
        f"{message.strip()}\n"
    )


# Note: The following unused string variable is a remnant from an edit
# operation and can be safely deleted in a future cleanup.
_UNUSED_LEFTOVER = """请提供仓库路径/Token/PR 信息"等请求，除非原始任务完全没有目标。\n"
        "6. 不要输出“请提供仓库路径/Token/PR 信息”等请求，除非原始任务完全没有目标；本次原始任务如下。\n"
        "\n"
        "【原始任务】\n"
        f"{message.strip()}\n"
    )
"""


def _remove_existing_link_entry(link: Path) -> None:
    from jiuwenavatar.agents.harness.team.team_skill_links import _is_windows_reparse_point

    if link.is_symlink():
        link.unlink()
    elif _is_windows_reparse_point(link):
        os.rmdir(link)
    elif link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()


def _link_or_replace_symlink(link: Path, target: Path) -> None:
    from jiuwenavatar.agents.harness.team.team_skill_links import (
        _create_directory_link,
        _is_windows_reparse_point,
    )

    target = target.resolve()
    if link.is_symlink() or _is_windows_reparse_point(link) or link.exists():
        try:
            if link.resolve() == target:
                return
        except OSError:
            pass
        _remove_existing_link_entry(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    # Windows 无开发者模式/管理员时 symlink 会报 WinError 1314；回退到 junction。
    _create_directory_link(target, link)


def _copy_tree_if_changed(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            _copy_tree_if_changed(item, target)
        else:
            if target.is_file() and target.read_bytes() == item.read_bytes():
                continue
            shutil.copy2(item, target)


def _builtin_assets_dir() -> Path:
    """内置 avatar-skills 根目录（含 claude-agents/ 与 claude-settings.json）."""
    from jiuwenavatar.common.utils import get_builtin_skills_dir

    return get_builtin_skills_dir()


class CliCodingEngine(CodingEngine):
    """外部 CLI 编码引擎的共享实现.

    子类只需声明：``kind`` / ``display_name`` / ``executable_name`` /
    ``workspace_dirname`` / ``passthrough_env``，并实现 ``_build_command`` 与
    （可选）``_prepare_workspace_extra``。
    """

    is_cli = True

    # 子类覆盖的声明
    kind: str = ""
    display_name: str = ""
    executable_name: str = ""
    workspace_dirname: str = ""
    passthrough_env: tuple[str, ...] = ()

    # 子类覆盖：Claude Code >=2.1 在 -p 模式下从 stdin 读取 prompt，
    # 命令行 positional 参数会被忽略。
    prompt_via_stdin: bool = False

    # ---- 可执行文件 ----

    def resolve_executable(self) -> str | None:
        home = Path.home()
        extra = [str(home / ".local" / "bin"), str(home / ".claude" / "bin")]
        try:
            import subprocess

            npm_prefix = subprocess.run(
                ["npm", "config", "get", "prefix"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if npm_prefix.returncode == 0 and npm_prefix.stdout.strip():
                prefix = Path(npm_prefix.stdout.strip())
                extra.append(str(prefix / "bin"))
                if os.name == "nt":
                    extra.append(str(prefix))
        except Exception:
            pass

        if os.name == "nt":
            for key in ("APPDATA", "LOCALAPPDATA"):
                root = os.environ.get(key)
                if root:
                    extra.append(str(Path(root) / "npm"))
            program_files = os.environ.get("ProgramFiles")
            if program_files:
                extra.append(str(Path(program_files) / "nodejs"))

        path_val = os.environ.get("PATH", "")
        for segment in extra:
            if segment and segment not in path_val.split(os.pathsep):
                path_val = f"{segment}{os.pathsep}{path_val}"
        return which(self.executable_name, path=path_val)

    def is_available(self) -> bool:
        return self.resolve_executable() is not None

    # ---- 工作区 ----

    def workspace(self) -> Path:
        from jiuwenavatar.common.utils import get_agent_workspace_dir

        base = get_agent_workspace_dir() / self.workspace_dirname
        # 按分身隔离产物目录：不同分身（如开发 / committer）各用独立工作区，
        # 互不覆盖；未绑定分身时回退到共享工作区，保持向后兼容。
        avatar = _WORKSPACE_AVATAR.get()
        if avatar:
            return base / avatar
        return base

    def _prepare_workspace(self, skills_root: Path) -> Path:
        ws = self.workspace()
        ws.mkdir(parents=True, exist_ok=True)
        _link_or_replace_symlink(ws / "skills", skills_root)
        self._prepare_workspace_extra(ws)
        return ws

    def _prepare_workspace_extra(self, workspace: Path) -> None:  # noqa: B027 - optional hook
        """子类可覆盖：放置 agent 定义 / settings / AGENTS.md 等."""

    # ---- 就绪 ----

    def ensure_ready(self, skills_root: Path, *, auto_install: bool = True) -> EngineStatus:
        ws = self._prepare_workspace(skills_root)
        exe = self.resolve_executable()
        detail = ""
        if exe is None and auto_install:
            from jiuwenavatar.server.runtime.coding.bootstrap import ensure_cli_installed

            detail = ensure_cli_installed(self.kind)
            exe = self.resolve_executable()
        if exe is None and not detail:
            detail = f"{self.executable_name} CLI not found"
        return EngineStatus(
            kind=self.kind,
            display_name=self.display_name,
            is_cli=True,
            available=exe is not None,
            executable=exe or "",
            workspace=str(ws),
            detail=detail,
        )

    # ---- 子进程环境 ----

    def _subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        try:
            from jiuwenavatar.common.enterprise import get_tenant_context, is_enterprise_mode

            ctx = get_tenant_context()
            group_id = str(ctx.group_id if ctx else "").strip()
            user_id = str(ctx.user_id if ctx else "").strip()
            if is_enterprise_mode() and group_id:
                from jiuwenavatar.gateway.model_catalog import get_model_catalog_service

                for key, value in get_model_catalog_service().resolve_coding_env(group_id, self.kind).items():
                    if value:
                        env[key] = value
                if user_id:
                    from jiuwenavatar.common.enterprise_user_config import get_enterprise_user_config_store

                    user_config = get_enterprise_user_config_store().load(group_id, user_id)
                    for config_key, env_key in _ENTERPRISE_USER_ENV_MAP.items():
                        value = user_config.get(config_key, "")
                        if value:
                            env[env_key] = value
        except Exception:
            logger.debug("Failed to resolve tenant coding env for %s", self.kind, exc_info=True)
        for key in self.passthrough_env:
            val = os.environ.get(key, "").strip()
            if val and key not in env:
                env[key] = val
        return env

    @abstractmethod
    def _build_command(self, executable: str, message: str, *, skip_permissions: bool) -> list[str]:
        """构造 CLI 命令行参数."""

    # ---- 运行 ----

    async def run_task(self, message: str, *, cwd: str | None = None) -> str:
        prompt = (message or "").strip()
        if not prompt:
            return "[ERROR] coding_task message is empty."

        exe = self.resolve_executable()
        if not exe:
            setup_script = (
                "scripts/setup_coding_cli.ps1"
                if os.name == "nt"
                else "scripts/setup_coding_cli.sh"
            )
            return (
                f"[ERROR] {self.display_name} CLI ({self.executable_name}) 未找到。"
                f"可运行 {setup_script} {self.kind} 安装，或检查 PATH。"
            )

        from jiuwenavatar.common.utils import get_agent_skills_dir

        ws = self._prepare_workspace(get_agent_skills_dir())
        work_dir = Path(cwd).expanduser().resolve() if cwd and str(cwd).strip() else ws
        if not work_dir.is_dir():
            work_dir = ws

        task_prompt = _non_interactive_task_prompt(
            engine_name=self.display_name,
            message=prompt,
            workspace=ws,
            skills_root=get_agent_skills_dir(),
        )
        args = self._build_command(exe, task_prompt, skip_permissions=True)
        env = self._subprocess_env()
        stdin_input = task_prompt.encode("utf-8") if self.prompt_via_stdin else None
        logger.info(
            "[coding_task] engine=%s cwd=%s prompt_len=%d wrapped_len=%d bin=%s stdin=%s",
            self.kind,
            work_dir,
            len(prompt),
            len(task_prompt),
            exe,
            self.prompt_via_stdin,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(work_dir),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin_input is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_input), timeout=_DEFAULT_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return f"[ERROR] {self.kind} timed out after {_DEFAULT_TIMEOUT_S:.0f}s"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[coding_task] subprocess failed: engine=%s", self.kind)
            return f"[ERROR] {self.kind} failed: {exc}"

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        code = proc.returncode if proc.returncode is not None else -1

        parts: list[str] = []
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        if not parts:
            parts.append(f"(empty response from {self.display_name})")
        if code != 0:
            parts.append(f"[exit_code={code}]")
        return _truncate_output("\n\n".join(parts))

    # ---- 提示注入 ----

    def prompt_section(self, *, skills_root: str, language: str) -> str:
        ws = str(self.workspace())
        if language == "cn":
            return (
                f"【编码后端 — {self.display_name}（外部 CLI）— 必须遵守】\n"
                f"- 编码/检视等 AIDLC 重活必须路由到 {self.display_name}：只要用户请求涉及代码检视/分析/实现，且当前 CodingAgent 可用，必须调用 coding_task 委派，不要自行分析代码或 diff\n"
                "- 禁止 Leader 预读 diff 内容：不要用 gitcode-repo/dev-reviewer 或任何其他方式先读取 diff 来“统计行数”或“了解变更范围”——这些工作全部交给外部 CLI 完成\n"
                "- 调用 coding_task 时必须一次性给足任务卡：用户原始目标、PR/Issue URL、期望输出、是否需要提交行评；不要只问“还需要什么信息”\n"
                "- 调用示例：coding_task(message=\"@dev-reviewer 独立检视此 PR: <URL>。请读取 ./skills/dev-reviewer/SKILL.md，使用 GITCODE_TOKEN 自主拉取 diff，生成 Must Fix/Should Fix 行评建议；不要反问。\")\n"
                f"- skills 根目录：{skills_root}\n"
                f"- {self.display_name} 工作目录：{ws}（已软链 skills）\n"
                "- GITCODE_TOKEN 会注入 coding_task 子进程；如果 CC 返回缺少仓库路径/Token/上下文，先用同一个 PR URL 与明确的自主执行要求重试一次，不要让用户补充\n"
                "- 你是 Leader（编排层）：理解意图 → 立即调用 coding_task（不要预读 diff）→ 基于 CC 返回结果用 gitcode-repo 提交检视意见 / 建 PR 等后续操作\n"
                "- 除非 coding_task 明确不可用或连续返回系统级错误，否则禁止绕过外部 CLI 自行检视；不要自己 bash 改业务代码"
            )
        return (
            f"[Coding backend — {self.display_name} (external CLI) — mandatory]\n"
            f"- Route all AIDLC coding/review work to {self.display_name}: whenever the user requests code review/analysis/implementation and the CodingAgent is available, you MUST call coding_task to delegate; do NOT analyze code or diff yourself.\n"
            "- Leader MUST NOT pre-read the diff: do NOT use gitcode-repo/dev-reviewer or any other means to read the diff first for 'counting lines' or 'understanding scope' — leave all that to the external CLI.\n"
            "- Pass a complete task card in one call: original user goal, PR/Issue URL, expected output, and whether inline comments are needed. Do not ask what extra information is needed.\n"
            "- Example: coding_task(message=\"@dev-reviewer independently review this PR: <URL>. Read ./skills/dev-reviewer/SKILL.md, use GITCODE_TOKEN to fetch the diff, produce Must Fix/Should Fix inline-comment suggestions, and do not ask follow-up questions.\")\n"
            f"- skills root: {skills_root}\n"
            f"- {self.display_name} workspace: {ws} (skills symlinked)\n"
            "- GITCODE_TOKEN is passed to the coding_task subprocess. If the engine asks for repo path/token/context, retry once with the same PR URL and explicit autonomous instructions instead of asking the user.\n"
            "- You are the Leader orchestrator: intent -> immediately call coding_task (do NOT pre-read the diff) -> then use gitcode-repo to submit review comments / open PRs based on the result.\n"
            "- Unless coding_task is unavailable or returns repeated system-level failures, do not bypass the external CLI to review; do not edit business code via bash yourself."
        )


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class ClaudeCodeEngine(CliCodingEngine):
    kind = CODING_ENGINE_CLAUDE_CODE
    display_name = "Claude Code"
    executable_name = "claude"
    workspace_dirname = "aidlc-cc"
    prompt_via_stdin = True
    passthrough_env = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "GITCODE_TOKEN",
        "AIDLC_PYTHON",
    )

    def is_credentials_configured(self) -> bool:
        try:
            from jiuwenavatar.common.enterprise import get_tenant_context, is_enterprise_mode

            ctx = get_tenant_context()
            group_id = str(ctx.group_id if ctx else "").strip()
            if is_enterprise_mode() and group_id:
                from jiuwenavatar.gateway.model_catalog import get_model_catalog_service

                env = get_model_catalog_service().resolve_coding_env(group_id, self.kind)
                if env.get("ANTHROPIC_API_KEY"):
                    return True
        except Exception:
            logger.debug("Failed to check tenant Claude Code credentials", exc_info=True)
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def resolve_executable(self) -> str | None:
        """优先使用 npm 包内的 claude.exe，避免 Windows .CMD/.PS1 包装器丢参."""
        found = super().resolve_executable()
        if os.name != "nt":
            return found
        candidates: list[Path] = []
        if found:
            wrapper = Path(found)
            npm_root = wrapper.parent
            candidates.append(
                npm_root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            )
        try:
            import subprocess

            npm_prefix = subprocess.run(
                ["npm", "config", "get", "prefix"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if npm_prefix.returncode == 0 and npm_prefix.stdout.strip():
                prefix = Path(npm_prefix.stdout.strip())
                candidates.append(
                    prefix / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
                )
        except Exception:
            pass
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return found

    def _prepare_workspace_extra(self, workspace: Path) -> None:
        assets = _builtin_assets_dir()
        agents_src = assets / "claude-agents"
        agents_dst = workspace / ".claude" / "agents"
        _copy_tree_if_changed(agents_src, agents_dst)

        settings_src = assets / "claude-settings.json"
        settings_dst = workspace / ".claude" / "settings.json"
        if settings_src.is_file() and (
            not settings_dst.is_file()
            or settings_dst.read_bytes() != settings_src.read_bytes()
        ):
            settings_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(settings_src, settings_dst)

        # Copy CLAUDE.md so CC starts executing immediately instead of
        # describing the workspace and asking for instructions.
        claude_md_src = assets / "CLAUDE.md"
        claude_md_dst = workspace / "CLAUDE.md"
        if claude_md_src.is_file() and (
            not claude_md_dst.is_file()
            or claude_md_dst.read_bytes() != claude_md_src.read_bytes()
        ):
            shutil.copy2(claude_md_src, claude_md_dst)

    def _build_command(self, executable: str, message: str, *, skip_permissions: bool) -> list[str]:
        # Claude Code >=2.1: -p 模式下 prompt 经 stdin 传入（见 prompt_via_stdin）。
        args = [executable, "-p"]
        if skip_permissions:
            args.append("--dangerously-skip-permissions")
        extra = os.getenv("CODING_CLI_CLAUDE_ARGS", "").strip()
        if extra:
            args.extend(extra.split())
        return args


# ---------------------------------------------------------------------------
# OpenAI Codex
# ---------------------------------------------------------------------------


class CodexEngine(CliCodingEngine):
    kind = CODING_ENGINE_CODEX
    display_name = "OpenAI Codex"
    executable_name = "codex"
    workspace_dirname = "aidlc-codex"
    passthrough_env = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_MODEL",
        "GITCODE_TOKEN",
        "AIDLC_PYTHON",
    )

    def is_credentials_configured(self) -> bool:
        try:
            from jiuwenavatar.common.enterprise import get_tenant_context, is_enterprise_mode

            ctx = get_tenant_context()
            group_id = str(ctx.group_id if ctx else "").strip()
            if is_enterprise_mode() and group_id:
                from jiuwenavatar.gateway.model_catalog import get_model_catalog_service

                env = get_model_catalog_service().resolve_coding_env(group_id, self.kind)
                if env.get("OPENAI_API_KEY"):
                    return True
        except Exception:
            logger.debug("Failed to check tenant Codex credentials", exc_info=True)
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())

    def _prepare_workspace_extra(self, workspace: Path) -> None:
        # Codex 读取工作目录下的 AGENTS.md 作为项目级指令。
        agents_md = workspace / "AGENTS.md"
        content = (
            "# AIDLC Coding Agent\n\n"
            "已加载的 AIDLC Skill 位于 `./skills/`（软链至分身技能目录）。\n"
            "执行代码检视/开发任务时，请遵循对应 Skill（如 dev-reviewer、dev-leader）的流程。\n"
        )
        if not agents_md.is_file() or agents_md.read_text(encoding="utf-8") != content:
            agents_md.write_text(content, encoding="utf-8")

    def _build_command(self, executable: str, message: str, *, skip_permissions: bool) -> list[str]:
        args = [executable, "exec"]
        if skip_permissions:
            # Codex CLI 非交互执行 + 跳过审批（旧/新版本标志名不同，允许 env 覆盖）
            flag = os.getenv("CODING_CLI_CODEX_APPROVAL_FLAG", "--dangerously-bypass-approvals-and-sandbox")
            if flag:
                args.append(flag)
        extra = os.getenv("CODING_CLI_CODEX_ARGS", "").strip()
        if extra:
            args.extend(extra.split())
        args.append(message)
        return args


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, CodingEngine] = {
    CODING_ENGINE_JIUWEN: JiuwenEngine(),
    CODING_ENGINE_CLAUDE_CODE: ClaudeCodeEngine(),
    CODING_ENGINE_CODEX: CodexEngine(),
}


def list_engine_kinds() -> list[str]:
    """返回所有已注册引擎种类."""
    return list(_REGISTRY.keys())


def get_coding_engine(kind: str | None) -> CodingEngine:
    """按名称返回引擎；未知/空 → 原生 jiuwen-coding."""
    key = (kind or "").strip() or DEFAULT_CODING_ENGINE
    return _REGISTRY.get(key, _REGISTRY[DEFAULT_CODING_ENGINE])


def coding_engine_selectability(kind: str) -> dict[str, str | bool]:
    """返回单个编码引擎是否可在 Web 上被分身选用."""
    from jiuwenavatar.server.runtime.coding.bootstrap import get_cli_install_status
    
    engine = get_coding_engine(kind)
    configured = engine.is_credentials_configured()
    selectable = configured if engine.is_cli else True
    reason = ""
    if engine.is_cli and not configured:
        if kind == CODING_ENGINE_CLAUDE_CODE:
            reason = "anthropic_not_configured"
        elif kind == CODING_ENGINE_CODEX:
            reason = "openai_not_configured"
        else:
            reason = "credentials_not_configured"
    
    # 获取 CLI 安装状态（仅对 CLI 引擎）
    cli_status = None
    if engine.is_cli:
        status = get_cli_install_status(kind)
        cli_status = {
            "running": status.running,
            "last_detail": status.last_detail,
            "success": not status.running and status.last_detail.startswith("installed"),
            "failed": not status.running and status.last_detail and not status.last_detail.startswith("installed"),
        }
    
    return {
        "kind": engine.kind,
        "display_name": engine.display_name,
        "configured": configured,
        "selectable": selectable,
        "reason": reason,
        "cli_install_status": cli_status,
    }


def list_coding_engine_selectability() -> dict[str, dict[str, str | bool]]:
    """返回所有已注册引擎的可选状态（供 Web 分身配置页使用）."""
    return {kind: coding_engine_selectability(kind) for kind in _REGISTRY}


def assert_coding_engine_selectable(kind: str | None) -> None:
    """分身保存编码引擎前校验；未配置凭据的 CLI 引擎不可选用."""
    key = (kind or "").strip()
    if not key:
        return
    engine = get_coding_engine(key)
    if engine.is_cli and not engine.is_credentials_configured():
        if key == CODING_ENGINE_CLAUDE_CODE:
            raise ValueError(
                "Claude Code 需要先在平台配置页填写专门给 CC 使用的 ANTHROPIC_API_KEY。"
            )
        if key == CODING_ENGINE_CODEX:
            raise ValueError(
                "Codex 需要先在配置页填写 OPENAI_API_KEY。"
            )
        raise ValueError(f"编码引擎 {key} 需要先完成凭据配置。")
