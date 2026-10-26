"""验证 GodView(通道级伪 member)不参与 /join 容器去重,以及 /exit 正确性基础。

背景:GodView 由 _maybe_register_godview 自动注册,接收全量输出,不是 /join 认领
的真实席位。两个 bug 同源:

1. /join 容器去重(lookup_by_container)曾未排除 GodView,导致飞书群 /mode team
   自动注册 GodView 后,再 /join 真实 member 被自己的 GodView 挡住(报
   "已加入 session 席位:GodView")。
2. /exit 不带 session_ref 时曾误以为 msg.session_id 是物理 chat_id。实际
   handle_message 入队前已调 resolve_member_by_user 把已 /join 用户的
   msg.session_id 改写为逻辑 session 并写入 member_name,故 /exit 直接用
   msg.session_id 即可;只需在未命中(未 /join)时用 channel state 的逻辑 session
   做提示,避免显示物理 chat_id。

Fix 1:lookup_by_container 跳过 is_godview,与 resolve_member_by_user 一致。
Fix 2:依赖 resolve_member_by_user 返回逻辑 session 且不命中 GodView(下面验证
   这条契约;handler 层的改动是去掉冗余反查兜底,不再单独测)。
"""
from __future__ import annotations

import pytest

from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry, SubRole


def _rk(channel_id: str, user_id: str, session_id: str) -> RoutingKey:
    return RoutingKey(
        user_id=user_id,
        channel_id=channel_id,
        app_id="default",
        agent_ref=AgentRef("team", "default"),
        session_id=session_id,
    )


async def _register(
    reg: SessionSharingRegistry, session_id: str, member_name: str,
    channel_id: str, user_id: str, chat_id: str,
) -> None:
    rk = _rk(channel_id, user_id, session_id)
    dt = make_delivery_target(channel_id, chat_id=chat_id, physical_user_id=user_id)
    await reg.register(session_id, member_name, rk, dt)


# ---------- Fix 1: lookup_by_container 排除 GodView ----------


@pytest.mark.asyncio
async def test_lookup_by_container_skips_godview():
    """GodView 订阅不应被 /join 容器去重命中。

    模拟飞书群 /mode team 已注册 GodView(逻辑 session feishu_xxx,
    delivery.chat_id=oc_xxx),同一容器再 /join 真实 member 时,
    lookup_by_container 应返回空(不被 GodView 挡)。
    """
    reg = SessionSharingRegistry()
    await _register(reg, "feishu_xxx", SubRole.GODVIEW, "feishu", "u1", "oc_group1")

    found = reg.lookup_by_container("feishu", "default", "oc_group1")
    assert found == [], f"GodView 不应参与容器去重, got {found}"


@pytest.mark.asyncio
async def test_lookup_by_container_still_finds_real_member_cross_session():
    """排除 GodView 后,同一容器跨 session 的真实 member 仍能被挡(去重约束保留)。

    用户用同一飞书群 chat_id 在 session_A /join 了 reviewer-1,又想在 session_B
    /join reviewer-2 → lookup_by_container 应命中 session_A 的真实 member 订阅。
    """
    reg = SessionSharingRegistry()
    await _register(reg, "session_A", "reviewer-1", "feishu", "u1", "oc_group1")
    # 同容器还有 GodView(不应干扰)
    await _register(reg, "session_A", SubRole.GODVIEW, "feishu", "u1", "oc_group1")

    found = reg.lookup_by_container("feishu", "default", "oc_group1")
    # 只命中真实 member,不含 GodView
    assert len(found) == 1
    sid, mname, _sub = found[0]
    assert sid == "session_A"
    assert mname == "reviewer-1"


@pytest.mark.asyncio
async def test_lookup_by_container_empty_container_id_returns_empty():
    reg = SessionSharingRegistry()
    await _register(reg, "s1", "reviewer-1", "feishu", "u1", "oc_group1")
    assert reg.lookup_by_container("feishu", "default", "") == []


# ---------- Fix 2: resolve_member_by_user 语义（/exit 正确性的基础） ----------
#
# /exit 不带 session_ref 时用 msg.session_id。这之所以正确，是因为 handle_message
# 在入队前调 resolve_member_by_user：已 /join 用户命中 → msg.session_id 被改写为
# 逻辑 session + metadata["member_name"] 写入。下面验证该反查返回的是逻辑 session
# （feishu_xxx）而非物理 chat_id（oc_xxx），且不命中 GodView。


@pytest.mark.asyncio
async def test_resolve_member_by_user_returns_logical_session_not_chat_id():
    """反查命中的 session_id 是逻辑 session(feishu_xxx),不是物理 chat_id(oc_xxx)。

    handle_message 据此把 msg.session_id 改写为逻辑 session，/exit 直接用即可。
    """
    reg = SessionSharingRegistry()
    # member 注册在逻辑 session feishu_xxx 下,delivery.chat_id=oc_xxx
    await _register(reg, "feishu_xxx", "reviewer-1", "feishu", "ou_user1", "oc_group1")

    resolved = reg.resolve_member_by_user("feishu", "default", "ou_user1", chat_id="oc_group1")
    assert resolved is not None
    sid, mname = resolved
    assert sid == "feishu_xxx", "反查应返回逻辑 session, 不是物理 chat_id oc_xxx"
    assert mname == "reviewer-1"


@pytest.mark.asyncio
async def test_resolve_member_by_user_skips_godview():
    """GodView 不被反查命中(否则会注入 member_name=GodView 触发 $GodView 前缀)。

    用户只注册了 GodView(没 /join 真实 member)→ 反查返回 None → handle_message
    不改写 msg.session_id（保持物理 chat_id）、不写 member_name → /exit 走"无需退出"。
    """
    reg = SessionSharingRegistry()
    await _register(reg, "feishu_xxx", SubRole.GODVIEW, "feishu", "ou_user1", "oc_group1")

    assert reg.resolve_member_by_user("feishu", "default", "ou_user1", chat_id="oc_group1") is None
    # 无 chat_id 兜底也不命中
    assert reg.resolve_member_by_user("feishu", "default", "ou_user1") is None


# ---------- Fix 3: lookup_by_identity（/exit team_name 一致性校验的基础） ----------
#
# /exit 带完整 session_ref 时，handler 用 lookup_by_identity 按
# (channel+app+user+session[+member]) 反查真实订阅，读 agent_ref.id 校验
# team_name。命中范围须与 unregister_all_for_identity 一致、跳过 GodView。


def _rk_team(channel_id: str, user_id: str, session_id: str, team_name: str) -> RoutingKey:
    """带指定 team_name 的 RoutingKey，用于 /exit team_name 校验测试。"""
    return RoutingKey(
        user_id=user_id,
        channel_id=channel_id,
        app_id="default",
        agent_ref=AgentRef("team", team_name),
        session_id=session_id,
    )


async def _register_team(
    reg: SessionSharingRegistry, session_id: str, member_name: str,
    channel_id: str, user_id: str, chat_id: str, team_name: str,
) -> None:
    rk = _rk_team(channel_id, user_id, session_id, team_name)
    dt = make_delivery_target(channel_id, chat_id=chat_id, physical_user_id=user_id)
    await reg.register(session_id, member_name, rk, dt)


@pytest.mark.asyncio
async def test_lookup_by_identity_finds_held_subscription_with_team_name():
    """/exit 校验基础：反查命中该用户在该 session 的真实订阅，可读 agent_ref.id。"""
    reg = SessionSharingRegistry()
    await _register_team(
        reg, "sess-1", "reviewer-1", "feishu", "ou_user1", "oc_group1",
        team_name="jiwen-team_sess-1",
    )

    held = reg.lookup_by_identity("feishu", "default", "ou_user1", session_id="sess-1")
    assert len(held) == 1
    sub = held[0]
    assert sub.member_name == "reviewer-1"
    assert sub.routing_key.agent_ref.mode == "team"
    assert sub.routing_key.agent_ref.id == "jiwen-team_sess-1"  # 校验 team_name 用


@pytest.mark.asyncio
async def test_lookup_by_identity_skips_godview():
    """GodView 订阅不参与 /exit 校验反查（与 unregister/resolve 语义一致）。"""
    reg = SessionSharingRegistry()
    await _register_team(
        reg, "sess-1", SubRole.GODVIEW, "feishu", "ou_user1", "oc_group1",
        team_name="jiwen-team_sess-1",
    )
    assert reg.lookup_by_identity("feishu", "default", "ou_user1", session_id="sess-1") == []


@pytest.mark.asyncio
async def test_lookup_by_identity_member_filter_and_wrong_user_miss():
    """指定 member_name 时只命中该 member；身份不符时返回空（→ /exit 报"未加入"）。"""
    reg = SessionSharingRegistry()
    await _register_team(
        reg, "sess-1", "reviewer-1", "feishu", "ou_user1", "oc_group1",
        team_name="jiwen-team_sess-1",
    )
    await _register_team(
        reg, "sess-1", "reviewer-2", "feishu", "ou_user2", "oc_group2",
        team_name="jiwen-team_sess-1",
    )
    # 指定 member=reviewer-1：只命中 ou_user1 那条
    held = reg.lookup_by_identity(
        "feishu", "default", "ou_user1", session_id="sess-1", member_name="reviewer-1",
    )
    assert len(held) == 1 and held[0].member_name == "reviewer-1"
    # member 名输错（reviewer-99）：不命中 → /exit 报 member 未加入
    assert reg.lookup_by_identity(
        "feishu", "default", "ou_user1", session_id="sess-1", member_name="reviewer-99",
    ) == []
    # 用户身份不符：不命中
    assert reg.lookup_by_identity(
        "feishu", "default", "ou_nobody", session_id="sess-1",
    ) == []
