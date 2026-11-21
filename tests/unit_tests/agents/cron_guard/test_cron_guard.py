# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the cron runaway guard (issue #5018)."""

from __future__ import annotations

import sys
import time

import pytest

from jiuwenswarm.common.cron_guard import config as cfg_mod
from jiuwenswarm.common.cron_guard import identity as id_mod
from jiuwenswarm.common.cron_guard import sleep_guard
from jiuwenswarm.common.cron_guard.config import clamp_deadlines
from jiuwenswarm.common.cron_guard.identity import (
    CronRunContext,
    RunBudget,
    RunRegistry,
    current_cron_run,
    maybe_sign_run_token,
    sign_run_token,
    verify_run_token,
)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def cron_ctx(tmp_path, monkeypatch):
    """An enabled-guard, trusted cron run context bound via ContextVar."""
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "test-secret")
    ctx = CronRunContext(
        run_id="run-1",
        job_id="job-1",
        sid="sid-1",
        trusted=True,
        max_iterations=30,
        base_timeout_seconds=3600,
        deadline_soft=time.monotonic() + 3000,
        deadline_hard=time.monotonic() + 3400,
        budget=RunBudget(run_id="run-1", max_iterations=30),
    )
    token = current_cron_run.set(ctx)
    yield ctx
    current_cron_run.reset(token)


@pytest.fixture
def guard_enabled(monkeypatch):
    """Force get_cron_guard_config to return enabled defaults."""
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    # sleep_guard imports the function lazily from the module — patch there too.
    monkeypatch.setattr(sleep_guard, "get_cron_guard_config", lambda: base, raising=False)
    return base


@pytest.fixture
def guard_config(guard_enabled):
    """Mutable enabled config for per-test tweaks."""
    return guard_enabled


# ---------------------------------------------------------------- sleep parser: BLOCK cases


@pytest.mark.parametrize(
    "cmd",
    [
        "sleep 120",
        "sleep 30",
        "sleep 1m30s",
        "sleep 1m 30s",
        "bash -c 'sleep 300'",
        "sh -c \"sleep 60 && echo hi\"",
        "while true; do sleep 5; done",
        "for i in 1 2 3; do sleep 20; done",
        "until [ -f ready ]; do sleep 10; done",
        "env FOO=1 sleep 45",
        "nohup sleep 100 &",
        "watch -n 5 cat /tmp/x",
        "echo $(sleep 30)",
        "echo `sleep 20`",
        "eval 'sleep 40'",
        "timeout 500 sleep 600",
        "sleep 0.5 && sleep 30",
        "x=1; sleep 90; echo $x",
        "sleep 2h",
        "bash -c \"bash -c 'sleep 15'\"",
    ],
)
def test_parser_detects_sleep(cmd):
    r = sleep_guard.parse_shell_command(cmd)
    assert r.has_sleep, f"expected sleep detection for {cmd!r}: {r}"


# ---------------------------------------------------------------- sleep parser: ALLOW cases

@pytest.mark.parametrize(
    "cmd",
    [
        "grep sleep file.log",
        "echo 'while true; sleep 120'",
        'echo "sleep 999"',
        "echo sleep",
        "cat /var/log/sleep.log",
        "ls -la",
        "python worker.py",
        "bash script.sh",
        "node app.js",
        "echo 'do not sleep'",
        "awk '{print $1}' file",
        "find . -name '*sleep*'",
        "ps aux | grep -v sleep",
        "echo done && echo finished",
        "git log --oneline",
    ],
)
def test_parser_allows_non_sleep(cmd):
    r = sleep_guard.parse_shell_command(cmd)
    assert not r.has_sleep, f"unexpected sleep detection for {cmd!r}: {r}"


def test_parser_sums_multi_arg_sleep():
    r = sleep_guard.parse_shell_command("sleep 1m 30s")
    assert r.sleep_seconds == pytest.approx(90.0)
    assert r.max_single_call_seconds == pytest.approx(90.0)
    assert r.sleep_calls == 1


def test_parser_max_single_per_call_not_across_calls():
    # Two separate sleep calls: each 5s — neither single call exceeds 10.
    r = sleep_guard.parse_shell_command("sleep 5 && sleep 5")
    assert r.sleep_calls == 2
    assert r.max_single_call_seconds == pytest.approx(5.0)
    assert r.sleep_seconds == pytest.approx(10.0)


def test_parser_unknown_duration_variable():
    r = sleep_guard.parse_shell_command("sleep $N")
    assert r.unknown_duration is True
    assert r.has_sleep is True


def test_parser_unknown_duration_command_sub_arg():
    r = sleep_guard.parse_shell_command("sleep $(cat wait.txt)")
    assert r.unknown_duration is True


def test_parser_loop_poll_flag():
    r = sleep_guard.parse_shell_command("while true; do sleep 5; done")
    assert r.loop_poll is True


def test_parser_keyword_in_quotes_not_loop():
    r = sleep_guard.parse_shell_command("echo 'while true; do sleep 120; done'")
    assert r.loop_poll is False
    assert r.has_sleep is False


def test_parser_background_flag():
    r = sleep_guard.parse_shell_command("sleep 300 &")
    assert r.background is True
    assert r.has_sleep is True


def test_parser_powershell_start_sleep():
    r = sleep_guard.parse_shell_command("Start-Sleep -Seconds 120")
    assert r.sleep_seconds == pytest.approx(120.0)
    r2 = sleep_guard.parse_shell_command("Start-Sleep -Milliseconds 500")
    assert r2.sleep_seconds == pytest.approx(0.5)


def test_parser_inline_python_sleep_literal():
    r = sleep_guard.parse_shell_command("python -c 'import time; time.sleep(30)'")
    assert r.sleep_calls >= 1
    assert r.max_single_call_seconds == pytest.approx(30.0)


def test_parser_inline_python_sleep_unknown():
    r = sleep_guard.parse_shell_command("python -c 'import time; time.sleep(n)'")
    assert r.unknown_duration is True


def test_parser_interpreter_file_not_scanned():
    r = sleep_guard.parse_shell_command("python worker.py")
    assert not r.has_sleep


def test_parser_nesting_depth_exceeded_blocks_deep():
    import shlex as _shlex

    # 4 levels of bash -c nesting exceeds default depth 3.
    inner = "sleep 5"
    for _ in range(4):
        inner = f"bash -c {_shlex.quote(inner)}"
    r = sleep_guard.parse_shell_command(inner, max_nesting_depth=3)
    assert r.has_sleep  # either parsed or flagged unparseable — never silently allowed
    assert r.unparseable is True


# ---------------------------------------------------------------- guard entry behavior

def test_guard_blocks_long_sleep_in_cron_run(guard_enabled, cron_ctx):
    err = sleep_guard.guard_shell_command("sleep 120")
    assert err is not None
    assert "cron_guard" in err


def test_guard_allows_short_sleep(guard_enabled, cron_ctx):
    assert sleep_guard.guard_shell_command("sleep 2") is None


def test_guard_blocks_unknown_duration(guard_enabled, cron_ctx):
    assert sleep_guard.guard_shell_command("sleep $N") is not None


def test_guard_unknown_assume_mode_counts(guard_config, cron_ctx):
    guard_config["sleep"]["unknown_duration"] = "assume"
    guard_config["sleep"]["unknown_assumed_seconds"] = 10
    assert sleep_guard.guard_shell_command("sleep $N") is None
    assert cron_ctx.budget.sleep_seconds == pytest.approx(10.0)


def test_guard_total_budget_blocks_fourth_call(guard_enabled):
    ctx = CronRunContext(
        run_id="r2", job_id="j", sid="s", trusted=True,
        budget=RunBudget(run_id="r2"),
    )
    token = current_cron_run.set(ctx)
    try:
        assert sleep_guard.guard_shell_command("sleep 5") is None   # 5s, call 1
        assert sleep_guard.guard_shell_command("sleep 5") is None   # 10s, call 2
        assert sleep_guard.guard_shell_command("sleep 5") is None   # 15s, call 3
        # 4th call exceeds max_sleep_calls=3 → blocked
        assert sleep_guard.guard_shell_command("sleep 5") is not None
    finally:
        current_cron_run.reset(token)


def test_guard_total_seconds_budget(guard_enabled):
    ctx = CronRunContext(
        run_id="r3", job_id="j", sid="s", trusted=True,
        budget=RunBudget(run_id="r3"),
    )
    token = current_cron_run.set(ctx)
    try:
        assert sleep_guard.guard_shell_command("sleep 9") is None   # 9s
        assert sleep_guard.guard_shell_command("sleep 9") is None   # 18s
        assert sleep_guard.guard_shell_command("sleep 9") is None   # 27s
        assert sleep_guard.guard_shell_command("sleep 9") is not None  # 36s > 30 total
    finally:
        current_cron_run.reset(token)


def test_guard_interactive_unaffected(monkeypatch):
    """No cron context → allow everything, even sleep 9999."""
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "s")
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    assert sleep_guard.guard_shell_command("sleep 9999") is None


def test_guard_disabled_allows(guard_enabled, cron_ctx):
    guard_enabled["enabled"] = False
    assert sleep_guard.guard_shell_command("sleep 9999") is None


def test_guard_warn_mode_allows(guard_config, cron_ctx):
    guard_config["sleep"]["mode"] = "warn"
    assert sleep_guard.guard_shell_command("sleep 9999") is None


def test_guard_background_sleep_blocked(guard_enabled, cron_ctx):
    assert sleep_guard.guard_shell_command("sleep 5", background=True) is not None


def test_guard_fail_open_on_internal_error(guard_enabled, cron_ctx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(sleep_guard, "parse_shell_command", boom)
    assert sleep_guard.guard_shell_command("sleep 120") is None


# ---------------------------------------------------------------- deadline clamp invariant

@pytest.mark.parametrize("base", [60, 3600, 7200, 45, 31, 30, 10, 5, 3, 2, 1, 0.5])
def test_clamp_deadline_invariant(base):
    cfg = {
        "deadline": {
            "soft_ratio": 0.85,
            "hard_ratio": 0.95,
            "gateway_margin_seconds": 10,
            "reserve_seconds": 5,
            "kill_grace_seconds": 3,
        }
    }
    hard, soft = clamp_deadlines(base, cfg)
    assert 0 < soft < hard, f"base={base}: soft={soft} hard={hard}"
    assert hard <= max(base, 2.0) + 1e-9, f"base={base}: hard={hard} > base"


def test_clamp_deadline_large_base_ratios():
    cfg = {"deadline": {"soft_ratio": 0.85, "hard_ratio": 0.95}}
    hard, soft = clamp_deadlines(3600, cfg)
    # hard = max(3600*0.95, 3600-30) = max(3420, 3570) = 3570
    assert hard == pytest.approx(3570.0)
    assert soft == pytest.approx(3060.0)


def test_clamp_deadline_small_base_uses_minus30_floor():
    cfg = {"deadline": {"soft_ratio": 0.85, "hard_ratio": 0.95}}
    hard, soft = clamp_deadlines(100, cfg)
    # max(95, 70) = 95 hard; min(85, 85) = 85 soft
    assert hard == pytest.approx(95.0)
    assert soft == pytest.approx(85.0)


# ---------------------------------------------------------------- identity / token

def test_token_sign_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "topsecret")
    tok = sign_run_token("r1", "j1", "s1", b"topsecret")
    assert verify_run_token(tok, "r1", "j1", "s1") is True
    assert verify_run_token(tok, "r1", "j1", "s2") is False
    assert verify_run_token("forged", "r1", "j1", "s1") is False
    assert verify_run_token(None, "r1", "j1", "s1") is False


def test_maybe_sign_without_secret(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_CRON_RUN_SECRET", raising=False)
    assert maybe_sign_run_token("r", "j", "s") is None


def test_resolve_identity_scheduled_run(monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    meta = {
        "cron": {
            "job_id": "j1",
            "run_id": "r1",
            "run_token": sign_run_token("r1", "j1", "sid-9", b"sec"),
            "timeout_seconds": 120,
        }
    }
    ctx = id_mod.resolve_cron_run_context(meta, {}, "sid-9")
    assert ctx is not None
    assert ctx.trusted is True
    assert ctx.run_id == "r1"
    assert ctx.base_timeout_seconds == 120
    assert 0 < (ctx.deadline_hard - ctx.deadline_soft)


def test_resolve_identity_forged_token_tightens_only(monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    meta = {"cron": {"job_id": "j1", "run_id": "r1", "run_token": "garbage"}}
    ctx = id_mod.resolve_cron_run_context(meta, {}, "sid-9")
    assert ctx is not None
    assert ctx.trusted is False  # tightening layers apply, state-changing disabled


def test_resolve_identity_interactive(monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    assert id_mod.resolve_cron_run_context({}, {}, "sid-1") is None
    assert id_mod.resolve_cron_run_context(None, None, None) is None


def test_resolve_identity_guard_disabled(monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = False
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    meta = {"cron": {"job_id": "j1", "run_id": "r1"}}
    assert id_mod.resolve_cron_run_context(meta, {}, "s") is None


# ---------------------------------------------------------------- registry / budget

def test_registry_session_lookup():
    reg = RunRegistry()
    ctx = CronRunContext(
        run_id="r1", job_id="j", sid="s1", trusted=True,
        budget=RunBudget(run_id="r1"),
    )
    reg.register(ctx)
    assert reg.get_by_session("s1") is ctx
    assert reg.get_by_session("nope") is None
    reg.unregister("r1")
    assert reg.get_by_session("s1") is None



def test_parser_nested_substitution_counted_once():
    """Nested $(...) / backtick-in-$(...) sleeps count exactly once."""
    r = sleep_guard.parse_shell_command("echo $(cat $(sleep 8))")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(8.0)
    r = sleep_guard.parse_shell_command("echo $(echo $(sleep 8))")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(8.0)
    r = sleep_guard.parse_shell_command("echo $(cat `sleep 8`)")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(8.0)
    # plain backticks are still detected on their own
    r = sleep_guard.parse_shell_command("echo `sleep 8`")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(8.0)


def test_parser_multiline_sleep_segmented():
    """A sleep on its own line must not hide as an argument of the previous command.

    shlex consumes ``\\n`` as whitespace, so without newline segmentation
    ``echo done\\nsleep 120`` lexed as a single ``echo`` command and the
    sleep escaped interception entirely.
    """
    r = sleep_guard.parse_shell_command("echo done\nsleep 120")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(120.0)
    r = sleep_guard.parse_shell_command("cd /tmp\nsleep 600")
    assert r.sleep_seconds == pytest.approx(600.0)
    r = sleep_guard.parse_shell_command("export A=1\nsleep 900")
    assert r.sleep_seconds == pytest.approx(900.0)
    # multi-line loop polling is still flagged as loop_poll
    r = sleep_guard.parse_shell_command("while true\ndo\nsleep 60\ndone")
    assert r.loop_poll is True
    assert r.sleep_seconds == pytest.approx(60.0)


def test_parser_multiline_after_operator_segmented():
    """A sleep on the line after a trailing ``&&``/``;``/``||`` stays intercepted.

    ``_normalise_newlines`` rewrites that newline to ``;`` so shlex punctuation
    mode lexes ``&&;``/``;;``/``||;`` as one glued run — the splitter must treat
    each operator character inside the run as a command boundary.
    """
    r = sleep_guard.parse_shell_command("echo a &&\nsleep 120")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(120.0)
    assert r.background is False  # `&&` is not a background job
    r = sleep_guard.parse_shell_command("echo a;\nsleep 120")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(120.0)
    r = sleep_guard.parse_shell_command("echo a ||\nsleep 90")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(90.0)


def test_parser_subshell_next_line_segmented():
    """``(sleep …)`` on its own line is its own command, not a hidden argument.

    The ``(`` opens the next segment; it must be skipped to reach command
    position, and the trailing ``)`` must not turn the duration unknown.
    """
    r = sleep_guard.parse_shell_command("foo\n(sleep 60)")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(60.0)
    r = sleep_guard.parse_shell_command("foo\n\n(sleep 1m30s)\nbar")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(90.0)
    # same-line group forms stay caught too
    r = sleep_guard.parse_shell_command("echo done && (sleep 30)")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(30.0)


def test_parser_loop_condition_sleep_segmented():
    """A sleep in the loop header condition is a poll too.

    ``_parse_script`` used to hand the whole header segment (loop keyword
    still in command position) to ``_parse_segment``, so
    ``while sleep 5; do echo hi; done`` detected nothing at all.
    """
    r = sleep_guard.parse_shell_command("while sleep 5; do echo hi; done")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(5.0)
    assert r.loop_poll is True
    r = sleep_guard.parse_shell_command("until sleep 30; do :; done")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(30.0)
    assert r.loop_poll is True
    # loop-body detection unchanged
    r = sleep_guard.parse_shell_command("for i in 1 2; do sleep 10; done")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(10.0)
    assert r.loop_poll is True


def test_parser_quoted_multiline_text_not_blocked():
    """Newlines inside quotes are data — no segmentation, no detection."""
    r = sleep_guard.parse_shell_command('grep "foo\nsleep 120" /var/log/x.log')
    assert r.sleep_calls == 0
    assert r.sleep_seconds == 0.0


def test_parser_heredoc_body_not_blocked():
    """Heredoc bodies are data: sleeps written into a file must not block."""
    r = sleep_guard.parse_shell_command("cat > f.txt <<EOF\nsleep 999\nEOF\nls")
    assert r.sleep_calls == 0
    r = sleep_guard.parse_shell_command("cat > f.txt <<'EOF'\nsleep 999\nEOF")
    assert r.sleep_calls == 0
    # but a real sleep following the heredoc is still caught
    r = sleep_guard.parse_shell_command("cat > f.txt <<EOF\ndata here\nEOF\nsleep 888")
    assert r.sleep_seconds == pytest.approx(888.0)


def test_parser_line_continuation_not_blocked():
    """Backslash-newline continuation joins lines — no separator inserted."""
    r = sleep_guard.parse_shell_command("echo run\\\nsleep 1")
    assert r.sleep_calls == 0
    assert r.sleep_seconds == 0.0


def test_budget_add_sleep_counts_calls():
    b = RunBudget(run_id="rb")
    b.add_sleep(5.0, calls=2)
    b.add_sleep(5.0)
    assert b.sleep_calls == 3
    assert b.sleep_seconds == pytest.approx(10.0)


def test_guard_budget_counts_sleep_calls_not_commands(guard_enabled):
    """max_sleep_calls bounds sleep invocations, not shell commands."""
    ctx = CronRunContext(
        run_id="rc", job_id="j", sid="s", trusted=True,
        budget=RunBudget(run_id="rc"),
    )
    token = current_cron_run.set(ctx)
    try:
        # one command, two sleep calls → 2 of the 3-call budget
        assert sleep_guard.guard_shell_command("sleep 2; sleep 2") is None
        assert ctx.budget.sleep_calls == 2
        # the second command's two calls push past max_sleep_calls=3
        assert sleep_guard.guard_shell_command("sleep 2; sleep 2") is not None
    finally:
        current_cron_run.reset(token)


def test_record_shell_wait_accumulates_and_exceeds(guard_enabled, cron_ctx):
    from jiuwenswarm.common.cron_guard.identity import (
        record_shell_wait,
    )

    cron_ctx.budget.tool_wait_budget_seconds = 10.0
    record_shell_wait(time.monotonic() - 12.0)
    assert cron_ctx.budget.shell_seconds >= 12.0
    assert cron_ctx.budget.tool_wait_exceeded() is True


def test_record_shell_wait_no_run_noop(guard_enabled):
    from jiuwenswarm.common.cron_guard.identity import (
        record_shell_wait,
    )

    # no cron run in the contextvar and no session registry hit → silent no-op
    record_shell_wait(time.monotonic() - 5.0)




def test_lazy_facade_resolves_package_root_names():
    """AIReview 10209 id=200915741：门面 __getattr__ 的 import_module 必须带
    包全名前缀（裸名会被当顶层模块导入，__all__ 经门面 100% 失败）。锁定
    package 根名解析契约：config/identity/ledger/sleep_guard 四面全可达。"""
    import importlib
    pkg = importlib.import_module("jiuwenswarm.common.cron_guard")
    assert pkg.get_cron_guard_config is not None
    assert pkg.CronRunContext is not None
    assert pkg.CronRunLedger is not None
    assert pkg.clamp_deadlines is not None
    assert pkg.current_cron_run is not None
    assert pkg.STATE_RUNNING == "running"
    import jiuwenswarm.common.cron_guard.sleep_guard as sg  # 子模块在树
    assert sg.guard_shell_command is not None


# ── R6 检视整改回归（姊妹单同步：MF-2 ledger 同根 / SF-4 谓词）────────


def test_default_ledger_path_shares_checkpoint_root(tmp_path, monkeypatch):
    """MF-2: ledger path resolves through the checkpointer's own chain."""
    from jiuwenswarm.common import utils as cw_utils
    from jiuwenswarm.common.cron_guard import ledger as ledger_mod

    monkeypatch.setattr(cw_utils, "_workspace_base_dir", tmp_path)
    path = ledger_mod.default_ledger_path()
    assert path == cw_utils.get_checkpoint_dir() / "cron_run_ledger.db"


def test_mark_orphans_spares_current_process_rows(tmp_path):
    """SF-4: rows started by this process are never orphaned."""
    import sqlite3 as _sqlite3

    from jiuwenswarm.common.cron_guard import ledger as ledger_mod

    db = tmp_path / "cron_run_ledger.db"
    led = ledger_mod.CronRunLedger(db)
    led.start_run("r-mine", "job-a", "sid-a")
    old_ts = ledger_mod._PROCESS_START - 3600
    conn = _sqlite3.connect(db)
    conn.execute(
        "INSERT INTO cron_runs (run_id, job_id, sid, state, started_at)"
        " VALUES ('r-old', 'job-b', 'sid-b', 'running', ?)",
        (old_ts,),
    )
    conn.commit()
    conn.close()

    assert led.mark_orphans() == 1
    assert led.lookup_run("r-old")["state"] == ledger_mod.STATE_ORPHANED
    assert led.lookup_run("r-mine")["state"] == ledger_mod.STATE_RUNNING


# ── PR !10209 review fixes (roots 200900602/606/608/609/610/611/612/616/620) ──


@pytest.mark.parametrize(
    "cmd",
    [
        "echo '$(sleep 120)'",
        "echo '`sleep 120`'",
        "echo 'run $(sleep 1) and `sleep 2` now'",
    ],
)
def test_parser_single_quoted_substitution_is_literal(cmd):
    """200900602: single-quoted $(...)/backticks are data, never a sleep call."""
    r = sleep_guard.parse_shell_command(cmd)
    assert not r.has_sleep, f"single-quoted literal misread as sleep: {cmd!r} -> {r}"


def test_parser_double_quoted_substitution_still_detected():
    """200900602: double quotes execute $(...) — detection must stay."""
    r = sleep_guard.parse_shell_command('echo "$(sleep 120)"')
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(120.0)


@pytest.mark.parametrize(
    "cmd",
    [
        "env -i sleep 45",
        "env -u FOO sleep 45",
        "env --ignore-environment sleep 45",
    ],
)
def test_parser_env_flag_forms_blocked(cmd):
    """200900606: env's own flags must not occupy the command position."""
    r = sleep_guard.parse_shell_command(cmd)
    assert r.has_sleep, f"env flag form slipped through: {cmd!r} -> {r}"
    assert r.sleep_seconds == pytest.approx(45.0)


def test_parser_env_assignments_unchanged():
    """200900606: VAR=VALUE stripping keeps working."""
    r = sleep_guard.parse_shell_command("env FOO=1 x=1 sleep 45")
    assert r.sleep_calls == 1
    assert r.sleep_seconds == pytest.approx(45.0)


@pytest.mark.parametrize(
    "cmd",
    [
        "(while true; do sleep 5; done)",
        "(while true; do sleep 5; done) &",
        "(until [ -f x ]; do sleep 8; done)",
    ],
)
def test_parser_subshell_loop_poll(cmd):
    """200900611: a leading `(` must not hide the loop keyword."""
    r = sleep_guard.parse_shell_command(cmd)
    assert r.loop_poll is True, f"subshell loop poll escaped: {cmd!r} -> {r}"


@pytest.mark.parametrize("bad", ["abc", [1], {"a": 1}, "1h"])
def test_resolve_identity_garbage_timeout_seconds_falls_back(monkeypatch, bad):
    """200900608/200900616: timeout_seconds must not raise; fall back to 3600."""
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    meta = {"cron": {"job_id": "j1", "run_id": "r1", "timeout_seconds": bad}}
    ctx = id_mod.resolve_cron_run_context(meta, {}, "s")
    assert ctx is not None
    assert ctx.base_timeout_seconds == pytest.approx(3600.0)


@pytest.mark.parametrize("key", ["max_iterations", "default_timeout_seconds"])
def test_config_validate_top_level_numeric_keys(key):
    """200900608: top-level numeric config keys fall back on garbage."""
    cfg = {"deadline": {}, "max_iterations": 30, "default_timeout_seconds": 3600}
    cfg[key] = "garbage"
    cfg_mod._validate(cfg)
    assert cfg[key] == cfg_mod.DEFAULTS[key]
    cfg = {key: -5}
    cfg_mod._validate(cfg)
    assert cfg[key] == cfg_mod.DEFAULTS[key]


@pytest.mark.parametrize("base", [12, 15, 30, 50, 100, 3600])
def test_clamp_small_base_soft_not_degenerate(base):
    """200900612: small bases keep soft at a sane share of hard."""
    cfg = {"deadline": {"soft_ratio": 0.85, "hard_ratio": 0.95, "gateway_margin_seconds": 10}}
    hard, soft = clamp_deadlines(base, cfg)
    assert 0 < soft < hard
    # the old fixed 10s reserve collapsed soft to hard-10 (~1s for tiny bases)
    assert soft >= hard * 0.5, f"base={base}: soft={soft} hard={hard}"


def test_clamp_consumes_gateway_margin_seconds():
    """200900620: deadline.gateway_margin_seconds actually changes the output."""
    base = 50
    cfg = {"deadline": {"soft_ratio": 0.85, "hard_ratio": 0.95, "gateway_margin_seconds": 10}}
    hard1, soft1 = clamp_deadlines(base, cfg)
    cfg["deadline"]["gateway_margin_seconds"] = 3
    hard2, soft2 = clamp_deadlines(base, cfg)
    assert hard1 == hard2
    assert soft2 != soft1  # the key is consumed now (root 200900620)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="starttime identity requires /proc (Linux only)")
def test_proc_start_time_read_and_gone():
    """200900609: starttime identity is readable live and None once exited.

    atomgit 2026-11-18 #10209: _proc_start_time reads /proc (Linux only, cf.
    test_pid_identity_unknown_starttime_fail_open) — on macOS the not-None
    assert was a platform-dependent red; the child now runs sys.executable
    instead of depending on a system `sleep` binary.
    """
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        start = id_mod._proc_start_time(proc.pid)
        assert start is not None
        assert start == id_mod._proc_start_time(proc.pid)
        assert id_mod._pid_identity_matches(proc.pid, start) is True
    finally:
        proc.kill()
        proc.wait()
    assert id_mod._proc_start_time(proc.pid) is None


def test_pid_identity_unknown_starttime_fail_open():
    """200900609: unverifiable identity (non-Linux) keeps the old behaviour."""
    assert id_mod._pid_identity_matches(999999, None) is True


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="identity-based reap requires /proc (Linux only)")
def test_reap_kills_registered_process():
    """200900609: matching identity → the registered process is reaped."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        reg = RunRegistry()
        reg.register_proc("r-reap", proc.pid)
        assert proc.pid in reg.registered_pids("r-reap")
        assert reg.reap("r-reap", kill_grace_seconds=1.0) is True
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_reap_spares_recycled_pid_identity_mismatch():
    """200900609: a starttime mismatch means a recycled PID — never signalled."""
    import subprocess

    proc = subprocess.Popen(["sleep", "30"])
    try:
        reg = RunRegistry()
        reg.register_proc("r-recycle", proc.pid)
        # Corrupt the recorded identity: the pid now "belongs to a stranger".
        with reg._lock:
            reg._procs["r-recycle"][proc.pid] = (id_mod._proc_start_time(proc.pid) or 0) + 100000
        assert reg.reap("r-recycle", kill_grace_seconds=0.2) is True
        assert proc.poll() is None  # untouched — the stranger survived
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize(
    "cmd",
    [
        "timeout -k 5 300 sleep 300",
        "timeout -s KILL 300 sleep 300",
        "timeout --signal=KILL 300 sleep 300",
        "env -C /tmp sleep 300",
        "env --chdir /tmp sleep 300",
        "bash -ec 'sleep 300'",
        "bash -xec 'sleep 300'",
        "bash -c'sleep 300'",
    ],
)
def test_parser_option_values_and_c_clusters_blocked(cmd):
    """atomgit 2026-11-18 #10209：取参选项值与短选项簇不得占据命令位。

    `timeout -k 5 300 sleep 300`、`env -C /tmp sleep 300`、`bash -ec 'sleep'`、
    `-c` 粘连写法过去都让 L1 sleep 护栏对包装命令完全失效（静默放行）。
    """
    r = sleep_guard.parse_shell_command(cmd)
    assert r.has_sleep, f"option-value/cluster form slipped through: {cmd!r} -> {r}"


def test_config_max_iterations_null_means_unlimited():
    """atomgit 2026-11-18 #10209：`max_iterations: null`（部署模板注释语义＝
    不限制）不得被顶层数值校验 float(None) 静默改写为默认 30。"""
    cfg = {"deadline": {}, "max_iterations": None, "default_timeout_seconds": 3600}
    cfg_mod._validate(cfg)
    assert cfg["max_iterations"] is None
