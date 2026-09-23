# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""验证 append_history_record 在 history 大文件慢 open 场景下不阻塞事件循环。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import session_history as sh

_SIMULATED_SLOW_OPEN_SECONDS = 1.5
_HEARTBEAT_INTERVAL = 0.05


def _stop_flush_thread():
    sh._flush_stop_event.set()
    t = getattr(sh, "_FLUSH_THREAD", None)
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=2.0)
    sh._flush_stop_event.clear()


def _clear_buffer_state():
    sh._session_buffer.clear()
    sh._session_buffer_type.clear()
    sh._session_buffer_request_id.clear()
    sh._session_buffer_root.clear()
    sh._session_tool_update_buffer.clear()
    sh._session_tool_update_root.clear()
    sh._session_pending.clear()
    sh._FLUSH_THREAD_STARTED = False
    sh._FLUSH_THREAD = None
    sh._SHUTDOWN_DONE = False
    sh._flush_stop_event.clear()


@pytest.fixture(autouse=True)
def _isolated_history_state(monkeypatch):
    _stop_flush_thread()
    _clear_buffer_state()
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    yield
    _stop_flush_thread()
    _clear_buffer_state()


async def test_append_history_record_keeps_event_loop_responsive_on_slow_open(
    monkeypatch, tmp_path
):
    sessions_root = str(tmp_path)
    sid = "sess_repro_001"
    history_path = Path(sessions_root) / sid / "history.json"
    history_path.parent.mkdir(parents=True)
    big_content = "x" * (2 * 1024 * 1024)
    history_path.write_text(
        json.dumps([{"id": "seed", "role": "user", "content": big_content}]),
        encoding="utf-8",
    )

    original_open = Path.open

    def slow_open(self, *args, **kwargs):
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if "r" in mode and self == history_path:
            time.sleep(_SIMULATED_SLOW_OPEN_SECONDS)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)

    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    hb = asyncio.create_task(heartbeat())

    stacks: list[str] = []
    stop_watchdog = threading.Event()
    main_tid = threading.get_ident()

    def watchdog():
        while not stop_watchdog.is_set():
            frame = sys._current_frames().get(main_tid)
            if frame is not None:
                names = []
                f = frame
                while f is not None and len(names) < 10:
                    names.append(f.f_code.co_name)
                    f = f.f_back
                stacks.append(" <- ".join(names))
            stop_watchdog.wait(0.05)

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()

    await asyncio.sleep(0.3)

    sh.append_history_record(
        session_id=sid,
        request_id="req-1",
        channel_id="officeclaw",
        role="assistant",
        content="tool result payload",
        timestamp=time.time(),
        event_type="chat.tool_result",
        extra={"tool_call_id": "call-1"},
        sessions_root=sessions_root,
    )

    await asyncio.sleep(0.3)
    hb.cancel()
    try:
        await hb
    except asyncio.CancelledError:
        pass
    stop_watchdog.set()
    wd.join(timeout=2.0)

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "heartbeat never ticked"
    max_gap = max(gaps)
    assert max_gap < _SIMULATED_SLOW_OPEN_SECONDS, (
        f"event loop stalled {max_gap:.2f}s during append; "
        "history write must run off the event loop"
    )
    assert not any(
        "_peek_first_non_ws_char" in s or "_write_records_to_path" in s
        for s in stacks
    ), "history disk I/O ran on the event loop thread"

    sh._WRITE_QUEUE.join()
    with open(history_path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().split("\n") if ln.strip()]
    assert any('"chat.tool_result"' in ln for ln in lines)
