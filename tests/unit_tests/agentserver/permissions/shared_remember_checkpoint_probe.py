"""Subprocess regression for real Core handler, Host writer and SQLite state."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


async def run(root: Path, permanent: bool, phase: str) -> None:
    os.environ["JIUWENSWARM_DATA_DIR"] = str(root / "data")
    global_path = root / "data" / "config" / "config.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    if not global_path.exists():
        global_path.write_text("permissions:\n  enabled: true\n  mode: auto\n  defaults: {'*': allow}\n  tools: {unknown_lookup: allow}\n  file_guard: {enabled: false}\n")

    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.runner.callback import AbortError
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.single_agent.interrupt.handler import ToolInterruptHandler, ResumeContext
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY, INTERRUPTION_KEY
    from openjiuwen.core.single_agent.rail.base import InvokeInputs
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.common import config
    from jiuwenswarm.common.utils import get_checkpoint_dir
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        ensure_persistent_checkpointer, close_persistent_checkpointer,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
    from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
    from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
        RootPermissionQueueRail, RootPermissionCompletionRail,
        bind_root_permission_request, reset_root_permission_request,
    )
    from tests.unit_tests.agentserver.permissions.test_shared_remember_chain import build, context

    await ensure_persistent_checkpointer()
    session = Session(session_id="checkpoint-session", card=AgentCard(id="remember-root", name="remember-root"), close_stream_on_post_run=False)
    await session.pre_run()
    policy = config.load_yaml_round_trip(global_path)["permissions"]
    rail = build(policy, root / "workspace", session.get_session_id(), True)
    target = layers.user_permissions_path() if permanent else layers.session_permissions_path(session.get_session_id())
    try:
        if phase == "approve":
            queue = RootPermissionQueue()
            queue_rail = RootPermissionQueueRail(queue)
            ctx = context(session.get_session_id(), "unknown_lookup", {})
            ctx.session = session
            token = bind_root_permission_request(root_session_id=session.get_session_id(), request_id="approve", enabled=True, queue=queue)
            try:
                await queue_rail.before_tool_call(ctx)
                try:
                    await rail.before_tool_call(ctx)
                except AbortError as interrupted:
                    ctx.exception = interrupted
                    await queue_rail.after_tool_call(ctx)
                    interrupt = interrupted.cause
                else:
                    raise AssertionError("initial call must require approval")
                handler = ToolInterruptHandler(SimpleNamespace())
                state, _ = handler.build_interrupt_state(
                    [(interrupt, None)], [ctx.inputs.tool_call],
                    AssistantMessage(tool_calls=[ctx.inputs.tool_call]), 0,
                )
                assert interrupt.request.auto_confirm_key == ""
                handler.save(state, session)
                snapshot = {
                    "result_type": "interrupt", "interrupt_ids": [ctx.inputs.tool_call.id],
                    "state": [{"id": ctx.inputs.tool_call.id, "value": interrupt.request}],
                }
                card = queue.reconcile(snapshot, root_session_id=session.get_session_id()).cards[0]
                answer = InteractiveInput()
                answer.update(card.key.invocation_id, {"approved": True, "auto_confirm": True, "persist_allow": permanent})
                reserved = queue.reserve_answer(session.get_session_id(), answer)
                calls = []
                async def execute(resume_ctx, tool_calls, sdk_session, _context):
                    assert sdk_session is session and len(tool_calls) == 1
                    assert session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})
                    resume_ctx.inputs.tool_call = tool_calls[0]
                    await queue_rail.before_tool_call(resume_ctx)
                    await rail.before_tool_call(resume_ctx)
                    assert not resume_ctx.extra.get("_skip_tool")
                    calls.append(tool_calls[0].id)
                    await RootPermissionCompletionRail(queue).before_tool_call(resume_ctx)
                    return [(None, None)]
                resumed_ctx = context(session.get_session_id(), "unknown_lookup", {})
                resumed_ctx.session = session
                result = await handler.handle_resume(ResumeContext(
                    state=state, user_input=reserved.interactive_input, ctx=resumed_ctx,
                    context=SimpleNamespace(), session=session, invoke_inputs=InvokeInputs(query=reserved.interactive_input),
                    execute_tool_call=execute,
                ))
                assert result is None and calls == ["call-1"] and target.is_file()
                handler.save(state, session)
                session.update_state({"remember_checkpoint_probe": "approved"})
                await session.commit()
                report = {"handler_calls": len(calls), "persisted": target.is_file()}
            finally:
                reset_root_permission_request(token)
        else:
            assert session.get_state("remember_checkpoint_probe") == "approved"
            assert target.is_file()
            assert not session.get_state(INTERRUPTION_KEY).interrupted_tools
            session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: {}})
            ctx = context(session.get_session_id(), "unknown_lookup", {}, "restart")
            ctx.session = session
            await rail.before_tool_call(ctx)
            assert not ctx.extra.get("_skip_tool")
            report = {"sqlite_restored": True, "file_rule_allowed_without_marker": True}
        assert any(path.is_file() and path.read_bytes()[:16] == b"SQLite format 3\x00" for path in get_checkpoint_dir().rglob("*"))
        (root / f"{phase}.json").write_text(json.dumps(report))
    finally:
        await close_persistent_checkpointer()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), bool(int(sys.argv[2])), sys.argv[3]))
