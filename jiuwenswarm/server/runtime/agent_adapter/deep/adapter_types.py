"""adapter 装配用轻量类型、常量与 MCP 缓冲结构。

职责：
- GitSnapshot / RailBuildInfo 数据类
- ACP / skill retrieval / mode 显示 / deepresearch rewrite 常量
- request-scoped MCP 缓冲与 builtin 短名冲突异常

无运行时副作用，只承载结构定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.common.mcp_config import RequestScopedOfficeClawMcpTool

_ACP_BLOCKED_DEFAULT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "bash",
    }
)

_SKILL_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "skill_index_build",
        "skill_branch_explore",
        "skill_branch_peek",
    }
)


@dataclass(frozen=True)
class _GitSnapshot:
    """Git state as of a conversation's first turn, held for its whole life.

    Attributes:
        head: Raw ``HEAD`` file contents when the snapshot was taken. Checking
            it costs a file read rather than a subprocess, which is what makes
            it affordable to verify on every turn.
        branch: Current branch name, or "HEAD" when detached.
        status: ``git status --short`` output, capped at 50 lines.
        recent_commits: ``git log --oneline -5`` output.
    """

    head: str
    branch: str
    status: str
    recent_commits: str


@dataclass
class _RailBuildInfo:
    """One rail's construction recipe, shared by the agent and code rail sets.

    Attributes:
        attr_name: Adapter attribute the built rail is assigned to. Also names
            the rail in the build-timing breakdown, minus its leading underscore.
        build_func: Callable returning the rail instance, or None to skip it.
        params: Keyword arguments for ``build_func``; empty when omitted.
    """

    attr_name: str
    build_func: Callable
    params: dict = None

    def __post_init__(self):
        """Normalize the optional params mapping to an empty dict."""
        self.params = self.params or {}


_MODE_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "agent": {"cn": "智能体模式", "en": "Agent Mode"},
    # Web 的 work 单 agent 计划模式（mode=agent.plan + work_mode=work）。
    "agent.plan": {"cn": "计划模式", "en": "Plan Mode"},
    # 历史 token 归一到合并后的 agent 显示名（兼容旧会话 / 旧请求）。
    "agent.fast": {"cn": "智能体模式", "en": "Agent Mode"},
    "team": {"cn": "集群模式", "en": "Cluster Mode"},
    "team.plan": {"cn": "集群计划模式", "en": "Cluster Plan Mode"},
    "code.team": {"cn": "代码集群模式", "en": "Code Team Mode"},
}


class OfficeClawMcpBuiltinNameConflict(RuntimeError):
    """A request-scoped connector tool's short name collides with a *built-in*
    agent tool (not a foreign request-scoped tool).

    Unlike a foreign short-name conflict (which must fail closed to prevent a
    concurrent request from stealing the mapping), colliding with a built-in
    tool is benign: the connector tool simply cannot claim that short name, so
    the caller downgrades by skipping just that tool instead of rolling back
    the whole registration. Carries ``existing_id`` for diagnostics.
    """

    def __init__(self, message: str, *, existing_id: str = "") -> None:
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class _RequestMcpToolBuffers:
    """One-request MCP tool-install accumulators (G.FNM.03)."""

    tool_ids: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    registered_tools: list[RequestScopedOfficeClawMcpTool] = field(default_factory=list)
    seen_names: set[str] = field(default_factory=set)
    # Discover every source before mutating the shared resource/ability managers.
    # The bool preserves fail-closed registration for identity-pinned system tools.
    pending_tools: list[tuple[RequestScopedOfficeClawMcpTool, bool]] = field(default_factory=list)
    research_servers: list[dict[str, Any]] = field(default_factory=list, repr=False)
    research_tool_names: dict[str, list[str]] = field(default_factory=dict)
    # server -> {raw tool name -> staged name} for built-in-shadow renames.
    research_name_rewrites: dict[str, dict[str, str]] = field(default_factory=dict)
    unavailable_connectors: dict[str, str] = field(default_factory=dict)

# shutdown_context_session_memory 用哨兵区分"属性缺失（框架改名/异型
# rail）"与"属性合法为 None（未配置或已关闭，框架初始即 None，仅在
# session_memory_compressor_config.enabled=true 时创建）"——两者日志
# 级别不同，getattr 默认值无法表达该区分（CR-5）。
_MISSING_SESSION_MEMORY_MGR = object()

_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY = "deepresearch_rewrite_fast_path_replays"
_DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION = 1
_DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES = 32
_DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH = 256
_DEEPRESEARCH_REWRITE_REPLAY_MAX_BYTES = 256 * 1024
_DEEPRESEARCH_REWRITE_REPLAY_PATH_MAX_BYTES = 4096
_DEEPRESEARCH_REWRITE_REPLAY_TERMINAL_KINDS = frozenset(
    {"success", "report_delivery_failed", "publish_uncertain"}
)
_DEEPRESEARCH_REWRITE_SUCCESS_MESSAGE = (
    "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
    "如需继续改写，可直接选择下一处内容。"
)
_DEEPRESEARCH_REWRITE_DELIVERY_FAILURE_MESSAGE = (
    "改写版本已成功保留，但报告文件交付失败。"
)
_DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE = (
    "改写版本状态不确定，请刷新会话后重试。"
)
_DEEPRESEARCH_CHECKPOINT_UNCERTAIN = "DEEPRESEARCH_CHECKPOINT_UNCERTAIN"
_DEEPRESEARCH_CHECKPOINT_UNCERTAIN_MESSAGE = (
    "会话检查点状态不确定，请重试以重新加载会话。"
)
