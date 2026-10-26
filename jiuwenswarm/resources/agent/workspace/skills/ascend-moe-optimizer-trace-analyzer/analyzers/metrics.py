from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


Row = Dict[str, Any]
Table = List[Row]
Interval = Tuple[float, float]

_EXTRA_RE = re.compile(r"\s+\[extra:[^\]]+\]")
_SEQ_RE = re.compile(r"\s+#-?\d+$")
_TID_CORE_RE = re.compile(r"type(\d+)_core(\d+)")
_CORE_GROUPS = {
    "0": ("cube", "cube"),
    "1": ("vector_recv", "vector"),
    "2": ("vector_send", "vector"),
}
_CORE_GROUP_ORDER = {
    "cube": 0,
    "vector_recv": 1,
    "vector_send": 2,
    "unknown": 99,
}


def normalize_event_name(name: str) -> str:
    """
    Strip trace_collector suffixes so labels match source TRACE_POINT names.
    Example: "gmm2-combine wait quant [extra:0] #3" -> "gmm2-combine wait quant".
    """
    normalized = _SEQ_RE.sub("", name)
    normalized = _EXTRA_RE.sub("", normalized)
    return normalized.strip()


def _safe_sort_value(value: Any) -> Tuple[int, str]:
    if value is None:
        return (1, "")
    return (0, str(value))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_core_type(value: Any, tid: Any = None) -> Optional[str]:
    if value is not None and value != "":
        return str(value)

    if tid is None:
        return None
    match = _TID_CORE_RE.search(str(tid))
    if match:
        return match.group(1)
    return None


def _normalize_core_id(value: Any, tid: Any = None) -> Optional[int]:
    if value is not None and value != "":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if tid is None:
        return None
    match = _TID_CORE_RE.search(str(tid))
    if match:
        return int(match.group(2))
    return None


def _core_group(core_type: Any) -> str:
    group, _ = _CORE_GROUPS.get(str(core_type), ("unknown", "unknown"))
    return group


def _core_kind(core_type: Any) -> str:
    _, kind = _CORE_GROUPS.get(str(core_type), ("unknown", "unknown"))
    return kind


def _phase_category(phase: str, name: str, mapper: Any = None) -> str:
    configured = getattr(mapper, "phase_categories", {}).get(phase) if mapper is not None else None
    if configured:
        return configured

    text = normalize_event_name(name).lower()
    phase_text = phase.lower()

    if text in {"processing", "dispatch-gmm1", "gmm2-combine"}:
        return "container"
    if "wait" in text or "waiting" in text or "sleep" in text:
        return "wait"
    if "sync" in text or "barrier" in text:
        return "sync"
    if "init" in text:
        return "init"
    if "clean" in text or "update" in text:
        return "cleanup"
    if "quant" in text or "swiglu" in text:
        return "quant"
    if "block-epilogue" in text:
        return "epilogue"
    _comm_tokens = ("send", "recv", "combine", "dispatch", "local-copy")
    if any(tok in text for tok in _comm_tokens):
        return "communication"
    if "aic" in text or "aiv" in text or "compcorefunc" in text:
        return "compute"
    if "moe-process" in text:
        return "compute"
    if "gmm" in phase_text or "gmm" in text:
        return "compute"
    return "other"


def build_phase_instances(events: List[dict], mapper: Any) -> Table:
    """
    Convert parser output into interval rows.
    Each row is one matched full interval event.
    """
    rows: Table = []
    for e in events:
        name = e.get("name")
        if not name:
            continue

        phase = mapper.map_event_name(name)
        if phase is None:
            continue

        args = e.get("args") or {}
        tid = e.get("tid")
        core_type = _normalize_core_type(args.get("core_type"), tid)
        core_id = _normalize_core_id(args.get("core_id"), tid)

        row = {
            "phase": phase,
            "category": _phase_category(phase, name, mapper),
            "name": name,
            "normalized_name": normalize_event_name(name),
            "ts_start": _float(e.get("ts_start")),
            "ts_end": _float(e.get("ts_end")),
            "dur": _float(e.get("dur")),
            "pid": e.get("pid"),
            "tid": tid,
            "rank_id": args.get("rank_id"),
            "core_type": core_type,
            "core_group": _core_group(core_type),
            "core_kind": _core_kind(core_type),
            "core_id": core_id,
            "extra_id": args.get("extra_id"),
            "event_id": args.get("event_id"),
            "cat": e.get("cat"),
            "ph": e.get("ph"),
        }
        rows.append(row)

    rows.sort(
        key=lambda r: (
            _safe_sort_value(r.get("pid")),
            _safe_sort_value(r.get("tid")),
            _float(r.get("ts_start")),
            _float(r.get("ts_end")),
            str(r.get("name", "")),
        )
    )
    return rows


def _merge_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    valid = sorted((float(s), float(e)) for s, e in intervals if e >= s)
    if not valid:
        return []

    merged: List[List[float]] = [[valid[0][0], valid[0][1]]]
    for s, e in valid[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    return [(float(s), float(e)) for s, e in merged]


def _interval_union_length(intervals: Iterable[Interval]) -> float:
    return float(sum(e - s for s, e in _merge_intervals(intervals)))


def _interval_overlap_length(intervals_a: Iterable[Interval], intervals_b: Iterable[Interval]) -> float:
    a = _merge_intervals(intervals_a)
    b = _merge_intervals(intervals_b)

    i = 0
    j = 0
    overlap = 0.0

    while i < len(a) and j < len(b):
        sa, ea = a[i]
        sb, eb = b[j]

        left = max(sa, sb)
        right = min(ea, eb)
        if right > left:
            overlap += right - left

        if ea <= eb:
            i += 1
        else:
            j += 1

    return float(overlap)


def _intersect_intervals(intervals_a: Iterable[Interval], intervals_b: Iterable[Interval]) -> List[Interval]:
    a = _merge_intervals(intervals_a)
    b = _merge_intervals(intervals_b)
    result: List[Interval] = []
    i = 0
    j = 0

    while i < len(a) and j < len(b):
        sa, ea = a[i]
        sb, eb = b[j]
        left = max(sa, sb)
        right = min(ea, eb)
        if right > left:
            result.append((left, right))
        if ea <= eb:
            i += 1
        else:
            j += 1

    return result


def _subtract_intervals(parents: Iterable[Interval], children: Iterable[Interval]) -> List[Interval]:
    child_merged = _merge_intervals(children)
    gaps: List[Interval] = []

    for ps, pe in _merge_intervals(parents):
        cursor = ps
        for cs, ce in child_merged:
            if ce <= cursor:
                continue
            if cs >= pe:
                break
            if cs > cursor:
                gaps.append((cursor, min(cs, pe)))
            cursor = max(cursor, ce)
            if cursor >= pe:
                break
        if cursor < pe:
            gaps.append((cursor, pe))

    return gaps


def _intervals(rows: Table) -> List[Interval]:
    return [(_float(r.get("ts_start")), _float(r.get("ts_end"))) for r in rows]


def _group_by(rows: Table, key: str) -> Dict[Any, Table]:
    grouped: Dict[Any, Table] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _main_category(rows: Table) -> Optional[str]:
    categories = Counter(r.get("category") for r in rows if r.get("category"))
    return categories.most_common(1)[0][0] if categories else None


def _nunique(rows: Table, key: str) -> int:
    return len({r.get(key) for r in rows if r.get(key) is not None})


def _first_non_none(rows: Table, key: str) -> Any:
    for row in rows:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _summarize_rows(group_name: str, group_value: Any, rows: Table, total_wall: float) -> Row:
    durations = [_float(r.get("dur")) for r in rows]
    union_us = _interval_union_length(_intervals(rows))
    total_us = float(sum(durations))
    count = len(rows)

    row = {
        group_name: group_value,
        "count": count,
        "total_us": total_us,
        "avg_us": total_us / count if count else 0.0,
        "max_us": max(durations) if durations else 0.0,
        "min_us": min(durations) if durations else 0.0,
        "union_us": union_us,
        "ratio_to_total_wall": union_us / total_wall if total_wall > 0 else 0.0,
        "first_ts": min((_float(r.get("ts_start")) for r in rows), default=0.0),
        "last_ts": max((_float(r.get("ts_end")) for r in rows), default=0.0),
        "pid_nunique": _nunique(rows, "pid"),
        "tid_nunique": _nunique(rows, "tid"),
    }
    if group_name != "category":
        row["category"] = _main_category(rows)
    return row


def _total_wall(instances: Table) -> float:
    if not instances:
        return 0.0
    return max(_float(r.get("ts_end")) for r in instances) - min(_float(r.get("ts_start")) for r in instances)


def build_phase_summary(instances: Table) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    rows = [
        _summarize_rows("phase", phase, group, total_wall)
        for phase, group in _group_by(instances, "phase").items()
    ]
    rows.sort(
        key=lambda r: (_float(r.get("union_us")), _float(r.get("total_us"))),
        reverse=True,
    )
    return rows


def build_category_summary(instances: Table) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    rows = []
    for category, group in _group_by(instances, "category").items():
        row = _summarize_rows("category", category, group, total_wall)
        rows.append(row)
    rows.sort(key=lambda r: (_float(r.get("union_us")), _float(r.get("total_us"))), reverse=True)
    return rows


def build_core_group_summary(instances: Table) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    rows: Table = []
    for core_group, group in _group_by(instances, "core_group").items():
        row = _summarize_rows("core_group", core_group, group, total_wall)
        row["core_kind"] = _first_non_none(group, "core_kind")
        row["core_type"] = _first_non_none(group, "core_type")
        row["observed_core_count"] = _nunique(group, "tid")
        rows.append(row)

    rows.sort(key=lambda r: (_CORE_GROUP_ORDER.get(str(r.get("core_group")), 99), -_float(r.get("union_us"))))
    return rows


def _build_summary_by_core_group(instances: Table, key: str) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    core_groups = _group_by(instances, "core_group")
    core_group_union = {
        core_group: _interval_union_length(_intervals(group))
        for core_group, group in core_groups.items()
    }

    grouped: Dict[Tuple[Any, Any], Table] = defaultdict(list)
    for row in instances:
        grouped[(row.get("core_group"), row.get(key))].append(row)

    rows: Table = []
    for (core_group, value), group in grouped.items():
        summary = _summarize_rows(key, value, group, total_wall)
        summary["core_group"] = core_group
        summary["core_kind"] = _first_non_none(group, "core_kind")
        summary["core_type"] = _first_non_none(group, "core_type")
        summary["observed_core_count"] = _nunique(group, "tid")
        summary["core_group_union_us"] = core_group_union.get(core_group, 0.0)
        summary["ratio_to_core_group_wall"] = (
            summary["union_us"] / summary["core_group_union_us"]
            if summary["core_group_union_us"] > 0 else 0.0
        )
        rows.append(summary)

    rows.sort(
        key=lambda r: (
            _CORE_GROUP_ORDER.get(str(r.get("core_group")), 99),
            -_float(r.get("union_us")),
            str(r.get(key)),
        )
    )
    return rows


def build_phase_core_group_summary(instances: Table) -> Table:
    return _build_summary_by_core_group(instances, "phase")


def build_category_core_group_summary(instances: Table) -> Table:
    return _build_summary_by_core_group(instances, "category")


def build_name_summary(instances: Table) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    rows: Table = []
    for name, group in _group_by(instances, "name").items():
        row = _summarize_rows("name", name, group, total_wall)
        phase_counts = Counter(r.get("phase") for r in group if r.get("phase"))
        row["phase"] = phase_counts.most_common(1)[0][0] if phase_counts else None
        row["normalized_name"] = normalize_event_name(str(name))
        rows.append(row)

    rows.sort(
        key=lambda r: (
            _float(r.get("total_us")),
            _float(r.get("union_us")),
            int(r.get("count", 0)),
        ),
        reverse=True,
    )
    return rows


def build_phase_tid_summary(instances: Table) -> Table:
    if not instances:
        return []

    total_wall = _total_wall(instances)
    grouped: Dict[Tuple[Any, Any, Any], Table] = defaultdict(list)
    for row in instances:
        grouped[(row.get("phase"), row.get("pid"), row.get("tid"))].append(row)

    rows: Table = []
    for (phase, pid, tid), group in grouped.items():
        summary = _summarize_rows("phase", phase, group, total_wall)
        summary["pid"] = pid
        summary["tid"] = tid
        rows.append(summary)

    rows.sort(key=lambda r: (str(r.get("phase")), -_float(r.get("union_us")), str(r.get("tid"))))
    return rows


def build_overlap_summary(instances: Table) -> Table:
    if not instances:
        return []

    phase_to_intervals: Dict[str, List[Interval]] = {}
    phase_to_union: Dict[str, float] = {}
    for phase, group in _group_by(instances, "phase").items():
        intervals = _intervals(group)
        phase_to_intervals[phase] = intervals
        phase_to_union[phase] = _interval_union_length(intervals)

    phases = list(phase_to_intervals.keys())
    rows: Table = []

    for i, phase_a in enumerate(phases):
        for phase_b in phases[i + 1:]:
            overlap_us = _interval_overlap_length(phase_to_intervals[phase_a], phase_to_intervals[phase_b])
            union_a = phase_to_union[phase_a]
            union_b = phase_to_union[phase_b]
            min_union = min(union_a, union_b)
            max_union = max(union_a, union_b)
            rows.append(
                {
                    "phase_a": phase_a,
                    "phase_b": phase_b,
                    "overlap_us": overlap_us,
                    "overlap_ratio_a": overlap_us / union_a if union_a > 0 else 0.0,
                    "overlap_ratio_b": overlap_us / union_b if union_b > 0 else 0.0,
                    "overlap_ratio_min": overlap_us / min_union if min_union > 0 else 0.0,
                    "overlap_ratio_max": overlap_us / max_union if max_union > 0 else 0.0,
                    "union_us_a": union_a,
                    "union_us_b": union_b,
                    "non_overlap_a_us": max(union_a - overlap_us, 0.0),
                    "non_overlap_b_us": max(union_b - overlap_us, 0.0),
                }
            )

    rows.sort(
        key=lambda r: (
            _float(r.get("overlap_us")),
            _float(r.get("overlap_ratio_a")),
            _float(r.get("overlap_ratio_b")),
        ),
        reverse=True,
    )
    return rows


def _child_intervals_for_parent(instances: Table, parent_phase: str) -> List[Interval]:
    children: Table = []
    for row in instances:
        phase = str(row.get("phase") or "")
        if phase == parent_phase:
            continue
        if parent_phase == "processing":
            if phase.startswith(("dispatch", "gmm2_combine", "combine")):
                children.append(row)
        elif parent_phase == "dispatch_gmm1":
            if phase.startswith("dispatch_gmm1_") or phase.startswith("dispatch_"):
                children.append(row)
        elif parent_phase == "gmm2_combine":
            if phase.startswith("gmm2_combine_") or phase.startswith("combine_"):
                children.append(row)
    return _intervals(children)


def build_bubble_summary(instances: Table) -> Table:
    if not instances:
        return []

    phase_groups = _group_by(instances, "phase")
    rows: Table = []

    for parent_phase in ("processing", "dispatch_gmm1", "gmm2_combine"):
        parent_rows = phase_groups.get(parent_phase)
        if not parent_rows:
            continue

        parent_intervals = _merge_intervals(_intervals(parent_rows))
        parent_union = _interval_union_length(parent_intervals)
        child_intervals = _child_intervals_for_parent(instances, parent_phase)
        child_inside_parent = _intersect_intervals(parent_intervals, child_intervals)
        child_covered = _interval_union_length(child_inside_parent)
        bubble_intervals = _subtract_intervals(parent_intervals, child_inside_parent)
        bubble_us = _interval_union_length(bubble_intervals)
        max_gap = max((e - s for s, e in bubble_intervals), default=0.0)

        rows.append(
            {
                "parent_phase": parent_phase,
                "parent_union_us": parent_union,
                "child_covered_us": child_covered,
                "bubble_us": bubble_us,
                "bubble_ratio": bubble_us / parent_union if parent_union > 0 else 0.0,
                "gap_count": len(bubble_intervals),
                "max_gap_us": max_gap,
            }
        )

    rows.sort(key=lambda r: (_float(r.get("bubble_us")), _float(r.get("bubble_ratio"))), reverse=True)
    return rows


def build_trace_overview(instances: Table) -> dict:
    if not instances:
        return {
            "num_instances": 0,
            "num_phases": 0,
            "num_names": 0,
            "num_pids": 0,
            "num_tids": 0,
            "num_core_groups": 0,
            "core_groups": [],
            "trace_start": None,
            "trace_end": None,
            "total_wall_us": 0.0,
        }

    trace_start = min(_float(r.get("ts_start")) for r in instances)
    trace_end = max(_float(r.get("ts_end")) for r in instances)

    return {
        "num_instances": len(instances),
        "num_phases": _nunique(instances, "phase"),
        "num_names": _nunique(instances, "name"),
        "num_pids": _nunique(instances, "pid"),
        "num_tids": _nunique(instances, "tid"),
        "num_core_groups": _nunique(instances, "core_group"),
        "core_groups": sorted(
            {r.get("core_group") for r in instances if r.get("core_group")},
            key=lambda value: _CORE_GROUP_ORDER.get(str(value), 99),
        ),
        "trace_start": trace_start,
        "trace_end": trace_end,
        "total_wall_us": trace_end - trace_start,
    }
