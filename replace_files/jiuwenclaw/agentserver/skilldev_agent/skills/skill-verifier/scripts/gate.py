#!/usr/bin/env python3
"""Best-effort verification gate.

Pipeline: validate → package → upload → safety_scan
Each stage runs whenever its prerequisites are available.
Stages that depend on a failed prerequisite are marked as ``skipped``.
The gate returns a structured JSON summary of all stage results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import yaml

from scripts.validate import find_skill_root, validate_skill
from scripts.package import package_skill
from scripts.upload_skill import record_gate_obs_upload, upload_file
from scripts.safety_scan import scan_url, _get_conclusion, _format_failure

logger = logging.getLogger(__name__)


def _read_skill_name(skill_root: Path) -> str | None:
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        return None
    content = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None
    fm = yaml.safe_load(match.group(1))
    if isinstance(fm, dict):
        return str(fm.get("name") or "").strip() or None
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Gate failed:")
        print("Usage: python3 -m scripts.gate <workspace>")
        return 2

    workspace = Path(argv[1]).resolve()
    skill_root = find_skill_root(workspace / "skill")
    if skill_root is None:
        print("Gate failed: cannot find skill root under <workspace>/skill/")
        return 1

    stages: dict[str, dict] = {}

    # Stage 1: validate
    valid, message = validate_skill(skill_root)
    stages["validate"] = {"passed": valid, "message": message}
    if valid:
        print("Stage [validate]: passed")
    else:
        print(f"Stage [validate]: FAILED\n{message}")

    # Stage 2: package (does not require validate to pass)
    output_dir = workspace / "output"
    packaged = package_skill(skill_root, output_dir)
    if packaged is not None:
        stages["package"] = {"passed": True, "file": str(packaged)}
        print(f"Stage [package]: {packaged}")
    else:
        stages["package"] = {"passed": False, "message": "packaging failed"}
        print("Stage [package]: FAILED")

    # Stage 3: upload (requires package artifact)
    url: str | None = None
    if packaged is not None:
        url = asyncio.run(upload_file(str(packaged)))
        if url is not None:
            record_gate_obs_upload(workspace, url, packaged)
            stages["upload"] = {"passed": True, "url": url}
            print(f"Stage [upload]: {url}")
        else:
            stages["upload"] = {"passed": False, "message": "upload failed"}
            print("Stage [upload]: FAILED")
    else:
        stages["upload"] = {"passed": False, "message": "skipped (no package)"}
        print("Stage [upload]: skipped (no package)")

    # Stage 4: safety_scan (requires upload URL)
    if url is not None:
        skill_name = _read_skill_name(skill_root) or skill_root.name
        raw_result = scan_url(skill_name=skill_name, url=url)
        conclusion = _get_conclusion(raw_result)
        if str(conclusion).upper() == "BENIGN":
            stages["safety_scan"] = {"passed": True, "message": "BENIGN"}
            print("Stage [safety_scan]: passed")
        else:
            failure_detail = _format_failure(raw_result)
            stages["safety_scan"] = {"passed": False, "message": failure_detail}
            print(f"Stage [safety_scan]: FAILED\n{failure_detail}")
    else:
        stages["safety_scan"] = {"passed": False, "message": "skipped (no URL)"}
        print("Stage [safety_scan]: skipped (no URL)")

    all_passed = all(s["passed"] for s in stages.values())
    summary = {"passed": all_passed, "stages": stages}
    print("---GATE_RESULT_JSON---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if all_passed:
        print(f"Gate passed. Packaged: {packaged} | URL: {url}")
    else:
        failed = [name for name, s in stages.items() if not s["passed"]]
        print(f"Gate completed with failures: {', '.join(failed)}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main(sys.argv))
