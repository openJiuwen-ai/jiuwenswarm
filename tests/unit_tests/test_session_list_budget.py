"""session.list 单页字节预算收缩（_fit_sessions_page_bytes）单测。"""
import json

import pytest

from jiuwenswarm.server.agent_ws_server import (
    _fit_sessions_page_bytes,
    _skeleton_session_row,
)


def _row(i: int, pad: int = 0) -> dict:
    return {
        "session_id": f"desktop_test_{i:04d}",
        "title": f"会话标题 {i}" + "长" * pad,
        "channel_id": "desktop",
        "message_count": i,
    }


class TestFitSessionsPageBytes:
    def test_small_page_untouched(self):
        rows = [_row(i) for i in range(5)]
        out = _fit_sessions_page_bytes(rows, budget=4 * 1024 * 1024)
        assert out is rows
        assert len(out) == 5

    def test_oversized_page_shrinks_to_budget(self):
        rows = [_row(i, pad=1500) for i in range(3000)]
        budget = 512 * 1024
        out = _fit_sessions_page_bytes(rows, budget=budget)
        assert 1 <= len(out) < 3000
        size = len(json.dumps({"sessions": out}, ensure_ascii=False).encode("utf-8"))
        assert size <= budget
        # 收缩保序：保留的是前缀行（offset 分页语义不被破坏）
        assert out[0]["session_id"] == rows[0]["session_id"]

    def test_never_returns_empty_for_nonempty_input(self):
        # 单行就超预算（病态场景）：floor 1 行，绝不允许空页——空页会被客户端误判为末页
        rows = [_row(0, pad=200000)]
        out = _fit_sessions_page_bytes(rows, budget=1024)
        assert len(out) == 1

    def test_empty_input_passthrough(self):
        assert _fit_sessions_page_bytes([], budget=1024) == []

    def test_realistic_full_page_no_shrink(self):
        # 真实量级：2000 行 × ~2KB ≈ 4MB 贴线，不收缩；500 行轻松通过
        rows = [_row(i, pad=300) for i in range(500)]
        out = _fit_sessions_page_bytes(rows)
        assert out is rows

    def test_shrink_converges_quickly(self):
        # 比例收缩应在常数轮内收敛（不退化成逐行循环）
        rows = [_row(i, pad=1500) for i in range(5000)]
        out = _fit_sessions_page_bytes(rows, budget=256 * 1024)
        assert 1 <= len(out) < 5000
        size = len(json.dumps({"sessions": out}, ensure_ascii=False).encode("utf-8"))
        assert size <= 256 * 1024

    def test_single_row_over_budget_degrades_to_skeleton(self):
        # 病态场景：单行自身超预算（异常大嵌套字段）——floor=1 也发不出去，
        # 必须降级为骨架行而不是把 response_too_large 留给客户端
        giant = {
            "session_id": "desktop_giant_001",
            "title": "正常标题",
            "channel_id": "desktop",
            "channel_metadata": {"blob": "x" * (5 * 1024 * 1024)},
        }
        out = _fit_sessions_page_bytes([giant], budget=512 * 1024)
        assert len(out) == 1
        size = len(json.dumps({"sessions": out}, ensure_ascii=False).encode("utf-8"))
        assert size <= 512 * 1024
        assert out[0]["session_id"] == "desktop_giant_001"
        assert out[0]["title"] == "正常标题"
        assert "channel_metadata" not in out[0]

    def test_skeleton_truncates_oversized_string_fields(self):
        row = {
            "session_id": "desktop_s",
            "title": "t" * 5000,
            "channel_id": "desktop",
            "mode": "agent",
            "status": "active",
            "pinned": True,
            "history_text": "h" * 100000,
        }
        sk = _skeleton_session_row(row)
        assert sk["session_id"] == "desktop_s"
        assert len(sk["title"]) == 512
        assert sk["pinned"] is True
        assert len(sk["history_text"]) == 512
        assert sk["mode"] == "agent" and sk["status"] == "active"

    def test_skeleton_keeps_row_count_semantics(self):
        # 病态行在页首的混排：比例收缩一路裁到 floor=1（仍超预算）→ 骨架化，
        # 行数语义不变（不丢行位、不产生空页），total/offset 对账不受影响
        rows = [{"session_id": "desktop_giant", "channel_metadata": {"b": "y" * (2 * 1024 * 1024)}}] + [
            _row(i, pad=1500) for i in range(300)
        ]
        budget = 512 * 1024
        out = _fit_sessions_page_bytes(rows, budget=budget)
        assert len(out) == 1
        size = len(json.dumps({"sessions": out}, ensure_ascii=False).encode("utf-8"))
        assert size <= budget
        assert out[0]["session_id"] == "desktop_giant"
        assert "channel_metadata" not in out[0]
