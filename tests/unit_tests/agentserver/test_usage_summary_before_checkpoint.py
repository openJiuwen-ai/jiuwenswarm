# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""usage_summary / is_complete 必须在 session checkpoint 之前发出，避免并发收尾被落盘堵住。"""

from __future__ import annotations

from pathlib import Path


def _interface_deep_source() -> str:
    root = Path(__file__).resolve().parents[3]
    return (
        root
        / "jiuwenswarm"
        / "server"
        / "runtime"
        / "agent_adapter"
        / "interface_deep.py"
    ).read_text(encoding="utf-8")


def test_stream_emits_usage_summary_before_session_checkpoint() -> None:
    src = _interface_deep_source()
    # 锚定 process_message_stream_impl 方法体，避免命中其它同名调用
    start = src.find("async def process_message_stream_impl")
    assert start != -1
    end = src.find("\n    @staticmethod\n    def _stream_text_payload", start)
    assert end != -1
    body = src[start:end]
    summary_at = body.find("_log_and_make_usage_summary_chunk")
    complete_at = body.find("is_complete=True")
    checkpoint_at = body.find("await self._persist_session_checkpoint")
    assert summary_at != -1, "missing usage_summary emission"
    assert checkpoint_at != -1, "missing session checkpoint"
    assert summary_at < checkpoint_at, (
        "usage_summary must be yielded before _persist_session_checkpoint"
    )
    assert complete_at != -1 and complete_at < checkpoint_at, (
        "is_complete frame must be yielded before _persist_session_checkpoint"
    )


def test_traced_stream_logs_llm_http_done() -> None:
    src = _interface_deep_source()
    start = src.find("def _apply_llm_io_trace_patch")
    assert start != -1
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert "log_llm_first_token_ms" in body
    assert "log_llm_http_done" in body
