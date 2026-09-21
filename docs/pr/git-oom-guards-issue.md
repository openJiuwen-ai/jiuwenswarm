# Issue: Agent repeats identical failing shell commands with no runtime containment

## Summary

On **2026-08-02**, a single `bash` call running:

```bash
git log -1 --format=%ad --date=format:'%m月%d日'
```

reached roughly **8.5 GB working set / ~49 GB private memory** per invocation on **Git for Windows**, returned `exit 128` / `fatal: Out of memory, realloc failed`, and the agent **reissued the same call nine times in ~2m34s** before a human stopped the run.

The Git allocation defect is **upstream** (reproduces without an agent; works on glibc). This issue tracks the **JiuwenSwarm runtime gaps** that let one external fault become nine.

## Impact

- **Resource exhaustion** on the host (Windows especially): each call can complete quickly with OOM; wall-clock timeout does not help when the command finishes.
- **No automatic stop** despite explicit, byte-identical tool failures.
- **Existing circuit-breaker thresholds were tuned but inactive** (`execution_guard.circuit_breaker.enabled: false` on existing installs).

## Root causes (runtime)

| Gap | Effect |
| --- | --- |
| Tool failures returned bare `[ERROR]: …` strings | `ToolResultErrorDetector` counted them toward **no** streak |
| `generic_repeat` matched tool+args only, **warning @10** | Incident signature (same tool, args, **and result**) had no critical path |
| Circuit breaker **disabled by default** | No detector ran |
| `terminate_shell_process` on Windows kills the **shell wrapper**, not the allocating descendant | Timeout/cancel can report success while the child keeps running |

## Scope of this issue

**In scope (containment in the agent harness):**

- Make command/bash failure payloads legible to the execution guards.
- Enable the circuit breaker by default and wire new thresholds through config → adapter → rail.
- Add **`identical_repeat`**: same tool + args + result; warn @3 (log only), abort @5 (force-finish, `result_type=error`); abort requires **failing** repeats so healthy polling is not cut.
- Reap process **descendants** on kill/timeout/cancel for both **`bash`** (primary shell tool in the incident) and `mcp_exec_command`.
- Document incident, decisions, and remaining gaps (`docs/en/CommandExecutionGuards.md`, `docs/zh/命令执行防护.md`).

**Explicitly out of scope (follow-ups):**

- Fixing Git for Windows.
- Per-command RSS ceiling in the harness (peers bound wall clock; sandboxes/jiuwenbox use cgroup — separate issue).
- Steering messages at warning level (measured to harm recovery on the incident model; see docs §5).
- Job Object / native Windows memory limits in agent-core.

## Acceptance criteria

- [ ] Timeout, safety-block, cancel, and start failures return structured JSON failures detectable by `ToolResultErrorDetector`.
- [ ] `execution_guard.circuit_breaker.enabled` defaults to **true**; installs predating the change are migrated on config merge, while a `false` written against the new template is left alone (see implementation note in PR).
- [ ] Five consecutive **identical failing** `bash` calls abort the run before a human must intervene (default thresholds 3 warn / 5 abort).
- [ ] Five consecutive **identical successful** calls (e.g. polling `git status`) are **not** aborted.
- [ ] After timeout/cancel, no grandchild process started by the command remains alive (POSIX test; Windows path via psutil descendant reap).
- [ ] Unit/e2e tests cover the above and guard against false positives (polling, changing results, config wiring seams).

## References

- Design doc (to land with fix): `docs/en/CommandExecutionGuards.md`
- Config keys: `execution_guard.circuit_breaker.identical_repeat_threshold` (default `3`), `identical_repeat_abort_threshold` (default `5`; `0` disables a stage)

## Labels (suggested)

`bug`, `execution-guard`, `reliability`, `windows`
