import asyncio
import copy
import hashlib
from unittest.mock import Mock

import pytest

from segment_state_fixtures import storage
from test_accelerator_manifest import declaration
from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec
from jiuwenswarm.server.runtime.skill_turbo.resume_context import (
    SKILL_TURBO_RESUME_CTX_KEY, SkillStateError, isolated_skill_state,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import (
    LedgerBusy, LedgerError, SkillRunLedger, skill_execution_lease,
)


def spec(name="prep", order=10):
    return AcceleratorSpec.model_validate(declaration(name, order))


def result(name="prep", status="ok", value="topic"):
    return {"status": status, "facts": {f"{name}_fact": value},
            "facts_provenance": {f"{name}_fact": {"verified": True}}, "artifacts": []}


async def open_task(storage, tmp_path, **kwargs):
    return await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(tmp_path),
                                     query="  initial\n query  ", **kwargs)


async def test_identity_readonly_reuse_todos_and_fresh_start(storage, tmp_path):
    session = storage.session()
    assert await SkillRunLedger.current(session, skill="demo-craft", workspace_base=str(tmp_path)) is None
    assert await SkillRunLedger.load(session, skill="demo-craft", task_key="missing") is None
    assert storage.cp.writes == 0 and not (tmp_path / "skill_runs").exists()
    ledger = await open_task(storage, tmp_path)
    expected = hashlib.sha1(f"initial query|{tmp_path.resolve()}|1".encode()).hexdigest()[:12]
    assert ledger.task_key == expected
    from pathlib import Path
    marker = Path(ledger.data["run_dir"]) / "user.txt"
    marker.write_text("keep", encoding="utf-8")
    for fresh in (False, True):
        resumed = await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(tmp_path),
                                            query="changed query", fresh_start=fresh)
        assert resumed.task_key == expected
    other = await open_task(storage, tmp_path, task_id="todo-b")
    assert other.task_key != expected and other.data["identity"]["sequence"] == 2
    repeated = await open_task(storage, tmp_path, task_id="todo-b")
    assert repeated.task_key == other.task_key
    assert (await SkillRunLedger.current(storage.session(), skill="demo-craft", workspace_base=str(tmp_path))).task_key == other.task_key
    assert marker.read_text(encoding="utf-8") == "keep"
    with pytest.raises(LedgerError, match="task_not_found"):
        await open_task(storage, tmp_path, task_key="absent")


@pytest.mark.parametrize("mode", ["before", "drop", "after", "confirm"])
@pytest.mark.parametrize("todo", ["", "todo-b"])
async def test_identity_must_persist_and_confirm_before_business(storage, tmp_path, mode, todo):
    first = await open_task(storage, tmp_path, task_id="todo-a") if todo else None
    business = Mock()
    storage.cp.mode = mode
    with pytest.raises(LedgerError, match="task_creation_failed"):
        ledger = await open_task(storage, tmp_path, task_id=todo)
        business(ledger.task_key)
    business.assert_not_called()
    storage.cp.mode = ""
    storage.cp.fail_next_read = False
    recovered = await open_task(storage, tmp_path, task_id=todo)
    assert recovered.data["identity"]["sequence"] == (2 if todo else 1)
    again = await open_task(storage, tmp_path, task_id=todo)
    assert again.task_key == recovered.task_key
    if first:
        assert (await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=first.task_key)).task_key == first.task_key


async def test_revision_winner_time_winner_and_other_tasks_preserved(storage, tmp_path):
    a = await open_task(storage, tmp_path, task_id="todo-a")
    stale = copy.deepcopy(a)
    b = await open_task(storage, tmp_path, task_id="todo-b")
    a.record_segment(spec=spec(), result=result(value="new"), plan_code_hash="h")
    assert await a.save(storage.session())
    assert await stale.save(storage.session())
    assert stale.data == a.data and stale.save_outcome["winner"] == "stored"
    same_revision = copy.deepcopy(a)
    same_revision.data["updated_at"] += 1
    same_revision.data["facts"]["prep_fact"] = "later"
    assert await same_revision.save(storage.session())
    assert await a.save(storage.session())
    assert a.data["facts"]["prep_fact"] == "later"
    assert (await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=b.task_key)).task_key == b.task_key


async def test_latest_runs_survives_history_and_ordinary_save_failure(storage, tmp_path):
    ledger = await open_task(storage, tmp_path)
    ledger.record_segment(spec=spec(), result=result(), plan_code_hash="h")
    for index in range(35):
        ledger.record_segment(spec=spec("outline", 20), result=result("outline", value=index), plan_code_hash="o")
    assert len(ledger.data["runs"]) == 32
    assert ledger.data["latest_runs"]["prep"]["status"] == "ok"
    assert set(ledger.data["covered_stages"]) == {"prep", "outline"}
    assert await ledger.save(storage.session())
    completed = result(value="actual")
    ledger.record_segment(spec=spec(), result=completed, plan_code_hash="h")
    storage.cp.mode = "before"
    assert not await ledger.save(storage.session())
    assert completed["status"] == "ok" and ledger.data["facts"]["prep_fact"] == "actual"
    assert not ledger.digest()["persisted"]


@pytest.mark.parametrize("mode", ["", "before", "drop", "after", "confirm"])
async def test_rerun_invalidation_is_durable_precondition(storage, tmp_path, mode):
    ledger = await open_task(storage, tmp_path)
    ledger.record_segment(spec=spec(), result=result(), plan_code_hash="h")
    assert await ledger.save(storage.session())
    old_revision = ledger.data["revision"]
    from pathlib import Path
    marker = Path(ledger.data["run_dir"]) / "original.txt"
    marker.write_text("original", encoding="utf-8")
    storage.cp.mode = mode
    business = Mock(side_effect=lambda: marker.write_text("rerun", encoding="utf-8"))
    async with skill_execution_lease(storage.session()):
        if await ledger.prepare_rerun(storage.session(), spec=spec()):
            business()
    assert business.call_count == (1 if not mode else 0)
    assert marker.read_text(encoding="utf-8") == ("original" if mode else "rerun")
    assert not ledger.data["facts_provenance"]["prep_fact"]["verified"]
    assert ledger.data["revision"] == old_revision and len(ledger.data["runs"]) == 1
    storage.cp.mode = ""
    loaded = await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
    assert loaded.data["facts_provenance"]["prep_fact"]["verified"] is (mode in {"before", "drop"})
    assert ("prep" in loaded.data["covered_stages"]) is (mode in {"before", "drop"})


@pytest.mark.parametrize("status", ["paused", "cancelled", "failed"])
async def test_rerun_pause_cancel_result_save_failure_keeps_invalidation(storage, tmp_path, status):
    ledger = await open_task(storage, tmp_path)
    ledger.record_segment(spec=spec(), result=result(), plan_code_hash="h")
    assert await ledger.save(storage.session())
    assert await ledger.prepare_rerun(storage.session(), spec=spec())
    ledger.record_segment(spec=spec(), result={"status": status, "facts": {}}, plan_code_hash="h")
    storage.cp.mode = "before"
    assert not await ledger.save(storage.session())
    storage.cp.mode = ""
    saved = await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
    assert saved.data["revision"] == 1 and len(saved.data["runs"]) == 1
    assert not saved.data["facts_provenance"]["prep_fact"]["verified"]
    assert saved.data["covered_stages"] == []


async def test_session_lease_parallel_busy_pending_and_other_session(storage, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def first():
        async with skill_execution_lease(storage.session()):
            entered.set()
            await release.wait()
    running = asyncio.create_task(first())
    await entered.wait()
    try:
        with pytest.raises(LedgerBusy, match="session_busy"):
            async with skill_execution_lease(storage.session()):
                pytest.fail("parallel business")
        async with skill_execution_lease(storage.session(sid="different")):
            pass
    finally:
        release.set()
        await running
    async with isolated_skill_state(storage.session(), write=True) as state:
        state.replace(SKILL_TURBO_RESUME_CTX_KEY, {"plan_code": "pending"})
    with pytest.raises(LedgerBusy, match="pending_checkpoint"):
        await open_task(storage, tmp_path)


async def test_snapshot_replacement_and_stale_finally_do_not_revive(storage, tmp_path):
    first_session = storage.session()
    ledger = await SkillRunLedger.open(first_session, skill="demo-craft", workspace_base=str(tmp_path))
    await first_session.pre_run(inputs=None)
    ledger.record_segment(spec=spec(), result=result(), plan_code_hash="h")
    assert await ledger.save(first_session)
    independent = storage.session()
    current = await SkillRunLedger.load(independent, skill="demo-craft", task_key=ledger.task_key)
    del current.data["facts"]["prep_fact"]
    current.data["revision"] += 1
    assert await current.save(independent)
    await first_session.post_run()
    fresh = await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
    assert "prep_fact" not in fresh.data["facts"]


async def test_no_default_key_fallback_or_workspace_guess(storage, tmp_path):
    from types import SimpleNamespace
    with pytest.raises(SkillStateError):
        await SkillRunLedger.current(SimpleNamespace(), skill="demo-craft", workspace_base=str(tmp_path))
    with pytest.raises(LedgerError, match="workspace_missing"):
        await open_task(storage, "")
    main = storage.session(turbo=False)
    ledger = await SkillRunLedger.open(main, skill="demo-craft", workspace_base=str(tmp_path))
    await main.pre_run(inputs=None)
    assert main.get_state(SkillRunLedger.KEY) is None
    assert (await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)) is not None


@pytest.mark.parametrize("mode", ["after", "confirm"])
async def test_ambiguous_creation_and_caller_finally_preserve_actual_identity(storage, tmp_path, mode):
    caller = storage.session()
    await caller.pre_run(inputs=None)
    storage.cp.mode = mode
    with pytest.raises(LedgerError, match="task_creation_failed"):
        await SkillRunLedger.open(caller, skill="demo-craft", workspace_base=str(tmp_path), query="original")
    storage.cp.mode = ""
    saved = await SkillRunLedger.current(storage.session(), skill="demo-craft", workspace_base=str(tmp_path))
    await caller.post_run()
    retry = await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(tmp_path), query="rewritten")
    assert retry.task_key == saved.task_key and retry.data["initial_query"] == "original"
    assert retry.data["identity"]["sequence"] == 1


async def test_select_existing_todo_updates_current_without_new_sequence(storage, tmp_path):
    a = await open_task(storage, tmp_path, task_id="a")
    await open_task(storage, tmp_path, task_id="b")
    assert (await open_task(storage, tmp_path, task_id="a")).task_key == a.task_key
    assert (await open_task(storage, tmp_path)).task_key == a.task_key


async def test_abort_and_cancellation_never_become_save_success(storage, tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    from unittest.mock import AsyncMock
    ledger = await open_task(storage, tmp_path)
    monkeypatch.setattr(storage.cp, "post_agent_execute", AsyncMock(side_effect=AbortError("pause")))
    with pytest.raises(AbortError):
        await ledger.save(storage.session())
    with pytest.raises(AbortError):
        await ledger.prepare_rerun(storage.session(), spec=spec())
    monkeypatch.setattr(storage.cp, "post_agent_execute", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await ledger.save(storage.session())
