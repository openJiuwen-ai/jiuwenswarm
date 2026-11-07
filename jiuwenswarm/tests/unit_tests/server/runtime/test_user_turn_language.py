from __future__ import annotations

import json
import sys
import types

if "gateway_protocol.sdk.crypto_utility" not in sys.modules:
    _gateway = types.ModuleType("gateway_protocol")
    _gateway.__path__ = []
    _sdk = types.ModuleType("gateway_protocol.sdk")
    _sdk.__path__ = []
    _crypto = types.ModuleType("gateway_protocol.sdk.crypto_utility")

    class _CryptoProvider:
        pass

    _crypto.CryptoProvider = _CryptoProvider
    _sdk.crypto_utility = _crypto
    sys.modules.setdefault("gateway_protocol", _gateway)
    sys.modules.setdefault("gateway_protocol.sdk", _sdk)
    sys.modules.setdefault("gateway_protocol.sdk.crypto_utility", _crypto)

from jiuwenswarm.server.runtime.agent_adapter.user_turn import (
    HOST_USER_PROMPT_PREFIX_EN,
    HOST_USER_PROMPT_PREFIX_ZH,
    UserTurn,
)

_QUERY = "使用subagent查询上海和杭州的天气"


def _turn(language: str) -> UserTurn:
    return UserTurn(
        text=_QUERY,
        channel="web",
        language=language,
        files={},
    )


def _assert_rendered_turn(language: str, prefix: str, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.user_turn._handle_skills_use_slash_command",
        lambda content: ([], content),
    )
    rendered = _turn(language).render()
    assert rendered.startswith(prefix)
    assert json.loads(rendered.removeprefix(prefix))["content"] == _QUERY


def test_user_turn_zh_keeps_chinese_prefix_and_content(monkeypatch) -> None:
    _assert_rendered_turn("zh", HOST_USER_PROMPT_PREFIX_ZH, monkeypatch)


def test_user_turn_cn_uses_chinese_prefix_and_keeps_content(monkeypatch) -> None:
    _assert_rendered_turn("cn", HOST_USER_PROMPT_PREFIX_ZH, monkeypatch)


def test_user_turn_en_keeps_english_prefix_and_content(monkeypatch) -> None:
    _assert_rendered_turn("en", HOST_USER_PROMPT_PREFIX_EN, monkeypatch)
