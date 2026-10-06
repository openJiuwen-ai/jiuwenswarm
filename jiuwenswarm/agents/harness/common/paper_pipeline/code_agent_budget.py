"""Lift the hidden 15-step cap on the paper pipeline's coding agent.

agent-core ``paper_opt/auto_research/modules/code_implementation/agent.py`` builds its coder with
``create_code_agent(..., rails=[..., TaskCompletionRail(max_rounds=max_iterations)])`` and does not
pass ``max_iterations``. The factory default (``harness/subagents/code_agent.py``) is 15, so every
validation cycle stops after 15 ReAct steps while ``code_implementation.max_iterations`` only bounds
the outer task loop — the comment there says the inner loop "stays unbounded", which is not what
happens. On non-trivial designs the coder never finishes ``run.py`` / ``requirements.txt`` and all
retries fail with "missing entry point" / "missing contract file".

The module imports the factory lazily from ``openjiuwen.harness.subagents`` inside the method, so
replacing that package attribute is enough; callers that pass ``max_iterations`` explicitly are left
untouched.
"""

from __future__ import annotations

import os

DEFAULT_CODE_REACT_ITERATIONS = 80
ENV_VAR = "JIUWENSWARM_PAPER_CODE_REACT_ITERATIONS"
_MARKER = "_jiuwenswarm_react_cap"


def resolve_limit(limit: int | None = None) -> int:
    if limit is not None:
        return max(1, int(limit))
    raw = os.getenv(ENV_VAR, "").strip()
    return max(1, int(raw)) if raw else DEFAULT_CODE_REACT_ITERATIONS


def install_code_agent_iteration_fix(limit: int | None = None) -> int:
    """Default ``max_iterations`` of ``openjiuwen.harness.subagents.create_code_agent`` to ``limit``.

    Idempotent; a later call only updates the limit. Returns the limit in effect.
    """
    import openjiuwen.harness.subagents as subagents

    effective = resolve_limit(limit)
    current = subagents.create_code_agent
    if getattr(current, _MARKER, None) is not None:
        setattr(current, _MARKER, effective)
        return effective

    original = current

    def create_code_agent(*args, **kwargs):
        kwargs.setdefault("max_iterations", getattr(create_code_agent, _MARKER))
        return original(*args, **kwargs)

    create_code_agent.__wrapped__ = original
    create_code_agent.__doc__ = original.__doc__
    setattr(create_code_agent, _MARKER, effective)
    subagents.create_code_agent = create_code_agent
    return effective
