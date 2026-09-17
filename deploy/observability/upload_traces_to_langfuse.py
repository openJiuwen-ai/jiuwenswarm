# coding: utf-8
"""Upload ``.jsonl`` trace files to Langfuse via the local OTel collector.

Accepts either a single file or a directory:

    python upload_traces_to_langfuse.py <path>
    python upload_traces_to_langfuse.py --file traces-2026-06-29.jsonl
    python upload_traces_to_langfuse.py --dir <traces_dir>

Where ``<path>`` is:
  - a ``.jsonl`` file → upload every line in it
  - a directory     → upload every ``*.jsonl`` directly under it (flat,
                      no sub-folder walking)

With no arguments, the default traces directory is used —
``~/.jiuwenswarm/.trace`` (overridable via ``JIUWENSWARM_DATA_DIR``),
matching the file exporter's output path configured in team_manager.

The file exporter writes one per-day ``traces-<YYYY-MM-DD>.jsonl`` whose
lines are spans from potentially many traces, interleaved. Every line
is a standalone OTLP JSON ``ExportTraceServiceRequest`` carrying a
single span — just POST each line to the collector. No reconstruction,
no merging. The collector splits traces by the ``traceId`` carried on
each span, so interleaving is irrelevant for ingestion.
``session.id`` (if present) is read by Langfuse from span attributes,
not the filename.

After upload, the script prints the unique trace IDs ingested, parsed
from each uploaded line's ``resourceSpans[].scopeSpans[].spans[].traceId``.

Prerequisites:
    docker-compose up -d   # from deploy/observability/

The collector listens on :4318 (OTLP HTTP, no auth).
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_logger = logging.getLogger(__name__)

# NOTE: use 127.0.0.1, not "localhost". On macOS, getaddrinfo("localhost")
# returns IPv6 ::1 first and Python's socket.create_connection stops at the
# first address whose TCP handshake completes. Docker Desktop's IPv6 loopback
# port-forward accepts the connection but cannot relay it to the container,
# answering 502 Bad Gateway with an empty body — so every POST fails. The
# IPv4 path (127.0.0.1) works. curl avoids this via Happy Eyeballs; Python does
# not fall back once ::1 connects. See deploy/observability/README.md.
_COLLECTOR = "http://127.0.0.1:4318/v1/traces"


def _default_traces_dir() -> str:
    """Return the default traces directory, matching the file exporter output path.

    Priority (mirrors ``team_manager`` → ``get_user_workspace_dir()``):
      1. ``JIUWENSWARM_DATA_DIR`` env / ``.trace``
      2. ``~/.jiuwenswarm/.trace``
    """
    data_dir = os.getenv("JIUWENSWARM_DATA_DIR")
    if data_dir:
        return str(Path(data_dir) / ".trace")
    return str(Path.home() / ".jiuwenswarm" / ".trace")


def _iter_lines(path: str):
    """Yield non-empty stripped lines from a .jsonl file."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


def _extract_trace_id(line_body: bytes) -> str | None:
    """Parse one OTLP JSON line and return its traceId, or None."""
    try:
        data = json.loads(line_body)
    except json.JSONDecodeError:
        return None
    for rs in data.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                tid = sp.get("traceId")
                if isinstance(tid, str):
                    return tid
    return None


def _post_line(body: bytes, endpoint: str) -> bool:
    """POST one OTLP JSON line to the collector. Returns True on success."""
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except urllib.error.HTTPError as e:
        snippet = e.read()[:200]
        _logger.warning("HTTP %d: %s", e.code, snippet)
    except urllib.error.URLError as e:
        _logger.warning("url error: %s", e)
    except OSError as e:
        _logger.warning("net error: %s", e)
    return False


def _upload_one(path: str, endpoint: str) -> tuple[int, int, list[str]]:
    """Upload every line of one ``.jsonl`` file.

    Returns (lines_ok, lines_fail, trace_ids) where trace_ids are the
    unique traceIds seen in successfully uploaded lines.
    """
    ok = 0
    fail = 0
    seen: set[str] = set()
    trace_ids: list[str] = []
    for line in _iter_lines(path):
        body = line.encode("utf-8")
        tid = _extract_trace_id(body)
        if _post_line(body, endpoint):
            ok += 1
            if tid and tid not in seen:
                seen.add(tid)
                trace_ids.append(tid)
        else:
            fail += 1
    return ok, fail, trace_ids


def _collect_files(path: str) -> list[str] | None:
    """Return list of .jsonl files to upload from a file/dir path.

    Returns None if path doesn't exist; empty list if dir has no .jsonl.
    Only top-level ``*.jsonl`` under a directory are picked up (flat
    layout — matches the file exporter's per-day output).
    """
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "*.jsonl")))
    return None


def _resolve_input_path(args: argparse.Namespace) -> str | None:
    """Pick the input path from positional / --dir / --file (in that order)."""
    if args.path:
        return args.path
    if args.dir:
        return args.dir
    if args.file:
        return args.file
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Upload per-day .jsonl trace files to collector")
    parser.add_argument(
        "path",
        nargs="?",
        help="trace .jsonl file or directory containing .jsonl files "
             "(default: ~/.jiuwenswarm/.trace)",
    )
    parser.add_argument(
        "--dir",
        help="directory containing .jsonl trace files (alternative to positional path)",
    )
    parser.add_argument(
        "--file",
        help="single .jsonl trace file (alternative to positional path)",
    )
    parser.add_argument("--endpoint", default=_COLLECTOR)
    args = parser.parse_args()

    path = _resolve_input_path(args) or _default_traces_dir()

    files = _collect_files(path)
    if files is None:
        _logger.error("path not found: %s", path)
        return 2
    if not files:
        _logger.error("no *.jsonl found under %s", path)
        return 2

    _logger.info("source=%s  files=%d  endpoint=%s", path, len(files), args.endpoint)

    total_ok = 0
    total_fail = 0
    uploaded_trace_ids: list[str] = []
    t0 = time.time()
    for fpath in files:
        ok, fail, trace_ids = _upload_one(fpath, args.endpoint)
        total_ok += ok
        total_fail += fail
        uploaded_trace_ids.extend(trace_ids)

    elapsed = time.time() - t0
    _logger.info(
        "total_lines=%d ok=%d fail=%d elapsed=%.1fs",
        total_ok + total_fail,
        total_ok,
        total_fail,
        elapsed,
    )

    unique_ids = list(dict.fromkeys(uploaded_trace_ids))
    if unique_ids:
        _logger.info("trace_ids (%d):", len(unique_ids))
        for tid in unique_ids:
            _logger.info("  %s", tid)
    else:
        _logger.warning("no trace ids parsed from uploaded files")

    return 0 if total_fail == 0 and total_ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
