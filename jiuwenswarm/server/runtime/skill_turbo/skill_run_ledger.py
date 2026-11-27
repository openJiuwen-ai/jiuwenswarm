"""跨段任务身份、事实与有限历史；不执行节点、不接管HITL断点。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.resume_context import (
    SKILL_TURBO_RESUME_CTX_KEY,
    SkillStateError,
    isolated_skill_state,
    skill_state_identity,
)

logger = logging.getLogger(__name__)
_execution_owners: dict[tuple[str, str], asyncio.Task] = {}
_directory_io = asyncio.Semaphore(4)


class LedgerError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class LedgerBusy(LedgerError):
    pass


def task_key_for(query: str, workspace_base: str, sequence: int) -> str:
    normalized = " ".join(query.split())
    return hashlib.sha1(f"{normalized}|{workspace_base}|{sequence}".encode("utf-8")).hexdigest()[:12]


def _task_paths(skill: str, workspace_base: str, task_key: str = "") -> tuple[str, str]:
    if not workspace_base:
        raise LedgerError("workspace_missing")
    if not skill or skill in {".", ".."} or any(c in skill for c in "/\\:"):
        raise LedgerError("invalid_skill")
    workspace = Path(workspace_base)
    if not workspace.is_absolute():
        raise LedgerError("workspace_must_be_absolute")
    workspace = workspace.resolve()
    run = (workspace / "skill_runs" / skill / task_key).resolve()
    if not run.is_relative_to(workspace):
        raise LedgerError("run_dir_outside_workspace")
    return str(workspace), str(run)


def _active_key(skill: str, workspace: str, task_id: str) -> str:
    return hashlib.sha256(json.dumps([skill, workspace, task_id], ensure_ascii=False).encode()).hexdigest()


def _read_book(state) -> dict:
    book = state.get(SkillRunLedger.KEY)
    if book is None:
        return {"version": 1, "sequence": 0, "active": {}, "tasks": {}}
    if (not isinstance(book, dict) or book.get("version") != 1
            or not isinstance(book.get("tasks"), dict) or not isinstance(book.get("active"), dict)
            or type(book.get("sequence")) is not int):
        raise SkillStateError("invalid skill ledger book")
    return book


def _ordering(task: dict) -> tuple[int, int]:
    return task["revision"], task["updated_at"]


def _touch(task: dict) -> None:
    task["updated_at"] = max(time.time_ns(), task.get("updated_at", 0) + 1)


def _coverage(task: dict) -> list[str]:
    return sorted({stage for run in task["latest_runs"].values() if run["status"] == "ok"
                   for stage in run["covered_stages"]})


@asynccontextmanager
async def skill_execution_lease(session: Any, *, resume_ctx: dict | None = None):
    """会话/card级单进程租约；暂停断点继续占据业务槽，读台账不需要租约。"""
    identity = skill_state_identity(session)
    owner = asyncio.current_task()
    existing = _execution_owners.get(identity)
    if existing is not None:
        if existing is owner:
            yield
            return
        raise LedgerBusy("session_busy")
    _execution_owners[identity] = owner
    try:
        async with isolated_skill_state(session) as state:
            pending = state.get(SKILL_TURBO_RESUME_CTX_KEY)
        if isinstance(pending, dict) and (pending.get("plan_code") or pending.get("resume_in_flight")):
            # 只有已经核验的同一断点可接管自己的槽；新调用和旧答案仍拒绝。
            if resume_ctx is None or pending != resume_ctx:
                raise LedgerBusy("pending_checkpoint")
        yield
    finally:
        _execution_owners.pop(identity, None)


class SkillRunLedger:
    KEY = "__skill_turbo_skill_ledger__"

    def __init__(self, data: dict):
        self.data = copy.deepcopy(data)
        self.persisted = True
        self.save_outcome: dict[str, Any] = {}

    @property
    def task_key(self) -> str:
        return self.data["task_key"]

    @classmethod
    async def current(cls, session, *, skill: str, workspace_base: str, task_id: str = ""):
        async with _directory_io:
            workspace, _ = await asyncio.to_thread(_task_paths, skill, workspace_base)
        async with isolated_skill_state(session) as state:
            book = _read_book(state)
            key = book["active"].get(_active_key(skill, workspace, task_id))
            task = book["tasks"].get(key)
        return cls(task) if task is not None else None

    @classmethod
    async def load(cls, session, *, skill: str, task_key: str):
        async with isolated_skill_state(session) as state:
            task = _read_book(state)["tasks"].get(task_key)
        if task is None or task.get("skill") != skill:
            return None
        return cls(task)

    @classmethod
    async def open(cls, session, *, skill: str, workspace_base: str, task_id: str = "",
                   query: str = "", task_key: str = "", fresh_start: bool = False):
        # fresh_start在段路径没有身份/清理语义；唯一创建信号是新todo关联。
        async with _directory_io:
            workspace, _ = await asyncio.to_thread(_task_paths, skill, workspace_base)
        try:
            async with skill_execution_lease(session):
                async with isolated_skill_state(session, write=True) as state:
                    book = _read_book(state)
                    active_key = _active_key(skill, workspace, task_id)
                    existing = task_key or book["active"].get(active_key)
                    task = book["tasks"].get(existing)
                    if existing and task is None:
                        raise LedgerError("task_not_found")
                    if task is not None:
                        if task["skill"] != skill or task["workspace_base"] != workspace:
                            raise LedgerError("task_identity_mismatch")
                        current_key = _active_key(skill, workspace, "")
                        if not task_key and book["active"].get(current_key) != existing:
                            book["active"][current_key] = existing
                            state.replace(cls.KEY, book)
                            await state.commit()
                    else:
                        sequence = book["sequence"] + 1
                        key = task_key_for(query, workspace, sequence)
                        async with _directory_io:
                            _, run_dir = await asyncio.to_thread(_task_paths, skill, workspace, key)
                        task = {
                            "version": 1, "skill": skill, "task_key": key, "workspace_base": workspace,
                            "run_dir": run_dir, "initial_query": query,
                            "identity": {"normalized_query": " ".join(query.split()), "sequence": sequence,
                                         "task_id": task_id},
                            "revision": 0, "updated_at": time.time_ns(), "facts": {"run_dir": run_dir},
                            "facts_provenance": {}, "artifacts": [], "latest_runs": {}, "runs": [],
                            "covered_stages": [],
                        }
                        book["sequence"] = sequence
                        book["tasks"][key] = task
                        book["active"][active_key] = key
                        book["active"][_active_key(skill, workspace, "")] = key
                        state.replace(cls.KEY, book)
                        await state.commit()
                async with _directory_io:
                    _, actual = await asyncio.to_thread(_task_paths, skill, workspace, task["task_key"])
                    if actual != task["run_dir"]:
                        raise LedgerError("run_dir_changed")
                    await asyncio.to_thread(Path(actual).mkdir, parents=True, exist_ok=True)
                return cls(task)
        except (AbortError, LedgerError):
            raise
        except Exception as exc:
            logger.warning("[SkillLedger] task creation/lookup failed: %s", exc)
            raise LedgerError("task_creation_failed") from exc

    async def save(self, session) -> bool:
        try:
            async with isolated_skill_state(session, write=True) as state:
                book = _read_book(state)
                saved = book["tasks"].get(self.task_key)
                if saved is None or saved["skill"] != self.data["skill"]:
                    raise SkillStateError("task identity is not persisted")
                local_wins = _ordering(self.data) > _ordering(saved)
                conflict = self.data != saved
                selected = copy.deepcopy(self.data if local_wins else saved)
                book["tasks"][self.task_key] = selected
                state.replace(self.KEY, book)
                await state.commit()
                self.data = selected
                self.persisted = True
                self.save_outcome = {"persisted": True, "winner": "local" if local_wins else "stored",
                                     "conflict": conflict, "revision": selected["revision"]}
                return True
        except AbortError:
            raise
        except Exception as exc:
            self.persisted = False
            self.save_outcome = {"persisted": False, "revision": self.data["revision"], "error": str(exc)}
            logger.warning("[SkillLedger] result save failed (business result retained): %s", exc)
            return False

    async def prepare_rerun(self, session, *, spec) -> bool:
        try:
            async with skill_execution_lease(session):
                async with isolated_skill_state(session, write=True) as state:
                    book = _read_book(state)
                    task = book["tasks"].get(self.task_key)
                    if task is None or task["skill"] != self.data["skill"]:
                        raise SkillStateError("task identity is not persisted")
                    if _ordering(self.data) > _ordering(task):
                        task = copy.deepcopy(self.data)
                        book['tasks'][self.task_key] = task
                    for provenance in task["facts_provenance"].values():
                        if provenance.get("produced_by") == spec.id:
                            provenance["verified"] = False
                    latest = task["latest_runs"].get(spec.id)
                    if latest is not None:
                        latest["status"] = "invalidated"
                    task["covered_stages"] = _coverage(task)
                    task["invalidation_token"] = uuid.uuid4().hex
                    _touch(task)
                    # 未确认的尝试也不能把当前内存里的旧事实继续当作已验证产出。
                    self.data = copy.deepcopy(task)
                    self.persisted = False
                    state.replace(self.KEY, book)
                    await state.commit()
                    self.data = copy.deepcopy(task)
                    self.persisted = True
                    self.save_outcome = {"persisted": True, "revision": task["revision"],
                                         "invalidation_token": task["invalidation_token"]}
                    return True
        except AbortError:
            raise
        except LedgerBusy:
            raise
        except Exception as exc:
            self.persisted = False
            self.save_outcome = {"persisted": False, "error": str(exc), "revision": self.data["revision"]}
            logger.warning("[SkillLedger] rerun invalidation failed; do not start business: %s", exc)
            return False

    def record_segment(self, *, spec, result: dict, plan_code_hash: str) -> None:
        if result["status"] in {"paused", "cancelled"}:
            return
        revision = self.data["revision"] + 1
        updated = {}
        if any(self.data["facts_provenance"].get(key, {}).get("produced_by") not in {None, spec.id}
               for key in spec.outputs.facts):
            raise LedgerError("fact_owner_mismatch")
        for key, provenance in self.data["facts_provenance"].items():
            if provenance.get("produced_by") == spec.id:
                provenance["verified"] = False
        for key in spec.outputs.facts:
            old = self.data["facts_provenance"].get(key, {})
            provenance = copy.deepcopy(result.get("facts_provenance", {}).get(key, {}))
            provenance.update(produced_by=spec.id, revision=revision, at=time.time_ns())
            provenance["verified"] = bool(provenance.get("verified")) and result["status"] in {"ok", "partial"}
            if key in result.get("facts", {}):
                if old:
                    updated[key] = {"produced_by": spec.id, "previous_revision": old.get("revision"), "revision": revision}
                self.data["facts"][key] = copy.deepcopy(result["facts"][key])
            else:
                provenance["verified"] = False
            self.data["facts_provenance"][key] = provenance
        run = {"accelerator": spec.id, "status": result["status"], "revision": revision,
               "plan_code_hash": plan_code_hash, "covered_stages": list(spec.covers.stages),
               "consumed_facts": copy.deepcopy(result.get("consumed_facts", {}))}
        for key in ("batch_id", "batch_index", "elapsed_seconds", "delivery_confirmed", "delivery_owner", "delivery_evidence", "params"):
            if key in result:
                run[key] = result[key]
        self.data["artifacts"] = [a for a in self.data["artifacts"] if a.get("produced_by") != spec.id]
        self.data["artifacts"].extend({**copy.deepcopy(a), "produced_by": spec.id, "revision": revision}
                                      for a in result.get("artifacts", []))
        self.data["latest_runs"][spec.id] = run
        self.data["runs"] = (self.data["runs"] + [copy.deepcopy(run)])[-32:]
        self.data["covered_stages"] = _coverage(self.data)
        self.data["revision"] = revision
        _touch(self.data)
        self.persisted = False
        result["facts_provenance"] = {key: copy.deepcopy(self.data["facts_provenance"][key])
                                       for key in spec.outputs.facts}
        result["fact_updated_by"] = updated

    def digest(self) -> dict:
        return {"skill": self.data["skill"], "task_key": self.task_key, "revision": self.data["revision"],
                "run_dir": self.data["run_dir"], "covered_stages": list(self.data["covered_stages"]),
                "persisted": self.persisted, "save": copy.deepcopy(self.save_outcome)}
