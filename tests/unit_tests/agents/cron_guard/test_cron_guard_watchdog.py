# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the cron runaway guard (issue #5018)."""

from __future__ import annotations

import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.cron_guard import config as cfg_mod
from jiuwenswarm.common.cron_guard.identity import (
    CronRunContext,
    RunBudget,
    RunRegistry,
    current_cron_run,
    sign_run_token,
)
from jiuwenswarm.common.cron_guard.ledger import (
    STATE_FAILED,
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
    return base


@pytest.fixture
def guard_config(guard_enabled):
    """Mutable enabled config for per-test tweaks."""
    return guard_enabled


# ---------------------------------------------------------------- sleep parser: BLOCK cases


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
    import sqlite3
    import time as _time

    led = CronRunLedger(tmp_path / "ledger.db")
    led.start_run("r1", "j1", "s1")
    led.start_run("r2", "j2", "s2")
    led.finish_run("r1", STATE_FINISHED)
    # SF-4 semantics: only rows left ``running`` by a *previous* process
    # (started before this process imported the module) are orphans.
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.execute(
        "UPDATE cron_runs SET started_at = ? WHERE run_id = 'r2'",
        (_time.time() - 3600,),
    )
    conn.commit()
    conn.close()
    n = led.mark_orphans()
    assert n == 1
    assert led.lookup_run("r2")["state"] == STATE_ORPHANED
    assert led.lookup_run("r1")["state"] == STATE_FINISHED
    # idempotent: no more previous-process running rows
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
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg

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
    """Review root 200900674: empty rows (other session / TOCTOU cleanup) are
    a benign no-op — success with no quarantine prefix, not a backup failure."""
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg

    db = tmp_path / "checkpoint.db"
    _make_checkpoint_db(db, "other_sid")
    ok, prefix = cg.quarantine_checkpoint("cron_missing", db_path=db)
    assert ok is True and prefix is None


def test_quarantine_missing_db_is_benign(tmp_path):
    """Review root 200900674: a missing checkpoint DB is a benign no-op."""
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg

    ok, prefix = cg.quarantine_checkpoint("cron_missing", db_path=tmp_path / "nope.db")
    assert ok is True and prefix is None


def test_govern_restore_at_entry_tripped_prior(monkeypatch, tmp_path):
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
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
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
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
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
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
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
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
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
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


def test_govern_quarantine_benign_noop_allows_run(monkeypatch, tmp_path):
    """Review root 200900674: a benign quarantine no-op ((True, None) — db
    missing / rows already cleaned by another process) must allow the run;
    only a real backup failure ((False, None)) judges it dead."""
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setattr(
        cg, "quarantine_checkpoint", lambda s, db_path=None: (True, None)
    )
    fake_ledger = SimpleNamespace(
        latest_for_sid=lambda s, exclude_run_id=None: {
            "run_id": "old", "state": STATE_TRIPPED, "sid": s
        }
    )
    monkeypatch.setattr(cg, "get_run_ledger", lambda: fake_ledger)
    monkeypatch.setattr(cg, "has_checkpoint_blob", lambda s, db_path=None: True)
    ctx = CronRunContext(
        run_id="new-run", job_id="j", sid="cron_benign", trusted=True,
        budget=RunBudget(run_id="new-run"),
    )
    cg.govern_restore_at_entry(ctx)  # benign branch: must not raise


# ---------------------------------------------------------------- LIKE escaping (review root 200900678)


def test_quarantine_sid_like_wildcards_do_not_cross_sessions(tmp_path):
    """Review root 200900678: a sid containing ``_`` must not glob other
    sessions' checkpoint rows into quarantine (copy + DELETE = data loss)."""
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg

    db = tmp_path / "checkpoint.db"
    _make_checkpoint_db(db, "cron_a", keys={
        "cron_a:agent:jiuwenswarm:agent_state_blobs": "BLOB_A",
        "cronxa:agent:jiuwenswarm:agent_state_blobs": "BLOB_X",
        "cron%a:agent:jiuwenswarm:agent_state_blobs": "BLOB_PCT",
    })
    ok, prefix = cg.quarantine_checkpoint("cron_a", db_path=db)
    assert ok is True and prefix
    keys = _db_keys(db)
    # other sessions' rows stay untouched
    assert "cronxa:agent:jiuwenswarm:agent_state_blobs" in keys
    assert "cron%a:agent:jiuwenswarm:agent_state_blobs" in keys
    # only the exact session's rows moved
    assert not any(k.startswith("cron_a:agent:") for k in keys)
    assert any(k.startswith(f"{prefix}:cron_a:agent:") for k in keys)
    # reversible restore also uses the escaped prefix pattern
    assert cg.restore_quarantined("cron_a", prefix, db_path=db) is True
    keys = _db_keys(db)
    assert "cron_a:agent:jiuwenswarm:agent_state_blobs" in keys
    assert "cronxa:agent:jiuwenswarm:agent_state_blobs" in keys


def test_has_checkpoint_blob_sid_wildcards_escaped(tmp_path):
    """Review root 200900678: probing a sid whose ``_``/``%`` would wildcard
    match another session's keys must return False (exact namespace only)."""
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg

    db = tmp_path / "checkpoint.db"
    _make_checkpoint_db(db, "cronxa")
    assert cg.has_checkpoint_blob("cronxa", db_path=db) is True
    assert cg.has_checkpoint_blob("cron_a", db_path=db) is False  # _ must not match
    assert cg.has_checkpoint_blob("cron%a", db_path=db) is False  # % must not match


# ---------------------------------------------------------------- budget rail

@pytest.mark.asyncio
async def test_budget_rail_iteration_cap(guard_enabled, cron_ctx):
    from jiuwenswarm.common.cron_guard.budget_rail import (
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
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

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
    result = finished.get("result", {})
    # review root 200900676: repo-wide force-finish contract is
    # output/result_type (jiuwenswarm/runtime/iteration_limit.py); a
    # "content" key is not projected by the engine → empty final answer.
    assert "output" in result
    assert result.get("result_type") == "answer"
    assert result.get("code") == "CRON_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_budget_rail_iteration_cap_force_finish_idempotent(guard_enabled, cron_ctx):
    """Review root 200900675: once the iteration cap consumed a force finish,
    later over-limit model calls must not re-inject the finish request (same
    idempotent semantics as the soft-deadline branch)."""
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

    rail = CronBudgetRail()
    cron_ctx.max_iterations = 1
    cron_ctx.budget.max_iterations = 1
    calls = []

    class FakeCtx:
        session = SimpleNamespace(session_id="sid-1")

        def request_force_finish(self, result):
            calls.append(result)

    await rail.before_model_call(FakeCtx())  # call 1: within budget
    assert calls == []
    await rail.before_model_call(FakeCtx())  # call 2: over → force finish
    assert cron_ctx.force_finish_requested is True
    assert len(calls) == 1
    await rail.before_model_call(FakeCtx())  # call 3: idempotent — no re-inject
    await rail.before_model_call(FakeCtx())  # call 4: still no re-inject
    assert len(calls) == 1, (
        "repeated over-limit calls must not re-request force finish"
    )


@pytest.mark.asyncio
async def test_budget_rail_interactive_noop(guard_enabled):
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

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
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

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
    from jiuwenswarm.common.cron_guard import watchdog as wd

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
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod

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


def test_watchdog_begin_quarantine_rolls_back_registry(tmp_path, monkeypatch):
    """A quarantined entry must not leave the run registered forever."""
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    from jiuwenswarm.common.cron_guard import checkpoint_guard as cg
    from jiuwenswarm.common.cron_guard import ledger as led_mod
    from jiuwenswarm.common.cron_guard import watchdog as wd

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


# ---------------------------------------------------------------- async end path + terminal state (AI review 2026-10-14)

def test_cron_guard_end_async_lifecycle(tmp_path, monkeypatch):
    """cron_guard_end_async mirrors the sync end: terminal ledger row + unregister; ctx=None is a no-op."""
    import asyncio
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod

    led_mod.reset_run_ledger()
    try:
        monkeypatch.setattr(wd, "_orphans_marked", True)
        asyncio.run(wd.cron_guard_end_async(None, None, None))  # no-op, no raise
        tok = sign_run_token("rWA", "jWA", "sWA", b"sec")
        req = SimpleNamespace(
            metadata={"cron": {"job_id": "jWA", "run_id": "rWA", "run_token": tok, "timeout_seconds": 3600}},
            params={},
        )
        ctx, cvar_token, timer = wd.cron_guard_begin(req, "sWA")
        assert ctx is not None
        led = led_mod.get_run_ledger()
        asyncio.run(wd.cron_guard_end_async(ctx, cvar_token, timer, final_state="finished"))
        assert led.lookup_run("rWA")["state"] == STATE_FINISHED
        assert wd.get_run_registry().get("rWA") is None
    finally:
        led_mod.reset_run_ledger()


def test_cron_guard_end_async_reap_off_event_loop(tmp_path, monkeypatch):
    """The blocking reap poll must run on a worker thread: a loop task proceeds
    while reap is still blocked (and reap itself runs off the main thread)."""
    import asyncio
    import copy
    import threading

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod

    led_mod.reset_run_ledger()
    try:
        ctx = SimpleNamespace(run_id="rOff", budget=SimpleNamespace(snapshot=lambda: {}))
        release = threading.Event()
        order = []
        ran_off_main = {}

        def blocked_reap(run_id, kill_grace_seconds=3.0):
            ran_off_main["value"] = threading.current_thread() is not threading.main_thread()
            order.append("reap_start")
            release.wait(timeout=5)
            order.append("reap_end")
            return True

        reg = wd.get_run_registry()
        monkeypatch.setattr(reg, "reap", blocked_reap)

        async def probe():
            order.append("probe")
            release.set()

        async def main():
            task = asyncio.create_task(
                wd.cron_guard_end_async(ctx, None, None, final_state="finished")
            )
            await asyncio.sleep(0.05)  # let the end path enter to_thread(reap)
            await probe()
            await task

        asyncio.run(main())
        assert ran_off_main["value"] is True
        assert order == ["reap_start", "probe", "reap_end"]
    finally:
        led_mod.reset_run_ledger()


def test_cron_guard_end_async_survives_reap_raise(tmp_path, monkeypatch):
    """never-raises contract holds on the async path; the ledger still gets its terminal row."""
    import asyncio
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod

    led_mod.reset_run_ledger()
    try:
        ctx = SimpleNamespace(run_id="rBR", budget=SimpleNamespace(snapshot=lambda: {}))

        def boom(run_id, kill_grace_seconds=3.0):
            raise RuntimeError("reap exploded")

        reg = wd.get_run_registry()
        monkeypatch.setattr(reg, "reap", boom)
        asyncio.run(wd.cron_guard_end_async(ctx, None, None, final_state="finished"))
        row = led_mod.get_run_ledger().lookup_run("rBR")
        assert row["state"] == STATE_FINISHED
    finally:
        led_mod.reset_run_ledger()


def test_cron_guard_end_async_cancelled_reap_still_finishes(tmp_path, monkeypatch):
    """Review root 200900679: cancellation while awaiting the reap (hard
    deadline / client disconnect) must still land the terminal ledger row —
    the row would otherwise stay ``running`` and be mislabelled orphaned by
    mark_orphans at the next process start."""
    import asyncio
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod

    led_mod.reset_run_ledger()
    try:
        ctx = SimpleNamespace(run_id="rCX", budget=SimpleNamespace(snapshot=lambda: {}))

        def cancelled(run_id, kill_grace_seconds=3.0):
            raise asyncio.CancelledError()

        reg = wd.get_run_registry()
        monkeypatch.setattr(reg, "reap", cancelled)
        # cancellation semantics are preserved (CancelledError propagates)…
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(wd.cron_guard_end_async(ctx, None, None, final_state="finished"))
        # …but the ledger still got its terminal row (fallback insert path)
        row = led_mod.get_run_ledger().lookup_run("rCX")
        assert row is not None
        assert row["state"] == STATE_FINISHED
        assert row["reaped"] == 0
    finally:
        led_mod.reset_run_ledger()


def test_cron_terminal_state_mapping():
    """Guard trips are 'tripped'; a producer crash is 'failed'; clean is 'finished'."""
    from jiuwenswarm.common.cron_guard.watchdog import cron_terminal_state

    assert cron_terminal_state(None) == "finished"
    assert cron_terminal_state("deadline_exceeded") == "tripped"
    assert cron_terminal_state("budget_exceeded") == "tripped"
    assert cron_terminal_state("producer_error") == "failed"


def test_cron_guard_end_async_producer_error_records_failed(tmp_path, monkeypatch):
    """A producer crash must land in the ledger as state=failed with its reason, not a green finished."""
    import asyncio
    import copy

    base = copy.deepcopy(cfg_mod.DEFAULTS)
    base["enabled"] = True
    base["budget_ledger"]["path"] = str(tmp_path / "ledger.db")
    monkeypatch.setattr(cfg_mod, "get_cron_guard_config", lambda: base)
    monkeypatch.setenv("JIUWENSWARM_CRON_RUN_SECRET", "sec")
    from jiuwenswarm.common.cron_guard import watchdog as wd
    from jiuwenswarm.common.cron_guard import ledger as led_mod
    from jiuwenswarm.common.cron_guard import identity as id_mod

    led_mod.reset_run_ledger()
    try:
        monkeypatch.setattr(wd, "_orphans_marked", True)
        tok = sign_run_token("rPE", "jPE", "sPE", b"sec")
        req = SimpleNamespace(
            metadata={"cron": {"job_id": "jPE", "run_id": "rPE", "run_token": tok, "timeout_seconds": 3600}},
            params={},
        )
        ctx, cvar_token, timer = wd.cron_guard_begin(req, "sPE")
        assert ctx is not None
        led = led_mod.get_run_ledger()
        asyncio.run(wd.cron_guard_end_async(
            ctx, cvar_token, timer,
            final_state=wd.cron_terminal_state("producer_error"),
            trip_reason="producer_error",
        ))
        row = led.lookup_run("rPE")
        assert row["state"] == STATE_FAILED
        assert row["trip_reason"] == "producer_error"
    finally:
        # asyncio.run() isolates contextvars in a copied context, so the
        # current_cron_run.reset inside cron_guard_end_async never reaches
        # this thread — restore it here or rPE leaks into later tests.
        id_mod.current_cron_run.set(None)
        led_mod.reset_run_ledger()


def test_ledger_finish_fallback_insert_missing_start(tmp_path):
    """finish_run for a run_id whose start row is missing must still land the
    terminal row (the fallback INSERT bound 5 params against 6 placeholders
    and silently lost the write)."""
    led = CronRunLedger(tmp_path / "ledger.db")
    led.finish_run("rFB", STATE_FAILED, trip_reason="producer_error", reaped=True)
    row = led.lookup_run("rFB")
    assert row is not None
    assert row["state"] == STATE_FAILED
    assert row["trip_reason"] == "producer_error"
    assert row["reaped"] == 1
    assert row["started_at"] > 0
    assert row["ended_at"] >= row["started_at"]



def test_lazy_facade_resolves_package_root_names():
    """AIReview 10209 id=200915741：门面 __getattr__ 的 import_module 必须带
    包全名前缀（裸名会被当顶层模块导入，__all__ 经门面 100% 失败）。锁定
    package 根名解析契约：config/identity/ledger 三面全部经门面可达。"""
    import importlib
    pkg = importlib.import_module("jiuwenswarm.common.cron_guard")
    assert pkg.get_cron_guard_config is not None
    assert pkg.CronRunContext is not None
    assert pkg.CronRunLedger is not None
    assert pkg.clamp_deadlines is not None
    assert pkg.current_cron_run is not None
    assert pkg.STATE_RUNNING == "running"


def test_lazy_facade_missing_module_raises_attribute_error(monkeypatch):
    """Review root 200900677: when a split PR has not landed its submodule
    yet, the facade must raise AttributeError (the getattr protocol) instead
    of ModuleNotFoundError, so hasattr()/default probing stays fail-open."""
    import importlib
    import sys

    pkg = sys.modules["jiuwenswarm.common.cron_guard"]
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name.startswith("jiuwenswarm.common.cron_guard."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(AttributeError):
        pkg.get_cron_guard_config  # noqa: B018 — attribute probe on purpose
    with pytest.raises(AttributeError):
        pkg.STATE_RUNNING  # noqa: B018
    # unknown names keep the plain AttributeError path
    with pytest.raises(AttributeError):
        pkg.definitely_not_there  # noqa: B018


# ── R6 检视整改回归（MF-2 / SF-4）─────────────────────────────────────


def test_default_ledger_path_shares_checkpoint_root(tmp_path, monkeypatch):
    """MF-2: ledger path resolves through the checkpointer's own chain."""
    from jiuwenswarm.common import paths as cw_paths
    from jiuwenswarm.common import utils as cw_utils
    from jiuwenswarm.common.cron_guard import ledger as ledger_mod

    # paths refactor: _workspace_base_dir moved utils -> paths
    monkeypatch.setattr(cw_paths, "_workspace_base_dir", tmp_path)
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


def test_leftover_orphan_marking_runs_exactly_once_under_contention():
    """SF-4: concurrent cron_guard entrants mark leftovers at most once."""
    import threading as _threading
    import time as _time

    from jiuwenswarm.common.cron_guard import watchdog as wd

    calls = []
    barrier = _threading.Barrier(4)

    class _CountingLedger:
        def mark_orphans(self):
            calls.append(1)
            _time.sleep(0.05)
            return 0

    original = wd._orphans_marked
    wd._orphans_marked = False
    try:
        def _enter():
            barrier.wait()
            wd._mark_leftover_orphans_once(_CountingLedger())

        threads = [_threading.Thread(target=_enter) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(calls) == 1
    finally:
        wd._orphans_marked = original


@pytest.mark.asyncio
async def test_budget_rail_contextvar_cross_session_noop(guard_enabled):
    """Leak regression #2 (PR 10210 CI UT, 242-failure permission cluster):
    a cron context leaked into a reused worker ContextVar must not govern a
    request whose session id differs; only the run's own session is governed.
    """
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

    leaked = CronRunContext(
        run_id="leak-run-2",
        job_id="job-leak-2",
        sid="cron-session",
        trusted=True,
        max_iterations=1,
        base_timeout_seconds=3600,
        deadline_soft=time.monotonic() + 3000,
        deadline_hard=time.monotonic() + 3400,
        budget=RunBudget(run_id="leak-run-2", max_iterations=1),
    )
    token = current_cron_run.set(leaked)
    try:
        rail = CronBudgetRail()
        calls = []

        class ForeignCtx:
            session = SimpleNamespace(session_id="web-session")

            def request_force_finish(self, result):
                calls.append(result)

        class OwnCtx:
            session = SimpleNamespace(session_id="cron-session")

            def request_force_finish(self, result):
                calls.append(result)

        foreign = ForeignCtx()
        for _ in range(5):
            await rail.before_model_call(foreign)
        assert calls == []
        assert leaked.budget.model_calls == 0

        own = OwnCtx()
        for _ in range(3):
            await rail.before_model_call(own)
        assert leaked.budget.model_calls == 3
        assert calls, "own-session run must still be governed"
    finally:
        current_cron_run.reset(token)


@pytest.mark.asyncio
async def test_budget_rail_ignores_registry_only_registration(guard_enabled):
    """Leak regression (PR 10210 CI UT): a cron run present in the process
    registry but NOT bound via ContextVar must never govern the same session's
    interactive requests.  Registrations can outlive their entry generator
    (abandoned/cancelled stream skips cron_guard_end), and a later interactive
    run that reuses the session id must stay untouched (design §3.1).
    """
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail
    from jiuwenswarm.common.cron_guard.identity import get_run_registry

    leaked = CronRunContext(
        run_id="leak-run",
        job_id="job-leak",
        sid="session-1",
        trusted=True,
        max_iterations=1,
        base_timeout_seconds=3600,
        deadline_soft=time.monotonic() + 3000,
        deadline_hard=time.monotonic() + 3400,
        budget=RunBudget(run_id="leak-run", max_iterations=1),
    )
    reg = get_run_registry()
    reg.register(leaked)
    try:
        rail = CronBudgetRail()
        calls = []

        class FakeCtx:
            session = SimpleNamespace(session_id="session-1")

            def request_force_finish(self, result):
                calls.append(result)

        for _ in range(5):
            await rail.before_model_call(FakeCtx())
        assert calls == []
        assert leaked.budget.model_calls == 0
        assert not leaked.force_finish_requested
    finally:
        reg.unregister("leak-run")


def test_ledger_finish_run_backfill_carries_job_sid(tmp_path):
    """atomgit 2026-11-18 #10210：start 早先写失败时 finish_run 补插的终态行
    必须携带真实 job_id/sid。

    写死空串＝终态行永远无法按 sid 聚合，checkpoint_guard.govern_restore_at_entry
    的 latest_for_sid 视其为「首次运行」直接放行，「上一轮 tripped/failed 后
    检查点被原样复活」这一 issue #5018 核心场景检测失效。
    """
    led = CronRunLedger(tmp_path / "ledger.db")
    # 无 start_run —— 模拟「start 写失败」面：finish_run 直接补插终态行。
    led.finish_run("r-nostart", STATE_TRIPPED, trip_reason="deadline_exceeded",
                   job_id="j-backfill", sid="s-backfill")
    prior = led.latest_for_sid("s-backfill", exclude_run_id="other")
    assert prior is not None
    assert prior["job_id"] == "j-backfill"
    assert prior["trip_reason"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_budget_rail_tool_wait_force_finish_idempotent(guard_enabled, cron_ctx):
    """atomgit 2026-11-18：tool_wait 超限分支补 force_finish 幂等守卫——
    与迭代/软截止分支同判据，finish 已消费后不得重复注入。修复前：每次
    before_model_call 重复注入 finish＝红。"""
    from jiuwenswarm.common.cron_guard.budget_rail import CronBudgetRail

    rail = CronBudgetRail()
    cron_ctx.budget.tool_wait_exceeded = lambda: True
    calls = []

    class FakeCtx:
        session = SimpleNamespace(session_id="sid-1")

        def request_force_finish(self, result):
            calls.append(result)

    await rail.before_model_call(FakeCtx())  # 1st: exceeded -> force finish
    assert cron_ctx.force_finish_requested is True
    assert len(calls) == 1
    await rail.before_model_call(FakeCtx())  # already consumed -> idempotent
    await rail.before_model_call(FakeCtx())
    assert len(calls) == 1, "tool_wait overrun must not re-request force finish"
