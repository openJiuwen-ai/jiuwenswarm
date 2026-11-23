# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable execution intent and bounded recovery of interrupted Team runtimes.

The Runner owns checkpoints and member recovery. This module owns only the
application's decision to resume, never a copy of model inputs or tool calls.
All disk operations are called off the business loop by asynchronous callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
import uuid
from typing import Any

from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.supervisor.identity import deployment_scope

logger = logging.getLogger(__name__)
_KEY = "team_recovery"
_MAX_ATTEMPTS = 3
_STABLE_SECONDS = 600.0
_SCAN_SECONDS = 5.0


class RecoveryAdmissionError(RuntimeError):
    """A platform-authored recovery reason safe to expose without payloads."""


def begin(session_id: str, recipe: dict[str, Any], *, token: str | None = None) -> str:
    """Arm recovery for a real submission, or validate an acquired recovery."""
    with lifecycle.resource_lock("session", session_id):
        lifecycle.guard(session_id)
        lifecycle.assert_runtime_owner(session_id)
        value = lifecycle.state("session", session_id)
        if token is not None:
            record = value.get(_KEY, {})
            if record.get("token") != token or record.get("state") != "recovering":
                raise RuntimeError("Team recovery was superseded or stopped")
            return token
        token = uuid.uuid4().hex
        value[_KEY] = {
            "token": token, "state": "running", "desired": True,
            "recipe": recipe, "deployment": deployment_scope(),
            "owner": lifecycle.current_runtime_owner(), "attempts": 0,
            "started_at": time.time(), "updated_at": time.time(),
        }
        lifecycle.save_locked("session", session_id, value)
        return token


def snapshot(session_id: str) -> dict[str, Any]:
    """Return public recovery facts, without assembly arguments or owner data."""
    record = lifecycle.state("session", session_id).get(_KEY, {})
    result = {}
    for key in ("state", "attempts", "updated_at", "last_progress_at", "error"):
        if key in record:
            result[key] = record[key]
    return result


def recovery_instructions(session_id: str) -> str:
    """Read original human instructions as references, never tool replay input.

    A crash inside the first native turn can precede its context checkpoint.
    The application's durable transcript still owns the user's task. Refuse
    partial reconstruction rather than silently dropping early scope changes.
    """
    from jiuwenswarm.server.runtime.session.session_history import (
        get_read_history_path, is_agent_initiated_record, iter_history_jsonl_rows, load_history_records,
    )

    path = get_read_history_path(session_id)
    rows = iter_history_jsonl_rows(path) if path.suffix == ".jsonl" else iter(load_history_records(session_id))
    instructions = []
    size = 0
    for row in rows:
        if row.get("role") not in {"user", "human"} or row.get("subagent_id") or is_agent_initiated_record(row):
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        size += len(content.encode("utf-8"))
        if size > 65536:
            raise RecoveryAdmissionError("Original task instructions exceed automatic recovery context budget")
        instructions.append(content)
    if not instructions:
        raise RecoveryAdmissionError("Original task instructions are unavailable; refusing a goal-free wakeup")
    return json.dumps(instructions, ensure_ascii=False)


def stop(session_id: str, reason: str, *, token: str | None = None) -> bool:
    """Fence a pending takeover before an explicit user lifecycle action."""
    with lifecycle.resource_lock("session", session_id):
        value = lifecycle.state("session", session_id)
        record = value.get(_KEY)
        if not record:
            return False
        if token is not None and record.get("token") != token:
            return False
        try:
            lifecycle.write_guard(session_id)
        except lifecycle.LifecycleError:
            # Stop is a drain write, not a new execution admission. Once writes
            # are isolated, leave the lifecycle record untouched.
            return False
        lifecycle.assert_runtime_owner(session_id)
        record.update(desired=False, state=reason, updated_at=time.time())
        lifecycle.save_locked("session", session_id, value)
        return True


def observe(session_id: str, token: str, event: dict[str, Any]) -> bool:
    """Persist lifecycle boundaries, not every streamed token or audit event."""
    kind = event.get("event_type")
    states = {
        "team.recovered": "restored", "team.completed": "completed",
        "team.idle": "idle", "chat.ask_user_question": "waiting_for_input",
        "chat.error": "failed", "chat.tool_call": "running", "chat.delta": "running",
    }
    target = states.get(kind)
    if target is None:
        return False
    with lifecycle.resource_lock("session", session_id):
        value = lifecycle.state("session", session_id)
        record = value.get(_KEY, {})
        if record.get("token") != token or record.get("state") in {"paused", "cancelled"}:
            return False
        if target in {"restored", "idle"} and record.get("state") == "waiting_for_input":
            return False
        changed = record.get("state") != target
        now = time.time()
        if target == "running":
            # Write at most once per minute while work is already progressing.
            if record.get("state") == target and now - record.get("last_progress_at", 0) < 60:
                return False
            record["last_progress_at"] = now
            if now - record.get("started_at", now) >= _STABLE_SECONDS:
                record["attempts"] = 0
        record.update(state=target, updated_at=now)
        record["desired"] = target in {"running", "restored"}
        if target == "failed":
            record["error"] = "Team stream failed; inspect the session error for details"
        lifecycle.save_locked("session", session_id, value)
        return changed


def candidates() -> list[str]:
    """Only enumerate explicit intents; never infer execution from old busy rows."""
    root = lifecycle.get_agent_root_dir() / "lifecycle" / "resources"
    result = []
    for path in root.glob("session_*.json"):
        try:
            value = lifecycle.read_json(path)
            if value.get("blocked") or value.get("deleted"):
                continue
            record = value.get(_KEY, {})
            if record.get("desired") and record.get("deployment") == deployment_scope():
                result.append(path.stem[len("session_"):])
        except Exception:
            logger.exception("[TeamRecovery] unreadable intent file=%s", path.name)
    return result


def claim(session_id: str) -> dict[str, Any] | None:
    """Atomically acquire an orphaned intent, fenced by the live runtime owner."""
    with lifecycle.resource_lock("session", session_id):
        value = lifecycle.state("session", session_id)
        record = value.get(_KEY, {})
        if not record.get("desired") or record.get("deployment") != deployment_scope():
            return None
        lifecycle.guard(session_id)
        lifecycle.assert_runtime_owner(session_id)
        owner = record.get("owner") or {}
        if owner and lifecycle.runtime_owner_alive(owner):
            return None
        now = time.time()
        if now < record.get("retry_at", 0):
            return None
        if record.get("attempts", 0) >= _MAX_ATTEMPTS:
            record.update(state="recovery_failed", desired=False, updated_at=now,
                          error="Consecutive Team recovery attempts exhausted")
            lifecycle.save_locked("session", session_id, value)
            return dict(record)
        record.update(
            token=uuid.uuid4().hex, state="recovering", updated_at=now,
            owner=lifecycle.current_runtime_owner(), started_at=now,
            attempts=record.get("attempts", 0) + 1,
        )
        value["runtime_owner"] = record["owner"]
        lifecycle.save_locked("session", session_id, value)
        logger.info("[TeamRecovery] acquired session=%s attempt=%s owner=%s token=%s",
                    session_id, record["attempts"], record["owner"], record["token"])
        return dict(record)


def failed(session_id: str, token: str, error: Exception) -> None:
    """Release a failed admission for bounded retry without resurrecting stops."""
    with lifecycle.resource_lock("session", session_id):
        value = lifecycle.state("session", session_id)
        record = value.get(_KEY, {})
        if record.get("token") != token or not record.get("desired"):
            return
        exhausted = record.get("attempts", 0) >= _MAX_ATTEMPTS
        record.update(
            state="recovery_failed" if exhausted else "retrying",
            desired=not exhausted, owner={}, updated_at=time.time(),
            retry_at=time.time() + min(60, 5 * 2 ** record.get("attempts", 0)),
            error=(str(error) if isinstance(error, RecoveryAdmissionError)
                   else f"{type(error).__name__}: Team recovery admission failed; inspect server logs"),
        )
        lifecycle.save_locked("session", session_id, value)


def prepare_wakeup(session_id: str, token: str) -> bool:
    """Fence one semantic recovery notice after the SDK's recovered gate.

    This is not a replay of the old request. A lost receipt is not retried
    inside the same generation, since it might already have reached the leader.
    """
    with lifecycle.resource_lock("session", session_id):
        lifecycle.guard(session_id)
        value = lifecycle.state("session", session_id)
        record = value.get(_KEY, {})
        if record.get("token") != token or record.get("state") not in {"restored", "idle", "running"}:
            return False
        if record.get("wakeup_token") == token:
            return False
        record.update(wakeup_token=token, desired=True, state="restored", updated_at=time.time())
        lifecycle.save_locked("session", session_id, value)
        return True


class TeamRecoveryService:
    """Worker-owned scanner; survives Front detach without needing a browser."""

    def __init__(
        self, restore: Callable[[str, dict[str, Any]], Awaitable[None]],
        publish: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._restore = restore
        self._publish = publish
        self._task: asyncio.Task | None = None
        self._limit = asyncio.Semaphore(2)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="team-recovery")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def recover_once(self) -> None:
        async def limited(session_id: str) -> None:
            async with self._limit:
                await self._recover_one(session_id)

        sessions = await asyncio.to_thread(candidates)
        await asyncio.gather(*(limited(session_id) for session_id in sessions))

    async def _recover_one(self, session_id: str) -> None:
        record = None
        try:
            record = await asyncio.to_thread(claim, session_id)
            if record is not None:
                await self._notify(session_id, record)
                if record.get("state") == "recovery_failed":
                    return
                async with asyncio.timeout(120):
                    await self._restore(session_id, record)
                await self._notify(session_id, record)
        except Exception as exc:
            logger.exception("[TeamRecovery] admission failed session=%s", session_id)
            if record is not None:
                await asyncio.to_thread(failed, session_id, record["token"], exc)
                await self._notify(session_id, record)

    async def _notify(self, session_id: str, record: dict[str, Any]) -> None:
        if self._publish is not None:
            try:
                await self._publish(session_id, record)
            except Exception:
                logger.exception("[TeamRecovery] notification failed session=%s", session_id)

    async def _run(self) -> None:
        while True:
            try:
                await self.recover_once()
            except Exception:
                logger.exception("[TeamRecovery] scan failed")
            await asyncio.sleep(_SCAN_SECONDS)
