# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``e2a.message``。

仅保留 ``jiuwenswarm.common.schema.message`` 旧 import 路径兼容，re-export 协议包
的同一对象——``Message`` / ``ReqMethod`` / ``EventType`` / ``Mode`` 及线协议事件名
常量。``ReqMethod`` 的成员扩展（``RECOMMENDATION`` / ``TRAJECTORY_*``）现由协议包
直接提供；仅协议包当前 pin 尚未发布的成员（``SESSION_ASSETS_REMOVE``）保留最小
运行时补齐，避免两仓各维护一份枚举类。
"""

from __future__ import annotations

from enum import Enum

from e2a.message import (  # noqa: F401
    PROMPT_SUGGESTION_READY_EVENT,
    SESSION_REQUEST_COMPLETED_EVENT,
    TEAM_ATTACH_ONLY_PARAM,
    TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
    TOPIC_STARTERS_READY_EVENT,
    EventType,
    Message,
    Mode,
    ReqMethod,
)


def _ensure_protocol_member(enum_cls: type[Enum], name: str, value: str) -> None:
    """Register a member of a protocol enum the pinned protocol package does not ship yet.

    ``openjiuwen-protocol`` owns the enum; 这里只为协议包尚未发布的成员开一个缺口，
    待协议包提供同名同值成员后本函数与调用一并删除。
    """
    existing = enum_cls.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"{enum_cls.__name__}.{name} is {existing.value!r}, expected {value!r}"
            )
        return
    if any(item.value == value for item in enum_cls):
        raise RuntimeError(f"{enum_cls.__name__} value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(enum_cls)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(enum_cls, name, member)
    enum_cls._member_map_[name] = member  # pylint: disable=protected-access
    enum_cls._value2member_map_[value] = member  # pylint: disable=protected-access
    enum_cls._member_names_.append(name)  # pylint: disable=protected-access


def _ensure_req_method(name: str, value: str) -> None:
    _ensure_protocol_member(ReqMethod, name, value)


# Published release methods required by JiuwenMate and its existing callers.
_ensure_req_method("JIUWENMATE_FEISHU_INBOUND", "jiuwenmate.feishu.inbound")
_ensure_req_method("JIUWENMATE_DINGTALK_INBOUND", "jiuwenmate.dingtalk.inbound")
_ensure_req_method("JIUWENMATE_DINGTALK_BINDING_CHECK", "jiuwenmate.dingtalk.binding_check")
_ensure_req_method("JIUWENMATE_WELINK_INBOUND", "jiuwenmate.welink.inbound")
_ensure_req_method("JIUWENMATE_CONNECTION_ATTACH", "jiuwenmate.connection.attach")
_ensure_req_method("JIUWENMATE_COLLEAGUE_CONNECTIONS", "jiuwenmate.colleague.connections")
_ensure_req_method("JIUWENMATE_COLLEAGUE_REMOVE", "jiuwenmate.colleague.remove")
_ensure_req_method("JIUWENMATE_FEISHU_PREPARE", "jiuwenmate.feishu.prepare")
_ensure_req_method("JIUWENMATE_DELIVERY_ACK", "jiuwenmate.delivery.ack")
_ensure_req_method("JIUWENMATE_THINKING_NOTICE_ACK", "jiuwenmate.thinking_notice.ack")
_ensure_req_method("JIUWENMATE_GROUP_RESPONSE", "jiuwenmate.group.response")
_ensure_req_method("GROUP_EXECUTION_LIST", "group.execution.list")
_ensure_req_method("GROUP_EXECUTION_GET", "group.execution.get")
_ensure_req_method("GROUP_FILE_RENAME", "group.file.rename")
_ensure_req_method("GROUP_FILE_DELETE", "group.file.delete")
_ensure_req_method("GROUP_HISTORY_CLEAR", "group.history.clear")
_ensure_req_method("MODELS_REFRESH_STATUS", "models.refresh_status")
_ensure_req_method("VOICE_TASK_PENDING_INTERRUPT", "voice.task.pending_interrupt")
_ensure_req_method("VOICE_SESSION_RECEIPTS", "voice.session.receipts")
_ensure_req_method("JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT", "jiuwenmate.colleague.ensure_default")
_ensure_req_method("JIUWENMATE_COLLEAGUE_LIST", "jiuwenmate.colleague.list")
_ensure_req_method("JIUWENMATE_COLLEAGUE_GET", "jiuwenmate.colleague.get")
_ensure_req_method("JIUWENMATE_COLLEAGUE_CREATE", "jiuwenmate.colleague.create")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SETTINGS_GET", "jiuwenmate.colleague.settings.get")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE", "jiuwenmate.colleague.settings.update")
_ensure_req_method("JIUWENMATE_COLLEAGUE_PUBLISH", "jiuwenmate.colleague.publish")
_ensure_req_method("JIUWENMATE_COLLEAGUE_SET_ENABLED", "jiuwenmate.colleague.set_enabled")
_ensure_req_method("JIUWENMATE_COLLEAGUE_DELETE", "jiuwenmate.colleague.delete")
_ensure_req_method("JIUWENMATE_MODELS_LIST", "jiuwenmate.models.list")
_ensure_req_method("JIUWENMATE_CHAT_SEND", "jiuwenmate.chat.send")
_ensure_req_method("JIUWENMATE_CHAT_HISTORY", "jiuwenmate.chat.history")
_ensure_req_method("JIUWENMATE_ARTIFACT_LIST", "jiuwenmate.artifact.list")
_ensure_req_method("JIUWENMATE_REQUEST_GET", "jiuwenmate.request.get")
_ensure_req_method("JIUWENMATE_REQUEST_ABORT", "jiuwenmate.request.abort")
_ensure_req_method("JIUWENMATE_PERMISSION_DECIDE", "jiuwenmate.permission.decide")
_ensure_req_method("JIUWENMATE_SHARING_LIST", "jiuwenmate.sharing.list")
_ensure_req_method("JIUWENMATE_SHARING_DECIDE", "jiuwenmate.sharing.decide")
_ensure_req_method("JIUWENMATE_SHARING_REVOKE", "jiuwenmate.sharing.revoke")
_ensure_req_method("JIUWENMATE_CONVERSATION_LIST", "jiuwenmate.conversation.list")
_ensure_req_method("JIUWENMATE_CONVERSATION_RENAME", "jiuwenmate.conversation.rename")
_ensure_req_method("JIUWENMATE_CONVERSATION_PIN", "jiuwenmate.conversation.pin")
_ensure_req_method("JIUWENMATE_CONVERSATION_DELETE", "jiuwenmate.conversation.delete")
_ensure_req_method("JIUWENMATE_ACTIVITY_GET", "jiuwenmate.activity.get")
_ensure_req_method("JIUWENMATE_KNOWLEDGE_LIST", "jiuwenmate.knowledge.list")
_ensure_req_method("JIUWENMATE_SCHEDULE_LIST", "jiuwenmate.schedule.list")
_ensure_req_method("JIUWENMATE_SCHEDULE_CREATE", "jiuwenmate.schedule.create")
_ensure_req_method("JIUWENMATE_SCHEDULE_SET_ENABLED", "jiuwenmate.schedule.set_enabled")
_ensure_req_method("JIUWENMATE_SCHEDULE_DELETE", "jiuwenmate.schedule.delete")
_ensure_req_method("JIUWENMATE_SCHEDULE_UPDATE", "jiuwenmate.schedule.update")
_ensure_req_method("JIUWENMATE_SCHEDULE_RUN", "jiuwenmate.schedule.run")
_ensure_req_method("JIUWENMATE_SCHEDULE_HISTORY", "jiuwenmate.schedule.history")
_ensure_req_method("JIUWENMATE_CHANNEL_LIST", "jiuwenmate.channel.list")
_ensure_req_method("JIUWENMATE_CHANNEL_MESSAGES", "jiuwenmate.channel.messages")
_ensure_req_method("JIUWENMATE_CHANNEL_TRACE", "jiuwenmate.channel.trace")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_STATUS", "jiuwenmate.welink_office.status")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_CONNECT", "jiuwenmate.welink_office.connect")
_ensure_req_method("JIUWENMATE_WELINK_OFFICE_DISCONNECT", "jiuwenmate.welink_office.disconnect")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_STATUS", "jiuwenmate.agent_mail.status")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_BEGIN", "jiuwenmate.agent_mail.auth.begin")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_POLL", "jiuwenmate.agent_mail.auth.poll")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_AUTH_CANCEL", "jiuwenmate.agent_mail.auth.cancel")
_ensure_req_method("JIUWENMATE_AGENT_MAIL_DISCONNECT", "jiuwenmate.agent_mail.disconnect")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_STATUS", "jiuwenmate.dingtalk_auth.status")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_BEGIN", "jiuwenmate.dingtalk_auth.begin")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_POLL", "jiuwenmate.dingtalk_auth.poll")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_CANCEL", "jiuwenmate.dingtalk_auth.cancel")
_ensure_req_method("JIUWENMATE_DINGTALK_AUTH_DISCONNECT", "jiuwenmate.dingtalk_auth.disconnect")

_ensure_req_method("PROMPT_SUGGESTION_CANCEL", "prompt_suggestion.cancel")
_ensure_req_method("MODE_SET", "mode.set")
_ensure_req_method("MODE_GET", "mode.get")
_ensure_req_method("RECOMMENDATION", "recommendation.job")
# Dispatch compares this member on every request. The pinned protocol package
# does not define it, so a missing attribute fails chat and session creation
# before their own handlers run.
_ensure_req_method("MCP_OAUTH_START", "mcp.oauth.start")
# Trajectory reads are served by the AgentServer. The pinned protocol package
# does not define these members yet; registration leaves a matching upstream
# member in place once that package ships the same name and value.
_ensure_req_method("TRAJECTORY_SUBJECTS_LIST", "trajectory.subjects.list")
_ensure_req_method("TRAJECTORY_STREAM_FRAMES_GET", "trajectory.stream_frames.get")
_ensure_req_method("TRAJECTORY_SEQUENCES_GET", "trajectory.sequences.get")
_ensure_req_method("TRAJECTORY_CHECKPOINTS_GET", "trajectory.checkpoints.get")
_ensure_req_method("TRAJECTORY_USAGE_GET", "trajectory.usage.get")
_ensure_req_method("TRAJECTORY_SUBJECT_GET", "trajectory.subject.get")
_ensure_req_method("TRAJECTORY_RAW_GET", "trajectory.raw.get")
_ensure_req_method("TRAJECTORY_ARCHIVE_OPEN", "trajectory.archive.open")
_ensure_req_method("TRAJECTORY_BLOB_READ", "trajectory.blob.read")
_ensure_req_method("TRAJECTORY_BLOB_CLOSE", "trajectory.blob.close")
# Enterprise verify still registers these wire methods in HANDLERS / adapters,
# but the pinned openjiuwen-protocol revision does not ship them yet. Without
# ensure, importing dispatch crashes the runtime worker and HTTP :8766 never
# binds (readiness stays connection-refused).
_ensure_req_method("FILE_READ_UPLOAD_CHUNK", "file.read_upload_chunk")
_ensure_req_method("WORKSPACE_TREE", "workspace.tree")
_ensure_req_method("WORKSPACE_PREVIEW", "workspace.preview")
_ensure_req_method("WORKSPACE_USAGE", "workspace.usage")
_ensure_req_method("WORKSPACE_ENTRIES_DELETE", "workspace.entries.delete")
_ensure_req_method("SKILLS_SOURCE_PROVIDERS", "skills.source.providers")
_ensure_req_method("SKILLS_SOURCE_SEARCH", "skills.source.search")
_ensure_req_method("SKILLS_SOURCE_INSTALL", "skills.source.install")
_ensure_req_method("SKILLS_UPDATES_CHECK", "skills.updates.check")
_ensure_req_method("SKILLS_UPDATE", "skills.update")
_ensure_req_method("SKILLS_ENTERPRISE_LIST", "skills.enterprise.list")
_ensure_req_method("SKILLS_ENTERPRISE_INSTALL", "skills.enterprise.install")
_ensure_req_method("SKILLS_ENTERPRISE_UNINSTALL", "skills.enterprise.uninstall")
_ensure_req_method("SKILLS_ENTERPRISE_SOURCE_PROVIDERS", "skills.enterprise.source.providers")
_ensure_req_method("SKILLS_ENTERPRISE_SOURCE_SEARCH", "skills.enterprise.source.search")
_ensure_req_method("SKILLDEV_START", "skilldev.start")
_ensure_req_method("SKILLDEV_RESPOND", "skilldev.respond")
_ensure_req_method("SKILLDEV_STATUS", "skilldev.status")
_ensure_req_method("SKILLDEV_DOWNLOAD", "skilldev.download")
_ensure_req_method("SKILLDEV_CANCEL", "skilldev.cancel")
_ensure_req_method("SKILLDEV_FILE_LIST", "skilldev.file.list")
_ensure_req_method("SKILLDEV_FILE_READ", "skilldev.file.read")
# Removing a composer attachment chip should also drop the session-asset
# registry entry it created (see session_assets.py). The pinned protocol
# package does not define this member yet.
_ensure_req_method("SESSION_ASSETS_REMOVE", "session.assets.remove")
# Hub expert/equipment catalog served by AgentServer (dispatch.py / methods.py /
# http_invoke.py reference this member); 协议包当前 pin 尚未定义。
_ensure_req_method("HUB_EXPERT_PACKAGES_CATALOG", "hub.expert_packages.catalog")
# MCP Apps host bridge (io.modelcontextprotocol/ui); 协议包当前 pin 尚未定义。
_ensure_req_method("MCP_APP_LIST_TOOLS", "mcp_app.list_tools")
_ensure_req_method("MCP_APP_READ_RESOURCE", "mcp_app.read_resource")
_ensure_req_method("MCP_APP_CALL_TOOL", "mcp_app.call_tool")
# The Gateway forwards an event only when the protocol EventType knows it; the
# pinned protocol package does not define this one yet.
_ensure_protocol_member(EventType, "CHAT_CLIENT_TOOL_REQUEST", "chat.client_tool_request")
# Client attachments (e.g. the Office add-in task pane): a client attaches the
# resource it edits to a conversation, renews the attachment's lease, and reads
# the other attachments and their conversations. The pinned protocol package
# does not define these members yet.
_ensure_req_method("CLIENT_ATTACH", "client.attach")
_ensure_req_method("CLIENT_RENEW", "client.renew")
_ensure_req_method("CLIENT_LIST", "client.list")
_ensure_req_method("CLIENT_CONVERSATION_MESSAGES", "client.conversation.messages")
_ensure_req_method("CLIENT_CONVERSATIONS", "client.conversations")
_ensure_protocol_member(EventType, "CLIENT_ATTACHMENTS_CHANGED", "client.attachments_changed")

_ensure_req_method("VOICE_SESSION_RECEIPTS", "voice.session.receipts")
_ensure_req_method("VOICE_TASK_PENDING_INTERRUPT", "voice.task.pending_interrupt")

__all__ = [
    "PROMPT_SUGGESTION_READY_EVENT",
    "SESSION_REQUEST_COMPLETED_EVENT",
    "TEAM_ATTACH_ONLY_PARAM",
    "TOPIC_STARTERS_CONTROL_CHANGED_EVENT",
    "TOPIC_STARTERS_READY_EVENT",
    "EventType",
    "Message",
    "Mode",
    "ReqMethod",
]
