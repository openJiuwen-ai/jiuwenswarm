# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""xiaoyi 渠道 clientVariables（workspace/permission）+ 权限审批桥接单测。

覆盖：
  - data part variables.clientVariables 解析（workspace 落 project_dir/cwd/trusted_dirs，
    permission 仅作用于本条消息，不写共享配置、不触发热重载）
  - 空消息守卫（仅 variables/events 的帧不触发任务运行）
  - chat.ask_user_question → 手机端审批提示渲染 + 待答复登记
  - 用户回复（文本约定 / PermissionReply 事件）→ interrupt resume 路由参数
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from types import SimpleNamespace

import pytest
import yaml

import jiuwenswarm.common.config as cfgmod
from jiuwenswarm.common.permission_profile import (
    PERMISSION_PROFILE_FULL_ACCESS,
    normalize_permission_profile,
    permission_profile_config_patch,
    resolve_client_workspace,
    resolve_trusted_dirs,
    with_time_directive,
    with_workspace_directive,
)
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
    _normalize_approval_text,
)


# ------------------------------------------------------------------ fixtures


class _FakeWs:
    def __init__(self, sink: list):
        self.sink = sink

    async def send(self, data):
        self.sink.append(json.loads(data))


def _make_channel(captured: list, sent: list) -> XiaoyiChannel:
    """绕过 __init__ 的最小化渠道实例（仅填充被测路径用到的字段）。"""
    ch = XiaoyiChannel.__new__(XiaoyiChannel)
    ch.config = XiaoyiChannelConfig(
        enabled=True, agent_id="ag1", ak="ak", sk="sk", channel_id="xiaoyi"
    )
    ch._on_message_cb = lambda m: (captured.append(m) or True)
    ch.bus = None
    ch._session_task_map = {}
    ch._session_active = set()
    ch._active_push_sessions = {}
    ch._task_timeout_tasks = {}
    ch._session_timeout_tasks = {}
    ch._session_heartbeat_tasks = {}
    ch._sessions_waiting_for_push = {}
    ch._pending_approvals = {}
    ch._ws_connections = {"k": _FakeWs(sent)}
    ch._accumulated_texts = {}
    ch._send_locks = {}
    ch._reload_permissions_cb = None
    ch._status_update_interval = 3600
    # beta3 新增状态（_handle_message_stream/_finalize 路径引用）
    ch._active_tasks = set()
    ch._latest_platform_tasks = {}
    ch._team_sessions = set()
    ch._team_tasks = set()
    ch._team_last_leader_finals = {}
    ch._sessions_marked_for_cleanup = {}
    ch._stream_text_buffers = {}
    ch._task_last_activity = {}
    ch._ws_flush_buffers = {}
    ch._ws_flush_tasks = {}
    ch._push_merge_buffers = {}
    ch._push_flush_tasks = {}
    # 连发顶替收口路径引用（_cancel_superseded_platform_task）
    ch._canceled_platform_tasks = {}
    ch._text_stream_prefix = {}
    ch._text_stream_pending = {}
    ch._data_event_handlers = {}
    ch._gui_agent_handlers = []
    ch._device_command_locks = {}
    ch.push_id = ""
    return ch


def _build_stream_msg(text, client_vars=None, conv="conv-1", top="top-1", task="task-1", files=None):
    parts = []
    if client_vars is not None:
        parts.append({
            "kind": "data",
            "data": {"variables": {
                "clientVariables": client_vars,
                "systemVariables": {"push_id": "p1"},
            }},
        })
    if text:
        parts.append({"kind": "text", "text": text})
    for f in files or []:
        parts.append({"kind": "file", "file": f})
    return {
        "conversationId": conv,
        "deviceId": "dev",
        "id": task,
        "jsonrpc": "2.0",
        "method": "message/stream",
        "params": {
            "id": task,
            "message": {"kind": "message", "messageId": task, "parts": parts, "role": "user"},
            "sessionId": conv,
        },
        "sessionId": top,
        "agentId": "agent0c18",
        "agentMode": "OpenClawToC",
        "userId": "u1",
    }


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "permissions:\n"
        "  enabled: false\n"
        "  permission_mode: normal\n"
        "  tools:\n"
        "    bash: allow\n"
        "  file_guard:\n"
        "    enabled: true\n"
        "    defaults:\n"
        "      read: allow\n"
        "      write: allow\n"
        "      exec: ask\n"
        "  rules:\n"
        "    - id: keepme\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "CONFIG_YAML_PATH", path)
    return path


@pytest.fixture()
def workspace(tmp_path):
    d = tmp_path / "ws01"
    d.mkdir()
    return d


def _cleanup_tasks(ch: XiaoyiChannel) -> None:
    for t in list(ch._task_timeout_tasks.values()) + list(ch._session_timeout_tasks.values()):
        t.cancel()


# ------------------------------------------------------------------ permission_profile 纯函数


def test_normalize_permission_profile_aliases():
    assert normalize_permission_profile("default") == "default"
    assert normalize_permission_profile("默认权限") == "default"
    assert normalize_permission_profile(" full_access ") == "full_access"
    assert normalize_permission_profile("完全访问权限") == "full_access"
    assert normalize_permission_profile("FullAccess") == "full_access"
    assert normalize_permission_profile("替我审批") == "auto_approve"
    assert normalize_permission_profile("") is None
    assert normalize_permission_profile("未识别值") is None
    assert normalize_permission_profile(None) is None


def test_permission_profile_config_patch():
    p = permission_profile_config_patch("default")
    assert p["enabled"] is True and p["permission_mode"] == "strict"
    assert p["tools"]["bash"] == "ask" and p["tools"]["mcp_free_search"] == "ask"
    assert p["file_guard_rw"] == "ask"
    p = permission_profile_config_patch("full_access")
    assert p["enabled"] is False and p["file_guard_rw"] == "allow"
    assert permission_profile_config_patch("garbage") is None


def test_resolve_client_workspace(workspace):
    abspath = os.path.abspath(str(workspace))
    assert resolve_client_workspace(str(workspace)) == abspath
    assert resolve_client_workspace({"name": "x", "path": str(workspace)}) == abspath
    assert resolve_client_workspace("C:/不存在的目录xyz") == ""
    assert resolve_client_workspace("") == ""
    assert resolve_client_workspace(None) == ""
    assert resolve_trusted_dirs("default", str(workspace)) == [os.path.abspath(str(workspace))]
    assert resolve_trusted_dirs(PERMISSION_PROFILE_FULL_ACCESS, str(workspace)) is None
    assert resolve_trusted_dirs("default", "") is None


def test_with_workspace_directive(workspace):
    text = with_workspace_directive("你好", str(workspace), "default")
    assert text.startswith("你好\n\n")
    assert "<claw_workspace>" in text and "【工作空间】当前项目目录是" in text
    assert "必须落在该目录" in text
    # payload 携带本条消息的动态时间戳（本地时间，YYYY-MM-DD HH:MM:SS）
    assert re.search(r'"timestamp": "\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"', text)
    # full_access：也注入（位置提示，不含约束措辞）——否则该档下模型对工作空间零感知
    full = with_workspace_directive("你好", str(workspace), "full_access")
    assert full.startswith("你好\n\n") and "<claw_workspace>" in full
    assert "必须落在该目录" not in full
    assert re.search(r'"timestamp": "\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"', full)
    assert with_workspace_directive("你好", "", "default") == "你好"


def test_with_time_directive():
    """未选工作空间时仅注入时间尾段（与工作空间尾段同构，可被既有剥离链处理）。"""
    text = with_time_directive("你好")
    assert text.startswith("你好\n\n")
    assert "<claw_time>" in text and "【当前时间】现在是" in text
    # payload 携带本条消息的动态时间戳（本地时间，YYYY-MM-DD HH:MM:SS）
    assert re.search(r'"timestamp": "\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"', text)


def test_update_permission_profile_in_config(cfg_file):
    overlay = cfg_file.with_name("config.user.yaml")
    assert cfgmod.update_permission_profile_in_config("default") is True
    assert not overlay.is_file()
    system = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert system["permissions"]["enabled"] is True
    assert system["permissions"]["permission_mode"] == "strict"
    assert system["permissions"]["tools"]["bash"] == "ask"
    assert system["permissions"]["tools"]["mcp_free_search"] == "ask"
    assert system["permissions"]["file_guard"]["defaults"]["read"] == "ask"
    assert system["permissions"]["rules"] == [{"id": "keepme"}]
    assert system["permissions"]["file_guard"]["defaults"]["exec"] == "ask"
    merged = cfgmod.get_config_raw()
    assert merged["permissions"]["enabled"] is True
    assert merged["permissions"]["permission_mode"] == "strict"
    assert merged["permissions"]["rules"] == [{"id": "keepme"}]
    assert merged["permissions"]["file_guard"]["defaults"]["exec"] == "ask"
    assert cfgmod.update_permission_profile_in_config("默认权限") is False
    assert cfgmod.update_permission_profile_in_config("完全访问权限") is True
    assert yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["permissions"]["enabled"] is False
    assert cfgmod.update_permission_profile_in_config("未识别") is False


# ------------------------------------------------------------------ 审批回复识别


def test_approval_reply_vocabulary():
    assert _normalize_approval_text(" 同意。 ") == "同意"
    assert _normalize_approval_text("OK！") == "ok"

    ch = _make_channel([], [])
    session = "s1"
    assert ch._resolve_approval_reply(session, "同意", None) is None  # 无 pending

    def arm(request_id: str):
        ch._pending_approvals[session] = {
            "request_id": request_id,
            "source": "permission_interrupt",
            "task_id": "t",
            "created_at": time.time(),
        }

    arm("r1")
    ans, pending = ch._resolve_approval_reply(session, "同意", None)
    assert ans == {"selected_options": ["本次允许"], "custom_input": ""}
    assert pending["request_id"] == "r1"
    assert session not in ch._pending_approvals  # 已消费

    arm("r2")
    ans, _ = ch._resolve_approval_reply(session, "会话内允许", None)
    assert ans["selected_options"] == ["会话内记住"]

    arm("r3")
    ans, _ = ch._resolve_approval_reply(session, "永久允许", None)
    assert ans["selected_options"] == ["永久记住"]

    arm("r4")
    ans, _ = ch._resolve_approval_reply(session, "拒绝！", None)
    assert ans["selected_options"] == ["拒绝"]

    # 其他意见 → 拒绝 + custom_input 原文（对齐 Web 端 custom_input 语义）
    arm("r5")
    ans, _ = ch._resolve_approval_reply(session, "这个目录不对，换一个", None)
    assert ans == {"selected_options": [], "custom_input": "这个目录不对，换一个"}

    # PermissionReply data event
    arm("r6")
    ans, _ = ch._resolve_approval_reply(session, "", {"action": "always_allow"})
    assert ans["selected_options"] == ["永久记住"]

    # 未识别 action / 空文本不消费 pending
    arm("r7")
    assert ch._resolve_approval_reply(session, "", {"action": "whatever"}) is None
    assert ch._resolve_approval_reply(session, "   ", None) is None
    assert session in ch._pending_approvals

    # 过期自动作废
    ch._pending_approvals[session]["created_at"] = time.time() - 31 * 60
    assert ch._resolve_approval_reply(session, "同意", None) is None
    assert session not in ch._pending_approvals


# ------------------------------------------------------------------ 消息流全链路


@pytest.mark.asyncio
async def test_message_stream_applies_workspace_and_permission(cfg_file, workspace):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    original_permissions = cfgmod.load_yaml_round_trip(cfg_file)["permissions"]
    try:
        # full_access：project_dir/cwd 下发、无 trusted_dirs、仅注入位置提示
        await ch._handle_message_stream(
            _build_stream_msg("帮我打个zip包", {"workspace": str(workspace), "permission": "full_access"})
        )
        m = captured[-1]
        assert m.params["project_dir"] == os.path.abspath(str(workspace))
        assert m.params["cwd"] == os.path.abspath(str(workspace))
        assert "trusted_dirs" not in m.params
        assert m.params["query"].startswith("帮我打个zip包\n\n")
        assert "<claw_workspace>" in m.params["query"]
        assert "必须落在该目录" not in m.params["query"]
        assert m.metadata["xiaoyi_task_id"] == "task-1"
        # beta3：session_id 取 params.sessionId（逻辑会话），chat_id 兜底同值
        assert m.session_id == "conv-1" and m.chat_id == "conv-1"
        assert cfgmod.load_yaml_round_trip(cfg_file)["permissions"] == original_permissions

        # default：trusted_dirs + 工作空间约束指令，保持共享配置不变
        await ch._handle_message_stream(
            _build_stream_msg("再打一个", {"workspace": str(workspace), "permission": "default"}, task="task-2")
        )
        m = captured[-1]
        assert m.params["trusted_dirs"] == [os.path.abspath(str(workspace))]
        assert m.params["query"].startswith("再打一个\n\n")
        assert "<claw_workspace>" in m.params["query"]
        assert cfgmod.load_yaml_round_trip(cfg_file)["permissions"] == original_permissions

        # 不携带 clientVariables：无工作空间 → 仅注入时间尾段（时间感知不丢）
        await ch._handle_message_stream(_build_stream_msg("普通消息", None, task="task-3"))
        m = captured[-1]
        assert "project_dir" not in m.params
        assert m.params["query"].startswith("普通消息\n\n")
        assert "<claw_time>" in m.params["query"] and "【当前时间】现在是" in m.params["query"]
        assert re.search(r'"timestamp": "\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"', m.params["query"])

        # 空消息（仅 variables）：不触发路由
        n = len(captured)
        await ch._handle_message_stream(_build_stream_msg("", {"workspace": str(workspace)}, task="task-4"))
        assert len(captured) == n
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_message_stream_downloads_into_workspace(cfg_file, workspace, monkeypatch):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    seen: dict = {}

    async def fake_download(files, options=None, save_dir=None):
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import (
            DownloadedMedia,
        )

        seen["save_dir"] = save_dir
        dest = save_dir or "tmp"
        return [
            DownloadedMedia(
                path=os.path.join(dest, files[0].name),
                content_type="application/pdf",
                placeholder="<media:document>",
                file_name=files[0].name,
            )
        ]

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media.download_and_save_media_list",
        fake_download,
    )
    try:
        await ch._handle_message_stream(
            _build_stream_msg(
                "分析一下文档内容",
                {"workspace": str(workspace), "permission": "full_access"},
                files=[{
                    "name": "a.pdf",
                    "uri": "https://obs.example.com/a.pdf",
                    "mimeType": "application/pdf",
                }],
            )
        )
        ws = os.path.abspath(str(workspace))
        assert seen["save_dir"] == ws
        assert captured[-1].params["files"][0]["path"] == os.path.join(ws, "a.pdf")
        assert captured[-1].params["project_dir"] == ws
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_message_stream_download_without_workspace_omits_save_dir(
    cfg_file, monkeypatch
):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    seen: dict = {}

    async def fake_download(files, options=None, save_dir=None):
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import (
            DownloadedMedia,
        )

        seen["save_dir"] = save_dir
        return [
            DownloadedMedia(
                path=os.path.join("tmp", files[0].name),
                content_type="application/pdf",
                placeholder="<media:document>",
                file_name=files[0].name,
            )
        ]

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media.download_and_save_media_list",
        fake_download,
    )
    try:
        await ch._handle_message_stream(
            _build_stream_msg(
                "看这个",
                None,
                files=[{
                    "name": "b.pdf",
                    "uri": "https://obs.example.com/b.pdf",
                    "mimeType": "application/pdf",
                }],
            )
        )
        assert seen["save_dir"] is None
        assert "project_dir" not in captured[-1].params
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_enabled", [True, False])
async def test_message_profiles_do_not_change_global_permissions_or_reload(cfg_file, initial_enabled):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    reloads = []
    ch._reload_permissions_cb = lambda: reloads.append(True) or asyncio.sleep(0)
    cfg_file.write_text(f"permissions:\n  enabled: {str(initial_enabled).lower()}\n", encoding="utf-8")
    original = cfgmod.load_yaml_round_trip(cfg_file)["permissions"]
    try:
        for i, profile in enumerate(("full_access", None, "default", "auto_approve", "unknown")):
            # Distinct conversations must not change the shared policy either.
            client_vars = {"permission": profile} if profile is not None else None
            await ch._handle_message_stream(_build_stream_msg(
                "继续", client_vars, conv=f"conv-{i}", task=f"task-{i}",
            ))
            assert cfgmod.load_yaml_round_trip(cfg_file)["permissions"] == original
            assert reloads == []
        assert len(captured) == 5
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bash", "read_inside", "read_outside"])
async def test_message_profile_reaches_tool_engine_and_does_not_leak(cfg_file, workspace, monkeypatch, operation):
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, InterruptResult
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_trusted_dirs, build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )
    from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_CHANNEL_ID

    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "0")
    baseline = {"enabled": True, "package_builtin_rules": False, "defaults": {"*": "allow"},
                "tools": {"bash": "ask"},
                "file_guard": {"enabled": True, "defaults": {"read": "ask", "write": "ask"}}}
    cfg_file.write_text(yaml.safe_dump({"permissions": baseline}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "get_config_file", lambda: cfg_file)
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    rail = build_permission_rail({"permissions": baseline})
    try:
        # Reuse the rail across turns, including a profile-free message and a
        # different conversation after full_access. Both must recover ASK.
        for index, profile in enumerate(("full_access", None, "default", "auto_approve", "default")):
            await ch._handle_message_stream(_build_stream_msg(
                "执行", {"workspace": str(workspace), **({"permission": profile} if profile else {})},
                conv=f"profile-{index}", task=f"profile-task-{index}",
            ))
            message = captured[-1]
            assert message.metadata.get("xiaoyi_permission_profile") == profile
            args = ({"command": "echo profile-regression", "workdir": str(workspace)} if operation == "bash"
                    else {"file_path": str((workspace if operation == "read_inside" else workspace.parent) / "example.txt")})
            call = ToolCall(id=f"call-{index}", type="function", name="bash" if operation == "bash" else "read_file",
                            arguments=json.dumps(args))
            state = {}
            ctx = SimpleNamespace(
                session=SimpleNamespace(get_session_id=lambda: message.session_id, get_state=state.get,
                                        update_state=state.update), extra={},
                inputs=SimpleNamespace(tool_call=call, tool_name=call.name, tool_args=args),
            )
            token = setup_permission_context(message)
            channel_token = CURRENT_CHANNEL_ID.set(message.channel_id)
            try:
                apply_permission_trusted_dirs(rail, message.params.get("trusted_dirs"), str(workspace))
                decision = await rail.resolve_interrupt(ctx, call, None)
                allowed = profile in {"full_access", "auto_approve"} or operation == "read_inside"
                assert isinstance(decision, ApproveResult if allowed else InterruptResult), (profile, decision)
                if not allowed:
                    resumed = await rail.resolve_interrupt(ctx, call, {"approved": True, "auto_confirm": False})
                    assert isinstance(resumed, ApproveResult)
                    assert rail._engine.config["enabled"] is True
            finally:
                CURRENT_CHANNEL_ID.reset(channel_token)
                cleanup_permission_context(token)
            assert cfgmod.load_yaml_round_trip(cfg_file)["permissions"] == baseline
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_question_prompt_and_resume(cfg_file, workspace):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ask = type("M", (), {})()
        ask.event_type = EventType.CHAT_ASK_USER_QUESTION
        ask.payload = {
            "event_type": "chat.ask_user_question",
            "request_id": "req-777",
            "source": "permission_interrupt",
            "questions": [{
                "question": "允许执行 bash: zip ... 吗？",
                "header": "权限审批: bash",
                "options": [{"label": "本次允许"}, {"label": "会话内记住"},
                            {"label": "永久记住"}, {"label": "拒绝"}],
            }],
        }
        ask.metadata = {
            "xiaoyi_permission_profile": "default",
            "xiaoyi_session_id": "top-1",
            "xiaoyi_task_id": "task-2",
            "xiaoyi_conversation_id": "conv-1",
        }
        ask.session_id = "conv-1"
        ask.id = "req-777"
        await ch._send_legacy(ask)

        # 审批提示：登记待答复（含 question_id 映射）+ status-update(input-required)
        # 双 part 帧（text part 人类可读 + data part ToolExecute/Invoke 选项卡）
        assert ch._pending_approvals["conv-1"]["request_id"] == "req-777"
        assert ch._pending_approvals["conv-1"]["question_ids"] == {
            "req-777": "允许执行 bash: zip ... 吗？",
        }
        prompt = sent[-1]
        assert prompt["msgType"] == "agent_response"
        inner = json.loads(prompt["msgDetail"])
        result = inner["result"]
        # artifact-update 承载
        assert result["kind"] == "artifact-update"
        assert result["final"] is False  # 任务等输入，final=true 会断开端云通道
        assert result["append"] is False
        assert result["lastChunk"] is True
        parts = result["artifact"]["parts"]
        text_part = parts[0]
        assert text_part["kind"] == "text"
        # text part 只放问题本身：不带标题/标签/可选项/回复约定话术（卡片承载）
        assert text_part["text"] == "允许执行 bash: zip ... 吗？"
        data_part = parts[1]
        assert data_part["kind"] == "data"
        command = data_part["data"]["commands"][0]
        assert command["header"] == {"namespace": "ToolExecute", "name": "Invoke"}
        invoke = command["payload"]
        assert invoke["source"] == "permission_interrupt"
        assert "expiresAt" not in invoke  # 契约外字段，不发
        q = invoke["questions"][0]
        assert q["question_id"] == "req-777"
        assert q["question"] == "允许执行 bash: zip ... 吗？"
        assert "header" not in q  # 不下发短标签：端侧不渲染副标题（对齐 PC 端）
        assert q["multi_select"] is False
        assert q["text_input"] == {
            "enabled": True,
            "placeholder": "也可输入其他内容",
            "required": False,
        }
        assert q["options"][0] == {"index": 1, "label": "本次允许"}
        assert [o["label"] for o in q["options"]] == [
            "本次允许", "会话内记住", "永久记住", "拒绝",
        ]

        # AskUser 挂起期间收到流收尾事件（is_processing=false）：不得发
        # completed/final=true（手机气泡会显示"已完成"并关闭等待态），
        # 应降级为 working"等待您的确认"，且不触发会话收尾。
        ch._session_active.add("top-1")  # 状态分支守卫按顶层 sessionId 判活跃
        proc = type("M", (), {})()
        proc.event_type = EventType.CHAT_PROCESSING_STATUS
        proc.payload = {"is_processing": False}
        proc.metadata = ask.metadata
        proc.session_id = "conv-1"
        proc.id = "req-777"
        await ch._send_legacy(proc)
        inner = json.loads(sent[-1]["msgDetail"])
        assert inner["result"]["status"]["state"] == "working"
        assert inner["result"]["final"] is False
        assert "等待您的确认" in inner["result"]["status"]["message"]["parts"][0]["text"]

        # 用户回复「同意」→ interrupt resume 路由（回原始任务气泡）
        await ch._handle_message_stream(_build_stream_msg("同意", None, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-777"
        assert m.params["answers"] == [{"selected_options": ["本次允许"], "custom_input": ""}]
        assert m.params["source"] == "permission_interrupt"
        assert m.params["query"] == "" and m.params["mode"] == "agent"
        assert m.params["task_id"] == "task-2"
        assert m.metadata["xiaoyi_permission_profile"] == "default"
        assert m.session_id == "conv-1"
        assert "conv-1" not in ch._pending_approvals
        # 状态回执（msgDetail 内层 JSON 默认 \uXXXX 转义，需解析后比对）
        ack_texts = []
        for p in sent:
            inner = json.loads(p["msgDetail"])
            if inner["result"].get("kind") == "status-update":
                ack_texts.append("".join(
                    part.get("text", "")
                    for part in inner["result"]["status"]["message"]["parts"]
                ))
        assert any("已收到您的回复" in t for t in ack_texts)
    finally:
        _cleanup_tasks(ch)


def _build_ask_answer_msg(answers, source, conv="conv-1", top="top-1", task="task-1"):
    """端侧结构化应答帧：仅含 askUserAnswer data part，无 text/file。"""
    return {
        "conversationId": conv,
        "deviceId": "dev",
        "id": task,
        "jsonrpc": "2.0",
        "method": "message/stream",
        "params": {
            "id": task,
            "message": {
                "kind": "message",
                "messageId": task,
                "parts": [{"kind": "data", "data": {
                    "askUserAnswer": {"source": source, "answers": answers},
                }}],
                "role": "user",
            },
            "sessionId": conv,
        },
        "sessionId": top,
        "agentId": "agent0c18",
        "agentMode": "OpenClawToC",
        "userId": "u1",
    }


def _build_e2a_style_msg(data, conv="conv-1", top="top-1", task="task-1"):
    """端侧 E2A 风格应答帧（对齐桌面端 chat.send 参数的 data part）：仅 data part。"""
    return {
        "conversationId": conv,
        "deviceId": "dev",
        "id": task,
        "jsonrpc": "2.0",
        "method": "message/stream",
        "params": {
            "id": task,
            "message": {
                "kind": "message",
                "messageId": task,
                "parts": [{"kind": "data", "data": data}],
                "role": "user",
            },
            "sessionId": conv,
        },
        "sessionId": top,
        "agentId": "agent0c18",
        "agentMode": "OpenClawToC",
        "userId": "u1",
    }


def _build_invoke_rsp_msg(payload, conv="conv-1", top="top-1", task="task-1"):
    """端侧 ToolExecute/InvokeRsp 结构化应答帧：仅 data part。"""
    return {
        "conversationId": conv,
        "deviceId": "dev",
        "id": task,
        "jsonrpc": "2.0",
        "method": "message/stream",
        "params": {
            "id": task,
            "message": {
                "kind": "message",
                "messageId": task,
                "parts": [{"kind": "data", "data": {
                    "commands": [{
                        "header": {
                            "namespace": "ToolExecute",
                            "name": "InvokeRsp",
                        },
                        "payload": payload,
                    }],
                }}],
                "role": "user",
            },
            "sessionId": conv,
        },
        "sessionId": top,
        "agentId": "agent0c18",
        "agentMode": "OpenClawToC",
        "userId": "u1",
    }


@pytest.mark.asyncio
async def test_ask_user_answer_structured_reply_routes_resume(cfg_file):
    """端侧 askUserAnswer data part → interrupt resume（权限类白名单精确命中）。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-888",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{
                "question": "允许执行 bash 吗？",
                "selectedOptions": ["永久记住"],
                "customInput": "",
            }],
            "permission_interrupt",
            task="task-9",
        ))
        m = captured[-1]
        assert m.params["request_id"] == "req-888"
        assert m.params["source"] == "permission_interrupt"
        assert m.params["answers"] == [{"selected_options": ["永久记住"], "custom_input": ""}]
        assert m.params["query"] == "" and m.params["mode"] == "agent"
        assert m.params["task_id"] == "task-2"
        assert "conv-1" not in ch._pending_approvals  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_answer_structured_reply_fail_closed(cfg_file):
    """权限类中断选项未命中白名单 → fail-closed 按拒绝处理，不猜测放行。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-889",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        # 选项外的字符串 + 补充意见：不能授权，作拒绝理由反馈
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{
                "question": "允许执行 bash 吗？",
                "selectedOptions": ["随便怎样都行"],
                "customInput": "这个目录不对，换一个",
            }],
            "permission_interrupt",
            task="task-9",
        ))
        m = captured[-1]
        assert m.params["request_id"] == "req-889"
        assert m.params["answers"][0]["selected_options"] == ["拒绝"]
        assert m.params["answers"][0]["custom_input"] == "这个目录不对，换一个"

        # 空选 + 空文本：仍按拒绝（带未选说明），不允许放行
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-890",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "允许执行 bash 吗？", "selectedOptions": [], "customInput": ""}],
            "permission_interrupt",
            task="task-10",
        ))
        m = captured[-1]
        assert m.params["request_id"] == "req-890"
        assert m.params["answers"][0]["selected_options"] == ["拒绝"]
        assert "未选择" in m.params["answers"][0]["custom_input"]
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_answer_free_question_passthrough(cfg_file):
    """ask_user_interrupt 自由问答：answers 原样透传（含 question 对齐 + 多问题）。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-900",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [
                {"question": "你想要什么风格？", "selectedOptions": ["深色"], "customInput": ""},
                {"question": "要加图标吗？", "selectedOptions": [], "customInput": "不用了"},
            ],
            "ask_user_interrupt",
            task="task-9",
        ))
        m = captured[-1]
        assert m.params["request_id"] == "req-900"
        assert m.params["source"] == "ask_user_interrupt"
        assert m.params["answers"] == [
            {"question": "你想要什么风格？", "selected_options": ["深色"], "custom_input": ""},
            {"question": "要加图标吗？", "selected_options": [], "custom_input": "不用了"},
        ]
        assert "conv-1" not in ch._pending_approvals
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_answer_without_pending_falls_through(cfg_file):
    """无待答复审批：结构化应答帧按普通消息处理（不路由 resume、不构造任务）。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        n = len(captured)
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "q", "selectedOptions": ["深色"], "customInput": ""}],
            "ask_user_interrupt",
            task="task-9",
        ))
        # 仅 data part、无 text/file → 空帧拦截，不路由任何消息
        assert len(captured) == n
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_expired_pending_falls_through(cfg_file):
    """待答复审批过期：不消费、按普通消息处理。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-901",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time() - 31 * 60,
        }
        n = len(captured)
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "q", "selectedOptions": ["本次允许"], "customInput": ""}],
            "permission_interrupt",
            task="task-9",
        ))
        assert len(captured) == n
        assert "conv-1" not in ch._pending_approvals  # 过期条目已清理
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_invoke_rsp_reply_routes_resume(cfg_file):
    """端侧 InvokeRsp data part → interrupt resume（question_id 对齐 + custom_text）。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-910",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["允许执行 bash 吗？"],
            "question_ids": {"call_abc": "允许执行 bash 吗？"},
        }
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "responder_device_id": "dev_abc123",  # 多端抢答设备标识
            "answers": [{
                "question_id": "call_abc",
                "selected_options": ["永久记住"],
                "custom_text": None,
            }],
        }, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-910"
        assert m.params["source"] == "permission_interrupt"
        # 权限类白名单精确命中；custom_text(null) → custom_input("")
        assert m.params["answers"] == [
            {"selected_options": ["永久记住"], "custom_input": ""}
        ]
        # responder_device_id 透传进 resume（审计/UI"由指定设备完成选择"）
        assert m.params["responder_device_id"] == "dev_abc123"
        assert "conv-1" not in ch._pending_approvals  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_invoke_rsp_multi_question_alignment(cfg_file):
    """ask_user 多问题 InvokeRsp：question_id → question 文本对齐后逐条透传。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-911",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["你想要什么风格？", "要加图标吗？"],
            "question_ids": {
                "call_001": "你想要什么风格？",
                "call_001#2": "要加图标吗？",
            },
        }
        # 乱序回传：answers 按 question_id 关联，不依赖顺序/问题文本
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [
                {"question_id": "call_001#2", "selected_options": [], "custom_text": "不用了"},
                {"question_id": "call_001", "selected_options": ["深色"], "custom_text": None},
            ],
        }, task="task-9"))
        m = captured[-1]
        assert m.params["source"] == "ask_user_interrupt"
        assert m.params["answers"] == [
            {"question": "要加图标吗？", "selected_options": [], "custom_input": "不用了"},
            {"question": "你想要什么风格？", "selected_options": ["深色"], "custom_input": ""},
        ]
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_invoke_rsp_user_cancel(cfg_file):
    """user_cancel：ask_user → 空答案（跳过续跑）；权限类 → fail-closed 拒绝。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-912",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["你想要什么风格？"],
            "question_ids": {"call_001": "你想要什么风格？"},
        }
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": True,
            "answers": [],
        }, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-912"
        assert m.params["answers"] == [
            {"question": "你想要什么风格？", "selected_options": [], "custom_input": ""}
        ]

        ch._pending_approvals["conv-1"] = {
            "request_id": "req-913",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["允许执行 bash 吗？"],
            "question_ids": {"call_abc": "允许执行 bash 吗？"},
        }
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": True,
            "answers": [],
        }, task="task-10"))
        m = captured[-1]
        assert m.params["answers"][0]["selected_options"] == ["拒绝"]
        assert m.params["answers"][0]["custom_input"] == "用户取消了选项卡"
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_e2a_style_answer_routes_resume(cfg_file):
    """E2A 风格应答（data 顶层 request_id/source/answers，桌面端 chat.send 同构）
    → interrupt resume。端侧复用桌面端报文结构经 A2A data part 回传。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-920",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["你想要什么风格？"],
            "question_ids": {},
        }
        await ch._handle_message_stream(_build_e2a_style_msg({
            "request_id": "req-920",
            "source": "ask_user_interrupt",
            "answers": [{
                "question": "你想要什么风格？",
                "selected_options": ["深色"],
                "custom_input": "夜间用",
            }],
        }, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-920"
        assert m.params["source"] == "ask_user_interrupt"
        # question 文本对齐 + snake_case 字段原样透传
        assert m.params["answers"] == [
            {
                "question": "你想要什么风格？",
                "selected_options": ["深色"],
                "custom_input": "夜间用",
            }
        ]
        assert "conv-1" not in ch._pending_approvals  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_invoke_rsp_agent_event_routes_resume(cfg_file):
    """云端多端同步回传形态：AgentEvent/InvokeRsp 挂 content.events（c.json
    实际形态）→ 递归定位 → question_id 对齐还原 → resume。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-940",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["这份周报的主题方向？", "风格偏好？"],
            "question_ids": {
                "call_b6bd1b87f9304aaba47358c5d57cc701": "这份周报的主题方向？",
                "call_b6bd1b87f9304aaba47358c5d57cc701#2": "风格偏好？",
            },
            "session_id": "top-1",
            "agent_id": "ag",
            "device_id": "dev",
            "push_id": "push",
        }
        # c.json 实际形态 + relay 规范化后的顶层 conversationId（真实帧
        # MSG_IN 日志 conv 有值，即到达 PC 时带顶层 conversationId）
        frame = {
            "method": "message/stream",
            "conversationId": "conv-1",
            "sessionId": "top-1",
            "session": {
                "sessionId": "top-1",
                "conversationId": "conv-1",
                "deviceId": "dev",
                "requestId": "task-9",
            },
            "content": {
                "events": [
                    {
                        "header": {"namespace": "AgentEvent", "name": "InvokeRsp"},
                        "payload": {
                            "action_type": "button_click",
                            "source": "ask_user_interrupt",
                            "user_cancel": False,
                            "success": True,
                            "answers": [
                                {
                                    "question_id": "call_b6bd1b87f9304aaba47358c5d57cc701",
                                    "selected_options": ["工作汇报/总结"],
                                    "custom_text": "",
                                },
                                {
                                    "question_id": "call_b6bd1b87f9304aaba47358c5d57cc701#2",
                                    "selected_options": ["科技深色渐变风"],
                                    "custom_text": "",
                                },
                            ],
                        },
                    }
                ]
            },
        }
        await ch._handle_message_stream(frame)
        m = captured[-1]
        assert m.params["request_id"] == "req-940"
        assert m.params["source"] == "ask_user_interrupt"
        # question_id 经待答表映射还原为 question 文本，多问题逐条透传
        assert m.params["answers"] == [
            {
                "question": "这份周报的主题方向？",
                "selected_options": ["工作汇报/总结"],
                "custom_input": "",
            },
            {
                "question": "风格偏好？",
                "selected_options": ["科技深色渐变风"],
                "custom_input": "",
            },
        ]
        assert "conv-1" not in ch._pending_approvals  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_timeout_default_answer(cfg_file):
    """超时兜底：ask_user 每题选第一个选项；权限类 fail-closed 拒绝。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-930",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time() - 3 * 60,
            "questions": ["你想要什么风格？", "要加图标吗？"],
            "question_ids": {},
            "question_options": {
                "你想要什么风格？": ["深色", "浅色"],
                "要加图标吗？": [],
            },
            "session_id": "top-1",
            "agent_id": "ag",
            "device_id": "dev",
            "push_id": "push",
        }
        await ch._resume_timed_out_ask_user("conv-1", ch._pending_approvals.pop("conv-1"))
        m = captured[-1]
        assert m.params["request_id"] == "req-930"
        assert m.params["source"] == "ask_user_interrupt"
        # 有选项的题选第一项；无选项的题给显式默认值"跳过"
        assert m.params["answers"] == [
            {"question": "你想要什么风格？", "selected_options": ["深色"], "custom_input": ""},
            {"question": "要加图标吗？", "selected_options": [], "custom_input": "跳过"},
        ]

        ch._pending_approvals["conv-1"] = {
            "request_id": "req-931",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time() - 3 * 60,
            "questions": ["允许执行 bash 吗？"],
            "question_ids": {},
            "question_options": {"允许执行 bash 吗？": ["本次允许", "拒绝"]},
            "session_id": "top-1",
            "agent_id": "ag",
            "device_id": "dev",
            "push_id": "push",
        }
        await ch._resume_timed_out_ask_user("conv-1", ch._pending_approvals.pop("conv-1"))
        m = captured[-1]
        # 权限类超时绝不默认放行：fail-closed 拒绝
        assert m.params["answers"][0]["selected_options"] == ["拒绝"]
        assert "超时未应答" in m.params["answers"][0]["custom_input"]
    finally:
        _cleanup_tasks(ch)


def test_build_answer_from_ask_user_empty_answers_returns_none():
    """边界守护：answers 为空列表时返回 None，不抛 IndexError。

    当前调用链（三形态解析 + user_cancel 合成）保证非空，此用例锁定
    防御性行为，防未来新调用方破坏不变式时崩溃。"""
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
        XiaoyiChannel,
    )

    assert (
        XiaoyiChannel._build_answer_from_ask_user(
            {"answers": [], "user_cancel": False},
            {"source": "ask_user_interrupt", "request_id": "req-x"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_ask_user_answer_evolution_labels_passthrough(cfg_file):
    """演进审批白名单分流：合法演进选项原样透传，不做 permission 系重映射。

    用户点「接收」必须放行（此前套用 permission 白名单 fail-closed 反转成
    拒绝）；「总是允许」不得映射成「永久记住」（interface.py 演进分支无此
    值，会被判 unknown 再次反转成拒绝）。命不中演进白名单 fail-closed 按拒绝。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 「接收」放行且原样透传
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-ev1",
            "source": "evolution_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "是否接收该技能演进？", "selectedOptions": ["接收"], "customInput": ""}],
            "evolution_interrupt",
            task="task-ev1",
        ))
        m = captured[-1]
        assert m.params["answers"][0]["selected_options"] == ["接收"]
        assert "conv-1" not in ch._pending_approvals  # 命中后消费

        # 「总是允许」原样透传（不重映射成「永久记住」）
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-ev2",
            "source": "evolution_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "q", "selectedOptions": ["总是允许"], "customInput": ""}],
            "evolution_interrupt",
            task="task-ev2",
        ))
        m = captured[-1]
        assert m.params["answers"][0]["selected_options"] == ["总是允许"]

        # 旧 source（skill_evolution_approval）同待遇；英文别名 accept 放行
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-ev3",
            "source": "skill_evolution_approval",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "q", "selectedOptions": ["accept"], "customInput": ""}],
            "skill_evolution_approval",
            task="task-ev3",
        ))
        m = captured[-1]
        assert m.params["answers"][0]["selected_options"] == ["accept"]

        # 命不中演进白名单（permission 系专有选项）：fail-closed 按拒绝
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-ev4",
            "source": "evolution_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{"question": "q", "selectedOptions": ["会话内记住"], "customInput": ""}],
            "evolution_interrupt",
            task="task-ev4",
        ))
        m = captured[-1]
        assert m.params["answers"][0]["selected_options"] == ["拒绝"]
        assert "会话内记住" in m.params["answers"][0]["custom_input"]
    finally:
        _cleanup_tasks(ch)


def test_peek_pending_approval_keeps_record_for_retry(cfg_file):
    """先读后删：peek 不消费待答记录（校验不过时文本回退/超时兜底仍可达）；
    过期记录就地清除。"""
    ch = _make_channel([], [])
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-peek",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
        }
        pending = ch._peek_pending_approval_for_answer("conv-1")
        assert pending is not None
        assert pending["request_id"] == "req-peek"
        assert "conv-1" in ch._pending_approvals  # 未消费

        # 过期记录：就地清除并返回 None（超时兜底不再重复处理）
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-stale",
            "source": "permission_interrupt",
            "task_id": "task-2",
            "created_at": time.time() - 4000,
        }
        assert ch._peek_pending_approval_for_answer("conv-1") is None
        assert "conv-1" not in ch._pending_approvals

        # 空表：None
        assert ch._peek_pending_approval_for_answer("conv-none") is None
    finally:
        _cleanup_tasks(ch)


def test_build_ask_user_command_payload_shape():
    """出站 ToolExecute/Invoke 指令 payload 形状（端侧契约）。

    question_id 供 InvokeRsp 回传对齐；multi_select/options[].recommended/
    text_input（含 placeholder）显式化；index 从 1 起保留给文本回退按序号匹配；
    不发 expiresAt 等契约外字段。
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
        _build_ask_user_command_payload,
    )

    command = _build_ask_user_command_payload({
        "source": "ask_user_interrupt",
        "request_id": "req-100",
        "questions": [{
            "question": "你想要什么风格？",
            "header": "配色",
            "multi_select": True,
            "options": [
                {"label": "深色", "description": "适合夜间使用", "recommended": True},
                {"label": "浅色"},
                {"label": "  "},  # 空 label 剔除
            ],
        }],
    })
    assert command["header"] == {"namespace": "ToolExecute", "name": "Invoke"}
    payload = command["payload"]
    assert payload["source"] == "ask_user_interrupt"
    assert "expiresAt" not in payload  # 契约外字段
    q = payload["questions"][0]
    assert q["question_id"] == "req-100"  # 无 tool_call_id → 事件 request_id
    assert q["question"] == "你想要什么风格？"
    assert "header" not in q  # 不下发短标签（对齐 PC 端，端侧无副标题）
    assert q["multi_select"] is True
    # 选项 = 序号 + label + recommended；不带 description 小字
    assert q["options"] == [
        {"index": 1, "label": "深色", "recommended": True},
        {"index": 2, "label": "浅色"},
    ]
    # 有选项 → 自由文本为选填补充
    assert q["text_input"] == {
        "enabled": True,
        "placeholder": "也可输入其他内容",
        "required": False,
    }

    # 权限类：tool 上下文透传（snake_case）；question_id 优先 tool_call_id
    command = _build_ask_user_command_payload({
        "source": "permission_interrupt",
        "request_id": "req-101",
        "questions": [{
            "question": "需要授权",
            "tool_call_id": "call_abc",
            "tool_name": "bash",
            "tool_args": {"command": "rm -rf ./build"},
            "options": [
                {"label": "本次允许", "description": "仅本次授权执行"},
                {"label": "拒绝"},
            ],
        }],
    })
    q = command["payload"]["questions"][0]
    assert q["question_id"] == "call_abc"
    assert q["tool_call_id"] == "call_abc"
    assert q["tool_name"] == "bash"
    assert q["tool_args"] == {"command": "rm -rf ./build"}
    assert q["multi_select"] is False
    assert q["options"][0] == {"index": 1, "label": "本次允许"}

    # 空选项 → text_input.required（只能自由作答）；同 id 多问题加 #序号 消歧
    command = _build_ask_user_command_payload({
        "source": "ask_user_interrupt",
        "request_id": "call_001",
        "questions": [
            {"question": "说说你的想法", "options": []},
            {"question": "还要补充吗", "options": []},
        ],
    })
    questions = command["payload"]["questions"]
    assert questions[0]["options"] == []
    assert questions[0]["text_input"] == {
        "enabled": True,
        "placeholder": "请输入内容",
        "required": True,
    }
    assert questions[0]["question_id"] == "call_001"
    assert questions[1]["question_id"] == "call_001#2"
