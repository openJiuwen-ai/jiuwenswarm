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

Main table: File / Symbol / Span / Line / AUC. Report EditLoc, but locate-only
has empty ``model_patch`` so official evaluate falls back to gold ``patch`` —
label it as such. ``--limit 5`` is smoke, not the official
``contextbench_verified`` 500 (four slices). That 500 is the live-board subset;
it is not SWE-bench Verified 500.

`--profile off` — original Root tools, no graph, no `code_agent`.
`--profile off --graph-agent code_agent` — same grep tools on `code_agent`; Root only has `task_tool`.
`--profile graph --graph-agent root` — find_* tools on Root (locate exam).
`--profile graph --graph-agent code_agent` — find_* tools on `code_agent`; Root only has `task_tool`.

`--graph-agent` is who owns the tools (`root` | `code_agent`). It is the same
knob as product yaml `code_graph.agent`, with a different default: `--profile
graph` still defaults to `code_agent` so previous ContextBench numbers stay
valid. Product yaml writes `agent: root`. An omitted yaml `agent` key also
hangs on `code_agent`.

`--task-mode locate` (default) is the retrieval exam: no patch, hide edit.
`--task-mode coding` is the coding exam: keep edit/bash, write `model_patch`
from `git diff`, keep `usage` on pred, and take the last `<PATCH_CONTEXT>`
after the patch (a second turn asks for it if the first turn omitted it).
Coding still has no SWE Docker / Pass@1. Output folder is `cfg_b__graph__coding`.

This directory has no SWE runner, no testbed, no `--arm`, and no repair loop.
ContextBench scoring still uses the official last `<PATCH_CONTEXT>` block.

Eval injects exam prompts. Locate hangs `submit_code_context` and forbids a
patch. Coding uses the product graph prompt (locate then edit, no submit tool)
and asks the agent to type the MiniSWE block after editing. Product `profile:
graph` hides `grep`/`glob` only while the parser can index, and restores them
on `UNAVAILABLE`. Locate eval also hides edit; coding eval does not.

Files:

- `run_contextbench.py` — read parquet, run instances, write `raw/`
- `run_evaluate.py` — rewrite pred.jsonl and call official `contextbench.evaluate`
- `coding_agent.py` — assemble the product agent without the WebSocket server
- `trajectory.py` — official ContextBench trajectory + per-tool counts (`*.trace.json`)
- `eval_env.py` — ContextBench paths, project `.env`, pinned-engine check

Default `--output` is `docs/ai/experiments/03-contextbench-before-productization/runs/scratch-contextbench`.
Numbered experiments must pass `--output` so they do not overwrite each other.

```bash
export CONTEXTBENCH_ROOT=/path/to/ContextBench   # required unless ../ContextBench exists
uv run --extra code-graph --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile off
uv run --extra code-graph --with pyarrow python scripts/eval/run_contextbench.py \
  --limit 5 --profile graph --graph-agent root
uv run --extra code-graph --with pyarrow python scripts/eval/run_contextbench.py \
  --task-mode coding --instance pallets__flask-5014 \
  --profile graph --graph-agent root --max-iterations 20
uv run --extra code-graph --with pyarrow python scripts/eval/run_evaluate.py \
  --pred docs/ai/experiments/03-contextbench-before-productization/runs/<run>/cfg_b__graph/raw
```
