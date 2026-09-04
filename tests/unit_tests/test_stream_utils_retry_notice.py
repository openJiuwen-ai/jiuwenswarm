# coding: utf-8
"""stream_utils.is_retry_notice_payload：重试通知判定（标记优先、文案兜底）。"""

from jiuwenswarm.server.utils.stream_utils import is_retry_notice_payload


def test_structured_flag_wins_regardless_of_wording():
    # 文案完全不含关键词，有标记即算通知（rail 改文案不击穿过滤链）
    assert is_retry_notice_payload({"error": "totally reworded", "retry_notice": True}) is True


def test_text_fallback_for_legacy_framework():
    assert is_retry_notice_payload({"error": "模型调用异常，将在 0.5 秒后进行第 1 次重试（共 2 次）"}) is True


def test_real_error_not_a_notice():
    assert is_retry_notice_payload({"error": "[181001] model call failed, reason: boom"}) is False
    assert is_retry_notice_payload({"error": "重试策略配置说明见文档"}) is False  # 缺"模型调用异常"
    assert is_retry_notice_payload(None) is False
    assert is_retry_notice_payload({}) is False
