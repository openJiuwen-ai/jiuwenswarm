# Loop Tasks (Loop Engineering)

> **Goal**: Introduce the two entry points of Loop Engineering orchestration (the `jiuwenswarm-loop` CLI and the `/loop` slash command), all supported arguments, and typical usage.
> **中文版：** [中文文档](../zh/Loop任务.md)

---

## 1. Overview

Loop Engineering is a task execution framework: you provide a single task description, and the system automatically runs a loop of "rubric decomposition → maker execution → machine verification → independent grader acceptance → gap feedback", until the goal is verified, the budget is exhausted, or the work escalates to a human.

```
1. Rubric decompose   task → 4-7 binary-verifiable acceptance criteria (frozen to disk)
2. Maker execute      the real jiuwenswarm harness does the work (same capabilities
                      as a normal session: tools / skills / multiple modes)
3. Machine verify     the --verify command (deterministic, authoritative signal,
                      output annotated with an explicit exit code)
4. Grader accept      an independent LLM grades every criterion (conservative
                      principle: unconfirmable → fail)
5. Revision loop      unmet criteria → gap list fed back to the maker → re-verify
                      (up to N iterations)
```

"Done" is a structural guarantee: machine verification exits 0 AND the grader passes every criterion AND the consistency check passes AND the budget is not exhausted. The protocol design follows LangChain deepagents' RubricMiddleware (five-state verdict, per-criterion gaps, cross-field consistency checks, injection defenses).

## 2. Two Entry Points

| | Entry 1: standalone CLI | Entry 2: slash command |
|---|---|---|
| Trigger | run `jiuwenswarm-loop ...` in a terminal | type `/loop ...` inside a session |
| Service required | No (in-process runtime is self-contained) | Yes (running service required) |
| Experience | command-line logs + final summary report | streaming events + final chat.final summary |
| Arguments | full set | lightweight subset (rest inherited from the session) |
| Best for | scripted / batch tasks / CI | ad-hoc tasks during daily conversations |

## 3. Standalone CLI Arguments

### 3.1 Required

| Argument | Description |
|------|------|
| `task` | Task description; when prefixed with `@`, the file content is read as the task (e.g. `@task.md`) |

### 3.2 Optional

| Argument | Default | Description |
|------|------|------|
| `--cwd PATH` | current directory | working directory of the maker |
| `--project-dir PATH` | `--cwd` | project identity directory |
| `--trusted-dir PATH` | `--cwd` | trusted directory (repeatable; approval whitelist — always include the working directory) |
| `--mode MODE` | `agent.code.normal` | maker mode: `code.normal` (code) / `agent` (general tasks) / `team.*` (multi-agent) |
| `--max-iterations N` | `3` | maximum loop iterations |
| `--state-dir PATH` | `<cwd>/loop_state` | state output directory |
| `--round-timeout SECONDS` | `900` | per-round timeout for the maker |
| `--verify "CMD"` | none | machine verification command; exit code 0 means pass (**strongly recommended**) |
| `--diff-repo PATH` | auto-detected | git diff evidence directory (explicit > cwd > probe one level of subdirectories for a git repo) |
| `--evidence-file PATH` | none | artifact file evidence (repeatable; required for non-git tasks) |

### 3.3 Exit Codes

`0` = satisfied and machine verification passed; `1` = other error; `2` = iteration cap reached; `3` = rubric not evaluable; `130` = interrupted.

## 4. Slash Command Arguments

```
/loop [--verify "cmd"] [--max-iterations N] task description
```

`cwd` / `project_dir` / `trusted_dirs` / `mode` are inherited from the initiating session. Token boundary: only `/loop` or `/loop ...` match; inputs like `/loops` or "please explain /loop" are still treated as ordinary messages.

## 5. Typical Usage

```bash
# Code fix + test verification (most typical)
jiuwenswarm-loop --cwd ~/myproject --trusted-dir ~/myproject \
  --verify "python -m pytest tests/ -q" \
  "Fix the bugs behind the three failing tests under tests/"

# SWE-bench style: task file + verification script
jiuwenswarm-loop --cwd /workspace --trusted-dir /workspace \
  --verify "bash /workspace/verify.sh" "@/workspace/task.md"

# Writing task: agent mode + artifact evidence (non-git)
jiuwenswarm-loop --mode agent --cwd /out --trusted-dir /out \
  --evidence-file /out/article.md \
  "Write an article of about 1000 words to /out/article.md"

# Ad-hoc inside a session
/loop --verify "npm test" Update the README install steps to the latest CLI usage
```

## 6. State File

Each run produces `loop_state.json` under `--state-dir`: the frozen rubric, per-iteration machine verification and grader verdicts, per-criterion pass/fail with gaps, the final state (`satisfied` / `failed` / `max_iterations_reached`) and escalation log. The file is the loop's external state — auditable and resumable.

## 7. Notes

1. **Model config**: shares `models.defaults[0]` of `~/.jiuwenswarm/config/config.yaml` with Web/CLI
2. **Trusted directories**: the standalone CLI must pass the working directory via `--trusted-dir`; otherwise maker operations trigger permission prompts that nobody answers in unattended runs
3. **Write the task completely**: the protocol includes unattended discipline (no questions, no waiting) — constraints, paths, and verification methods must all be stated in the task
4. **Value of `--verify`**: the authoritative signal for the grader; without it the grader can only judge conservatively, which often leads to repeated needs_revision verdicts

## Back to Navigation

[Back to Documentation Home](../README_EN.md)
[Back to Project Home](../../README.md)
