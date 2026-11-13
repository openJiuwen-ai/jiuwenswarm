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
import re
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
    with_time_directive,
    with_workspace_directive,
)
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
    _normalize_approval_text,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.a2a_vars import (
    extract_model_input,
    normalize_model_input,
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
    # 默认按达到门限的手机端跑（app_ver=99.99.99.200，systemVariables 上报，
    # 门限见 ASK_USER_TABS_MIN_APP_VER——现网 11.7.x 均在门限之下）：
    # AskUser 结构化选项卡出站/应答路径开启；版本开关相关用例显式覆盖本表
    # （未达门限 / 未上报 → 文本回退，见 test_ask_user_prompt_falls_back_*）。
    ch._session_app_vers = {"conv-1": "99.99.99.200"}
    ch._session_model_names = {}
    ch._ws_connections = {"k": _FakeWs(sent)}
    ch._accumulated_texts = {}
    ch._send_locks = {}
    ch._reload_permissions_cb = None
    ch._status_update_interval = float(
        getattr(ch.config, "status_update_interval", 20) or 20
    )
    # beta3 新增状态（_handle_message_stream/_finalize 路径引用）
    ch._active_tasks = set()
    ch._answer_receipts = {}
    ch._streamed_chain_texts = {}
    ch._pending_resume_seeds = {}
    ch._delivered_tool_call_ids = {}
    ch._last_ask_user_chain = {}
    ch._card_time_chain_migrated = set()
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


def _build_stream_msg(text, client_vars=None, conv="conv-1", top="top-1", task="task-1", files=None, app_ver="99.99.99.200"):
    parts = []
    if client_vars is not None:
        parts.append({
            "kind": "data",
            "data": {"variables": {
                "clientVariables": client_vars,
                "systemVariables": {"push_id": "p1", "app_ver": app_ver},
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


# ------------------------------------------------------------------ a2a_vars：模型输入模态


def test_normalize_model_input_list_string_and_empty():
    assert normalize_model_input([" Text ", "text", "IMAGE", ""]) == ["text", "image"]
    # 单字符串形态（手机实测为 "" / 也兼容 "text,image" 扁平写法）
    assert normalize_model_input("text,image") == ["text", "image"]
    assert normalize_model_input("IMAGE") == ["image"]
    assert normalize_model_input("") is None
    assert normalize_model_input([]) is None
    assert normalize_model_input(None) is None
    assert normalize_model_input(["", "  ", 1, None]) is None


def test_extract_model_input_from_client_variables():
    def msg_with(client_vars):
        return [{
            "kind": "data",
            "data": {"variables": {"clientVariables": client_vars}},
        }]

    assert extract_model_input(msg_with({"modelInput": ["text", "image"]})) == ["text", "image"]
    assert extract_model_input(msg_with({"modelInput": "text,image"})) == ["text", "image"]
    # 空字符串 = 不声明（手机 Auto-Model 实测形态）
    assert extract_model_input(msg_with({"modelInput": ""})) is None
    assert extract_model_input(msg_with({"modelName": "Auto-Model"})) is None
    assert extract_model_input([{"kind": "text", "text": "hi"}]) is None
    assert extract_model_input(None) is None


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
async def test_message_stream_forwards_model_input(cfg_file, workspace):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 手机选中支持图片的模型：modelInput 随 chat.send params 透传给 AgentServer
        await ch._handle_message_stream(
            _build_stream_msg(
                "看看这张图",
                {"modelName": "DeepSeek_V4.1_Flash_VE", "modelInput": ["text", "image"]},
            )
        )
        m = captured[-1]
        assert m.params["model_name"] == "DeepSeek_V4.1_Flash_VE"
        assert m.params["model_input"] == ["text", "image"]

        # 扁平字符串形态（"text,image"）归一化后同样透传
        await ch._handle_message_stream(
            _build_stream_msg(
                "再看看",
                {"modelName": "DeepSeek_V4.1_Flash_VE", "modelInput": "text,image"},
                task="task-2",
            )
        )
        assert captured[-1].params["model_input"] == ["text", "image"]

        # 空 modelInput（手机 Auto-Model 实测形态）：不注入，保持旧行为
        await ch._handle_message_stream(
            _build_stream_msg("普通消息", {"modelName": "Auto-Model", "modelInput": ""}, task="task-3")
        )
        m = captured[-1]
        assert m.params["model_name"] == "Auto-Model"
        assert "model_input" not in m.params
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
        # append=true（partial 续写）：不重置端侧缓冲——卡片若重置会清掉
        # 已流出段1，且种子回补会使云端话题存储含段1 两份（PC 轮末重拉重复）
        assert result["append"] is True
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


def _make_ask_user_event(request_id: str):
    """构造 chat.ask_user_question 服务端事件（权限审批单问）。"""
    ask = type("M", (), {})()
    ask.event_type = EventType.CHAT_ASK_USER_QUESTION
    ask.payload = {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
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
    ask.id = request_id
    return ask


@pytest.mark.asyncio
async def test_ask_user_prompt_falls_back_to_text_for_old_client(cfg_file):
    """app_ver 未达门限（11.7.9.x 旧版）：出站回退纯文本
    审批提示（无选项卡 data part/ask_user_id）。

    登记精简待答复（无 question_ids/ask_user_tabs 标记）；流收尾 completed
    不降级（与已发旧端侧行为一致）；新契约结构化应答帧不消费、按普通消息处理。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._session_app_vers["conv-1"] = "11.7.9.200"
        ask = _make_ask_user_event("req-778")
        await ch._send_legacy(ask)

        pending = ch._pending_approvals["conv-1"]
        assert pending["request_id"] == "req-778"
        assert "question_ids" not in pending
        assert "ask_user_tabs" not in pending
        inner = json.loads(sent[-1]["msgDetail"])
        result = inner["result"]
        parts = result["artifact"]["parts"]
        assert len(parts) == 1  # 无选项卡 data part
        part = parts[0]
        assert part["kind"] == "text"
        assert "需要您的确认" in part["text"]
        assert "权限审批: bash" in part["text"]
        assert "同意" in part["text"] and "拒绝" in part["text"]
        assert result["final"] is False
        assert result["append"] is True  # 文本回退沿用旧形态 append=true
        assert result["lastChunk"] is True

        # 流收尾事件（is_processing=false）：completed 原样发出，不降级 working
        ch._session_active.add("top-1")
        proc = type("M", (), {})()
        proc.event_type = EventType.CHAT_PROCESSING_STATUS
        proc.payload = {"is_processing": False}
        proc.metadata = ask.metadata
        proc.session_id = "conv-1"
        proc.id = "req-778"
        await ch._send_legacy(proc)
        inner = json.loads(sent[-1]["msgDetail"])
        assert inner["result"]["status"]["state"] == "completed"

        # 新契约结构化应答：不消费、按普通消息处理（空帧拦截，无 resume 路由）
        n = len(captured)
        await ch._handle_message_stream(_build_ask_answer_msg(
            [{
                "question": "允许执行 bash: zip ... 吗？",
                "selectedOptions": ["本次允许"],
                "customInput": "",
            }],
            "permission_interrupt",
            task="task-9",
        ))
        assert len(captured) == n
        assert ch._pending_approvals["conv-1"]["request_id"] == "req-778"  # 待答复保留

        # 文本约定词表回复照常走 interrupt resume（旧路径兜底）
        await ch._handle_message_stream(_build_stream_msg("同意", None, task="task-10"))
        m = captured[-1]
        assert m.params["request_id"] == "req-778"
        assert m.params["answers"] == [{"selected_options": ["本次允许"], "custom_input": ""}]
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_prompt_fails_closed_without_app_ver(cfg_file):
    """app_ver 未上报：fail-closed 按不支持处理，回退文本提示。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._session_app_vers.pop("conv-1", None)
        await ch._send_legacy(_make_ask_user_event("req-779"))

        assert ch._pending_approvals["conv-1"]["request_id"] == "req-779"
        inner = json.loads(sent[-1]["msgDetail"])
        parts = inner["result"]["artifact"]["parts"]
        assert len(parts) == 1
        assert parts[0]["kind"] == "text"
        assert "需要您的确认" in parts[0]["text"]
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_app_ver_extracted_from_system_variables(cfg_file):
    """入站 systemVariables.app_ver 按逻辑会话记录并随 metadata 透传。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._session_app_vers.pop("conv-1", None)
        await ch._handle_message_stream(_build_stream_msg(
            "你好",
            client_vars={"workspace": None},
            task="task-11",
            app_ver="99.99.99.200",
        ))
        assert ch._session_app_vers["conv-1"] == "99.99.99.200"
        m = captured[-1]
        assert m.metadata["xiaoyi_app_ver"] == "99.99.99.200"
        assert ch._ask_user_tabs_enabled("conv-1") is True
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_resume_carries_card_round_model(cfg_file):
    """发卡轮模型快照进 pending，答卡 resume 随请求带回（续跑沿用发卡轮模型）。

    手机端模型选择随消息 clientVariables.modelName 上报：会话级记录最新值，
    ask_user 发卡登记时快照进待答表（答卡期间切换模型不影响本轮），resume
    请求（含超时兜底续跑）带回 model_name——服务端每轮按 params.model_name
    覆盖出站模型，缺失才回落默认模型。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 用户消息携带 clientVariables.modelName → 会话级最新模型记录
        await ch._handle_message_stream(_build_stream_msg(
            "用askuser询问我选择一个颜色",
            client_vars={"workspace": None, "modelName": "LLM_GLM5.3"},
            task="task-1",
        ))
        assert ch._session_model_names["conv-1"] == "LLM_GLM5.3"

        ask = _make_ask_user_event("req-779")
        await ch._send_legacy(ask)
        # 发卡轮快照：pending 记录当轮模型
        assert ch._pending_approvals["conv-1"]["model_name"] == "LLM_GLM5.3"

        # 答卡（InvokeRsp，question_id 对齐 request_id）
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "req-779",
                "selected_options": ["本次允许"],
                "custom_text": None,
            }],
        }, task="task-9"))
        m = captured[-1]
        assert m.params["request_id"] == "req-779"
        # 续跑沿用发卡轮的模型，不回落默认模型
        assert m.params["model_name"] == "LLM_GLM5.3"
        assert "conv-1" not in ch._pending_approvals  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_session_model_name_record_lifecycle(cfg_file):
    """会话级模型记录生命周期：上报更新、未携带保持、显式置空清除、清理路径收口。

    端侧契约允许 modelName="none"/空 表示回到默认模型——显式置空须清除
    会话记录（否则发卡快照到已弃用的旧模型）；未携带则保持上次选择不动。
    清上下文/强清会话同步收口该表。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 上报 modelName → 记录更新
        await ch._handle_message_stream(_build_stream_msg(
            "你好",
            client_vars={"workspace": None, "modelName": "LLM_GLM5.3"},
            task="task-1",
        ))
        assert ch._session_model_names["conv-1"] == "LLM_GLM5.3"

        # 未携带 modelName → 记录保持（不动上次选择）
        await ch._handle_message_stream(_build_stream_msg(
            "再见",
            client_vars={"workspace": None},
            task="task-2",
        ))
        assert ch._session_model_names["conv-1"] == "LLM_GLM5.3"

        # 显式置空（none，端侧回到默认模型）→ 记录清除
        await ch._handle_message_stream(_build_stream_msg(
            "你好",
            client_vars={"workspace": None, "modelName": "none"},
            task="task-3",
        ))
        assert "conv-1" not in ch._session_model_names

        # 清空上下文 → 记录收口（清前重设一次，区分两条清理路径）
        ch._session_model_names["conv-1"] = "MiniMax_M3.1_Flash"
        await ch._handle_clear_context({"sessionId": "conv-1", "conversationId": "conv-1"})
        assert "conv-1" not in ch._session_model_names

        ch._session_model_names["conv-1"] = "MiniMax_M3.1_Flash"
        ch._force_cleanup_session("conv-1")
        assert "conv-1" not in ch._session_model_names
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_timeout_loop_skips_text_fallback_entries(
    cfg_file, monkeypatch
):
    """超时兜底循环仅处理选项卡路径登记（ask_user_tabs 标记）。

    文本回退条目（旧版手机端）到期不自动续跑，保持既有"到期过期、迟到
    文本按普通消息处理"语义。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    resumed: list[tuple[str, str]] = []
    ch._running = True

    async def _fake_resume(logical_session, pending):
        resumed.append((logical_session, pending["request_id"]))
        ch._pending_approvals.pop(logical_session, None)

    monkeypatch.setattr(ch, "_resume_timed_out_ask_user", _fake_resume)
    ch._pending_approvals["conv-1"] = {
        "request_id": "req-940",
        "source": "permission_interrupt",
        "task_id": "task-2",
        "created_at": time.time() - 31 * 60,
    }
    ch._pending_approvals["conv-2"] = {
        "request_id": "req-941",
        "source": "permission_interrupt",
        "task_id": "task-3",
        "created_at": time.time() - 31 * 60,
        "ask_user_tabs": True,
    }
    sleeps = {"n": 0}

    async def _fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    try:
        await ch._ask_user_timeout_loop()
    except asyncio.CancelledError:
        pass
    # 仅选项卡条目自动续跑；文本回退条目保留到期自然过期
    assert resumed == [("conv-2", "req-941")]
    assert ch._pending_approvals["conv-1"]["request_id"] == "req-940"
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
            "model_name": "LLM_GLM5.3",
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
        # 发卡轮模型快照随 resume 带回：服务端每轮按 params.model_name 覆盖
        assert m.params["model_name"] == "LLM_GLM5.3"
        # 主投跟随应答帧带回的 task_id（task-9）：云端在 InvokeRsp 后只对
        # 应答帧链路做下发（实测挂原链手机完全收不到）；原任务链 task-2
        # 记入收口账本，resume 终态时向其补发收口终帧
        assert m.params["task_id"] == "task-9"
        assert m.metadata["xiaoyi_task_id"] == "task-9"
        assert "conv-1" not in ch._pending_approvals  # 已消费
        assert ch._answer_receipts.get(("conv-1", "task-9")) == "task-2"
        # resume 轮登记为会话活跃任务（跟随的 task_id 未曾在用户消息路径
        # 登记过）：续跑流收尾的 processing_status(false) 才能通过
        # _is_session_active 检查，completed+final=true 终帧正常下发
        assert ("conv-1", "task-9") in ch._active_tasks
        assert ch._latest_platform_tasks.get("conv-1") == "task-9"
        assert ch._session_task_map.get("task-9") == "conv-1"

        # 续跑流收尾：processing_status(is_processing=false) 须放行并下发
        # completed+final=true 终帧（漏登记时被 _is_session_active 误跳过，
        # 手机气泡永远等不到收尾，表现为"回答后卡住"），并向原任务链
        # （task-2）补发收口终帧，尽力闭环原链上的任务气泡
        proc = type("M", (), {})()
        proc.event_type = EventType.CHAT_PROCESSING_STATUS
        proc.payload = {"is_processing": False}
        proc.metadata = {
            "xiaoyi_session_id": "conv-1",
            "xiaoyi_task_id": "task-9",
            "xiaoyi_conversation_id": "conv-1",
        }
        proc.session_id = "conv-1"
        proc.id = "task-9"
        await ch._send_legacy(proc)
        main = json.loads(sent[-2]["msgDetail"])
        receipt = json.loads(sent[-1]["msgDetail"])
        assert main["result"]["kind"] == "status-update"
        assert main["result"]["taskId"] == "task-9"
        assert main["result"]["status"]["state"] == "completed"
        assert main["result"]["final"] is True
        # 原链收口帧：同态同 final，落在待答表登记的 task-2 链路
        assert receipt["result"]["taskId"] == "task-2"
        assert receipt["result"]["status"]["state"] == "completed"
        assert receipt["result"]["final"] is True
        assert ("conv-1", "task-9") not in ch._answer_receipts  # 已消费
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_resume_seeds_streamed_text_to_follow_chain(cfg_file):
    """播种 v3：格式对齐（含扣片尾片+段间换行），双形态，随首正文帧 partial。

    确认后端侧/云端以 streamType=start 重开消息流（缓冲重置），确认前已
    流出的正文会丢。播种=累积+扣片尾片+
（格式与旧链路 tool_phase
    flush 对齐：段完整、续写另起一行），挂起后随确认后首个正文事件以
    append=true（partial）帧发出。仅链迁移形态播种（新链存储/缓冲从零，
    种子是段1 第一份拷贝不重复）；同链不播种——云端话题存储按任务拼接
    全部 text part，同链重发使存储含段1 两份，轮末重拉渲染到 PC 重复。
    种子吸收源链扣片后，flush/续增量不再重发尾片。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 段1（未收尾）：累积器=已发帧拼接，最后一片扣在 pending
        acc = "第一段。\n第二段已发"
        held = "收尾"
        seg1 = acc + held + "\n"  # 播种期望：累积+尾片+换行
        ch._streamed_chain_texts[("conv-1", "task-2")] = acc
        ch._text_stream_pending[("conv-1", "task-2")] = (held, "text")
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-960",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["问1？"],
            "question_ids": {"call_960": "问1？"},
        }
        # 形态一：链迁移（手机端点选，应答帧带回新 rpc id task-9）
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "call_960",
                "selected_options": ["选项A"],
                "custom_text": None,
            }],
        }, task="task-9"))
        # 播种挂起（不立即发帧）；尾片已被吸收；新链累积=种子；原链清理
        assert ch._pending_resume_seeds.get(("conv-1", "task-9")) == seg1
        # 链式累积：新链累积器=种子（B+C），续写 D 追加其上
        assert ch._streamed_chain_texts.get(("conv-1", "task-9")) == seg1
        assert ("conv-1", "task-2") not in ch._streamed_chain_texts
        assert ("conv-1", "task-2") not in ch._text_stream_pending
        # 未随应答立即发正文帧——仅"已收到您的回复"回执状态帧
        assert all(
            json.loads(s["msgDetail"])["result"]["kind"] == "status-update"
            for s in sent
        )

        # 续跑首个正文 delta：种子以 partial 帧发出（格式对齐后完整段1）
        delta = type("M", (), {})()
        delta.event_type = EventType.CHAT_DELTA
        delta.payload = {"content": "你的", "is_complete": False}
        delta.metadata = {
            "xiaoyi_session_id": "conv-1",
            "xiaoyi_task_id": "task-9",
            "xiaoyi_conversation_id": "conv-1",
        }
        delta.session_id = "conv-1"
        delta.id = "task-9"
        await ch._send_legacy(delta)
        seed = json.loads(sent[1]["msgDetail"])  # sent[0]=回执状态帧
        assert seed["result"]["taskId"] == "task-9"
        assert seed["result"]["append"] is True
        assert seed["result"]["lastChunk"] is False
        assert seed["result"]["artifact"]["parts"][0]["text"] == seg1
        # 后续续写正常追加到新链累积（扣片：首片扣住未发）
        delta.payload = {"content": "选择已收到。", "is_complete": False}
        await ch._send_legacy(delta)
        assert ch._streamed_chain_texts.get(("conv-1", "task-9")) == seg1 + "你的"

        # 链式 ask_user：第二张卡挂 task-9 链，第二次应答播种=段1+续写
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-961",
            "source": "ask_user_interrupt",
            "task_id": "task-9",
            "created_at": time.time(),
            "questions": ["问2？"],
            "question_ids": {"call_961": "问2？"},
        }
        sent.clear()
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "call_961",
                "selected_options": ["选项B"],
                "custom_text": None,
            }],
        }, task="task-10"))
        # 第二次播种=链式累积（B+C+\n：段1+续写，含此时扣住的"选择已收到。"）
        seg2 = seg1 + "你的选择已收到。\n"
        assert ch._pending_resume_seeds.get(("conv-1", "task-10")) == seg2
        assert ch._streamed_chain_texts.get(("conv-1", "task-10")) == seg2
        assert ("conv-1", "task-9") not in ch._streamed_chain_texts

        # 形态二：同链（PC 端点选，应答帧带回原任务 id）——不播种：
        # 云端话题存储按任务拼接全部 text part，原链已含段1（实时 partial
        # 入存储），种子重发会使存储含段1 两份，轮末重拉渲染到 PC 重复
        ch._streamed_chain_texts[("conv-1", "task-2")] = acc
        ch._text_stream_pending[("conv-1", "task-2")] = (held, "text")
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-962",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["问3？"],
            "question_ids": {"call_962": "问3？"},
        }
        sent.clear()
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "call_962",
                "selected_options": ["确认"],
                "custom_text": None,
            }],
        }, task=""))
        # 同链：无种子，累积器保留（续写在其上追加）
        assert ch._pending_resume_seeds.get(("conv-1", "task-2")) is None
        assert ch._streamed_chain_texts.get(("conv-1", "task-2")) == acc
        # 同链无种子：扣片尾片留在 pending，随续写首增量/收尾 flush 流出
        assert ("conv-1", "task-2") in ch._text_stream_pending
        # 确认后首个正文事件=tool_phase flush：尾片补换行发出（同链收尾语义）
        await ch._flush_text_stream_segment(
            "conv-1", "task-2", ("conv-1", "task-2"), reason="tool_phase"
        )
        tail_frame = json.loads(sent[-1]["msgDetail"])
        assert tail_frame["result"]["taskId"] == "task-2"
        assert tail_frame["result"]["append"] is True
        assert tail_frame["result"]["artifact"]["parts"][0]["text"] == held + "\n"
        assert ("conv-1", "task-2") not in ch._pending_resume_seeds
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_ask_user_answer_follow_delivery_and_lifecycle(cfg_file):
    """跟随应答帧链路主投 + 原链收口：生命周期随键迁移完整收口。

    发卡轮 task-2 经普通消息路径登记进 _active_tasks 并挂 20s 周期保活；
    应答帧带回 task-9，主投跟随（云端 InvokeRsp 后只对答链下发，实测挂
    原链手机完全收不到）。键迁移时原键收口（活跃表/保活/超时撤销），
    终态时主投 completed+final=true 落 task-9、原链 task-2 补发收口终帧
    ——否则旧键残留的保活每 20s 上报 working「任务正在处理中」，端云按
    已结束链路回 11115 Connection closed，端侧气泡永远停在"准备任务"。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 复现真实状态：发卡轮已登记活跃 + 周期保活在跑 + 最近平台任务指向它
        ch._active_tasks.add(("conv-1", "task-2"))
        ch._latest_platform_tasks["conv-1"] = "task-2"
        keepalive = asyncio.create_task(
            ch._periodic_status_keepalive("conv-1", "task-2")
        )
        ch._session_timeout_tasks[("conv-1", "task-2")] = keepalive
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-950",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["单弹窗调试：你能看到这个带两个选项的问题卡片吗？"],
            "question_ids": {
                "call_950": "单弹窗调试：你能看到这个带两个选项的问题卡片吗？"
            },
        }
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "call_950",
                "selected_options": ["选项B"],
                "custom_text": None,
            }],
        }, task="task-9"))
        # 键迁移：主投 task-9 登记活跃，原键 task-2 收口（保活撤销），
        # 收口账本登记原链
        m = captured[-1]
        assert m.params["task_id"] == "task-9"
        assert m.metadata["xiaoyi_task_id"] == "task-9"
        assert ch._answer_receipts.get(("conv-1", "task-9")) == "task-2"
        assert ("conv-1", "task-9") in ch._active_tasks
        assert ch._latest_platform_tasks.get("conv-1") == "task-9"
        assert ch._session_task_map.get("task-9") == "conv-1"
        assert ("conv-1", "task-2") not in ch._active_tasks
        assert ("conv-1", "task-2") not in ch._session_timeout_tasks
        assert ("conv-1", "task-2") not in ch._task_timeout_tasks
        await asyncio.gather(keepalive, return_exceptions=True)
        assert keepalive.done()  # 旧键保活已终止，不再上报 working

        # 续跑终帧：主投 completed+final=true 落 task-9（手机内容链收口），
        # 原链 task-2 补发收口终帧（尽力闭环手机/桌面原链气泡）
        proc = type("M", (), {})()
        proc.event_type = EventType.CHAT_PROCESSING_STATUS
        proc.payload = {"is_processing": False}
        proc.metadata = {
            "xiaoyi_session_id": "conv-1",
            "xiaoyi_task_id": "task-9",
            "xiaoyi_conversation_id": "conv-1",
        }
        proc.session_id = "conv-1"
        proc.id = "task-9"
        await ch._send_legacy(proc)
        main = json.loads(sent[-2]["msgDetail"])
        receipt = json.loads(sent[-1]["msgDetail"])
        assert main["result"]["taskId"] == "task-9"
        assert main["result"]["status"]["state"] == "completed"
        assert main["result"]["final"] is True
        assert receipt["result"]["taskId"] == "task-2"
        assert receipt["result"]["status"]["state"] == "completed"
        assert receipt["result"]["final"] is True
        assert ("conv-1", "task-9") not in ch._answer_receipts  # 已消费
        assert ("conv-1", "task-9") not in ch._active_tasks
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_periodic_keepalive_breaks_when_newer_task_takes_over():
    """保活翻篇守护：更新的平台任务接管会话后，旧任务保活必须停止上报。

    防御性兜底：任何未来路径若再制造旧键残留（在 _active_tasks 未撤销），
    保活也必须在 _latest_platform_tasks 翻篇后停发，不再产生僵尸 working。
    """
    sent_frames: list = []

    async def _fake_send(task_id, session_id, text, *args, **kwargs):
        sent_frames.append((task_id, text))

    ch = _make_channel([], [])
    ch._status_update_interval = 0.01
    ch._send_status_update = _fake_send
    # 旧键仍在活跃表（未收口），但最近平台任务已翻篇到 task-9
    ch._active_tasks.add(("conv-1", "task-2"))
    ch._latest_platform_tasks["conv-1"] = "task-9"
    keepalive = asyncio.create_task(
        ch._periodic_status_keepalive("conv-1", "task-2")
    )
    await asyncio.sleep(0.05)
    assert keepalive.done()  # 翻篇即退出，尽管旧键仍在 _active_tasks
    assert sent_frames == []  # 一条僵尸 working 都不发

    # 对照：未翻篇时正常周期上报
    ch._active_tasks.add(("conv-1", "task-9"))
    ch._latest_platform_tasks["conv-1"] = "task-9"
    keepalive2 = asyncio.create_task(
        ch._periodic_status_keepalive("conv-1", "task-9")
    )
    try:
        await asyncio.sleep(0.05)
        assert not keepalive2.done()
        assert sent_frames and sent_frames[0][0] == "task-9"
    finally:
        keepalive2.cancel()
        await asyncio.gather(keepalive2, return_exceptions=True)


@pytest.mark.asyncio
async def test_ask_user_invoke_rsp_task_id_fallback_to_pending(cfg_file):
    """InvokeRsp 帧未带回 task_id（params.id 空）→ 回退待答表登记 task_id。"""
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        ch._pending_approvals["conv-1"] = {
            "request_id": "req-911",
            "source": "ask_user_interrupt",
            "task_id": "task-2",
            "created_at": time.time(),
            "questions": ["选一个方向？"],
            "question_ids": {"call_def": "选一个方向？"},
        }
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "call_def",
                "selected_options": ["方向A"],
                "custom_text": None,
            }],
        }, task=""))
        m = captured[-1]
        assert m.params["request_id"] == "req-911"
        assert m.params["task_id"] == "task-2"
        assert m.metadata["xiaoyi_task_id"] == "task-2"
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
        # 云端回传帧无 params.id（task_id 空）→ 回退待答表登记 task_id
        assert m.params["task_id"] == "task-2"
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


@pytest.mark.asyncio
async def test_tool_event_replay_dedup_on_resume(cfg_file):
    """中断恢复重执行的工具事件按 tool_call_id 去重（repeat.md 建议二）。

    恢复会重新执行被中断的工具并重发 tool_call/update 事件；端侧按 id
    配对渲染工具卡，重发会产生第二张"已完成"卡——网关按会话记录已下发
    id，重发整帧抑制；tool_result 是新事件照常放行，把原卡补成已完成态。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        def _tool_msg(event_type, payload):
            m = type("M", (), {})()
            m.event_type = event_type
            m.payload = payload
            m.metadata = {
                "xiaoyi_session_id": "conv-1",
                "xiaoyi_task_id": "task-2",
                "xiaoyi_conversation_id": "conv-1",
            }
            m.session_id = "conv-1"
            m.id = "task-2"
            return m

        def _tool_parts(frame):
            result = frame["result"]
            return (
                (result.get("status", {}).get("message", {}).get("parts"))
                or []
            )

        call_payload = {
            "tool_call": {"tool_call_id": "call_dup", "name": "ask_user",
                          "arguments": "{}"},
            "is_processing": True,
        }
        await ch._send_legacy(_tool_msg(EventType.CHAT_TOOL_CALL, call_payload))
        first_parts = [
            p for f in (json.loads(s["msgDetail"]) for s in sent)
            for p in _tool_parts(f)
            if p.get("kind") == "data"
        ]
        assert any(
            p["data"].get("toolCall", {}).get("id") == "call_dup"
            for p in first_parts
        )

        # 恢复重发同名 tool_call / tool_update：整帧抑制
        sent.clear()
        await ch._send_legacy(_tool_msg(EventType.CHAT_TOOL_CALL, call_payload))
        await ch._send_legacy(_tool_msg(EventType.CHAT_TOOL_UPDATE, {
            "tool_call_id": "call_dup", "tool_name": "ask_user",
        }))
        assert sent == []

        # tool_result 是新事件：照常放行（把原卡补成已完成态）
        await ch._send_legacy(_tool_msg(EventType.CHAT_TOOL_RESULT, {
            "tool_call_id": "call_dup", "tool_name": "ask_user",
            "result": "问1？: 选项A",
        }))
        assert sent, "tool_result must pass through"
        result_parts = [
            p for f in (json.loads(s["msgDetail"]) for s in sent)
            for p in _tool_parts(f)
            if p.get("kind") == "data"
        ]
        assert any(
            p["data"].get("toolCall", {}).get("id") == "call_dup"
            and p["data"].get("toolCall", {}).get("status") == "completed"
            for p in result_parts
        )
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_chained_card_time_chain_migration_seeds_new_chain(cfg_file):
    """链式 ask_user 卡片期链迁移：前链累积并进新链，应答时随种子补齐。

    链式后续卡片可能被云端开到新交互链（卡片期迁移，应答帧与待答表同
    链，应答期迁移判定探测不到）——新链从零累积，非发起端还可能整段收
    不到新链正文。卡片登记时把前链累积并入新链并打迁移标记，应答期按
    标记播种，随确认后首个正文事件 partial 补进新链消息。
    """
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        # 第一卡在链 task-a：累积含第一问前正文与确认后续写
        # （累积器键与卡片/应答桥的 session 取值一致）
        ch._streamed_chain_texts[("conv-1", "task-a")] = (
            "第一问正文。你选的是樱花粉。\n"
        )
        ch._last_ask_user_chain["conv-1"] = "task-a"

        ask = type("M", (), {})()
        ask.event_type = EventType.CHAT_ASK_USER_QUESTION
        ask.payload = {
            "event_type": "chat.ask_user_question",
            "request_id": "req-2nd",
            "source": "ask_user_interrupt",
            "questions": [{
                "question": "第二个问题？",
                "options": [{"label": "选项A"}, {"label": "选项B"}],
            }],
        }
        ask.metadata = {
            "xiaoyi_session_id": "conv-1",
            "xiaoyi_task_id": "task-b",
            "xiaoyi_conversation_id": "conv-1",
        }
        ask.session_id = "conv-1"
        ask.id = "req-2nd"
        await ch._send_legacy(ask)

        # 卡片期迁移：前链累积并入新链，迁移标记登记
        assert ch._streamed_chain_texts.get(("conv-1", "task-b")) == (
            "第一问正文。你选的是樱花粉。\n"
        )
        assert ("conv-1", "task-a") not in ch._streamed_chain_texts
        assert "conv-1" in ch._card_time_chain_migrated

        # 应答二（应答帧与待答表同链 task-b）：按卡片期迁移标记播种
        await ch._handle_message_stream(_build_invoke_rsp_msg({
            "user_cancel": False,
            "answers": [{
                "question_id": "req-2nd",
                "selected_options": ["选项A"],
                "custom_text": None,
            }],
        }, task="task-b"))
        seed = ch._pending_resume_seeds.get(("conv-1", "task-b"))
        assert seed == "第一问正文。你选的是樱花粉。\n"
        # 链跟踪归一到本轮实际链路
        assert ch._last_ask_user_chain.get("conv-1") == "task-b"
    finally:
        _cleanup_tasks(ch)
