"""Generic core lifecycle hooks adapted to desktop behavior reporting."""
import asyncio
import uuid

from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.interrupt import InterruptRequest
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail

from jiuwenswarm.common.behavior_security import ACTIVE_BEHAVIOR, _event_id, raw_event


class BehaviorSecurityRail(DeepAgentRail):
    priority = 100

    def __init__(self, bridge, skill_reporter=None):
        super().__init__()
        self.bridge = bridge
        self.skill_reporter = skill_reporter
        self.skill_observer = skill_reporter.new_observer() if skill_reporter else None

    async def before_invoke(self, ctx):
        if ctx.session is None:
            return
        key = "behavior_security.turn_id"
        turn = ctx.session.get_state(key)
        if not isinstance(getattr(ctx.inputs, "query", None), InteractiveInput) or not turn:
            turn = uuid.uuid4().hex
            ctx.session.update_state({key: turn})
        ctx.extra["behavior.turn_id"] = turn

    async def before_model_call(self, ctx):
        pairs = []
        for message in getattr(ctx.inputs, "messages", None) or []:
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if not isinstance(content, str):
                continue
            if role == "user":
                pairs.append({"text": content, "response": ""})
            elif role == "assistant" and pairs:
                pairs[-1]["response"] += content
        ctx.extra["behavior.dialogue_context"] = pairs

    async def before_tool_call(self, ctx):
        # Establish this tool's own baseline; an unrelated manual installation
        # must not acquire a fabricated tool correlation on the next user turn.
        if self.skill_observer is not None:
            await self.skill_observer.scan(self.skill_reporter.roots)
        ctx.extra["behavior.finished"] = False
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(lambda _: ctx.extra.__setitem__("behavior.finished", True))
        local = self.bridge.local_decisions.get(_event_id(ctx, "tool.before"))
        if local is not None:
            ctx.extra["behavior.local_permission"] = local
        ctx.extra["behavior.context_token"] = ACTIVE_BEHAVIOR.set((self.bridge, ctx, asyncio.get_running_loop()))
        # Desktop full-access mode still uses PermissionInterruptRail. Its
        # evaluated result drives report/authorize; never synthesize an ALLOW.

    async def after_tool_call(self, ctx):
        ctx.extra["behavior.finished"] = True
        token = ctx.extra.pop("behavior.context_token", None)
        if token is not None:
            ACTIVE_BEHAVIOR.reset(token)
        confirmation = ctx.extra.pop("behavior.install_confirmation", None)
        if confirmation:
            BaseInterruptRail()._raise_interrupt(ctx.inputs.tool_name, ctx.inputs.tool_call,
                InterruptRequest(message=confirmation, payload_schema=ConfirmPayload.to_schema(),
                                 metadata={"source": "permission_interrupt"}))
        # No output exists for a denied or interrupted tool.
        decision = ctx.extra.get("_interrupt_decision")
        if decision is not None and type(decision).__name__ != "ApproveResult":
            return
        if getattr(ctx.inputs, "execution_started", False):
            self.bridge.report(raw_event(ctx, "tool.after"))
            if self.skill_observer is not None:
                await self.skill_observer.scan(self.skill_reporter.roots, context=ctx)

    def uninit(self, agent):
        for task in self.bridge.tasks:
            task.cancel()
