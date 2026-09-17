# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent/Code debug trace — request-level human-readable dump.

Phase 1: request-level ``/debug`` directive only. Mirrors the Team mode
``TeamStreamLogger`` concept (see ``openjiuwen.agent_teams.monitor``) but
with mode/source fields instead of team member/role, plus explicit run
boundaries. OTel and the ``debug_trace`` config block are deferred to
later phases.
"""

from jiuwenswarm.server.runtime.debug_trace.config import (
    DebugTraceSettings,
    resolve_debug_trace_settings,
)
from jiuwenswarm.server.runtime.debug_trace.context import (
    get_debug_trace_logger,
    reset_debug_trace_logger,
    set_debug_trace_logger,
)
from jiuwenswarm.server.runtime.debug_trace.directives import strip_debug_directive
from jiuwenswarm.server.runtime.debug_trace.paths import (
    debug_trace_dir,
    debug_trace_file,
)
from jiuwenswarm.server.runtime.debug_trace.stream_logger import DebugTraceLogger
from jiuwenswarm.server.runtime.debug_trace.subagent_capture import (
    invoke_subagent_with_trace,
)

__all__ = [
    "DebugTraceLogger",
    "DebugTraceSettings",
    "debug_trace_dir",
    "debug_trace_file",
    "get_debug_trace_logger",
    "invoke_subagent_with_trace",
    "reset_debug_trace_logger",
    "resolve_debug_trace_settings",
    "set_debug_trace_logger",
    "strip_debug_directive",
]
