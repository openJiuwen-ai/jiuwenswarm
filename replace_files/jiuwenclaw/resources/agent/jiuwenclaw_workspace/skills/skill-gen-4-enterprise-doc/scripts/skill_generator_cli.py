#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""CLI: SOP plain-text extraction and URL fetch for this skill package.

Appends this file's directory to ``sys.path`` (not the front) so ``skill_gen`` imports
without shadowing stdlib. Pass an absolute path to this script; do not rely on process cwd.

Examples:

    python3 /ABS/.../scripts/skill_generator_cli.py sop-text --sop-file /ABS/SOP.md \\
        [--out-text /ABS/out.txt] [--print-raw-chars]

    python3 /ABS/.../scripts/skill_generator_cli.py url-fetch --url 'https://...' [--out-json /ABS/out.json]

Full ``SOPStructure`` JSON comes from ``skill_gen.sop_parser.parse_sop_file`` or
``parse_sop_raw_text`` plus ``invoke_llm_json`` (see ``reference/sop-structure-pipeline.md``).

End-to-end draft flow: ``../SKILL.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_stdout_payload_log = logging.getLogger(f"{__name__}.stdout_payload")


def _ensure_skill_gen_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parent
    s = str(scripts_dir)
    if s not in sys.path:
        sys.path.append(s)


def _ensure_stdout_payload_log() -> logging.Logger:
    if not _stdout_payload_log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _stdout_payload_log.addHandler(handler)
        _stdout_payload_log.setLevel(logging.INFO)
        _stdout_payload_log.propagate = False
    return _stdout_payload_log


def _emit_stdout_json(payload: Any) -> None:
    _ensure_stdout_payload_log().info("%s", json.dumps(payload, ensure_ascii=False, indent=2))


async def _cmd_sop_text(args: argparse.Namespace) -> int:
    sop_path = args.sop_file.expanduser().resolve()
    if not sop_path.is_file():
        logger.error("error: file not found: %s", sop_path)
        return 2

    from skill_gen.sop_parser import _extract_raw_text

    raw = await _extract_raw_text(sop_path)
    if args.print_raw_chars:
        _ensure_stdout_payload_log().info("%s", len(raw))
        return 0
    if args.out_text:
        args.out_text.parent.mkdir(parents=True, exist_ok=True)
        args.out_text.write_text(raw, encoding="utf-8")
        return 0
    _ensure_stdout_payload_log().info("%s", raw)
    return 0


async def _cmd_url_fetch(args: argparse.Namespace) -> int:
    from skill_gen.url_ingest import fetch_pages_from_url

    url = (args.url or "").strip()
    if not url:
        logger.error("error: empty --url")
        return 2
    pages = await fetch_pages_from_url(url)
    payload = [asdict(p) for p in pages]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text, encoding="utf-8")
    else:
        _ensure_stdout_payload_log().info("%s", text)
    return 0


async def _async_main() -> int:
    parser = argparse.ArgumentParser(
        description="skill-gen-4-enterprise-doc scripts (SOP text, URL fetch).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser(
        "sop-text",
        help="Extract plain text from an SOP file (same ingestion as parse_sop_file). No SOPStructure JSON.",
    )
    p_st.add_argument("--sop-file", type=Path, required=True)
    p_st.add_argument("--out-text", type=Path, default=None, help="Write UTF-8 text to this path.")
    p_st.add_argument("--print-raw-chars", action="store_true", help="Print character count only.")
    p_st.set_defaults(handler=_cmd_sop_text)

    p_uf = sub.add_parser("url-fetch", help="Fetch HTTP(S) page or WeChat article → text + metadata JSON.")
    p_uf.add_argument("--url", type=str, required=True)
    p_uf.add_argument("--out-json", type=Path, default=None)
    p_uf.set_defaults(handler=_cmd_url_fetch)

    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        logger.error("internal error: missing handler")
        return 2
    return await handler(args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    _ensure_skill_gen_on_path()
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
