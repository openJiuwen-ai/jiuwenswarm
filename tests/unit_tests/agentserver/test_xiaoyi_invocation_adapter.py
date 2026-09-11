from __future__ import annotations

from jiuwenswarm.common.invocation_context import (
    INVOCATION_CONTEXT_VERSION,
    InvocationContext,
)
from jiuwenswarm.common.invocation_context.model_trace import export_trace_headers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.xiaoyi_invocation import (
    build_xiaoyi_device_command_context,
    build_xiaoyi_invocation_extension,
    build_xiaoyi_trace_context,
    get_xiaoyi_invocation_extension,
    get_xiaoyi_trace_header_exporters,
    request_billing_trace_id,
)


def test_xiaoyi_extension_keeps_device_routing_out_of_public_context() -> None:
    request = AgentRequest(
        request_id="request-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        chat_id="root-1",
        params={"session_id": "params-1", "task_id": "task-1"},
        metadata={
            "xiaoyi_root_session_id": "root-override",
            "xiaoyi_params_session_id": "params-override",
            "xiaoyi_task_id": "task-override",
            "xiaoyi_rpc_id": "message-1",
            "xiaoyi_device_id": "device-1",
            "scheduled_device": {"required_intents": ["CreateNote"]},
            "cron": {"job_id": "job-1"},
            "app_id": "app-1",
            "binding_id": "binding-1",
        },
    )
    invocation = InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id="invocation-1",
        request_id=request.request_id,
        session_id=request.session_id,
        channel_id=request.channel_id,
        chat_id=request.chat_id,
        metadata=build_xiaoyi_invocation_extension(request),
    )

    xiaoyi = get_xiaoyi_invocation_extension(invocation)
    device = build_xiaoyi_device_command_context(invocation)

    assert xiaoyi is not None
    assert xiaoyi.task_id == "task-override"
    assert device.xiaoyi_root_session_id == "root-override"
    assert device.xiaoyi_task_id == "task-override"
    assert device.xiaoyi_rpc_id == "message-1"
    assert device.metadata == {
        "invocation_id": "invocation-1",
        "app_id": "app-1",
        "binding_id": "binding-1",
        "scheduled_device": {"required_intents": ["CreateNote"]},
        "cron": {"job_id": "job-1"},
    }


def _invocation_for(request: AgentRequest) -> InvocationContext:
    return InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id="invocation-1",
        request_id=request.request_id,
        session_id=request.session_id,
        channel_id=request.channel_id,
        chat_id=request.chat_id,
        trace=build_xiaoyi_trace_context(request),
        metadata=build_xiaoyi_invocation_extension(request),
    )


def test_desktop_explicit_interaction_id_pins_trace() -> None:
    """桌面计费链路：metadata.interaction_id + session_id → trace=sessionId&interactionId。"""
    request = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id="desktop_sess_1",
        metadata={"interaction_id": "inter-1"},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert trace.trace_id == "desktop_sess_1&inter-1"
    assert trace.conversation_id == "desktop_sess_1"
    assert trace.interaction_id == "inter-1"


def test_desktop_trace_stable_across_hitl_resume() -> None:
    """HITL 续跑换新 request_id，但 interaction_id 复用 → trace 全程不变。"""
    first = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id="desktop_sess_1",
        metadata={"interaction_id": "inter-1"},
    )
    resumed = AgentRequest(
        request_id="req-2",
        channel_id="desktop",
        session_id="desktop_sess_1",
        metadata={"interaction_id": "inter-1"},
    )

    assert build_xiaoyi_trace_context(first).trace_id == build_xiaoyi_trace_context(resumed).trace_id


def test_desktop_without_interaction_id_keeps_no_trace() -> None:
    """未下发 interaction_id 的普通桌面请求不注入 trace（行为不变）。"""
    request = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id="desktop_sess_1",
    )

    assert build_xiaoyi_trace_context(request) is None


def test_desktop_cron_run_uses_session_and_interaction() -> None:
    """桌面 cron（params.cron）：显式 interaction_id 优先于 cron/task_id 分支。"""
    request = AgentRequest(
        request_id="req-cron-1",
        channel_id="desktop",
        session_id="desktop_cron_sess",
        params={"cron": {"job_id": "job-1", "run_id": "run-1"}},
        metadata={"interaction_id": "cron-run-1"},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert trace.trace_id == "desktop_cron_sess&cron-run-1"


def test_desktop_long_interaction_id_shortened_to_8() -> None:
    """x-hag-trace-id 上限 64（celia 模型网关拒绝更长、回空错误帧）：
    interactionId 为长 id（UUID 36 字符）时只取前 8 位，缩短后整体不再超限。"""
    session_id = "desktop_1a03c6682bc_1e6cef4a2c5a"  # 32 字符
    interaction_id = "b7c1de13-5cbb-46b2-b703-cf6746b59ef2"  # UUID 36 字符
    request = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id=session_id,
        metadata={"interaction_id": interaction_id},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert trace.trace_id == f"{session_id}&{interaction_id[:8]}"
    assert len(trace.trace_id) <= 64
    # conversation_id/interaction_id 字段不受缩短影响（仍是完整值）
    assert trace.conversation_id == session_id
    assert trace.interaction_id == interaction_id


def test_desktop_overlong_trace_truncated_from_tail() -> None:
    """sessionId 极长时核心段上限 45（64 - 最长计费前缀 19），先截 session 段、
    保住 interaction 短码（每轮 query 的唯一区分维度）。"""
    session_id = "s" * 100
    interaction_id = "b7c1de13-5cbb-46b2-b703-cf6746b59ef2"
    request = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id=session_id,
        metadata={"interaction_id": interaction_id},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert len(trace.trace_id) == 45
    assert trace.trace_id == f'{"s" * 36}&{interaction_id[:8]}'


def test_xiaoyi_channel_task_id_branch_unchanged() -> None:
    """回归：xiaoyi 渠道仍按 task_id 全串派生 trace。"""
    request = AgentRequest(
        request_id="req-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        metadata={"xiaoyi_task_id": "sess-x&19&ea5d&0"},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert trace.trace_id == "sess-x&19&ea5d&0"
    assert trace.conversation_id == "sess-x"
    assert trace.interaction_id == "19"


def test_desktop_trace_headers_exported_via_fallback_exporter() -> None:
    """无 Xiaoyi 扩展的桌面 invocation：兜底 exporter 输出同形态 trace 头。"""
    request = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id="desktop_sess_1",
        metadata={"interaction_id": "inter-1"},
    )
    invocation = _invocation_for(request)

    headers = export_trace_headers(invocation, get_xiaoyi_trace_header_exporters())

    assert headers == {
        "x-hag-trace-id": "desktop_sess_1&inter-1",
        "x-session-id": "desktop_sess_1",
        "x-interaction-id": "inter-1",
    }


def test_xiaoyi_invocation_headers_not_claimed_by_desktop_exporter() -> None:
    """xiaoyi invocation 仍由 xiaoyi exporter 导出（兜底 exporter 不抢）。"""
    request = AgentRequest(
        request_id="req-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        metadata={"xiaoyi_task_id": "sess-x&19&ea5d&0"},
    )
    invocation = _invocation_for(request)

    headers = export_trace_headers(invocation, get_xiaoyi_trace_header_exporters())

    assert headers["x-hag-trace-id"] == "sess-x&19&ea5d&0"


def test_xiaoyi_bare_conversation_id_session_pins_trace() -> None:
    """V3：新方案下 xiaoyi 会话 session_id = 裸 conversationId，显式 interaction_id
    时 trace 核心段首段即 conversationId，全链路一致。"""
    conversation_id = "1788936453184"
    request = AgentRequest(
        request_id="req-1",
        channel_id="xiaoyi",
        session_id=conversation_id,
        metadata={"interaction_id": "inter-1"},
    )

    trace = build_xiaoyi_trace_context(request)

    assert trace is not None
    assert trace.trace_id == f"{conversation_id}&inter-1"
    assert trace.conversation_id == conversation_id
    assert trace.interaction_id == "inter-1"


def test_request_billing_trace_id_matches_xiaoyi_trace_context() -> None:
    """hook/GaussPD 注入值与 build_xiaoyi_trace_context.trace_id 同源。"""
    desktop = AgentRequest(
        request_id="e2c7953d-96b4-4aaa-bbbb-cccccccccccc",
        channel_id="desktop",
        session_id="desktop_1a08f97a7d6_5e266462c9be",
        metadata={"interaction_id": "e2c7953d-96b4-4aaa-bbbb-cccccccccccc"},
    )
    xiaoyi = AgentRequest(
        request_id="req-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        metadata={"xiaoyi_task_id": "sess-x&19&ea5d&0"},
    )
    bare_desktop = AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        session_id="desktop_sess_1",
    )

    assert (
        request_billing_trace_id(desktop)
        == "desktop_1a08f97a7d6_5e266462c9be&e2c7953d"
    )
    assert request_billing_trace_id(xiaoyi) == "sess-x&19&ea5d&0"
    assert request_billing_trace_id(bare_desktop) == ""

