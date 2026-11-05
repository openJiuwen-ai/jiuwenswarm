# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 启动包装器：内核级转发白名单兼容层。

jiuwenmate / group 执行管理 / models.refresh_status 等请求方法由
AgentServer 实现，但独立仓 agent-gateway 的转发白名单未包含它们，
会把这批请求拒绝为 ``unknown method``。

时序是关键：gateway 装配函数在函数体内 from-import
``_FORWARD_REQ_METHODS`` / ``_FORWARD_NO_LOCAL_HANDLER_METHODS`` 两个
frozenset（``app_gateway.py``），此后转发闭包持有的是**对象引用**，事后
重赋值模块属性无法生效。因此本模块作为启动包装器
（``python -m jiuwenswarm.gateway_compat``）在委托
``agent_gateway.app_gateway.main()`` **之前**完成补齐，使装配闭包捕获到
扩展后的白名单：

1. ``openjiuwen_protocol.ReqMethod`` 枚举成员（import
   ``jiuwenswarm.common.schema.message`` 触发运行时补齐；缺成员时
   ``WebChannel._parse_req_method`` 解析为 None，白名单也无法命中）；
2. 转发白名单两个 frozenset（jiuwenmate.* 46 个 + group.* 5 个 +
   models.refresh_status，均由 AgentServer 处理、Gateway 无本地实现）；
3. Web 推送事件的完整 payload 放行（``jiuwenmate.chat.stream`` 等）；
4. 群聊 stream 取消豁免：群成员消息各自独立，新的 mention 不能取消
   整个群正在进行的流（AgentServer 侧 group_requests.py 另做 session
   归一兜底）。

启动拉起点（app.py / desktop_app.py / frozen exe 入口 / 控制台脚本
jiuwenswarm-gateway）统一指向本模块。暂不覆盖：
RC-3 孤儿事件丢弃、``jiuwenmate.colleague.delete`` 的 Gateway 本地预处理。
HarmonyOS（harmony_entry.py）仍直启 agent_gateway，行为与上游一致。
"""

from __future__ import annotations

from typing import Any

# AgentServer 侧实现、Gateway 无本地 handler 的转发方法。
# 与 jiuwenswarm AgentServer 的 JiuwenMateWebAdapter / group_service /
# web_adapter 方法集保持同步（jiuwenmate.colleague.delete 除外，见模块注释）。
_EXTRA_FORWARD_METHODS: frozenset[str] = frozenset({
    # Group chat execution/file/history management（AgentServer group_service 处理）。
    "group.execution.list",
    "group.execution.get",
    "group.file.rename",
    "group.file.delete",
    "group.history.clear",
    # 模型刷新状态：AgentServer 的 JiuwenMateWebAdapter 直接处理。
    "models.refresh_status",
    # JiuwenMate 同事管理：AgentServer 的 JiuwenMateWebAdapter 处理。
    "jiuwenmate.colleague.ensure_default",
    "jiuwenmate.colleague.list",
    "jiuwenmate.colleague.get",
    "jiuwenmate.colleague.create",
    "jiuwenmate.colleague.settings.get",
    "jiuwenmate.colleague.settings.update",
    "jiuwenmate.colleague.publish",
    "jiuwenmate.colleague.set_enabled",
    "jiuwenmate.models.list",
    "jiuwenmate.chat.send",
    "jiuwenmate.chat.history",
    "jiuwenmate.request.get",
    "jiuwenmate.request.abort",
    "jiuwenmate.permission.decide",
    "jiuwenmate.sharing.list",
    "jiuwenmate.sharing.decide",
    "jiuwenmate.sharing.revoke",
    "jiuwenmate.conversation.list",
    "jiuwenmate.conversation.rename",
    "jiuwenmate.conversation.pin",
    "jiuwenmate.conversation.delete",
    "jiuwenmate.activity.get",
    "jiuwenmate.knowledge.list",
    "jiuwenmate.schedule.list",
    "jiuwenmate.schedule.create",
    "jiuwenmate.schedule.set_enabled",
    "jiuwenmate.schedule.delete",
    "jiuwenmate.schedule.update",
    "jiuwenmate.schedule.run",
    "jiuwenmate.schedule.history",
    "jiuwenmate.channel.list",
    "jiuwenmate.channel.messages",
    "jiuwenmate.channel.trace",
    "jiuwenmate.welink_office.status",
    "jiuwenmate.welink_office.connect",
    "jiuwenmate.welink_office.disconnect",
    "jiuwenmate.agent_mail.status",
    "jiuwenmate.agent_mail.auth.begin",
    "jiuwenmate.agent_mail.auth.poll",
    "jiuwenmate.agent_mail.auth.cancel",
    "jiuwenmate.agent_mail.disconnect",
    "jiuwenmate.dingtalk_auth.status",
    "jiuwenmate.dingtalk_auth.begin",
    "jiuwenmate.dingtalk_auth.poll",
    "jiuwenmate.dingtalk_auth.cancel",
    "jiuwenmate.dingtalk_auth.disconnect",
})

# 需要 Web 客户端拿到完整 payload 的新增推送事件。
_EXTRA_FULL_PAYLOAD_EVENTS: frozenset[str] = frozenset({
    "chat.moa_status",
    # JiuwenMate 私聊的流式片段：前端按 request_id、execution_id 与 seq 合并。
    "jiuwenmate.chat.stream",
})

_applied = False


def apply_gateway_compat_patches() -> None:
    """Idempotently extend gateway runtime whitelists in the current process.

    Must run before ``agent_gateway.app_gateway``'s assembly function executes
    its in-function from-import of the forward whitelists.
    """
    global _applied
    if _applied:
        return
    _applied = True

    # 1) ReqMethod 枚举补齐：pin 版本的 openjiuwen_protocol 缺 jiuwenmate.* 等
    #    成员；该模块 import 时运行 _ensure_req_method 扩展共享枚举。
    import jiuwenswarm.common.schema.message  # noqa: F401  pylint: disable=unused-import

    # 2) 转发白名单：装配函数随后 from-import 捕获的即为此扩展后的对象。
    from agent_gateway.channel_manager.web import app_web_handlers

    app_web_handlers._FORWARD_REQ_METHODS = (  # noqa: SLF001
        frozenset(app_web_handlers._FORWARD_REQ_METHODS) | _EXTRA_FORWARD_METHODS  # noqa: SLF001
    )
    app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS = (  # noqa: SLF001
        frozenset(app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS)  # noqa: SLF001
        | _EXTRA_FORWARD_METHODS
    )

    # 3) 事件完整 payload 放行：_WEB_FULL_PAYLOAD_EVENT_TYPES 为请求期读取的
    #    模块属性；video.core.result 在 _should_preserve_full_payload 的内联
    #    集合里，以包装器放行。
    from agent_gateway.channel_manager.web import web_connect

    web_connect._WEB_FULL_PAYLOAD_EVENT_TYPES = (  # noqa: SLF001
        frozenset(web_connect._WEB_FULL_PAYLOAD_EVENT_TYPES)  # noqa: SLF001
        | _EXTRA_FULL_PAYLOAD_EVENTS
    )
    _original_preserve = web_connect.WebChannel._should_preserve_full_payload

    @staticmethod
    def _preserve_full_payload_with_compat(event_name: str) -> bool:  # noqa: ANN001
        if event_name == "video.core.result":
            return True
        return bool(_original_preserve(event_name))

    web_connect.WebChannel._should_preserve_full_payload = (  # noqa: SLF001
        _preserve_full_payload_with_compat
    )

    # 4) 群聊 stream 取消豁免：带 group_id 的 chat.send 不取消现有流
    #    （群成员消息各自独立），其余行为保持原实现。
    from agent_gateway.message_handler.message_handler import MessageHandler

    _original_should_cancel = (
        MessageHandler._should_cancel_existing_stream_before_chat_send
    )

    @classmethod
    def _should_cancel_with_group_exemption(cls, msg: Any) -> bool:  # noqa: N805, ANN001
        params = msg.params if isinstance(msg.params, dict) else {}
        group_id = params.get("group_id")
        if isinstance(group_id, str) and group_id.strip():
            return False
        return bool(_original_should_cancel.__func__(cls, msg))  # type: ignore[attr-defined]

    MessageHandler._should_cancel_existing_stream_before_chat_send = (  # noqa: SLF001
        _should_cancel_with_group_exemption
    )


def main() -> None:
    """Gateway entry: apply compat patches, then delegate to agent_gateway."""
    apply_gateway_compat_patches()
    from agent_gateway.app_gateway import main as gateway_main

    gateway_main()


if __name__ == "__main__":
    main()
