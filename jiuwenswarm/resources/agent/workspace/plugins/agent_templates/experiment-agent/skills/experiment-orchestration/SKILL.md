---
name: experiment-orchestration
description: Orchestrate Module 3 from a Module 2 plan through data preparation, implementation resolution, execution, analysis, and a strict Module 4 handoff. Use when running or resuming the experiment-agent workflow.
---

# Experiment Orchestration

Use the deterministic tools; do not reproduce their calculations in conversation.

1. Read [references/agent-handoffs.md](references/agent-handoffs.md) before initiating or resuming a run.
2. Initialize through the root control tool.
3. Delegate exactly one direct child at a time in this fixed order: data, implementation, execution, analysis.
4. On `CODE_REVIEW_REQUIRED`, create an isolated root-review context, read only the backend's verifiable review material, and submit the exact structured first-stage decision. The implementation child must never review itself.
   After every implementation-child response, query root control status. If the backend remains at `DATA_READY` and there is no tool-produced structured terminal blocker, re-delegate the child to complete `inspect` followed by `request_approval`; never promote free-text `REPLAN` into planning feedback.
5. Only an `APPROVE_SMOKE` decision plus deterministic gates may trigger the backend's bounded smoke test. On `EXECUTION_REVIEW_REQUIRED`, review the fresh code, command and real smoke evidence in a new isolated context and submit the exact second-stage decision.
6. LLM decisions never write approval fields. Delegate execution only after the backend verifies all identities, digests and gates and returns `IMPLEMENTATION_READY`.
7. Pass only `run_dir`, `run_id`, and download settings between stages. Dataset search/download is enabled by default and does not require another user confirmation; preserve an explicit offline override.
   The data tool owns source resolution: it uses a Module 2 URL first, otherwise searches public registries for one exact dataset-name match. Preserve any `REPLAN` candidate list, registry error, and manual target directory in the child response; never ask the data child to choose an ambiguous result.
8. Stop on `COMPLETED`, `REPLAN`, or `FAILED`.
9. Treat persisted artifacts as authoritative and never hand-edit workflow state.
10. Task shape is supplied by the backend inspection. In particular, `memory_compression` consumes LongBench JSON/JSONL without a label, training split, GPU, or LLM inference and uses the registered deterministic proxy metrics. Do not impose tabular classification/regression rules on it.

The canonical experiment schemas and executable backend live in the shared `workspace/skills/experiment` skill. This orchestration skill deliberately does not duplicate that backend.
