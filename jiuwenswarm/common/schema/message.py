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


def _ensure_req_method(name: str, value: str) -> None:
    """Register a ReqMethod member the pinned protocol package does not ship yet.

    ``openjiuwen-protocol`` owns the enum; 这里只为协议包尚未发布的成员开一个缺口，
    待协议包提供同名同值成员后本函数与调用一并删除。
    """
    existing = ReqMethod.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"ReqMethod.{name} is {existing.value!r}, expected {value!r}"
            )
        return
    if any(item.value == value for item in ReqMethod):
        raise RuntimeError(f"ReqMethod value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(ReqMethod)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(ReqMethod, name, member)
    ReqMethod._member_map_[name] = member  # pylint: disable=protected-access
    ReqMethod._value2member_map_[value] = member  # pylint: disable=protected-access
    ReqMethod._member_names_.append(name)  # pylint: disable=protected-access


_ensure_req_method("PROMPT_SUGGESTION_CANCEL", "prompt_suggestion.cancel")
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
# MCP Apps host bridge (io.modelcontextprotocol/ui); 协议包当前 pin 尚未定义。
_ensure_req_method("MCP_APP_LIST_TOOLS", "mcp_app.list_tools")
_ensure_req_method("MCP_APP_READ_RESOURCE", "mcp_app.read_resource")
_ensure_req_method("MCP_APP_CALL_TOOL", "mcp_app.call_tool")

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
