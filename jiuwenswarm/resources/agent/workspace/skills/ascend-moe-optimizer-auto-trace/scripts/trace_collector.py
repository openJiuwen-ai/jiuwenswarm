#!/usr/bin/env python3
"""
从 rank*.pt 和 point_map.json 生成 Chrome Trace JSON。
支持 64 位组合 ID 解析、B/E 配对、区间深度过滤。
"""

import glob
import json
import logging
import os
import sys
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

import torch

logger = logging.getLogger(__name__)

CLOCK_DIVISOR = 50.0  # MHz，cycle → us


def extract_point_id_parts(combined_id: int) -> Tuple[int, int]:
    base = combined_id & 0xFFFFFFFF
    extra = (combined_id >> 32) & 0xFFFFFFFF
    if base >= 0x80000000:
        base -= 0x100000000
    if extra >= 0x80000000:
        extra -= 0x100000000
    return int(base), int(extra)


def parse_profiling_data(tensor: torch.Tensor, core_type: int, core_id: int) -> List[Dict]:
    prof_size = tensor.shape[1]
    raw_count = int(tensor[core_id][0].item())
    record_count = raw_count - 1
    if record_count <= 0:
        return []
    initial_ts = tensor[core_id][-1].item()
    records = []
    max_rec = min(record_count, (prof_size - 2) // 2)
    for i in range(max_rec):
        combined_id = tensor[core_id][1 + i].item()
        raw_ts = tensor[core_id][-2 - i].item()
        diff = (raw_ts - initial_ts) & 0xFFFFFFFFFFFFFFFF
        base_id, extra_id = extract_point_id_parts(combined_id)
        records.append({
            "timestamp_us": diff / CLOCK_DIVISOR,
            "timestamp_cycles": diff,
            "combined_id": int(combined_id),
            "base_point_id": base_id,
            "extra_id": extra_id,
            "core_type": core_type,
            "core_id": core_id,
        })
    return records


def load_all_ranks(data_dir: str) -> Dict[int, List[Dict]]:
    all_data = {}
    for pt_file in sorted(glob.glob(os.path.join(data_dir, "rank*.pt"))):
        rank_id = int(os.path.basename(pt_file).split(".")[0][4:])
        try:
            split_tensors = torch.load(pt_file, map_location="cpu")
            rank_records: List[Dict] = []
            for type_idx, tensor in enumerate(split_tensors):
                for cid in range(tensor.shape[0]):
                    for r in parse_profiling_data(tensor, type_idx, cid):
                        r["rank_id"] = rank_id
                        rank_records.append(r)
            all_data[rank_id] = rank_records
            logger.info("Rank %s: %d records", rank_id, len(rank_records))
        except Exception as e:
            logger.warning("Failed to load %s: %s", pt_file, e)
    return all_data


def load_mapping(path: str) -> Dict:
    if not os.path.exists(path):
        logger.error("ERROR: point_map not found: %s", os.path.abspath(path))
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    inner = data.get("points", data) if isinstance(data, dict) and "points" in data else data
    if not isinstance(inner, dict):
        logger.error(
            'ERROR: point_map must be a JSON object or {"points": {...}}',
        )
        return {}
    # Keys must be strings for lookup (str(base_point_id)).
    return {str(k): v for k, v in inner.items()}


def diagnose_mapping_overlap(all_data: Dict[int, List[Dict]], mapping: Dict) -> None:
    """Print how many profiling base_point_ids exist in point_map (root cause when skipped_no_mapping is high)."""
    uniq = set()
    for rid in all_data:
        for rec in all_data[rid]:
            uniq.add(str(rec["base_point_id"]))
    mk = set(mapping.keys())
    inter = uniq & mk
    logger.warning("diagnose: unique base_point_id in rank*.pt: %d", len(uniq))
    logger.warning("diagnose: point_map keys: %d", len(mk))
    logger.warning("diagnose: intersection (ids that can be decoded to labels): %d", len(inter))
    if uniq and mk and not inter:
        logger.warning(
            "diagnose: NO OVERLAP — point_map.json does not match this kernel build. "
            "Use the point_map.json from the same ascend_kernels_*_proj directory "
            "that was compiled into the installed OPP.",
        )
    missing = uniq - mk
    if missing:
        def _ik(x: str) -> int:
            try:
                return int(x)
            except ValueError:
                return 0

        samp = sorted(missing, key=_ik)[:40]
        logger.warning(
            "diagnose: sample ids present in pt but missing from point_map: %s",
            samp,
        )
    if mk:
        sampk = sorted(mk, key=lambda x: int(x) if str(x).lstrip("-").isdigit() else 0)[:25]
        logger.warning("diagnose: sample point_map keys: %s", sampk)


def build_interval_tree(intervals: List[Dict]):
    if not intervals:
        return
    intervals.sort(key=lambda x: x["s"])
    n = len(intervals)
    parent = [None] * n
    for i in range(n):
        cur = intervals[i]
        best, best_end = None, float("inf")
        for j in range(n):
            if i == j:
                continue
            oth = intervals[j]
            if oth["s"] <= cur["s"] and oth["e"] >= cur["e"] and (oth["e"] - oth["s"]) > (cur["e"] - cur["s"]):
                if oth["e"] < best_end:
                    best, best_end = j, oth["e"]
        parent[i] = best
    children = [[] for _ in range(n)]
    for i, p in enumerate(parent):
        if p is not None:
            children[p].append(i)
    depth = [0] * n

    def dfs(node):
        if not children[node]:
            depth[node] = 1
        else:
            depth[node] = max(dfs(c) for c in children[node]) + 1
        return depth[node]

    for i in range(n):
        if parent[i] is None:
            dfs(i)
    for i, iv in enumerate(intervals):
        iv["obj"]["depth_from_leaf"] = depth[i]


def generate_chrome_trace(
    all_data: Dict[int, List[Dict]],
    mapping: Dict,
    output_file: str = "chrome_trace.json",
    extra_mode: str = "seq",
    depth: int = 0,
):
    metadata = []
    sorted_ranks = sorted(all_data.keys())
    rank_to_pid = {}
    for idx, rid in enumerate(sorted_ranks):
        rank_to_pid[rid] = idx
        metadata.append({"name": "process_name", "ph": "M", "pid": idx, "args": {"name": "Rank"}})
        metadata.append({"name": "process_sort_index", "ph": "M", "pid": idx, "args": {"sort_index": rid}})

    tid_groups: Dict[Tuple, List[Dict]] = defaultdict(list)
    tid_meta_set = set()

    skipped_no_mapping = 0
    for rid in sorted_ranks:
        pid = rank_to_pid[rid]
        for rec in all_data[rid]:
            bp = str(rec["base_point_id"])
            info = mapping.get(bp, {})
            et = info.get("event_type")
            if not et:
                skipped_no_mapping += 1
                continue
            label = info.get("label", f"point_{bp}")
            raw_extra = rec["extra_id"]
            if extra_mode == "seq":
                high24 = (raw_extra >> 8) & 0xFFFFFF
                low8 = raw_extra & 0xFF
                base_name = f"{label} [extra:{low8}]"
                full_name = f"{base_name} #{high24}"
                seq = high24
            else:
                base_name = f"{label} [extra:{raw_extra}]"
                full_name = base_name
                seq = None

            tid_str = f"type{rec['core_type']}_core{rec['core_id']:03d}"
            tid_key = (rid, rec["core_type"], rec["core_id"])

            if tid_key not in tid_meta_set:
                tid_meta_set.add(tid_key)
                core_no = rec["core_id"]
                metadata.append({
                    "name": "thread_name",
                    "ph": "M",
                    "pid": pid,
                    "tid": tid_str,
                    "args": {"name": f"Core {core_no}"},
                })
                metadata.append({
                    "name": "thread_sort_index",
                    "ph": "M",
                    "pid": pid,
                    "tid": tid_str,
                    "args": {"sort_index": core_no},
                })

            item = {
                "base": {
                    "cat": "trace", "ts": rec["timestamp_us"], "pid": pid, "tid": tid_str,
                    "ph": et.upper(),
                    "args": {"base_point_id": rec["base_point_id"], "extra_id": raw_extra,
                             "rank_id": rid, "core_type": rec["core_type"], "core_id": rec["core_id"],
                             "file": info.get("file", ""), "line": info.get("line", 0)},
                },
                "ph": et.upper(), "base_name": base_name, "full_name": full_name,
                "ts": rec["timestamp_us"], "cycles": rec["timestamp_cycles"],
                "seq": seq,
            }
            tid_groups[tid_key].append(item)

    all_intervals = []
    unpaired = []

    for tid_key, items in tid_groups.items():
        items.sort(key=lambda x: x["ts"])
        groups = defaultdict(list)
        for it in items:
            groups[it["base_name"]].append(it)

        for bname, group in groups.items():
            group.sort(key=lambda x: x["ts"])
            if extra_mode == "seq":
                i = 0
                while i < len(group):
                    it = group[i]
                    if it["ph"] == "B":
                        j = i + 1
                        while j < len(group) and not (group[j]["ph"] == "E" and group[j]["seq"] == it["seq"]):
                            j += 1
                        if j < len(group):
                            b_ev = it["base"].copy()
                            b_ev["name"] = it["full_name"]
                            e_ev = group[j]["base"].copy()
                            e_ev["name"] = it["full_name"]
                            iv = {
                                "name": it["full_name"],
                                "start_event": b_ev,
                                "end_event": e_ev,
                                "s": it["cycles"],
                                "e": group[j]["cycles"],
                            }
                            iv["obj"] = iv
                            all_intervals.append(iv)
                            i = j + 1
                        else:
                            ev = it["base"].copy()
                            ev["name"] = it["full_name"]
                            unpaired.append(ev)
                            i += 1
                    elif it["ph"] == "E":
                        ev = it["base"].copy()
                        ev["name"] = it["full_name"]
                        unpaired.append(ev)
                        i += 1
                    else:
                        i += 1
            else:
                bq = deque()
                seq_ctr = 0
                for it in group:
                    if it["ph"] == "B":
                        it["_seq"] = seq_ctr
                        seq_ctr += 1
                        bq.append(it)
                    elif it["ph"] == "E":
                        if bq:
                            bi = bq.popleft()
                            fn = f"{bname} #{bi['_seq']}"
                            b_ev = bi["base"].copy()
                            b_ev["name"] = fn
                            e_ev = it["base"].copy()
                            e_ev["name"] = fn
                            iv = {
                                "name": fn,
                                "start_event": b_ev,
                                "end_event": e_ev,
                                "s": bi["cycles"],
                                "e": it["cycles"],
                            }
                            iv["obj"] = iv
                            all_intervals.append(iv)
                        else:
                            ev = it["base"].copy()
                            ev["name"] = f"{bname} #-1"
                            unpaired.append(ev)
                for leftover in bq:
                    ev = leftover["base"].copy()
                    ev["name"] = f"{bname} #{leftover['_seq']}"
                    unpaired.append(ev)

    final_events = []
    if depth == 0:
        for iv in all_intervals:
            final_events.extend([iv["start_event"], iv["end_event"]])
    else:
        by_thread = defaultdict(list)
        for iv in all_intervals:
            key = (iv["start_event"]["pid"], iv["start_event"]["tid"])
            by_thread[key].append(iv)
        for key, ivs in by_thread.items():
            build_interval_tree([{"s": i["s"], "e": i["e"], "obj": i} for i in ivs])
        for iv in all_intervals:
            if iv.get("depth_from_leaf", 0) <= depth:
                final_events.extend([iv["start_event"], iv["end_event"]])

    final_events.extend(unpaired)

    trace = {
        "traceEvents": metadata + final_events,
        "displayTimeUnit": "us",
        "otherData": {
            "version": "1.0",
            "total_events": len(final_events),
            "total_ranks": len(all_data),
            "clock_divisor": CLOCK_DIVISOR,
            "extra_mode": extra_mode,
            "depth": depth,
            "skipped_no_mapping": skipped_no_mapping,
        },
    }
    with open(output_file, "w") as f:
        json.dump(trace, f, indent=2)
    logger.info(
        "Chrome trace generated: %s (%d events)",
        output_file,
        len(final_events),
    )
    if skipped_no_mapping:
        logger.warning(
            "WARNING: %s raw records skipped (no point_map entry for base_point_id). "
            "Use point_map.json from the same build as the installed kernel.",
            skipped_no_mapping,
        )
    return trace


def main():
    import argparse

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Generate Chrome Trace JSON from profiling data.")
    parser.add_argument("data_dir", help="directory containing rank*.pt files")
    parser.add_argument("mapping_file", help="point_map.json from preprocessor")
    parser.add_argument("-o", "--output", default="chrome_trace.json")
    parser.add_argument("--clock-divisor", type=float, default=50.0, help="clock frequency in MHz")
    parser.add_argument("--extra-mode", choices=["legacy", "seq"], default="seq")
    parser.add_argument("--depth", type=int, default=0, help="filter depth from leaf: 0=all")
    args = parser.parse_args()

    global CLOCK_DIVISOR
    CLOCK_DIVISOR = args.clock_divisor

    all_data = load_all_ranks(args.data_dir)
    if not all_data:
        logger.error("No rank*.pt files found in %s", args.data_dir)
        return
    n_raw = sum(len(v) for v in all_data.values())
    if n_raw == 0:
        logger.warning(
            "No profiling records parsed from rank*.pt (per-core counters [*,0] look empty). "
            "Dump profiling_data only after torch_npu.npu.synchronize() so device writes are visible on host.",
        )
    mapping = load_mapping(args.mapping_file)
    logger.warning(
        "diagnose: point_map file: %s",
        os.path.abspath(args.mapping_file),
    )
    diagnose_mapping_overlap(all_data, mapping)
    generate_chrome_trace(all_data, mapping, args.output, args.extra_mode, args.depth)


if __name__ == "__main__":
    main()
