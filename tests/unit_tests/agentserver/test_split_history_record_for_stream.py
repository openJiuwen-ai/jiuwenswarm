# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""split_history_record_for_stream 主路径测试（精简版）。

只覆盖三条核心断言：
1. 小 chat.final 不切片，单帧无 _part
2. 大 chat.final 按 32KB 切片，拼回后与原文一致
3. 非 chat.final（chat.tool_result / chat.reasoning）不切片，走旧 sanitize
"""
from __future__ import annotations

import json

from jiuwenswarm.server.wire_truncate import (
    _HISTORY_WIRE_RECORD_MAX_BYTES,
    split_history_record_for_stream,
)


def _wire_bytes(value: object) -> int:
    """模拟实际 wire 编码：ensure_ascii=False"""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def test_small_chat_final_returns_single_frame_without_part():
    """小 chat.final（≤64KB）单帧、无 _part，与旧协议兼容"""
    record = {
        "id": "msg-small",
        "role": "assistant",
        "event_type": "chat.final",
        "timestamp": 1.0,
        "content": "短回复",
    }
    chunks = split_history_record_for_stream(record)
    assert len(chunks) == 1
    assert "_part" not in chunks[0]
    assert chunks[0]["content"] == "短回复"


def test_large_chat_final_splits_and_reassembles_to_original():
    """大 chat.final（200KB）切片，_part 元数据正确，拼接后字节等于原文"""
    original_content = "α" * 200_000  # α 是 2 字节字符，共 400KB
    record = {
        "id": "msg-large",
        "role": "assistant",
        "event_type": "chat.final",
        "timestamp": 1.0,
        "content": original_content,
    }
    chunks = split_history_record_for_stream(record)
    assert len(chunks) > 1
    for chunk in chunks:
        # 每片都 ≤ 单条 record wire 预算
        assert _wire_bytes(chunk) <= _HISTORY_WIRE_RECORD_MAX_BYTES
        assert chunk["_part"]["record_id"] == "msg-large"
        assert chunk["_part"]["total_parts"] == len(chunks)
    # part_idx 是 [0..N-1] 的排列
    indices = sorted(chunk["_part"]["part_idx"] for chunk in chunks)
    assert indices == list(range(len(chunks)))
    # 按 idx 顺序拼回，字节与原文一致
    by_idx = {chunk["_part"]["part_idx"]: chunk["content"] for chunk in chunks}
    reassembled = "".join(by_idx[i] for i in sorted(by_idx))
    assert reassembled.encode("utf-8") == original_content.encode("utf-8")


def test_multi_byte_chinese_content_not_lost_at_slice_boundary():
    """中文（3 字节 UTF-8）+ 默认 32KB 切片：按字符切保证边界不丢字符。
    之前用 errors='ignore' 按字节切会丢字符，现在按字符切保证拼回与原文一致。"""
    # 混合中文（3字节）+ α（2字节）+ emoji（4字节），覆盖所有多字节宽度
    original = ("你好世界" * 10_000) + ("α" * 10_000) + ("🎉" * 1_000)
    record = {
        "id": "msg-multi-byte",
        "role": "assistant",
        "event_type": "chat.final",
        "timestamp": 1.0,
        "content": original,
    }
    chunks = split_history_record_for_stream(record)
    assert len(chunks) > 1
    for chunk in chunks:
        # 每片仍是合法 UTF-8（不靠 errors='ignore' 丢字符）
        chunk["content"].encode("utf-8")  # 不抛异常即合法
        assert _wire_bytes(chunk) <= _HISTORY_WIRE_RECORD_MAX_BYTES
    by_idx = {chunk["_part"]["part_idx"]: chunk["content"] for chunk in chunks}
    reassembled = "".join(by_idx[i] for i in sorted(by_idx))
    # 拼回必须等于原文——绝不能丢字符
    assert reassembled == original


def test_non_chat_final_large_record_not_split_uses_old_sanitize():
    """非 chat.final（chat.tool_result / chat.reasoning）超大 record 不切片，
    走旧 _sanitize_history_record_for_wire（content 字符串截断到 16KB+[truncated]）"""
    large_text = "x" * 100_000
    for event_type in ("chat.tool_result", "chat.reasoning"):
        record = {
            "id": f"{event_type}-large",
            "role": "assistant",
            "event_type": event_type,
            "timestamp": 1.0,
            "content": large_text,
        }
        chunks = split_history_record_for_stream(record)
        assert len(chunks) == 1, f"{event_type} 不应切片"
        assert "_part" not in chunks[0]
        assert chunks[0]["content"].endswith("[truncated]")
        assert _wire_bytes(chunks[0]) <= _HISTORY_WIRE_RECORD_MAX_BYTES
