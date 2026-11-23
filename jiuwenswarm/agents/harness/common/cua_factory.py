"""Mount the external REPL on the existing desktop subagent."""

from __future__ import annotations

from importlib.resources import files
import logging

from openjiuwen.harness.rails.multimodal_context_summarizer_rail import MultimodalContextSummarizerRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.subagents.cua_agent import build_cua_agent_config as build_core_cua_config
from openjiuwen.harness.tools.cua.rails import CuaProgressRail

from jiuwenswarm.common import cua_config

from .prompt.cua_task_prompt import build_cua_repl_prompt

_LOG = logging.getLogger(__name__)


def build_cua_agent_config(
    model,
    *,
    card=None,
    workspace=None,
    sys_operation=None,
    language="cn",
    max_iterations=100,
    cua_snapshot_keep_last_k=3,
    cua_pause_on_user_input=False,
    rails=None,
) -> SubAgentConfig | None:
    """Build a desktop subagent only when a supported backend is installed."""
    repl_available = cua_config.is_cua_repl_available()
    legacy_available = cua_config.is_legacy_cua_available()
    if not repl_available and not legacy_available:
        return None

    spec = build_core_cua_config(
        model,
        card=card,
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        max_iterations=max_iterations,
        cua_snapshot_keep_last_k=cua_snapshot_keep_last_k,
        cua_pause_on_user_input=cua_pause_on_user_input,
        rails=rails,
    )
    if not repl_available:
        return spec

    from .rails.cua_extension_rail import CuaExtensionRail

    try:
        api = files("computer_use").joinpath("__init__.pyi").read_text(encoding="utf-8")
    except (ImportError, OSError, TypeError, UnicodeError) as error:
        _LOG.warning("Computer Use SDK interface is unavailable: %s", error)
        api = None
    prompt = build_cua_repl_prompt(language, legacy_available=legacy_available, api=api)

    spec.rails = list(spec.rails or [])
    if not legacy_available:
        # The core factory always installs legacy tools and their preflight.
        # Materialize a plain DeepAgent with the existing CUA progress reporting.
        spec.factory_name = None
        spec.factory_kwargs = {"enable_sys_operation": False}
        spec.rails.extend([CuaProgressRail(""), MultimodalContextSummarizerRail(cua_snapshot_keep_last_k)])
        spec.system_prompt = prompt
    else:
        spec.system_prompt += f"\n{prompt}"
    spec.rails.append(CuaExtensionRail(pause_on_user_input=cua_pause_on_user_input))
    spec.parallel_tool_calls = False
    return spec
