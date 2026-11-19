# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""离线场景下精确 token 计数的降级行为。

出厂配置默认开启 ``enable_tiktoken_counter``，但企业内网 / 桌面端可能既无外网
也无预置词表。此时 ``TiktokenCounter`` 构造必须降级为字符估算，而不是让会话
创建失败或无限等待——这条路径是开启默认值的前提。
"""

import sys
import types

from openjiuwen.core.context_engine.token.encode_guard import estimate_tokens
from openjiuwen.core.context_engine.token.tiktoken_counter import TiktokenCounter


def test_counter_falls_back_when_vocabulary_unavailable(monkeypatch):
    """词表拉取失败（离线 + 无缓存）时构造不抛错，计数回退字符估算。"""

    def _offline(*_args, **_kwargs):
        raise OSError("offline: cannot fetch BPE vocabulary")

    fake_tiktoken = types.ModuleType("tiktoken")
    fake_tiktoken.get_encoding = _offline
    fake_tiktoken.encoding_for_model = _offline
    monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)

    counter = TiktokenCounter(model="gpt-4")

    assert counter._enc is None
    assert counter.measurement_source == "string_length_fallback"
    text = "双碳目标下的能源转型与产业机遇"
    assert counter.count(text) == estimate_tokens(text)
