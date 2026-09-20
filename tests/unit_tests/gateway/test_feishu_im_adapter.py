# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Feishu-specific user display-name resolution."""

from unittest.mock import patch

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_im_adapter import (
    FeishuIMPlatformAdapter,
)


def test_resolve_user_display_name_prefers_feishu_name() -> None:
    adapter = FeishuIMPlatformAdapter()

    with patch.object(adapter, "get_user_name_by_open_id", return_value=" 张三 "):
        assert adapter.resolve_user_display_name("ou_12345678") == "张三"


def test_resolve_user_display_name_falls_back_to_open_id_suffix() -> None:
    adapter = FeishuIMPlatformAdapter()

    assert (
        adapter.resolve_user_display_name("ou_12345678")
        == "Open ID 尾号是 5678 的用户"
    )
    assert adapter.resolve_user_display_name("") == ""
