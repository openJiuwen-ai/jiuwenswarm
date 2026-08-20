# ContextBench eval (Code Graph)

This directory is **tester-only**. Product users turn Code Graph on with
`code_graph.profile: graph` in `jiuwenswarm/resources/config.yaml`.
The product exposes two profiles:

- `off` — original coding tools (grep / read / edit). No graph.
- `graph` — find_* retrieval tools on `code_agent`.

These scripts do **not** ship as a user-facing API. Pair both repos on
`feat/code-graph`. Scripts prepend `../agent-core`.

Gold: `../reconstruct_tmp/ContextBench/data/contextbench_verified.parquet`.

Main table: File / Symbol / Span / AUC. Do not report EditLoc. `--limit 5` is
smoke, not Verified 500.

`--profile off` — original Root tools, no graph, no `code_agent`.
`--profile off --graph-agent code_agent` — same grep tools on `code_agent`; Root only has `task_tool`.
`--profile graph --graph-agent root` — find_* tools on Root (locate exam).
`--profile graph --graph-agent code_agent` — find_* tools on `code_agent`; Root only has `task_tool`.

`--graph-agent` is who owns the tools (`root` | `code_agent`). Product yaml never
sets this; the product always hangs graph tools on `code_agent`.

This directory has no SWE runner, no testbed, no `--arm`, and no repair loop.
ContextBench scoring still uses the official last `<PATCH_CONTEXT>` block.

Eval injects locate-exam prompts (`submit_code_context`, no patch). The product
prompt tells the agent to locate then edit, and does **not** register
`submit_code_context`.

Files:

- `run_contextbench.py` — read parquet, run instances, write `raw/`
- `run_evaluate.py` — rewrite pred.jsonl and call official `contextbench.evaluate`
- `coding_agent.py` — assemble the product agent without the WebSocket server
- `trajectory.py` / `trace.py` — MiniSWE trajectory + per-tool telemetry
- `local_openjiuwen.py` — prepend `../agent-core` and load `resources/.env`
- `install_local_agent_core.sh` — editable install for the product / TUI

Default `--output` is `docs/ai/experiments-contextbench/runs/scratch-contextbench`.
Numbered experiments must pass `--output` so they do not overwrite each other.

```bash
UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile off
UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile graph --graph-agent root
UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_evaluate.py \
  --pred docs/ai/experiments-contextbench/runs/<run>/cfg_b__graph/raw
```
