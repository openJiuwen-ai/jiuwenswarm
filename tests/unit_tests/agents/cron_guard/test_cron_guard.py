# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the cron runaway guard (issue #5018)."""

from __future__ import annotations

import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.cron_guard import config as cfg_mod
from jiuwenswarm.agents.harness.common.cron_guard import identity as id_mod
from jiuwenswarm.agents.harness.common.cron_guard import sleep_guard
from jiuwenswarm.agents.harness.common.cron_guard.config import clamp_deadlines
from jiuwenswarm.agents.harness.common.cron_guard.identity import (
    CronRunContext,
    RunBudget,
    RunRegistry,
    current_cron_run,
    maybe_sign_run_token,
    sign_run_token,
    verify_run_token,
)
from jiuwenswarm.agents.harness.common.cron_guard.ledger import (
    STATE_FINISHED,
    STATE_ORPHANED,
    STATE_RUNNING,
    STATE_TRIPPED,
    CronRunLedger,
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


def test_budget_thread_safety():
    import threading

    b = RunBudget(run_id="r")
    threads = [
        threading.Thread(target=lambda: [b.add_model_call() for _ in range(100)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.model_calls == 800


def test_budget_iteration_exceeded():
    b = RunBudget(run_id="r", max_iterations=3)
    assert not b.iterations_exceeded()
    for _ in range(3):
        b.add_model_call()
    assert not b.iterations_exceeded()
    b.add_model_call()
    assert b.iterations_exceeded()


def test_reap_kills_registered_process():
    import subprocess

    proc = subprocess.Popen(["sleep", "300"])
    reg = RunRegistry()
    reg.register_proc("r1", proc.pid)
    ok = reg.reap("r1", kill_grace_seconds=2)
    assert ok is True
    proc.wait(timeout=5)
    assert proc.returncode != 0 or proc.poll() is not None


def test_reap_no_procs_returns_true():
    reg = RunRegistry()
    assert reg.reap("nothing", kill_grace_seconds=0.1) is True


# ---------------------------------------------------------------- ledger state machine

def test_ledger_lifecycle(tmp_path):
    led = CronRunLedger(tmp_path / "ledger.db")
    led.start_run("r1", "j1", "s1")
    row = led.lookup_run("r1")
    assert row["state"] == STATE_RUNNING
    led.update_budget("r1", {"shell_seconds": 1.5, "sleep_seconds": 10, "sleep_calls": 2, "model_calls": 5})
    row = led.lookup_run("r1")
    assert row["sleep_calls"] == 2
    led.finish_run("r1", STATE_FINISHED, snapshot={"model_calls": 6})
    row = led.lookup_run("r1")
    assert row["state"] == STATE_FINISHED
    assert row["model_calls"] == 6


def test_ledger_orphans(tmp_path):
    led = CronRunLedger(tmp_path / "ledger.db")
    led.start_run("r1", "j1", "s1")
    led.start_run("r2", "j2", "s2")
    led.finish_run("r1", STATE_FINISHED)
    n = led.mark_orphans()
    assert n == 1
    assert led.lookup_run("r2")["state"] == STATE_ORPHANED
    assert led.lookup_run("r1")["state"] == STATE_FINISHED
    # idempotent: no more running rows
    assert led.mark_orphans() == 0


def test_ledger_latest_for_sid_excludes_run(tmp_path):
    led = CronRunLedger(tmp_path / "ledger.db")
    led.start_run("r1", "j", "s")
    led.finish_run("r1", STATE_TRIPPED, trip_reason="deadline_exceeded")
    prior = led.latest_for_sid("s", exclude_run_id="r2")
    assert prior is not None
    assert prior["run_id"] == "r1"
    assert prior["trip_reason"] == "deadline_exceeded"
    assert led.latest_for_sid("s", exclude_run_id="r1") is None


def test_ledger_degraded_does_not_raise(tmp_path):
    # Point the ledger at an unwritable location.
    led = CronRunLedger(tmp_path / "nonexistent-root" / "deep" / "ledger.db")
    os.chmod(tmp_path, 0o500)
    try:
        led.start_run("r1", "j", "s")  # must not raise
        assert led.degraded is True
        assert led.lookup_run("r1") is None
    finally:
        os.chmod(tmp_path, 0o700)


# ---------------------------------------------------------------- checkpoint quarantine

def _make_checkpoint_db(path, sid, keys=None):
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE kv_store ("key" VARCHAR(255) PRIMARY KEY, value VARCHAR(4096))')
    keys = keys or {
        f"{sid}:agent:jiuwenswarm:agent_state_blobs": "BLOB1",
        f"{sid}:agent:jiuwenswarm:agent_state_blobs_dump_type": "pickle",
    }
    conn.executemany("INSERT INTO kv_store VALUES (?, ?)", list(keys.items()))
    conn.commit()
    conn.close()


def _db_keys(path):
    conn = sqlite3.connect(str(path))
    try:
        return {row[0] for row in conn.execute("SELECT key FROM kv_store")}
    finally:
        conn.close()


def test_quarantine_reversible(tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg

    db = tmp_path / "checkpoint.db"
    sid = "cron_job1"
    _make_checkpoint_db(db, sid)
    ok, prefix = cg.quarantine_checkpoint(sid, db_path=db)
    assert ok is True and prefix
    keys = _db_keys(db)
    assert not any(k.startswith(f"{sid}:agent:") for k in keys)
    assert any(k.startswith(f"{prefix}:") for k in keys)
    # reversible
    assert cg.restore_quarantined(sid, prefix, db_path=db) is True
    keys = _db_keys(db)
    assert f"{sid}:agent:jiuwenswarm:agent_state_blobs" in keys


def test_quarantine_missing_blob(tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg

    db = tmp_path / "checkpoint.db"
    _make_checkpoint_db(db, "other_sid")
    ok, prefix = cg.quarantine_checkpoint("cron_missing", db_path=db)
    assert ok is False


def test_govern_restore_at_entry_tripped_prior(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)

    db = tmp_path / "checkpoint.db"
    sid = "cron_jobX"
    _make_checkpoint_db(db, sid)

    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: {
            "run_id": "old-run", "state": STATE_TRIPPED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    monkeypatch.setattr(cg, "checkpoint_db_path", lambda: db)

    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid=sid, trusted=True,
        budget=RunBudget(run_id="new-run"),
    )
    with pytest.raises(cg.CronCheckpointQuarantined):
        cg.govern_restore_at_entry(ctx)
    # blob moved to quarantine, original cleared
    keys = _db_keys(db)
    assert not any(k.startswith(f"{sid}:agent:") for k in keys)


def test_govern_finished_prior_allowed(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: {
            "run_id": "old-run", "state": STATE_FINISHED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid="cron_jobY", trusted=True,
        budget=RunBudget(run_id="new-run"),
    )
    cg.govern_restore_at_entry(ctx)  # must not raise


def test_govern_untrusted_log_only(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    calls = []
    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: calls.append(1) or {
            "run_id": "old", "state": STATE_TRIPPED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid="cron_jobZ", trusted=False,
        budget=RunBudget(run_id="new-run"),
    )
    cg.govern_restore_at_entry(ctx)  # untrusted: no state change, no raise
    assert calls == []


def test_govern_quarantine_and_fresh_continues(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["checkpoint"]["on_trip"] = "quarantine_and_fresh"
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    db = tmp_path / "checkpoint.db"
    sid = "cron_jobF"
    _make_checkpoint_db(db, sid)
    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: {
            "run_id": "old", "state": STATE_ORPHANED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    monkeypatch.setattr(cg, "checkpoint_db_path", lambda: db)
    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid=sid, trusted=True,
        budget=RunBudget(run_id="new-run"),
    )
    cg.govern_restore_at_entry(ctx)  # fresh: quarantine happened, no raise
    assert not any(k.startswith(f"{sid}:agent:") for k in _db_keys(db))


def test_govern_backup_failure_fails_run(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    db = tmp_path / "checkpoint.db"
    sid = "cron_jobQ"
    _make_checkpoint_db(db, sid)
    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: {
            "run_id": "old", "state": STATE_TRIPPED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    monkeypatch.setattr(cg, "checkpoint_db_path", lambda: db)
    monkeypatch.setattr(cg, "quarantine_checkpoint", lambda s, db_path=None: (False, None))
    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid=sid, trusted=True,
        budget=RunBudget(run_id="new-run"),
    )
    with pytest.raises(cg.CronCheckpointQuarantined):
        cg.govern_restore_at_entry(ctx)
    # backup failed → original keys must NOT be cleared
    assert f"{sid}:agent:jiuwenswarm:agent_state_blobs" in _db_keys(db)


# ---------------------------------------------------------------- budget rail

@pytest.mark.asyncio
async def test_budget_rail_iteration_cap(guard_enabled, cron_ctx):
    from jiuwenswarm.agents.harness.common.cron_guard.budget_rail import (
        CronBudgetRail,
        CronBudgetExceeded,
    )

    rail = CronBudgetRail()
    cron_ctx.max_iterations = 2
    cron_ctx.budget.max_iterations = 2

    class FakeCtx:
        session = SimpleNamespace(session_id="sid-1")
        def request_force_finish(self, result):
            raise RuntimeError("not consumable here")

    fake = FakeCtx()
    await rail.before_model_call(fake)  # call 1
    await rail.before_model_call(fake)  # call 2
    with pytest.raises(CronBudgetExceeded):
        await rail.before_model_call(fake)  # call 3 > 2


@pytest.mark.asyncio
async def test_budget_rail_force_finish_consumed(guard_enabled, cron_ctx):
    from jiuwenswarm.agents.harness.common.cron_guard.budget_rail import CronBudgetRail

    rail = CronBudgetRail()
    cron_ctx.max_iterations = 1
    cron_ctx.budget.max_iterations = 1
    finished = {}

    class FakeCtx:
        session = SimpleNamespace(session_id="sid-1")
        def request_force_finish(self, result):
            finished["result"] = result

    await rail.before_model_call(FakeCtx())  # call 1 ok
    await rail.before_model_call(FakeCtx())  # over → force finish requested, no raise
    assert "cron_guard_reason" in finished.get("result", {})


@pytest.mark.asyncio
async def test_budget_rail_interactive_noop(guard_enabled):
    from jiuwenswarm.agents.harness.common.cron_guard.budget_rail import CronBudgetRail

    rail = CronBudgetRail()
    calls = []

    class FakeCtx:
        session = SimpleNamespace(session_id="interactive-sid")
        def request_force_finish(self, result):
            calls.append(result)

    for _ in range(100):
        await rail.before_model_call(FakeCtx())
    assert calls == []


@pytest.mark.asyncio
async def test_budget_rail_soft_deadline(guard_enabled, cron_ctx):
    from jiuwenswarm.agents.harness.common.cron_guard.budget_rail import CronBudgetRail

    rail = CronBudgetRail()
    cron_ctx.soft_deadline_reached = True
    finished = {}

    class FakeCtx:
        session = SimpleNamespace(session_id="sid-1")
        def request_force_finish(self, result):
            finished["ok"] = True

    await rail.before_model_call(FakeCtx())
    assert finished.get("ok") is True


# ---------------------------------------------------------------- watchdog begin/end

def test_watchdog_begin_interactive_none(monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    from jiuwenswarm.agents.harness.common.cron_guard import watchdog as wd

    monkeypatch.setattr(wd, "_orphans_marked", True)
    req = SimpleNamespace(metadata={}, params={})
    ctx, token, timer = wd.cron_guard_begin(req, "sid-x")
    assert ctx is None and token is None and timer is None
    wd.cron_guard_end(None, None, None)  # no-op, no raise


def test_watchdog_begin_end_lifecycle(tmp_path, monkeypatch):
    import copy
    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    from jiuwenswarm.agents.harness.common.cron_guard import watchdog as wd
    from jiuwenswarm.agents.harness.common.cron_guard import ledger as led_mod

    led_mod.reset_run_ledger()
    try:
        monkeypatch.setattr(wd, "_orphans_marked", True)
        tok = sign_run_token("rW", "jW", "sW", b"sec")
        req = SimpleNamespace(
            metadata={"cron": {"job_id": "jW", "run_id": "rW", "run_token": tok, "timeout_seconds": 3600}},
            params={},
        )
        ctx, cvar_token, timer = wd.cron_guard_begin(req, "sW")
        assert ctx is not None and ctx.trusted is True
        led = led_mod.get_run_ledger()
        assert led.lookup_run("rW")["state"] == STATE_RUNNING
        wd.cron_guard_end(ctx, cvar_token, timer, final_state="finished")
        assert led.lookup_run("rW")["state"] == STATE_FINISHED
        assert wd.get_run_registry().get("rW") is None
    finally:
        led_mod.reset_run_ledger()


# ---------------------------------------------------------------- review fixes (static scan + code review)

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
    from jiuwenswarm.agents.harness.common.cron_guard.identity import (
        record_shell_wait,
    )

    cron_ctx.budget.tool_wait_budget_seconds = 10.0
    record_shell_wait(time.monotonic() - 12.0)
    assert cron_ctx.budget.shell_seconds >= 12.0
    assert cron_ctx.budget.tool_wait_exceeded() is True


def test_record_shell_wait_no_run_noop(guard_enabled):
    from jiuwenswarm.agents.harness.common.cron_guard.identity import (
        record_shell_wait,
    )

    # no cron run in the contextvar and no session registry hit → silent no-op
    record_shell_wait(time.monotonic() - 5.0)


def test_watchdog_begin_quarantine_rolls_back_registry(tmp_path, monkeypatch):
    """A quarantined entry must not leave the run registered forever."""
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    from jiuwenswarm.agents.harness.common.cron_guard import checkpoint_guard as cg
    from jiuwenswarm.agents.harness.common.cron_guard import ledger as led_mod
    from jiuwenswarm.agents.harness.common.cron_guard import watchdog as wd

    led_mod.reset_run_ledger()
    try:
        monkeypatch.setattr(wd, "_orphans_marked", True)

        def _quarantine(ctx):
            raise cg.CronCheckpointQuarantined("quarantined in test")

        monkeypatch.setattr(cg, "govern_restore_at_entry", _quarantine)
        tok = sign_run_token("rQ", "jQ", "sQ", b"sec")
        req = SimpleNamespace(
            metadata={
                "cron": {
                    "job_id": "jQ",
                    "run_id": "rQ",
                    "run_token": tok,
                    "timeout_seconds": 3600,
                }
            },
            params={},
        )
        with pytest.raises(cg.CronCheckpointQuarantined):
            wd.cron_guard_begin(req, "sQ")
        assert wd.get_run_registry().get("rQ") is None
        assert wd.get_run_registry().get_by_session("sQ") is None
        assert led_mod.get_run_ledger().lookup_run("rQ")["state"] == STATE_TRIPPED
    finally:
        led_mod.reset_run_ledger()
