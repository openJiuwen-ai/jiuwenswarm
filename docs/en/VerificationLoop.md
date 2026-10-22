# Verification Loop (`run_tests`)

Code mode can run the project's tests through a dedicated tool, `run_tests`, instead of the model composing `pytest …` / `npm test` commands in bash and reading the raw output back. The tool picks the focused tests for the files the model changed, returns a compact result, and its outcome is recorded directly as verification evidence by the implementation completeness gate.

中文版：[验证闭环](../zh/验证闭环.md)

## 1. Why

Measured on 10 SWE-bench Pro instances (wave-1 A/B run, deepseek-flash, gate on):

| Observation | Value |
| --- | --- |
| bash calls that ran tests or Python (repo-tools arm) | 106 of ~200 |
| bash calls that searched the code | 58 |
| median input tokens per model call | 40–45k |

Test runs are the largest single use of the shell, and every run's full output enters the context. The completion gate also had to *guess* whether a shell command was a real test run by matching the command string. `run_tests` addresses both: one call runs the right tests, the model sees a bounded summary, and the gate receives a structured verdict.

## 2. How it works

```text
model ── run_tests(files=[...]) ──► RunTestsTool.invoke
                                     │
                                     ├─ discovery.discover_tests(root)       (cached; file probes only)
                                     ├─ tool.plan_runs(...)                  (tests > files-mapping > whole suite)
                                     │    └─ mapping.map_files_to_tests(root, files)
                                     ├─ runner.run_framework(fw, targets)    (argv, timeout, parse, spill)
                                     └─ runner.render_summary(results)       (≤ max_summary_chars)
                                     │
               ToolOutput(success=True, data={content, outcome, passed_languages, results})
                                     │
      ImplementCompletenessRail.after_tool_call ── evidence.parse_run_tests_evidence
                                     └─ ImplementationStateMachine.record_verification_evidence(languages, success)
      CodeExecutionDisciplineRail.after_tool_call ── same evidence → verification checkpoint cleared
```

### 2.1 Discovery (`verification/discovery.py`)

Reads project files only; never imports project code or runs a subprocess.

| Framework | Detected from |
| --- | --- |
| pytest | `pytest.ini`; `[tool.pytest…]` in `pyproject.toml`; `[pytest]`/`[tool:pytest]` in `tox.ini`/`setup.cfg`; `conftest.py`; otherwise test files named the way pytest finds them by default (`test_*.py`, `*_test.py`) anywhere up to 4 directories deep, including the project root |
| unittest | a Python project (`pyproject.toml`/`setup.py`/`setup.cfg`/`tox.ini`) with no pytest signal |
| vitest / jest | `vitest` / `jest` / `ts-jest` in `package.json` dependencies |
| npm test | a real `scripts.test` in `package.json` (the npm placeholder is ignored) |
| go test | `go.mod` |
| cargo test | `Cargo.toml` |

The test-file scan is breadth first and bounded (at most 400 directories and 4 levels deep). Like pytest itself, it skips hidden directories, `node_modules`, `venv`, `build`, `dist`, `site-packages`, `*.egg-info` and any directory with a `pyvenv.cfg`, and it never follows symlinks.

The result is cached per workspace root and invalidated when any probed file or the root directory changes. A result without a Python framework is never reused, so a test file written later in the session is found on the next call.

**Projects found only by test-file names.** When pytest was detected from file names rather than configuration, `run_tests` first checks that the project's interpreter (§2.3) can import pytest. The check is one short subprocess; a positive answer is remembered for that interpreter. If pytest is missing and the test files use `unittest`, the tests run with `python -m unittest` instead, and the summary says so. Otherwise the call returns `error` with a note naming the interpreter and how to fix it: install pytest in the project's environment, set `modes.code.run_tests.python`, or run the tests with `bash`.

When nothing is detected, the error lists everything that was checked.

### 2.2 Choosing what to run (`tool.plan_runs`, `verification/mapping.py`)

Every `tests` and `files` entry is validated first: its path part (before an optional `::node::id`) is resolved against the workspace root with symlinks followed and must stay inside it, and an entry starting with `-` is refused, so a target can never be read as a runner option. A rejected entry makes the whole call fail with a message naming it.

1. `tests` given: run exactly those ids, grouped by language (`tests/test_x.py::test_y`, `web/a.spec.ts`, `./pkg`).
2. `files` given: map each changed source file to tests:
   - the file itself when it is a test file;
   - same-name tests anywhere (`test_foo.py` / `foo_test.py`; `foo.test.ts` / `foo.spec.ts` / `__tests__/foo.ts`);
   - Go: the file's package directory (`./dir`);
   - Python: a reverse import index — test files whose `import` / `from … import` lines name the changed module (with `src/` and `lib/` layouts);
   - a language whose files map to nothing returns `no_tests` with a note asking for explicit `tests` (default); with `fallback_whole_suite: true` it runs that language's whole suite instead, and the summary says so.
3. Neither: every detected suite (the model asked for it explicitly).

The walk skips VCS, dependency and cache directories and is bounded (20,000 files; 2,000 test files scanned for imports).

### 2.3 Running (`verification/runner.py`)

- argv without a shell, never the bundled interpreter. The Python interpreter is chosen in this order: `modes.code.run_tests.python` when set, `<root>/.venv/bin/python`, `<root>/venv/bin/python`, `$VIRTUAL_ENV/bin/python`, then `python` / `python3` on `PATH`. A desktop user rarely has an activated shell, so the project's virtual environment is preferred over whatever is first on `PATH`.
- pytest runs as `python -m pytest -q -rfE --no-header --tb=short -p no:cacheprovider <extra_args> -- <targets>`; jest gets `-- <targets>` too; vitest, go and unittest take targets positionally (they are already validated not to start with `-`).
- `extra_args` is the only option channel, and it is checked against a per-runner **allow-list** (`verification/argpolicy.py`) of options that only select, filter or format the run:

  | Runner | Allowed |
  | --- | --- |
  | pytest | `-q` `-v` `-x` `-s` `-l` `--lf` `--ff` `--nf` `--sw` `--no-header` `--no-summary` `--strict-markers` `--runxfail` `--disable-warnings` `--maxfail N` `-k EXPR` `-m EXPR` `--deselect ID` `--durations N` `-r CHARS` `--tb STYLE` `--capture MODE` `--color` `-n N` `--timeout N` (and long forms) |
  | unittest | `-v` `-q` `-f` `-b` `-c` `--locals` `-k PATTERN` `--durations N` |
  | jest | `--ci` `--silent` `--verbose` `-i`/`--runInBand` `--bail` `--no-coverage` `--coverage=false` `--detectOpenHandles` `--forceExit` `--no-cache` `-t`/`--testNamePattern` `--maxWorkers` `--testTimeout` |
  | vitest | `--silent` `--no-file-parallelism` `--no-coverage` `-t`/`--testNamePattern` `--bail N` `--retry N` `--maxWorkers` `--testTimeout` |
  | go | `-v` `-short` `-failfast` `-race` `-cover` `-run RE` `-skip RE` `-count N` `-parallel N` `-cpu LIST` `-timeout D` (one or two dashes) |
  | cargo | `-q` `--release` `--no-fail-fast` `--lib` `--bins` `--tests` `--doc` `--workspace` `--all-features` `--no-default-features` `--locked` `--offline` `--frozen` `-j N` `-p NAME` `--features F` `--test NAME` `--bin NAME`, test-name filters, and after `--`: `--exact` `--nocapture` `--ignored` `--include-ignored` `--show-output` `--test-threads N` `--skip NAME` |
  | npm script | the jest or vitest list when `scripts.test` directly runs `jest` / `vitest run`; otherwise `extra_args` are refused |

  Everything else is refused, including bare positional arguments (they would be extra test paths outside the `tests` containment), `@argfile` expansion, clustered short flags unless every letter is an allowed no-value flag, and a separate value that starts with `-`. Options that load code or configuration (`-p`, `-c`, `--rootdir`, `--config`, `--setupFiles…`, `-toolexec`, `-Z`, …), write files (`--junitxml`, `--debug`, `--logfile`, coverage reports) or send output elsewhere (`--pastebin`) are simply not on any list.
- `PYTHONSAFEPATH`, `PYTHONNOUSERSITE` and `PYTHONHOME` are removed from the child environment, so a launcher that exported them cannot break `import <pkg>` from the repository root. Variables that inject runner options, plugins or wrappers (`PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `NODE_OPTIONS`, `GOFLAGS`, `RUSTC_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER`, `CARGO_BUILD_RUSTC_WRAPPER`, `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`, `RUSTDOCFLAGS`) are removed too, unless `inherit_runner_env: true`.
- The timeout is one budget for the whole call: frameworks run one after another share it, and a framework that would start with less than 5 s left is reported as `timeout` ("not started"). Output is read from a pipe the tool owns. When the test process exits, children still holding its output get 2 s, then the process group is killed and the pipe closed, so a helper left running by the tests cannot turn a finished run into a timeout. The process group is killed on every path, timeout or not; a child that moved to its own session (`setsid`) survives but loses the pipe.
- Output is bounded in memory: the first 1 MB and the last 4 MB are kept for parsing and the summary.
- agent-core wraps every tool call in a 300 s deadline unless the tool card declares its own; run_tests declares `timeout + 60 s` in `properties.resilience.timeout_s`, so its own budget, and the structured `timeout` result, always fire first.
- Full output is written to `<agent workspace>/run_tests/<time>-<framework>-<id>.log`. That directory is inside the agent workspace, which the Code-mode sandbox (`restrict_to_sandbox`) allows `read_file`/`grep` to open, and never inside the user's checkout. Each file is capped at `spill_max_bytes` (head plus tail, with a marker for the dropped middle), and the oldest files are deleted when the directory exceeds it. Files younger than 2 minutes are never deleted by another call, so concurrent calls do not remove each other's output.

Outcomes:

| outcome | Meaning | Gate effect |
| --- | --- | --- |
| `passed` | exit 0 and at least one test *executed and verified* (skipped, deselected, xfailed, xpassed, unittest expected failures and go "[no tests to run]" do not count) | verification succeeded for the languages run |
| `failed` | tests ran, at least one failed | verification failed |
| `timeout` | killed after `timeout` s | verification failed |
| `no_tests` | nothing verified: nothing collected, every test skipped, only expected failures, go packages with no matching tests, JS runner without parseable counts, or no mapped tests with the fallback off | no evidence |
| `error` | collection/usage error, runner missing, unparseable exit | no evidence |

A run with failures is still a *successful tool call* (`success=True`): the model sees the summary, not an error. Only "no framework detected" / "nothing to run" return `success=False`.

### 2.4 Gate evidence

`ImplementationStateMachine.record_verification_evidence(languages, success, command)` sets the same fields as the shell path (`verification_attempted`, `verification_succeeded`, `verification_revision`) but takes the verified languages explicitly. A later edit makes the verification stale exactly as before, and every changed language still needs its own passing tests. The existing bash-regex path is unchanged; both kinds of evidence coexist, and the most recent one wins.

Evidence is judged per language (`data.language_outcomes`), not by the overall outcome: the overall `outcome` is `passed` only when every framework passed, so a Python pass next to an npm suite without tests would otherwise be lost. Any failure or timeout makes the call failed evidence; otherwise a real pass for at least one language is evidence, and the gate still requires every changed language to be among `passed_languages`.

## 3. Configuration

`run_tests` is enabled by listing it in `modes.code.tools` (it is listed in the shipped templates). Remove it from the list to disable the tool, its prompt section, and the swarm element. Existing installations keep their own `config.yaml`: they do not gain `run_tests` automatically and must add it to `modes.code.tools` (and the `run_tests` block) to use it.

```yaml
modes:
  code:
    tools:
      - run_tests
    run_tests:
      timeout: 600               # budget per run_tests call, seconds (the model may ask for less, never more)
      max_summary_chars: 6000    # summary cap, ~1.5k tokens
      spill: true                # write the full output to a file
      spill_dir: ""              # empty = <agent workspace>/run_tests
      spill_max_bytes: 52428800  # directory cap, oldest first; 0 = no cap
      python: ""                 # interpreter path; empty = .venv → venv → $VIRTUAL_ENV → PATH
      fallback_whole_suite: false  # run a language's whole suite when changed files map to no tests
      inherit_runner_env: false    # pass PYTEST_ADDOPTS / NODE_OPTIONS / GOFLAGS / RUSTC_WRAPPER … to the tests
```

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `timeout` | number | `600` | budget for the whole call, shared by frameworks; clamped to 5–7200; the tool card declares it plus 60 s as agent-core's outer deadline |
| `max_summary_chars` | int | `6000` | clamped to 500–50,000 |
| `spill` | bool | `true` | off: no file, the summary is all the model gets |
| `spill_dir` | string | `""` | an explicit directory must be readable in the sandbox to be useful |
| `spill_max_bytes` | int | `52428800` | per-file and directory cap; `0` disables it |
| `python` | string | `""` | explicit interpreter path or command name; empty uses the order in §2.3 |
| `fallback_whole_suite` | bool | `false` | off: unmapped changes return `no_tests` and ask for `tests`; on: run the whole suite (costly on large repositories) |
| `inherit_runner_env` | bool | `false` | off: runner option variables from the agent's environment are removed; on: they reach the tests (for setups that rely on them) |

Permissions: the templates list `run_tests: allow` under `permissions.tools`, next to `bash: allow`, and the permission layer classifies it as a code tool (`dynamic_execution`, high risk). In automatic permission mode, a code tool that is not a JiuwenBox execution tool always goes to manual review (`requires_manual_execution_provider_review`), so run_tests is never auto-allowed there. Deployments that ask before shell commands should set `run_tests: ask` as well.

Sandbox mode: run_tests starts the test process on the host, not through JiuwenBox. When the Code session's SysOperation runs in sandbox mode (`OperationMode.SANDBOX`), the adapter and the swarm element do not register the tool, and an existing instance refuses every call if the SysOperation is switched to a sandbox later. Test commands then go through bash inside the sandbox. This keeps `run_tests: allow` in the templates from bypassing the sandbox. The approval request shows the tool arguments (`files`, `tests`, `extra_args`); the resolved command line is in the tool result (`data.results[].command`).

Tool arguments: `files` (list), `tests` (list), `extra_args` (list, runner options from the allow-list), `timeout` (int).

## 4. Prompt cost

| Item | Size | When |
| --- | --- | --- |
| Tool card (description + JSON schema) | ~1,090 chars, ~270 tokens | tool listed |
| "Verifying changes" system prompt section | 303 chars, ~75 tokens | tool listed |

`build_code_system_prompt()` without arguments returns the previous prompt byte for byte.

## 5. How to confirm it is working

- The tool is registered: the adapter logs `[JiuwenSwarmCodeAdapter] Tool run_tests registered from config`.
- A call's result starts with a machine-readable line, e.g. `run_tests: outcome=failed passed_languages=-`, followed by one line per framework, the failing test ids with their traceback tail, and `full output: <path>`.
- The full output file exists under `<agent workspace>/run_tests/`.
- After a passing run following an edit, the `implement_completeness` and `code_execution_discipline` prompt sections disappear on the next model call.

## 6. Limitations

- Mapping is heuristic. It finds conventional test names and direct imports; tests reached through fixtures, plugins or dynamic imports need explicit `tests` (or `fallback_whole_suite: true`).
- Python is first-class. jest/vitest/npm, go and cargo are detected and parsed best-effort; Rust/C changes always run the whole `cargo test`.
- Discovery reads configuration at the workspace root only (`pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `package.json`, `go.mod`, `Cargo.toml`). Monorepos with `backend/pyproject.toml` or `packages/*/package.json` are detected only partly (or as plain unittest); open the sub-project as the workspace, or pass explicit `tests`.
- `npx --no-install` is used for jest/vitest, so they must already be installed.
- The tool runs the project's own test code with the agent's privileges on the host. The argument checks stop the model from pointing it outside the project or loading extra code through runner options, but the tests themselves (and the project's `conftest.py`, `package.json` scripts, `build.rs`, the project's `.venv` interpreter, …) run unrestricted, exactly as when a developer runs them. In local mode, govern it with the permission policy as you would local `bash`; in sandbox mode it is not available (§3).
- run_tests is not a shell tool, so a shell network policy (for example `permissions.shell_network: deny`, or the headless default `deny`, from the shell-policy MR) does not apply to it: test code can still reach the network. Where that matters, run headless tasks in a network-less container, as the benchmark harness does.
- The model can write test code, so it can also write code that prints a fake summary (for example a module that prints "1 passed" at import time while every test is skipped). The parser reads the runner's output; it cannot tell a real summary from a forged one. The completion gate's evidence is only as trustworthy as the test code in the repository.
- The npm-script detection is literal: a script that runs jest through `node node_modules/jest/bin/jest.js` or a shell chain is not recognised, and `extra_args` are refused for it.

## 7. Code map

| File | Role |
| --- | --- |
| `jiuwenswarm/agents/harness/code/verification/discovery.py` | framework detection, cache, `language_of` |
| `jiuwenswarm/agents/harness/code/verification/mapping.py` | file → test mapping |
| `jiuwenswarm/agents/harness/code/verification/runner.py` | argv, execution, parsing, spill, summary |
| `jiuwenswarm/agents/harness/code/verification/argpolicy.py` | `extra_args` allow-list per runner |
| `jiuwenswarm/agents/harness/code/verification/tool.py` | `RunTestsTool`, card, run planning |
| `jiuwenswarm/agents/harness/code/verification/evidence.py` | result → gate evidence |
| `jiuwenswarm/agents/harness/code/implementation_state.py` | `record_verification_evidence` |
| `jiuwenswarm/agents/harness/code/rails/implement_completeness_rail.py`, `code_execution_discipline_rail.py` | consume the evidence |
| `jiuwenswarm/server/runtime/agent_adapter/interface_code.py` | `_build_run_tests_tool`, prompt tool names |
| `jiuwenswarm/agents/swarm/providers/tools.py`, `config_specs.py`, `registry.py` | `swarm.run_tests` |
