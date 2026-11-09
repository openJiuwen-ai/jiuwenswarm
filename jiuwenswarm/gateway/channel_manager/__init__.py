# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Channel 模块 - 客户端连接抽象.

启动时延优化: PEP 562 惰性导出。原 __init__ 模块级 eager import
base + channel_manager + web_connect + 8 个 IM 平台 connect (telegram/dingtalk/
xiaoyi/slack/discord/wecom/wechat) + acp，bare import 该包本地实测 1.34s（累计
2.15s，8 IM 平台占大头）。该包被 gateway 入口 app_gateway.py:69 `from ...channel_manager.base
import BaseWebChannel` 触发（导入子模块 base 先运行父包 __init__）——而 ready 前真用到
的只有 base/channel_manager/web_connect，8 IM 平台 connect 仅在通道配置时用
（feishu 5.1s 仅通道配置时付，Worker/通道已证惰性）。

PEP 562: bare import 该包（或导入任意子模块触发父包 __init__）不再 eager 拉任何兄弟——
__getattr__ 仅在真访问导出名时才 lazy import 对应子模块并缓存。gateway/__init__.py:18
的 `from ...channel_manager import ChannelManager` 已在 TYPE_CHECKING 下（静态分析可见、
运行时不触发），故 ready 前 __init__ 无人访问导出名 → 8 IM 平台 connect 链塌缩至 ~0。
"""

from __future__ import annotations

# 兄弟子模块 → (模块路径, [(导出名, 模块属性名)]) 映射。lazy import 后 populate 包命名空间。
_LAZY: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "base": (
        "jiuwenswarm.gateway.channel_manager.base",
        (("BaseChannel", "BaseChannel"), ("ChannelMetadata", "ChannelMetadata")),
    ),
    "channel_manager": (
        "jiuwenswarm.gateway.channel_manager.channel_manager",
        (("ChannelManager", "ChannelManager"),),
    ),
    "web_connect": (
        "jiuwenswarm.gateway.channel_manager.web.web_connect",
        (("WebChannel", "WebChannel"),),
    ),
    "xiaoyi": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect",
        (("XiaoyiChannel", "XiaoyiChannel"), ("XiaoyiChannelConfig", "XiaoyiChannelConfig")),
    ),
    "telegram": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.telegram.telegram_connect",
        (("TelegramChannel", "TelegramChannel"), ("TelegramChannelConfig", "TelegramChannelConfig")),
    ),
    "discord": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.discord.discord_connect",
        (("DiscordChannel", "DiscordChannel"), ("DiscordChannelConfig", "DiscordChannelConfig")),
    ),
    "slack": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
        (("SlackChannel", "SlackChannel"), ("SlackChannelConfig", "SlackChannelConfig")),
    ),
    "dingtalk": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_connect",
        (("DingTalkChannel", "DingTalkChannel"), ("DingTalkConfig", "DingTalkConfig")),
    ),
    "whatsapp": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.whatsapp.whatsapp_connect",
        (("WhatsAppChannel", "WhatsAppChannel"), ("WhatsAppChannelConfig", "WhatsAppChannelConfig")),
    ),
    "wecom": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.wecom.wecom_connect",
        (("WecomChannel", "WecomChannel"), ("WecomConfig", "WecomConfig")),
    ),
    "wechat": (
        "jiuwenswarm.gateway.channel_manager.im_platforms.wechat.wechat_connect",
        (("WechatChannel", "WechatChannel"), ("WechatConfig", "WechatConfig")),
    ),
    "acp": (
        "jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect",
        (("AcpChannel", "AcpChannel"), ("AcpChannelConfig", "AcpChannelConfig")),
    ),
}

__all__ = [
    "BaseChannel",
    "ChannelManager",
    "ChannelMetadata",
    "WebChannel",
    "XiaoyiChannel",
    "XiaoyiChannelConfig",
    "TelegramChannel",
    "TelegramChannelConfig",
    "DiscordChannel",
    "DiscordChannelConfig",
    "SlackChannel",
    "SlackChannelConfig",
    "DingTalkChannel",
    "DingTalkConfig",
    "WhatsAppChannel",
    "WhatsAppChannelConfig",
    "WecomChannel",
    "WecomConfig",
    "WechatChannel",
    "WechatConfig",
    "AcpChannel",
    "AcpChannelConfig",
]


def _lazy_export(mod_path: str, export_name: str, module_attr: str):
    """Import the target submodule and return the named attribute.

    Fail fast: if ``_LAZY`` maps an export name to a module attribute that no
    longer exists (the module renamed or dropped it), raise ``ImportError``
    pointing at the stale mapping entry instead of silently returning ``None``.
    This preserves the eager-import era's fail-fast contract: a drifted
    symbol name surfaces at first access with a message that names the mapping,
    not as a misleading ``TypeError: 'NoneType' object is not callable`` deep in
    a channel configuration path. The bare ``getattr`` (no ``None`` default)
    and the attribute-existence check both gate ``None`` from ever being cached
    into ``globals()``.
    """
    import importlib

    mod = importlib.import_module(mod_path)
    if not hasattr(mod, module_attr):
        raise ImportError(
            f"_LAZY maps {export_name!r} to {mod_path}.{module_attr}, "
            f"but the module has no such attribute"
        )
    return getattr(mod, module_attr)


def __getattr__(name: str):
    """Lazy-load sibling channel symbols on first access (PEP 562)."""
    for _key, (mod_path, attrs) in _LAZY.items():
        attr_names = {a[0]: a[1] for a in attrs}
        if name in attr_names:
            value = _lazy_export(mod_path, name, attr_names[name])
            # 缓存该子模块的全部导出名, 使后续兄弟查找直接命中, 不再走 __getattr__。
            for export_name, m_attr in attrs:
                globals()[export_name] = _lazy_export(mod_path, export_name, m_attr)
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
