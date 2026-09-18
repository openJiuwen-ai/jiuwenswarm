# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pytest configuration for team resilience tests.

Pre-mocks missing dependencies from the installed openjiuwen package
so that test modules can be collected without ImportError.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Pre-flight: patch missing symbols in installed openjiuwen before any
# jiuwenswarm.agents.harness.team import triggers the chain.
# ---------------------------------------------------------------------------

# 1. openjiuwen.core.common.logging may lack ``server_logger`` in the
#    installed wheel.  Inject a no-op logger so team_manager.py imports.
import openjiuwen.core.common.logging as _oj_logging

if not hasattr(_oj_logging, "server_logger"):
    _oj_logging.server_logger = logging.getLogger("openjiuwen.server")

# 2. Some modules import Runner at module level; ensure it's importable.
try:
    from openjiuwen.core.runner import Runner  # noqa: F401
except Exception:
    # Create a minimal stub so module-level imports don't crash.
    _runner_mod = MagicMock()
    _runner_mod.Runner = MagicMock()
    sys.modules.setdefault("openjiuwen.core.runner", _runner_mod)
    sys.modules.setdefault("openjiuwen.core.runner.runner", _runner_mod)

# 3. openjiuwen.harness may lack DeepAgent or rails in the installed wheel.
try:
    from openjiuwen.harness import DeepAgent  # noqa: F401
except Exception:
    _harness_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.harness", _harness_mod)

# 4. Ensure openjiuwen.harness.rails sub-modules are importable.
for _rail_name in [
    "SysOperationRail", "HeartbeatRail", "SecurityRail",
    "EvolutionInterruptRail", "SkillEvolutionRail",
    "TaskPlanningRail", "TeamSkillEvolutionRail", "TeamSkillCreateRail",
]:
    pass  # These are imported via the real package; only needed if missing.

try:
    from openjiuwen.harness.rails import SkillEvolutionRail  # noqa: F401
except Exception:
    _rails_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.harness.rails", _rails_mod)

try:
    from openjiuwen.harness.rails.evolution import EvolutionReviewRuntime  # noqa: F401
except Exception:
    _evo_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.harness.rails.evolution", _evo_mod)

try:
    from openjiuwen.harness.rails.context_engineer import ContextProcessorRail  # noqa: F401
except Exception:
    _ctx_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.harness.rails.context_engineer", _ctx_mod)

# 5. openjiuwen.core.foundation.tool.ToolCard
try:
    from openjiuwen.core.foundation.tool import ToolCard  # noqa: F401
except Exception:
    _tool_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.core.foundation.tool", _tool_mod)

# 6. openjiuwen.core.foundation.llm
try:
    from openjiuwen.core.foundation.llm import Model  # noqa: F401
except Exception:
    _llm_mod = MagicMock()
    sys.modules.setdefault("openjiuwen.core.foundation.llm", _llm_mod)

# 7. openjiuwen.agent_teams sub-modules
for _sub in [
    "openjiuwen.agent_teams.monitor",
    "openjiuwen.agent_teams.monitor.models",
    "openjiuwen.agent_teams.context",
    "openjiuwen.agent_teams.paths",
    "openjiuwen.agent_teams.agent.team_agent",
    "openjiuwen.agent_teams.runtime.pool",
    "openjiuwen.agent_teams.schema.blueprint",
    "openjiuwen.agent_teams.spawn.shared_resources",
    "openjiuwen.agent_teams.tools.database.config",
    "openjiuwen.agent_teams.observability",
]:
    try:
        __import__(_sub)
    except Exception:
        sys.modules.setdefault(_sub, MagicMock())

# 8. jiuwenswarm internal modules that may fail during import chain
for _sub in [
    "jiuwenswarm.agents.harness.team.event_types",
    "jiuwenswarm.agents.harness.team.handlers.base_monitor_handler",
    "jiuwenswarm.agents.harness.team.bootstrap",
    "jiuwenswarm.agents.harness.team.config_loader",
    "jiuwenswarm.agents.harness.team.distributed_runtime",
    "jiuwenswarm.agents.harness.team.kv_cache_hooks",
    "jiuwenswarm.agents.harness.team.remote_member_bootstrap",
    "jiuwenswarm.agents.harness.team.team_skill_links",
    "jiuwenswarm.agents.harness.team.team_runtime_inheritance",
    "jiuwenswarm.agents.harness.team.rails.team_workspace_report_path_rail",
    "jiuwenswarm.agents.harness.common.rails.ask_user_rail",
    "jiuwenswarm.agents.harness.common.rails.avatar_rail",
    "jiuwenswarm.agents.harness.common.rails.response_prompt_rail",
    "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail",
    "jiuwenswarm.agents.harness.common.rails.stream_event_rail",
    "jiuwenswarm.agents.swarm",
    "jiuwenswarm.common.config",
    "jiuwenswarm.common.log_preview",
    "jiuwenswarm.common.utils",
    "jiuwenswarm.common.reasoning_injector",
    "jiuwenswarm.common.openrouter_attribution",
    "jiuwenswarm.server.runtime.session.session_metadata",
    "jiuwenswarm.server.runtime.skill",
    "jiuwenswarm.server.runtime.team_binding_store",
    "jiuwenswarm.server.runtime.team_entity_store",
]:
    try:
        __import__(_sub)
    except Exception:
        sys.modules.setdefault(_sub, MagicMock())
