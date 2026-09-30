"""paper-gen-agent input contract rail.

Injects the canonical invoke-protocol contract and REPLAN route table into
every model call, so the root LLM has the stage transition rules in scope
before deciding which tool to invoke. Mirrors plan-supervisor's
InputValidationRail shape; uses the same deep-adapter that automatically
injects AskUserRail / StructuredAskUserRail elsewhere in JiuwenSwarm.
"""

from __future__ import annotations

from openjiuwen.core.sys_operation.rail import Rail, RailContext


PROTOCOL_BLOCK = """
# paper-gen-agent invoke protocol (canonical, v0.2)

Each invoke_*_subagent tool returns the same shape:

  {
    "status": "completed | replan | revise | aborted | failed",
    "output_dir": "<abs path>",
    "summary": "<one-line summary>",
    "artifacts": { ... stage-specific ... },
    "next_action": {
      "type": "proceed | replan_to:<stage> | abort",
      "target_stage": "<stage>",
      "reason": "<human-readable>"
    }
  }

Route by next_action.type:
  proceed         -> update_pipeline_state mark current stage completed, advance current_stage
  replan_to:X     -> aggregate feedback to feedback/replan_to_<X>.json, re-invoke stage X
  revise          -> re-invoke current stage with iteration_context.rounds++
  abort / aborted -> mark current_stage aborted, stop pipeline, render summary

Stage order (default progression, replan can jump anywhere):
  conception -> planning -> experiment -> writing -> done

Iteration limits (enforced via update_pipeline_state):
  planning:  max_planning_replan = 2
  writing:   max_writing_revise   = 3
  -> exceed -> abort

Pipeline state schema: see tools/paper_gen_tools.py ReadPipelineStateTool._initial_state
""".strip()


class PaperGenInputRail(Rail):
    """Inject the 4-stage invoke protocol before every model call."""

    name = "paper-gen-input-validation"

    def before_model_call(self, context: RailContext) -> RailContext:
        # Append the protocol block to the system prompt. The framework's
        # deep adapter ensures this lands before any per-turn user message.
        context.system_prompt = (context.system_prompt or "") + "\n\n" + PROTOCOL_BLOCK
        return context
