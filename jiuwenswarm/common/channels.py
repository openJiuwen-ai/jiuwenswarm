# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""渠道身份共享基础设施：渠道只声明一次，其余列表由此派生。

位于 ``common/`` 层供 ``gateway.channel_manager``、``gateway.routing``、
``runtime.cron`` 与 ``agents.harness`` 共同 import，避免 runtime 反向依赖
gateway。

渠道身份原先在网关启动、Channel 类型枚举、定时任务目标枚举、IM 会话输入与
投递地址工厂里各枚举一次，彼此已经不一致：定时任务枚举缺 telegram/discord/
slack/ssh，Channel 类型枚举缺 feishu_enterprise，投递地址工厂缺 ssh。
"""

from __future__ import annotations

from enum import Enum


class ChannelType(str, Enum):
    """本产品发布的全部渠道。

    成员值即渠道 id：``channels.<id>`` 配置键、``Message.channel_id`` 与定时
    任务 ``targets`` 都用它。成员顺序即网关应用渠道配置的顺序。
    """

    ACP = "acp"
    A2A = "a2a"
    WEB = "web"
    CLI = "tui"
    FEISHU = "feishu"
    FEISHU_ENTERPRISE = "feishu_enterprise"
    XIAOYI = "xiaoyi"
    DINGTALK = "dingtalk"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    SLACK = "slack"
    WECOM = "wecom"
    WECHAT = "wechat"
    SSH = "ssh"


ALL_CHANNEL_IDS: frozenset[str] = frozenset(channel.value for channel in ChannelType)

# 定时任务可推送的渠道。按排除声明：新增渠道默认即可作为推送目标，不会再像
# 原先那样因为漏登记而被静默改写成 web。
# - acp / a2a：协议入站，没有可回发的人类会话。
# - feishu_enterprise：实例 id 形如 ``feishu_enterprise:<app_id>``，裸 id 定位
#   不到实例，实例 id 由 cron 的前缀规则单独校验。
CRON_TARGET_CHANNEL_IDS: frozenset[str] = ALL_CHANNEL_IDS - {
    ChannelType.ACP.value,
    ChannelType.A2A.value,
    ChannelType.FEISHU_ENTERPRISE.value,
}

# 走共享 IM 会话输入路径的渠道：IM 平台之外的渠道各有自己的入站路径。
SHARED_IM_CHANNEL_IDS: frozenset[str] = ALL_CHANNEL_IDS - {
    ChannelType.ACP.value,
    ChannelType.A2A.value,
    ChannelType.WEB.value,
    ChannelType.CLI.value,
    ChannelType.SSH.value,
}


def channel_for_id(channel_id: object) -> ChannelType | None:
    """按渠道 id 查成员，带 ``:<实例>`` 后缀的 id 归到其基础渠道。

    飞书企业版实例 id 形如 ``feishu_enterprise:<app_id>``。未知渠道返回 None。
    """
    text = str(channel_id or "").strip().lower()
    if not text:
        return None
    for candidate in (text, text.split(":", 1)[0]):
        try:
            return ChannelType(candidate)
        except ValueError:
            continue
    return None
