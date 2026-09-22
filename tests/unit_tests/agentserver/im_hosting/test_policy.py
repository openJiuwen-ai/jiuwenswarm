from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.im.im_hosting.policy import HostingPolicyStore


def test_policy_defaults_and_patch(tmp_path: Path):
    store = HostingPolicyStore(tmp_path / "hosting.db")
    loaded = store.load()
    assert loaded["feishu"]["auto_host_groups"] is False
    assert loaded["feishu"]["auto_host_users"] is False
    assert loaded["feishu"]["auto_host_max_new"] == 20
    patched = store.patch_channel(
        "feishu",
        {"poll_interval_seconds": 30, "auto_host_users": True, "expert_persona": "## 人设\n同事口吻"},
    )
    assert patched["feishu"]["poll_interval_seconds"] == 30
    assert patched["feishu"]["auto_host_users"] is True
    assert patched["feishu"]["expert_persona"] == "## 人设\n同事口吻"
    assert patched["feishu"]["default_group_rule"]["match_mode"] == "keyword"
    assert patched["feishu"]["default_user_rule"]["match_mode"] == "relevant"
    assert patched["feishu"]["reply_enabled"] is True
    assert patched["dingtalk"]["poll_interval_seconds"] == 10
