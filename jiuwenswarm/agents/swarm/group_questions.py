"""Bind the existing structured ask-user rail to a public-group question owner."""

import json

from openjiuwen.agent_teams.rails.team_context import get_team_backend
from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
    question_interactions,
)
from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime.group_questions import GroupQuestions, register_question_target


async def publish_group_issue_reviews(ctx, context, group):
    """Send an addressed issue confirmation to the public group's owner."""
    result = getattr(getattr(ctx, "inputs", None), "result", None)
    if not isinstance(result, dict) or result.get("result_type") != "interrupt":
        return
    backend = get_team_backend(context)
    if getattr(backend, "team_name", None) != group["team_name"] or group["channel_id"] != "web":
        return
    for interaction in question_interactions(result.get("state")):
        payload = convert_interactions_to_ask_user_question([interaction])
        if not payload or payload["source"] != "confirm_interrupt" or not payload.get("interrupt_id"):
            continue
        if not any(question.get("issue_report") for question in payload["questions"]):
            continue
        target = register_question_target(
            context.session_id, context.session_id, context.member_name, ctx.agent,
        )
        active = GroupExecutions(context.session_id).active(context.member_name)
        execution_id = active["execution_id"] if active is not None else None
        await GroupQuestions(context.session_id).ask(payload, target, context.member_name, execution_id)


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
        self.target = register_question_target(
            self.group_id, self.context.session_id, self.context.member_name, ctx.agent,
        )

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
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
        execution_id = self.execution_id
        if execution_id is None:
            active = GroupExecutions(self.group_id).active(self.member_name)
            execution_id = active["execution_id"] if active is not None else None
        await GroupQuestions(self.group_id).ask(payload, self.target, self.member_name, execution_id)
        return decision
