#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Measure the fixed per-call prompt of Code mode for one or more profiles.

For each profile this runs one tiny headless turn ("reply OK") through the
process CLI against a *copy* of an initialised data dir with
``react.context_engine_config.enable_context_debug: true``, then reads the first
``llm_request.jsonl`` record the context engine dumps and reports:

* system prompt size, and within it the "# Skills" catalogue section;
* tool-schema size (the JSON the model receives) and the tool names;
* a token estimate (tiktoken ``cl100k_base`` when installed, else chars / 3.5).

It costs one short model call per profile (about $0.01 with deepseek-flash).
The source data dir is never modified; ``$HOME/.jiuwenswarm`` is never touched.

Usage::

    python scripts/measure_code_prompt.py --data-dir /path/to/initialised/data \
        --profiles default lean [--json out.json]

The data dir must contain ``config/config.yaml`` and ``config/.env`` with a
working model configuration (API_BASE / API_KEY / MODEL_NAME ...).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_INPUT = "Reply with exactly the single word OK and nothing else. Do not call any tools."


def _estimator():
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken cl100k_base", lambda text: len(enc.encode(text))
    except Exception:  # noqa: BLE001 - tiktoken is optional; any import/setup failure falls back
        return "chars/3.5", lambda text: round(len(text) / 3.5)


def _set(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _skills_section(system: str) -> str:
    start = system.find("\n# Skills")
    if start < 0:
        return ""
    end = system.find("\n# ", start + 3)
    return system[start:end if end > 0 else len(system)]


def measure_profile(source_data_dir: Path, profile: str, workdir: Path) -> dict[str, Any]:
    data_dir = workdir / f"data-{profile}"
    project = workdir / f"project-{profile}"
    shutil.copytree(source_data_dir, data_dir)
    project.mkdir()
    (project / "README.md").write_text("measurement project\n", encoding="utf-8")

    cfg_path = data_dir / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    _set(cfg, "react.context_engine_config.enable_context_debug", True)
    _set(cfg, "modes.code.profile", profile)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    request = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": f"measure-{profile}",
        "input": REQUEST_INPUT,
        "mode": "agent.code.normal",
        "workspace": {
            "cwd": str(project),
            "project_dir": str(project),
            "trusted_dirs": [str(project)],
        },
        "timeout_seconds": 180,
    }
    req_path = workdir / f"request-{profile}.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")

    env = dict(os.environ)
    env["JIUWENSWARM_DATA_DIR"] = str(data_dir)
    # Measure this checkout, not whatever copy an editable install points at.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    proc = subprocess.run(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main", "--run-json", str(req_path)],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    dumps = sorted(data_dir.rglob("llm_request.jsonl"))
    if not dumps:
        raise RuntimeError(
            f"profile {profile}: no llm_request.jsonl (exit {proc.returncode}); "
            f"stderr tail: {proc.stderr[-800:]}"
        )
    record = json.loads(dumps[0].read_text(encoding="utf-8").splitlines()[0])
    system = "\n".join(
        str(m.get("content")) for m in record.get("messages", []) if m.get("role") == "system"
    )
    tools = record.get("tools") or []
    tools_json = json.dumps(tools, ensure_ascii=False)
    names = [
        (t.get("function") or t).get("name") if isinstance(t, dict) else str(t) for t in tools
    ]
    return {
        "profile": profile,
        "exit_code": proc.returncode,
        "system_chars": len(system),
        "skills_chars": len(_skills_section(system)),
        "tools_chars": len(tools_json),
        "tool_count": len(tools),
        "tool_names": names,
        "_system": system,
        "_tools_json": tools_json,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="initialised data dir (copied, not modified)")
    parser.add_argument("--profiles", nargs="+", default=["default", "lean"])
    parser.add_argument("--json", type=Path, help="write the results as JSON here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary work dir")
    args = parser.parse_args(argv)

    method, count = _estimator()
    workdir = Path(tempfile.mkdtemp(prefix="jw-measure-"))
    results = []
    try:
        for profile in args.profiles:
            row = measure_profile(args.data_dir, profile, workdir)
            row["system_tokens"] = count(row.pop("_system"))
            row["tools_tokens"] = count(row.pop("_tools_json"))
            row["skills_tokens"] = round(row["skills_chars"] / 3.5)
            row["fixed_tokens"] = row["system_tokens"] + row["tools_tokens"]
            results.append(row)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    print(f"token estimate: {method}")
    print(f"{'profile':<9}{'system':>14}{'  of which skills':>18}{'tool schemas':>16}{'tools':>7}{'fixed total':>14}")
    for r in results:
        print(
            f"{r['profile']:<9}{r['system_chars']:>8} ch {r['system_tokens']:>5}t"
            f"{r['skills_chars']:>10} ch {r['skills_tokens']:>4}t"
            f"{r['tools_chars']:>9} ch {r['tools_tokens']:>5}t{r['tool_count']:>7}{r['fixed_tokens']:>12}t"
        )
    for r in results:
        print(f"{r['profile']} tools: {', '.join(r['tool_names'])}")
    if args.json:
        args.json.write_text(json.dumps({"estimator": method, "results": results}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
