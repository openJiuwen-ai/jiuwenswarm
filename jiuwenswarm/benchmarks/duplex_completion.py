"""Submit after a passing final check without asking the model to finish again."""
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.single_agent.rail.base import AgentRail, ToolCallInputs


class VerifyAndFinish(Tool):
    def __init__(self, *, execute, submission_state, events):
        super().__init__(ToolCard(name="VerifyAndFinish", description=(
            "Run your final assertion-based checks on the saved deliverable and submit it. "
            "Use a nonzero exit code for any failed check. On success this ends your turn "
            "without another model call. Failed checks return for repair. This is not the hidden grader."),
            input_params={"type": "object", "properties": {
                "command": {"type": "string"}, "summary": {"type": "string"},
                "timeout_seconds": {"type": "integer"}},
                "required": ["command", "summary"], "additionalProperties": False}))
        self.execute, self.submission_state, self.events = execute, submission_state, events
        self.accepted = None

    async def invoke(self, inputs, **kwargs):
        self.accepted = None
        before = await self.submission_state()
        if not before["ready"]:
            return self._reject(before["reason"])
        output = await self.execute(inputs, tool_name="VerifyAndFinish")
        if not output.startswith("exit_code=0\n"):
            return self._reject("Final checks failed; repair the observed failure before submitting.", output)
        after = await self.submission_state()
        if not after["ready"] or after["revision"] != before["revision"]:
            return self._reject("Deliverable changed during verification; verify the current version.", output)
        self.accepted = {"submitted": True, "summary": inputs["summary"],
                         "revision": after["revision"], "verification_output": output[:2000]}
        self.events.add("submission_accepted", revision=after["revision"], summary=inputs["summary"])
        return self.accepted

    def _reject(self, reason, output=""):
        self.events.add("submission_rejected", reason=reason)
        return {"submitted": False, "reason": reason, "verification_output": output[:2000]}

    async def stream(self, inputs, **kwargs):
        raise NotImplementedError


class FinishAfterVerification(AgentRail):
    priority = 900

    def __init__(self, tool):
        self.tool = tool

    async def after_tool_call(self, ctx):
        if (isinstance(ctx.inputs, ToolCallInputs) and ctx.inputs.tool_name == "VerifyAndFinish"
                and ctx.exception is None and self.tool.accepted is not None):
            ctx.request_force_finish(self.tool.accepted)
