# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Context diet for Code mode: keep tool output out of the model window.

Two cooperating mechanisms, both configured under ``modes.code.context_diet``:

* **Ingestion trimming** (:mod:`.trim`, applied by
  :class:`~jiuwenswarm.agents.harness.code.rails.context_diet_trim_rail.ContextDietTrimRail`)
  rewrites the *model-facing* text of a tool result right after the tool ran.
  Long shell / grep output keeps its head, tail and "signal" lines (errors,
  failures, tracebacks); the full text is spilled to a file under the user data
  directory and the path is quoted in the message so the model can read it back
  with ``read_file`` / ``grep``. Nothing on disk in the project changes.
* **Mid-turn compaction** (:mod:`.processors`) registers two
  ``ToolResultWindowProcessor`` subclasses with agent-core's context engine.
  Once more than ``keep_last_k`` results of the managed tools sit in the
  window, the oldest ones are replaced by a one-line summary (tool, argument,
  key conclusion) with an ``[[OFFLOAD: ...]]`` handle the model can reload.
  Edit / write results are never touched.

The package deliberately has no dependency on the adapter: the pure helpers in
:mod:`.trim` accept plain strings and dicts so they can be unit-tested and
reused by the headless driver or offline metrics tooling.
"""

from jiuwenswarm.agents.harness.code.context_diet.config import (
    CompactionConfig,
    ContextDietConfig,
    TrimConfig,
    load_context_diet_config,
)
from jiuwenswarm.agents.harness.code.context_diet.trim import (
    TrimResult,
    summarize_tool_result,
    trim_tool_output,
)

__all__ = [
    "CompactionConfig",
    "ContextDietConfig",
    "TrimConfig",
    "TrimResult",
    "load_context_diet_config",
    "summarize_tool_result",
    "trim_tool_output",
]
