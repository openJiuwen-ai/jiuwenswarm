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

        # 审批提示：登记待答复 + status-update(input-required) 双 part 帧
        # （text part 人类可读 + data part AskUser 选项卡，final=false 不关闭气泡）
        assert ch._pending_approvals["conv-1"]["request_id"] == "req-777"
        prompt = sent[-1]
        assert prompt["msgType"] == "agent_response"
        inner = json.loads(prompt["msgDetail"])
        result = inner["result"]
        assert result["kind"] == "status-update"
        assert result["final"] is False
        assert result["status"]["state"] == "input-required"
        parts = result["status"]["message"]["parts"]
        text_part = parts[0]
        assert text_part["kind"] == "text"
        assert "需要您的确认" in text_part["text"]
        assert "权限审批: bash" in text_part["text"]
        assert "同意" in text_part["text"] and "拒绝" in text_part["text"]
        data_part = parts[1]
        assert data_part["kind"] == "data"
        command = data_part["data"]["commands"][0]
        assert command["header"] == {"namespace": "Common", "name": "AskUser"}
        ask_user = command["payload"]["askUser"]
        assert ask_user["source"] == "permission_interrupt"
        assert isinstance(ask_user["expiresAt"], int)
        q = ask_user["questions"][0]
        assert q["question"] == "允许执行 bash: zip ... 吗？"
        assert q["header"] == "权限审批: bash"
        assert q["multiSelect"] is False
        assert q["options"][0] == {"index": 1, "label": "本次允许"}
        assert [o["label"] for o in q["options"]] == [
            "本次允许", "会话内记住", "永久记住", "拒绝",
        ]

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


def test_build_ask_user_command_payload_shape():
    """出站 AskUser 指令 payload 形状（camelCase、index 从 1 起、空选项自由输入）。"""
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
        _build_ask_user_command_payload,
    )

    command = _build_ask_user_command_payload({
        "source": "ask_user_interrupt",
        "questions": [{
            "question": "你想要什么风格？",
            "header": "配色",
            "multi_select": True,
            "options": [
                {"label": "深色", "description": "适合夜间使用"},
                {"label": "浅色"},
                {"label": "  "},  # 空 label 剔除
            ],
        }],
    })
    assert command["header"] == {"namespace": "Common", "name": "AskUser"}
    ask_user = command["payload"]["askUser"]
    assert ask_user["source"] == "ask_user_interrupt"
    assert isinstance(ask_user["expiresAt"], int) and ask_user["expiresAt"] > 0
    q = ask_user["questions"][0]
    assert q["question"] == "你想要什么风格？"
    assert q["header"] == "配色"
    assert q["multiSelect"] is True
    assert q["options"] == [
        {"index": 1, "label": "深色", "description": "适合夜间使用"},
        {"index": 2, "label": "浅色"},
    ]

    # 权限类：tool 上下文透传（camelCase）
    command = _build_ask_user_command_payload({
        "source": "permission_interrupt",
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
    q = command["payload"]["askUser"]["questions"][0]
    assert q["toolCallId"] == "call_abc"
    assert q["toolName"] == "bash"
    assert q["toolArgs"] == {"command": "rm -rf ./build"}
    assert q["multiSelect"] is False
    assert q["options"][0] == {"index": 1, "label": "本次允许", "description": "仅本次授权执行"}

    # 空选项 = 自由文本输入
    command = _build_ask_user_command_payload({
        "source": "ask_user_interrupt",
        "questions": [{"question": "说说你的想法", "options": []}],
    })
    assert command["payload"]["askUser"]["questions"][0]["options"] == []
