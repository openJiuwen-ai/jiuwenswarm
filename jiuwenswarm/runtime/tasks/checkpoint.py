"""Exact Host execution binding for existing Agent model/tool callbacks."""

from contextlib import contextmanager
import json

from .store import TaskStore, task_database


class TaskCheckpoint:
    def __init__(self, store, task, root, harness=None):
        self.store, self.task_id, self.root = store, task["id"], root
        self.request_id = task["request_id"]
        self.closed = False
        self.harness = harness
        self.native_executions = set()
        self.settlement_unobserved = False

    def check(self, ctx):
        run = ctx.extra.get("run_context")
        extra = (
            run.get("extra", {}) if isinstance(run, dict) else getattr(run, "extra", {})
        )
        if self.closed or extra.get("managed_task_request") != self.request_id:
            raise RuntimeError("TASK_EXECUTION_BINDING_STALE")
        task = self.store.read(self.task_id)
        if task["request_id"] != self.request_id or task["status"] != "running":
            raise RuntimeError("TASK_EXECUTION_NOT_RUNNING")

    async def before_model(self, ctx):
        self.check(ctx)
        # A Core output stream can close before its cancelled scheduler task drains.
        # Capture that exact handle while the round still owns it; never infer
        # settlement from a terminal status or disappearance from the scheduler.
        active = getattr(self.harness, "active_round", None)
        controller = getattr(self.harness, "loop_controller", None)
        scheduler = getattr(controller, "task_scheduler", None)
        entry = getattr(scheduler, "_running_tasks", {}).get(
            getattr(active, "task_id", None)
        )
        if entry and entry[1] is not None:
            self.native_executions.add(entry[1])
        elif active is not None:
            self.settlement_unobserved = True
        from openjiuwen.core.foundation.llm import UserMessage

        with self.store.transaction() as db:
            task = self.store.get(db, self.task_id)
            if task["request_id"] != self.request_id or task["status"] != "running":
                raise RuntimeError("TASK_EXECUTION_NOT_RUNNING")
            pending = [c for c in task["changes"] if c["state"] == "pending"]
            for change in pending:
                change["state"] = "claimed"
            self.store.put(db, task)
        for change in pending:
            # SQLite and model context are not one transaction: a lost ACK stays unknown.
            await ctx.context.add_messages(
                UserMessage(
                    content=json.dumps(
                        {
                            "managed_task_change": change["id"],
                            "instruction": change["instruction"],
                            "meaning": "User changes this task's requirements; retain other constraints and permissions.",
                        },
                        ensure_ascii=False,
                    )
                )
            )

            def acknowledge(task, change_id=change["id"]):
                for item in task["changes"]:
                    if item["id"] == change_id and item["state"] == "claimed":
                        item["state"] = "context_written"

            self.store.update(self.task_id, acknowledge)
        self.check(ctx)

    def after_model(self, ctx):
        # This observes the final request, not semantic compliance with the change.
        messages = getattr(ctx.inputs, "messages", []) or []
        observed = set()
        for message in messages:
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            if not isinstance(content, str):
                continue
            try:
                value = json.loads(content)
                if isinstance(value, dict):
                    observed.add(value.get("managed_task_change"))
            except (ValueError, TypeError):
                continue
        if observed:

            def mark(task):
                for change in task["changes"]:
                    if (
                        change["state"] == "context_written"
                        and change["id"] in observed
                    ):
                        change["state"] = "model_input_observed"

            self.store.update(self.task_id, mark)


@contextmanager
def bind_task_execution(request, adapter, inputs):
    path = task_database()
    if (
        request.channel_id != "video_tool"
        or not path.is_file()
        or not getattr(adapter, "_is_session_scoped_adapter", False)
    ):
        yield
        return
    store = TaskStore(path)
    with store.transaction() as db:
        candidates = [
            t
            for t in store.rows(db)
            if t["core_session_id"] == request.session_id
            and t["request_id"] == request.request_id
        ]
    if not candidates:
        yield
        return
    root = getattr(getattr(adapter, "_instance", None), "_react_agent", None)
    if root is None or not getattr(adapter, "_is_session_scoped_adapter", False):
        raise RuntimeError("Managed tasks require the session-owned Agent")
    rail = adapter._stream_event_rail
    bindings = getattr(rail, "_managed_tasks", None)
    if bindings is None:
        rail._managed_tasks = bindings = {}
    if request.session_id in bindings:
        raise RuntimeError("Task execution already bound")
    with store.transaction() as db:
        task = store.get(db, candidates[0]["id"])
        if task["status"] != "running" or task.get("execution_bound"):
            raise RuntimeError("Task execution is not available for a new invocation")
        task["execution_bound"] = True
        task["sequence"] += 1
        store.put(db, task)
    checkpoint = TaskCheckpoint(store, candidates[0], root, adapter._instance)
    bindings[request.session_id] = checkpoint
    previous = inputs.get("run")
    run = dict(previous or {})
    context = dict(run.get("context") or {})
    context["extra"] = {
        **context.get("extra", {}),
        "managed_task_request": request.request_id,
    }
    inputs["run"] = {**run, "context": context}
    try:
        yield
    finally:
        checkpoint.closed = True
        bindings.pop(request.session_id, None)
        if previous is None:
            inputs.pop("run", None)
        else:
            inputs["run"] = previous
        store.update(checkpoint.task_id, lambda t: t.update(checkpoint_open=False))

        def settled(_done=None):
            if not checkpoint.settlement_unobserved and all(
                task.done() for task in checkpoint.native_executions
            ):
                store.update(
                    checkpoint.task_id, lambda t: t.update(execution_settled=True)
                )

        for native_task in checkpoint.native_executions:
            if not native_task.done():
                native_task.add_done_callback(settled)
        settled()


async def task_checkpoint(rail, ctx, stage):
    checkpoint = getattr(rail, "_managed_tasks", {}).get(
        rail._resolve_sid(ctx, ctx.session)
    )
    from openjiuwen.core.runner.callback.errors import AbortError

    if checkpoint is None:
        run = ctx.extra.get("run_context")
        extra = (
            run.get("extra", {}) if isinstance(run, dict) else getattr(run, "extra", {})
        )
        if extra.get("managed_task_request"):
            raise AbortError("TASK_EXECUTION_BINDING_CLOSED")
        return
    if ctx.agent is not checkpoint.root:
        return
    try:
        if stage == "before_model":
            await checkpoint.before_model(ctx)
        elif stage == "after_model":
            checkpoint.after_model(ctx)
        else:
            checkpoint.check(ctx)
    except Exception as exc:
        raise AbortError("TASK_CHECKPOINT_REJECTED", cause=exc) from exc
