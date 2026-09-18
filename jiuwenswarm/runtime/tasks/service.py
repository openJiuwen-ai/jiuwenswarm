"""Durable user tasks over an injected Agent executor, independent of media/RPC."""

import asyncio
import time
import uuid

import portalocker

from .store import TaskStore

TERMINAL = {"completed", "failed", "cancelled"}


class ExecutionUncertain(RuntimeError):
    """The transport lost the final execution fact; never automatically replay."""


class TaskService:
    def __init__(self, store: TaskStore, executor, *, on_change=None, concurrency=2):
        self.store, self.executor, self.on_change = store, executor, on_change
        self.capacity = asyncio.Semaphore(concurrency)
        self.workers = {}
        self.cancellations = {}
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
                    if task["status"] in {"running", "cancelling"}:
                        task.update(
                            status="unknown",
                            error="Execution ownership lost; tools were not replayed",
                        )
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
        for task in tasks:
            scope = (task["owner"], task["session"])
            if (
                task["status"] == "queued"
                and scope not in self.workers
                and not any(
                    t["status"] in {"running", "cancelling", "unknown"}
                    and (t["owner"], t["session"]) == scope
                    for t in tasks
                )
            ):
                worker = asyncio.create_task(
                    self._drain(scope), name="managed-agent-task"
                )
                self.workers[scope] = worker
                worker.add_done_callback(
                    lambda done, key=scope: self._worker_done(key, done)
                )

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
            execution_bound=False,
            output_closed=False,
        )

    def submit(self, owner, session, command_id, instruction, request=None):
        self.start()
        if (
            not owner
            or not session
            or not isinstance(instruction, str)
            or not instruction.strip()
        ):
            raise ValueError("Task owner, conversation and instruction are required")
        if len(instruction) > 16000:
            raise ValueError("Task instruction exceeds 16000 characters")
        request = request or {}
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
        if (
            not isinstance(offset, int)
            or offset < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid pagination")
        with self.store.transaction() as db:
            tasks = self.store.rows(db, owner, session)
        tasks = [
            t
            for t in tasks
            if (not status or t["status"] == status)
            and query.casefold() in t["instruction"].casefold()
        ]
        return tasks[offset : offset + limit], (
            offset + limit if len(tasks) > offset + limit else None
        )

    def modify(self, owner, session, task_id, command_id, revision, instruction):
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
                raise ValueError("Task revision changed; query the current task")
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
        request = {**task["request"], "prior_result": task["result"]}
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
            if task["status"] == "queued":
                task["status"] = "cancelled"
            elif task["status"] not in TERMINAL:
                task["status"] = "cancelling"
            task["revision"] += 1
            task["sequence"] += 1
            for change in task["changes"]:
                if change["state"] == "pending":
                    change["state"] = "rejected"
            self.store.put(db, task)
            receipt = dict(
                task_id=task_id,
                operation_id=command_id,
                state="accepted" if task["status"] == "cancelling" else task["status"],
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

    def reorder(self, owner, session, task_id, revision, before_id=None):
        self.start()
        with self.store.transaction() as db:
            task = self.store.get(db, task_id, owner, session)
            if (
                task["status"] != "queued"
                or self.queue_version(db, owner, session) != revision
            ):
                raise ValueError("Queue changed; query before reordering")
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
                return
            tasks = [t for t in tasks if t["id"] != task_id]
            index = next((i for i, t in enumerate(tasks) if t["id"] == before_id), 0)
            tasks.insert(index, task)
            for index, item in enumerate(tasks):
                item.update(
                    position=index,
                    revision=item["revision"] + 1,
                    sequence=item["sequence"] + 1,
                )
                self.store.put(db, item)

    def queue_version(self, db, owner, session):
        # Records are never deleted; all observable mutations advance sequence.
        return sum(t["sequence"] for t in self.store.rows(db, owner, session))

    def snapshot(self, owner, session):
        self.start()
        with self.store.transaction() as db:
            tasks = self.store.rows(db, owner, session)
            return tasks, self.queue_version(db, owner, session)

    async def preempt(self, owner, session, task_id, revision, command_id):
        # Admission/reorder has no await: a stale request cannot stop another task.
        self.reorder(owner, session, task_id, revision)
        tasks, _ = self.snapshot(owner, session)
        for active in tasks:
            if active["status"] == "running":
                await self.cancel(owner, session, active["id"], command_id)
                break
        # Dispatch stays blocked until the exact old execution settles.

    async def _notify(self, task):
        if self.on_change:
            try:
                await self.on_change(task)
            except Exception:
                # Query/reconnect reads the same saved result; transport never owns execution.
                pass

    async def _drain(self, scope):
        while not self.closed:
            async with self.capacity:
                with self.store.transaction() as db:
                    tasks = self.store.rows(db, *scope)
                    if any(
                        t["status"] in {"running", "cancelling", "unknown"}
                        for t in tasks
                    ):
                        return
                    queued = sorted(
                        (t for t in tasks if t["status"] == "queued"),
                        key=lambda t: t["position"],
                    )
                    if not queued:
                        return
                    task = queued[0]
                    task.update(
                        status="running",
                        checkpoint_open=True,
                        sequence=task["sequence"] + 1,
                    )
                    task["progress"].append(
                        dict(
                            stage="started",
                            title="Agent execution started",
                            status="running",
                            sequence=1,
                            timestamp=time.time(),
                        )
                    )
                    self.store.put(db, task)
                await self._notify(task)

                async def progress(
                    entry, task_id=task["id"], request_id=task["request_id"]
                ):
                    def append(current):
                        if current["request_id"] != request_id or current[
                            "status"
                        ] not in {"running", "cancelling"}:
                            return
                        current["progress"].append(
                            {
                                **entry,
                                "sequence": len(current["progress"]) + 1,
                                "timestamp": time.time(),
                            }
                        )

                    await self._notify(self.store.update(task_id, append))

                outcome, result, error = "completed", None, ""
                try:
                    result = await self.executor.run(task, progress)
                except asyncio.CancelledError:
                    outcome, error = "unknown", "Execution detached; result is unknown"
                    raise
                except ExecutionUncertain as exc:
                    outcome, error = "unknown", str(exc)
                except Exception as exc:
                    outcome, error = "failed", str(exc)
                finally:
                    with self.store.transaction() as db:
                        current = self.store.get(db, task["id"])
                        current["checkpoint_open"] = False
                        current["output_closed"] = True
                        if current["status"] == "running":
                            current.update(status=outcome, result=result, error=error)
                        elif current["status"] == "cancelling" and result is not None:
                            # Completion won the race; do not discard the actual result.
                            current.update(status="completed", result=result, error="")
                        pending = [
                            c for c in current["changes"] if c["state"] == "pending"
                        ]
                        if pending and current["status"] == "completed":
                            child = self._successor(
                                db,
                                current,
                                "\n".join(c["instruction"] for c in pending),
                            )
                            for change in pending:
                                change.update(
                                    state="followup", successor_id=child["id"]
                                )
                        for change in current["changes"]:
                            if change["state"] == "claimed":
                                change["state"] = "unknown"
                            elif change["state"] == "pending":
                                change["state"] = "rejected"
                        current["sequence"] += 1
                        self.store.put(db, current)
                    await self._notify(current)

    async def close(self):
        self.closed = True
        pending = [*self.workers.values(), *self.cancellations.values()]
        for worker in pending:
            worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.started:
            self.lease.release()
