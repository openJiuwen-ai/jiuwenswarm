"""Durable user tasks over an injected Agent executor, independent of media/RPC."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

import portalocker

from .store import TaskStore
from .errors import QueueVersionConflict, TaskRevisionConflict

TERMINAL = {"completed", "failed", "cancelled"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskModification:
    revision: int
    instruction: str


class ExecutionUncertain(RuntimeError):
    """The transport lost the final execution fact; never automatically replay."""


class InteractionPending(RuntimeError):
    """The Agent ended this stream at an observed interaction, not a result."""


class TaskService:
    def __init__(self, store: TaskStore, executor, *, on_change=None, concurrency=2,
                 notification_timeout=2):
        self.store, self.executor, self.on_change = store, executor, on_change
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("Task concurrency must be a positive integer")
        self.concurrency = concurrency
        self.notification_timeout = notification_timeout
        self.workers = {}
        self.cancellations = {}
        self.notifications = {}
        self.pending_notifications = {}
        self.closed = False
        self.started = False
        self.lease = portalocker.Lock(str(store.path) + ".owner", timeout=0)

    def start(self):
        if self.closed:
            raise RuntimeError("Task service is closed")
        if self.started:
            return
        self.lease.acquire()
        try:
            with self.store.transaction() as db:
                for task in self.store.rows(db):
                    if task["status"] in {"running", "cancelling", "waiting_user"} or (
                        task["status"] == "queued" and task.get("resume_answer")
                    ):
                        task.update(
                            status="unknown",
                            error="Execution ownership lost; tools were not replayed",
                        )
                        if task.get("interaction"):
                            task["interaction"]["state"] = "expired"
                        for change in task["changes"]:
                            if change["state"] == "claimed":
                                change["state"] = "unknown"
                        task["sequence"] += 1
                        self.store.put(db, task)
        except BaseException:
            self.lease.release()
            raise
        self.started = True
        self.kick()

    def kick(self):
        if self.closed or not self.started:
            return
        with self.store.transaction() as db:
            tasks = self.store.rows(db)
        active = []
        for task in tasks:
            if task["status"] in {"running", "cancelling", "unknown", "waiting_user"} or task["id"] in self.workers:
                active.append(task)
        # Capacity covers executions owned by this service. Unknown executions
        # still constrain dependencies/resources; their activity is not inferred.
        for task in sorted(tasks, key=lambda t: t["position"]):
            if (
                sum(
                    t["status"] != "unknown"
                    and not (
                        t["status"] == "waiting_user" and t.get("execution_settled")
                    )
                    for t in active
                )
                >= self.concurrency
            ):
                break
            if task["status"] != "queued" or task["id"] in self.workers:
                continue
            if not self._ready(task, tasks, active):
                continue
            worker = asyncio.create_task(
                self._drain(task["id"]), name="managed-agent-task"
            )
            self.workers[task["id"]] = worker
            active.append(task)
            worker.add_done_callback(
                lambda done, key=task["id"]: self._worker_done(key, done)
            )

    @staticmethod
    def _ready(task, tasks, active):
        request = task["request"]
        by_id = {t["id"]: t for t in tasks}
        if any(
            by_id.get(key, {}).get("status") != "completed"
            for key in request.get("depends_on", [])
        ):
            return False

        def same_scope(t):
            return (t["owner"], t["session"]) == (task["owner"], task["session"])

        if not request.get("independent", False) and any(
            same_scope(t)
            and t["id"] != task["id"]
            and (
                t in active
                or (t["position"] < task["position"] and t["status"] not in TERMINAL)
            )
            for t in tasks
        ):
            return False
        # Missing declarations mean unknown resources, not a proven conflict.
        resources = set(request.get("resources", []))
        for other in active:
            held = set(other["request"].get("resources", []))
            if not resources or not held:
                continue
            if "*" in resources or "*" in held or resources & held:
                return False
        return True

    def _worker_done(self, scope, worker):
        self.workers.pop(scope, None)
        # Observe failures; the persisted attempt remains unknown rather than replayed.
        if not worker.cancelled() and worker.exception() is None:
            self.kick()

    @staticmethod
    def _new(owner, session, instruction, request, *, parent=None):
        task_id = uuid.uuid4().hex
        return dict(
            id=task_id,
            owner=owner,
            session=session,
            instruction=instruction,
            request=request,
            status="queued",
            revision=1,
            sequence=1,
            position=time.time_ns(),
            created_at=time.time(),
            core_session_id="managed-task-" + task_id,
            request_id="task-" + uuid.uuid4().hex,
            changes=[],
            progress=[],
            result=None,
            error="",
            parent_id=parent,
            successor_id=None,
            checkpoint_open=False,
            execution_settled=False,
            execution_cancelled=False,
            execution_bound=False,
            output_closed=False,
        )

    def submit(self, owner, session, command_id, instruction, request=None):
        self.start()
        # WebChannel permits an absent user; persist it as an exact empty scope.
        # None remains invalid because store reads use it to mean "all owners".
        if not isinstance(owner, str) or not session:
            raise ValueError("Task owner must be a string and conversation is required")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Task instruction is required")
        if len(instruction) > 16000:
            raise ValueError("Task instruction exceeds 16000 characters")
        request = dict(request or {})
        if not isinstance(request.get("independent", False), bool):
            raise ValueError("independent must be a boolean")
        for key in ("depends_on", "resources"):
            items = request.get(key, [])
            if (
                not isinstance(items, list)
                or len(items) > 32
                or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 256
                    for v in items
                )
            ):
                raise ValueError(f"Invalid {key}")
        if "resources" in request:
            request["resources"] = sorted(
                {v.strip().replace("\\", "/").casefold() for v in request["resources"]}
            )
        with self.store.transaction() as db:
            replay, fingerprint = self.store.replay(
                db,
                owner,
                session,
                command_id,
                ["submit", instruction, request],
            )
            if replay:
                return {
                    **self.store.get(db, replay["task_id"], owner, session),
                    "reused": True,
                }
            for dependency in request.get("depends_on", []):
                self.store.get(db, dependency, owner, session)
            task = self._new(owner, session, instruction.strip(), request)
            self.store.put(db, task)
            self.store.command(
                db,
                task,
                command_id,
                fingerprint,
                {"task_id": task["id"], "state": "accepted"},
            )
        self.kick()
        return task

    def get(self, owner, session, task_id):
        self.start()
        return self.store.read(task_id, owner, session)

    def list(self, owner, session, *, query="", status="", offset=0, limit=50):
        self.start()
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid pagination")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Invalid pagination")
        with self.store.transaction() as db:
            tasks = self.store.rows(db, owner, session)
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        tasks = [t for t in tasks if query.casefold() in t["instruction"].casefold()]
        end = offset + limit
        return tasks[offset:end], (end if len(tasks) > end else None)

    def modify(self, owner, session, task_id, command_id, change: TaskModification):
        revision, instruction = change.revision, change.instruction
        self.start()
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or len(instruction) > 4000
        ):
            raise ValueError("Modification must contain 1–4000 characters")
        with self.store.transaction() as db:
            task = self.store.get(db, task_id, owner, session)
            replay, fingerprint = self.store.replay(
                db,
                owner,
                session,
                command_id,
                ["modify", task_id, revision, instruction],
            )
            if replay:
                return replay
            if revision != task["revision"] or task["successor_id"]:
                raise TaskRevisionConflict("Task revision changed; query the current task")
            if task["status"] not in {"queued", "running", "completed"}:
                raise ValueError("This task cannot accept modifications")
            change = dict(
                id=command_id, instruction=instruction.strip(), state="pending"
            )
            if task["status"] == "queued":
                # Same transaction as the dispatch claim: no lost update at admission.
                if len(task["instruction"]) + len(instruction) > 15970:
                    raise ValueError(
                        "Accumulated task instruction exceeds 16000 characters"
                    )
                task["instruction"] += "\nUser modification: " + instruction.strip()
                change["state"] = "queued_input_updated"
            elif task["status"] == "completed":
                child = self._successor(db, task, instruction.strip())
                change.update(state="followup", successor_id=child["id"])
            task["changes"].append(change)
            task["revision"] += 1
            task["sequence"] += 1
            self.store.put(db, task)
            receipt = dict(
                task_id=task_id,
                operation_id=command_id,
                state=change["state"],
                revision=task["revision"],
                successor_id=change.get("successor_id"),
            )
            self.store.command(db, task, command_id, fingerprint, receipt)
        self.kick()
        return receipt

    def _successor(self, db, task, instruction):
        if task["successor_id"]:
            return self.store.get(db, task["successor_id"])
        adopted = "\n".join(
            c["instruction"]
            for c in task["changes"]
            if c["state"] in {"context_written", "model_input_observed"}
        )
        request = {
            **task["request"],
            "prior_result": task["result"],
            "depends_on": [task["id"]],
        }
        # A successor is a new execution, not a replay of the original call_id.
        for key in ("tool_call_id", "turn_id", "frame_data_url"):
            request.pop(key, None)
        child = self._new(
            task["owner"],
            task["session"],
            task["instruction"] + "\n" + adopted + "\nUser revision: " + instruction,
            request,
            parent=task["id"],
        )
        task["successor_id"] = child["id"]
        self.store.put(db, child)
        return child

    async def cancel(self, owner, session, task_id, command_id):
        self.start()
        with self.store.transaction() as db:
            task = self.store.get(db, task_id, owner, session)
            replay, fingerprint = self.store.replay(
                db, owner, session, command_id, ["cancel", task_id]
            )
            if replay:
                return replay
            if task["status"] == "queued" and not task.get("resume_answer"):
                task["status"] = "cancelled"
            elif task["status"] not in TERMINAL:
                task["status"] = "cancelling"
            task["revision"] += 1
            task["sequence"] += 1
            for change in task["changes"]:
                if change["state"] == "pending":
                    change["state"] = "rejected"
            if task.get("interaction"):
                task["interaction"]["state"] = "expired"
            self.store.put(db, task)
            receipt = dict(
                task_id=task_id,
                operation_id=command_id,
                state="accepted" if task["status"] == "cancelling" else task["status"],
                task_status=task["status"],
                stopped=task["status"] == "cancelled",
                message=("取消已受理，正在等待执行停止；尚未确认停止。"
                         if task["status"] == "cancelling" else
                         "任务已取消。" if task["status"] == "cancelled" else
                         "任务已经结束，未执行新的取消操作。"),
            )
            self.store.command(db, task, command_id, fingerprint, receipt)
        if task["status"] == "cancelling" and task_id not in self.cancellations:
            work = asyncio.create_task(self._cancel(task), name="managed-task-cancel")
            self.cancellations[task_id] = work
            work.add_done_callback(lambda _: self.cancellations.pop(task_id, None))
        await self._notify(task)
        return receipt

    async def _cancel(self, task):
        try:
            await self.executor.cancel(task)
            current = self.store.read(task["id"])
            if current.get("execution_cancelled") and current["execution_settled"]:
                observer = self.workers.get(task["id"])
                if observer and not observer.done():
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
            # Let the output observer publish any completion that won the race.
            while not self.store.read(task["id"])["output_closed"]:
                await asyncio.sleep(0.05)
            # The adapter must await the exact execution's local settlement.
            current = self.store.read(task["id"])
            if not current["execution_settled"]:
                raise RuntimeError(
                    "Stop accepted; execution settlement is not confirmed"
                )
            task = self.store.update(
                task["id"],
                lambda t: (
                    t.update(status="cancelled", error="")
                    if t["status"] == "cancelling"
                    else None
                ),
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            task = self.store.update(
                task["id"],
                lambda t: (
                    t.update(error=message) if t["status"] == "cancelling" else None
                ),
            )
        await self._notify(task)
        self.kick()

    def reorder(
        self, owner, session, task_id, revision, before_id=None, *, command_id=None
    ):
        self.start()
        with self.store.transaction() as db:
            task = self.store.get(db, task_id, owner, session)
            if command_id is not None:
                replay, fingerprint = self.store.replay(
                    db,
                    owner,
                    session,
                    command_id,
                    ["reorder", task_id, revision, before_id],
                )
                if replay:
                    return replay
            if task["status"] != "queued":
                raise ValueError("Task is no longer waiting; running tasks cannot be reordered")
            if self.queue_version(db, owner, session) != revision:
                raise QueueVersionConflict("Queue changed; query before reordering")
            tasks = sorted(
                (
                    t
                    for t in self.store.rows(db, owner, session)
                    if t["status"] == "queued"
                ),
                key=lambda t: t["position"],
            )
            if before_id and not any(t["id"] == before_id for t in tasks):
                raise ValueError("Queue target is no longer waiting")
            if before_id == task_id:
                tasks = []
            else:
                tasks = [t for t in tasks if t["id"] != task_id]
                index = next(
                    (i for i, t in enumerate(tasks) if t["id"] == before_id), 0
                )
                tasks.insert(index, task)
            for index, item in enumerate(tasks):
                item.update(
                    position=index,
                    revision=item["revision"] + 1,
                    sequence=item["sequence"] + 1,
                )
                self.store.put(db, item)
            receipt = dict(
                task_id=task_id,
                state="reordered",
                queue_version=self.queue_version(db, owner, session),
            )
            if command_id is not None:
                self.store.command(db, task, command_id, fingerprint, receipt)
        self.kick()
        return receipt

    @staticmethod
    def _queue_answer(task):
        # Retain the suspended execution identity until dispatch wins over cancel.
        task.update(status="queued", resume_answer=True)

    async def answer(
        self, owner, session, task_id, command_id, interaction_id, *, answers
    ):
        """Answer only a server-observed information question; never an approval."""
        from .interactions import validate_answers

        self.start()
        with self.store.transaction() as db:
            task = self.store.get(db, task_id, owner, session)
            replay, fingerprint = self.store.replay(
                db,
                owner,
                session,
                command_id,
                ["answer", task_id, interaction_id, answers],
            )
            if replay:
                return replay
            interaction = task.get("interaction") or {}
            if (
                task["status"] != "waiting_user"
                or interaction.get("state") != "pending"
                or interaction.get("id") != interaction_id
            ):
                raise ValueError("Question is no longer pending for this task")
            answers = validate_answers(interaction, answers)
            interaction.update(
                state="accepted", answers=answers, operation_id=command_id
            )
            suspended = interaction["source"] == "ask_user_interrupt"
            if suspended and task["output_closed"] and task["execution_settled"]:
                self._queue_answer(task)
            elif not suspended:
                if task["output_closed"]:
                    raise ValueError("The original answer channel has closed")
                task["status"] = "running"
            task["sequence"] += 1
            self.store.put(db, task)
            receipt = dict(task_id=task_id, operation_id=command_id, state="accepted")
            self.store.command(db, task, command_id, fingerprint, receipt)
        if not suspended:
            # This is control input to the still-owned output stream, not a new task.
            try:
                await self.executor.answer(task)
            except Exception as exc:
                error = str(exc)
                self.store.update(
                    task_id,
                    lambda t: (
                        t.update(status="unknown", error=error)
                        if t["status"] == "running"
                        and t["request_id"] == task["request_id"]
                        else None
                    ),
                )
                raise
        await self._notify(self.store.read(task_id))
        self.kick()
        return receipt

    def queue_version(self, db, owner, session):
        # An opaque, JS-safe token of scheduling facts, not streamed progress.
        facts = [
            [t["id"], t["status"], t["position"], t["request"].get("independent", False),
             t["request"].get("depends_on", []), t["request"].get("resources", [])]
            for t in self.store.rows(db, owner, session)
        ]
        return int(hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:12], 16)

    def snapshot(self, owner, session):
        self.start()
        with self.store.transaction() as db:
            tasks = self.store.rows(db, owner, session)
            all_tasks = self.store.rows(db)
            by_id = {t["id"]: t for t in all_tasks}
            for task in tasks:
                if task["status"] != "queued":
                    continue
                dependencies = [
                    by_id.get(key, {}) for key in task["request"].get("depends_on", [])
                ]
                task["wait_reason"] = (
                    "dependency_failed_or_cancelled"
                    if any(
                        t.get("status") in {"failed", "cancelled"} for t in dependencies
                    )
                    else "dependency_pending"
                    if any(t.get("status") != "completed" for t in dependencies)
                    else "capacity_order_or_resource"
                )
            return tasks, self.queue_version(db, owner, session)

    async def preempt(self, owner, session, task_id, revision, command_id):
        # Admission/reorder has no await: a stale request cannot stop another task.
        tasks, _ = self.snapshot(owner, session)
        if sum(t["status"] == "running" for t in tasks) > 1:
            raise ValueError("Multiple tasks are running; cancel an exact task instead")
        self.reorder(owner, session, task_id, revision)
        for active in tasks:
            if active["status"] == "running":
                await self.cancel(owner, session, active["id"], command_id)
                break
        # Dispatch stays blocked until the exact old execution settles.

    async def _notify(self, task):
        if not self.on_change or self.closed:
            return
        # One sender and one latest snapshot per task. Slow clients must not
        # backpressure Agent output or accumulate an unbounded notification queue.
        key = task["id"]
        self.pending_notifications[key] = task
        if key not in self.notifications:
            self.notifications[key] = asyncio.create_task(self._send_notifications(key))

    async def _send_notifications(self, key):
        failures = 0
        try:
            while key in self.pending_notifications:
                task = self.pending_notifications.pop(key)
                try:
                    await asyncio.wait_for(self.on_change(task), timeout=self.notification_timeout)
                except Exception:
                    # Prefer a newer snapshot if one arrived during delivery.
                    if key in self.pending_notifications:
                        failures = 0
                    else:
                        failures += 1
                        if failures < 3:
                            self.pending_notifications[key] = task
                        else:
                            logger.warning("Task notification retries exhausted for %s", key)
                else:
                    failures = 0
                await asyncio.sleep(0.1)
        finally:
            self.notifications.pop(key, None)

    async def _drain(self, task_id):
        # Recheck after admission: queued cancel/modify/reorder can win before this runs.
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            tasks = self.store.rows(db)
            active = []
            for other in tasks:
                if other["id"] == task_id:
                    continue
                if other["status"] in {"running", "cancelling", "unknown", "waiting_user"}:
                    active.append(other)
            if task["status"] != "queued" or not self._ready(task, tasks, active):
                return
            if any(
                t["status"] == "queued"
                and t["position"] < task["position"]
                and (t["owner"], t["session"]) == (task["owner"], task["session"])
                and self._ready(t, tasks, active)
                for t in tasks
            ):
                return
            if task.get("resume_answer"):
                # One business task may produce files in several interrupted rounds.
                # Retain each settled request before assigning the next execution ID.
                task.setdefault("prior_request_ids", []).append(task["request_id"])
                task.update(
                    request_id="task-" + uuid.uuid4().hex,
                    execution_bound=False,
                    execution_settled=False,
                    execution_cancelled=False,
                    output_closed=False,
                )
                task["interaction"]["state"] = "submitted"
            task.update(
                status="running", checkpoint_open=True, sequence=task["sequence"] + 1
            )
            task["progress"].append(
                dict(
                    stage="started",
                    title="Agent execution started",
                    status="running",
                    sequence=len(task["progress"]) + 1,
                    timestamp=time.time(),
                )
            )
            self.store.put(db, task)
        await self._notify(task)

        async def persist_progress(
            entry, task_id=task["id"], request_id=task["request_id"]
        ):
            def append(current):
                if current["request_id"] != request_id or current["status"] not in {
                    "running",
                    "waiting_user",
                    "cancelling",
                }:
                    return
                if entry.get("interaction") and current["status"] != "cancelling":
                    interaction = entry["interaction"]
                    old = current.get("interaction") or {}
                    if (
                        old.get("request_id") != interaction["request_id"]
                        or old.get("execution_request_id") != request_id
                    ):
                        interaction = {
                            **interaction,
                            "id": uuid.uuid4().hex,
                            "execution_request_id": request_id,
                        }
                        current.update(interaction=interaction, status="waiting_user")
                current["progress"].append(
                    {
                        **entry,
                        "sequence": len(current["progress"]) + 1,
                        "timestamp": time.time(),
                    }
                )

            await self._notify(self.store.update(task_id, append))

        reasoning = None
        last_flush = time.monotonic()

        async def flush_progress():
            nonlocal reasoning, last_flush
            if reasoning is not None:
                entry, reasoning = reasoning, None
                await persist_progress(entry)
            last_flush = time.monotonic()

        async def progress(entry):
            nonlocal reasoning
            if entry.get("stage") == "reasoning":
                if reasoning is None:
                    reasoning = dict(entry)
                else:
                    reasoning["content"] = reasoning.get("content", "") + entry.get(
                        "content", ""
                    )
                if (
                    time.monotonic() - last_flush >= 0.25
                    or len(reasoning.get("content", "")) >= 4096
                ):
                    await flush_progress()
            else:
                await flush_progress()
                await persist_progress(entry)

        outcome, result, error = "completed", None, ""
        try:
            result = await self.executor.run(task, progress)
        except InteractionPending:
            outcome = "waiting_user"
        except asyncio.CancelledError:
            outcome, error = "unknown", "Execution detached; result is unknown"
            raise
        except ExecutionUncertain as exc:
            outcome, error = "unknown", str(exc)
        except Exception as exc:
            outcome, error = "failed", str(exc)
        finally:
            await flush_progress()
            with self.store.transaction() as db:
                current = self.store.get(db, task["id"])
                current["checkpoint_open"] = False
                current["output_closed"] = True
                if current["execution_cancelled"] and current["execution_settled"]:
                    # A cancellation flush can contain chat.final/partial text.
                    # The exact execution's stop fact takes precedence over that text.
                    current.update(status="cancelled", result=None, partial_result=result, error="")
                elif current["status"] in {"running", "waiting_user"}:
                    current.update(status=outcome, result=result, error=error)
                elif current["status"] == "cancelling" and result is not None:
                    # Completion won the race; do not discard the actual result.
                    current.update(status="completed", result=result, error="")
                if current["status"] == "waiting_user":
                    interaction = current.get("interaction") or {}
                    if (
                        interaction.get("state") == "accepted"
                        and current["execution_settled"]
                    ):
                        self._queue_answer(current)
                elif current.get("interaction") and current["status"] in TERMINAL | {
                    "unknown"
                }:
                    current["interaction"]["state"] = "expired"
                pending = [c for c in current["changes"] if c["state"] == "pending"]
                if pending and current["status"] == "completed":
                    child = self._successor(
                        db,
                        current,
                        "\n".join(c["instruction"] for c in pending),
                    )
                    for change in pending:
                        change.update(state="followup", successor_id=child["id"])
                for change in current["changes"]:
                    if change["state"] == "claimed":
                        change["state"] = "unknown"
                    elif change["state"] == "pending" and current["status"] not in {
                        "waiting_user",
                        "queued",
                    }:
                        change["state"] = "rejected"
                current["sequence"] += 1
                self.store.put(db, current)
            await self._notify(current)

    async def close(self):
        self.closed = True
        pending = [
            *self.workers.values(),
            *self.cancellations.values(),
            *self.notifications.values(),
        ]
        for worker in pending:
            worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        close_executor = getattr(self.executor, "close", None)
        if close_executor is not None:
            await close_executor()
        self.pending_notifications.clear()
        if self.started:
            self.lease.release()
