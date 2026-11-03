#!/usr/bin/env python3
"""
TRACE_POINT 预处理器：扫描源码中的 TRACE_POINT("label", "B/E")，
为每个调用分配唯一 point_id（int32），替换源码后生成 point_map.json 映射表。
支持事件 ID 分配、嵌套校验、映射表导出。
"""

import json
import logging
import os
import re
import sys
from collections import defaultdict, deque
from typing import Dict, List

logger = logging.getLogger(__name__)


class TracePreprocessor:
    def __init__(self):
        self.next_event_id = 1
        self.next_point_id = 1
        self.max_base_point_id = 0xFFFFFFFF
        self.event_map: Dict[str, int] = {}
        self.point_map: Dict[int, Dict] = {}
        self.point_to_event: Dict[int, int] = {}

    def process_file(self, filepath: str, modify: bool = False) -> List[Dict]:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = r'TRACE_POINT\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)'
        matches = list(re.finditer(pattern, content))
        if not matches:
            return []

        items = []
        for m in matches:
            label, event_type = m.group(1), m.group(2)
            line = content[: m.start()].count("\n") + 1
            items.append({"match": m, "label": label, "event_type": event_type, "line": line,
                          "start": m.start(), "end": m.end()})
        items.sort(key=lambda x: x["line"])

        points = []
        for it in items:
            label, event_type = it["label"], it["event_type"]
            if label not in self.event_map:
                self.event_map[label] = self.next_event_id
                self.next_event_id += 1
            event_id = self.event_map[label]

            point_id = self.next_point_id
            if point_id > self.max_base_point_id:
                logger.error("ERROR: point_id overflow at %s:%s", filepath, it["line"])
                return []
            self.next_point_id += 1

            self.point_map[point_id] = {
                "label": label, "file": filepath, "line": it["line"],
                "event_type": event_type, "event_id": event_id,
            }
            self.point_to_event[point_id] = event_id
            points.append({"point_id": point_id, "event_id": event_id, "label": label,
                           "line": it["line"], "event_type": event_type,
                           "start": it["start"], "end": it["end"]})

        # nesting check (uppercase B/E only)
        stack = deque()
        for p in points:
            if p["event_type"] == "B":
                stack.append(p)
            elif p["event_type"] == "E":
                if stack and stack[-1]["label"] == p["label"]:
                    stack.pop()

        if modify and points:
            points_rev = sorted(points, key=lambda x: x["start"], reverse=True)
            for p in points_rev:
                content = content[: p["start"]] + str(p["point_id"]) + content[p["end"]:]
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("  replaced %d trace points in %s", len(points), filepath)

        return points

    def process_directory(self, src_path: str, modify: bool = False) -> List[Dict]:
        all_points = []
        for root, _, files in os.walk(src_path):
            for fn in files:
                if fn.endswith((".c", ".cpp", ".cc", ".h", ".hpp")):
                    all_points.extend(self.process_file(os.path.join(root, fn), modify))
        return all_points

    def save_mappings(self, output_dir: str):
        os.makedirs(output_dir, exist_ok=True)
        data = {}
        for pid, info in self.point_map.items():
            rel = os.path.relpath(info["file"], output_dir) if output_dir else info["file"]
            data[str(pid)] = {
                "label": info["label"], "file": rel, "line": info["line"],
                "event_type": info["event_type"], "event_id": info["event_id"],
            }
        out = os.path.join(output_dir, "point_map.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"points": data}, f, indent=2, ensure_ascii=False)
        logger.info(
            "mapping saved: %s (%d points, %d labels)",
            out,
            len(data),
            len(self.event_map),
        )


def main():
    import argparse

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="TRACE_POINT preprocessor")
    parser.add_argument("src", help="source file or directory")
    parser.add_argument("output", help="output directory for point_map.json")
    parser.add_argument("--modify", action="store_true", help="replace TRACE_POINT in source files")
    args = parser.parse_args()

    pp = TracePreprocessor()
    if os.path.isfile(args.src):
        pts = pp.process_file(args.src, args.modify)
    elif os.path.isdir(args.src):
        pts = pp.process_directory(args.src, args.modify)
    else:
        logger.error("not found: %s", args.src)
        sys.exit(1)
    if pts:
        pp.save_mappings(args.output)


if __name__ == "__main__":
    main()
