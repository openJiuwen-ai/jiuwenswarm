"""Bind the existing structured ask-user rail to a public-group question owner."""

import json
import logging

from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime.group_questions import (
    GroupQuestions, ORGANIZATION_RESUME_KEY, register_question_target,
)


_RESUME_RAIL_KEY = "swarm_group_resume_rail"
logger = logging.getLogger(__name__)


class _GroupResumeBindingRail(AgentRail):
    """Bind the actual inner invocation before interrupt tools copy its context."""

    def __init__(self, owner):
        self.owner = owner

    async def before_invoke(self, ctx):
        self.owner.restore_organization_turn(ctx)
        from jiuwenswarm.agents.swarm.group_turn import TURN_KEY

        turn = ctx.extra.get(TURN_KEY)
        if turn is not None and turn.route == "unknown":
            ctx.request_force_finish({"output": turn.error, "result_type": "error"})


class GroupAskUserRail(StructuredAskUserRail):
    def __init__(self, context, params):
        super().__init__(language=getattr(context, "language", None) or "cn")
        self.context = context
        self.group_id = params.get("group_id") or context.session_id
        self.member_name = params.get("member_name") or context.member_name
        self.execution_id = params.get("execution_id")
        self.target = None

    def init(self, agent):
        super().init(agent)
        self.target = register_question_target(
            self.group_id, self.context.session_id, self.context.member_name, agent,
        )

    async def before_invoke(self, ctx):
        from openjiuwen.harness.deep_agent import DeepAgent

        self.target = register_question_target(
            self.group_id, self.context.session_id, self.context.member_name, ctx.agent,
        )
        # NativeHarness sends work through an already-running scheduler. Its
        # consumer does not inherit this task's ContextVars. Register on the
        # actual inner agent, but keep the question target on the outer host.
        if isinstance(ctx.agent, DeepAgent) and ctx.agent.react_agent is not None:
            inner = ctx.agent.react_agent
            if _RESUME_RAIL_KEY not in ctx.extra:
                binding = _GroupResumeBindingRail(self)
                ctx.extra[_RESUME_RAIL_KEY] = (inner, binding)
                try:
                    await inner.register_rail(binding)
                except Exception as exc:
                    from openjiuwen.core.runner.callback import AbortError

                    raise AbortError("Cannot install group clarification binding", cause=exc) from exc
        self.restore_organization_turn(ctx)

    async def after_invoke(self, ctx):
        mounted = getattr(ctx, "extra", {}).pop(_RESUME_RAIL_KEY, None)
        if mounted is not None:
            inner, binding = mounted
            await inner.unregister_rail(binding)

    def restore_organization_turn(self, ctx):
        """Rebind a fresh invoke from its exact persisted ask-user call, never the latest plan."""
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
        from jiuwenswarm.agents.swarm.group_turn import GroupTurn, TURN_KEY
        from jiuwenswarm.agents.swarm.group_route import candidate

        query = getattr(getattr(ctx, "inputs", None), "query", None)
        if not isinstance(query, InteractiveInput) or self.context.session_id != self.group_id:
            return
        expected = {}
        for native_id, payload in query.user_inputs.items():
            if not isinstance(payload, dict) or ORGANIZATION_RESUME_KEY not in payload:
                continue
            expected[native_id] = payload[ORGANIZATION_RESUME_KEY]
        identities = {value for value in expected.values() if isinstance(value, str) and value}
        failed_message_id = next(iter(identities)) if len(identities) == 1 else None
        # Retain the server's intended message only for failure reporting. It
        # never grants organization execution without the checks below.
        unknown = GroupTurn(self.group_id, self.member_name, failed_message_id, "unknown",
                            "The clarification has no unambiguous organization owner.")
        try:
            records = []
            for record in GroupQuestions(self.group_id).read().values():
                if record.get("native_request_id") not in query.user_inputs:
                    continue
                if record.get("target") != self.target or record.get("member_name") != self.member_name:
                    continue
                if record.get("execution_id") is not None:
                    continue
                client_message_id = record.get("client_message_id")
                if not isinstance(client_message_id, str) or not client_message_id:
                    continue
                records.append(record)
        except (ValueError, OSError, AttributeError, TypeError):
            ctx.extra[TURN_KEY] = unknown
            logger.exception("Cannot read group clarification ownership group=%s", self.group_id)
            return
        if expected and any(not any(record["native_request_id"] == native_id
                                    and record["client_message_id"] == message_id for record in records)
                            for native_id, message_id in expected.items()):
            ctx.extra[TURN_KEY] = unknown
            return
        if not records:
            return
        message_ids = {record["client_message_id"] for record in records}
        message_id = next(iter(message_ids)) if len(message_ids) == 1 else None
        # Keep a denied binding if candidate loading fails inside a callback;
        # the callback framework may otherwise log an exception and continue.
        ctx.extra[TURN_KEY] = unknown
        try:
            item = candidate(self.group_id, message_id) if message_id else None
        except (ValueError, OSError, AttributeError, TypeError):
            logger.exception("Cannot read group organization ownership group=%s", self.group_id)
            return
        if (item is None or item.get("owner_member") != self.member_name
                or any(record.get("status") != "answered" for record in records)):
            return
        ctx.extra[TURN_KEY] = GroupTurn(self.group_id, self.member_name, message_id, "organization")

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
        from jiuwenswarm.agents.swarm.group_turn import TURN_KEY
        from jiuwenswarm.agents.swarm.group_route import admission_block_reason

        turn = getattr(ctx, "extra", {}).get(TURN_KEY)
        if turn is not None and turn.route == "organization":
            blocked = admission_block_reason(self.group_id, turn.client_message_id, self.member_name, "ask_user")
            if blocked:
                return self.reject(tool_result=blocked)
        decision = await super().resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)
        if not isinstance(decision, InterruptResult) or tool_call is None:
            return decision
        arguments = tool_call.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return self.reject(tool_result="Call ask_user with valid JSON containing query or questions.")
        if not isinstance(arguments, dict):
            return self.reject(tool_result="Call ask_user with an object containing query or questions.")
        payload = convert_interactions_to_ask_user_question([{
            "id": tool_call.id,
            "value": {"tool_name": "ask_user", "tool_args": arguments,
                      "questions": arguments.get("questions", []), "message": arguments.get("query", "")},
        }])
        if payload is None:
            return self.reject(tool_result="Call ask_user with a nonempty query or structured questions.")
        if self.target is None:
            self.target = register_question_target(
                self.group_id, self.context.session_id, self.context.member_name, ctx.agent,
            )
        from jiuwenswarm.agents.swarm.group_route import candidate, block_reason

        item = candidate(self.group_id, turn.client_message_id) if turn is not None else None
        blocked = block_reason(item, self.member_name, "ask_user")
        if blocked:
            return self.reject(tool_result=blocked)
        if item is not None and item["status"] in {"pending", "clarification"}:
            from jiuwenswarm.agents.swarm.group_route import route_lock, save_candidate

            async with route_lock(self.group_id, turn.client_message_id):
                item = candidate(self.group_id, turn.client_message_id)
                if item["status"] not in {"pending", "clarification"}:
                    return self.reject(tool_result="This message already has a routing decision.")
                await GroupQuestions(self.group_id).ask(
                    payload, self.target, self.member_name, None, client_message_id=turn.client_message_id,
                )
                item.update(status="clarification")
                await save_candidate(self.group_id, item)
            from jiuwenswarm.agents.swarm.group_route import publish

            await publish(self.group_id, item)
        else:
            execution_id = self.execution_id
            if execution_id is None:
                active = GroupExecutions(self.group_id).active(self.member_name)
                execution_id = active["execution_id"] if active is not None else None
            await GroupQuestions(self.group_id).ask(payload, self.target, self.member_name, execution_id)
        return decision
