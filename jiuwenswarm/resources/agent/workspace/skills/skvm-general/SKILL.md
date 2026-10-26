---
name: skvm-general
description: Drive the skvm CLI on behalf of a user to profile models, AOT-compile skills, run skill-assisted tasks, run benchmarks, and manage compiled proposals. Trigger when the user asks to "profile", "aot-compile", "bench", "run a single ad-hoc task with a skill", or asks about skvm proposals. Do NOT trigger for `jit-optimize` or when the user wants to optimize/improve a skill — use the sibling `skvm-jit` skill instead.
---

# SkVM General Usage

You are driving `skvm`, a CLI that AOT-compiles and runs LLM agent skills across heterogeneous models. Use this skill when the user wants to *use* skvm — profile a model, AOT-compile a skill, run a task with a skill, run a benchmark, or manage optimization proposals. Do **not** invent flags — every example below uses the real flag set from the installed `skvm` binary.

## Step 1: Prerequisite self-check

Split the check in two — the binary must always be present, but the API key is only required for commands that call an LLM.

**Always required** — skvm is on PATH:

```bash
skvm --help >/dev/null 2>&1 || { echo "skvm not installed — tell the user to run: curl -fsSL https://skillvm.ai/install.sh | sh"; exit 1; }
```

**Required only before LLM-calling commands** — `profile` (without `--list`), `aot-compile`, `pipeline`, `run`, `bench`, `jit-optimize`. Local filesystem commands (`profile --list`, `proposals list|show|reject`, `logs`, `clean-jit`) do **not** need the API key — run them even if the key is unset.

```bash
# Before running profile/aot-compile/run/bench/jit-optimize:
test -n "${OPENROUTER_API_KEY:-}" || { echo "OPENROUTER_API_KEY is not set — ask the user for their key"; exit 1; }
```

If a required prerequisite is missing, **stop** and tell the user what is missing. Do not install anything yourself.

## Step 2: Profile a model

A profile (TCP — Target Capability Profile) records what an LLM can do across 26 primitive capabilities. It is the input for AOT compilation and is cached so subsequent compile calls reuse it.

```bash
skvm profile --model=<id>                              # profile one model
skvm profile --model=<id1>,<id2> --concurrency=4       # profile several in parallel
skvm profile --model=<id> --adapter=opencode           # non-default adapter
skvm profile --model=<id> --force                      # ignore cache, re-run
skvm profile --list                                    # list cached profiles
```

Notes:
- Default adapter is `bare-agent`. Other valid adapters: `opencode`, `openclaw`, `hermes`, `jiuwenswarm`.
- Cache lives at `$SKVM_PROFILES_DIR` (default `.skvm/profiles/`).
- Profiling is expensive — confirm with the user before running on several models, and prefer `--concurrency` over sequential runs.

## Step 3: AOT-compile a skill

AOT compilation rewrites a skill's `SKILL.md` (and optionally bundle files) so it fits a specific target model's capability profile. The three-pass AOT compiler runs by default.

```bash
skvm aot-compile --skill=<path> --model=<id>                       # all three passes
skvm aot-compile --skill=<path> --model=<id> --pass=1,2,3          # explicit
skvm aot-compile --skill=<path> --model=<id> --pass=1              # only pass 1 (SCR + gap analysis)
skvm aot-compile --skill=<path> --model=<id> --dry-run             # no write
skvm pipeline --skill=<path> --model=<id>                          # profile-if-needed → aot-compile
```

Pass semantics:
- `--pass=1` — SCR extraction, gap analysis, capability substitution/compensation
- `--pass=2` — dependency manifest + env-binding script generation
- `--pass=3` — workflow decomposition + DAG parallelism extraction

Compiled variants land under the proposals tree (`proposals/aot-compile/...`). Multiple passes can be combined in any subset: `--pass=1,3` runs passes 1 and 3, skipping 2.

## Step 4: Run a single task with a skill

For ad-hoc debugging of one skill on one task:

```bash
skvm run --task=<path/to/task.json> --model=<id>                    # no skill
skvm run --task=<path> --model=<id> --skill=<path/to/SKILL.md>      # with skill
skvm run --task=<path> --model=<id> --adapter=opencode --verbose    # explicit adapter + debug
```

Use this to reproduce a single failing task or validate a skill edit. Do **not** use it for benchmarking — use `skvm bench` instead.

## Step 5: Bench a skill

Benchmarking runs a skill across many tasks and condition variants. It can get expensive fast — always confirm with the user before running across many models or tasks, and use `--concurrency` for parallelism.

```bash
skvm bench --model=<id>                                              # all conditions, all tasks
skvm bench --model=<id> --conditions=original,aot-compiled           # baseline + compiled
skvm bench --model=<id> --conditions=jit-optimized                   # use latest jit-optimize best round
skvm bench --model=<id> --conditions=jit-boost --jit-runs=5          # 5 warmup runs for solidification
skvm bench --model=<id1>,<id2> --concurrency=4                       # multi-model in parallel
skvm bench --model=<id> --tasks=task_01,task_02 --runs-per-task=3    # specific tasks, 3 reps each
skvm bench --model=<id> --async-judge                                # defer LLM-judge to post-run batch
skvm bench --resume=latest                                           # resume an interrupted session
skvm bench --list-sessions                                           # list past sessions
```

Valid `--conditions` strings:
- `no-skill` — run the task with no skill injected (baseline floor)
- `original` — the skill as-written (baseline ceiling)
- `aot-compiled` — full 3-pass AOT compiled variant
- `aot-compiled-p1`, `-p2`, `-p3`, `-p12`, `-p13`, `-p23` — single or partial AOT passes
- `jit-optimized` — the latest best-round variant from `skvm jit-optimize` proposals
- `jit-boost` — code-solidification runtime optimization

Bench logs land at `.skvm/log/bench/<sessionId>/`.

## Step 6: Manage jit-optimize proposals

Proposals are the artifact produced by `skvm jit-optimize` (and by the sibling `skvm-jit` skill). Each proposal contains the original skill in `round-0/`, one or more improved rounds in `round-N/`, and metadata recording which round the engine considered best.

```bash
skvm proposals list                                           # all proposals
skvm proposals list --status=pending                          # only pending (not yet accepted/rejected)
skvm proposals list --skill=<name> --target-model=<id>        # filter by skill + target model
skvm proposals show <id>                                      # print metadata and per-round summary
skvm proposals accept <id>                                    # deploy the engine-recommended best round
skvm proposals accept <id> --round=2                          # override: deploy round 2 instead
skvm proposals accept <id> --target=<dir>                     # deploy to a non-default skill dir
skvm proposals reject <id>                                    # mark as rejected (no deploy)
skvm proposals cancel <id>                                    # stop a detached run still in phase=running
```

Proposal id format: `<harness>/<safe-target-model>/<skill-name>/<timestamp>`, where `<safe-target-model>` is the slugified target model id (forward slashes in the CLI id become `--`). When the user gives you an id like `bare-agent/openrouter--anthropic--claude-sonnet-4.6/calendar/20260401T120000Z`, pass it verbatim — do not reformat it.

Detached runs (`skvm jit-optimize --detach`) write an extra `run-status.json` inside the proposal directory that tracks execution phase (`running` / `done` / `failed`), separate from `meta.json.status`. `skvm proposals show` renders this header and tails the last 20 lines of `run.log` for running / failed detached runs. If the user wants to stop a detached optimization mid-run, use `cancel`; sync runs (no `--detach`) do not need `cancel`, they block until complete.

**Critical rule**: only run `skvm proposals accept` when the user has **explicitly** asked to deploy. If the user just says "check the proposals", run `list` and `show` and stop there. Accepting without confirmation overwrites the skill files in place.

## Step 7: Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `OPENROUTER_API_KEY` | yes | OpenRouter key used by bare-agent, profiler, compiler (when routed through OpenRouter), and jit-optimize optimizer |
| `ANTHROPIC_API_KEY` | optional | Enables the Anthropic SDK backend for the compiler and judge |
| `SKVM_DATA_DIR` | optional | Override the input dataset root (default: `./skvm-data` submodule) |
| `SKVM_CACHE` | optional | Override the runtime cache root (default: `~/.skvm`) |
| `SKVM_PROPOSALS_DIR` | optional | Override the proposals storage root (default: `~/.skvm/proposals/`) |

`OPENROUTER_API_KEY` is only required for commands that actually call an LLM. Local-only commands (`proposals list/show/reject`, `profile --list`, `logs`, `clean-jit`) run without it.

## Rules

- **Never run `bench` or `profile` across many models without explicit user confirmation** — they can cost tens of dollars per run. Always quote an expected model count back to the user before starting.
- **Never run `skvm proposals accept` unless the user explicitly asked to deploy.**
- **Prefer `--concurrency=<n>` over sequential loops** for multi-model work.
- **Do not invent flags.** If the user asks for something you don't see in this skill, run `skvm <command> --help` to check before guessing.
- **Do not install anything.** If `skvm` is missing, tell the user to run the installer; if `OPENROUTER_API_KEY` is missing, ask them for it.
- **Surface skvm's stderr progress lines** (e.g. `Installing bundled opencode…`, `Downloading profile…`) as normal output — they are not errors.
