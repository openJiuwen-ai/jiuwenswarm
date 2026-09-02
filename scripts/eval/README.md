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
knob as product yaml `code_graph.agent`. ContextBench `--profile graph` still
defaults to `code_agent` so previous numbers stay valid. **SWE defaults to
Root** and does not open `code_agent` unless you pass `--graph-agent
code_agent`. Product yaml writes `agent: root`.

`--benchmark contextbench` (default) is the existing collect path. Do not
change it when you only want SWE.

`--benchmark swe` reuses the same agent, hidden-tool rules, `git diff`
(`model_patch`), `usage`, `*.traj.json`, and `swe_preds.jsonl`. It does
**not** ask for `<PATCH_CONTEXT>` and does **not** call
`contextbench.evaluate`. Rows come from HuggingFace
(`--swe-split verified|lite|test`). **`verified` is Verified 500.**
`--swe-split test` is the full SWE-bench **2294**, not Verified's HF
`split=test`. Gold `patch` / `FAIL_TO_PASS` stay off the prompt.
Official submissions do **not** use `hints_text` (issue comments). That
is the default. Pass `--hints` only for an explicit ablation. `--limit 5`
is smoke; official Verified is `--full` (500). `--offset N` skips the
first N instances of the split (after `--instance`). Failed checkouts still
write an empty-patch pred so they count as not resolved.

This SWE path is **not** the product default (yaml `profile: off`,
`agent: root`, no exam prompt). Eval defaults stay `--profile graph` and
`--graph-agent code_agent` so earlier ContextBench numbers stay valid.

`--task-mode locate` (default on ContextBench) is the retrieval exam: no patch, hide edit.
`--task-mode coding` is the coding exam: keep edit/bash, write `model_patch`
from `git diff`, keep `usage` on pred. ContextBench coding still takes the
last `<PATCH_CONTEXT>` (a second turn asks for it if omitted). SWE rejects
locate and defaults to coding. Output folders:
`cfg_b__graph__coding` (ContextBench) vs `cfg_b__graph__swe` (SWE).

Official Verified Pass@1 is Docker `resolved / 500`. Missing, failed, and
empty patches all count as not resolved. `resolved / submitted` is smoke
only. After eval the runner calls official `swebench submit package` only
when `SWE_BENCH_ROOT` has `swebench.submit` (re-grades from
`test_output.txt`). It does **not** write a self-copied `submission/` from
`report.json`. That helper is `--unofficial-package` →
``raw/unofficial-submission/`` and is not a leaderboard entry. Metadata
still needs a human; Mac Docker results cannot be submitted.

```bash
export SWE_BENCH_ROOT=/path/to/SWE-bench   # or sibling ../SWE-bench
uv run --extra code-graph --with datasets --with-editable "$SWE_BENCH_ROOT" \
  python scripts/eval/run_evaluate.py \
  --benchmark swe --swe-split verified \
  --pred docs/ai/experiments/06-swe-verified-current/runs/<run>/cfg_b__graph__swe/raw
# official package only (needs SWE-bench submit):
uv run --with-editable "$SWE_BENCH_ROOT" python scripts/eval/run_evaluate.py \
  --benchmark swe --package-only \
  --pred docs/ai/experiments/06-swe-verified-current/runs/<run>/cfg_b__graph__swe/raw
# local report.json copy (not for the board):
uv run python scripts/eval/run_evaluate.py \
  --benchmark swe --package-only --unofficial-package \
  --pred docs/ai/experiments/06-swe-verified-current/runs/<run>/cfg_b__graph__swe/raw
```

ARM Mac is not the leaderboard host. Scripts set
`SWE_BENCH_ALLOW_EXPERIMENTAL_HOST=1` and `DOCKER_DEFAULT_PLATFORM=linux/amd64`;
still pre-pull `linux/amd64` images. Do not ignore harness
`total_instances=500`.

This directory has no testbed, no `--arm`, and no repair loop.
ContextBench scoring still uses the official last `<PATCH_CONTEXT>` block.

Eval injects exam prompts. Locate hangs `submit_code_context` and forbids a
patch. Coding uses the product graph prompt (locate then edit, no submit tool)
and asks the agent to type the MiniSWE block after editing. Product `profile:
graph` hides `grep`/`glob` only while the parser can index, and restores them
on `UNAVAILABLE`. Locate eval also hides edit; coding eval does not.

Files:

- `run_contextbench.py` — ContextBench parquet or `--benchmark swe`; write `raw/`
- `swe_dataset.py` — SWE HuggingFace rows; gold patch / F2P stay off the prompt
- `run_evaluate.py` — ContextBench `evaluate.py`, or `--benchmark swe` → `swebench eval` + official `submit package` when present
- `swe_submit.py` — local `unofficial-submission/` helper (copies `report.json`; not a board entry)
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
uv run --extra code-graph --with datasets python scripts/eval/run_contextbench.py \
  --benchmark swe --swe-split verified --limit 1 \
  --profile graph --graph-agent root --instance astropy__astropy-13579
# official Verified 500 (Linux x86_64 Docker; hints off by default):
# uv run --extra code-graph --with datasets python scripts/eval/run_contextbench.py \
#   --benchmark swe --swe-split verified --full \
#   --profile graph --graph-agent root
```
