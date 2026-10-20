"""Host entry point for the engine's prepared runtime and stdio MCP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.cua_engine.hosting import prepare_native as prepare_engine

from . import engine_root


def prepare_native() -> Path:
    cache = None if getattr(sys, "frozen", False) else Path(get_user_workspace_dir()) / "computer_use/runtime"
    return prepare_engine(engine_root(), cache=cache)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-native", action="store_true")
    args = parser.parse_args()
    if not args.prepare_native and sys.version_info < (3, 12):
        raise RuntimeError("cua_repl requires Python 3.12+. Set react.subagents.cua_agent.repl_python.")
    executable = prepare_native()
    if args.prepare_native:
        # The native adapter parses this single JSON response at subprocess EOF.
        json.dump({"executable": str(executable)}, sys.stdout)
        return
    from computer_use.mcp.server import cli

    cli([])


if __name__ == "__main__":
    main()
