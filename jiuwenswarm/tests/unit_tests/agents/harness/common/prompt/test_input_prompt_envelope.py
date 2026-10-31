from __future__ import annotations

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

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import _input_prompt
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


def test_input_prompt_en_lists_live_envelope_keys_and_binds_the_task_to_content(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.user_turn._handle_skills_use_slash_command",
        lambda content: ([], content),
    )
    turn = UserTurn(
        text="使用subagent查询上海和杭州的天气",
        channel="web",
        language="en",
        files={},
    )
    envelope = turn._build_envelope(turn.text, "web", origin_kind=turn.origin_kind)
    text = _input_prompt("en").content["en"]

    for key in envelope:
        assert f'"{key}"' in text
    assert '"channel"' not in text
    assert "only the string in `content`" in text
    assert "format illustration" in text
