# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``PendingInteraction`` 落盘前的敏感键过滤。

两个方向都要守住：凭证形状的键必须丢弃，``tokenizer_*`` / ``token_count``
这类正常元数据必须原样保留。过度匹配是用一个新缺陷换掉旧缺陷。
"""

import json

import pytest

from jiuwenswarm.gateway.routing import interaction_context
from jiuwenswarm.gateway.routing.interaction_context import (
    PendingInteraction,
    _filter_sensitive,
)

# 最后一个词段是凭证中心词 —— 必须丢弃。
SENSITIVE_KEYS = [
    "token",
    "api_key",
    "authorization",
    "Authorization",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "bot_token",
    "app_token",
    "accessToken",
    "apiKey",
    "x-api-key",
    "app_secret",
    "client_secret",
    "secret",
    "password",
    "passwd",
    "credentials",
    "private_key",
    "signature",
    "bearer",
]

# 正常元数据 —— 必须原样保留。
BENIGN_KEYS = [
    # 本仓库真实存在的 tokenizer 配置键。
    "tokenizer_cache_dir",
    "tokenizer_offline",
    "enable_tiktoken_counter",
    "tokenizer_id",
    "tokenizerCacheDir",
    # LLM 计数类：token 是限定词，不是中心词。
    "token_count",
    "token_usage",
    "token_counter",
    "token_budget",
    "tokens_used",
    "total_tokens",
    "prompt_tokens",
    "token_expires_at",
    # ``key`` 单独出现过宽，必须带一个凭证限定词才算。
    "cache_key",
    "route_key",
    # 追问记录赖以路由的身份与上下文字段。
    "im_sender_user_id",
    "open_id",
    "sender_id",
    "chat_type",
    "avatar_original_query",
    "reply_target_name",
]


@pytest.mark.parametrize("key", SENSITIVE_KEYS)
def test_credential_shaped_keys_are_dropped(key):
    assert _filter_sensitive({key: "s3cr3t", "chat_type": "group"}) == {
        "chat_type": "group"
    }


@pytest.mark.parametrize("key", BENIGN_KEYS)
def test_benign_keys_are_preserved(key):
    assert _filter_sensitive({key: "value"}) == {key: "value"}


def test_filter_accepts_non_string_keys():
    """键名不是字符串时不得抛异常。``save()`` 在自己的 try 之外建这个 dict。"""
    assert _filter_sensitive({1: "a", "token_count": 7}) == {1: "a", "token_count": 7}


def test_save_omits_credentials_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(interaction_context, "get_interactions_dir", lambda: tmp_path)

    pending = PendingInteraction(
        interaction_id="gpq_sess_u1",
        mode="group",
        origin_channel_id="C1",
        origin_session_id="sess",
        origin_content="原始请求",
        origin_sender_name="张三",
        origin_sender_id="u1",
        question="哪一天？",
        target_user_id="u1",
        origin_metadata={
            "bot_token": "must-not-persist-1",
            "app_secret": "must-not-persist-2",
            "chat_type": "group",
            "tokenizer_cache_dir": "/var/cache/tok",
            "token_count": 128,
        },
    )
    pending.save()

    raw = (tmp_path / "gpq_sess_u1.json").read_text(encoding="utf-8")
    assert "must-not-persist" not in raw

    stored = json.loads(raw)["origin_metadata"]
    assert stored == {
        "chat_type": "group",
        "tokenizer_cache_dir": "/var/cache/tok",
        "token_count": 128,
    }
