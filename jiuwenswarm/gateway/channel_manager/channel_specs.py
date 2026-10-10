# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""渠道注册表：每个内置渠道在此声明配置形态与投递事实，宿主按表遍历。

``ChannelType``（``common.channels``）声明渠道身份，本表为每个成员补上网关侧
事实：

- ``config_fields``：``channels.<id>`` 的必填字段；``None`` 表示该渠道不由
  ``channels.<id>`` 配置创建（web/tui/acp/a2a 在启动时直接创建）。
- ``delivery``：构造该渠道的 ``DeliveryTarget`` 子类。
- ``default_metadata``：定时任务无会话时，从配置里取最近一次可回发的平台身份。

``_SPECS`` 覆盖 ``ChannelType`` 全部成员，缺失即 import 失败，新增渠道必须在
此声明事实，不能再散落到各处分支。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.channels import ChannelType
from jiuwenswarm.gateway.routing.keys import (
    AcpDeliveryTarget,
    DeliveryTarget,
    DingTalkDeliveryTarget,
    DiscordDeliveryTarget,
    FeishuDeliveryTarget,
    SlackDeliveryTarget,
    TelegramDeliveryTarget,
    TuiDeliveryTarget,
    WebDeliveryTarget,
    WechatDeliveryTarget,
    WecomDeliveryTarget,
    WhatsAppDeliveryTarget,
    XiaoyiDeliveryTarget,
)


@dataclass(frozen=True)
class ChannelSpec:
    """一个渠道的网关侧声明。"""

    channel: ChannelType
    config_fields: tuple[str, ...] | None = None
    delivery: Callable[..., DeliveryTarget] | None = None
    default_metadata: Callable[..., dict | None] | None = None


# ── DeliveryTarget 工厂 ──
# 每个工厂只声明自己读取的字段，其余关键字参数丢弃。


def _web_target(channel_id: str, *, ws_id: str = "", **_: Any) -> DeliveryTarget:
    return WebDeliveryTarget(channel_id=channel_id, ws_id=ws_id)


def _tui_target(channel_id: str, *, ws_id: str = "", **_: Any) -> DeliveryTarget:
    return TuiDeliveryTarget(channel_id=channel_id, ws_id=ws_id)


def _acp_target(channel_id: str, *, request_id: str = "", **_: Any) -> DeliveryTarget:
    return AcpDeliveryTarget(channel_id=channel_id, request_id=request_id)


def _xiaoyi_target(
    channel_id: str, *, chat_id: str = "", physical_user_id: str = "", **kwargs: Any
) -> DeliveryTarget:
    return XiaoyiDeliveryTarget(
        channel_id=channel_id,
        agent_id=physical_user_id,
        push_id=kwargs.get("push_id", ""),
        xiaoyi_session_id=kwargs.get("xiaoyi_session_id", "") or chat_id,
        conversation_id=kwargs.get("conversation_id", ""),
        url_key=kwargs.get("url_key", ""),
    )


def _feishu_target(
    channel_id: str,
    *,
    chat_id: str = "",
    receive_id: str = "",
    physical_user_id: str = "",
    **_: Any,
) -> DeliveryTarget:
    return FeishuDeliveryTarget(
        channel_id=channel_id,
        chat_type="group" if chat_id else "p2p",
        chat_id=chat_id,
        receive_id=receive_id or chat_id or physical_user_id,
        id_type="chat_id" if chat_id else "open_id",
        physical_user_id=physical_user_id,
    )


def _wecom_target(
    channel_id: str, *, chat_id: str = "", physical_user_id: str = "", **_: Any
) -> DeliveryTarget:
    return WecomDeliveryTarget(
        channel_id=channel_id,
        chat_type="group" if chat_id else "p2p",
        chat_id=chat_id,
        physical_user_id=physical_user_id,
    )


def _dingtalk_target(channel_id: str, *, physical_user_id: str = "", **_: Any) -> DeliveryTarget:
    return DingTalkDeliveryTarget(channel_id=channel_id, physical_user_id=physical_user_id)


def _telegram_target(
    channel_id: str, *, chat_id: str = "", physical_user_id: str = "", **_: Any
) -> DeliveryTarget:
    return TelegramDeliveryTarget(
        channel_id=channel_id,
        chat_id=int(chat_id or "0"),
        physical_user_id=physical_user_id,
    )


def _discord_target(channel_id: str, *, physical_user_id: str = "", **_: Any) -> DeliveryTarget:
    return DiscordDeliveryTarget(channel_id=channel_id, physical_user_id=physical_user_id)


def _slack_target(
    channel_id: str,
    *,
    chat_id: str = "",
    receive_id: str = "",
    physical_user_id: str = "",
    **kwargs: Any,
) -> DeliveryTarget:
    return SlackDeliveryTarget(
        channel_id=channel_id,
        chat_type=kwargs.get("chat_type", "group"),
        target_channel_id=chat_id or receive_id,
        thread_ts=kwargs.get("thread_ts", ""),
        physical_user_id=physical_user_id,
    )


def _whatsapp_target(
    channel_id: str, *, chat_id: str = "", receive_id: str = "", **_: Any
) -> DeliveryTarget:
    return WhatsAppDeliveryTarget(channel_id=channel_id, target_jid=chat_id or receive_id)


def _wechat_target(
    channel_id: str, *, receive_id: str = "", physical_user_id: str = "", **_: Any
) -> DeliveryTarget:
    return WechatDeliveryTarget(channel_id=channel_id, user_id=physical_user_id or receive_id)


# ── 定时任务缺省投递身份 ──
# 渠道把最近一次可回发的会话写进 ``channels.<id>.last_*``；定时任务推送没有
# 会话时按此还原目标。同样只声明自己读取的参数。


def _feishu_last_identity(cfg: dict) -> dict | None:
    last_chat_id = str(cfg.get("last_chat_id") or "").strip()
    last_open_id = str(cfg.get("last_open_id") or "").strip()
    if not last_chat_id and not last_open_id:
        return None
    return {"feishu_chat_id": last_chat_id, "feishu_open_id": last_open_id}


def _feishu_metadata(
    *, channel_id: str = "", ch_cfg: dict | None = None, app_id: str = "", **_: Any
) -> dict | None:
    cfg = ch_cfg or {}
    target_app_id = app_id
    if not target_app_id and channel_id.startswith("feishu:"):
        target_app_id = channel_id.split(":", 1)[1].strip()
    apps = cfg.get("apps") or []
    if isinstance(apps, list):
        for app in apps:
            if not isinstance(app, dict):
                continue
            if target_app_id and app.get("app_id") != target_app_id:
                continue
            if not target_app_id and not app.get("is_default", False):
                continue
            metadata = _feishu_last_identity(app)
            return metadata if metadata is not None else _feishu_last_identity(cfg)
    # 兜底：apps 列表为空或无匹配，回退到旧平铺字段
    return _feishu_last_identity(cfg)


def _feishu_enterprise_metadata(
    *, channel_id: str = "", channels_cfg: dict | None = None, **_: Any
) -> dict | None:
    app_id = channel_id.split(":", 1)[1].strip() if ":" in channel_id else ""
    enterprise_cfg = (channels_cfg or {}).get(ChannelType.FEISHU_ENTERPRISE.value) or {}
    if not isinstance(enterprise_cfg, dict) or not app_id:
        return None
    for bot_cfg in enterprise_cfg.values():
        if not isinstance(bot_cfg, dict):
            continue
        if str(bot_cfg.get("app_id") or "").strip() != app_id:
            continue
        return _feishu_last_identity(bot_cfg)
    return None


def _xiaoyi_metadata(*, ch_cfg: dict | None = None, **_: Any) -> dict | None:
    cfg = ch_cfg or {}
    last_session_id = str(cfg.get("last_session_id") or "").strip()
    last_task_id = str(cfg.get("last_task_id") or "").strip()
    if not last_session_id and not last_task_id:
        return None
    return {"xiaoyi_session_id": last_session_id, "xiaoyi_task_id": last_task_id}


def _whatsapp_metadata(*, ch_cfg: dict | None = None, **_: Any) -> dict | None:
    last_jid = str((ch_cfg or {}).get("last_jid") or "").strip()
    return {"whatsapp_jid": last_jid} if last_jid else None


def _wecom_metadata(*, ch_cfg: dict | None = None, **_: Any) -> dict | None:
    cfg = ch_cfg or {}
    last_chat_id = str(cfg.get("last_chat_id") or "").strip()
    last_user_id = str(cfg.get("last_user_id") or "").strip()
    if not last_chat_id and not last_user_id:
        return None
    return {"wecom_chat_id": last_chat_id, "wecom_user_id": last_user_id}


def _wechat_metadata(*, ch_cfg: dict | None = None, **_: Any) -> dict | None:
    cfg = ch_cfg or {}
    last_user_id = str(cfg.get("last_user_id") or "").strip()
    if not last_user_id:
        return None
    metadata = {"wechat_user_id": last_user_id, "reply_to_user_id": last_user_id}
    last_context_token = str(cfg.get("last_context_token") or "").strip()
    if last_context_token:
        metadata["wechat_context_token"] = last_context_token
        metadata["context_token"] = last_context_token
    return metadata


def _dingtalk_metadata(*, ch_cfg: dict | None = None, **_: Any) -> dict | None:
    cfg = ch_cfg or {}
    last_sender_id = str(cfg.get("last_sender_id") or "").strip()
    last_conversation_id = str(cfg.get("last_conversation_id") or "").strip()
    if not last_sender_id and not last_conversation_id:
        return None
    # 钉钉 send() 按 metadata 决定单聊/群聊：sender_id 为单聊兜底接收者，
    # 群聊以 conversation_id 为主。
    return {
        "dingtalk_sender_id": last_sender_id,
        "dingtalk_chat_id": last_conversation_id,
        "conversation_id": last_conversation_id,
        "conversation_type": str(cfg.get("last_conversation_type") or "").strip() or "1",
    }


_DECLARED: tuple[ChannelSpec, ...] = (
    ChannelSpec(ChannelType.ACP, delivery=_acp_target),
    ChannelSpec(ChannelType.A2A),
    ChannelSpec(ChannelType.WEB, delivery=_web_target),
    ChannelSpec(ChannelType.CLI, delivery=_tui_target),
    ChannelSpec(
        ChannelType.FEISHU,
        config_fields=("app_id", "app_secret"),
        delivery=_feishu_target,
        default_metadata=_feishu_metadata,
    ),
    ChannelSpec(
        ChannelType.FEISHU_ENTERPRISE,
        config_fields=("app_id", "app_secret"),
        delivery=_feishu_target,
        default_metadata=_feishu_enterprise_metadata,
    ),
    ChannelSpec(
        ChannelType.XIAOYI,
        config_fields=("ak", "sk", "agent_id"),
        delivery=_xiaoyi_target,
        default_metadata=_xiaoyi_metadata,
    ),
    ChannelSpec(
        ChannelType.DINGTALK,
        config_fields=("client_id", "client_secret"),
        delivery=_dingtalk_target,
        default_metadata=_dingtalk_metadata,
    ),
    ChannelSpec(
        ChannelType.TELEGRAM,
        config_fields=("bot_token",),
        delivery=_telegram_target,
    ),
    # whatsapp 的开关判定依赖 bridge_ws_url 的缺省值，不走 config_fields。
    ChannelSpec(
        ChannelType.WHATSAPP,
        config_fields=(),
        delivery=_whatsapp_target,
        default_metadata=_whatsapp_metadata,
    ),
    ChannelSpec(
        ChannelType.DISCORD,
        config_fields=("bot_token",),
        delivery=_discord_target,
    ),
    ChannelSpec(
        ChannelType.SLACK,
        config_fields=("bot_token", "app_token"),
        delivery=_slack_target,
    ),
    ChannelSpec(
        ChannelType.WECOM,
        config_fields=("bot_id", "secret"),
        delivery=_wecom_target,
        default_metadata=_wecom_metadata,
    ),
    ChannelSpec(
        ChannelType.WECHAT,
        config_fields=(),
        delivery=_wechat_target,
        default_metadata=_wechat_metadata,
    ),
    ChannelSpec(ChannelType.SSH, config_fields=("listen_port",)),
)

_SPECS: dict[ChannelType, ChannelSpec] = {spec.channel: spec for spec in _DECLARED}

_MISSING = [channel.name for channel in ChannelType if channel not in _SPECS]
if _MISSING:
    raise RuntimeError(f"channel specs missing for: {', '.join(_MISSING)}")


def spec_for(channel: ChannelType | None) -> ChannelSpec | None:
    """按渠道成员取声明；``None`` 透传为 ``None``。"""
    return _SPECS.get(channel) if channel is not None else None


def configured_channel_ids() -> tuple[str, ...]:
    """由 ``channels.<id>`` 配置创建的渠道 id，顺序即网关应用配置的顺序。"""
    return tuple(spec.channel.value for spec in _DECLARED if spec.config_fields is not None)
