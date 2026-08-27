# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuWenSwarm Code Adapter — code 模式配置驱动适配器.

继承 JiuWenSwarmDeepAdapter，重写 create_instance() 和 rails/tools 注册方法。
从 config.yaml::modes.code.rails/tools 读取配置列表，
通过名字映射查找构建方法来注册。
统一使用 create_deep_agent()，不再使用 create_code_agent()。

Code 模式独占逻辑全部收敛于此：
- LspRail、ProjectMemoryRail、CodingMemoryRail 等 code 专属 rail
- code_agent / explore_agent / plan_agent subagent 配置
- code 模式下 rail 生命周期（保留 SubagentRail、补充 ProjectMemoryRail 等）
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.retrieval.code_graph.models import CodeGraphConfig
from openjiuwen.core.runner import Runner as Runner
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import (
    AgentModeRail,
    CodingMemoryRail as _BaseCodingMemoryRail,
    SysOperationRail,
    LspRail,
)
from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

from openjiuwen.harness.rails.context_engineer.context_assemble_rail import ContextAssembleRail
from openjiuwen.harness.lsp import InitializeOptions
from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (
    CodeGraphFlags,
    PROFILE_OFF,
    resolve_code_graph_flags,
)
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.code_agent import build_code_agent_config
from openjiuwen.harness.subagents.explore_agent import build_explore_agent_config
from openjiuwen.harness.subagents.plan_agent import build_plan_agent_config
from openjiuwen.harness.tools import WebFetchWebpageTool, WebPaidSearchTool
from openjiuwen.harness.tools.worktree import WorktreeConfig, WorktreeRail
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _AGENT_CARD_ID,
    _AgentProfileRailSpecs,
    _AgentRailBuildSpec,
    _CRON_TOOL_CHANNEL_ID,
    _deep_agent_context_engine_config,
    _deep_agent_kv_cache_affinity_config,
    parse_int,
)
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    STATUSLINE_SETUP_AGENT_TYPE,
    build_statusline_setup_agent_config,
)
from jiuwenswarm.server.runtime.agent_adapter.trusted_web_search import (
    TrustedWebFreeSearchTool,
)
from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
    build_code_system_prompt,
)
from jiuwenswarm.agents.harness.code.rails import (
    CodePlanPrePermissionGuardRail,
    CodeTaskPlanningRail,
    PlanApprovalInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails import (
    ProjectMemoryRail,
    StructuredAskUserRail,
)
from jiuwenswarm.agents.harness.common.memory.config import is_memory_enabled
from jiuwenswarm.agents.harness.common.tools import (
    SkillToolkit,
)
from jiuwenswarm.agents.harness.common.tools.acp_chat import acp_chat
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.tool_ownership import mark_stateless, register_tool
from jiuwenswarm.common.coding_memory_paths import (
    resolve_project_coding_memory_dir,
)
from jiuwenswarm.common.mode_matrix import resolve_agent_composition_scope
from jiuwenswarm.server.runtime.agent_adapter.code_agent_rail import CodeAgentRail
from jiuwenswarm.common.task_loop_config import (
    resolve_task_loop_completion_timeout,
)
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_default_project_session_workspace_dir,
)

logger = logging.getLogger(__name__)


class CodingMemoryRail(_BaseCodingMemoryRail):
    """Keep Coding Memory cold-start indexing out of the request path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._manager_init_task: asyncio.Task[None] | None = None

    async def before_invoke(self, ctx: Any) -> None:
        """Start manager initialization without delaying the user request."""
        if not self._manager_initialized and self._manager_init_task is None:
            self._manager_init_task = asyncio.create_task(
                self._initialize_manager_in_background(ctx),
                name="coding-memory-init",
            )

        self._recalled_content = None
        self._prefetch_task = None

        is_read_only = getattr(self, "_read_only_tools", False) or self._is_read_only(
            ctx.inputs
        )
        if not is_read_only and self._manager:
            query = self._extract_last_user_query(ctx)
            if query:
                self._prefetch_task = asyncio.create_task(self._auto_recall(query))

    async def _initialize_manager_in_background(self, ctx: Any) -> None:
        """Run the base initializer and record success or degraded state."""
        cancelled = False
        try:
            await self._init_coding_memory_manager(ctx)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[CodingMemoryRail] background initialization failed: %s",
                exc,
            )
        finally:
            # A cancelled task may finish after uninit() and a new lifecycle
            # has started. Only the current task may update this rail state.
            if not cancelled and self._manager_init_task is asyncio.current_task():
                self._manager_initialized = True

    @staticmethod
    def _is_read_only(inputs: Any) -> bool:
        """Support callback inputs and lightweight test doubles."""
        values = []
        for name in ("is_cron", "is_heartbeat"):
            value = getattr(inputs, name, False)
            values.append(value() if callable(value) else value)
        return any(values)

    def uninit(self, agent: Any) -> None:
        """Cancel pending initialization before the rail is torn down."""
        task = self._manager_init_task
        self._manager_init_task = None
        if task is not None and not task.done():
            task.cancel()
        super().uninit(agent)


# ---------------------------------------------------------------------------
# Static plan mode system prompt note (KV-cache-friendly: same content every turn)
# Injected into system prompt so the model knows it's in plan mode BEFORE its
# first tool call. Without this, the model only sees a weak <system-reminder>
# in the user message and may execute code before calling enter_plan_mode.
# ---------------------------------------------------------------------------

_PLAN_MODE_SYSTEM_NOTE = """\
Plan mode is active. You must only plan — you must NOT make any modifications
(except to the plan file, after calling `enter_plan_mode`), must NOT run any
write operations (including mkdir, touch, rm, mv, cp, chmod, chown, dd, tee,
output redirection, file edits outside the plan file, or git commit/push/add),
and must NOT make any changes to the system. This constraint takes priority
over any other instructions you receive.

Read-only actions are allowed directly: you may read files and explore the
codebase, and run read-only commands (read_file, grep, list_files, glob, bash
for read-only operations such as gh pr list/view/diff or git status/diff/log).
Write operations and non-read-only tools are blocked by the runtime.

If you need to design an implementation approach and produce a plan, call
`enter_plan_mode` — it creates the plan file and gives you full plan mode
instructions. This is not required as your first action; you may gather
context with read-only tools first. Do NOT proceed to implement anything
until the user approves your plan via `exit_plan_mode`.
"""

# ---------------------------------------------------------------------------
# Plan mode instructions for enter_plan_mode tool_result
# instructions live in conversation, not system prompt
# ---------------------------------------------------------------------------

_ENTER_PLAN_MODE_INSTRUCTIONS_EN = """
## Entering Plan Mode

You are now in **plan mode**. You must only plan — you must not make any modifications (except to the plan file), must not run any non-read-only tools, and must not make any changes to the system. This constraint takes priority over any other instructions you receive.

### Available Tools (only)
- Read-only tools: read_file, grep, list_files, glob
- Plan file tools: write_file, edit_file (only .plans/<slug>.md)
- Interactive tools: ask_user
- Sub-agent tool: task_tool (dispatch explore_agent / plan_agent)
- Control tools: exit_plan_mode
- bash (read-only operations only; git write / mkdir / touch / rm are blocked)

### Prohibited Actions
- Do NOT use switch_mode to exit plan mode
- Do NOT edit any file except the plan file
- Do NOT execute git write operations (commit, push, add, merge, rebase, etc.)
- Do NOT use bash for write operations (mkdir, touch, rm, mv, etc.)
- Do NOT use todo_create, sessions_list, or other session management tools

### Plan Workflow

#### Phase 1: Initial Understanding
Goal: Gain a comprehensive understanding of the user's request by reading code and asking questions.
1. Focus on understanding existing architecture and patterns; identify relevant files and dependencies
2. Launch explore sub-agents via task_tool to efficiently explore the codebase
3. Quality over quantity — use the fewest agents possible

#### Phase 2: Design
Goal: Design the implementation approach.
1. Launch a plan sub-agent via task_tool, based on Phase 1 exploration results
2. Provide full background context in the agent prompt

#### Phase 3: Review
Goal: Review the Phase 2 plan to ensure alignment with user intent.
1. Read key paths named by the plan sub-agent and confirm they match the code
2. Use ask_user to clarify any unresolved questions with the user

#### Phase 4: Write Final Plan
Goal: Write the final plan to the plan file.
- Start with a Context section
- Include key file paths that need modification
- Reference reusable existing functions and tools
- Include a Verification section

#### Phase 5: End Planning Phase
Call exit_plan_mode to end the planning phase.

### Turn Ending Rules
Your turn can only end in one of these two ways:
1. Call ask_user to clarify requirements or ask the user to choose between options
2. Call exit_plan_mode to end the planning phase and request user approval

Do not end your turn without calling exit_plan_mode when planning is complete.
ask_user is only for clarifying requirements — do not use it for approval questions.
"""

# ---------------------------------------------------------------------------
# Plan mode exit notification appended to exit_plan_mode tool_result.
# Explicitly tells the model it can now edit files. Without this, the model only sees
# MODE_INSTRUCTIONS removed from system prompt but receives no explicit signal.
# The tool_result this is appended to carries the whole plan text, so a mild
# "proceed with the plan" wording loses to that blob and the model just echoes the
# plan back and asks whether to start. Hence the explicit do-NOT-restate wording.
# ---------------------------------------------------------------------------

_EXIT_PLAN_MODE_NOTIFICATION = """\
<system-reminder>
The user approved this plan. Plan mode has ended and you are now in normal mode:
you can edit files, run write operations, and make changes to the system.

Start executing the first step now. Do NOT restate the plan and do NOT ask again
whether to begin — the user has already read and approved it. This turn's output
should be the work itself and its results. Use ask_user only if execution is
genuinely blocked.
</system-reminder>"""


# 名字 → 构建方法映射（rail/tool 名字与类方法名对照）
_RAIL_BUILD_NAMES: dict[str, str] = {
    "SysOperationRail": "_build_filesystem_rail",
    "FileSystemRail": "_build_filesystem_rail",     # 别名映射
    "SkillUseRail": "_build_skill_rail_via_config",
    "HeartbeatRail": "_build_heartbeat_rail",
    "AvatarPromptRail": "_build_avatar_rail",
    "TaskPlanningRail": "_build_task_planning_rail",
    "SubagentRail": "_build_subagent_rail",
    "ContextAssembleRail": "_build_context_assemble_rail",
    "ContextProcessorRail": "_build_context_processor_rail",
    "SkillEvolutionRail": "_build_skill_evolution_rail_via_config",
    "ProjectMemoryRail": "_build_project_memory_rail",
    "CodingMemoryRail": "_build_coding_memory_rail",
    "WorktreeRail": "_build_worktree_rail_via_config",
    "CodeAgentRail": "_build_code_agent_rail",
    "PlanApprovalInterruptRail": "_build_plan_approval_rail",
}

_TOOL_BUILD_NAMES: dict[str, str] = {
    "web_free_search": "_build_web_free_search_tool",
    "web_fetch_webpage": "_build_web_fetch_webpage_tool",
    "web_paid_search": "_build_paid_search_tool",
    "user_todos": "_build_user_todos_tool",
    "skill_toolkit": "_build_skill_toolkit",
    "skill_retrieval": "_build_skill_retrieval_toolkit",
    "acp_chat": "_build_acp_chat_tool",
}


def _resolve_coding_memory_dir(
    *,
    project_dir: str | None,
    agent_workspace_dir: str,
) -> str:
    """Resolve the app-owned CodingMemory directory scoped by project."""
    return resolve_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace_dir,
        project_dir=project_dir,
    )


def _build_coding_memory_directory_node(
    coding_memory_path: str,
    *,
    description: str,
) -> dict[str, Any]:
    return {
        "name": "coding_memory",
        "description": description,
        "path": coding_memory_path,
        "children": [
            {
                "name": "MEMORY.md",
                "description": "Coding 记忆索引",
                "path": "MEMORY.md",
                "children": [],
                "is_file": True,
                "default_content": "",
            },
        ],
    }


def _set_workspace_coding_memory_directory(
    workspace: Any,
    *,
    project_dir: str | None,
    agent_workspace_dir: str,
    description: str = "Coding Agent memory",
) -> None:
    set_directory = getattr(workspace, "set_directory", None)
    if not callable(set_directory):
        return

    # CodingMemoryRail uses this same app-owned directory for MEMORY.md.  The
    # coding-memory tools resolve individual memory files through the workspace
    # node, so the node must point at the absolute storage directory as well;
    # a project-relative node would split memory files and their index across
    # two locations.
    coding_memory_path = resolve_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace_dir,
        project_dir=project_dir,
    )
    set_directory(
        _build_coding_memory_directory_node(
            coding_memory_path,
            description=description,
        )
    )


def create_coding_memory_rail(
    *,
    project_dir: str | None,
    agent_workspace_dir: str,
    config: dict[str, Any] | None,
) -> CodingMemoryRail:
    """Create CodingMemoryRail, falling back when embedding config is incomplete."""
    embed_config = config.get("embed") if isinstance(config, dict) else None
    embed_config = embed_config if isinstance(embed_config, dict) else {}

    embed_api_key = embed_config.get("embed_api_key") or None
    embed_base_url = (
        embed_config.get("embed_base_url")
        or embed_config.get("embed_api_base")
        or ""
    )
    embed_model = embed_config.get("embed_model") or "text-embedding-v3"
    embedding_config_complete = bool(embed_api_key and embed_base_url and embed_model)
    if not embedding_config_complete:
        embed_api_key = None
        logger.warning(
            "[JiuwenSwarmCodeAdapter] CodingMemoryRail: incomplete embedding config; "
            "registering tools with memory fallback provider"
        )

    coding_memory_dir = _resolve_coding_memory_dir(
        project_dir=project_dir,
        agent_workspace_dir=agent_workspace_dir,
    )
    os.makedirs(coding_memory_dir, exist_ok=True)

    return CodingMemoryRail(
        coding_memory_dir=coding_memory_dir,
        embedding_config=EmbeddingConfig(
            model_name=embed_model,
            base_url=embed_base_url,
            api_key=embed_api_key,
        ),
        language="en",
    )


# ─── Plan mode allowed tools for code mode ──────────────────────────────
# Excludes switch_mode so the LLM cannot unilaterally exit plan mode.

_CODE_PLAN_ALLOWED_TOOLS: list[str] = [
    "enter_plan_mode",
    "exit_plan_mode",
    "ask_user",
    "task_tool",
    "read_file",
    "grep",
    "list_files",
    "glob",
    "bash",
    "write_file",
    "edit_file",
]


class JiuwenSwarmCodeAdapter(JiuWenSwarmDeepAdapter):
    """Code 模式适配器 — 配置驱动注册 rails/tools.

    继承 JiuWenSwarmDeepAdapter，只重写：
    - create_instance(): 统一使用 create_deep_agent()（completion_timeout 从配置读取）
    - _build_profile_rail_specs(): 固定 Rails (含 LspRail/ProjectMemoryRail/CodingMemoryRail) + 从 config.yaml 读取动态 Rails
    - _get_tool_cards(): 从 config.yaml 读取动态 Tools
    - _build_configured_subagents(): 固定 explore_agent/plan_agent + 按配置启用 code_agent/browser_agent
    - _update_rails_for_mode(): code 模式 rail 生命周期
    - _update_runtime_config(): 保留 ProjectMemoryRail 语言同步
    """

    # 固定 Rails 名字集合 — 用于动态 Rails 去重
    _FIXED_RAIL_NAMES = frozenset({
        "RuntimePromptRail", "ResponsePromptRail",
        "JiuSwarmStreamEventRail", "SecurityRail",
        "LspRail", "ProjectMemoryRail", "PermissionInterruptRail",
        "ContextProcessorRail",
        "SysOperationRail", "CodingMemoryRail",
        "MemoryForbiddenRail",
        "AgentModeRail", "StructuredAskUserRail", "ConfirmInterruptRail",
        "FileSystemRail",  # 别名
        "CodeGraphProfileRail",
    })

    def __init__(self) -> None:
        super().__init__()
        # Code 模式专属 rails — 父类不定义这些属性
        self._lsp_rail: LspRail | None = None
        self._project_memory_rail: ProjectMemoryRail | None = None
        self._coding_memory_rail: CodingMemoryRail | None = None
        self._code_plan_pre_permission_guard_rail: (
            CodePlanPrePermissionGuardRail | None
        ) = None
        self._worktree_rail: WorktreeRail | None = None
        # 单点 source-of-truth, 让 sysop_builder 的"主写入根"分支
        # (project_dir vs get_agent_workspace_dir) 落到 code-agent 这一支。
        # 父类默认 False (deep agent → workspace), Code adapter override 成
        # True (code-agent → project_dir). 见
        # ``sysop_builder.build_filesystem_policy`` 中 line 545 附近的分支。
        self._is_code_agent: bool = True
        self._runtime_language_override: str | None = None
        self._force_english_runtime_prompt: bool = True
        self._code_graph_profile_rail: CodeGraphProfileRail | None = None
        self._code_graph_reload_fingerprint: tuple[Any, ...] | None = None
        self._code_graph_needs_warmup: bool = False
        self._code_agent_rail = None
        self._code_plan_approval_rail = None

    # ─── Language override ────────────────────────

    @staticmethod
    def _resolve_prompt_language() -> str:
        """Code mode always uses English for system prompts."""
        return "en"

    def _resolve_runtime_language(self) -> str:
        """Resolve runtime prompt language for code profile rails."""
        return self._runtime_language_override or "en"

    def _resolve_output_language(self) -> str:
        """Resolve user's preferred output language for runtime_state display.

        Distinct from prompt/runtime language (always "en" in code mode).
        Returns the normalized language code ("cn"/"en") based on
        config.yaml preferred_language, so the Language section injected
        by RuntimePromptRail can instruct the LLM to respond in the
        user's chosen language.
        """
        config_base = get_config()
        raw = str(config_base.get("preferred_language", "zh")).strip().lower()
        if raw == "zh":
            raw = "cn"
        return resolve_language(raw)

    # ─── 初始化 ──────────────────────────────

    async def create_instance(self, config: dict[str, Any] | None = None, *,
                              mode: str = "code", sub_mode: str = None) -> None:
        """初始化 DeepAgent 实例（code 模式）.

        统一使用 create_deep_agent()，不传 vision_model_config /
        audio_model_config。
        completion_timeout 从配置读取，可在 react / modes.code 中自定义。
        """
        # Propagate create params to per-session child adapters (see
        # JiuWenSwarmDeepAdapter._get_or_create_session_adapter).  The parent
        # deep adapter sets these fields; code mode must do the same or every
        # chat turn spawns a session adapter with project_dir=None → default
        # coding_memory/.
        self._session_instance_config = dict(config or {}) if isinstance(config, dict) else None
        self._session_instance_mode = mode
        self._session_instance_sub_mode = sub_mode
        composition_scope = resolve_agent_composition_scope(mode, sub_mode)

        await self.set_checkpoint()

        self._instance_overrides = dict(config or {}) if isinstance(config, dict) else {}
        config_base = get_config()
        self._config_base_cache = config_base.copy()
        self._refresh_multimodal_configs(config_base)
        config = config_base.get('react', {}).copy()
        self._config_cache = config.copy()
        self._agent_name = self._instance_overrides.get(
            "agent_name", config.get("agent_name", "main_agent")
        )
        self._project_dir = self._instance_overrides.get(
            "project_dir", config.get("project_dir")
        )
        # _workspace_dir: 项目上下文路径，用于 Workspace(root_path)、LspTool 等需要项目目录的组件。
        # 优先使用 project_dir（LspTool sandbox 校验需要）。
        self._workspace_dir = (
            self._project_dir
            or config.get("workspace_dir")
            or str(get_agent_workspace_dir())
        )
        # _agent_workspace_dir: agent 数据存储路径，始终指向系统 workspace，
        # 用于 coding_memory、todo文件等不应写入用户项目目录的数据。
        self._agent_workspace_dir = str(get_agent_workspace_dir())
        self._permission_workspace_root = self._resolve_permission_workspace_root()
        self._platform_trusted_root = Path(get_agent_workspace_dir()).resolve(
            strict=False
        )

        self._dreaming_mode = "code"

        if self._skip_own_instance_build():
            return

        model = self._create_model(config_base)
        agent_card = AgentCard(name=self._agent_name, id=_AGENT_CARD_ID)

        tool_cards = await self._get_tool_cards(self._tool_owner_id())
        self._tool_cards = tool_cards

        # 权限护栏由 openjiuwen PermissionInterruptRail + ToolPermissionHost 接管；
        # 无需初始化 jiuwenswarm 内置 PermissionEngine（已弃用）。

        sys_operation = self._create_sys_operation()
        if sys_operation is None:
            raise RuntimeError("sys_operation is not available, maybe task is not running")
        self._sys_operation = sys_operation
        self._prepare_browser_runtime_security(model, config_base)
        rails_list = self._build_agent_rails(
            config,
            config_base,
            mode="code",
            composition_scope=composition_scope,
        )

        configured_subagents, _should_add_general = self._build_configured_subagents(model, config, config_base)
        configured_subagents = configured_subagents or []

        workspace = Workspace(
            root_path=self._workspace_dir or "./",
            language=self._resolve_runtime_language(),
        )
        _set_workspace_coding_memory_directory(
            workspace,
            project_dir=self._project_dir or self._workspace_dir,
            agent_workspace_dir=self._agent_workspace_dir,
            description="Coding Agent 记忆模块",
        )

        self._instance = create_deep_agent(
            model=model,
            card=agent_card,
            tool_owner_id=self._tool_owner_id(),
            system_prompt=build_code_system_prompt(),
            tools=tool_cards if tool_cards else [],
            subagents=configured_subagents,
            rails=rails_list if rails_list else [],
            enable_task_loop=config.get("enable_task_loop", True),
            max_iterations=config.get("max_iterations", 15),
            workspace=workspace,
            sys_operation=sys_operation,
            language=self._resolve_runtime_language(),
            enable_read_image_multimodal=self._resolve_enable_read_image_multimodal(config),
            context_engine_config=_deep_agent_context_engine_config(config),
            kv_cache_affinity_config=_deep_agent_kv_cache_affinity_config(config, model),
            auto_create_workspace=False,
            completion_timeout=resolve_task_loop_completion_timeout(config),
            code_graph_config=self._build_code_graph_config(config_base),
        )
        self._remember_code_graph_reload_fingerprint(config_base)

        # 改动3：让 agent 初始化（ensure_initialized）在独立线程 + 独立事件循环里跑，
        # 主事件循环在初始化的十几秒里保持响应，esc 的 cancel 不再堵队列、后端能尽快停。
        #
        # 为什么不能直接 `await self._instance.ensure_initialized()`：
        #   ensure_initialized 是 async def（openjiuwen/harness/deep_agent.py:864），但它内部
        #   _ensure_initialized 里有同步阻塞段——init_workspace()（建目录）、rail_inst.init(self)
        #   （各 rail init，ProjectMemoryRail 扫描项目记忆文件/建索引就在这）。这些是 CPU/磁盘密集
        #   的同步调用，跑时不 yield 事件循环，主事件循环被占住，WebSocket recv() 读不进 esc 的
        #   cancel 消息——用户按 esc 后后端要等十几秒才停（真 bug，日志 14:25:30→50 坐实）。
        #
        # 为什么不能用 asyncio.to_thread：to_thread 只能包同步函数；ensure_initialized 是 async def，
        # 直接传会在无运行 loop 的线程里报错。必须用 asyncio.run 在子线程里另起独立 loop 来跑它。
        #
        # 做法：run_in_executor 把"在子线程跑 asyncio.run(ensure_initialized())"丢到线程池，
        # 主事件循环 await 这个 future 时保持响应，期间能读取并处理 esc 的 cancel。
        # 跑完后回主 loop 继续后续代码，时序与语义不变；唯一变化是初始化期间主 loop 活。
        #
        # 安全性：ensure_initialized 操作的是 self._pending_rails、workspace 文件等实例自己的资源，
        # 不碰全局 asyncio 原语，不跨 loop 访问主 loop 对象，不会死锁。
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: asyncio.run(self._instance.ensure_initialized()),
        )
        # 修正 .agent_history 写入路径：openjiuwen 文件工具默认将
        # .agent_history 写到 Workspace.root_path（即项目目录），
        # 这里覆写为 agent 系统 workspace，避免污染用户项目目录。
        for rail in getattr(self._instance, '_registered_rails', []):
            for tool in getattr(rail, 'tools', []) or []:
                if hasattr(tool, '_workspace_path'):
                    setattr(tool, '_workspace_path', self._agent_workspace_dir)
        initial_workspace = self._project_dir or str(
            get_default_project_session_workspace_dir()
        )
        self._seed_runtime_cwd(initial_workspace, workspace=initial_workspace)

        setattr(self._instance, "_jiuwenswarm_adapter_mode", "code")
        setattr(
            self._instance,
            "_jiuwenswarm_code_project_dir",
            initial_workspace,
        )
        setattr(
            self._instance,
            "_jiuwenswarm_project_dir",
            initial_workspace,
        )

        # code 模式不传: vision_model_config, audio_model_config
        #（context_engine_config / completion_timeout 已从配置读取传入）

        # Cron tools belong to the agent's standing toolset, not to any one
        # request; build them here so the first turn does not pay for it either.
        self._ensure_cron_tools_registered(self._parent_session_id)
        self._registered_mcp_server_ids.clear()
        self._registered_mcp_servers.clear()
        await self._register_mcp_servers_from_config(config_base, tag="code")
        logger.info("[JiuwenSwarmCodeAdapter] 初始化完成: agent_name=%s", self._agent_name)

        await self.load_user_rails()
        self._schedule_code_graph_warmup()

    # ─── Rails 构建 ──────────────────────────

    def _build_profile_rail_specs(
        self,
        config: dict[str, Any],
        config_base: dict[str, Any],
        *,
        mode: str,
    ) -> _AgentProfileRailSpecs:
        """Return Code-only rail specs for the parent-owned composition."""
        before_stream_event = [
            _AgentRailBuildSpec("_runtime_prompt_rail", self._build_runtime_prompt_rail),
            _AgentRailBuildSpec("_response_prompt_rail", self._build_response_prompt_rail),
            _AgentRailBuildSpec(
                "_skill_retrieval_prompt_rail",
                self._build_skill_retrieval_prompt_rail,
            ),
        ]
        before_permission = [
            _AgentRailBuildSpec("_code_agent_mode_rail", self._build_agent_mode_rail),
            _AgentRailBuildSpec(
                "_code_plan_pre_permission_guard_rail",
                self._build_plan_pre_permission_guard_rail,
            ),
            _AgentRailBuildSpec("_security_rail", self._build_security_rail),
            _AgentRailBuildSpec("_lsp_rail", self._build_lsp_rail_via_config),
            _AgentRailBuildSpec(
                "_project_memory_rail", self._build_project_memory_rail
            ),
        ]
        after_permission = [
            _AgentRailBuildSpec("_code_filesystem_rail", self._build_filesystem_rail),
            _AgentRailBuildSpec("_coding_memory_rail", self._build_coding_memory_rail),
            _AgentRailBuildSpec(
                "_memory_forbidden_rail", self._build_memory_forbidden_rail
            ),
            _AgentRailBuildSpec(
                "_code_ask_user_rail", self._build_structured_ask_user_rail
            ),
            _AgentRailBuildSpec(
                "_code_confirm_interrupt_rail",
                self._build_confirm_interrupt_rail,
                {"tool_names": ["switch_mode"]},
            ),
            _AgentRailBuildSpec(
                "_context_processor_rail", self._build_context_processor_rail
            ),
            _AgentRailBuildSpec(
                "_code_task_planning_rail", self._build_code_task_planning_rail
            ),
            _AgentRailBuildSpec("_code_agent_rail", self._build_code_agent_rail),
            _AgentRailBuildSpec(
                "_code_plan_approval_rail", self._build_plan_approval_rail
            ),
        ]

        flags = self._code_graph_flags(config_base)
        if flags.on_root:
            after_permission.append(
                _AgentRailBuildSpec(
                    "_code_graph_profile_rail",
                    self._build_code_graph_profile_rail,
                    {"config_base": config_base},
                )
            )

        # 动态 Rails — 从 config.yaml::modes.code.rails 读取
        # 跳过已在固定列表中的 rail，避免重复注册
        mode_config = config_base.get("modes", {}).get("code", {})
        configured_rails = mode_config.get("rails") or []

        for rail_name in configured_rails:
            if rail_name in self._FIXED_RAIL_NAMES:
                logger.info(
                    "[JiuwenSwarmCodeAdapter] Rail %s already in fixed set, skipping dynamic registration",
                    rail_name,
                )
                continue
            method_name = _RAIL_BUILD_NAMES.get(rail_name)
            if method_name is None:
                if rail_name == "MemoryRail":
                    logger.warning(
                        "[JiuwenSwarmCodeAdapter] MemoryRail is not supported in code mode; "
                        "use CodingMemoryRail instead. Skipping",
                    )
                else:
                    logger.warning(
                        "[JiuwenSwarmCodeAdapter] Unknown rail name in config: %s, skipping",
                        rail_name,
                    )
                continue
            method = getattr(self, method_name, None)
            if method is None:
                logger.warning(
                    "[JiuwenSwarmCodeAdapter] Build method %s not found, skipping",
                    method_name,
                )
                continue
            attr_name = f"_dynamic_{rail_name}"
            after_permission.append(_AgentRailBuildSpec(attr_name, method))
            logger.info(
                "[JiuwenSwarmCodeAdapter] Dynamic rail %s queued from config",
                rail_name,
            )

        return _AgentProfileRailSpecs(
            before_stream_event=tuple(before_stream_event),
            before_permission=tuple(before_permission),
            after_permission=tuple(after_permission),
        )

    # ─── Code 专属 Rail 构建 ────────────────

    def _build_filesystem_rail(self) -> SysOperationRail | None:
        """构建 SysOperationRail（FileSystemRail）."""
        try:
            fs_rail = SysOperationRail()
            logger.info("[JiuwenSwarmCodeAdapter] SysOperationRail create success")
            return fs_rail
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] SysOperationRail create failed: %s", exc)
            return None

    def _code_graph_flags(self, config_base: dict[str, Any] | None) -> CodeGraphFlags:
        return resolve_code_graph_flags(config_base)

    def _resolve_code_graph_cache_dir(self, raw_cache: object) -> str:
        """Absolute checkpoint dir. Relative values follow the agent workspace.

        Never a process-cwd relative path: that would write different places on
        another machine or after a chdir. Omit yaml ``cache_dir`` unless you
        need a shared disk; do not put a machine-specific home path in yaml.
        """
        workspace = getattr(self, "_agent_workspace_dir", None) or getattr(
            self, "_project_dir", None
        )
        if raw_cache:
            path = Path(str(raw_cache)).expanduser()
            if not path.is_absolute():
                base = Path(workspace) if workspace else Path.cwd()
                path = base / path
            return str(path.resolve())
        base = Path(workspace) if workspace else Path.cwd()
        return str((base / ".code_graph_cache").resolve())

    def _build_code_graph_config(self, config_base: dict[str, Any] | None) -> CodeGraphConfig:
        raw = (config_base or {}).get("code_graph") if isinstance(config_base, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        defaults = CodeGraphConfig()
        cache_dir = self._resolve_code_graph_cache_dir(raw.get("cache_dir"))
        return CodeGraphConfig(
            cache_dir=cache_dir,
            max_files=parse_int(raw.get("max_files"), defaults.max_files),
            max_source_bytes=parse_int(raw.get("max_source_bytes"), defaults.max_source_bytes),
            max_build_rss_mb=parse_int(raw.get("max_build_rss_mb"), defaults.max_build_rss_mb),
            max_cache_size_mb=parse_int(raw.get("max_cache_size_mb"), defaults.max_cache_size_mb or 2048),
        )

    def _code_graph_reload_key(self, config_base: dict[str, Any] | None) -> tuple[Any, ...]:
        """User-facing knobs that must remount the live rail when they change."""
        flags = self._code_graph_flags(config_base)
        cfg = self._build_code_graph_config(config_base)
        return (
            flags.profile,
            flags.on_root,
            flags.on_code_agent,
            cfg.max_files,
            cfg.max_source_bytes,
            cfg.max_build_rss_mb,
            cfg.max_cache_size_mb,
            cfg.cache_dir,
        )

    def _remember_code_graph_reload_fingerprint(self, config_base: dict[str, Any] | None) -> None:
        self._code_graph_reload_fingerprint = self._code_graph_reload_key(config_base)

    def _sync_code_graph_rail_for_reload(
        self,
        config_base: dict[str, Any] | None,
    ) -> CodeGraphProfileRail | None:
        """Rebuild the Root graph rail when yaml changed. None keeps the live rail.

        DeepAgent.configure only cycles rails whose type is in the new list.
        Returning a rail — including ``profile=off`` — retires the old one.
        """
        fingerprint = self._code_graph_reload_key(config_base)
        if fingerprint == self._code_graph_reload_fingerprint:
            return None
        self._code_graph_reload_fingerprint = fingerprint
        flags = self._code_graph_flags(config_base)
        if flags.on_root:
            rail = self._build_code_graph_profile_rail(config_base or {})
        else:
            rail = CodeGraphProfileRail(PROFILE_OFF)
        self._code_graph_profile_rail = rail
        self._code_graph_needs_warmup = flags.enabled
        inst = getattr(self, "_instance", None)
        deep_config = getattr(inst, "deep_config", None) if inst is not None else None
        if deep_config is not None:
            deep_config.code_graph_config = self._build_code_graph_config(config_base)
        logger.info(
            "[JiuwenSwarmCodeAdapter] Code Graph config hot-reloaded profile=%s on_root=%s",
            flags.profile,
            flags.on_root,
        )
        return rail

    def _schedule_code_graph_warmup(self) -> None:
        """Build as soon as the coding session has a project. Do not block start."""
        config_base = getattr(self, "_config_base_cache", None) or get_config()
        flags = self._code_graph_flags(config_base)
        if not flags.enabled:
            return
        root = self._project_dir or self._workspace_dir
        if not root:
            return
        cfg = self._build_code_graph_config(config_base)
        inst = getattr(self, "_instance", None)
        deep_config = getattr(inst, "deep_config", None) if inst is not None else None
        if deep_config is not None:
            deep_config.code_graph_config = cfg
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _warm() -> None:
            try:
                from openjiuwen.core.retrieval.code_graph.manager import get_code_graph_manager

                await get_code_graph_manager(cfg).ensure_fresh(str(root), cfg)
            except Exception as exc:  # noqa: BLE001 — first find_* still retries
                logger.warning("[JiuwenSwarmCodeAdapter] Code Graph warmup failed: %s", exc)

        loop.create_task(_warm())
        logger.info(
            "[JiuwenSwarmCodeAdapter] Code Graph warmup scheduled for %s",
            root,
        )

    def _subagent_graph_factory_kwargs(
        self,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sub-agent create kwargs. Graph hang is chosen by ``code_graph.agent``."""
        kwargs: dict[str, Any] = {
            "auto_create_workspace": False,
        }
        if extra:
            kwargs.update(extra)
        return kwargs

    def _build_code_graph_profile_rail(self, config_base: dict[str, Any]) -> CodeGraphProfileRail:
        """Hang find_* on Root. Eval locate still uses coding_agent.py, not this."""
        flags = self._code_graph_flags(config_base)
        rail = CodeGraphProfileRail(
            flags.profile,
            config=self._build_code_graph_config(config_base),
        )
        logger.info(
            "[JiuwenSwarmCodeAdapter] CodeGraphProfileRail on Root profile=%s",
            flags.profile,
        )
        return rail

    def _code_agent_graph_kwargs(self, config_base: dict[str, Any] | None) -> dict[str, Any]:
        """Profile kwargs for code_agent when it owns the graph.

        Product yaml ``agent: root`` keeps this sub-agent on the original tools.
        An omitted agent key still hangs here (eval / old yaml). An off profile
        carries no index config: building one would resolve a cache directory
        for an agent that never queries the graph.
        """
        flags = self._code_graph_flags(config_base)
        if not flags.on_code_agent:
            return {"code_graph_profile": PROFILE_OFF}
        kwargs: dict[str, Any] = {"code_graph_profile": flags.profile}
        kwargs["code_graph_config"] = self._build_code_graph_config(config_base)
        return kwargs

    def _build_agent_mode_rail(self) -> AgentModeRail | None:
        """构建 CodeAgentModeRail。

        与 Claude Code 对齐：
        - ``plan_mode_attachment_note``: 通过 prompt attachment 注入 plan 提示，
          不修改 system prompt，保持 KV-cache 稳定，
          告知 LLM 必须先调 ``enter_plan_mode``。
        - ``enter_plan_instructions``: 追加到 ``enter_plan_mode`` 的 tool_result，
          包含完整的 5-phase 工作流说明（指令在对话中，不在 system prompt）。
        - ``exit_plan_notification``: 追加到 ``exit_plan_mode`` 的 tool_result，
          显式告知 LLM 已退出 plan 模式，可以开始编辑文件。
        """
        try:
            from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
                CodeAgentModeRail,
            )

            return CodeAgentModeRail(
                allowed_tools=_CODE_PLAN_ALLOWED_TOOLS,
                plan_mode_attachment_note=_PLAN_MODE_SYSTEM_NOTE,
                enter_plan_instructions=_ENTER_PLAN_MODE_INSTRUCTIONS_EN,
                exit_plan_notification=_EXIT_PLAN_MODE_NOTIFICATION,
            )
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] CodeAgentModeRail create failed: %s", exc)
            return None

    def _build_plan_pre_permission_guard_rail(
        self,
    ) -> CodePlanPrePermissionGuardRail | None:
        """Run the existing Code plan guard before Permission review."""

        if self._code_agent_mode_rail is None:
            logger.warning(
                "[JiuwenSwarmCodeAdapter] Code plan pre-Permission guard skipped: "
                "CodeAgentModeRail unavailable"
            )
            return None
        return CodePlanPrePermissionGuardRail(self._code_agent_mode_rail)

    @staticmethod
    def _build_code_task_planning_rail() -> CodeTaskPlanningRail | None:
        """Register todo tools without openjiuwen todo system prompt injection."""
        try:
            return CodeTaskPlanningRail()
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] CodeTaskPlanningRail create failed: %s", exc)
            return None

    def _build_structured_ask_user_rail(self) -> StructuredAskUserRail | None:
        """构建 StructuredAskUserRail."""
        try:
            return StructuredAskUserRail(language=self._resolve_runtime_language())
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] StructuredAskUserRail create failed: %s", exc)
            return None

    def _build_confirm_interrupt_rail(self, tool_names: list[str] | None = None) -> Any | None:
        """构建 CodeConfirmInterruptRail（控制类工具需用户确认，含可读提示）."""
        try:
            from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
                CodeConfirmInterruptRail,
            )

            # exit_plan_mode 由 PlanApprovalInterruptRail 负责计划审批，不再走 ConfirmInterrupt。
            filtered = [name for name in (tool_names or []) if name != "exit_plan_mode"]
            return CodeConfirmInterruptRail(tool_names=filtered or ["switch_mode"])
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] CodeConfirmInterruptRail create failed: %s", exc)
            return None

    def _build_lsp_rail_via_config(self) -> Any:
        """构建 LspRail（带 project_dir 参数）."""
        logger.info(
            "[JiuwenSwarmCodeAdapter] Building LspRail with project_dir=%s",
            self._project_dir,
        )
        return self._build_lsp_rail(workspace_dir=self._project_dir)

    def _build_lsp_rail(self, workspace_dir: str | None = None) -> LspRail | None:
        """Build LspRail（code 模式专属）."""
        try:
            lsp_rail = LspRail(InitializeOptions(cwd=workspace_dir))
            logger.info("[JiuwenSwarmCodeAdapter] LspRail create success")
        except ImportError as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] LspRail create failed: [config_error] %s", exc)
            lsp_rail = None
        except FileNotFoundError as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] LspRail create failed: [server_start_failed] %s", exc)
            lsp_rail = None
        except OSError as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] LspRail create failed: [server_start_failed] %s", exc)
            lsp_rail = None
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] LspRail create failed: [unknown] %s", exc)
            lsp_rail = None
        return lsp_rail

    def _build_coding_memory_rail(self) -> CodingMemoryRail | None:
        """构建 CodingMemoryRail（主 Agent 和 code_agent subagent 共用）.

        通过 self._coding_memory_rail 缓存避免重复构建。
        受 modes.code.memory.enabled 开关控制，关闭时返回 None。
        """
        # 检查 memory 开关
        if not is_memory_enabled("code", get_config()):
            logger.info("[JiuwenSwarmCodeAdapter] CodingMemoryRail disabled by modes.code.memory.enabled")
            return None

        # 单例保护：如果已构建，直接返回缓存实例
        if self._coding_memory_rail is not None:
            logger.info("[JiuwenSwarmCodeAdapter] CodingMemoryRail reuse cached instance")
            return self._coding_memory_rail

        try:
            coding_memory_rail = create_coding_memory_rail(
                project_dir=self._project_dir or self._workspace_dir,
                agent_workspace_dir=self._agent_workspace_dir,
                config=get_config(),
            )
            # 缓存实例，供 code_agent 复用
            self._coding_memory_rail = coding_memory_rail
            logger.info("[JiuwenSwarmCodeAdapter] CodingMemoryRail create success")
            return coding_memory_rail

        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] CodingMemoryRail create failed: %s", exc)
            return None

    def _build_project_memory_rail(self) -> ProjectMemoryRail | None:
        """Build ProjectMemoryRail to auto-load JIUWENSWARM.md / CLAUDE.md etc.

        Code 模式专属 — 受 modes.code.memory.enabled 开关控制。
        确保能检索到 /init 命令创建 JIUWENSWARM.md 的目录（当前工作目录）。
        """
        # 检查 memory 开关
        if not is_memory_enabled("code", get_config()):
            logger.info("[JiuwenSwarmCodeAdapter] ProjectMemoryRail disabled by modes.code.memory.enabled")
            return None

        try:
            workspace = self._project_dir or self._workspace_dir or "./"
            language = self._resolve_runtime_language()
            raw_additional_dirs = self._instance_overrides.get(
                "project_memory_additional_directories",
                self._config_cache.get("project_memory", {}).get("additional_directories"),
            )
            if raw_additional_dirs is None:
                raw_additional_dirs = os.getenv("JIUWENSWARM_ADDITIONAL_DIRECTORIES", "")

            if isinstance(raw_additional_dirs, str):
                additional_dirs = [
                    item.strip()
                    for item in raw_additional_dirs.split(os.pathsep)
                    if item.strip()
                ]
            elif isinstance(raw_additional_dirs, (list, tuple, set)):
                additional_dirs = [
                    str(item).strip()
                    for item in raw_additional_dirs
                    if str(item).strip()
                ]
            else:
                additional_dirs = []

            rail = ProjectMemoryRail(
                workspace=workspace,
                language=language,
                additional_directories=tuple(additional_dirs),
            )
            logger.info(
                "[JiuwenSwarmCodeAdapter] ProjectMemoryRail create success "
                "(workspace=%s, language=%s, additional_dirs=%d)",
                workspace,
                language,
                len(additional_dirs),
            )
            return rail
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuwenSwarmCodeAdapter] ProjectMemoryRail create failed: %s", exc,
            )
            return None

    def _build_worktree_rail_via_config(self) -> WorktreeRail | None:
        """Build WorktreeRail for code mode.

        Mounts enter_worktree / exit_worktree tools on the main agent so it
        can work in an isolated git worktree. WorktreeManager resolves the
        repo root from the runtime cwd via ``find_canonical_git_root``;
        jiuwenswarm anchors the agent cwd to project_dir through Workspace
        on init, so worktrees land under the user's actual repo. Only
        enabled=True is set; other WorktreeConfig knobs keep library
        defaults until a config schema is introduced. The instance is
        cached because mode switches do not unregister this rail.
        """
        if self._worktree_rail is not None:
            logger.info("[JiuwenSwarmCodeAdapter] WorktreeRail reuse cached instance")
            return self._worktree_rail
        try:
            rail = WorktreeRail(config=WorktreeConfig(enabled=True))
            self._worktree_rail = rail
            logger.info(
                "[JiuwenSwarmCodeAdapter] WorktreeRail create success (project_dir=%s)",
                self._project_dir,
            )
            return rail
        except Exception as exc:  # noqa: BLE001
            logger.warning("[JiuwenSwarmCodeAdapter] WorktreeRail create failed: %s", exc)
            return None

    # ─── 配置驱动的 Rail/Tool 构建代理 ──────────

    def _build_skill_rail_via_config(self) -> Any:
        """构建 SkillUseRail（从 config 读取参数）."""
        include_tools = not self._is_acp_tool_profile(self._instance_overrides)
        return self._build_skill_rail(
            self._config_cache,
            include_tools=include_tools,
        )

    def _build_context_assemble_rail(self) -> Any:
        """构建 ContextEngineeringRail."""
        return ContextAssembleRail()

    def _build_context_processor_rail(self) -> Any:
        """构建 ContextProcessorRail — 复用父类逻辑."""
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _build_context_processor_rail
        return _build_context_processor_rail(self._config_cache)

    def _build_skill_evolution_rail_via_config(self) -> Any:
        """构建 SkillEvolutionRail."""
        return self._build_skill_evolution_rail(get_config())

    # ─── Subagent 配置 ──────────────────────────

    @staticmethod
    def _subagent_list_has_name(subagents: list, name: str) -> bool:
        """检查 subagents 列表中是否已包含指定名字的 subagent."""
        for spec in subagents:
            if isinstance(spec, SubAgentConfig):
                if spec.agent_card.name == name:
                    return True
            else:
                card = getattr(spec, "card", None)
                if getattr(card, "name", None) == name:
                    return True
        return False

    def _build_configured_subagents(
            self,
            model: Model,
            config: dict[str, Any],
            config_base: dict[str, Any] | None = None,
    ) -> tuple[list[Any] | None, bool]:
        """Build subagents for code mode, including the built-in status-line setup agent.

        explore_agent / plan_agent 固定挂载（Code 模式核心子代理）。
        code_agent / browser_agent 按配置启用。

        每个 spec 都带上主 Agent 的 ``sys_operation``：子 Agent 必须和父 Agent 处在
        同一个文件系统边界里。若留空，``DeepAgent.create_subagent`` 会给子 Agent
        新建一个 ``OperationMode.LOCAL`` 的 SysOperation，并按
        ``spec.restrict_to_work_dir or deep_config.restrict_to_work_dir``（父侧默认
        True）打开 ``restrict_to_sandbox``，于是两个方向同时出错：

        - 本地模式（用户选「完全访问」）下子 Agent 反而被锁死在 workspace 里，
          读父 Agent 能读的项目路径会报 "Access denied: Path ... outside sandbox"；
        - sandbox 模式下子 Agent 却拿到 LOCAL SysOperation，直接落到宿主机上跑，
          绕过了整个沙箱。

        注意 ``create_subagent`` 只有在 ``spec.workspace`` 也非空时才采纳
        ``spec.sys_operation``，这里每个 spec 都显式传了 workspace，条件成立。
        """
        react_cfg = config if isinstance(config, dict) else {}
        subagents_cfg = react_cfg.get("subagents")

        resolved_language = self._resolve_runtime_language()
        workspace = self._workspace_dir or "./"
        sys_operation = self._sys_operation
        subagents: list[Any] = []
        self._sync_browser_runtime_environment(config_base)

        statusline_setup_cfg = (
            subagents_cfg.get(STATUSLINE_SETUP_AGENT_TYPE)
            if isinstance(subagents_cfg, dict)
            else None
        )
        if self._is_subagent_default_enabled(statusline_setup_cfg):
            statusline_setup_options = (
                statusline_setup_cfg if isinstance(statusline_setup_cfg, dict) else {}
            )
            subagents.append(
                build_statusline_setup_agent_config(
                    model,
                    workspace=workspace,
                    sys_operation=sys_operation,
                    language=resolved_language,
                    max_iterations=parse_int(
                        statusline_setup_options.get("max_iterations"),
                        DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
                    ),
                )
            )

        # ── 固定挂载：explore_agent（Code 模式核心子代理，始终启用）──
        if not self._subagent_list_has_name(subagents, "explore_agent"):
            explore_agent_cfg = subagents_cfg.get("explore_agent") if isinstance(subagents_cfg, dict) else None
            explore_spec = build_explore_agent_config(
                model=model,
                workspace=workspace,
                sys_operation=sys_operation,
                language=resolved_language,
                max_iterations=parse_int(
                    explore_agent_cfg.get("max_iterations") if isinstance(explore_agent_cfg, dict) else None,
                    react_cfg.get("max_iterations", 15),
                ),
            )
            explore_spec.factory_kwargs = self._subagent_graph_factory_kwargs()
            subagents.append(explore_spec)

        # ── 固定挂载：plan_agent（Code 模式核心子代理，始终启用）──
        if not self._subagent_list_has_name(subagents, "plan_agent"):
            plan_agent_cfg = subagents_cfg.get("plan_agent") if isinstance(subagents_cfg, dict) else None
            plan_spec = build_plan_agent_config(
                model=model,
                workspace=workspace,
                sys_operation=sys_operation,
                language=resolved_language,
                max_iterations=parse_int(
                    plan_agent_cfg.get("max_iterations") if isinstance(plan_agent_cfg, dict) else None,
                    react_cfg.get("max_iterations", 15),
                ),
            )
            plan_spec.factory_kwargs = self._subagent_graph_factory_kwargs()
            subagents.append(plan_spec)

        # Code Graph is a profile, not a separate sub-agent. Root hang is a
        # rail on this DeepAgent; code_agent hang is factory_kwargs below.

        if isinstance(subagents_cfg, dict):
            # code_agent subagent — 按配置启用
            code_agent_cfg = subagents_cfg.get("code_agent")
            if self._is_subagent_enabled(code_agent_cfg):
                code_agent_rails = None
                # 复用主 Agent 已构建的 CodingMemoryRail
                coding_memory_rail = self._coding_memory_rail
                if coding_memory_rail is not None:
                    # SysOperationRail is default rail for code_agent;
                    # passing rails overrides defaults, must include it explicitly
                    code_agent_rails = [SysOperationRail(), coding_memory_rail]
                graph_kwargs = self._code_agent_graph_kwargs(config_base)
                code_spec = build_code_agent_config(
                    model,
                    workspace=workspace,
                    sys_operation=sys_operation,
                    language=resolved_language,
                    rails=code_agent_rails,
                    max_iterations=parse_int(
                        code_agent_cfg.get("max_iterations"),
                        react_cfg.get("max_iterations", 15),
                    ),
                    **graph_kwargs,
                )
                code_spec.factory_kwargs = self._subagent_graph_factory_kwargs(
                    code_spec.factory_kwargs,
                )
                subagents.append(code_spec)

            # browser_agent
            browser_agent_cfg = subagents_cfg.get("browser_agent")

            browser_enabled = self._browser_runtime_enabled()
            if browser_enabled:
                if not str(os.getenv("BROWSER_DRIVER") or "").strip():
                    os.environ["BROWSER_DRIVER"] = "managed"
                    logger.info(
                        "[JiuwenSwarmCodeAdapter] browser subagent enabled without BROWSER_DRIVER; "
                        "defaulting to managed mode"
                    )
                browser_kwargs: dict[str, Any] = {}
                if self._browser_runtime_settings is not None:
                    browser_kwargs["settings"] = self._browser_runtime_settings
                browser_spec = build_browser_agent_config(
                    model,
                    workspace=workspace,
                    sys_operation=sys_operation,
                    language=resolved_language,
                    max_iterations=parse_int(
                        browser_agent_cfg.get("max_iterations") if isinstance(browser_agent_cfg, dict) else None,
                        DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
                    ),
                    **browser_kwargs,
                )
                browser_spec.factory_kwargs["auto_create_workspace"] = False
                subagents.append(browser_spec)

        # ── 自定义 agent 不加入 deep_config.subagents ──
        # Code 模式下，自定义 agent 由 CodeAgentRail 的 Agent 工具管理，
        # 不走 SubagentRail 的 task_tool 路径。
        # （agent 模式仍由 interface_deep.py 的 _load_custom_subagents 管理）

        return subagents or None, False

    # ─── Rail 生命周期(mode切换) ───────────────────

    def _user_interaction_rail_attribute(self) -> str:
        return "_code_ask_user_rail"

    async def _update_rails_for_mode(self, mode: str) -> None:
        """Code 模式下的 rail 生命周期管理.

        code.normal / code.plan 等模式：
        - 保留 SubagentRail（主 Agent 通过 task_tool 派发 explore/plan 子代理）
        - 保留 ProjectMemoryRail（code 模式始终挂载）
        - 保留 CodingMemoryRail（code 模式始终挂载）
        - 卸载 TaskPlanningRail、SkillEvolutionRail
        """
        # 卸载非 code 专属 rails
        rail_specs = (
            ("_task_planning_rail", "TaskPlanningRail"),
            ("_skill_evolution_rail", "SkillEvolutionRail"),
            ("_evolution_interrupt_rail", "EvolutionInterruptRail"),
        )

        for attr, label in rail_specs:
            rail = getattr(self, attr, None)
            if rail is not None:
                await self._instance.unregister_rail(rail)
                setattr(self, attr, None)
                logger.info(
                    "[JiuwenSwarmCodeAdapter] %s unregistered for %s mode",
                    label, mode,
                )

        # code 模式保留 SubagentRail；若缺失则补充注册
        if self._subagent_rail is None:
            self._subagent_rail = self._build_subagent_rail()
            if self._subagent_rail is not None:
                await self._instance.register_rail(self._subagent_rail)
                logger.info(
                    "[JiuwenSwarmCodeAdapter] SubagentRail (re)registered for %s",
                    mode,
                )

        # code 模式保留 ProjectMemoryRail；若缺失则补充注册
        if self._project_memory_rail is None:
            self._project_memory_rail = self._build_project_memory_rail()
            if self._project_memory_rail is not None:
                await self._instance.register_rail(self._project_memory_rail)
                logger.info(
                    "[JiuwenSwarmCodeAdapter] ProjectMemoryRail (re)registered for %s",
                    mode,
                )

        # code 模式保留 CodingMemoryRail；若缺失则补充注册
        if self._coding_memory_rail is None:
            coding_memory_rail = self._build_coding_memory_rail()
            if coding_memory_rail is not None:
                # _build_coding_memory_rail 已缓存到 self._coding_memory_rail
                await self._instance.register_rail(coding_memory_rail)
                logger.info(
                    "[JiuwenSwarmCodeAdapter] CodingMemoryRail (re)registered for %s",
                    mode,
                )

    def _build_code_agent_rail(self) -> CodeAgentRail | None:
        """构建 CodeAgentRail，管理 /agents 创建的自定义 agent。"""
        try:
            rail = CodeAgentRail(workspace_dir=self._workspace_dir)
            logger.info("[JiuwenSwarmCodeAdapter] CodeAgentRail created")
            return rail
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] CodeAgentRail create failed: %s", exc)
            return None

    def _build_plan_approval_rail(self) -> PlanApprovalInterruptRail | None:
        """构建 PlanApprovalInterruptRail，管理 plan 审批生命周期。

        ``exit_plan_mode`` 触发即时审批弹窗（对齐 Claude Code），
        用户批准后立即恢复 normal 模式。
        """
        try:
            rail = PlanApprovalInterruptRail()
            logger.info("[JiuwenSwarmCodeAdapter] PlanApprovalInterruptRail created")
            return rail
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] PlanApprovalInterruptRail create failed: %s", exc)
            return None

    def _make_deep_agent_config(self, **kwargs: Any) -> Any:
        """Keep ``code_graph_config`` on the live DeepAgent across hot reload."""
        deep_cfg = super()._make_deep_agent_config(**kwargs)
        config_base = kwargs.get("config_base") or getattr(self, "_config_base_cache", None)
        deep_cfg.code_graph_config = self._build_code_graph_config(config_base)
        return deep_cfg

    async def reload_agent_config(
        self,
        config_base: dict[str, Any] | None = None,
        env_overrides: dict[str, Any] | None = None,
        target_session_id: str | None = None,
    ) -> None:
        await super().reload_agent_config(
            config_base,
            env_overrides,
            target_session_id=target_session_id,
        )
        if self._code_graph_needs_warmup:
            self._code_graph_needs_warmup = False
            self._schedule_code_graph_warmup()

    def _get_current_agent_rails(
        self, config: dict[str, Any], config_base: dict[str, Any] | None = None
    ) -> list[Any]:
        """扩展父类方法，将 Code/Plan 专属 Rail 纳入热重载范围。

        父类 _get_current_agent_rails 只返回 skill/context/memory 等 rail，
        CodeAgentRail 和 PlanApprovalInterruptRail 不在其中。覆盖此方法确保 config reload
        时这些 rail 被正确重新初始化。
        """
        rails_list = super()._get_current_agent_rails(config, config_base)
        if self._code_agent_rail is not None:
            rails_list.append(self._code_agent_rail)
        if self._code_plan_approval_rail is not None:
            rails_list.append(self._code_plan_approval_rail)
        snapshot = config_base if isinstance(config_base, dict) else getattr(
            self, "_config_base_cache", None
        )
        graph_rail = self._sync_code_graph_rail_for_reload(snapshot)
        if graph_rail is not None:
            rails_list.append(graph_rail)
        return rails_list

    # ─── Runtime config ──────────────────────────

    async def _update_runtime_config(self, runtime_config: "JiuWenSwarmDeepAdapter._RuntimeConfig") -> None:
        """Code 模式 runtime config: ProjectMemoryRail 语言同步 + rail 模式切换."""
        if self._instance is None:
            raise RuntimeError("JiuwenSwarmCodeAdapter 未初始化，请先调用 create_instance()")

        project_workspace = (
            runtime_config.workspace
            or runtime_config.project_dir
            or self._project_dir
            or str(get_default_project_session_workspace_dir(runtime_config.session_id))
        )
        task_cwd = runtime_config.cwd or project_workspace
        self._seed_runtime_cwd(task_cwd, workspace=project_workspace)
        resolved_language = self._resolve_runtime_language()
        resolved_channel = str(runtime_config.channel_id or
                               self._resolve_prompt_channel(runtime_config.session_id) or "web").strip() or "web"
        if self._runtime_prompt_rail:
            # Language section (response language) must follow the user's
            # preferred language, not the code-mode "en" which only governs
            # system-prompt scaffolding (time/runtime/env sections).
            self._runtime_prompt_rail.set_language(self._resolve_output_language())
            self._runtime_prompt_rail.set_force_english(self._force_english_runtime_prompt)
            self._runtime_prompt_rail.set_channel(resolved_channel)
            self._runtime_prompt_rail.set_model_name(self._resolve_model_name())
            self._runtime_prompt_rail.set_mode(runtime_config.mode)
            self._runtime_prompt_rail.set_trusted_dirs(runtime_config.trusted_dirs)
            self._runtime_prompt_rail.set_runtime_paths(
                cwd=task_cwd,
                project_dir=runtime_config.project_dir or self._project_dir,
            )
            self._runtime_prompt_rail.set_session_id(runtime_config.session_id)
        # PermissionInterruptRail: per-request trusted_dirs 注入，使 external_directory
        # 检查将这些子树视为 internal 而跳过 ask/deny（与 RuntimePromptRail 对齐）。
        # 用 getattr 兼容绕过 __init__ 的测试构造（_permission_rail 仅在 rail 构建流程赋值）。
        permission_rail = getattr(self, "_permission_rail", None)
        if permission_rail is not None:
            try:
                permission_rail.set_trusted_dirs(runtime_config.trusted_dirs)
            except Exception:
                logger.debug(
                    "[JiuwenSwarmCodeAdapter] permission_rail.set_trusted_dirs failed",
                    exc_info=True,
                )
        self._write_runtime_state(
            mode=runtime_config.mode,
            language=self._resolve_output_language(),
            channel=resolved_channel,
            session_id=runtime_config.session_id,
            project_dir=runtime_config.project_dir
            or task_cwd
            or self._project_dir
            or self._workspace_dir,
        )

        # ProjectMemoryRail 语言同步 + trusted_dirs 注入
        if self._project_memory_rail is not None:
            self._project_memory_rail.set_language(resolved_language)
            # trusted_dirs 来自 CLI 端的 trusted_dirs / workspace-dir，
            # 包含用户项目目录（即 /init 写 JIUWENSWARM.md 的目录）
            if runtime_config.trusted_dirs:
                self._project_memory_rail.set_additional_directories(
                    runtime_config.trusted_dirs,
                )

        # code 模式始终走 _update_rails_for_mode 的 code 逻辑
        await self._update_rails_for_mode(runtime_config.mode)
        await self._set_user_interaction_enabled(runtime_config.supports_user_interaction)
        await self._update_tools_for_mode(runtime_config.mode, runtime_config.session_id, runtime_config.request_id)
        await self._update_session_tools(
            runtime_config.session_id,
            runtime_config.request_id,
            channel_id=runtime_config.channel_id,
        )
        self._refresh_acp_runtime_tools(
            runtime_config.session_id,
            runtime_config.request_id,
            runtime_config.channel_id,
            runtime_config.request_metadata,
        )
        self._update_prompt_for_mode(runtime_config.mode, resolved_language)

        # user_todos channel_id per-request sync
        try:
            from jiuwenswarm.agents.harness.common.tools.user_todo_tool import (
                get_decorated_tools as _get_user_todo_tools,
                set_global_workspace_dir as _set_user_todo_workspace,
                set_global_channel_id as _set_user_todo_channel_id,
            )
            _set_user_todo_workspace(self._agent_workspace_dir)
            _set_user_todo_channel_id(_CRON_TOOL_CHANNEL_ID.get())
            for tool in _get_user_todo_tools():
                self._register_shared_tool(tool)
                self._instance.ability_manager.add(tool.card)
        except ImportError:
            pass

    # ─── Tools 构建 ──────────────────────────

    async def _get_tool_cards(self, agent_id: str) -> list[Any]:
        return self.build_code_tool_cards(agent_id)

    def build_code_tool_cards(self, agent_id: str) -> list[Any]:
        """Get tool cards for code mode — from config.yaml::modes.code.tools.

        ``modes.code.tools`` is an open extension point, so ownership is read
        off each card rather than guessed from the build func: a card that does
        not declare itself shared is treated as agent-owned, which is the safe
        default (a shared instance registered per agent costs an extra object;
        an agent-owned instance registered as shared would pin the first
        session's state for the whole process).

        Args:
            agent_id: Owner id this adapter registers its own instances under.

        Returns:
            The cards of every successfully registered configured tool.
        """
        tool_cards = []

        config_base = get_config()
        mode_config = config_base.get("modes", {}).get("code", {})
        configured_tools = mode_config.get("tools") or []

        for tool_name in configured_tools:
            result = self._get_tool_build_func(tool_name, agent_id)
            if result is None:
                logger.warning(
                    "[JiuwenSwarmCodeAdapter] Unknown or failed tool: %s, skipped",
                    tool_name,
                )
                continue
            if isinstance(result, list):
                for tool_instance in result:
                    register_tool(tool_instance, agent_id)
                    tool_cards.append(tool_instance.card)
            else:
                register_tool(result, agent_id)
                tool_cards.append(result.card)
            logger.info(
                "[JiuwenSwarmCodeAdapter] Tool %s registered from config",
                tool_name,
            )

        return tool_cards

    def _get_tool_build_func(self, tool_name: str, agent_id: str) -> Any | None:
        """根据 tool 名字调用对应构建方法."""
        method_name = _TOOL_BUILD_NAMES.get(tool_name)
        if method_name is None:
            logger.warning(
                "[JiuwenSwarmCodeAdapter] Unknown tool name in config: %s, skipping",
                tool_name,
            )
            return None
        method = getattr(self, method_name, None)
        if method is None:
            return None
        return method(agent_id)

    def _build_web_free_search_tool(self, agent_id: str) -> Any:
        """构建 web_free_search 工具."""
        return TrustedWebFreeSearchTool(
            language=self._resolve_runtime_language(), agent_id=agent_id
        )

    def _build_web_fetch_webpage_tool(self, agent_id: str) -> Any:
        """构建 web_fetch_webpage 工具."""
        return WebFetchWebpageTool(
            language=self._resolve_runtime_language(), agent_id=agent_id
        )

    def _build_paid_search_tool(self, agent_id: str) -> WebPaidSearchTool | None:
        """条件注册付费搜索工具：有任意一个付费 API Key 才注册."""
        if not any(
            os.environ.get(key)
            for key in ("BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY")
        ):
            logger.info("[JiuwenSwarmCodeAdapter] web_paid_search skipped: no paid search API key")
            return None
        tool = WebPaidSearchTool(
            language=self._resolve_runtime_language(), agent_id=agent_id
        )
        self._paid_search_tool = tool
        self._paid_search_registered = True
        return tool

    def _build_user_todos_tool(self, agent_id: str) -> list[Any] | None:
        """注册 user_todos 工具."""
        try:
            from jiuwenswarm.agents.harness.common.tools.user_todo_tool import (
                get_decorated_tools as _get_user_todo_tools,
                set_global_workspace_dir as _set_user_todo_workspace,
                set_global_channel_id as _set_user_todo_channel_id,
            )
            _set_user_todo_workspace(self._agent_workspace_dir)
            _set_user_todo_channel_id(self._runtime_cron_tool_context.channel_id)
            tools = _get_user_todo_tools()
            return tools
        except ImportError:
            logger.info("[JiuwenSwarmCodeAdapter] user_todos skipped: module not importable")
            return None

    def _build_skill_toolkit(self, agent_id: str) -> list[Any] | None:
        """构建 SkillToolkit 工具（不注册到 Runner，由 _get_tool_cards 统一注册）.

        Declared shared for the same reason as the deep adapter's path: skill
        install/status is a process-wide conversation, so one toolkit instance
        has to serve every adapter.
        """
        try:
            skill_toolkit = SkillToolkit(manager=self._skill_manager)
            tools = mark_stateless(skill_toolkit.get_tools())
            logger.info(
                "[JiuwenSwarmCodeAdapter] SkillToolkit built: tools=%s",
                [t.card.name for t in tools],
            )
            return tools
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] skill_toolkit build failed: %s", exc)
            return None

    def _skill_retrieval_tools_enabled_for_runtime(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        """Respect code-mode configured tools during runtime skill retrieval sync."""
        if not super()._skill_retrieval_tools_enabled_for_runtime(config_base):
            return False
        config = config_base if isinstance(config_base, dict) else get_config()
        configured_tools = config.get("modes", {}).get("code", {}).get("tools") or []
        return "skill_retrieval" in configured_tools

    def _build_skill_retrieval_toolkit(self, agent_id: str) -> list[Any] | None:
        """构建 SkillRetrievalToolkit 工具（不注册到 Runner，由 _get_tool_cards 统一注册）."""
        if not self._skill_retrieval_tools_enabled_for_runtime():
            logger.info("[JiuwenSwarmCodeAdapter] SkillRetrievalToolkit skipped: disabled")
            return None
        try:
            tools = self._create_skill_retrieval_tools()
            if not tools:
                return None
            self._skill_retrieval_tools = tools
            self._skill_retrieval_tools_registered = bool(tools)
            logger.info(
                "[JiuwenSwarmCodeAdapter] SkillRetrievalToolkit built: tools=%s",
                [tool.card.name for tool in tools],
            )
            return tools
        except Exception as exc:
            logger.warning("[JiuwenSwarmCodeAdapter] skill_retrieval build failed: %s", exc)
            return None

    def _build_acp_chat_tool(self, agent_id: str) -> Any | None:
        """Register acp_chat when at least one external ACP profile is configured."""
        acp_cfg = get_config().get("acp_agents")
        if not isinstance(acp_cfg, dict) or not acp_cfg:
            logger.info("[JiuwenSwarmCodeAdapter] acp_chat skipped: no acp_agents configured")
            return None
        return acp_chat

    def merge_member_mcp_configs(self, agent: Any, config_base: dict[str, Any]) -> int:
        """Merge enabled code-mode MCP configs into a team member agent."""
        deep_config = getattr(agent, "deep_config", None)
        if deep_config is None:
            return 0

        configured_mcps = list(getattr(deep_config, "mcps", None) or [])
        configured_ids = {
            str(getattr(cfg, "server_id", "") or getattr(cfg, "server_name", "") or "")
            for cfg in configured_mcps
        }
        added = 0
        for entry in self._extract_enabled_mcp_server_entries(config_base):
            cfg = self._build_mcp_server_config(entry)
            if cfg is None:
                logger.warning(
                    "[JiuwenSwarmCodeAdapter] skip invalid member mcp server entry: %s",
                    entry.get("name", "<unknown>"),
                )
                continue
            server_id = str(getattr(cfg, "server_id", "") or getattr(cfg, "server_name", "") or "")
            if not server_id or server_id in configured_ids:
                continue
            configured_mcps.append(cfg)
            configured_ids.add(server_id)
            added += 1
        deep_config.mcps = configured_mcps
        return added

    def configure_team_member_agent(
        self,
        agent: Any,
        *,
        parent_agent: Any | None = None,
        skill_manager: Any | None = None,
        member_name: str | None = None,
        role: str | None = None,
        session_id: str | None = None,
        channel_id: str | None = None,
        project_dir: str | None = None,
        runtime_language: str | None = None,
        force_english_runtime_prompt: bool = True,
    ) -> None:
        """Apply the code runtime profile to a team member DeepAgent."""
        if skill_manager is not None and hasattr(self, "set_skill_manager"):
            self.set_skill_manager(skill_manager)

        normalized_runtime_language = str(runtime_language or "").strip().lower()
        if normalized_runtime_language == "zh":
            normalized_runtime_language = "cn"
        self._runtime_language_override = (
            resolve_language(normalized_runtime_language)
            if normalized_runtime_language
            else None
        )
        self._force_english_runtime_prompt = force_english_runtime_prompt

        config_base = get_config()
        self._refresh_multimodal_configs(config_base)
        react_config = (config_base.get("react") or {}).copy()
        self._config_cache = react_config.copy()
        self._instance = agent

        card = getattr(agent, "card", None)
        agent_id = str(getattr(card, "id", "") or member_name or "team_member")
        self._agent_name = str(getattr(card, "name", "") or member_name or role or "team_member")

        parent_project_dir = (
            project_dir
            or str(getattr(parent_agent, "_jiuwenswarm_code_project_dir", "") or "")
            or _resolve_member_workspace_root(parent_agent)
        )
        member_workspace_root = _resolve_member_workspace_root(agent)
        self._project_dir = parent_project_dir or member_workspace_root or react_config.get("project_dir")
        self._workspace_dir = (
            self._project_dir
            or member_workspace_root
            or react_config.get("workspace_dir")
            or str(get_agent_workspace_dir())
        )
        # Coding memory is application data; keep it out of member/project cwd.
        self._agent_workspace_dir = str(get_agent_workspace_dir())
        self._instance_overrides = {
            "agent_name": self._agent_name,
            "project_dir": self._project_dir,
            "channel_id": channel_id,
        }
        initial_workspace = self._project_dir or self._workspace_dir
        self._seed_runtime_cwd(initial_workspace, workspace=initial_workspace)

        model = self._create_model(config_base)
        deep_config = getattr(agent, "deep_config", None)
        if deep_config is not None and getattr(deep_config, "model", None) is None:
            deep_config.model = model
        if deep_config is not None:
            # Keep ``self._sys_operation`` in sync with the member agent's config:
            # _build_configured_subagents() below hands it to every subagent spec so
            # the subagents stay inside the member's filesystem boundary.
            inherited_sys_operation = getattr(deep_config, "sys_operation", None)
            if inherited_sys_operation is None:
                inherited_sys_operation = self._create_sys_operation()
                deep_config.sys_operation = inherited_sys_operation
            self._sys_operation = inherited_sys_operation
        tool_cards = self.build_code_tool_cards(agent_id)
        added_tools = _merge_tool_cards(agent, tool_cards)

        _set_coding_memory_directory(
            agent,
            initial_workspace,
            self._agent_workspace_dir,
        )

        rails = self._build_agent_rails(
            react_config,
            config_base,
            mode="code",
            composition_scope="team_member",
        )
        added_rails = sum(1 for rail in rails if _queue_rail_if_missing(agent, rail))

        subagents, _should_add_general = self._build_configured_subagents(model, react_config, config_base)
        added_subagents = _merge_subagents(agent, subagents)
        added_mcps = self.merge_member_mcp_configs(agent, config_base)
        if getattr(deep_config, "subagents", None):
            subagent_rail = self._build_subagent_rail()
            if _queue_rail_if_missing(agent, subagent_rail):
                added_rails += 1

        setattr(agent, "_jiuwenswarm_adapter_mode", "code")
        setattr(agent, "_jiuwenswarm_code_project_dir", self._project_dir or self._workspace_dir)
        setattr(agent, "_jiuwenswarm_project_dir", self._project_dir or self._workspace_dir)
        setattr(agent, "_jiuwenswarm_code_team_member", True)

        logger.info(
            "[JiuwenSwarmCodeAdapter] configured team member as code profile: "
            "member=%s role=%s session=%s channel=%s tools=%d rails=%d subagents=%d mcps=%d project_dir=%s",
            member_name,
            role,
            session_id,
            channel_id,
            added_tools,
            added_rails,
            added_subagents,
            added_mcps,
            self._project_dir,
        )


def _tool_card_identity(card: Any) -> tuple[str, str]:
    return (
        str(getattr(card, "id", "") or ""),
        str(getattr(card, "name", "") or ""),
    )


def _subagent_name(spec: Any) -> str:
    if isinstance(spec, SubAgentConfig):
        return str(getattr(spec.agent_card, "name", "") or "")
    card = getattr(spec, "card", None)
    return str(getattr(card, "name", "") or "")


def _iter_agent_rails(agent: Any) -> list[Any]:
    rails: list[Any] = []
    for attr_name in ("_pending_rails", "_registered_rails"):
        value = getattr(agent, attr_name, None)
        if isinstance(value, list):
            rails.extend(value)
    return rails


def _agent_has_rail_type(agent: Any, rail: Any) -> bool:
    return any(isinstance(existing, type(rail)) for existing in _iter_agent_rails(agent))


def _queue_rail_if_missing(agent: Any, rail: Any) -> bool:
    add_rail = getattr(agent, "add_rail", None)
    if rail is None or not callable(add_rail) or _agent_has_rail_type(agent, rail):
        return False
    add_rail(rail)
    return True


def _merge_tool_cards(agent: Any, tool_cards: list[Any]) -> int:
    ability_manager = getattr(agent, "ability_manager", None)
    add_ability = getattr(ability_manager, "add", None)
    list_abilities = getattr(ability_manager, "list", None)

    existing_keys: set[tuple[str, str]] = set()
    if callable(list_abilities):
        existing_keys = {
            _tool_card_identity(card)
            for card in (list_abilities() or [])
        }

    added = 0
    for card in tool_cards:
        key = _tool_card_identity(card)
        if key in existing_keys:
            continue
        if callable(add_ability):
            add_ability(card)
        existing_keys.add(key)
        added += 1

    deep_config = getattr(agent, "deep_config", None)
    if deep_config is not None:
        configured_tools = list(getattr(deep_config, "tools", None) or [])
        configured_keys = {_tool_card_identity(card) for card in configured_tools}
        for card in tool_cards:
            key = _tool_card_identity(card)
            if key not in configured_keys:
                configured_tools.append(card)
                configured_keys.add(key)
        deep_config.tools = configured_tools
    return added


def _merge_subagents(agent: Any, subagents: list[Any] | None) -> int:
    if not subagents:
        return 0

    deep_config = getattr(agent, "deep_config", None)
    if deep_config is None:
        return 0

    configured_subagents = list(getattr(deep_config, "subagents", None) or [])
    configured_names = {_subagent_name(spec) for spec in configured_subagents}
    added = 0
    for spec in subagents:
        name = _subagent_name(spec)
        if not name or name in configured_names:
            continue
        configured_subagents.append(spec)
        configured_names.add(name)
        added += 1
    deep_config.subagents = configured_subagents
    return added


def _resolve_member_workspace_root(agent: Any) -> str | None:
    deep_config = getattr(agent, "deep_config", None)
    workspace = getattr(deep_config, "workspace", None)
    root_path = getattr(workspace, "root_path", None)
    if root_path:
        return str(root_path)
    return None


def _set_coding_memory_directory(
    agent: Any,
    project_dir: str | None,
    agent_workspace_dir: str,
) -> None:
    deep_config = getattr(agent, "deep_config", None)
    workspace = getattr(deep_config, "workspace", None)
    _set_workspace_coding_memory_directory(
        workspace,
        project_dir=project_dir,
        agent_workspace_dir=agent_workspace_dir,
        description="Coding Agent memory",
    )


def configure_code_team_member_agent(
    agent: Any,
    *,
    parent_agent: Any | None = None,
    skill_manager: Any | None = None,
    member_name: str | None = None,
    role: str | None = None,
    session_id: str | None = None,
    channel_id: str | None = None,
    project_dir: str | None = None,
    runtime_language: str | None = None,
    force_english_runtime_prompt: bool = True,
) -> None:
    """Apply JiuwenSwarmCodeAdapter's runtime profile to a team member DeepAgent."""

    adapter = JiuwenSwarmCodeAdapter()
    adapter.configure_team_member_agent(
        agent,
        parent_agent=parent_agent,
        skill_manager=skill_manager,
        member_name=member_name,
        role=role,
        session_id=session_id,
        channel_id=channel_id,
        project_dir=project_dir,
        runtime_language=runtime_language,
        force_english_runtime_prompt=force_english_runtime_prompt,
    )
