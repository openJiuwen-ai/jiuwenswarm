# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""网页脱敏：开关关闭直出，打开后流式替换，历史里的用户原文不动。"""

from __future__ import annotations

import jiuwenswarm.gateway.channel_manager.web.sensitive_mask as mask


def _config(enabled: bool) -> dict:
    return {
        "sensitive_mask": {
            "enabled": enabled,
            "rules": [
                {"title": "API Key", "pattern": r"(?:sk|rk|pk|ak)-[A-Za-z0-9]{8,}"},
                {"title": "手机号", "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)"},
            ],
        }
    }


def test_switch_off_passes_text_through(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(False))
    mask.reset_sensitive_mask_state()
    payload = {"content": "密钥是sk-abcdef999", "request_id": "r"}
    mask.apply_web_event_mask("chat.delta", payload, "r")
    assert payload["content"] == "密钥是sk-abcdef999"


def test_stream_holds_key_until_delimiter(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    first = {"content": "密钥是sk-abc", "request_id": "r1"}
    second = {"content": "def999，请查收", "request_id": "r1"}
    mask.apply_web_event_mask("chat.delta", first, "r1")
    mask.apply_web_event_mask("chat.delta", second, "r1")
    assert first["content"] == "密钥是"
    assert second["content"] == "sk-＊＊＊＊f999，请查收"


def test_final_masks_complete_text(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    payload = {"content": "手机13812345678。", "request_id": "r2"}
    mask.apply_web_event_mask("chat.final", payload, "r2")
    assert payload["content"] == "手机138＊＊＊＊5678。"


def test_history_keeps_user_text_and_masks_assistant(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    records = mask.mask_history_records([
        {"role": "user", "content": "我的手机13812345678"},
        {"role": "assistant", "content": "收到13812345678"},
    ])
    assert records[0]["content"] == "我的手机13812345678"
    assert records[1]["content"] == "收到138＊＊＊＊5678"


def test_tool_result_masks_nested_raw_output(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    raw_output = {"result": "客户手机13812345678", "tool_name": "crm"}
    payload = {"raw_output": raw_output, "tool_name": "crm"}
    mask.apply_web_event_mask("chat.tool_result", payload, "r5")
    assert payload["raw_output"]["result"] == "客户手机138＊＊＊＊5678"
    assert raw_output["result"] == "客户手机13812345678"
    assert payload["tool_name"] == "crm"


def test_tool_result_is_masked(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    payload = {"result": "客户手机13812345678", "tool_name": "crm"}
    mask.apply_web_event_mask("chat.tool_result", payload, "r3")
    assert payload["result"] == "客户手机138＊＊＊＊5678"
    assert payload["tool_name"] == "crm"


def test_final_flushes_pending_tail_before_empty_final(monkeypatch) -> None:
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _config(True))
    mask.reset_sensitive_mask_state()
    first = {"content": "密钥是sk-abc", "request_id": "r4", "session_id": "s4"}
    second = {"content": "def99", "request_id": "r4", "session_id": "s4"}
    mask.apply_web_event_mask("chat.delta", first, "r4")
    mask.apply_web_event_mask("chat.delta", second, "r4")
    final = {"content": "", "request_id": "r4", "session_id": "s4"}
    tails = mask.pop_final_tail_frames(final, "r4")
    mask.apply_web_event_mask("chat.final", final, "r4")
    assert first["content"] == "密钥是"
    assert second["content"] == ""
    assert tails[0]["event"] == "chat.delta"
    assert tails[0]["payload"]["content"] == "sk-＊＊＊＊ef99"
    assert final["content"] == ""


def test_invalid_rule_rejected() -> None:
    try:
        mask.validate_sensitive_mask_rules([{"title": "坏", "pattern": "("}])
    except ValueError as exc:
        assert "无法编译" in str(exc)
    else:
        raise AssertionError("invalid pattern should be rejected")
