# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""Tests for session_history buffer layer (merge + pending + flush).

- merge functions (delta/reasoning/tool_update/tool_calls.delta) + field preservation
- tool_calls.delta pending mechanism (conditions A/B, nested id, order)
- normal buffer layer flush (type switch / request switch / capacity / degrade)
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from jiuwenswarm.server.runtime.session import session_history as sh


# ──────────────────────── fixtures ────────────────────────

def _stop_flush_thread():
    """停掉可能残留的 flush 线程，避免跨测试并发跑 _periodic_flush 污染断言。

    有些测试通过 append_history_record 间接起线程后不调 shutdown，需在 fixture 兜底停掉。
    """
    sh._flush_stop_event.set()
    t = getattr(sh, "_FLUSH_THREAD", None)
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=2.0)
    sh._flush_stop_event.clear()


class TestStripSkillEnvFromHistoryItem:
    """P1 修复配套：persist 侧 env 剥离固化（tool_call/tool_calls/arguments 三形态）。"""

    def test_tool_call_dict_arguments_stripped(self):
        item = {
            "tool_call": {
                "name": "bash",
                "arguments": {"command": "echo hi", "env": {"EM_API_KEY": "k"}},
            }
        }
        sh._strip_skill_env_from_history_item(item)
        assert "env" not in item["tool_call"]["arguments"]
        assert item["tool_call"]["arguments"]["command"] == "echo hi"

    def test_tool_calls_list_stripped(self):
        item = {
            "tool_calls": [
                {"name": "bash", "arguments": {"env": {"EM_API_KEY": "k"}}},
                {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"command": "x", "env": {"EM_API_KEY": "k"}}
                    ),
                },
            ]
        }
        sh._strip_skill_env_from_history_item(item)
        assert "env" not in item["tool_calls"][0]["arguments"]
        assert "env" not in json.loads(item["tool_calls"][1]["arguments"])

    def test_item_level_arguments_stripped(self):
        item = {"arguments": json.dumps({"env": {"EM_API_KEY": "k"}, "a": 1})}
        sh._strip_skill_env_from_history_item(item)
        assert "env" not in json.loads(item["arguments"])

    def test_no_env_untouched(self):
        item = {"arguments": {"command": "echo hi"}}
        snapshot = json.dumps(item, sort_keys=True)
        sh._strip_skill_env_from_history_item(item)
        assert json.dumps(item, sort_keys=True) == snapshot


@pytest.fixture(autouse=True)
def _reset_buffer_state():
    """每个测试前后清空全局缓冲状态，避免互相污染。"""
    _stop_flush_thread()
    sh._session_buffer.clear()
    sh._session_buffer_type.clear()
    sh._session_buffer_request_id.clear()
    sh._session_buffer_root.clear()
    sh._session_tool_update_buffer.clear()
    sh._session_tool_update_root.clear()
    sh._session_pending.clear()
    sh._flush_stop_event.clear()
    sh._FLUSH_THREAD_STARTED = False
    sh._FLUSH_THREAD = None
    sh._SHUTDOWN_DONE = False
    yield
    _stop_flush_thread()
    sh._session_buffer.clear()
    sh._session_buffer_type.clear()
    sh._session_buffer_request_id.clear()
    sh._session_buffer_root.clear()
    sh._session_tool_update_buffer.clear()
    sh._session_tool_update_root.clear()
    sh._session_pending.clear()
    sh._flush_stop_event.clear()
    sh._FLUSH_THREAD_STARTED = False
    sh._FLUSH_THREAD = None
    sh._SHUTDOWN_DONE = False


def _tmp_sessions_root() -> str:
    return tempfile.mkdtemp()


def _read_history(sid: str, root: str) -> list[dict]:
    path = Path(root) / sid / "history.json"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_delta(req: str, content: str, ts: float = 1.0) -> dict:
    return {"request_id": req, "event_type": "chat.delta", "content": content, "timestamp": ts,
            "id": f"{req}:assistant", "role": "assistant", "session_id": "s1"}


def _make_reasoning(req: str, content: str, ts: float = 1.0) -> dict:
    return {"request_id": req, "event_type": "chat.reasoning", "content": content, "timestamp": ts,
            "id": f"{req}:assistant", "role": "assistant", "session_id": "s1"}


def _make_tool_update(req: str, call_id: str, status: str, ts: float = 1.0) -> dict:
    return {"request_id": req, "event_type": "chat.tool_update", "tool_call_id": call_id,
            "tool_name": "t", "arguments": "{}", "status": status, "timestamp": ts,
            "id": f"{req}:assistant", "role": "assistant", "session_id": "s1"}


def _make_tool_calls_delta(req: str, calls: list[dict], ts: float = 1.0) -> dict:
    return {"request_id": req, "event_type": "chat.tool_calls.delta", "tool_calls": calls,
            "timestamp": ts, "id": f"{req}:assistant", "role": "assistant", "session_id": "s1"}


def _make_tool_call(req: str, call_id: str, ts: float = 1.0) -> dict:
    return {"request_id": req, "event_type": "chat.tool_call",
            "tool_call": {"name": "t", "arguments": "{}", "tool_call_id": call_id},
            "content": "", "timestamp": ts, "id": f"{req}:assistant", "role": "assistant",
            "session_id": "s1"}


# ════════════════════════════════════════════════════════
# 合并函数测试
# ════════════════════════════════════════════════════════

class TestMergeDeltaEvents:
    @staticmethod
    def test_merge_two_deltas():
        a = _make_delta("r1", "我来", ts=1.0)
        b = _make_delta("r1", "为您", ts=2.0)
        merged = sh._merge_delta_events(a, b)
        assert merged["content"] == "我来为您"
        assert merged["delta_count"] == 2

    @staticmethod
    def test_merge_multiple_deltas():
        a = _make_delta("r1", "a", ts=1.0)
        merged = a
        for i, c in enumerate("bcd", start=2):
            merged = sh._merge_delta_events(merged, _make_delta("r1", c, ts=float(i)))
        assert merged["content"] == "abcd"
        assert merged["delta_count"] == 4

    @staticmethod
    def test_merge_preserves_start_and_end_ts():
        a = _make_delta("r1", "x", ts=1.0)
        b = _make_delta("r1", "y", ts=5.0)
        merged = sh._merge_delta_events(a, b)
        assert math.isclose(merged["start_ts"], 1.0)
        assert math.isclose(merged["timestamp"], 5.0)


class TestMergeReasoningEvents:
    @staticmethod
    def test_merge_two_reasonings():
        a = _make_reasoning("r1", "The")
        b = _make_reasoning("r1", " user")
        merged = sh._merge_reasoning_events(a, b)
        assert merged["content"] == "The user"
        assert merged["delta_count"] == 2


class TestMergeToolUpdateEvents:
    @staticmethod
    def test_merge_status_takes_latest():
        a = _make_tool_update("r1", "c1", "in_progress")
        b = _make_tool_update("r1", "c1", "failed")
        merged = sh._merge_tool_update_events(a, b)
        assert merged["status"] == "failed"

    @staticmethod
    def test_merge_arguments_overwrite():
        a = _make_tool_update("r1", "c1", "in_progress")
        a["arguments"] = '{"a":1}'
        b = _make_tool_update("r1", "c1", "failed")
        b["arguments"] = '{"a":2}'
        merged = sh._merge_tool_update_events(a, b)
        assert merged["arguments"] == '{"a":2}'  # 覆盖而非拼接

    @staticmethod
    def test_merge_same_call_id_status_latest():
        a = _make_tool_update("r1", "c1", "in_progress")
        b = _make_tool_update("r1", "c1", "failed")
        merged = sh._merge_tool_update_events(a, b)
        assert merged["tool_call_id"] == "c1"
        assert merged["status"] == "failed"


class TestMergeToolCallsDeltaEvents:
    @staticmethod
    def test_merge_by_id():
        a = _make_tool_calls_delta("r1", [
            {"id": "call_1", "tool_call_id": "call_1", "type": "function", "name": "t", "arguments": "{", "index": 0}
        ])
        b = _make_tool_calls_delta("r1", [
            {"id": "", "tool_call_id": "", "type": "", "name": "", "arguments": '"k":1}', "index": 0}
        ])
        merged = sh._merge_tool_calls_delta_events(a, b)
        assert len(merged["tool_calls"]) == 1
        call = merged["tool_calls"][0]
        assert call["id"] == "call_1"
        assert call["arguments"] == '{"k":1}'

    @staticmethod
    def test_merge_empty_id_by_index():
        a = _make_tool_calls_delta("r1", [
            {"id": "", "tool_call_id": "", "type": "", "name": "t", "arguments": "{", "index": 0}
        ])
        b = _make_tool_calls_delta("r1", [
            {"id": "", "tool_call_id": "", "type": "", "name": "", "arguments": "}", "index": 0}
        ])
        merged = sh._merge_tool_calls_delta_events(a, b)
        assert len(merged["tool_calls"]) == 1
        assert merged["tool_calls"][0]["arguments"] == "{}"

    @staticmethod
    def test_merge_multiple_parallel_calls():
        a = _make_tool_calls_delta("r1", [
            {"id": "c0", "tool_call_id": "c0", "type": "function", "name": "t0", "arguments": "{", "index": 0},
            {"id": "c1", "tool_call_id": "c1", "type": "function", "name": "t1", "arguments": "{", "index": 1},
        ])
        b = _make_tool_calls_delta("r1", [
            {"id": "", "tool_call_id": "", "type": "", "name": "", "arguments": "0}", "index": 0},
            {"id": "", "tool_call_id": "", "type": "", "name": "", "arguments": "1}", "index": 1},
        ])
        merged = sh._merge_tool_calls_delta_events(a, b)
        assert len(merged["tool_calls"]) == 2
        by_idx = {c["index"]: c for c in merged["tool_calls"]}
        assert by_idx[0]["arguments"] == "{0}"
        assert by_idx[1]["arguments"] == "{1}"

    @staticmethod
    def test_merge_preserves_source():
        a = _make_tool_calls_delta("r1", [{"id": "c0", "index": 0, "arguments": ""}])
        a["source"] = "llm_stream"
        b = _make_tool_calls_delta("r1", [{"id": "", "index": 0, "arguments": "x"}])
        merged = sh._merge_tool_calls_delta_events(a, b)
        assert merged["source"] == "llm_stream"


# ════════════════════════════════════════════════════════
# 暂留机制测试
# ════════════════════════════════════════════════════════

class TestPendingMechanism:
    @staticmethod
    def test_pending_activates_on_delta():
        root = _tmp_sessions_root()
        item = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        sh._route_event("s1", item, "chat.tool_calls.delta", root)
        assert "s1" in sh._session_pending
        assert _read_history("s1", root) == []

    @staticmethod
    def test_pending_merges_while_waiting():
        root = _tmp_sessions_root()
        a = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        b = _make_tool_calls_delta("r1", [{"id": "", "index": 0, "arguments": "x}"}])
        sh._route_event("s1", a, "chat.tool_calls.delta", root)
        sh._route_event("s1", b, "chat.tool_calls.delta", root)
        pending = sh._session_pending["s1"]
        assert pending.item["tool_calls"][0]["arguments"] == "{x}"

    @staticmethod
    def test_pending_frozen_no_write():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        delta = _make_delta("r1", "hi")
        usage = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", delta, "chat.delta", root)
        sh._route_event("s1", usage, "chat.usage_metadata", root)
        assert _read_history("s1", root) == []

    @staticmethod
    def test_condition_a_tool_call_match_discards_delta():
        # 条件 A：tool_call 命中 → 丢弃 delta，落盘积压事件 + tool_call
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "tool_call_id": "c1", "index": 0, "arguments": "{"}])
        usage = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        tc = _make_tool_call("r1", "c1")
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", usage, "chat.usage_metadata", root)
        sh._route_event("s1", tc, "chat.tool_call", root)
        history = _read_history("s1", root)
        types = [r.get("event_type") for r in history]
        assert "chat.tool_calls.delta" not in types
        assert types == ["chat.usage_metadata", "chat.tool_call"]
        assert "s1" not in sh._session_pending

    @staticmethod
    def test_pending_queue_accumulates_until_timeout():
        # 条件 B：积压 delta 进 pending_queue 合并，靠超时落盘
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        for i in range(3):
            sh._route_event("s1", _make_delta("r1", f"d{i}"), "chat.delta", root)
        assert "s1" in sh._session_pending
        assert _read_history("s1", root) == []
        sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
        sh._periodic_flush()
        history = _read_history("s1", root)
        types = [r.get("event_type") for r in history]
        assert types == ["chat.tool_calls.delta", "chat.delta"]
        assert history[1]["content"] == "d0d1d2"
        assert "s1" not in sh._session_pending

    @staticmethod
    def test_condition_b_timeout():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
        sh._periodic_flush()
        history = _read_history("s1", root)
        assert any(r.get("event_type") == "chat.tool_calls.delta" for r in history)
        assert "s1" not in sh._session_pending

    @staticmethod
    def test_pending_order_correct_on_timeout():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        usage = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        delta = _make_delta("r1", "after")
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", usage, "chat.usage_metadata", root)
        sh._route_event("s1", delta, "chat.delta", root)
        sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
        sh._periodic_flush()
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert types == ["chat.tool_calls.delta", "chat.usage_metadata", "chat.delta"]

    @staticmethod
    def test_unmatched_tool_call_waits_for_timeout():
        # 不命中的 tool_call 进 pending_queue 候着，靠超时落盘
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        unmatched = _make_tool_call("r1", "c2")  # c2 不在暂留 call_ids 里
        sh._route_event("s1", unmatched, "chat.tool_call", root)
        assert "s1" in sh._session_pending
        pending = sh._session_pending["s1"]
        raw_types = [v.get("event_type") for v in pending.pending_queue.values()]
        assert raw_types == ["chat.tool_call"]
        assert _read_history("s1", root) == []
        pending.start_time = sh.time.monotonic() - 10.0
        sh._periodic_flush()
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert types == ["chat.tool_calls.delta", "chat.tool_call"]
        assert "s1" not in sh._session_pending

    @staticmethod
    def test_tool_call_id_extraction_nested():
        item = _make_tool_call("r1", "call_xyz")
        assert sh._extract_tool_call_id(item) == "call_xyz"
        item2 = {"event_type": "chat.tool_call", "tool_call_id": "top_id"}
        assert sh._extract_tool_call_id(item2) == "top_id"  # 顶层无 tool_call 时回退

    @staticmethod
    def test_pending_request_switch_flushes():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        sh._route_event("s1", _make_delta("r1", "积压"), "chat.delta", root)
        assert "s1" in sh._session_pending
        sh._flush_on_request_switch("s1", "r2", root)
        assert "s1" not in sh._session_pending
        history = _read_history("s1", root)
        types = [r.get("event_type") for r in history]
        assert types == ["chat.tool_calls.delta", "chat.delta"]
        assert history[1]["content"] == "积压"


class TestPendingQueueMerge:
    @staticmethod
    def test_pending_queue_merges_same_type():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
        sh._route_event("s1", _make_delta("r1", "b"), "chat.delta", root)
        pending = sh._session_pending["s1"]
        delta_items = [v for k, v in pending.pending_queue.items() if k[1] == "chat.delta"]
        assert len(delta_items) == 1
        assert delta_items[0]["content"] == "ab"

    @staticmethod
    def test_pending_queue_preserves_non_buffer_order():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        u1 = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        u2 = {"request_id": "r1", "event_type": "chat.usage_summary", "content": "", "timestamp": 2.0}
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", u1, "chat.usage_metadata", root)
        sh._route_event("s1", u2, "chat.usage_summary", root)
        pending = sh._session_pending["s1"]
        raw_types = [v.get("event_type") for v in pending.pending_queue.values()]
        assert raw_types == ["chat.usage_metadata", "chat.usage_summary"]


# ════════════════════════════════════════════════════════
# tool_update per-call_id 分组
# ════════════════════════════════════════════════════════

class TestToolUpdatePerCallGrouping:
    """步骤3 对 tool_update 按 tool_call_id 分组：不同 call 各成一条，同 call 内合并。"""

    @staticmethod
    def test_different_call_ids_stay_separate():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_update("r1", "c1", "in_progress"), "chat.tool_update", root)
        sh._route_event("s1", _make_tool_update("r1", "c2", "failed"), "chat.tool_update", root)
        sh._flush_buffer("s1", root)
        history = _read_history("s1", root)
        by_id = {r.get("tool_call_id"): r.get("status") for r in history}
        assert by_id == {"c1": "in_progress", "c2": "failed"}

    @staticmethod
    def test_same_call_id_merges_status_latest():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_update("r1", "c1", "in_progress"), "chat.tool_update", root)
        sh._route_event("s1", _make_tool_update("r1", "c1", "failed"), "chat.tool_update", root)
        sh._flush_buffer("s1", root)
        history = _read_history("s1", root)
        assert len(history) == 1
        assert history[0]["tool_call_id"] == "c1"
        assert history[0]["status"] == "failed"

    @staticmethod
    def test_parallel_calls_interleaved_keep_correct_status():
        # 交错到达时各 call 的 status 仍正确归属
        root = _tmp_sessions_root()
        seq = [
            ("c1", "in_progress"), ("c2", "in_progress"),
            ("c1", "completed"), ("c2", "failed"),
        ]
        for cid, status in seq:
            sh._route_event("s1", _make_tool_update("r1", cid, status), "chat.tool_update", root)
        sh._flush_buffer("s1", root)
        by_id = {r.get("tool_call_id"): r.get("status") for r in _read_history("s1", root)}
        assert by_id == {"c1": "completed", "c2": "failed"}

    @staticmethod
    def test_tool_update_type_switch_to_delta_flushes_all_calls():
        # tool_update → delta 类型切换：per-call 记录按序落盘后开 delta 缓冲
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_update("r1", "c1", "in_progress"), "chat.tool_update", root)
        sh._route_event("s1", _make_tool_update("r1", "c2", "failed"), "chat.tool_update", root)
        sh._route_event("s1", _make_delta("r1", "after"), "chat.delta", root)
        sh._flush_buffer("s1", root)
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert types == ["chat.tool_update", "chat.tool_update", "chat.delta"]

    @staticmethod
    def test_tool_update_periodic_flush_uses_recorded_root():
        root_a = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_update("r1", "c1", "in_progress"), "chat.tool_update", root_a)
        sh._periodic_flush()
        history = _read_history("s1", root_a)
        assert len(history) == 1
        assert history[0]["tool_call_id"] == "c1"


# ════════════════════════════════════════════════════════
# 普通缓冲层与 flush 测试
# ════════════════════════════════════════════════════════

class TestTypeSwitchFlush:
    @staticmethod
    def test_type_switch_flushes_current():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
        sh._route_event("s1", _make_reasoning("r1", "think"), "chat.reasoning", root)
        history = _read_history("s1", root)
        assert history[0].get("event_type") == "chat.delta"
        assert history[0]["content"] == "a"

    @staticmethod
    def test_same_type_accumulates_no_flush():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
        sh._route_event("s1", _make_delta("r1", "b"), "chat.delta", root)
        assert _read_history("s1", root) == []
        assert sh._session_buffer["s1"]["content"] == "ab"

    @staticmethod
    def test_non_buffer_event_triggers_flush():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
        usage = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        sh._route_event("s1", usage, "chat.usage_metadata", root)
        history = _read_history("s1", root)
        assert history[0].get("event_type") == "chat.delta"

    @staticmethod
    def test_request_switch_flushes_old():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
        sh._flush_on_request_switch("s1", "r2", root)
        assert len(_read_history("s1", root)) == 1
        assert "s1" not in sh._session_buffer

    @staticmethod
    def test_capacity_flush_on_delta_count():
        root = _tmp_sessions_root()
        with mock.patch.object(sh, "BUFFER_MAX_SIZE", 3):
            sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root)
            sh._route_event("s1", _make_delta("r1", "b"), "chat.delta", root)
            assert _read_history("s1", root) == []
            sh._route_event("s1", _make_delta("r1", "c"), "chat.delta", root)
            history = _read_history("s1", root)
            assert len(history) == 1
            assert history[0]["content"] == "abc"
            assert "s1" not in sh._session_buffer

    @staticmethod
    def test_pending_not_flushed_by_type_switch():
        # 暂留激活后所有事件进 pending_queue，不触发普通缓冲层 flush
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        sh._route_event("s1", _make_delta("r1", "x"), "chat.delta", root)
        assert "s1" in sh._session_pending
        assert _read_history("s1", root) == []
        pending = sh._session_pending["s1"]
        delta_items = [v for k, v in pending.pending_queue.items() if k[1] == "chat.delta"]
        assert len(delta_items) == 1
        assert delta_items[0]["content"] == "x"


class TestAppendHistoryRecord:
    @staticmethod
    def test_degrade_on_failure():
        # 缓冲失败降级到 _WRITE_QUEUE，不向调用方上抛、不丢数据
        root = _tmp_sessions_root()
        with mock.patch.object(sh, "_route_event", side_effect=RuntimeError("boom")), \
             mock.patch.object(sh, "logger"):
            sh.append_history_record(
                session_id="s1", request_id="r1", channel_id="c", role="user",
                content="hello", timestamp=1.0, sessions_root=root,
            )
        sh._ensure_worker_started()
        import time as _t
        _t.sleep(0.1)
        history = _read_history("s1", root)
        assert len(history) == 1
        assert history[0]["content"] == "hello"

    @staticmethod
    def test_degrade_on_request_switch_failure():
        root = _tmp_sessions_root()
        with mock.patch.object(sh, "_flush_on_request_switch", side_effect=RuntimeError("switch boom")), \
             mock.patch.object(sh, "logger"):
            sh.append_history_record(
                session_id="s1", request_id="r1", channel_id="c", role="user",
                content="rescued", timestamp=1.0, sessions_root=root,
            )
        sh._ensure_worker_started()
        import time as _t
        _t.sleep(0.1)
        history = _read_history("s1", root)
        assert len(history) == 1
        assert history[0]["content"] == "rescued"


# ════════════════════════════════════════════════════════
# 落盘正确性：顺序保持 + recorded_root 兜底（多入口）
# ════════════════════════════════════════════════════════

class TestConcurrency:
    """落盘顺序与 root 归属正确性。"""

    @staticmethod
    def test_non_buffer_events_preserve_order():
        # 非缓冲事件同步批量落盘，保持到达顺序
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "buf"), "chat.delta", root)
        u1 = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 1.0}
        u2 = {"request_id": "r1", "event_type": "chat.usage_summary", "content": "", "timestamp": 2.0}
        sh._route_event("s1", u1, "chat.usage_metadata", root)
        sh._route_event("s1", u2, "chat.usage_summary", root)
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert types == ["chat.delta", "chat.usage_metadata", "chat.usage_summary"]

    @staticmethod
    def test_switch_items_use_recorded_root_on_mismatched_root():
        # delta 以 root_A 缓冲；非缓冲事件以 None root 到达（send_file_to_user 不传 root）。
        # 旧 delta 应落 root_A，非缓冲事件落 None→default。
        root_a = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "tenant_buf"), "chat.delta", root_a)
        assert "s1" in sh._session_buffer
        nb = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 2.0}
        sh._route_event("s1", nb, "chat.usage_metadata", None)
        assert _read_history("s1", root_a)[0].get("content") == "tenant_buf"

    @staticmethod
    def test_pending_condition_b_uses_recorded_root():
        # 条件 B（超时）落盘用暂留时记录的 root，不走 default
        default_root = _tmp_sessions_root()
        with mock.patch.object(sh, "get_agent_sessions_dir", return_value=default_root):
            root_a = _tmp_sessions_root()
            sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                            "chat.tool_calls.delta", root_a)
            sh._route_event("s1", _make_delta("r1", "积压"), "chat.delta", root_a)
            sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
            sh._periodic_flush()
        ha = _read_history("s1", root_a)
        assert [r.get("event_type") for r in ha] == ["chat.tool_calls.delta", "chat.delta"]
        assert ha[1]["content"] == "积压"
        assert not (Path(default_root) / "s1" / "history.json").exists()

    @staticmethod
    def test_request_switch_uses_recorded_root_on_mismatch():
        # 旧请求以 root_A 缓冲；新请求带 root_B → 旧 delta 须落 root_A 而非 root_B
        root_a = _tmp_sessions_root()
        root_b = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "tenant_buf"), "chat.delta", root_a)
        assert "s1" in sh._session_buffer
        sh._flush_on_request_switch("s1", "r2", root_b)
        assert _read_history("s1", root_a)[0].get("content") == "tenant_buf"
        assert not (Path(root_b) / "s1" / "history.json").exists()

    @staticmethod
    def test_capacity_flush_uses_recorded_root():
        root_a = _tmp_sessions_root()
        with mock.patch.object(sh, "BUFFER_MAX_SIZE", 2):
            sh._route_event("s1", _make_delta("r1", "a"), "chat.delta", root_a)
            sh._route_event("s1", _make_delta("r1", "b"), "chat.delta", root_a)
        history = _read_history("s1", root_a)
        assert len(history) == 1
        assert history[0]["content"] == "ab"
        assert history[0]["delta_count"] == 2


# ════════════════════════════════════════════════════════
# root 双写防护 + pending 超时竞态 + graceful shutdown
# ════════════════════════════════════════════════════════

class TestNonBufferRootSplit:
    """步骤4 两个 root 均非 None 且不同时，旧缓冲记录与本事件各落各文件。"""

    @staticmethod
    def test_switch_items_and_event_split_to_respective_roots():
        root_a = _tmp_sessions_root()
        root_b = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "tenant_buf"), "chat.delta", root_a)
        assert "s1" in sh._session_buffer
        nb = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 2.0}
        sh._route_event("s1", nb, "chat.usage_metadata", root_b)  # root_A != root_B → 类型切换
        assert _read_history("s1", root_a)[0].get("content") == "tenant_buf"
        assert _read_history("s1", root_b)[0].get("event_type") == "chat.usage_metadata"

    @staticmethod
    def test_switch_items_fallback_when_event_root_none():
        # sessions_root_s 为 None（send_file_to_user 不传 root）时旧记录用 recorded_root
        from pathlib import Path as _Path
        root_a = _tmp_sessions_root()
        default_root = _tmp_sessions_root()
        with mock.patch.object(sh, "get_agent_sessions_dir", return_value=_Path(default_root)):
            sh._route_event("s1", _make_delta("r1", "tenant_buf"), "chat.delta", root_a)
            nb = {"request_id": "r1", "event_type": "chat.usage_metadata", "content": "", "timestamp": 2.0}
            sh._route_event("s1", nb, "chat.usage_metadata", None)
        assert _read_history("s1", root_a)[0].get("content") == "tenant_buf"
        assert _read_history("s1", default_root)[0].get("event_type") == "chat.usage_metadata"


class TestPeriodicFlushRaceRecheck:
    """_periodic_flush 弹出超时 pending 后、落盘前若同 sid 被重建新暂留，旧 pending 仍按条件 B 独立落盘（不丢积压事件），新暂留保留各自走 A/B。"""

    @staticmethod
    def test_old_pending_flushed_when_recreated_before_write():
        import collections as _c
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        sh._route_event("s1", _make_delta("r1", "积压"), "chat.delta", root)
        sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
        # 放一条普通缓冲记录让 s1 进 buffer_sids，使 _flush_buffer 被调用，
        # 在 pending 落盘前注入"同 sid 重建暂留"模拟 event-loop 线程
        sh._session_buffer["s1"] = _make_delta("r1", "buf")
        sh._session_buffer_type["s1"] = "chat.delta"
        sh._session_buffer_request_id["s1"] = "r1"
        sh._session_buffer_root["s1"] = root

        real_flush_buffer = sh._flush_buffer

        def _spied_flush_buffer(sid, sessions_root):
            with sh._buffer_lock:
                if sid not in sh._session_pending:
                    sh._session_pending[sid] = sh._PendingState(
                        item={"event_type": "chat.tool_calls.delta", "tool_calls": [], "request_id": "r2"},
                        request_id="r2",
                        pending_queue=_c.OrderedDict(),
                        start_time=sh.time.monotonic(), sessions_root=root,
                    )
            return real_flush_buffer(sid, sessions_root)

        with mock.patch.object(sh, "_flush_buffer", side_effect=_spied_flush_buffer):
            sh._periodic_flush()
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert "chat.tool_calls.delta" in types  # 旧 delta 未被丢弃
        assert types.count("chat.delta") == 2  # 积压 delta + 普通缓冲 buf
        assert "s1" in sh._session_pending  # 新暂留（r2）保留
        assert sh._session_pending["s1"].request_id == "r2"


class TestGracefulShutdown:
    """shutdown() 在退出前 flush 缓冲 + 排空写队列，不丢数据。"""

    @staticmethod
    def test_shutdown_flushes_buffer():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "pending"), "chat.delta", root)
        assert _read_history("s1", root) == []
        sh.shutdown()
        history = _read_history("s1", root)
        assert len(history) == 1
        assert history[0]["content"] == "pending"

    @staticmethod
    def test_shutdown_flushes_pending():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        sh._session_pending["s1"].start_time = sh.time.monotonic() - 10.0
        sh.shutdown()
        history = _read_history("s1", root)
        assert any(r.get("event_type") == "chat.tool_calls.delta" for r in history)

    @staticmethod
    def test_shutdown_flushes_unexpired_pending():
        # 未超时的暂留在 shutdown 时也须落盘（_force_flush_all_pending 兜底）
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}]),
                        "chat.tool_calls.delta", root)
        assert sh.time.monotonic() - sh._session_pending["s1"].start_time < sh.PENDING_MAX_SECONDS
        sh.shutdown()
        history = _read_history("s1", root)
        assert any(r.get("event_type") == "chat.tool_calls.delta" for r in history)
        assert "s1" not in sh._session_pending

    @staticmethod
    def test_shutdown_unexpired_pending_flushes_queue():
        root = _tmp_sessions_root()
        tcd = _make_tool_calls_delta("r1", [{"id": "c1", "index": 0, "arguments": "{"}])
        sh._route_event("s1", tcd, "chat.tool_calls.delta", root)
        sh._route_event("s1", _make_delta("r1", "积压"), "chat.delta", root)
        assert sh.time.monotonic() - sh._session_pending["s1"].start_time < sh.PENDING_MAX_SECONDS
        sh.shutdown()
        types = [r.get("event_type") for r in _read_history("s1", root)]
        assert types == ["chat.tool_calls.delta", "chat.delta"]
        assert _read_history("s1", root)[1]["content"] == "积压"

    @staticmethod
    def test_shutdown_idempotent():
        # 第二次起短路返回（atexit + 显式调用会叠加）
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "x"), "chat.delta", root)
        assert not sh._SHUTDOWN_DONE
        sh.shutdown()
        assert sh._SHUTDOWN_DONE
        assert len(_read_history("s1", root)) == 1
        sh._route_event("s1", _make_delta("r1", "after"), "chat.delta", root)
        sh.shutdown()
        assert len(_read_history("s1", root)) == 1  # after 仍在缓冲，第二次短路
        assert "s1" in sh._session_buffer

    @staticmethod
    def test_shutdown_concurrent_no_duplicate_write():
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "only"), "chat.delta", root)
        barrier = threading.Barrier(8)

        def _call():
            barrier.wait()
            sh.shutdown()

        ts = [threading.Thread(target=_call) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        history = _read_history("s1", root)
        deltas = [r for r in history if r.get("event_type") == "chat.delta"]
        assert len(deltas) == 1, f"并发 shutdown 产生重复落盘: {len(deltas)} 条 delta"
        assert deltas[0]["content"] == "only"

    @staticmethod
    def test_shutdown_no_duplicate_flush_thread():
        root = _tmp_sessions_root()
        sh.append_history_record(  # 间接起 flush 线程
            session_id="s1", request_id="r1", channel_id="c", role="assistant",
            event_type="chat.delta", content="x", timestamp=1.0, sessions_root=root,
        )
        old_thread = sh._FLUSH_THREAD
        assert old_thread is not None and old_thread.is_alive()
        sh.shutdown()
        assert not old_thread.is_alive()
        sh.append_history_record(
            session_id="s1", request_id="r2", channel_id="c", role="assistant",
            event_type="chat.delta", content="y", timestamp=2.0, sessions_root=root,
        )
        new_thread = sh._FLUSH_THREAD
        assert new_thread is not None and new_thread.is_alive()
        assert new_thread is not old_thread

    @staticmethod
    def test_shutdown_flushes_events_arriving_mid_shutdown():
        # 首轮 flush 跑完后由残留协程投递的缓冲事件，shutdown 末次复扫兜底落盘
        root = _tmp_sessions_root()
        sh._route_event("s1", _make_delta("r1", "first"), "chat.delta", root)
        assert "s1" in sh._session_buffer

        # mock 首次 _force_flush_all_pending：返回后注入一条缓冲事件模拟残留协程投递
        real_force = sh._force_flush_all_pending

        def _spied_force():
            real_force()

            def _inject():
                sh.append_history_record(
                    session_id="s1", request_id="r2", channel_id="c", role="assistant",
                    event_type="chat.delta", content="RACE", timestamp=2.0, sessions_root=root,
                )
            threading.Thread(target=_inject, daemon=True).start()
            import time as _t
            _t.sleep(0.3)  # 等注入落进 _session_buffer

        with mock.patch.object(sh, "_force_flush_all_pending", side_effect=_spied_force):
            sh.shutdown()
        contents = [r.get("content") for r in _read_history("s1", root)]
        assert "RACE" in contents, f"shutdown 进行中到达的缓冲事件丢失: {contents}"
