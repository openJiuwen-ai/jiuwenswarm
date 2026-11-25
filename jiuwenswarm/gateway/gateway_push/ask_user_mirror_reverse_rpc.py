# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AskUser 跨进程镜像登记 capability（桌面发起、手机作答）。

背景：``app.py`` 把 agent_server 与 gateway 起成两个子进程。桌面端在「手机发起过」
的话题里续发消息时，agent 产出的 ``chat.ask_user_question`` 由 ``agent_ws_server``
直发桌面连接（不经网关）；用户在手机点选后的结构化应答回包却进网关 xiaoyi 通道。
若 xiaoyi 侧没有对应待答复 pending，回包会被判「无待答复审批」丢弃。

本 capability 由 agent_server 通过反向 RPC（``get_reverse_rpc_client().call``）
调起（cron run_now 同款通道，V1 传输固定走唯一 gateway 连接），在 gateway 进程内
把该 ask_user 登记到 xiaoyi 通道 pending —— 只登记、不发卡。手机卡片已由云端同步
呈现，登记后手机回包即可命中并 resume。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.reverse_rpc.constants import (
    ASK_USER_MIRROR_REVERSE_RPC_METHOD,
    ERROR_INVALID_REQUEST,
)
from jiuwenswarm.gateway.reverse_rpc.errors import CapabilityError
from jiuwenswarm.gateway.reverse_rpc.registry import (
    CapabilityRegistry,
    CapabilitySpec,
    ReverseRpcCapabilityContext,
)

logger = logging.getLogger(__name__)

_ASK_USER_MIRROR_MAX_TIMEOUT_SECONDS = 30.0


class AskUserMirrorCapability:
    """把桌面发起的 AskUser 登记到 xiaoyi 通道 pending（供手机作答命中）。"""

    def __init__(self, channel_manager: Any) -> None:
        self._channel_manager = channel_manager

    async def handle(
        self,
        ctx: ReverseRpcCapabilityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = payload or {}
        conv = str(data.get("conversation_id") or "").strip()
        event_payload = data.get("payload")
        if not conv or not isinstance(event_payload, dict):
            raise CapabilityError(
                ERROR_INVALID_REQUEST,
                "conversation_id and payload are required",
            )
        if self._channel_manager is None:
            raise CapabilityError(
                "CHANNEL_MANAGER_UNAVAILABLE",
                "channel manager is not active in this gateway process",
            )
        meta = {
            "xiaoyi_conversation_id": conv,
            "xiaoyi_root_session_id": str(
                data.get("root_session_id") or ""
            ).strip(),
            "im_sender_user_id": str(data.get("agent_id") or "").strip(),
        }
        session_id = str(data.get("session_id") or "").strip()
        registered = 0
        for ch in self._channel_manager.get_channels_by_id("xiaoyi"):
            register = getattr(ch, "register_mirrored_ask_user", None)
            if callable(register):
                register(event_payload, dict(meta), session_id)
                registered += 1
        logger.info(
            "[AskUserMirror] reverse_rpc 登记完成 registered=%d conv=%s rpc_id=%s",
            registered,
            conv,
            ctx.rpc_id,
        )
        return {"registered": registered}


def register_ask_user_mirror_reverse_rpc(
    registry: CapabilityRegistry,
    channel_manager: Any,
) -> None:
    """在 Gateway 组装期注册 ask_user.mirror capability。"""

    registry.register(
        CapabilitySpec(
            method=ASK_USER_MIRROR_REVERSE_RPC_METHOD,
            handler=AskUserMirrorCapability(channel_manager),
            # 纯登记操作，无中途取消/断连语义；尽力而为。
            supports_cancel=False,
            cancel_on_disconnect=False,
            max_timeout_seconds=_ASK_USER_MIRROR_MAX_TIMEOUT_SECONDS,
            # 不依赖 route 选连接（V1 固定走唯一 gateway 连接）。
            allow_empty_route=True,
        )
    )
