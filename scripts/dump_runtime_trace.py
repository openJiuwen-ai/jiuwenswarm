# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, human-readable dump of runtime trace databases.

Accepts one or more database files or role directories (``.../runtime``).
Databases from Front and Worker are merged per ``trace_id`` so a spawn span in
one process and the child startup span in another appear in one tree.

Normal shutdown databases are opened in place with ``mode=ro``. A database
with live ``-wal``/``-shm`` sidecars (killed process) is copied to a temporary
directory first; the source is never written.

Usage:
    python scripts/dump_runtime_trace.py <db-or-dir> [<db-or-dir> ...]
        [--run-id ID] [--copy] [--all-attributes] [--ids]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from shutil import copy2

_BASE_ATTRIBUTES = {
    "runtime.scope",
    "runtime.run_id",
    "runtime.role",
    "runtime.pid",
    "runtime.outcome",
}

_TERMINAL_SPANS = {
    "worker.exited",
    "worker.ipc_lost",
    "worker.unresponsive",
    "worker.failed",
}


@dataclass
class _Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    outcome: str | None
    start_ns: int
    end_ns: int
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass
class _Database:
    path: Path
    run_id: str
    role: str
    pid: int
    started_at: int
    closed: bool
    spans: list[_Span] = field(default_factory=list)


def _open_readonly(path: Path, *, copy: bool) -> sqlite3.Connection:
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
    needs_copy = copy or any(sidecar.exists() for sidecar in sidecars)
    if not needs_copy:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    temp_dir = tempfile.mkdtemp(prefix="runtime-trace-dump-")
    target = Path(temp_dir) / path.name
    copy2(path, target)
    for sidecar in sidecars:
        if sidecar.exists():
            copy2(sidecar, Path(str(target) + sidecar.name[len(path.name):]))
    return sqlite3.connect(f"file:{target}?mode=ro", uri=True)


def _decode_span(raw: bytes) -> dict | None:
    try:
        payload = json.loads(zlib.decompress(bytes(raw)))
        return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    except Exception:  # noqa: BLE001 - keep reading the rest of the database
        return None


def _attribute_value(value: dict) -> object:
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "arrayValue",
        "kvlistValue",
    ):
        if key in value:
            return value[key]
    return None


def _otel_attributes(span: dict | None) -> dict[str, object]:
    if not span:
        return {}
    result: dict[str, object] = {}
    for item in span.get("attributes") or []:
        key = item.get("key")
        if isinstance(key, str):
            result[key] = _attribute_value(item.get("value") or {})
    return result


def _load_database(path: Path, *, copy: bool) -> _Database | None:
    conn = _open_readonly(path, copy=copy)
    try:
        meta = conn.execute(
            "SELECT run_id, role, pid, started_at, closed_at FROM meta"
        ).fetchone()
        if meta is None:
            print(f"warning: {path} has no meta row; skipped", file=sys.stderr)
            return None
        database = _Database(
            path=path,
            run_id=meta[0],
            role=meta[1],
            pid=meta[2],
            started_at=meta[3],
            closed=meta[4] is not None,
        )
        rows = conn.execute(
            "SELECT trace_id, span_id, parent_span_id, name, outcome, "
            "start_ns, end_ns, raw_json FROM span_records "
            "ORDER BY start_ns, ingest_seq"
        ).fetchall()
        for row in rows:
            otlp = _decode_span(row[7])
            name = row[3] or (otlp or {}).get("name") or ""
            attributes = _otel_attributes(otlp)
            database.spans.append(
                _Span(
                    trace_id=row[0],
                    span_id=row[1],
                    parent_span_id=row[2],
                    name=name,
                    outcome=row[4] or attributes.get("runtime.outcome"),
                    start_ns=row[5],
                    end_ns=row[6],
                    attributes=attributes,
                )
            )
        return database
    finally:
        conn.close()


def _collect_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        candidate = Path(raw)
        if candidate.is_dir():
            found = sorted(candidate.glob("*/*.sqlite3"))
        elif candidate.is_file():
            found = [candidate]
        else:
            print(f"not found: {candidate}", file=sys.stderr)
            continue
        for path in found:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return paths


def _fmt_time(ns: int) -> str:
    seconds, remainder = divmod(ns, 1_000_000_000)
    return (
        datetime.fromtimestamp(seconds).strftime("%H:%M:%S.")
        + f"{remainder // 1_000_000:03d}"
    )


def _fmt_date(ns: int) -> str:
    seconds, remainder = divmod(ns, 1_000_000_000)
    return (
        datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S.")
        + f"{remainder // 1_000_000:03d}"
    )


def _fmt_duration(ns: int) -> str:
    if ns <= 0:
        return "instant"
    if ns < 1_000:
        return f"{ns}ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.1f}ms"
    return f"{ns / 1_000_000_000:.3f}s"


def _fmt_offset(ns: int) -> str:
    if ns == 0:
        return "+0"
    return ("+" if ns > 0 else "-") + _fmt_duration(abs(ns))


def _level_of(name: str) -> str:
    if name in {"startup", "shutdown"}:
        return "root"
    if name in _TERMINAL_SPANS:
        return "terminal"
    if name.startswith("worker."):
        return "event"
    if name.startswith("task."):
        return "task"
    return "phase"


def _short_key(key: str) -> str:
    return key[len("runtime."):] if key.startswith("runtime.") else key


def _fmt_attributes(
    attributes: dict[str, object],
    *,
    all_attributes: bool,
    span_name: str = "",
) -> str:
    items = []
    for key, value in sorted(attributes.items()):
        if not all_attributes and key in _BASE_ATTRIBUTES:
            continue
        if key == "runtime.outcome":
            continue
        text = str(value)
        if " " in text:
            text = f'"{text}"'
        label = _short_key(key)
        if key == "runtime.worker_pid" and span_name == "worker.spawn":
            # Windows launchers make Popen return a launcher pid that differs
            # from the real Worker pid reported in HELLO.
            label = "spawn_pid"
        items.append(f"{label}={text}")
    return " ".join(items)


def _build_index(
    spans: list[_Span],
) -> tuple[list[_Span], dict[str | None, list[_Span]], set[str]]:
    ids = {span.span_id for span in spans}
    by_parent: dict[str | None, list[_Span]] = {}
    for span in spans:
        by_parent.setdefault(span.parent_span_id, []).append(span)
    for children in by_parent.values():
        children.sort(key=lambda item: item.start_ns)

    roots = [span for span in spans if span.parent_span_id not in ids]
    roots.sort(key=lambda item: item.start_ns)
    # A root whose parent exists outside the loaded set is a child of an
    # upstream/other-process span that was not archived here.
    upstream = {
        span.parent_span_id for span in roots if span.parent_span_id
    }
    return roots, by_parent, upstream


def _subtree_rows(
    root: _Span, by_parent: dict[str | None, list[_Span]]
) -> list[tuple[_Span, str]]:
    rows: list[tuple[_Span, str]] = []

    def walk(span: _Span, prefix: str, connector: str, child_prefix: str) -> None:
        rows.append((span, f"{prefix}{connector}{span.name}"))
        children = by_parent.get(span.span_id, [])
        for index, child in enumerate(children):
            last = index == len(children) - 1
            walk(
                child,
                child_prefix,
                "`-- " if last else "|-- ",
                child_prefix + ("    " if last else "|   "),
            )

    walk(root, "", "", "")
    return rows


def _print_database(database: _Database) -> None:
    state = "yes" if database.closed else "no"
    print(
        f"  db  {database.path}  role={database.role} pid={database.pid} "
        f"run={database.run_id} started={_fmt_date(database.started_at)} "
        f"closed={state} spans={len(database.spans)}"
    )


def _root_label(span: _Span) -> str:
    role = span.attributes.get("runtime.role", "?")
    pid = span.attributes.get("runtime.pid", "?")
    run_id = str(span.attributes.get("runtime.run_id", "") or "")
    run = run_id[:8] if run_id else "?"
    return f"role={role} pid={pid} run={run}"


def _print_root_block(
    root: _Span,
    index: int,
    total: int,
    by_parent: dict[str | None, list[_Span]],
    *,
    show_ids: bool,
    all_attributes: bool,
) -> None:
    rows = _subtree_rows(root, by_parent)
    start_ns = root.start_ns
    end_ns = max(span.end_ns for span, _ in rows)
    print(
        f"[{index}/{total}] {root.name}  {_root_label(root)}  "
        f"range {_fmt_time(start_ns)} -> {_fmt_time(end_ns)}  "
        f"subtree={_fmt_duration(end_ns - start_ns)}"
    )

    name_width = min(64, max(len(name) for _, name in rows) + 1)
    print(
        f"  {'time':<12}  {'offset':<10}  {'duration':<10}  "
        f"{'level':<8}  {'name':<{name_width}}  {'outcome':<8}  attributes"
    )
    for span, name in rows:
        duration = _fmt_duration(span.end_ns - span.start_ns)
        outcome = span.outcome or "-"
        attributes = _fmt_attributes(
            span.attributes,
            all_attributes=all_attributes,
            span_name=span.name,
        )
        if show_ids:
            ids_text = f"span={span.span_id} parent={span.parent_span_id or '-'}"
            attributes = f"{attributes} {ids_text}".strip()
        print(
            f"  {_fmt_time(span.start_ns):<12}  "
            f"{_fmt_offset(span.start_ns - start_ns):<10}  "
            f"{duration:<10}  {_level_of(span.name):<8}  "
            f"{name:<{name_width}}  {outcome:<8}  {attributes}"
        )
    print()


def _print_trace(
    trace_id: str, spans: list[_Span], *, show_ids: bool, all_attributes: bool
) -> None:
    roots, by_parent, upstream = _build_index(spans)
    if not roots:
        return
    upstream_text = (
        " external_parent=" + ",".join(sorted(parent[:8] for parent in upstream))
        if upstream
        else ""
    )
    print(
        f"trace {trace_id}  spans={len(spans)} roots={len(roots)}"
        f"{upstream_text}"
    )
    print(
        "  each [n/N] is one independent root tree; offsets are relative to "
        "that root. external_parent is an upstream span or this run's "
        "generated virtual parent; neither is stored in the database."
    )
    print()
    for index, root in enumerate(roots, start=1):
        _print_root_block(
            root,
            index,
            len(roots),
            by_parent,
            show_ids=show_ids,
            all_attributes=all_attributes,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Human-readable runtime trace dump (read-only)."
    )
    parser.add_argument(
        "targets",
        nargs="+",
        help="database file(s) or role directory/directories",
    )
    parser.add_argument("--run-id", help="only show this run id")
    parser.add_argument(
        "--copy",
        action="store_true",
        help="always copy databases before reading (killed process)",
    )
    parser.add_argument(
        "--all-attributes",
        action="store_true",
        help="also print the base runtime attributes",
    )
    parser.add_argument("--ids", action="store_true", help="print span ids")
    args = parser.parse_args(argv)

    paths = _collect_paths(args.targets)
    if not paths:
        print("no runtime trace database found", file=sys.stderr)
        return 2

    databases: list[_Database] = []
    for path in paths:
        loaded = _load_database(path, copy=args.copy)
        if loaded is None:
            continue
        if args.run_id and loaded.run_id != args.run_id:
            continue
        databases.append(loaded)
    if not databases:
        print("no matching runtime trace database", file=sys.stderr)
        return 1

    print("runtime trace")
    for database in databases:
        _print_database(database)
    print()
    print(
        'note: "instant" is a point event (start == end). '
        "startup.phase.config is cumulative from process entry, so it "
        "overlaps the adjacent import phases by design."
    )
    print()

    merged: dict[str, list[_Span]] = {}
    for database in databases:
        for span in database.spans:
            merged.setdefault(span.trace_id, []).append(span)
    for trace_id, spans in merged.items():
        _print_trace(
            trace_id,
            spans,
            show_ids=args.ids,
            all_attributes=args.all_attributes,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
