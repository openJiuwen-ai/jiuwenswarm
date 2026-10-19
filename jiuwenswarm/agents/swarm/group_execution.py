"""Group task tools and invocation policy, using the existing harness extensions."""

import json

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element
from openjiuwen.agent_teams.rails.team_context import get_team_backend
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, RailSpec

from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime.group_service import GroupError, read_group

START_TASK = "swarm.group_start_task"
SUBMIT_TASK = "swarm.group_submit_task"
TASK_POLICY = "swarm.group_task_policy"
TASK_RULES = (
    "An @ mention alone is ordinary conversation: reply with group_send_message. "
    "For substantive work you choose to undertake, call start_group_task before doing the work. "
    "For an assigned execution, do not publish progress, reasoning or intermediate results to the group. "
    "Use submit_group_task_result for the final result and attachment references. "
    "An expert-team proxy may do the work directly and submit the result itself. "
    "Only call run_team when work requires the private team; it updates the same execution card."
)


def install_execution_policy(agent, *, include_tools=True):
    tools = list(agent.tools or [])
    known = {getattr(tool, "type", None) for tool in tools}
    task_tools = (START_TASK, SUBMIT_TASK) if include_tools else ()
    for tool_type in task_tools:
        if tool_type not in known:
            tools.append(BuiltinToolSpec(type=tool_type))
    agent.tools = tools
    rails = list(agent.rails or [])
    if not any(getattr(rail, "type", None) == TASK_POLICY for rail in rails):
        rails.append(RailSpec(type=TASK_POLICY))
    agent.rails = rails


class GroupTaskTool(Tool):
    def __init__(self, backend, context, *, submit: bool):
        name = "submit_group_task_result" if submit else "start_group_task"
        properties = (
            {"execution_id": {"type": "string"}, "result": {"type": "string"},
             "attachments": {"type": "array", "items": {"type": "object"}}}
            if submit else {"run_id": {"type": "string"}, "task": {"type": "string"}}
        )
        super().__init__(ToolCard(
            id=f"group.{name}", name=name,
            description=(
                "Publish the final task result." if submit else "Start substantive work, registering an execution."
            ),
            input_params={
                "type": "object", "properties": properties,
                "required": ["execution_id", "result"] if submit else ["run_id", "task"],
                "additionalProperties": False,
            },
        ))
        self.backend = backend
        self.context = context
        self.submit = submit

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    async def invoke(self, inputs, **kwargs):
        store = GroupExecutions(self.context.session_id)
        try:
            if self.submit:
                result = await store.finish(
                    inputs.get("execution_id"), self.backend, inputs.get("result"), inputs.get("attachments", []),
                )
            else:
                records = await store.create(
                    [self.backend.member_name], inputs.get("task"), inputs.get("run_id"),
                )
                result = await store.update(records[0]["execution_id"], status="running", phase="executing")
            return ToolOutput(success=True, data=result)
        except (ValueError, OSError) as exc:
            return ToolOutput(success=False, error=str(exc))


class GroupTaskPolicyRail(DeepAgentRail):
    def __init__(self, context):
        super().__init__()
        self.context = context

    async def before_invoke(self, ctx):
        from openjiuwen.harness.prompts import PromptSection

        group = read_group(self.context.session_id)
        if group is None:
            return
        store = GroupExecutions(self.context.session_id)
        await store.reconcile()
        active = store.active(self.context.member_name)
        member_names = {member["member_name"] for member in group["members"]}
        content = TASK_RULES
        if self.context.member_name not in member_names:
            content = (
                "Coordinate public conversation. Organization progress belongs to system execution events. "
                "Only publish the final organization deliverable to the group after the root task completes."
            )
        if active is not None:
            if active["status"] == "queued":
                active = await store.update(active["execution_id"], status="running", phase="executing")
            content += "\nAssigned execution (use this ID; do not create another task): " + json.dumps({
                "execution_id": active["execution_id"], "task": active["task"],
            }, ensure_ascii=False)
        builder = ctx.agent.system_prompt_builder
        builder.remove_section("group_task_policy")
        builder.add_section(PromptSection(
            name="group_task_policy", content={"en": content, "cn": content}, priority=90,
        ))

    async def before_tool_call(self, ctx):
        if ctx.inputs.tool_name not in {"group_send_message", "send_file"}:
            return
        if read_group(self.context.session_id) is None:
            return
        store = GroupExecutions(self.context.session_id)
        active = store.active(self.context.member_name)
        backend = get_team_backend(self.context)
        if active is None and getattr(backend, "is_leader", False):
            active = store.active(None)
        if active is None:
            return
        from openjiuwen.core.foundation.llm import ToolMessage

        error = (
            "An execution is active. Use submit_group_task_result for the final result; progress stays in the execution card."
        )
        call = ctx.inputs.tool_call
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = {"error": error}
        ctx.inputs.tool_msg = ToolMessage(content=error, tool_call_id=call.id if call else "")

    async def after_tool_call(self, ctx):
        org_task_tools = {"org_create_task", "org_update_task", "org_claim_task", "org_delegate_task"}
        if ctx.inputs.tool_name not in org_task_tools:
            return
        group = read_group(self.context.session_id)
        if group is None:
            return
        output = ctx.inputs.tool_result
        if isinstance(output, ToolOutput):
            output = output.model_dump()
        if not isinstance(output, dict) or not output.get("success"):
            return
        task = output.get("data")
        if not isinstance(task, dict) or task.get("parent_task_id"):
            return
        root_task_id = task.get("task_id")
        if not isinstance(root_task_id, str):
            return
        backend = get_team_backend(self.context)
        if backend is None or backend.team_name != group["team_name"]:
            return
        from jiuwenswarm.server.runtime.group_execution import project_organization_progress

        manager = backend.org_task_manager
        phases = {"OPEN": "queued", "COMPLETED": "root_completed", "FAILED": "failed"}
        phase = phases.get(task.get("status"), "executing")
        if ctx.inputs.tool_name == "org_create_task":
            phase = "queued"
        await project_organization_progress(self.context.session_id, backend.team_name, {
            "root_task_id": root_task_id, "phase": phase, "title": task.get("title"),
            "organization_id": manager.organization_id if manager is not None else None,
        })

    async def after_invoke(self, ctx):
        if read_group(self.context.session_id) is None:
            return
        store = GroupExecutions(self.context.session_id)
        active = store.active(self.context.member_name)
        if active is not None and active["phase"] != "publishing":
            await store.update(active["execution_id"], status="interrupted", phase="result_not_submitted")


@harness_element(kind=ElementKind.TOOL, name=START_TASK, description="Create a group execution.")
def build_start_group_task(params, context):
    return GroupTaskTool(_backend(context), context, submit=False)


@harness_element(kind=ElementKind.TOOL, name=SUBMIT_TASK, description="Submit a group execution result.")
def build_submit_group_task(params, context):
    return GroupTaskTool(_backend(context), context, submit=True)


def _backend(context):
    backend = get_team_backend(context)
    if backend is None or backend.conversation_mode != "group_chat":
        raise GroupError("MODE_MISMATCH", "group task tool requires group chat")
    return backend


@harness_element(kind=ElementKind.RAIL, name=TASK_POLICY, description="Separate group tasks from public conversation.")
def build_group_task_policy(params, context):
    return GroupTaskPolicyRail(context)
