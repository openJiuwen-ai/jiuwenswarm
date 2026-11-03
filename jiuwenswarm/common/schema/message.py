# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``openjiuwen_protocol.message``。

仅保留 ``jiuwenswarm.common.schema.message`` 旧 import 路径兼容，re-export 协议包
的同一对象——``Message`` / ``ReqMethod`` / ``EventType`` / ``Mode`` 及线协议事件名
常量。``ReqMethod`` 的成员扩展（``RECOMMENDATION`` / ``TRAJECTORY_*``）现由协议包
直接提供；仅协议包当前 pin 尚未发布的成员（``SESSION_ASSETS_REMOVE``）保留最小
运行时补齐，避免两仓各维护一份枚举类。
"""

from __future__ import annotations

from openjiuwen_protocol.message import (  # noqa: F401
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

from agent_gateway.common.protocol_methods import _ensure_protocol_member, _ensure_req_method
from jiuwenswarm.enterprise.edition import is_enterprise


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
# Dispatch compares this member on every request. The pinned protocol package
# does not define it, so a missing attribute fails chat and session creation
# before their own handlers run.
_ensure_req_method("MCP_OAUTH_START", "mcp.oauth.start")
# Enterprise verify still registers these wire methods in HANDLERS / adapters,
# but the pinned openjiuwen-protocol revision does not ship them yet. Without
# ensure, importing dispatch crashes the runtime worker and HTTP :8766 never
# binds (readiness stays connection-refused).
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

_TEAM_CONTROL_REQ_METHODS = (
    ("TEAM_CREATE", "team.create"),
    ("TEAM_GET", "team.get"),
    ("TEAM_STOP", "team.stop"),
    ("TEAM_MEMBER_JOIN", "team.member.join"),
    ("TEAM_MEMBER_LEAVE", "team.member.leave"),
    ("TEAM_MEMBERS_LIST", "team.members.list"),
    ("TEAM_QUOTA_GET", "team.quota.get"),
)


def ensure_team_control_req_methods() -> None:
    """Register enterprise team-control methods missing from the pinned protocol."""
    for name, value in _TEAM_CONTROL_REQ_METHODS:
        _ensure_req_method(name, value)


if is_enterprise():
    ensure_team_control_req_methods()

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
