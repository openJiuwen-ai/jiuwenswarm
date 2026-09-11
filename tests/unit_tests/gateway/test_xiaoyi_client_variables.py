# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""xiaoyi 渠道 clientVariables（workspace/permission）+ 权限审批桥接单测。

覆盖：
  - data part variables.clientVariables 解析（workspace 落 project_dir/cwd/trusted_dirs，
    permission 落 config.yaml permissions 段并触发热重载回调）
  - 空消息守卫（仅 variables/events 的帧不触发任务运行）
  - chat.ask_user_question → 手机端审批提示渲染 + 待答复登记
  - 用户回复（文本约定 / PermissionReply 事件）→ interrupt resume 路由参数
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
import yaml

import jiuwenswarm.common.config as cfgmod
from jiuwenswarm.common.permission_profile import (
    PERMISSION_PROFILE_FULL_ACCESS,
    normalize_permission_profile,
    permission_profile_config_patch,
    resolve_client_workspace,
    resolve_trusted_dirs,
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
    ch._data_event_handlers = {}
    ch._gui_agent_handlers = []
    ch._device_command_locks = {}
    ch.push_id = ""
    return ch


def _build_stream_msg(text, client_vars=None, conv="conv-1", top="top-1", task="task-1"):
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
    # full_access：也注入（位置提示，不含约束措辞）——否则该档下模型对工作空间零感知
    full = with_workspace_directive("你好", str(workspace), "full_access")
    assert full.startswith("你好\n\n") and "<claw_workspace>" in full
    assert "必须落在该目录" not in full
    assert with_workspace_directive("你好", "", "default") == "你好"


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
    try:
        # full_access：project_dir/cwd 下发、无 trusted_dirs、注入位置提示（非约束）、护栏关闭
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
        assert cfgmod.load_yaml_round_trip(cfg_file)["permissions"]["enabled"] is False

        # default：trusted_dirs + 工作空间指令 + strict 护栏
        await ch._handle_message_stream(
            _build_stream_msg("再打一个", {"workspace": str(workspace), "permission": "default"}, task="task-2")
        )
        m = captured[-1]
        assert m.params["trusted_dirs"] == [os.path.abspath(str(workspace))]
        assert m.params["query"].startswith("再打一个\n\n")
        assert "<claw_workspace>" in m.params["query"]
        data = cfgmod.load_yaml_round_trip(cfg_file)
        assert data["permissions"]["enabled"] is True
        assert data["permissions"]["permission_mode"] == "strict"

        # 不携带 clientVariables：行为不变
        await ch._handle_message_stream(_build_stream_msg("普通消息", None, task="task-3"))
        m = captured[-1]
        assert "project_dir" not in m.params and m.params["query"] == "普通消息"

        # 空消息（仅 variables）：不触发路由
        n = len(captured)
        await ch._handle_message_stream(_build_stream_msg("", {"workspace": str(workspace)}, task="task-4"))
        assert len(captured) == n
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_permission_profile_triggers_reload_callback(cfg_file):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    reloads = []
    ch._reload_permissions_cb = lambda: reloads.append(True) or asyncio.sleep(0)
    try:
        await ch._handle_message_stream(_build_stream_msg("干活", {"permission": "full_access"}))
        assert len(reloads) == 1  # 配置变更 → 触发热重载
        await ch._handle_message_stream(_build_stream_msg("继续", {"permission": "full_access"}, task="task-2"))
        assert len(reloads) == 1  # 配置无变更 → 不重复 reload
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
            "xiaoyi_session_id": "top-1",
            "xiaoyi_task_id": "task-2",
            "xiaoyi_conversation_id": "conv-1",
        }
        ask.session_id = "conv-1"
        ask.id = "req-777"
        await ch._send_legacy(ask)

        # 审批提示：登记待答复 + 文本提示（完整文本块、非 final，不关闭气泡）
        assert ch._pending_approvals["conv-1"]["request_id"] == "req-777"
        prompt = sent[-1]
        assert prompt["msgType"] == "agent_response"
        inner = json.loads(prompt["msgDetail"])
        part = inner["result"]["artifact"]["parts"][0]
        assert part["kind"] == "text"
        assert "需要您的确认" in part["text"]
        assert "权限审批: bash" in part["text"]
        assert "同意" in part["text"] and "拒绝" in part["text"]
        assert inner["result"]["final"] is False
        assert inner["result"]["lastChunk"] is True

        # 用户回复「同意」→ interrupt resume 路由（回原始任务气泡）
        await ch._handle_message_stream(_build_stream_msg("同意", None, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-777"
        assert m.params["answers"] == [{"selected_options": ["本次允许"], "custom_input": ""}]
        assert m.params["source"] == "permission_interrupt"
        assert m.params["query"] == "" and m.params["mode"] == "agent"
        assert m.params["task_id"] == "task-2"
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
