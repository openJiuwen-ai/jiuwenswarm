#!/usr/bin/env python3
"""print_blocks.py — Print stage JSON blocks in a readable format for the agent.

Stage01, stage02 and stage03 share one bounded representative-view budget so
no downstream stage can expand the model-visible context.
"""
import argparse
import logging
import pathlib
import sys

from environment_gate import ensure_environment

# Keep every web/image-stage script on the same selected interpreter.
ensure_environment("requests")

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

VIEW_MAX_BLOCKS = 120
VIEW_MAX_CHARS = 12_000
VIEW_TEXT_FLOOR = 36


def _evenly_spaced_indices(indices: list[int], count: int) -> list[int]:
    if count <= 0 or not indices:
        return []
    if count >= len(indices):
        return list(indices)
    if count == 1:
        return [indices[0]]
    last = len(indices) - 1
    chosen = {indices[round(i * last / (count - 1))] for i in range(count)}
    if len(chosen) < count:
        for idx in indices:
            chosen.add(idx)
            if len(chosen) == count:
                break
    return sorted(chosen)


def _select_bounded_indices(blocks: list[dict]) -> list[int]:
    if len(blocks) <= VIEW_MAX_BLOCKS:
        return list(range(len(blocks)))

    selected: set[int] = {0, len(blocks) - 1}
    priority_groups = [
        [i for i, b in enumerate(blocks) if b.get("type") == "heading" and int(b.get("level", 6)) <= 2],
        [i for i, b in enumerate(blocks) if b.get("type") == "image"],
        [i for i, b in enumerate(blocks) if b.get("type") == "heading" and int(b.get("level", 6)) > 2],
    ]
    for group in priority_groups:
        available = VIEW_MAX_BLOCKS - len(selected)
        if available <= 0:
            break
        remaining = [idx for idx in group if idx not in selected]
        selected.update(_evenly_spaced_indices(remaining, min(available, len(remaining))))

    available = VIEW_MAX_BLOCKS - len(selected)
    if available > 0:
        remaining = [i for i in range(len(blocks)) if i not in selected]
        selected.update(_evenly_spaced_indices(remaining, min(available, len(remaining))))
    return sorted(selected)


def _format_weight(fmt: str) -> int:
    if fmt in {"code", "editor"}:
        return 5
    if fmt in {"table", "definition"}:
        return 4
    if fmt in {"canvas", "js_text"}:
        return 3
    return 2


def _format_cap(fmt: str) -> int:
    if fmt in {"code", "editor"}:
        return 1_400
    if fmt in {"table", "definition"}:
        return 900
    if fmt in {"canvas", "js_text"}:
        return 600
    return 320


def _allocate_lengths(lengths: list[int], weights: list[int], budget: int) -> list[int]:
    if not lengths:
        return []
    if sum(lengths) <= budget:
        return list(lengths)
    base = min(VIEW_TEXT_FLOOR, max(0, budget) // len(lengths))
    allocated = [min(length, base) for length in lengths]
    remaining = max(0, budget - sum(allocated))
    active = {i for i, length in enumerate(lengths) if allocated[i] < length}
    while remaining > 0 and active:
        weight_sum = sum(weights[i] for i in active)
        progressed = False
        for i in list(active):
            share = max(1, remaining * weights[i] // max(1, weight_sum))
            add = min(share, lengths[i] - allocated[i], remaining)
            if add:
                allocated[i] += add
                remaining -= add
                progressed = True
            if allocated[i] >= lengths[i]:
                active.discard(i)
            if remaining <= 0:
                break
        if not progressed:
            break
    return allocated


def _truncate(text: str, allowed: int) -> str:
    if len(text) <= allowed:
        return text
    if allowed <= 4:
        return text[:allowed]
    return text[: allowed - 2].rstrip() + " …"


def _print_bounded(data: dict, found_stage: str) -> None:
    blocks = data.get("blocks", [])
    selected_indices = _select_bounded_indices(blocks)
    selected = [blocks[i] for i in selected_indices]

    header_lines = [
        f"SOURCE: {found_stage}",
        f"TITLE: {data.get('title', '')}",
        f"VIDEO_URLS: {data.get('video_urls', [])}",
        "BOUNDED_VIEW: shared stage01/stage02/stage03 budget; no pagination or continuation batch",
        f"BLOCK_COVERAGE: {len(selected)}/{len(blocks)}",
        "",
    ]

    records: list[dict] = []
    image_index = 0
    for block in selected:
        t = block["type"]
        src = block.get("source", "main")
        if t == "heading":
            indent = "  " * (block["level"] - 1)
            records.append({
                "kind": "fixed",
                "line": f"{indent}H{block['level']} [{src}]: {block.get('text', '')[:240]}",
            })
        elif t == "text":
            fmt = block.get("format", "text")
            text = block.get("text", "")
            records.append({
                "kind": "text",
                "prefix": f"  {fmt.upper()} [{src}]: ",
                "text": text,
                "cap": min(len(text), _format_cap(fmt)),
                "weight": _format_weight(fmt),
            })
        elif t == "image":
            image_index += 1
            path_field = block.get("path")
            raw_path = block.get("raw_path")
            status = block.get("review_status")
            if path_field:
                line = f"  IMG  [{src}]: path={path_field}  alt={block.get('alt', '')[:80]}"
            elif raw_path:
                alt = block.get("alt", "")[:80]
                line = f"  IMG#{image_index:03d} [{src}]: review={status or 'UNREVIEWED'}  alt={alt}"
            else:
                line = f"  IMG#{image_index:03d} [{src}]: alt={block.get('alt', '')[:80]}"
            records.append({"kind": "fixed", "line": line})

    fixed_chars = sum(len(line) + 1 for line in header_lines)
    fixed_chars += sum(len(record["line"]) + 1 for record in records if record["kind"] == "fixed")
    text_records = [record for record in records if record["kind"] == "text"]
    prefix_chars = sum(len(record["prefix"]) + 1 for record in text_records)
    text_budget = max(0, VIEW_MAX_CHARS - fixed_chars - prefix_chars - 96)
    lengths = [record["cap"] for record in text_records]
    weights = [record["weight"] for record in text_records]
    allocations = iter(_allocate_lengths(lengths, weights, text_budget))

    lines = list(header_lines)
    for record in records:
        if record["kind"] == "fixed":
            lines.append(record["line"])
        else:
            allowed = next(allocations, 0)
            lines.append(record["prefix"] + _truncate(record["text"], allowed))

    output = "\n".join(lines)
    if len(output) > VIEW_MAX_CHARS:
        marker = f"\n[{found_stage} view ended at the shared character budget]"
        output = output[: VIEW_MAX_CHARS - len(marker)].rstrip() + marker
    logger.info("%s", output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--stage", choices=("stage01", "stage02", "stage03"), default=None)
    args = parser.parse_args()

    path = None
    found_stage = None
    stage_files = (f"{args.stage}.json",) if args.stage else ("stage03.json", "stage02.json", "stage01.json")
    for stage_file in stage_files:
        candidate = common.work_path(args.slug, stage_file)
        if candidate.exists():
            path = candidate
            found_stage = stage_file
            break

    if path is None:
        logger.error("[print_blocks] ERROR: no stage JSON found for slug '%s'", args.slug)
        sys.exit(1)

    data = common.load_json(path)
    _print_bounded(data, found_stage)


if __name__ == "__main__":
    main()
