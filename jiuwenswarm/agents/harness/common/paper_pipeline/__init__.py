"""Hardening layer for the agent-core paper pipeline (openjiuwen ``paper_opt.auto_research``).

JiuwenSwarm's RSI "real" paper mode delegates to agent-core's ``PaperTreeOrchestrator``.
Running it end to end surfaced several defects; this package fixes them from the host side:

- ``code_agent_budget``: the coding agent's inner ReAct loop is silently capped at 15 steps
  (``create_code_agent`` default) regardless of ``code_implementation.max_iterations``.
- ``runner``: the tree orchestrator cannot resume ("paper scenario does not support resume");
  the inner ``ManagerRuntime`` can, so interrupted runs are resumed through it, keeping the
  same usage ledger and re-running the same judge afterwards.
- ``runner`` also injects an execution-environment note (network / dataset mirrors) as the
  create-mode initial prompt, so experiment code does not burn its budget on unreachable hosts.
- ``latex_check``: the reporting stage reports success even when the final PDF still has
  unresolved citations (rendered as "?"); detect and rebuild once.
- ``revision``: ``jiuwenswarm-paper revise`` resumes a finished run for one review-driven revision
  cycle (single-change ablations, replication settings, rewrite), with a host gate that keeps the
  manager from finishing before a new execution and a new paper exist.
- ``evidence``: the evidence contract — a protocol frozen by the host before execution, a hashed
  evidence manifest per execution, and the one ``verify`` used by the audit, the revision gate,
  ``acceptance.json`` and ``evidence_rail.PaperEvidenceRail`` (opt-in ``--evidence-rail``).

Only ``openjiuwen`` and the standard library are imported here, so the package can be used
without the full JiuwenSwarm service stack (``jiuwenswarm-paper`` CLI).
"""

from jiuwenswarm.agents.harness.common.paper_pipeline.code_agent_budget import (
    DEFAULT_CODE_REACT_ITERATIONS,
    install_code_agent_iteration_fix,
)

__all__ = ["DEFAULT_CODE_REACT_ITERATIONS", "install_code_agent_iteration_fix"]
