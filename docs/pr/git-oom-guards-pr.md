<!--  Thanks for sending a pull request!  Here are some tips for you:

1) If this is your first time, please read our contributor guidelines: https://gitcode.com/openJiuwen/openJiuwen/blob/master/CONTRIBUTING.md

2) If you want to contribute your code but don't know who will review and merge, please add label `openJiuwen-assistant` to the pull request, we will find and do it as soon as possible.
-->

**What type of PR is this?**
/kind bug


**What does this PR do / why do we need it**:

Fixes #\<ISSUE_NUMBER\> — runtime containment after the **2026-08-02 Git OOM incident**.

### Incident

On Windows, the model invoked **`bash`** with:

```bash
git log -1 --format=%ad --date=format:'%m月%d日'
```

On **Git for Windows** that command allocates without bound (~**8.5 GB working set / ~49 GB private bytes** per call), returns `exit 128` / `fatal: Out of memory, realloc failed` in ~18s, and **completes** — so the 300s wall-clock timeout never fires. The agent then **reissued the same call nine times in ~2m34s** before a human stopped the run. The model (`Gemma4-26B`) emitted no text across all nine turns.

The Git allocation defect is **upstream** (reproduces without an agent; the same command succeeds on glibc / `git 2.43.0`). This PR closes **JiuwenSwarm gaps** that turned one external fault into nine.

### Root causes (runtime)

| Gap | Effect in the incident |
| --- | --- |
| Timeout / safety / cancel paths returned bare `[ERROR]: …` strings | `ToolResultErrorDetector` counted them toward **no** streak |
| `generic_repeat` matched tool+args only, **warning @10** | Same tool + args + **result** had no critical path |
| `execution_guard.circuit_breaker.enabled: false` | Every tuned threshold sat idle |
| Windows `terminate_shell_process` kills the shell wrapper only | Timeout/cancel can report success while the allocating descendant keeps running |

### What this PR changes

1. **Legible failure payloads** — timeout, safety-block, cancel, and start failures in `command_tools.py` now return the JSON shape the guards already expect (`success: false`, non-zero `exit_code`, message in `stderr` via `_failure_payload`).

2. **Circuit breaker enabled by default** — `execution_guard.circuit_breaker.enabled: true` in shipped `config.yaml`, plus `_migrate_circuit_breaker_default_enabled` in `config.py` so deep-merge does not leave old installs with `enabled: false` while new thresholds appear configured. The new threshold keys date a config to the post-incident template, so a `false` written alongside them is treated as deliberate and left alone.

3. **`identical_repeat` detector (two stages)** — same tool + args + **result**:
   - `identical_repeat_threshold: 3` → **warning log only** (no steering — replay on the incident model showed harm; see docs §5)
   - `identical_repeat_abort_threshold: 5` → **force-finish** with `result_type=error`
   - Abort counts **failing** repeats only (healthy polling with identical success is not cut). Either threshold `0` disables that stage.
   - Interleaved calls to *other* tools do not reset the streak (bounded skip window).

4. **Process-tree reaping** — `reap_process_descendants` wired into **`bash`** (`bash_tool_safety` patches agent-core `AsyncProcessHandler._kill_process_tree`) and `mcp_exec_command` (`_terminate_command_tree`). Parent-first kill order; psutil descendant sweep on timeout/cancel/exception. **The incident used tool name `bash`.**

5. **Smaller rail fixes** — critical detections finish as `error` not `answer`; config → `interface_deep.py` → `CircuitBreakerRail` wiring for new thresholds.

### Deliberately out of scope

- Fixing Git for Windows.
- Per-command RSS ceiling in the harness (removed after review — peers bound wall clock; sandboxes/jiuwenbox use `cgroup.memory_max`; see `docs/en/CommandExecutionGuards.md` §4).
- Warning-level steering to the model (shipped then removed — replay on `Gemma4-26B`: recovery 10/10 when failure is last message, repetition 8–10/10 when any assistant/steering message follows).
- Job Object / native Windows memory limits in agent-core.

### Remaining gap

The **first** runaway invocation is still unbounded. This change cuts repetition (**9 → 5** abort by default), not peak memory per call. Tree reaping applies only on teardown (timeout/cancel/session kill), not on normal exit 0 — documented in docs §4.

Full design rationale: `docs/en/CommandExecutionGuards.md`, `docs/zh/命令执行防护.md`.


**Which issue(s) this PR fixes**:

Fixes #\<ISSUE_NUMBER\>


**What scenarios were tested, and what were the verification results（Function, performance, reliability, etc.）**：

### Automated tests

```bash
pytest tests/unit_tests/agents/test_command_tool_guards.py \
       tests/unit_tests/agents/test_circuit_breaker_identical_repeat.py \
       tests/unit_tests/agents/test_command_guards_end_to_end.py \
       -q --tb=short --no-cov
```

**Result:** **37 passed**, 0 failed (~71s).

| Area | File | What is verified |
| --- | --- | --- |
| **Function** — failure payloads visible to guards | `test_command_tool_guards.py` | `_failure_payload` JSON vs bare `[ERROR]:` strings; timeout/safety/cancel paths |
| **Reliability** — descendant reaping | `test_command_tool_guards.py` | Grandchild does not outlive command; tree reaper does not rely on process group; bash path reaps too |
| **Function** — identical repeat warn/abort | `test_circuit_breaker_identical_repeat.py` | Warn @3 log-only (no model context); abort @5 force-finish; `result_type=error`; failing-only abort; polling/success not cut; interleaved tools; threshold clamp/disable |
| **Reliability** — config wiring end-to-end | `test_command_guards_end_to_end.py` | Shipped config enables breaker; thresholds survive config → adapter → rail; migration flips old `false` default; real subprocess repeating failure is cut; real progress is not |

**Environment:** Linux (WSL2), Python 3.11.15, `jiuwenswarm` 0.2.4.beta4, `openjiuwen` 0.1.16 @ `709eb5d2`, psutil 7.2.2.

### Manual smoke (optional)

- Five consecutive identical **failing** shell commands → run aborts at default threshold.
- Five consecutive identical **successful** `git status` polls → no abort.

We do **not** attempt to reproduce the Git for Windows OOM in CI (upstream Git defect; Linux/glibc does not trigger it).

### Compatibility / risk

| Change | Risk | Mitigation |
| --- | --- | --- |
| `circuit_breaker.enabled: false → true` | Wakes existing detectors (e.g. `unknown_tool_repeat` @10) | Documented in `CommandExecutionGuards.md` §3.5; set `enabled: false` to restore old behaviour |
| New `identical_repeat_*` thresholds | False positive on polling | Two-stage design; abort requires failures; tests pin polling/progress/changing results |
| Config migration | Runs on `migrate_config_from_template` / init path only | Only a `false` without the new threshold keys is moved, so a deliberate opt-out survives upgrades |
| Tree reaping on teardown | Kills background jobs / workers still in the command tree | Intentional — matches POSIX `killpg` semantics; documented §4 |

**Config surface** (under `execution_guard.circuit_breaker`): `enabled` (default `true`), `identical_repeat_threshold` (default `3`, `0` = off), `identical_repeat_abort_threshold` (default `5`, `0` = off). No HTTP/API signature changes.

**Performance:** No hot-path regression expected — circuit breaker runs `after_tool_call` with bounded history window; psutil reap is best-effort on teardown only.


**Self-checklist**:（**Please check carefully,and mark an x in the [] brackets. We will review your completion status.**）

+ - [ ] **Design**: Has the solution corresponding to the PR been reviewed by the Maintainer, and have all review comments been replied to and revised
+ - [x] **Test**: Has the code in the PR been fully covered by UT/ST test cases, and have the newly added test cases been uploaded to the repository along with this PR or already uploaded.
+ - [x] **Verification**: Does the PR description contains a detailed description of the verification results regarding the achievement of the expected goals for the Feature, Refactor, and Bugfix to this PR.
+ - [x] **Interface**: Does it involve changes to external interfaces? The corresponding changes have been approved by the interface review organization, and the annotation information for the API has been correctly refreshed. *(Config-only under `execution_guard.circuit_breaker`; no public HTTP/API change.)*
+ - [x] **Document**: Does it involve modifications to the official website documentation? If so, please submit the materials to the Doc repository in a timely manner. *(In-repo docs only: `docs/en/CommandExecutionGuards.md`, `docs/zh/命令执行防护.md`, SUMMARY entries — not website Doc repo.)*

<!-- **Special notes for your reviewers**: -->
<!-- + - [ ] Whether it causes forward compatibility failure -->
<!-- Existing installs: migration enables breaker when `enabled` was the old implicit `false`; deliberate `enabled: false` after merge is left alone. New config keys have defaults in code. -->
<!-- + - [ ] Whether the dependent third-party library change is involved -->
<!-- Uses existing `psutil` dependency already required for process management; no new third-party library. -->
