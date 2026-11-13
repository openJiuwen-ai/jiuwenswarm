# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""common.app_version 版本判定单测。

AskUser 结构化选项卡按手机端 app_ver（systemVariables.app_ver，四段如
"11.7.10.900"）开关：>= 门限 ASK_USER_TABS_MIN_APP_VER（完整元组逐段
比较）开启。当前门限收紧为 99.99.99——现网不存在达到该版本的端侧，
等于全网关闭选项卡出口、统一文本回退（临时口径）；11.7.x 各版本
（含 11.7.9.301 内部版）/ 未上报 / 非法格式均 fail-closed 关闭。
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
        ("99.99.99", True),  # 门限等号（裸三段，含等号边界）
        ("99.99.99.0", True),  # 构建号 0 的四段形态
        ("99.99.99.200", True),  # 端侧实际四段格式
        ("99.99.99.beta1", True),  # 预发版本按数字前缀判定，解析为 (99,99,99)
        ("99.99.100", True),
        ("100.0.0", True),
        ("100.0.0.100", True),  # 端侧实际四段格式
        ("100.0.0.beta1", True),  # 预发版本按数字前缀判定
        ("101.8.0.1", True),
        ("12.0.0.300", False),  # 现网版本均在 99.99.99 门限之下
        ("11.7.11", False),  # 历史门限版本，收紧后不放行
        ("11.7.11.100", False),  # 端侧实际四段格式，收紧后不放行
        ("11.7.11.beta1", False),  # 预发版本按数字前缀判定
        ("11.7.10", False),  # 上一版门限版本（含等号语义），收紧后不放行
        ("11.7.10.200", False),  # 端侧实际四段格式，收紧后不放行
        ("11.7.10.900", False),  # 端侧当前实测版本，收紧后不放行
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
    """最低版本常量锁定 99.99.99（含等号，元组比较）。

    现网不存在达到该版本的端侧：全网 fail-closed 关闭选项卡、统一文本
    回退（临时口径）；仅合成版本（>= 99.99.99）可开启，用于验证比较
    语义本身。
    """
    assert ASK_USER_TABS_MIN_APP_VER == (99, 99, 99)
    assert xiaoyi_ask_user_tabs_supported("99.99.99")
    assert xiaoyi_ask_user_tabs_supported("99.99.99.200")
    assert xiaoyi_ask_user_tabs_supported("100.0.0")
    assert not xiaoyi_ask_user_tabs_supported("11.7.10.900")
    assert not xiaoyi_ask_user_tabs_supported("11.7.11.100")
    assert not xiaoyi_ask_user_tabs_supported("11.7.9.301")
    assert not xiaoyi_ask_user_tabs_supported("11.7.10")
