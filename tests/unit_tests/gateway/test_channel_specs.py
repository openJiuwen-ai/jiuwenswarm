"""渠道注册表：每个渠道声明一次，网关、路由、定时任务与 IM 管道按声明派生。

原先渠道身份在五处各枚举一次且已经不一致：定时任务目标枚举缺 telegram /
discord / slack / ssh，Channel 类型枚举缺 feishu_enterprise，投递地址工厂缺
ssh。本文件锁定派生关系，使新增渠道只能改一处声明。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.channels import (
    CRON_TARGET_CHANNEL_IDS,
    SHARED_IM_CHANNEL_IDS,
    ChannelType,
    channel_for_id,
)
from jiuwenswarm.gateway.channel_manager.channel_specs import (
    configured_channel_ids,
    spec_for,
)
from jiuwenswarm.gateway.im_pipeline.im_session_input import is_shared_im_channel
from jiuwenswarm.gateway.routing.keys import (
    DingTalkDeliveryTarget,
    FeishuDeliveryTarget,
    SlackDeliveryTarget,
    TelegramDeliveryTarget,
    WebDeliveryTarget,
    make_delivery_target,
)
from jiuwenswarm.runtime.cron.models import (
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.runtime.cron.store import CronJobStore


def test_every_channel_has_a_spec():
    for channel in ChannelType:
        assert spec_for(channel) is not None, channel.name


def test_configured_channels_are_the_channels_with_a_config_block():
    assert configured_channel_ids() == (
        "feishu",
        "feishu_enterprise",
        "xiaoyi",
        "dingtalk",
        "telegram",
        "whatsapp",
        "discord",
        "slack",
        "wecom",
        "wechat",
        "ssh",
    )
    for channel_id in configured_channel_ids():
        spec = spec_for(ChannelType(channel_id))
        assert spec is not None and spec.config_fields is not None


@pytest.mark.parametrize("channel_id", ["web", "tui", "acp", "a2a"])
def test_startup_channels_have_no_config_block(channel_id):
    spec = spec_for(ChannelType(channel_id))
    assert spec is not None and spec.config_fields is None
    assert channel_id not in configured_channel_ids()


def test_cron_targets_derive_from_the_declaration():
    assert CRON_TARGET_CHANNEL_IDS == {
        channel.value for channel in ChannelType if is_valid_target_channel_id(channel.value)
    }
    assert "acp" not in CRON_TARGET_CHANNEL_IDS


@pytest.mark.parametrize("channel_id", ["telegram", "discord", "slack", "ssh"])
def test_a_shipped_channel_is_a_cron_target(channel_id):
    """这四个渠道的连接器一直存在，却不在定时任务目标枚举里。"""
    assert is_valid_target_channel_id(channel_id)
    assert normalize_target_channel_id(channel_id) == channel_id


@pytest.mark.parametrize("channel_id", ["telegram", "discord", "ssh"])
async def test_cron_job_keeps_its_target_across_a_reload(tmp_path: Path, channel_id):
    """目标渠道不在枚举里时会被静默改写为 web：创建返回原值，重新加载变 web。"""
    store = CronJobStore(tmp_path / "cron_jobs.json")
    job = await store.create_job(
        name=f"job-{channel_id}",
        cron_expr="0 9 * * *",
        timezone="UTC",
        description="daily report",
        targets=channel_id,
    )
    assert job.targets == channel_id
    reloaded = [item for item in await store.list_jobs() if item.id == job.id]
    assert [item.targets for item in reloaded] == [channel_id]


def test_shared_im_channels_derive_from_the_declaration():
    assert SHARED_IM_CHANNEL_IDS == {
        channel.value for channel in ChannelType if is_shared_im_channel(channel.value)
    }
    assert is_shared_im_channel("feishu_enterprise:cli_9")
    assert not is_shared_im_channel("web")
    assert not is_shared_im_channel("ssh")
    assert not is_shared_im_channel("nope")


def test_channel_for_id_resolves_an_instance_suffix():
    assert channel_for_id("feishu_enterprise:cli_9") is ChannelType.FEISHU_ENTERPRISE
    assert channel_for_id("FEISHU") is ChannelType.FEISHU
    assert channel_for_id("nope") is None
    assert channel_for_id("") is None


def test_delivery_targets_come_from_the_declaration():
    feishu = make_delivery_target("feishu", chat_id="oc_1", physical_user_id="ou_1")
    assert isinstance(feishu, FeishuDeliveryTarget)
    assert (feishu.chat_type, feishu.receive_id, feishu.id_type) == ("group", "oc_1", "chat_id")

    telegram = make_delivery_target("telegram", chat_id="-100", physical_user_id="u")
    assert isinstance(telegram, TelegramDeliveryTarget)
    assert telegram.chat_id == -100 and telegram.container_kind == "group"

    slack = make_delivery_target("slack", chat_id="C1", thread_ts="1.2", chat_type="p2p")
    assert isinstance(slack, SlackDeliveryTarget)
    assert slack.get_container_id() == "C1:1.2"

    dingtalk = make_delivery_target("dingtalk", physical_user_id="staff")
    assert isinstance(dingtalk, DingTalkDeliveryTarget)

    # 未声明工厂的渠道仍回落到基础实例。
    assert isinstance(make_delivery_target("ssh", ws_id="w"), WebDeliveryTarget)
    assert isinstance(make_delivery_target("nope"), WebDeliveryTarget)


def test_cron_default_metadata_reads_the_channel_config():
    feishu = spec_for(ChannelType.FEISHU)
    assert feishu.default_metadata is not None
    channels_cfg = {
        "feishu": {
            "apps": [
                {"app_id": "cli_a", "is_default": True, "last_chat_id": "oc_a"},
                {"app_id": "cli_b", "last_open_id": "ou_b"},
            ]
        }
    }
    assert feishu.default_metadata(
        channel_id="feishu", ch_cfg=channels_cfg["feishu"], channels_cfg=channels_cfg, app_id="",
    ) == {"feishu_chat_id": "oc_a", "feishu_open_id": ""}
    assert feishu.default_metadata(
        channel_id="feishu",
        ch_cfg=channels_cfg["feishu"],
        channels_cfg=channels_cfg,
        app_id="cli_b",
    ) == {"feishu_chat_id": "", "feishu_open_id": "ou_b"}

    enterprise = spec_for(ChannelType.FEISHU_ENTERPRISE)
    enterprise_cfg = {
        "feishu_enterprise": {"bot": {"app_id": "cli_9", "last_chat_id": "oc_9"}}
    }
    assert enterprise.default_metadata(
        channel_id="feishu_enterprise:cli_9", ch_cfg={}, channels_cfg=enterprise_cfg, app_id="",
    ) == {"feishu_chat_id": "oc_9", "feishu_open_id": ""}

    wechat = spec_for(ChannelType.WECHAT)
    assert wechat.default_metadata(ch_cfg={"last_user_id": "u1"}) == {
        "wechat_user_id": "u1",
        "reply_to_user_id": "u1",
    }
    assert wechat.default_metadata(ch_cfg={}) is None


def test_channels_without_recorded_identity_declare_no_default_metadata():
    """telegram / discord / slack / ssh 不往配置里写最近会话，无可还原的身份。"""
    for channel in (
        ChannelType.TELEGRAM,
        ChannelType.DISCORD,
        ChannelType.SLACK,
        ChannelType.SSH,
    ):
        assert spec_for(channel).default_metadata is None
