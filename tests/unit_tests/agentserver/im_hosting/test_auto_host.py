from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.im.im_connector.types import ChannelTarget, Identity
from jiuwenswarm.server.im.im_hosting.auto_host import (
    channel_allows_auto_host,
    identity_ids,
    select_auto_host_candidates,
)
from jiuwenswarm.server.im.im_hosting.reply_bridge import resolve_target_persona


def test_select_auto_host_skips_hosted_self_and_caps():
    convs = [
        ChannelTarget(kind="user", external_id="me", title="自己"),
        ChannelTarget(kind="user", external_id="ou_1", title="许康"),
        ChannelTarget(kind="group", external_id="oc_old", title="已托管群"),
        ChannelTarget(kind="group", external_id="oc_new", title="新群"),
        ChannelTarget(kind="user", external_id="ou_2", title="李华"),
    ]
    picked = select_auto_host_candidates(
        convs,
        hosted={("group", "oc_old")},
        self_ids={"me"},
        auto_host_groups=True,
        auto_host_users=True,
        max_new=2,
    )
    assert [(c.kind, c.external_id) for c in picked] == [
        ("user", "ou_2"),
        ("user", "ou_1"),
    ]


def test_select_auto_host_respects_kind_switches():
    convs = [
        ChannelTarget(kind="user", external_id="ou_1", title="许康"),
        ChannelTarget(kind="group", external_id="oc_1", title="群"),
    ]
    users = select_auto_host_candidates(
        convs, hosted=set(), self_ids=set(), auto_host_groups=False, auto_host_users=True
    )
    assert [c.kind for c in users] == ["user"]
    groups = select_auto_host_candidates(
        convs, hosted=set(), self_ids=set(), auto_host_groups=True, auto_host_users=False
    )
    assert [c.kind for c in groups] == ["group"]


def test_identity_ids_reads_account_and_open_ids():
    ids = identity_ids(Identity(account="me", extra={"open_ids": ["ou_me", ""]}))
    assert ids == {"me", "ou_me"}


def test_channel_allows_auto_host():
    assert channel_allows_auto_host({"auto_host_users": True}) is True
    assert channel_allows_auto_host({"auto_host_groups": True}) is True
    assert channel_allows_auto_host({"auto_host_users": False, "auto_host_groups": False}) is False


def test_resolve_target_persona_falls_back_to_policy():
    assert resolve_target_persona({"expert_persona": "会话说明"}) == "会话说明"
    assert resolve_target_persona({}, {"expert_persona": "全局说明"}) == "全局说明"
    assert resolve_target_persona({"expert_persona": "  "}, {"expert_persona": "全局说明"}) == "全局说明"


@pytest.mark.asyncio
async def test_apply_policy_off_drops_auto_keeps_manual(tmp_path: Path):
    from jiuwenswarm.server.im.im_hosting.policy import HostingPolicyStore
    from jiuwenswarm.server.im.im_hosting.service import HostingPollService
    from jiuwenswarm.server.im.im_hosting.store import HostingStore

    store = HostingStore(tmp_path / "hosting.db")
    svc = HostingPollService(
        store=store,
        policy=HostingPolicyStore(store=store),
        connectors={},
    )
    store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_auto",
        title="吃饭群",
        source="auto",
    )
    manual = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_hand",
        title="手选群",
        source="manual",
    )
    await svc.apply_channel_policy(
        "dingtalk",
        {"auto_host_groups": False, "auto_host_users": False},
    )
    left = store.list_targets("dingtalk")
    assert [row["id"] for row in left] == [manual["id"]]
