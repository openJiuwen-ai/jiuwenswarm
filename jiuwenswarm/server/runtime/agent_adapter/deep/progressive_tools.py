"""ProgressiveToolRail 装配与 TTSE consult 首轮 eager 门控。

职责：
- 从 ``react.tool_lazy_load`` 构建 ProgressiveToolRail（main / subagent profile）
- 归一化 eager_tools（legacy 别名、tools_search/invoke_tool 保底）
- 与 TTSE 开关对齐：启用时把 ``ttse_consult`` 放进首轮可见工具，关闭时剔除

不含 adapter 实例状态；``interface_deep`` 按原名 re-export，UT 仍可从门面导入。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from openjiuwen.harness.prompts import resolve_language

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.common.config import (
    _get_ttse_config,
    coerce_config_bool,
    get_config,
    get_ttse_enabled,
    resolve_string_or_list_config,
)

logger = logging.getLogger(__name__)

_DEFAULT_PROGRESSIVE_EAGER_TOOLS = [
    "tools_search",
    "invoke_tool",
    "web_search",
    "fetch_webpage",
    "web_image_search",
    "ask_user",
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "skill_tool",
    "skill_complete",
    "todo_create",
    "todo_list",
    "todo_modify",
    # plan 模式两件套必须常驻（BUG20261105402443）：plan 态 reminder 指示模型调
    # enter_plan_mode，被懒加载剔除后模型探测无进展会触发 agent_ras 熔断 OA.05000060
    "enter_plan_mode",
    "exit_plan_mode",
]

_PROGRESSIVE_META_TOOL_NAMES = frozenset({"tools_search", "invoke_tool"})
_TTSE_CONSULT_TOOL_NAME = "ttse_consult"
_LEGACY_PROGRESSIVE_EAGER_TOOL_ALIASES = {
    "ask_user_question": "ask_user",
}


def _normalize_tool_names(value: Any, default: list[str] | None = None) -> list[str]:
    """Normalize comma-separated/list tool-name config values."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default or [])


def _ensure_progressive_meta_tools(eager_tools: list[str]) -> list[str]:
    """Ensure tools_search / invoke_tool are in the eager list."""
    if "tools_search" not in eager_tools:
        eager_tools.insert(0, "tools_search")
    if "invoke_tool" not in eager_tools:
        eager_tools.insert(1, "invoke_tool")
    return eager_tools


def _merge_ttse_config(runtime_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Yaml ``react.ttse`` plus runtime overlay (runtime keys win).

    Same merge used by TTSERail mount and ``ttse_consult`` eager gating so a
    sparse runtime cache (OfficeAce sync snapshot omitting ``ttse``) still
    inherits the on-disk default. An explicit runtime ``enabled: false`` wins.
    """
    merged: dict[str, Any] = {}
    try:
        yaml_ttse = _get_ttse_config(get_config())
    except Exception:
        yaml_ttse = {}
    if isinstance(yaml_ttse, dict):
        merged.update(yaml_ttse)
    runtime = _get_ttse_config(runtime_config)
    if isinstance(runtime, dict):
        merged.update(runtime)
    return merged


def _ttse_consult_should_be_eager(react_config: dict[str, Any] | None) -> bool:
    """True when TTSE is opted in and inject is on, so ``ttse_consult`` stays in schema.

    Master switch uses :func:`_merge_ttse_config` then :func:`get_ttse_enabled`
    so eager gating cannot diverge from TTSERail mount.
    """
    if not isinstance(react_config, dict):
        return False
    merged = _merge_ttse_config(react_config)
    if not get_ttse_enabled({"ttse": merged}):
        return False
    return coerce_config_bool(merged.get("inject_enabled"), True)


def _ensure_ttse_consult_eager_tool(
    eager_tools: list[str],
    react_config: dict[str, Any] | None,
) -> list[str]:
    """Expose ``ttse_consult`` on first turn only after TTSE is explicitly enabled.

    ProgressiveToolRail only exposes ``eager_tools``. After ``react.ttse.enabled``
    (and inject) is on, P:45 tells the model to call ``ttse_consult`` before
    ``skill_acceleration_exec``; if the name is filtered out, first-turn consult
    is impossible without tools_search. Default-off must strip the name even if
    a yaml eager list still includes it.
    """
    if not _ttse_consult_should_be_eager(react_config):
        return [name for name in eager_tools if name != _TTSE_CONSULT_TOOL_NAME]
    if _TTSE_CONSULT_TOOL_NAME in eager_tools:
        return eager_tools
    if "skill_acceleration_exec" in eager_tools:
        eager_tools.insert(
            eager_tools.index("skill_acceleration_exec"),
            _TTSE_CONSULT_TOOL_NAME,
        )
    else:
        insert_at = 2 if len(eager_tools) >= 2 else len(eager_tools)
        eager_tools.insert(insert_at, _TTSE_CONSULT_TOOL_NAME)
    return eager_tools


def _normalize_progressive_eager_tools(
    value: Any,
    default: list[str] | None = None,
) -> list[str]:
    """Normalize eager tools and migrate legacy registered names."""
    eager_tools: list[str] = []
    for tool_name in _normalize_tool_names(value, default):
        normalized_name = _LEGACY_PROGRESSIVE_EAGER_TOOL_ALIASES.get(
            tool_name,
            tool_name,
        )
        if normalized_name not in eager_tools:
            eager_tools.append(normalized_name)
    return _ensure_progressive_meta_tools(eager_tools)


def is_subagent_tool_lazy_load_enabled(react_config: dict[str, Any] | None) -> bool:
    """True when react.tool_lazy_load and subagents are both enabled."""
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        return False
    if not lazy_cfg.get("enabled", False):
        return False
    sub_cfg = lazy_cfg.get("subagents") or {}
    if not isinstance(sub_cfg, dict):
        return False
    return bool(sub_cfg.get("enabled", False))


def build_progressive_tool_rail_from_config(
    react_config: dict[str, Any],
    *,
    language: str,
    profile: str = "main",
    agent_id: str | None = None,
    agent_card_id: str | None = None,
    subagent_kind: str | None = None,
    deepresearch_context_provider: Callable[[], dict[str, str]] | None = None,
    active_skill_provider: Callable[[], str | None] | None = None,
) -> ProgressiveToolRail | None:
    """Build ProgressiveToolRail from react.tool_lazy_load config.

    Stable base eager-tools schema; deferred tools are reached via tools_search
    + invoke_tool, while ToolCard skill gates may add direct tools for the
    active session skill. For subagents, set profile="subagent" and configure
    react.tool_lazy_load.subagents.
    """
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        lazy_cfg = {}
    lazy_load_enabled = bool(lazy_cfg.get("enabled", False))

    enable_for_models = _normalize_tool_names(
        lazy_cfg.get("enable_for_models", []), []
    )

    normalized_profile = (profile or "main").strip().lower()
    if normalized_profile == "subagent":
        sub_cfg = lazy_cfg.get("subagents") or {}
        if not isinstance(sub_cfg, dict):
            sub_cfg = {}
        if not sub_cfg.get("enabled", False):
            return None

        main_eager = _normalize_progressive_eager_tools(
            lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
            _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
        )
        if sub_cfg.get("inherit_parent_eager_tools", False):
            eager_tools = list(main_eager)
        else:
            eager_tools = _normalize_progressive_eager_tools(
                sub_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
                _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
            )
    else:
        eager_tools = _normalize_progressive_eager_tools(
            lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
            _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
        )

    eager_tools = _ensure_ttse_consult_eager_tool(eager_tools, config)

    normalized_language = resolve_language(language)
    disabled_tools: list[str] = []
    try:
        disabled_tools = resolve_string_or_list_config(config.get("disabled_tools"))
    except Exception as exc:
        logger.warning(
            "[ProgressiveToolRail] ignore malformed disabled_tools while building "
            "progressive rail: %s",
            exc,
        )

    logger.info(
        "[ProgressiveToolRail] mounted lazy_load_enabled=%s profile=%s "
        "kind=%s eager_tools=%s "
        "agent_id=%s agent_card_id=%s enable_for_models=%s disabled_tools=%s",
        lazy_load_enabled,
        normalized_profile,
        subagent_kind or "",
        eager_tools,
        agent_id,
        agent_card_id,
        enable_for_models,
        disabled_tools,
    )

    return ProgressiveToolRail(
        enabled=lazy_load_enabled,
        eager_tools=eager_tools,
        language=normalized_language,
        agent_id=agent_id,
        agent_card_id=agent_card_id,
        enable_for_models=enable_for_models,
        deepresearch_context_provider=deepresearch_context_provider,
        active_skill_provider=active_skill_provider,
        disabled_tools=disabled_tools,
    )
