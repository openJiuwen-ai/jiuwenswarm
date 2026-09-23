"""Apply requirement changes to the exact root Agent model context."""

import json


class TaskCheckpoint:
    def __init__(self, endpoint, task, root):
        self.endpoint, self.task_id, self.root = endpoint, task["id"], root
        self.request_id = task["request_id"]
        self.closed = False

    async def check(self, ctx):
        run = ctx.extra.get("run_context")
        extra = (
            run.get("extra", {}) if isinstance(run, dict) else getattr(run, "extra", {})
        )
        if self.closed or extra.get("managed_task_request") != self.request_id:
            raise RuntimeError("TASK_EXECUTION_BINDING_STALE")
        task = await self.endpoint.call("status")
        if task["status"] not in {"running", "waiting_user"}:
            raise RuntimeError("TASK_EXECUTION_NOT_RUNNING")

    async def before_model(self, ctx):
        await self.check(ctx)
        from openjiuwen.core.foundation.llm import UserMessage

        receipt = await self.endpoint.call("claim")
        pending = receipt.get("changes", [])
        for change in pending:
            # SQLite and model context are not one transaction: a lost ACK stays unknown.
            await ctx.context.add_messages(
                UserMessage(
                    content=json.dumps(
                        {
                            "managed_task_change": change["id"],
                            "instruction": change["instruction"],
                            "meaning": (
                                "User changes this task's requirements; "
                                "retain other constraints and permissions."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
            )

            await self.endpoint.call("context_written", change_ids=[change["id"]])
        await self.check(ctx)

    async def after_model(self, ctx):
        await self.check(ctx)
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
            await self.endpoint.call("observed", change_ids=list(observed))
