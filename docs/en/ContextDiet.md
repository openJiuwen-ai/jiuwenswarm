# Context Diet (Code mode)

> Keep tool output out of the model window: trim shell / grep / file-read results when they are ingested, and replace old results with one-line summaries as the turn grows. Configured under `modes.code.context_diet`; Chinese version: [上下文瘦身](../zh/上下文瘦身.md). Related: [Context Compression](ContextCompression.md) covers agent-core's threshold-based compressors that this feature builds on.

## 1. Why

A 10-task SWE-bench Pro pilot (deepseek-flash, 20 headless runs) measured where Code-mode tokens go:

| Measurement | Value |
|---|---|
| Median input tokens per model call | 58.7k (max 151k) |
| Fixed part of every call (system prompt 10.6k + skills 2.0k + tool schemas 13.2k) | 25.8k |
| Tool results in the window, median per call | 32.7k (max 125k) |
| Tool-result text produced per run | 2.42M chars over 902 calls |
| Share of that text by tool | `read_file` 54%, `bash` 41%, `grep` 4%, edits <1% |
| Median / p90 size of one `read_file` result | 3.0k / 19k chars |
| Median / p90 size of one `bash` result | 1.1k / 3.3k chars (max 60k) |

Every tool result is re-sent on every following call, so a 20k-character test log read once costs its tokens 30 more times before the turn ends. agent-core only compacts when the window nears its limit or when four identical calls repeat (`tool_loop_compact`), and its `ToolResultWindowProcessor` is not wired into JiuwenSwarm. Context diet closes that gap at two points.

## 2. How it works

```
tool runs ──► ToolMessage ──► [ContextDietTrimRail.after_tool_call] ──► context engine
                                  │ head/tail/signal excerpt                 │ on_add_messages
                                  │ full text spilled to disk                ▼
                                  ▼                              [ContextDietShellWindowProcessor]
                  <agent workspace>/context_diet/<session>/…     [ContextDietReadWindowProcessor]
                                                                   older results → one-line summary
                                                                   + [[OFFLOAD: handle, path]]
```

### 2.1 Ingestion trimming — `ContextDietTrimRail`

* Hook: `after_tool_call`. agent-core lets rails rewrite `ctx.inputs.tool_msg`, the `ToolMessage` that enters the context; the structured `tool_result` used by the UI stream and by other rails (execution discipline, completeness) is left untouched.
* Shell (`bash`): when the output exceeds `bash_max_lines` **or** `bash_max_chars`, keep the first `bash_head_lines`, the last `bash_tail_lines` (the `Exit Code` line lives there) and up to `max_signal_lines` middle lines that look like failures, each prefixed with its original line number. Selection is two-pass: lines matching a *strong* pattern (error, fail, traceback, exception, assert, panic, fatal, denied, not found, pytest `E    ` lines, traceback frames, non-zero exit codes) fill the budget first; *weak* lines (warning, note, deprecated, skipped) only take what is left, so 300 warnings can never crowd out 3 errors and `PASSED` lines are not signal at all. If strong lines exceed the budget, a marker says how many more failures are in the spill file. Everything else is replaced by one marker line naming the omitted line count and the spill path. Few-but-huge lines (minified output, one-line JSON) are clipped per line instead.
* Shell commands that print file contents or diffs (*source prints*) are treated like `read_file` and kept whole: `cat file`, `head`/`tail file`, `sed -n '1,60p' file`, NR-range `awk`, `git diff` / `git show` / `git log -p` / `git blame`, `diff a b`, `grep`/`rg` with five or more context lines, and Python that prints `inspect.getsource(...)`. The check is lexical (`context_diet/source_print.py`): a pipeline counts only when it starts with such a viewer and every later stage only selects a line range (`| head`, `| sed -n 'N,Mp'`), so `cat x | grep y` is still trimmed. A command that also runs tests or a build (`pytest`, `python -m pytest`, `npm test`, `make`, `go test`…) is not exempt, because the test log is what trimming is for. Logs and scratch files are not source: files the same command wrote (`pytest > /tmp/out.log; sed -n '1,40p' /tmp/out.log`), paths under `/tmp`, `*.log`, and the diet's own spill files. Above `source_print_max_lines` (default 2,000) the first lines are kept and the rest spilled; a source print with a line longer than 2,000 characters (minified code, one-line JSON) is clipped like other shell output. `trim.exempt_source_prints: false` restores the earlier behaviour.
* `grep` / `glob`: more than `grep_max_lines` result lines → first `grep_head_lines` lines plus a count and the spill path.
* `read_file` (opt-in, `read_file_max_lines > 0`): more lines than the limit → the first `limit` lines plus a resume hint `read_file(offset=N)`. A read where the model already passed `limit` is never touched. Nothing is spilled: the file is on disk.
* Spill files: `<agent workspace>/context_diet/<session>/<tool>_<call id>.txt`, or `trim.spill_dir`. The agent workspace is the per-agent root from the cwd ContextVar (`get_workspace()`, `~/.jiuwenswarm/agent/workspace` by default, moved by `JIUWENSWARM_DATA_DIR`), which is one of the three roots Code mode's filesystem sandbox (`restrict_to_sandbox`) accepts, so the `read_file` / `grep` hint in the marker works. If the agent's workspace coincides with the project root or the shell cwd, the shared agent workspace directory is used instead: nothing is ever written inside the project. Each session directory is capped at `trim.spill_max_bytes` (default 50 MB); the oldest files are deleted first, the file just written is always kept.
* Pure helpers live in `jiuwenswarm/agents/harness/code/context_diet/trim.py` and work on plain strings, so they can be replayed offline over recorded transcripts.

### 2.2 Mid-turn compaction — window processors

* Two subclasses of agent-core's `ToolResultWindowProcessor`, registered with `ContextEngine.register_processor()` and appended to the Code profile's `ContextProcessorRail` by the adapter (`_build_context_processor_rail`). The preset compressor chain is unchanged.
* Once more than `keep_last_k` results of the managed tools are in the window, the oldest are rewritten. Small ones (`< min_offload_chars`) become a one-line summary in place; larger ones are offloaded through agent-core's offload store (session `offload/` directory, or in-memory when no workspace) and replaced by the summary plus an `[[OFFLOAD: handle=…, type=…, path=…]]` handle. The existing "Context Compression and Recovery" prompt section already tells the model how to reload a handle.
* Summary format (`summarize_tool_result`): `bash \`pytest -q\` → exit 1; FAILED tests/test_x.py::test_y`, `grep /def foo/ src → 12 lines`, `read_file src/app.py (L1–L400)`.
* `edit_file` / `write_file` results are never in `tool_names`, so edit history is preserved verbatim.
* Shell source prints (see §2.1) stay out of the shell window when `compaction.exempt_source_prints` is on (the default), matching the default `read_keep_last_k: 0` for `read_file`.

### 2.3 Prompt de-duplication

The system prompt was captured with `context_engine_config.enable_context_debug: true` (dump `context_debug/llm_request.jsonl`) and split by heading. No sentence of 60+ characters appeared twice, but three JiuwenSwarm sections restated tool descriptions the model already receives: the todo sizing rules (`todo_create` carries them), the "working directory persists" bullet (in the `bash` description) and the delegation paragraph (agent-core's *When to delegate* section and the `subagent_spawn` description). Those were cut.

| First model call, Code mode (this branch, real dump) | Before | After |
|---|---|---|
| System prompt | 37,568 chars ≈ 10.7k tokens | 36,921 chars ≈ 10.5k tokens |
| Tool schemas (21 tools) | 46,779 chars ≈ 13.4k tokens | unchanged |

The remaining fixed cost sits in agent-core-owned text and is listed in §6 as proposals: the `task_tool` schema alone is 8,751 chars (7,341 of them the description, which enumerates every Playwright capability), `bash` 7,651 (description 4,826), the three progressive-tool prompt sections 3,743. Method: JSON-serialised entries of the `tools` array and the description fields from the same first-call dump; prompt sections measured heading to heading.

## 3. Configuration reference

All keys live under `modes.code.context_diet`. `preset` selects a named set of defaults; any key written explicitly under `trim` / `compaction` wins over the preset. Missing keys take the preset value; out-of-range values are clamped. The shipped templates set only `preset`, the switches and the spill settings, and list the tunables as comments, so changing the preset is one line.

| Key | `default` preset | `aggressive` preset | Meaning |
|---|---|---|---|
| `preset` | `default` | — | `default` (recommended for interactive and headless use) or `aggressive` (experimental, opt-in; roughly doubles re-reads but lowers input per call, see §3.1). Unknown values fall back to `default`. |
| `trim.enabled` | `true` | `true` | Mount the trim rail. `false` = tool results enter the context verbatim (pre-feature behaviour). |
| `trim.bash_max_lines` | `45` | `30` | Shell output above this many lines is trimmed (min 20). |
| `trim.bash_max_chars` | `2500` | `1800` | …or above this many characters (min 1000). |
| `trim.bash_head_lines` | `20` | `15` | Lines kept from the start (min 5). |
| `trim.bash_tail_lines` | `15` | `12` | Lines kept from the end (min 5); the exit code lives here. |
| `trim.keep_signal_lines` | `true` | `true` | Keep middle lines that look like failures, strong signals (error / fail / traceback / assert…) first. |
| `trim.max_signal_lines` | `25` | `20` | Cap on kept signal lines. |
| `trim.grep_max_lines` | `40` | `30` | grep / glob results above this are cut… |
| `trim.grep_head_lines` | `30` | `25` | …to this many leading lines. |
| `trim.read_file_max_lines` | `0` | `250` | `0` = never trim reads. |
| `trim.exempt_source_prints` | `true` | `true` | Keep shell commands that print file contents or diffs whole, like file reads (§2.1). `false` = earlier behaviour. |
| `trim.source_print_max_lines` | `2000` | `2000` | Safety cap for those outputs: longer ones keep their first lines and spill the rest. `0` = never trim them. |
| `trim.spill_dir` | `""` | `""` | Where full outputs go. Empty = `<agent workspace>/context_diet/<session>` (inside the sandbox, outside the project). |
| `trim.spill_max_bytes` | `52428800` | `52428800` | Per-session cap on spilled bytes (50 MB); oldest files deleted first. `0` = no cap. |
| `compaction.enabled` | `true` | `true` | Register the window processors. |
| `compaction.shell_keep_last_k` | `4` | `3` | Newest bash / grep / glob results kept verbatim. `0` = off. |
| `compaction.read_keep_last_k` | `0` | `6` | Same for `read_file`. `0` = off. |
| `compaction.min_offload_chars` | `800` | `600` | Smaller old results are summarized in place (no offload file). |
| `compaction.summary_chars` | `240` | `240` | Maximum length of the summary line (80–2000). |
| `compaction.exempt_source_prints` | `true` | `true` | Keep shell source prints out of the shell window. `false` = earlier behaviour. |

A trim is dropped when the trimmed text would not be shorter than the original (the marker and spill path can cost more than a few removed lines); the result then enters the context unchanged and the rail logs `[ContextDiet] <tool> result kept whole: skipped=trim_not_shorter` at DEBUG.

Why `read_file` is off in `default`: the file the model has just read is usually the one it edits next, and a shortened or summarized read can change the `old_string` it produces. The first release assumed that shell and search output are observations, not edit sources. That was wrong for shell: in the 2026-10-02 benchmark, 35 of the 79 shell results the default preset trimmed came from commands that print file contents or diffs (`sed -n`, `cat`, `tail FILE`, `git diff`, `inspect.getsource`, `grep -A 30`). Those are now kept whole like file reads (§2.1). Test logs, build output and plain searches are still trimmed; their full text is always one `read_file` away.

### 3.1 Where the defaults come from

The first release used 120 lines / 6000 characters for shell output and 150 lines for grep. In the wave-1 SWE-bench Pro A/B run (10 instances, deepseek-flash) those fired on 13 of 475 tool calls: almost every result was below them. The `default` preset is fitted to the tool results of that run's base arm:

| Tool | Results | Chars p50 / p75 / p90 / max | Lines p50 / p75 / p90 / max |
|---|---|---|---|
| `bash` | 156 | 1,419 / 3,349 / 5,786 / 25,364 | 26 / 60 / 119 / 407 |
| `grep` | 32 | 2,138 / 4,564 / 6,650 / 25,306 | 15 / 75 / 123 / 263 |
| `read_file` | 101 | 4,450 / 10,766 / 31,772 / 60,000 | 80 / 228 / 621 / 1,372 |

Replaying those same results through the trimmer:

| Settings | `bash` trimmed / chars saved | `grep` trimmed / chars saved | `read_file` trimmed / chars saved |
|---|---|---|---|
| first release (120 / 6000 / 150) | 16 of 156 / 16% | 3 of 32 / 23% | 0 of 101 / 0% |
| `default` preset | 57 of 156 / 37% | 11 of 32 / 62% | 0 of 101 / 0% |
| `aggressive` preset | 75 of 156 / 44% | 13 of 32 / 66% | 10 of 101 / 18% |

`read_file` results carry the largest volume (1.06M characters in that arm against 0.42M for bash), which is why the headless preset trims them; interactive use keeps them whole. The compaction window drops from 8 to 4 shell results because a typical task makes about 16 shell calls, so a window of 8 rarely engaged.

**First measurement (2026-09-28), superseded below.** In a 20-instance SWE-bench Pro run (deepseek-flash, gate on, hermetic evaluation) the arm using the `aggressive` preset re-read files 1.7 to 2.3 times as often as two identical baseline runs (267 re-reads against 116 and 150; re-read share 71% against 58–62%), and read_file calls rose from 201–242 to 376. The baseline repeat showed that total tokens and continuation-cap hits vary too much between identical 20-instance runs to attribute a token change to the preset; the re-read increase is well outside that noise. The cause is the read_file window: once older read results are folded into one-line summaries, the model reads the files again, and the extra reads trip the implement gate's explore cap. read_file trimming itself fired only 13 times. The preset stays available for experiments; `default` (read_file untouched, shell/grep windows only) is the recommended setting, including for headless runs.

**Caveat (real-shape re-check, 2026-09-29):** that run used an implementation with a bug. agent-core passes tool arguments as the model's raw JSON string, and the trim rail kept them only when they were a dict. So a `read_file` call that asked for an explicit window of more than 250 lines was trimmed anyway, and the re-read hint lost the file path. Between 7 and 26 such reads per benchmark arm were affected, which likely added some of the measured re-reads. The bug is fixed and the preset was re-measured.

**Re-measured after the fix (2026-09-30).** Same 20 instances, deepseek-flash, gate on, hermetic evaluation, against a baseline run the same day from the same image:

* 31 explicit `read_file` windows over 250 lines were requested and none was trimmed, so the fix holds in real runs.
* Re-reads still rise sharply: 318 against 147 (share 74% against 57%). That part of the first measurement stands.
* Input per model call falls: 0.78x paired per instance (95% CI 0.70–0.87), lower on 18 of 20 tasks.
* Net tokens did not rise: 0.91x paired (CI 0.71–1.15), 0.79x in total; cost $6.73 against $8.51. Resolved 14 against 13, with no baseline-solved task lost. At 20 instances these are within noise.

So the preset is not harmful in this benchmark: it trades more re-reads for a smaller per-call context, and its net effect was no worse than `default` and possibly cheaper. It stays experimental and opt-in; `default` remains the default. Whether it should become the headless default needs a larger run.

Experimental, opt-in (see the re-measurement above):

```yaml
modes:
  code:
    context_diet:
      preset: aggressive
```

Off entirely (A/B "off" arm):

```yaml
modes:
  code:
    context_diet:
      trim: {enabled: false}
      compaction: {enabled: false}
```

## 4. How to confirm it is working

* Agent-server log at start-up:
  * `[JiuwenSwarmCodeAdapter] ContextDietTrimRail create success (preset=default bash_max_lines=45 …)` or `… disabled by modes.code.context_diet.trim.enabled`
  * `[JiuwenSwarmCodeAdapter] context-diet compaction processors added: ContextDietShellWindowProcessor(keep_last_k=4, tools=[…])` (`keep_last_k=3` with the `aggressive` preset), followed by agent-core's `context processors initialized: … ContextDietShellWindowProcessor=…`
* Per kept source print: `[ContextDiet] kept bash source print whole (sed -n, 61 lines)`.
* Per rewrite: `[ContextDiet] trimmed bash 18342→2210 chars (omitted 402 lines, kept 3 signal lines, spill=~/.jiuwenswarm/agent/workspace/context_diet/<session>/bash_call_3.txt)`
* In the transcript the model sees lines starting with `[context-diet]`; the metadata of the rewritten `ToolMessage` carries `context_diet: {kind, original_chars, trimmed_chars, omitted_lines, kept_signal_lines, spill_path}`, and compacted messages carry `context_diet_summary`.
* `context.usage` events (`parts.messages.tokens`) shrink accordingly.
* Spilled files: `ls ~/.jiuwenswarm/agent/workspace/context_diet/<session>/`; ask the agent to `read_file` one of them to confirm the sandbox lets it through.

## 5. Limitations

* Trimming happens after the tool ran: the tool's own cost (time, disk) is unchanged; only the model-facing text shrinks.
* The signal-line regexes are heuristic (English test-runner and compiler vocabulary plus `E    `-style pytest lines). A failure phrased differently may land in the omitted middle; it is still in the spill file and the tail usually carries the summary line. When more strong lines exist than `max_signal_lines`, the marker says so.
* The source-print check is lexical. It does not see file viewing through variables, loops over `$(…)`, `xargs cat`, `find -exec cat` or scripts, and a log with an ordinary name inside the project (`out.txt`) counts as a file. A command that mixes a file view with a test run is trimmed as a test run. On the benchmark transcripts it matched all 35 hand-labelled source prints among the 79 trimmed shell results with no false positives, and 13% (1,156 of 8,569) of all distinct shell commands from 25 runs.
* Compaction summaries are extractive, not model-generated, so they are cheap but terse. The `[[OFFLOAD]]` handle reloads the full text when needed.
* The two windows are global per tool group, not per file: with `read_keep_last_k: 6` (the `aggressive` preset), the 7th distinct read pushes out the oldest even if it is the file being edited. Keep `read_keep_last_k` generous or leave it off for interactive use.
* Spill files hold full command output in plain text (environment dumps included). They stay under the user data directory, capped per session by `trim.spill_max_bytes`; there is no time-based expiry.

## 6. Proposals for agent-core (not part of this change)

Measured on the first call of a Code-mode turn:

1. `task_tool`: schema 8,751 chars, description 7,341, most of it the per-capability Playwright tool list generated by `SubagentRail`. A one-line-per-subagent summary with details available through `tool_search` would save ~2k tokens per call.
2. `bash`: schema 7,651 chars, description 4,826, including Windows / PowerShell guidance that could be conditional on the host OS.
3. Progressive-tool prompt sections (*Deferred tools available through tool_search*, *tool_search usage rules*, *Dynamic Tool State Priority*): 3,743 chars on every call (1,891 + 982 + 870); the deferred directory could list names only.
4. `ToolResultWindowProcessor` could accept a per-tool `keep_last_k` map so hosts do not need one subclass per window.
