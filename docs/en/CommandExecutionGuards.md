# Command Execution Guards

Why these bounds exist, and what an incident showed was missing.

## 1. The incident

On 2026-08-02 a user asked the agent, in three consecutive turns, for the latest
commit's hash, then its subject, then its date. The first two were answered. For
the third the model produced:

```
git log -1 --format=%ad --date=format:'%m月%d日'
```

On Git for Windows that command allocates without bound: the custom `format:`
path enters `strbuf_addftime`, the C runtime's `strftime` returns 0, Git reads 0
as "buffer too small", doubles it, and repeats until `xrealloc` gives up. One
invocation reached roughly 8.5 GB working set and 49 GB private memory. The
command is correct on glibc — `git 2.43.0` returns `08月04日` — so this is a Git
for Windows defect, not a JiuwenSwarm one.

What follows is the JiuwenSwarm part.

The tool returned an explicit failure — `success=False`, exit 128,
`fatal: Out of memory, realloc failed` — and the agent issued the same command
again. Nine identical calls in two minutes thirty-four seconds, eight of them
completing with the same bytes, the ninth cancelled by a person. Across all nine
turns the model emitted no text at all.

Nothing in the runtime stopped it, and nothing would have in time.

## 2. What was missing

**Wall clock was the only bound, and it is the wrong axis.** `timeout_seconds`
defaults to 300, but each of the nine calls *completed* — it failed with OOM in
about eighteen seconds. A timeout never fires on a command that finishes. Even
the tightest default in this class of tool (Claude Code kills bash at two
minutes) would not have caught this, and the whole incident ran in 2m34s.

Bounding the resource rather than the clock is what would have caught it. §4
records why that is not in this change.

**The tool's own failures were invisible to the guards.** Several paths returned
a bare string:

```python
return f"[ERROR]: command timed out after {timeout_seconds}s."
```

`ToolResultErrorDetector` reads such a string as neither success nor error, so a
command that timed out or was blocked by the safety check counted toward no
streak in the circuit breaker. Only commands that ran and exited non-zero were
ever counted.

**No detector matched the signature.** The circuit breaker's ladder is:

| rule | threshold | level |
| --- | --- | --- |
| `global_breaker` — no progress | 30 | critical |
| `unknown_tool_repeat` — consecutive errors | 10 | critical |
| `ping_pong` — two tools alternating | 20 | critical |
| `generic_repeat` — **same tool, same arguments** | 10 | **warning only** |

The one rule that matches "identical call, identical failure, over and over" had
no critical level at any count. And a warning was `logger.warning` — a line in a
server log the model never sees, so it could not change what came next.

**The circuit breaker was off by default**, which makes every threshold above
moot: none of those rules ran, because the switch was false. This is not a
secondary detail — it is why a tuned ladder of detectors sat out an incident it
was built for.

**The one subsystem that did fire, fired late and was ignored.** `ws-dev.log`
records a single `context.compression_state` event for the whole run:
`ReasoningToolLoopCompactProcessor`, `reasoning_tool_loop_compacted:tool_args`,
15 ms, as the seventh failure arrived. It folded 23 messages into 14.

The fold is not a deletion. `_compact_messages` returns
`messages[:fold_start] + [summary]`, where `summary` is an `AssistantMessage`
carrying `_TOOL_ARGS_LOOP_WARNING_TEMPLATE_CN` — the failing command, the OOM
error in full, and 请跳出多轮重复工具调用, "break out of the repeated tool calls".
So at call 8 the model held two explicit failures plus an explicit instruction to
stop. It made calls 8 and 9 byte-identical.

Two things the fold does cost. The summary reproduces only the **latest** round
and never says how many were folded, so the repetition count — the one datum that
makes the situation unambiguous — is what it destroys. And the warning is an
`AssistantMessage`, so the instruction to break the loop is attributed to the
model itself rather than to the system.

This is why the guards below are bounds on damage rather than attempts to change
the model's mind. Something already tried that, in the model's own language, with
the evidence attached — and §5 shows the attempt is what stopped the recovery.

## 3. The guards

### 3.1 Terminating the command means terminating its tree

`terminate_shell_process` reaches the whole process group through `os.killpg` on
POSIX, but falls back to `proc.terminate()` on Windows, which reaches the direct
child only. That child is the shell wrapper; the process actually doing the work
is its descendant.

Agent-core's `AsyncProcessHandler._kill_process_tree` — the teardown behind the
`bash` tool — has the same shape. So on Windows a timeout or a cancel reported a
killed command while the process holding the memory kept running, on the platform
where the runaway allocation was happening.

`reap_process_descendants` sweeps the tree with psutil, and is wired into both
shell paths: `mcp_exec_command`'s loop directly, and `bash`'s through the existing
`bash_tool_safety` patch. **`bash` is the tool the incident used**, so covering
only the other one would have missed the case entirely.

**Order matters, and it is parent-first.** A shell whose current child disappears
simply starts the next command in the script, so sweeping descendants *before*
asking the parent to die opens a window for a fresh one to outlive the sweep. The
parent is terminated first; the descendant pids read just before that are what
gets swept afterwards.

**A reaped pid is never walked.** Terminating normally waits on the child, and
once a pid has been waited on the kernel may hand it to an unrelated process,
whose children are not ours to kill — the one way this teardown could do real
damage. Both shell paths therefore gate every walk on the process still being
unreaped, and rely on the pre-kill snapshot for the orphan case it exists to
cover. Best effort throughout: failing to reap one member must not stop the rest
from being reaped.

### 3.2 Failures the guards can read

Every failure path now returns the JSON shape the tool already documents, with a
non-zero `exit_code`, `success: false`, and the message in `stderr` — still
prefixed `[ERROR]:`, so anything reading logs recognises it.

This is what lets the circuit breaker count a timeout or a blocked command at
all. `tests/unit_tests/agents/test_command_tool_guards.py` pins both directions:
that the new payloads are detected, and that a bare `[ERROR]:` string still is
not, so the defect cannot return unnoticed.

A user cancel goes through the same helper. A cancel is a user action rather than
a tool fault, but it is still not a success, and leaving it as the one path the
detector reads differently is how "every failure path is legible" quietly stops
being true — an agent re-issuing a command the user keeps cancelling is stuck.

### 3.3 The circuit breaker is on

`execution_guard.circuit_breaker.enabled` is now **true**.

A guard that ships disabled protects nobody. The thresholds are what tunes
sensitivity; the switch only decided whether any of it ran, and in the incident
it decided no.

**Changing the template default is not enough on its own**, and that is worth
spelling out because it is invisible. `migrate_config_from_template` deep-merges
the template into the user's config and keeps the user's value for every key the
template already had — and this key has been in the template since 2026-06. Every
existing install carries an explicit `false` and would keep it, while the two new
thresholds, being new keys, *would* be added: a config that looks configured with
the guard switched off. `_migrate_circuit_breaker_default_enabled` moves that
value, and only that value.

A `false` alone cannot say whether it is the old default or a decision, so the
new thresholds are the marker. They ship in the same template change that flipped
the default, so a config that already carries them has been merged against the
post-incident template at least once and its `enabled` survived that merge. Such a
config is left alone, which is what makes the documented opt-out hold: write
`enabled: false` and it stays off across upgrades.

### 3.4 Identical repeats are cut, in two stages

`identical_repeat_threshold`, default **3**, floor of 2 — logs a warning.
`identical_repeat_abort_threshold`, default **5**, always above the first — ends
the run.

The rule is stricter than `generic_repeat`: same tool, same arguments, **and the
same result**. A tool returning something new each call is making progress even
when its arguments repeat; one returning identical bytes is not.

**The abort additionally requires every call in the streak to have failed.**
Identical *successful* results are the signature of legitimate polling — five
`git status` on a clean tree, a readiness check against a service that is not up
yet — and those are indistinguishable from the incident on tool, arguments and
result alike. Failure is the only thing that separates them, so the warning counts
either kind and only the abort insists. Setting either threshold to `0` disables
that stage; without that, zeroing one fell into the clamp and made the rule
*stricter* than the default.

Two stages because the low threshold is otherwise indiscriminate. Polling an
unfinished build three times is also identical tool, arguments and result, and is
not a fault — for the first few rounds a deterministic failure and legitimate
waiting have the same signature. The gap between the two thresholds is what a
legitimate poller survives.

On the incident's timeline the run ends at call 5 — before the loop compactor ran
at 7, and four calls before the person who intervened at 9.

Two details about what counts as a repeat. `description` is already excluded from
the argument hash for `bash`, so the rule matches the incident's calls even though
the model's narration changed between calls 1, 2 and 3. And calls to *other* tools
between attempts are skipped rather than ending the streak — a model that writes a
todo or reads a file between identical attempts is doing the same thing, and
requiring strict consecutiveness made one interleaved call enough to hide the
pattern completely. A different call to the *same* tool is real variation and does
end it. The skipping is bounded *per gap*, so a repeat from far back in a session
cannot be stitched onto a recent one. Per gap rather than per streak, because a
budget spent once for the whole walk caps the count at roughly the budget itself:
with two interleaved calls per attempt the rule would warn forever and never
reach the abort.

### 3.5 What enabling the breaker wakes up

Three detectors were inert before §3.3 and are now live, and none of them was
tuned with the payload change in §3.2 in mind — which makes more paths count as
errors than when their thresholds were chosen.

The one worth knowing about is `unknown_tool_repeat`: the name is historical, and
it counts consecutive *erroring* calls to one tool whether or not that tool
exists. Ten failing `pytest` runs in a debugging stretch is a normal thing for an
agent to do, and at the default of 10 that now ends the run. A success anywhere in
the streak resets it. Its message no longer calls a registered tool "unknown",
which it did.

`ping_pong` and `global_breaker` need 20 and 30 rounds respectively and are far
less likely to be reached. All three now have tests; they had none.

Two related changes:

- **A critical detection now finishes with `result_type="error"`, not `"answer"`.**
  Cutting a stuck run is a failure. Reporting it as an answer is how a run that
  never worked ends up looking like one that did.
- **Warnings stay in the log, deliberately.** An earlier version of this work
  pushed a steering message at the warning stage, reasoning that a warning only
  the log can see cannot change the next turn. Replaying the incident says the
  opposite, and §5 has the numbers: `push_steering` lands as a message appended
  *after* the failing tool result, and on the model that suffered the incident
  that is enough to stop it recovering. The abort is what stops a run; the warning
  is for whoever reads the log.

## 4. What this does not fix

**The Git defect is upstream.** These guards contain it; they do not repair it.
It reproduces without an agent and is worth reporting to git-for-windows.

**The model still chose to repeat.** The failure was explicit, structured and
identical eight times; alternatives satisfying the same request existed
(`--date=short` plus formatting) and were never tried; `ask_user` was registered
and never called; and from call 8 it had been told, in Chinese, to break out of
the loop. Nothing here excuses that — it bounds the damage.

Nor is it explained. Replaying the incident with 1, 2, 3, 5 and 8 identical
failures already in context — the request verbatim, the two-field `bash` schema,
the tool-result rendering the product stored — the model varied 52 times out of
52. The behaviour does not reproduce outside production, which is the argument
for guards that do not depend on predicting it.

**Nothing bounds what a single command may consume.** This is the largest gap
left, and it is deliberate rather than overlooked. An earlier version of this
change added a per-command memory ceiling — poll the process tree with psutil,
terminate above a limit — and it was removed.

The reason is that no comparable tool does it. Cloud sandboxes (E2B, Daytona,
Modal, Vercel) bound the whole environment at the container or microVM level and
never look at individual commands; local agents (Claude Code, Cursor, Aider) bound
wall clock alone. A per-command RSS ceiling would have been a mechanism this class
of tool has no precedent for, tuned on a single incident, with a false-positive
surface across every heavy build the agent is asked to run.

Where it belongs instead is `jiuwenbox`, which already models
`cgroup.memory_max` / `cpu_max` / `pids_max` per sandbox — and where all three
default to `null`, meaning no limit. That is the same "guard ships disabled"
pattern that kept the circuit breaker out of this incident, one layer down, and
it deserves its own issue.

Until then, the first invocation of a runaway command is unbounded. The repeat
rule reduces nine attempts to five; it does nothing about the first.

**Process-tree reaping is still weaker on Windows than on POSIX.** §3.1 closes the
case where the survivor is enumerable, but psutil can only kill what it sees at
that moment, so a tree racing to spawn can leave something behind, and there is no
job object making the teardown atomic. `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` is
the right primitive and would also give a per-command memory bound for free on
that platform; it needs native code and a Windows machine to test.

**Tree reaping can still surprise, and that is intentional on teardown.** §3.1
runs only when the runtime has already decided to stop an invocation — timeout,
user cancel, a `communicate()` hang (for example a grandchild holding stdout open
after ``cmd &``), or a harness `bash` session kill — not on a normal exit 0. In
those paths it kills every descendant psutil can enumerate, including background
jobs started with ``&`` in the same command, parallel build workers (`make -j`,
`pytest -x` workers), or a `nohup` child that has not detached from that tree.
That matches what `killpg` already did on POSIX: cancel means cancel the whole
tree, not leave a runaway child holding memory on Windows. It does **not** bound
the incident's OOM shape, where each call finished in ~18s without cancel. A
command that forks a long-lived daemon and exits immediately on the success path
is unaffected unless a later teardown targets its session.

**Thresholds remain blind to cost.** Three identical calls is right for a cheap
command and still generous for one taking 49 GB. A guard that consulted what the
previous attempt cost would be better; it does not exist yet.

**Detection is after the fact.** The count is taken in `after_tool_call`, so the
call that trips the threshold has already run and already cost whatever it costs.
`before_tool_call` exists on the same rail and would let the repeat be refused
instead of counted; that is a larger change than this one.

## 5. Why nothing here talks to the model

The obvious missing guard is the one that tells the model it is looping. This work
shipped it at first and then removed it, because replaying the incident measured
it doing harm.

The replay rebuilds the context production held at call 8 — the post-compaction
state, validated against the compaction event's own message census — and varies
only the last message. On `Gemma4-26B`, the model the incident ran on:

| last message | outcome |
| --- | --- |
| none: the failing tool result is last | **recovers 10/10** |
| a steering message after it (`push_steering`'s shape) | repeats 8/20 |
| any `assistant` message after it | **repeats 10/10** |

The content does not matter. A neutral note stating the repository is clean
produces the same 10/10 repetition as an explicit instruction to break out of the
loop. While the failure is the last thing in the context the model answers the
failure; once anything follows it, the failure is no longer in the position the
model responds to.

`ctx.push_steering` resolves to `_admit_user_message(..., prefix="[STEERING] ")`,
which is exactly that shape. So a warning that "reaches the model" makes the
incident more likely on the model that suffered it, and the rail no longer sends
one.

Two limits on this. It is one model — `deepseek-v4-flash` recovers 10/10 on every
arm, including the production one — and it is 10 trials per cell. It is enough not
to ship a mechanism whose only measurement says it backfires; it is not enough to
conclude anything general about steering.

The upstream half is out of scope here: `ReasoningToolLoopCompactProcessor` in
agent-core appends exactly such a message, which is what the replay above
reproduces. That belongs in an agent-core issue, not in this repository.
