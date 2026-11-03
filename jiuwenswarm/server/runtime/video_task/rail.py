"""Root Agent callbacks for voice-delegated task requirements and identity."""

from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentRail


class VoiceAgentTaskRail(AgentRail):
    # Preserve the checkpoint's position before StreamEventRail (priority 80).
    priority = 81

    def __init__(self, owner=None):
        super().__init__()
        self.owner = owner
        self.managed_tasks = {}

    async def before_model_call(self, ctx):
        await task_checkpoint(self, ctx, "before_model")

    async def after_model_call(self, ctx):
        await task_checkpoint(self, ctx, "after_model")

    async def before_tool_call(self, ctx):
        await task_checkpoint(self, ctx, "before_tool")


async def install_task_rail(owner, rail=None, *, reload=False):
    """Register the task rail; the Host owns its Agent and retained rail."""
    if rail is None or rail.owner is not owner:
        rail = VoiceAgentTaskRail(owner)
    elif not reload:
        return rail
    else:
        await owner.unregister_rail(rail)
    await owner.ensure_initialized()
    await owner.register_rail(rail)
    return rail


async def task_checkpoint(rail, ctx, stage):
    run = (getattr(ctx, "extra", None) or {}).get("run_context")
    extra = run.get("extra", {}) if isinstance(run, dict) else getattr(run, "extra", {})
    request_id = extra.get("managed_task_request")
    checkpoint = None
    for binding in rail.managed_tasks.values():
        if binding.request_id == request_id or (not request_id and binding.root is ctx.agent):
            checkpoint = binding
            break
    if checkpoint is None:
        if not request_id:
            return  # Ordinary requests do not use task storage or task checks.
        raise AbortError("TASK_EXECUTION_BINDING_CLOSED")
    if ctx.agent is not checkpoint.root:
        return
    try:
        if stage == "before_model":
            # Retain native work before the remote check can fail or be cancelled.
            checkpoint.capture_execution()
            await checkpoint.check(ctx)
            await checkpoint.before_model(ctx)
        elif stage == "after_model":
            await checkpoint.after_model(ctx)
        else:
            await checkpoint.check(ctx)
    except Exception as exc:
        raise AbortError("TASK_CHECKPOINT_REJECTED", cause=exc) from exc
