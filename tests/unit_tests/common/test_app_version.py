# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""common.app_version 版本判定单测。

AskUser 结构化选项卡按手机端 app_ver（systemVariables.app_ver，四段如
"11.7.10.900"）开关：>= 11.7.10（完整元组逐段比较）开启——裸 "11.7.10"
与带构建号的四段版本（11.7.10.200/.900）及 11.7.11 起弹框；11.7.9.x
及更低（含 11.7.9.301 内部版）/ 未上报 / 非法格式 fail-closed 关闭。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.app_version import (
    ASK_USER_TABS_MIN_APP_VER,
    parse_version_tuple,
    xiaoyi_ask_user_tabs_supported,
)


@pytest.mark.parametrize(
    "app_ver,expected",
    [
        ("11.7.10", True),  # 最低支持版本（裸三段，含等号边界）
        ("11.7.10.0", True),  # 构建号 0 的四段形态
        ("11.7.10.200", True),  # 端侧实际四段格式
        ("11.7.10.301", True),
        ("11.7.10.900", True),  # 端侧当前实测版本
        ("11.7.10.beta1", True),  # 预发版本按数字前缀判定，解析为 (11,7,10)
        ("11.7.11", True),
        ("11.7.11.100", True),  # 端侧实际四段格式
        ("11.7.11.beta1", True),  # 预发版本按数字前缀判定
        ("11.8.0.1", True),
        ("12.0.0.300", True),
        ("11.7.9.301", False),  # 11.7.9 内部版本不放行
        ("11.7.9.300", False),
        ("11.7.9", False),
        ("11.7.8.999", False),
        ("11.6.30.100", False),
        ("", False),  # 未上报：fail-closed
        ("unknown", False),  # 非法格式：fail-closed
        ("11", False),  # 段数不足
        ("abc.7.10", False),
    ],
)
def test_xiaoyi_ask_user_tabs_supported(app_ver, expected):
    assert xiaoyi_ask_user_tabs_supported(app_ver) is expected


@pytest.mark.parametrize(
    "app_ver,expected",
    [
        ("11.7.10.200", (11, 7, 10, 200)),
        ("11.7.10.900", (11, 7, 10, 900)),
        ("11.7.10.beta1", (11, 7, 10)),
        ("11.7.9.301", (11, 7, 9, 301)),
        ("2.0", (2, 0)),
        ("", None),
        (None, None),
        ("beta", None),
        ("11.7.x", (11, 7)),  # 非数字段截断，保留数字前缀
    ],
)
def test_parse_version_tuple(app_ver, expected):
    assert parse_version_tuple(app_ver) == expected


def test_min_app_ver_constant():
    """最低版本常量锁定 11.7.10（含等号，元组比较）。"""
    assert ASK_USER_TABS_MIN_APP_VER == (11, 7, 10)
    assert xiaoyi_ask_user_tabs_supported("11.7.10")
    assert xiaoyi_ask_user_tabs_supported("11.7.10.200")
    assert xiaoyi_ask_user_tabs_supported("11.7.10.900")
    assert xiaoyi_ask_user_tabs_supported("11.7.11")
    assert not xiaoyi_ask_user_tabs_supported("11.7.9.301")
    assert not xiaoyi_ask_user_tabs_supported("11.7.9")
