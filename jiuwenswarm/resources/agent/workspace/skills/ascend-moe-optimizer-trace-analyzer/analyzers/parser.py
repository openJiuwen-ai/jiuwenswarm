from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Event:
    name: str
    ts_start: float
    ts_end: float
    dur: float
    pid: Optional[int] = None
    tid: Optional[int] = None
    cat: Optional[str] = None
    ph: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_raw_events(trace_path: str) -> List[Dict[str, Any]]:
    with open(trace_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        raw_events = data.get("traceEvents", [])
        if isinstance(raw_events, list):
            return raw_events
        return []

    if isinstance(data, list):
        return data

    return []


def _parse_complete_event(item: Dict[str, Any]) -> Optional[Event]:
    """
    处理 ph == 'X' 的完整事件
    """
    name = item.get("name")
    ts = _safe_float(item.get("ts"))
    dur = _safe_float(item.get("dur"))

    if not name or ts is None or dur is None:
        return None

    return Event(
        name=name,
        ts_start=ts,
        ts_end=ts + dur,
        dur=dur,
        pid=item.get("pid"),
        tid=item.get("tid"),
        cat=item.get("cat"),
        ph="X",
        args=item.get("args", {}),
    )


def _pair_begin_end_events(raw_events: List[Dict[str, Any]]) -> List[Event]:
    """
    将 B/E 事件配对成完整 Event。

    配对策略：
    - 按 (pid, tid, name) 维护栈
    - 遇到 B 入栈
    - 遇到 E 出栈并生成 Event

    注意：
    - 若 E 找不到对应 B，则忽略
    - 若 B 最终未匹配到 E，则忽略
    """
    stacks: Dict[Tuple[Any, Any, str], List[Dict[str, Any]]] = {}
    paired_events: List[Event] = []

    for item in raw_events:
        ph = item.get("ph")
        if ph not in ("B", "E"):
            continue

        name = item.get("name")
        ts = _safe_float(item.get("ts"))
        pid = item.get("pid")
        tid = item.get("tid")

        if not name or ts is None:
            continue

        key = (pid, tid, name)

        if ph == "B":
            stacks.setdefault(key, []).append(item)
            continue

        # ph == "E"
        stack = stacks.get(key)
        if not stack:
            # 孤立的 E，直接跳过
            continue

        begin_item = stack.pop()
        ts_begin = _safe_float(begin_item.get("ts"))
        if ts_begin is None:
            continue

        if ts < ts_begin:
            # 时间反常，跳过
            continue

        begin_args = begin_item.get("args", {}) or {}
        end_args = item.get("args", {}) or {}

        merged_args = {}
        merged_args.update(begin_args)
        merged_args.update(end_args)

        paired_events.append(
            Event(
                name=name,
                ts_start=ts_begin,
                ts_end=ts,
                dur=ts - ts_begin,
                pid=pid,
                tid=tid,
                cat=begin_item.get("cat") or item.get("cat"),
                ph="BE",
                args=merged_args,
            )
        )

    return paired_events


def parse_trace_json(trace_path: str) -> List[Event]:
    """
    V1 parser:
    - 支持 ph == 'X'
    - 支持 ph == 'B' / 'E' 配对
    - 返回统一的完整事件列表
    """
    raw_events = _load_raw_events(trace_path)

    parsed_events: List[Event] = []

    # 1. 先解析 X 事件
    for item in raw_events:
        ph = item.get("ph")
        if ph == "X":
            event = _parse_complete_event(item)
            if event is not None:
                parsed_events.append(event)

    # 2. 再解析 B/E 配对事件
    be_events = _pair_begin_end_events(raw_events)
    parsed_events.extend(be_events)

    # 3. 按时间排序
    parsed_events.sort(key=lambda e: (e.ts_start, e.ts_end, e.name))
    return parsed_events


def events_to_dicts(events: List[Event]) -> List[Dict[str, Any]]:
    return [asdict(e) for e in events]