# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-sourced swarm-owned tool providers for team assembly.

Only swarm-owned tools are declared here as individual ``swarm.*`` tool
elements, each self-gated by the config source and filtered against the swarm
``TOOL_WHITELIST``:

* ``swarm.skill_toolkit`` — skill discovery / install / uninstall tools.
* ``swarm.skill_retrieval`` — agentic installed skill tree retrieval tools.
* ``swarm.user_todos`` — the personal todo tool.
* ``swarm.video`` — the video-understanding tool (``models.video`` gated).
* ``swarm.image_gen`` — the image-generation tool (``IMAGE_GEN_API_KEY`` gated).
* ``swarm.xiaoyi_phone`` — the xiaoyi phone tools (channel-switch gated).
* ``swarm.code_extra_tools`` — code-mode-exclusive ``acp_chat``.

Vision and audio tools are provided by openjiuwen (``core.vision`` /
``core.audio``). Team web tools use Jiuwen's unified search/fetch wrappers;
``vision_model_config_params`` /
``audio_model_config_params`` here fill the openjiuwen ``VisionModelConfig`` /
``AudioModelConfig`` constructor kwargs from the swarm config + env so
``config_specs`` can bake them into those elements' params.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)
from openjiuwen.core.runner import Runner

from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
    JiuwenHarnessWebSearchTool,
)
from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_image_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    dedicated_multimodal_model_configured,
    complete_multimodal_model_configured,
)
from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
    SkillRetrievalToolkit,
    is_skill_retrieval_enabled,
)
from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
from jiuwenswarm.agents.harness.common.tools.user_todo_tool import get_decorated_tools
from jiuwenswarm.agents.harness.common.tools.video_tools import video_understanding
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    add_collection,
    call_phone,
    convert_timestamp_to_utc8_time,
    create_alarm,
    create_calendar_event,
    create_note,
    delete_alarm,
    delete_collection,
    get_user_location,
    image_reading,
    modify_alarm,
    modify_note,
    query_collection,
    save_file_to_file_manager,
    save_media_to_gallery,
    search_alarms,
    search_calendar_event,
    search_contact,
    search_file,
    search_message,
    search_notes,
    search_photo_gallery,
    send_message,
    upload_file,
    upload_photo,
    view_push_result,
    xiaoyi_gui_agent,
)
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import TOOL_WHITELIST
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.local_env_config import get_local_config
from jiuwenswarm.common.tool_ownership import mark_stateless
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
SKILL_TOOLKIT = "swarm.skill_toolkit"
SKILL_RETRIEVAL = "swarm.skill_retrieval"
USER_TODOS = "swarm.user_todos"
VIDEO = "swarm.video"
IMAGE_GEN = "swarm.image_gen"
XIAOYI_PHONE = "swarm.xiaoyi_phone"
SYMPHONY_TOOLKIT = "swarm.symphony_toolkit"
CODE_EXTRA_TOOLS = "swarm.code_extra_tools"
WEB_SEARCH = "swarm.web_search"
WEB_FETCH = "swarm.web_fetch"
_CODE_MODES = frozenset({"code.team", "team.plan"})

# xiaoyi phone tool objects, gated by ``channels.xiaoyi.phone_tools_enabled``.
_XIAOYI_PHONE_TOOLS = (
    get_user_location,
    create_note,
    search_notes,
    modify_note,
    create_calendar_event,
    search_calendar_event,
    search_contact,
    search_photo_gallery,
    upload_photo,
    search_file,
    upload_file,
    call_phone,
    send_message,
    search_message,
    create_alarm,
    search_alarms,
    modify_alarm,
    delete_alarm,
    query_collection,
    add_collection,
    delete_collection,
    save_media_to_gallery,
    save_file_to_file_manager,
    convert_timestamp_to_utc8_time,
    view_push_result,
    image_reading,
    xiaoyi_gui_agent,
)


def _filter_whitelist(tools: list[Any]) -> list[Any]:
    """Keep only tools whose ``card.name`` is in the swarm ``TOOL_WHITELIST``."""
    return [tool for tool in tools if tool.card.name in TOOL_WHITELIST]


def _reuse_registered_tool(candidate: Any) -> Any:
    existing = Runner.resource_mgr.get_tool(candidate.card.id)
    if existing is None:
        return candidate
    if type(existing) is not type(candidate) or existing.card.name != candidate.card.name:
        raise ValueError(
            "Registered tool is incompatible with Team web tool: "
            f"tool_id='{candidate.card.id}', tool_name='{candidate.card.name}'"
        )
    return existing


def _member_web_tool_identity(ctx: SwarmBuildContext) -> tuple[str | None, str]:
    agent_id = str(
        getattr(ctx, "member_card_id", None)
        or getattr(ctx, "member_name", None)
        or ""
    ).strip() or None
    language = "en" if str(getattr(ctx, "language", "cn")).lower() == "en" else "cn"
    if agent_id is not None:
        agent_id = f"{agent_id}:{language}"
    return agent_id, language


def _mark_stateless(tools: list[Any]) -> list[Any]:
    """Flag module-level singleton tools as stateless.

    These tools are shared ``@tool`` singletons (no per-agent/session state), so
    they are registered once under their bare id and shared across members
    rather than agent-qualified. Without this flag the harness would treat them
    as agent-owned and rewrite the shared card id per member, corrupting it.
    """
    return mark_stateless(tools)


def _workspace_root(ctx: SwarmBuildContext) -> str | None:
    """Resolve the member workspace root path (None when absent)."""
    return getattr(ctx.workspace, "root_path", None) if ctx.workspace else None


def _scan_skill_names_from_dirs(skill_dirs: list[str], disabled_skills: set[str]) -> set[str]:
    names: set[str] = set()
    for raw_dir in skill_dirs:
        root = Path(raw_dir).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
                continue
            if child.name in disabled_skills:
                continue
            if (child / "SKILL.md").is_file():
                names.add(child.name)
    return names


def _collect_disabled_skills_from_state(skill_dirs: list[str]) -> set[str]:
    disabled: set[str] = set()
    for raw_dir in skill_dirs:
        state_path = Path(raw_dir).expanduser() / "skills_state.json"
        if not state_path.is_file():
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("[swarm.skill_retrieval] failed to read skills state: %s", state_path)
            continue
        skill_configs = data.get("skill_configs", {})
        if not isinstance(skill_configs, dict):
            continue
        for name, cfg in skill_configs.items():
            if isinstance(cfg, dict) and cfg.get("enabled") is False:
                disabled.add(str(name))
    return disabled


def _list_skill_dirs_for_context(ctx: SwarmBuildContext) -> list[str]:
    workspace = getattr(ctx, "workspace", None)
    skill_dirs: list[str] = []
    if workspace is not None:
        get_node_path = getattr(workspace, "get_node_path", None)
        if callable(get_node_path):
            skills_base = get_node_path("skills")
            if skills_base:
                skill_dirs.append(str(skills_base))

        list_team_links = getattr(workspace, "list_team_links", None)
        if callable(list_team_links):
            for _team_id, target_path in list_team_links():
                skill_dirs.append(str(Path(target_path) / "skills"))

    team_skills_dir = getattr(ctx, "team_skills_dir", None)
    if team_skills_dir:
        skill_dirs.append(str(team_skills_dir))
    return list(dict.fromkeys(skill_dirs))


def visible_skill_names_for_list_skill(ctx: SwarmBuildContext) -> set[str]:
    """Return the skill names that the matching SkillUseRail would expose."""
    if ctx.mode in _CODE_MODES:
        from jiuwenswarm.common.utils import resolve_agent_registered_skill_dirs
        from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

        # Note: the adapter's _resolve_skill_dirs has a whitelist-tenant branch
        # (scan only the tenant workspace), but SwarmBuildContext carries no
        # agent_id/service_id, so this provider-side scan cannot apply it.
        # Acceptable drift: whitelist tenants are enterprise-only and do not
        # build swarm members through this path today.
        if ctx.shared_skills_dirs:
            skill_dirs = list(ctx.shared_skills_dirs)
        else:
            skill_dirs = [str(p) for p in resolve_agent_registered_skill_dirs()]
        return _scan_skill_names_from_dirs(skill_dirs, set(load_execution_disabled_skills()))

    skill_dirs = _list_skill_dirs_for_context(ctx)
    disabled_skills = _collect_disabled_skills_from_state(skill_dirs)
    return _scan_skill_names_from_dirs(skill_dirs, disabled_skills)


def _parse_int(value: Any, default: int) -> int:
    """Parse an int from a possibly-None/str value, falling back to ``default``."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# openjiuwen vision / audio config filling. The tool elements live in
# openjiuwen (``core.vision`` / ``core.audio``); these helpers fill the
# openjiuwen config-constructor kwargs from the swarm config + env so
# ``config_specs`` can bake them into the elements' params.
# ---------------------------------------------------------------------------



def _tip_getenv(name: str, default: str = "") -> str:
    """Read Track-B tip (overlay/active); never bare os.environ for business keys."""
    value = get_local_config(name, default if default else None)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def vision_model_config_params(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``VisionModelConfig`` constructor kwargs, or {} when unconfigured.

    Requires a dedicated ``models.vision`` key and a complete ``VISION_*`` env
    mapping after applying the yaml config (mirrors the single-agent build).
    """
    if not dedicated_multimodal_model_configured(config, "vision"):
        return {}
    apply_vision_model_config_from_yaml(config)
    api_key = str(_tip_getenv("VISION_API_KEY")).strip()
    base_url = str(
        _tip_getenv("VISION_BASE_URL") or _tip_getenv("VISION_API_BASE") or ""
    ).strip()
    model_name = str(
        _tip_getenv("VISION_MODEL") or _tip_getenv("VISION_MODEL_NAME") or ""
    ).strip()
    if not api_key or not base_url or not model_name:
        return {}
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model_name,
        "max_retries": _parse_int(_tip_getenv("VISION_MAX_RETRIES"), 3),
    }


def audio_dedicated_configured(config: dict[str, Any]) -> bool:
    """Return whether a complete dedicated audio model is configured."""
    return complete_multimodal_model_configured(config, "audio")


def audio_model_config_params(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``AudioModelConfig`` constructor kwargs, or {} when incomplete."""
    if not complete_multimodal_model_configured(config, "audio"):
        return {}
    apply_audio_model_config_from_yaml(config)
    api_key = str(_tip_getenv("AUDIO_API_KEY")).strip()
    base_url = str(
        _tip_getenv("AUDIO_BASE_URL") or _tip_getenv("AUDIO_API_BASE") or ""
    ).strip()
    if not api_key or not base_url:
        return {}
    transcription_model = str(
        _tip_getenv("AUDIO_TRANSCRIPTION_MODEL") or _tip_getenv("AUDIO_MODEL_NAME") or "",
    ).strip()
    question_answering_model = str(
        _tip_getenv("AUDIO_QUESTION_ANSWERING_MODEL")
        or _tip_getenv("AUDIO_MODEL_NAME")
        or "",
    ).strip()
    config_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
        "max_retries": _parse_int(_tip_getenv("AUDIO_MAX_RETRIES"), 3),
        "http_timeout": _parse_int(_tip_getenv("AUDIO_HTTP_TIMEOUT"), 20),
        "max_audio_bytes": _parse_int(
            _tip_getenv("AUDIO_MAX_AUDIO_BYTES"), 25 * 1024 * 1024
        ),
    }
    acr_access_key = str(_tip_getenv("ACR_ACCESS_KEY")).strip()
    acr_access_secret = str(_tip_getenv("ACR_ACCESS_SECRET")).strip()
    acr_base_url = str(_tip_getenv("ACR_BASE_URL")).strip()
    if acr_access_key:
        config_kwargs["acr_access_key"] = acr_access_key
    if acr_access_secret:
        config_kwargs["acr_access_secret"] = acr_access_secret
    if acr_base_url:
        config_kwargs["acr_base_url"] = acr_base_url
    if transcription_model:
        config_kwargs["transcription_model"] = transcription_model
    if question_answering_model:
        config_kwargs["question_answering_model"] = question_answering_model
    return config_kwargs


# ---------------------------------------------------------------------------
# Swarm-owned tool builders (unfiltered) + provider elements (whitelist-filtered).
# ---------------------------------------------------------------------------


def _build_skill_toolkit_tools(workspace_root: str | None) -> list[Any]:
    """Build the skill-management tools bound to the member workspace."""
    try:
        manager = SkillManager(workspace_dir=workspace_root)
        toolkit = SkillToolkit(manager=manager)
        return list(toolkit.get_tools())
    except Exception as exc:
        logger.warning("[swarm.skill_toolkit] construction failed: %s", exc)
        return []


VisibleSkillNamesProvider = Callable[[], set[str] | frozenset[str] | None]


def _build_skill_retrieval_tools(
    visible_skill_names: set[str] | frozenset[str] | VisibleSkillNamesProvider | None = None,
) -> list[Any]:
    """Build installed-skill retrieval tools against the global installed skill root."""
    if not is_skill_retrieval_enabled():
        logger.info("[swarm.skill_retrieval] skipped: disabled")
        return []
    try:
        manager = SkillManager()
        if visible_skill_names is None:
            toolkit = SkillRetrievalToolkit(manager=manager)
        else:
            toolkit = SkillRetrievalToolkit(manager=manager, visible_skill_names=visible_skill_names)
        return list(toolkit.get_tools())
    except Exception as exc:
        logger.warning("[swarm.skill_retrieval] construction failed: %s", exc)
        return []


def _build_user_todo_tools() -> list[Any]:
    """Build the user's personal todo tool."""
    try:
        return _mark_stateless(list(get_decorated_tools()))
    except Exception as exc:
        logger.warning("[swarm.user_todos] construction failed: %s", exc)
        return []


def _build_video_tools(ctx: SwarmBuildContext) -> list[Any]:
    """Build the video understanding tool when ``models.video`` is complete."""
    config = ctx.config or {}
    apply_video_model_config_from_yaml(config)
    if not complete_multimodal_model_configured(config, "video"):
        return []
    video_api_key = str(_tip_getenv("VIDEO_API_KEY")).strip()
    video_api_base = str(_tip_getenv("VIDEO_API_BASE")).strip()
    video_model_name = str(_tip_getenv("VIDEO_MODEL_NAME")).strip()
    if not video_api_key or not video_api_base or not video_model_name:
        return []
    return _mark_stateless([video_understanding])


def _build_image_gen_tools(ctx: SwarmBuildContext) -> list[Any]:
    """Build the image-generation tool when ``models.image_gen`` is configured."""
    apply_image_gen_model_config_from_yaml(ctx.config or {})
    if not _tip_getenv("IMAGE_GEN_API_KEY"):
        return []
    return _mark_stateless([generate_image])


def _build_xiaoyi_phone_tools(ctx: SwarmBuildContext) -> list[Any]:
    """Build xiaoyi phone tools when ``channels.xiaoyi.phone_tools_enabled``."""
    config = ctx.config or {}
    enabled = (
        config.get("channels", {}).get("xiaoyi", {}).get("phone_tools_enabled", False)
    )
    if not enabled:
        return []
    return _mark_stateless(list(_XIAOYI_PHONE_TOOLS))


def _build_symphony_tools(ctx: SwarmBuildContext) -> list[Any]:
    """Build Symphony tools for the team leader."""
    if getattr(ctx, "role", "") != "leader":
        return []
    try:
        config = ctx.config or get_config()
        return list(SymphonyToolkit().get_tools(config))
    except Exception as exc:
        logger.warning("[swarm.symphony_toolkit] construction failed: %s", exc)
        return []


class SkillToolkitInput(ConstructionInput):
    """Construction inputs for the skill-toolkit tool."""

    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root; the SkillManager resolves skills from it.",
    )


class SkillRetrievalInput(ConstructionInput):
    """Construction inputs for global installed-skill retrieval tools."""

    global_skills_dir: str | None = context_field(
        attr="global_skills_dir",
        description="Global installed skills source directory.",
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=WEB_SEARCH,
    description="Jiuwen unified web search with paid-provider chain and free fallback.",
)
def build_jiuwen_web_search(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    agent_id, language = _member_web_tool_identity(ctx)
    from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
        get_agent_cache_registry,
    )

    content_cache = get_agent_cache_registry().get_cache_sync(agent_id or "default")
    tool = JiuwenHarnessWebSearchTool(language=language, agent_id=agent_id, cache=content_cache)
    return [_reuse_registered_tool(tool)]


@harness_element(
    kind=ElementKind.TOOL,
    name=WEB_FETCH,
    description="Jiuwen webpage fetch tool.",
)
def build_jiuwen_web_fetch(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    agent_id, language = _member_web_tool_identity(ctx)
    from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
        get_agent_cache_registry,
    )

    content_cache = get_agent_cache_registry().get_cache_sync(agent_id or "default")
    tool = JiuwenHarnessFetchWebpageTool(language=language, agent_id=agent_id, cache=content_cache)
    return [_reuse_registered_tool(tool)]


@harness_element(
    kind=ElementKind.TOOL,
    name=SKILL_TOOLKIT,
    description="Skill discovery / install / uninstall tools bound to the member workspace.",
    input_model=SkillToolkitInput,
)
def build_skill_toolkit(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered skill toolkit tools."""
    inp = SkillToolkitInput.resolve(params, ctx)
    return _filter_whitelist(_build_skill_toolkit_tools(inp.workspace_root))


@harness_element(
    kind=ElementKind.TOOL,
    name=SKILL_RETRIEVAL,
    description="Agentic installed skill tree retrieval tools for globally installed skills.",
    input_model=SkillRetrievalInput,
)
def build_skill_retrieval(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered installed-skill retrieval tools."""
    SkillRetrievalInput.resolve(params, ctx)
    return _filter_whitelist(
        _build_skill_retrieval_tools(lambda: visible_skill_names_for_list_skill(ctx))
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=USER_TODOS,
    description="The user's personal todo tool.",
)
def build_user_todos(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered user todo tools."""
    return _filter_whitelist(_build_user_todo_tools())


@harness_element(
    kind=ElementKind.TOOL,
    name=VIDEO,
    description="Video-understanding tool (built only when models.video is configured).",
)
def build_video_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered video tools."""
    return _filter_whitelist(_build_video_tools(ctx))


@harness_element(
    kind=ElementKind.TOOL,
    name=IMAGE_GEN,
    description="Image-generation tool (built only when IMAGE_GEN_API_KEY is set).",
)
def build_image_gen_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered image-generation tools."""
    return _filter_whitelist(_build_image_gen_tools(ctx))


@harness_element(
    kind=ElementKind.TOOL,
    name=XIAOYI_PHONE,
    description="xiaoyi phone tools (built only when the channel switch is on).",
)
def build_xiaoyi_phone(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the whitelist-filtered xiaoyi phone tools."""
    return _filter_whitelist(_build_xiaoyi_phone_tools(ctx))


@harness_element(
    kind=ElementKind.TOOL,
    name=SYMPHONY_TOOLKIT,
    description="Symphony planning tools (leader only, gated by symphony.enabled).",
)
def build_symphony_toolkit(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build Symphony tools for the leader; teammates get no tools."""
    return _build_symphony_tools(ctx)


class CodeExtraToolsInput(ConstructionInput):
    """Construction inputs for the code-extra tools."""

    acp_enabled: bool = param_field(
        default=False,
        description="Whether acp_agents is configured (gates acp_chat).",
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=CODE_EXTRA_TOOLS,
    description="Code-mode-exclusive tools (currently acp_chat).",
    input_model=CodeExtraToolsInput,
)
def build_code_extra_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build code-mode-exclusive tools (currently ``acp_chat``) from the config source."""
    inp = CodeExtraToolsInput.resolve(params, ctx)
    if not inp.acp_enabled:
        return []
    try:
        from jiuwenswarm.agents.harness.common.tools.acp_chat import acp_chat

        return [acp_chat]
    except Exception as exc:
        logger.warning("[swarm.code_extra_tools] acp_chat construction failed: %s", exc)
        return []


__all__ = [
    "SKILL_TOOLKIT",
    "SKILL_RETRIEVAL",
    "USER_TODOS",
    "VIDEO",
    "IMAGE_GEN",
    "XIAOYI_PHONE",
    "SYMPHONY_TOOLKIT",
    "CODE_EXTRA_TOOLS",
    "WEB_SEARCH",
    "WEB_FETCH",
    "vision_model_config_params",
    "audio_dedicated_configured",
    "audio_model_config_params",
    "build_symphony_toolkit",
    "build_code_extra_tools",
    "build_jiuwen_web_search",
    "build_jiuwen_web_fetch",
]
