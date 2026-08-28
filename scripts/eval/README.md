# ContextBench eval (Code Graph)

This directory is **tester-only**. Product users turn Code Graph on with
`code_graph.profile: graph` in yaml. Product template default hang is
`code_graph.agent: root`. These scripts do **not** ship as a user-facing API.

The product exposes two profiles:

- `off` — original coding tools (grep / read / edit). No graph.
- `graph` — find_* retrieval tools. Who owns them is `--graph-agent`.

`pyproject.toml` pins `openjiuwen` to `openJiuwen/agent-core` `agent_os_code_search`.
`uv sync --extra code-graph` installs that branch. `uv sync` does not install
`tree-sitter-language-pack`. Install it yourself; `jiuwenswarm-init` /
`jiuwenswarm-start` download grammars. Query paths never download.

Gold parquet and the official scorer live in a **ContextBench checkout**, which
is not part of this repo. Point the runner at it with one of:

```bash
export CONTEXTBENCH_ROOT=/path/to/ContextBench
# or clone it as a sibling of jiuwenswarm:
#   ../ContextBench
# or pass --contextbench-root / --parquet
```

Do **not** assume `reconstruct_tmp/ContextBench` exists; that was a local
layout on one machine. `CONTEXTBENCH_PARQUET` still overrides the gold file.

Main table: File / Symbol / Span / AUC. Do not report EditLoc. `--limit 5` is
smoke, not Verified 500.

`--profile off` — original Root tools, no graph, no `code_agent`.
`--profile off --graph-agent code_agent` — same grep tools on `code_agent`; Root only has `task_tool`.
`--profile graph --graph-agent root` — find_* tools on Root (locate exam).
`--profile graph --graph-agent code_agent` — find_* tools on `code_agent`; Root only has `task_tool`.

`--graph-agent` is who owns the tools (`root` | `code_agent`). It is the same
knob as product yaml `code_graph.agent`, with a different default: `--profile
graph` still defaults to `code_agent` so previous ContextBench numbers stay
valid. Product yaml writes `agent: root`. An omitted yaml `agent` key also
hangs on `code_agent`.

This directory has no SWE runner, no testbed, no `--arm`, and no repair loop.
ContextBench scoring still uses the official last `<PATCH_CONTEXT>` block.

Eval injects locate-exam prompts (`submit_code_context`, no patch). The product
prompt tells the agent to locate then edit, and does **not** register
`submit_code_context`. Product `profile: graph` hides `grep`/`glob` only while
the parser can index, and restores them on `UNAVAILABLE`. Eval still hides
search/edit tools for the exam and does **not** restore grep if the graph fails.

Files:

- `run_contextbench.py` — read parquet, run instances, write `raw/`
- `run_evaluate.py` — rewrite pred.jsonl and call official `contextbench.evaluate`
- `coding_agent.py` — assemble the product agent without the WebSocket server
- `trajectory.py` — official ContextBench trajectory + per-tool counts (`*.trace.json`)
- `eval_env.py` — ContextBench paths, project `.env`, pinned-engine check

Default `--output` is `docs/ai/experiments-contextbench/runs/scratch-contextbench`.
Numbered experiments must pass `--output` so they do not overwrite each other.

```bash
export CONTEXTBENCH_ROOT=/path/to/ContextBench   # required unless ../ContextBench exists
uv run --extra code-graph --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile off
uv run --extra code-graph --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile graph --graph-agent root
uv run --extra code-graph --with pyarrow python scripts/eval/run_evaluate.py \
  --pred docs/ai/experiments-contextbench/runs/<run>/cfg_b__graph/raw
```
