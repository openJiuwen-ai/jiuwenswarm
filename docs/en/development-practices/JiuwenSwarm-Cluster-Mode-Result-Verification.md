# JiuwenSwarm Cluster Mode Result Verification

> **Goal**: For users who can already start JiuwenSwarm. Run a single-Agent task and a cluster mode task on the same fixed input, then verify three things: whether collaboration actually happened, whether any step failed, and whether the conclusions stay faithful to the sources.
>
> **Chinese version:** [中文版](../../zh/开发实践/JiuwenSwarm集群模式结果核验实践.md)

---

## 1. Overview

When a cluster mode run ends, the task panel shows "completed" and the Leader writes a summary. Both only tell you the execution chain finished, not that the conclusions are correct. This document uses one real run to show how to check "task completed" and "result trustworthy" separately.

### 1.1 Use cases

- Evaluating cluster mode for the first time, and you need to confirm that several members really split the work rather than one model role-playing several roles.
- Using Agents for research or assessment tasks, where each conclusion must map to a source.
- Keeping a reviewable run record for an internal demo or a technology selection.

### 1.2 Environment

| Item | Value |
|------|-------|
| OS | macOS |
| Python | 3.11, dedicated virtual environment |
| Package | PyPI release `workswarm 0.2.7`, with `openjiuwen 0.1.19` |
| Model | DeepSeek, shown as `deepseek-flash` in the UI |
| Run date | 2026-10-21 |

> ⚠️ **Note**: All data here comes from one run with a fixed, limited input. It does not represent typical performance, cost or production readiness.

---

## 2. Prepare an isolated environment

### 2.1 Install and start

Use a dedicated virtual environment and data directory so that existing workspaces are not affected:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install "workswarm==0.2.7"

export JIUWENSWARM_HOME="$PWD/demo-home"
jiuwenswarm-init
jiuwenswarm-start
```

After startup, open `http://localhost:5173` in a browser.

### 2.2 About the data directory

- `JIUWENSWARM_HOME` replaces the user home directory, so configuration and workspace are created under `$JIUWENSWARM_HOME/.jiuwenswarm`. The variable is read by `get_user_home()` in `jiuwenswarm/common/utils.py`.
- For several long-running instances, see [Running multiple instances on one machine](../MultiInstance.md) first.
- Default ports are `18092 / 19000 / 19001 / 5173`. For port conflicts, see [Quickstart](../Quickstart.md).

---

## 3. Configure a model

In the Web UI settings, add a model service, enter its endpoint and credentials, test the connection and select a model. Field details follow [Configuration](../Configuration.md) and the current UI.

> ❌ **Do not**: put model credentials into prompts, output files or public screenshots.

---

## 4. Prepare a fixed input

Verification requires a fixed input. This run placed a `sources.md` in the workspace with 7 numbered excerpts from public pages:

```markdown
## S1 A platform's home page
...excerpt...

## S6 Distributed Team document
This document targets development and integration testing. A distributed Team on one channel currently uses single-active-session semantics...
```

A fixed input does two things:

- The Agent does not browse, so differences come only from the model and the framework, which makes the run reproducible.
- Every conclusion can be traced back to a numbered source.

---

## 5. Design two tasks

Both tasks read the same file but differ in output length and form, so their durations are not compared directly.

### 5.1 Single-Agent baseline

In single-Agent mode, send:

```text
Read sources.md in the workspace and use only that file. Do not browse, read other
directories, install dependencies or send messages. Write a one-page recommendation to
single-result.md with: 3 known facts tagged [S#], 2 open questions, and 3 minimal
verification steps. Keep facts and recommendations separate; do not invent scale or
commitments. At most 600 characters. Do not create scheduled tasks or call other Agents.
```

### 5.2 Cluster mode

Switch to cluster mode and send:

```text
Read sources.md in the workspace and use only that file. Do not browse, read other
directories or install dependencies. Organise two members working in parallel: one on
ecosystem and scenarios, one on technical integration and limits. Each submits a formal
result of at most 200 characters with at least one [S#] fact and one open item.
After the Leader reviews both results for sources and boundaries, save a combined
team-result.md of at most 700 characters. A member saying "done" does not mean the source
review passed. At most two members; do not expand the team.
```

> 💡 **Tip**: Fixing the member count and output locations in the prompt makes the trace easier to check afterwards.

---

## 6. Verify three things

### 6.1 Did collaboration actually happen

Do not rely on the Leader's summary. Check the tool-call trace and task status; per-turn tool calls are available through [Debug tracing](../DebugTrace.md). The cluster run trace contains:

| Tool | Count | Meaning |
|------|-------|---------|
| `build_team` | 1 | Build the team |
| `spawn_teammate` | 2 | Create two members |
| `create_task` | 2 | Create tasks for members |
| `claim_task` | 4 | Members claim tasks |
| `send_message` | 3 | Messages between members and the Leader |

The task panel shows both tasks completed:

![Cluster mode task progress: both tasks completed](../../assets/images/development-practices/cluster-mode-task-progress.png)

This shows that two members each claimed and finished a task, rather than one model playing several roles in a single reply.

### 6.2 Did any step fail

"Completed" does not mean nothing went wrong. Results of this run:

| Check | Single Agent | Cluster mode |
|-------|--------------|--------------|
| Time between first and last session event | about 36 s | about 137 s |
| Tool calls | 14 | 59 |
| Tool results flagged as failed | 1 (`write_file`) | 4 (1 `create_task`, 3 `write_file`) |
| Output files | 1 | 2 member results + 1 summary |

All failures were recovered in later steps and every file was produced. Record the failure count instead of only writing "completed successfully".

Also note that the 600-character single-Agent task made 14 tool calls, several of them for self-checking length. Tool overhead on short tasks is a candidate for tuning.

### 6.3 Are the conclusions faithful to the sources

This check is the easiest to skip. The two member results contained two statements that went beyond their sources:

| Source text | Member statement | Problem |
|-------------|------------------|---------|
| S6: "This document targets development and integration testing" | "Distributed Team is limited to development testing" | Turns the document's audience into a feature limitation |
| S2: public projects "cannot be directly called" the platform's own products | "Not own products" | Turns "cannot be established" into a definite negative |

The Leader still marked both results as passed. In this run's task records the `reviewer` field was `null`, meaning no independent reviewer role was configured.

Record verification line by line instead of trusting a model that says it has checked:

| Output text | Source | Overstated? | Correction |
|-------------|--------|-------------|------------|
| ... | S6 | Yes | "This document targets development and integration testing" |

> 💡 **Tip**: When accuracy matters, add a member whose only job is source verification, have it output the table above, and let the Leader summarise afterwards.

---

## 7. How to record usage

The single-Agent task's usage summary was: 431,945 input, 5,417 output, 437,362 tokens in total, of which 402,688 were cached (hit rate about 93.2%).

When recording usage:

- This is the framework's cumulative call count including cache, not the provider's billable tokens.
- The cluster run did not produce a summary with the same scope, so it is not compared with the single-Agent run.
- Do not estimate cost or savings before reconciling with the provider's bill.

---

## 8. Conclusions and limits

This run confirms that:

- The release package installs, initialises and starts on a local Mac.
- Cluster mode really created two members and two tasks, with separate results and a Leader summary.
- Task completion, Leader approval and faithfulness to sources are three separate checks.

Not verified in this run: cross-machine distributed runs, Team Organization, SwarmFlow human nodes, restart recovery, sustained concurrency and security isolation. A single sample cannot be used to compare frameworks.

---

## 9. FAQ

### 9.1 Is manual checking still needed when the Leader approves

**Problem**: The Leader's summary says the member results passed review.

**Solution**: Still check each statement against its source as in section 6.3. In this run, results approved by the Leader contained two overstatements.

### 9.2 How to confirm the result was not produced by a single model

**Problem**: The summary contains several roles speaking, but it is unclear whether work was really split.

**Solution**: Look for `spawn_teammate`, `create_task` and `claim_task` calls in the trace, and check the number and status of tasks in the task panel.

### 9.3 Port already in use at startup

**Problem**: A previous instance did not stop completely, or another application holds the port.

**Solution**: See the port notes in [Quickstart](../Quickstart.md) and stop the old instance with `jiuwenswarm-start --stop default`.

---

## Back to navigation

- [Back to documentation home](../../README_EN.md)
- [Back to project home](../../../README.md)
