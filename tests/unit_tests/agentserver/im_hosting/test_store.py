from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.im.im_hosting.store import HostingStore


def test_add_list_and_unique_target(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    first = store.add_target(
        channel_id="feishu",
        target_kind="group",
        external_id="oc_1",
        title="测试群",
    )
    again = store.add_target(
        channel_id="feishu",
        target_kind="group",
        external_id="oc_1",
        title="测试群",
    )
    assert first["id"] == again["id"]
    listed = store.list_targets("feishu")
    assert len(listed) == 1
    assert listed[0]["title"] == "测试群"


def test_add_target_stores_rule_override(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="feishu",
        target_kind="group",
        external_id="oc_rule",
        title="规则群",
        rule_override={"match_mode": "keyword", "keywords": "入职,报销"},
    )
    assert target["rule_override"]["match_mode"] == "keyword"
    assert target["rule_override"]["keywords"] == ["入职", "报销"]


def test_add_target_stores_expert(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="feishu",
        target_kind="user",
        external_id="ou_exp",
        title="许康",
        expert_service_id="service_default",
        expert_agent_id="agent_default",
    )
    assert target["expert_service_id"] == "service_default"
    assert target["expert_agent_id"] == "agent_default"
    updated = store.patch_target(
        target["id"],
        {"expert_agent_id": "agent_hr"},
    )
    assert updated is not None
    assert updated["expert_agent_id"] == "agent_hr"
    named = store.add_target(
        channel_id="feishu",
        target_kind="user",
        external_id="ou_persona",
        title="许康",
        expert_persona="## 人设\n- 语气：简短\n\n## 职责\n- 代回事务咨询",
    )
    assert named["expert_persona"] == "## 人设\n- 语气：简短\n\n## 职责\n- 代回事务咨询"


def test_claim_reply_turn_is_idempotent(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    assert store.claim_reply_turn("feishu", "group", "oc_1", "m1") is True
    assert store.claim_reply_turn("feishu", "group", "oc_1", "m1") is False


def test_reenable_clears_watermark(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="feishu",
        target_kind="user",
        external_id="ou_1",
        title="许康",
    )
    store.set_watermark("feishu", "user", "ou_1", last_processed_at_ms=100, last_processed_msg_id="m1")
    store.patch_target(target["id"], {"enabled": False})
    updated = store.patch_target(target["id"], {"enabled": True})
    assert updated is not None
    assert updated["enabled"] is True
    assert updated["hosting_since_ms"] >= target["hosting_since_ms"]
    assert store.get_watermark("feishu", "user", "ou_1") is None


def test_drop_auto_targets_keeps_manual(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    auto_group = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_auto",
        title="吃饭群",
        source="auto",
    )
    auto_user = store.add_target(
        channel_id="dingtalk",
        target_kind="user",
        external_id="ou_auto",
        title="许康",
        source="auto",
    )
    manual = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_manual",
        title="手选群",
        source="manual",
    )
    dropped = store.drop_auto_targets("dingtalk", kinds=["group", "user"])
    assert dropped == 2
    left = store.list_targets("dingtalk")
    assert [row["id"] for row in left] == [manual["id"]]
    assert store.get_target(auto_group["id"]) is None
    assert store.get_target(auto_user["id"]) is None


def test_release_target_stays_in_hosted_keys_until_manual_add(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_eat",
        title="吃饭群",
        source="auto",
    )
    assert store.release_target(target["id"]) is True
    released = store.get_target(target["id"])
    assert released is not None
    assert released["enabled"] is False
    assert ("group", "cid_eat") in store.hosted_keys("dingtalk")
    revived = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_eat",
        title="吃饭群",
        source="manual",
    )
    assert revived["id"] == target["id"]
    assert revived["enabled"] is True
    assert revived["source"] == "manual"
