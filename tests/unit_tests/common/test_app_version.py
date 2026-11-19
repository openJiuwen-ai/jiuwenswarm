# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""common.app_version 版本判定单测。

AskUser 结构化选项卡按手机端 app_ver（systemVariables.app_ver，四段如
"11.7.10.900"）开关：>= 门限 ASK_USER_TABS_MIN_APP_VER（完整元组逐段
比较）开启。门限为 11.7.10.208（11.7.10 线第 208 构建起支持契约）：
11.7.10.208（含等号）与 11.7.10.209+/11.7.11+ 开启；11.7.10.207 及更早
（含裸三段 "11.7.10"/"11.7.9"）/ 未上报 / 非法格式均 fail-closed 关闭。
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
        ("11.7.10.208", True),  # 门限等号（含等号边界）
        ("11.7.10.208.beta1", True),  # 预发按数字前缀判定，解析为 (11,7,10,208)
        ("11.7.10.209", True),  # 边界上方构建号
        ("11.7.10.900", True),  # 端侧当前实测版本
        ("11.7.11.100", True),
        ("11.7.11", True),
        ("12.0.0.300", True),
        ("99.99.99", True),  # 合成高版本仍放行
        ("100.0.0.100", True),
        ("11.7.10.207", False),  # 门限等号下方（边界-1）
        ("11.7.10.200", False),  # 低于门限构建
        ("11.7.10", False),  # 裸三段是门限元组的前缀，比较小于四段门限
        ("11.7.9.301", False),  # 旧门限，现已低于新门限
        ("11.7.9.301.beta1", False),
        ("11.7.9.302", False),
        ("11.7.9.300", False),
        ("11.7.9.200", False),  # 11.7.9 线更早构建
        ("11.7.9", False),  # 裸三段是门限元组的前缀
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
    """最低版本常量锁定 11.7.10.208（含等号，元组比较）。

    11.7.10 线第 208 构建起端侧支持选项卡契约：门限等号与
    11.7.10.209+/11.7.11+ 开启；11.7.10.207 及更早（含裸三段
    "11.7.10"/"11.7.9"）与未上报/非法格式 fail-closed。
    """
    assert ASK_USER_TABS_MIN_APP_VER == (11, 7, 10, 208)
    assert xiaoyi_ask_user_tabs_supported("11.7.10.208")
    assert xiaoyi_ask_user_tabs_supported("11.7.10.208.beta1")
    assert xiaoyi_ask_user_tabs_supported("11.7.10.900")
    assert xiaoyi_ask_user_tabs_supported("12.0.0.300")
    assert not xiaoyi_ask_user_tabs_supported("11.7.10.207")
    assert not xiaoyi_ask_user_tabs_supported("11.7.10.200")
    assert not xiaoyi_ask_user_tabs_supported("11.7.9.301")
    assert not xiaoyi_ask_user_tabs_supported("11.7.10")
    assert not xiaoyi_ask_user_tabs_supported("")
