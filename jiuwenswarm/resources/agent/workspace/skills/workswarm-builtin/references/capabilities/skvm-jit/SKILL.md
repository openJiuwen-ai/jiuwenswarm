---
name: skvm-jit
description: Trigger `skvm jit-optimize` with post-task evidence so it generates a reviewable optimization proposal for a skill. Use after finishing a task that was driven by any skill (not just SkVM-compiled ones) when the outcome was a failure, was partial, or the skill's instructions clearly caused confusion or an unnecessary detour. Clean successful runs usually do not need an optimization pass. For general skvm usage (profile/aot-compile/bench/proposals), use the sibling `skvm-general` skill instead.
---

# SkVM JIT Optimize Trigger

When you finish a task that was driven by a skill, and the skill's own instructions appear to have caused problems, collect a short structured record of what happened and invoke `skvm jit-optimize`. The optimizer reads that evidence, proposes improvements to the skill's files, and stores them as a **proposal** you can inspect with `skvm proposals show <id>`.

This skill is not a human review workflow. Its job is to turn task evidence into a concrete optimization run.

This works for **any** skill the host harness can load — it does not need to have been produced by SkVM's compiler. `jit-optimize` only needs the skill folder on disk; it does not require SkVM-specific artifacts.

## When to trigger

Run this skill **only** when both of the following hold:

1. The task you just completed was driven by a skill that the harness loaded from disk (not an ad-hoc user instruction).
2. At least one of the following is true:
   - the task **failed** or ended **partial**, and a clearer skill would plausibly have avoided it
   - you observed a concrete problem in the skill's instructions: ambiguity, a missing step, an incorrect claim, a confusing ordering, or an unnecessary detour the skill forced you into

Do **not** run this skill when:

- the task succeeded and the skill read cleanly — silent passes are fine, the optimizer does not need "nothing to report" submissions
- the failure was purely user-side (typo in the prompt, missing credentials, network failure) and no change to the skill would have helped
- the task did not use a skill at all, or used only a trivial one-shot instruction
- you are running inside `skvm bench` or any other SkVM-orchestrated flow — bench owns its own optimization loop, do not double-submit

## Step 1: Locate the skill directory

The skill directory contains a `SKILL.md` file. You need the absolute path to pass as `--skill=<dir>` in Step 3.

Each agent harness installs skills in well-known locations. Read `adapter-skill-paths.md` (sibling file in this skill's directory) and look up the section matching the harness you are currently running inside — it lists the search order for Claude Code, opencode, openclaw, hermes, jiuwenswarm, and bare-agent. Probe the listed paths in order and pick the first one that contains a `SKILL.md` for the skill name you are looking for.

If none of the listed paths contains the skill, or the reference file marks your harness as "confirm with user", ask the user for the path — do not guess.

## Step 2: Prepare optimizer input

Pick **one** of the two formats below. Save it anywhere (e.g. a temp file); you'll pass its path as `--logs=<path>` in Step 3.

### Format A — Simple optimization report (preferred for one-off observations)

Save as `report.json`:

```json
{
  "task": "<what the user asked, one or two sentences>",
  "outcome": "pass" | "fail" | "partial",
  "issues": [
    "short description of each problem you hit",
    "another problem"
  ],
  "skill_feedback": "concrete suggestion for how the SKILL.md could be clearer or more correct"
}
```

Keep `issues` focused on things the **skill's instructions** could prevent or clarify. Do not include issues that were purely user-side (typos in the prompt, missing credentials, network failures).

The JSON key is still named `skill_feedback` because that is the current report schema consumed by `jit-optimize`; treat it as an optimization hint for the engine, not as human-directed feedback.

When `outcome` is `fail` or `partial`, the optimizer treats the issues and `skill_feedback` as failure reasons attached to a synthetic "agent-reported" criterion. When `outcome` is `pass`, the report still enters the optimizer but with no failures, letting it notice what worked well.

### Format B — Conversation log (preferred when the full turn-by-turn trace is informative)

Save as `conv-log.jsonl`, one JSON object per line:

```jsonl
{"type":"request","ts":"<iso8601>","text":"<user prompt>"}
{"type":"response","ts":"<iso8601>","text":"<your reply or summary of the step>"}
{"type":"tool","ts":"<iso8601>","text":"<tool call summary>","toolCalls":[...]}
```

Only include entries that matter for diagnosing the skill's quality. Redact secrets.

## Step 3: Run jit-optimize

```bash
skvm jit-optimize --detach \
  --skill=<skill-directory> \
  --task-source=log \
  --logs=<path-to-report.json-or-conv-log.jsonl> \
  --target-model=<id-the-task-ran-on> \
  --optimizer-model=openrouter/z-ai/glm-5.1
```

`--detach` is what lets this skill stay snappy. Without it the optimizer runs in-process and blocks the agent for the full optimization pass (often a minute or more); with it the CLI returns in well under a second after spawning a background worker. Always pass it from this skill.

Required parameters:

- `--skill` — path to the skill directory (the one containing `SKILL.md`)
- `--task-source=log` — tells jit-optimize to analyze a conversation log without rerunning anything. **This is the only task source valid from this post-task optimization flow** — `real` and `synthetic` sources rerun tasks against a live model, which a post-hoc report cannot do.
- `--logs` — path to the report file you wrote in Step 2
- `--target-model=<id>` — **required for every `skvm jit-optimize` invocation, including `--task-source=log`**. In log mode the target model is not used for execution — it is the *storage key* that decides which folder under `proposals/<harness>/<target-model>/<skill>/` the proposal lands in, so proposals stay grouped by the model the skill is tuned for. Use the prefixed model id of the model that just ran the task — **that is you**, the agent reading this skill. Every id must carry a `<provider>/` prefix that matches a route in the user's `providers.routes`; read your own model id out of your system prompt / harness environment and prepend the right provider (Claude Code exposes it as the "exact model ID", e.g. map `claude-opus-4-6` → `anthropic/claude-opus-4.6` when Anthropic-routed, or `openrouter/anthropic/claude-opus-4.6` when OR-routed; opencode/openclaw/hermes similarly). If you genuinely cannot determine your own model id or provider, ask the user once and stop — do not substitute a placeholder.
- `--optimizer-model=<id>` — the LLM that drives the optimizer agent. Every id must carry a `<provider>/` prefix; `openrouter/z-ai/glm-5.1` is a good cheap default (needs `OPENROUTER_API_KEY`).

Optional:

- `--target-adapter=<name>` — purely informational in log mode (default: `bare-agent`). Set it if the log came from a non-default adapter (e.g., openClaw, Hermes, jiuwenswarm) so the proposal is filed under the right harness folder.
- `--failures=<path,...>` — structured failure-reasons JSON, one path per corresponding entry in `--logs`. Pass only when you already have a cleaner per-criterion breakdown than the report file itself; the count must match `--logs`. Skip it for single-report cases.

**What NOT to pass in log mode** (the CLI will error if you do):

- `--tasks`, `--test-tasks` — these belong to `--task-source=real`
- `--synthetic-count`, `--synthetic-test-count` — these belong to `--task-source=synthetic`
- `--runs-per-task`, `--convergence`, `--baseline` — the log mode does not rerun the task, so there is no loop to configure

With `--detach`, the CLI returns in well under a second. Stdout ends with a block like:

```
Proposal: <harness>/<safeTargetModel>/<skill>/<timestamp>
Proposal dir: <absolute-path>
Detached; watch with 'skvm proposals show <id>'
```

The optimization continues in a background worker process; its progress is recorded in `<proposal-dir>/run-status.json` (`phase: queued | running | done | failed`) and detailed log output goes to `<proposal-dir>/run.log`. Both are surfaced when the user runs `skvm proposals show <id>`.

**Capture the id from the line starting with `Proposal: ` only** — everything after `Proposal: ` up to the newline is the id. Do not parse `Proposal dir:` or the `Detached; watch with` line. Note the middle segment is `safeTargetModel` (derived from `--target-model`), not the optimizer model.

If `skvm jit-optimize` exits **non-zero** with no `Proposal:` line, the worker failed before it could allocate a proposal id. Read the stderr message and stop — do not retry blindly. The most common cause is `another optimization is in progress for <skill>`, which means a prior detach for the same skill+model is still running.

## Step 4: Report the proposal id

Print one line with the proposal id you captured:

> Triggered `skvm jit-optimize` for `<skill>`. Review with `skvm proposals show <id>`; accept with `skvm proposals accept <id>`.

Do **not** accept or deploy the proposal yourself unless the user explicitly asks you to. If the user asks you to deploy, run `skvm proposals accept <id>` and report the deployed file list.

## Rules

- Never include sensitive data (API keys, private file contents) in the report.
- Never edit the skill directory directly. Proposals are stored under `$SKVM_PROPOSALS_DIR` (default `~/.skvm/proposals/`); only `skvm proposals accept` writes back into the skill.
- If `skvm` is not on PATH, report it to the user and stop — do not install anything. If `skvm jit-optimize` fails with "opencode not found", tell the user to re-run the skvm installer (`curl -fsSL https://skillvm.ai/install.sh | sh` or `npm i -g @ipads-skvm/skvm`) rather than installing opencode yourself. skvm bundles its own private opencode copy and manages it through the installer.
- One report per task. Don't batch multiple unrelated tasks into a single report.
- `OPENROUTER_API_KEY` must be set in the environment for the optimizer to run. If it is missing, the background worker will fail and `skvm proposals show <id>` will display `run: FAILED` with the reason. The parent CLI cannot detect this in advance because the API key is only used inside the detached worker.

## Reference: what happens on the skvm side

`skvm jit-optimize --detach` validates the flags, then forks a background worker that does all the heavy work (creating the proposal directory, acquiring a per-skill lock, running the optimizer agent, snapshotting rounds). Once the worker has the lock and a proposal id, it tells the parent over an IPC channel; the parent prints `Proposal: <id>` and exits. The worker keeps running independently and writes its phase to `<proposal>/run-status.json` (`queued → running → done | failed`) so `skvm proposals show <id>` can report progress at any time.

The optimizer agent reads your report, inspects the skill folder, diagnoses a root cause, and edits files in a temp workspace (SKILL.md and/or bundle files). The edited folder is snapshotted as `round-1/` inside the proposal. `round-0/` is a copy of the original skill. The user can later diff the two with `skvm proposals show <id>` or reject the proposal if the root cause looks wrong. Proposals are keyed by `(harness, target-model, skill-name)`, which is why `--target-model` is required even in log mode and why concurrent detached runs for the same triple are rejected with a clean error rather than left to clobber each other.
