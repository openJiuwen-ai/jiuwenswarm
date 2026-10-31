# Research rails and CLI regression evidence

This documents the reproducible failures fixed by PR #3629, based on upstream
`52abe68db2dd167485f6bd79d6e36e193d608e64`. Tests use synthetic events and do not
call a model or need credentials.

## Failures and checks

| Trigger | Before | After | Regression test |
| --- | --- | --- | --- |
| Server sends a nested `chat.tool_call` | CLI displays `[tool] ?: {}` | CLI displays the tool name and arguments | `test_usage_reporting.py::test_nested_tool_call_displays_name_and_arguments` |
| Server sends usage and context events | CLI drops accounting, even on successful runs | One summary accumulates per-call tokens and uses the latest context occupancy | `test_usage_reporting.py::test_chat_reports_usage_once_on_success_and_failure` |
| A run fails or loses its connection after spending tokens | No usage summary | Summary is emitted once, and the client closes | Same test, error and disconnect cases |
| A run is cancelled after receiving usage | No accounting support | Cancellation propagates after emitting usage and closing the client | `test_usage_reporting.py::test_chat_reports_usage_when_cancelled` |
| A response cites a novelty claim with LaTeX `\cite{...}` | Governance rail flags it as uncited | Cited claims remain unflagged | `test_governance_review_rail.py::test_cited_novelty_claim_is_not_flagged` |

Existing rail tests also exercise the real `AgentCallbackContext.inputs.response`
path and both team and single-agent rail builders. Configuration tests exercise
the `JIUWENSWARM_CONFIG_DIR` override.

## Reproduce the CLI before/after result

From this PR checkout, with its test dependencies installed in `.venv`:

```bash
repo_dir=$(git rev-parse --show-toplevel)
baseline_dir=$(mktemp -d /tmp/jiuwenswarm-regression.XXXXXX)
git worktree add --detach "$baseline_dir" 52abe68db2dd167485f6bd79d6e36e193d608e64

# Expected failure: execute the new tests against the unmodified source.
(
  cd "$baseline_dir"
  "$repo_dir/.venv/bin/python" -m pytest \
    "$repo_dir/tests/unit_tests/channels/cli/test_usage_reporting.py" \
    --import-mode=importlib -o addopts='' -o log_cli=false --asyncio-mode=auto -q
)

# Expected success: execute the same tests against the repaired source.
.venv/bin/python -m pytest tests/unit_tests/channels/cli/test_usage_reporting.py \
  -o addopts='' -o log_cli=false --asyncio-mode=auto -q
```

On macOS / Python 3.13.13, the baseline run gives **6 failed**, and the repaired
run gives **6 passed**. Four cases fail on user-visible output (unknown tool name
or missing usage); two fail because the baseline lacks the accounting helper/API.
The cancellation case simulates task cancellation, not delivery of an OS signal.

The complete CLI suite gives **154 passed**. With coverage enabled for the
affected renderer and event classifier, `render.py` is **98%**, `events.py` is
**100%** (247 statements, 5 missed in total). This is scoped coverage, not a
claim about coverage of the whole repository.

## Portable desktop authorization tests

Two desktop OAuth tests constructed `_WindowApi(None)` even though macOS
initialization requires a runtime with clipboard support. They now invoke the
static authorization method through the class, preserving every allow/deny
assertion without constructing an unrelated window runtime. The focused macOS
run changed from **2 failed** to **11 passed**. This repairs a test fixture, not
an OAuth production vulnerability.

Runtime single-agent tests also reused a session ID without isolating persisted
session storage. A pre-existing web-owned session caused four process-channel
cases to fail the legitimate ownership check. A per-test temporary session
directory now isolates those fixtures without changing production validation or
assertions. With a conflicting session seeded, Python 3.11 results changed from
**4 failed, 40 passed** to **44 passed**.

## SQLite retention on Python 3.11

`TrajectoryStore.delete_expired()` committed expired-record deletion and then
used `execute("PRAGMA incremental_vacuum").fetchall()` to reclaim freed pages.
Python 3.11's sqlite cursor stops this no-column statement after one page, leaving
the rest on the freelist. The same behavior occurs with SQLite 3.46 and 3.53;
Python 3.13 does not reproduce it. Updating SQLite alone is insufficient.

The fix uses `executescript("PRAGMA incremental_vacuum;")` to run the fixed
statement to completion. It runs after the existing commit, so the retention
transaction and rollback behavior do not change. No user input enters the SQL.
The existing regression asserts a changed epoch, an empty freelist, fewer pages,
and a smaller file; its assertions are unchanged:

```bash
# Run with a Python 3.11 virtual environment.
python -m pytest tests/unit_tests/observability/test_trajectory_retention.py \
  -k retention_rotates_the_epoch_and_shrinks_the_file \
  -o addopts='' -o log_cli=false --asyncio-mode=auto -q
```

Before the fix, the test fails because 9 free pages remain instead of 0 on both
Python 3.11.9 and 3.11.15. Afterwards it passes on both, and also on 3.13.13.
The complete observability suite gives **136 passed** on Python 3.11.15, with
**90%** statement coverage for `store.py`, including the changed vacuum call.

## CI environment issue remains a separate check

The downloadable !7499 UT report generated on 28-Sep-2026 at 00:18:13 records
commit `f6fb99f61`, not the subsequent PR head. Of its 680 failed/error entries,
669 contain an `_ExtendedAttributes` import failure. The SDK loads from the
project virtual environment while the API loads from the system Python
site-packages. The report does not identify which runner configuration caused
this mismatch.

The pipeline is managed outside this repository. Local test success is not a
replacement for a completed CI run on the latest commit. No test is skipped,
no check is suppressed, and no package-search-path workaround is introduced by
these regression tests.
