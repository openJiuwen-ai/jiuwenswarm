"""Run the Builder-owned dynamic-memory-cli construction in staging."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _run(command: list[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        result: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    return {
        "command": command,
        "returncode": completed.returncode,
        "result": result,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    workspace = Path.cwd().resolve()
    memory_root = (workspace / args.memory_root).resolve()
    batch = (workspace / args.batch).resolve()
    output = (workspace / args.output).resolve()
    for path in (memory_root, batch, output.parent):
        if workspace != path and workspace not in path.parents:
            raise SystemExit(f"path escapes Builder workspace: {path}")
    cli = (
        workspace
        / "skills"
        / "dynamic-memory-cli"
        / "scripts"
        / "dynamic_memory_cli.py"
    )
    prefix = [sys.executable, str(cli), "--root", str(memory_root)]
    commands = [
        [*prefix, "build-pending", "--file", str(batch)],
        [*prefix, "test", "--built-only"],
        [*prefix, "bench"],
    ]
    records: list[dict[str, Any]] = []
    for command in commands:
        record = _run(command, workspace)
        records.append(record)
        if record["returncode"] != 0:
            break
    manifest = {
        "schema_version": 1,
        "memory_root": str(memory_root.relative_to(workspace)),
        "batch": str(batch.relative_to(workspace)),
        "commands": records,
        "success": len(records) == 3
        and all(item["returncode"] == 0 for item in records),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    logger.info("%s", json.dumps(manifest, ensure_ascii=False))
    return 0 if manifest["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
