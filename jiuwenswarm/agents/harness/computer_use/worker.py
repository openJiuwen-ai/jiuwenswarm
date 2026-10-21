"""Host entry point for the engine's prepared runtime and stdio MCP."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from jiuwenswarm.common.utils import get_logs_dir, get_user_workspace_dir
from jiuwenswarm.cua_engine.hosting import prepare_native as prepare_engine

from . import engine_root


def prepare_native(*, build: bool = False) -> Path:
    cache = None if getattr(sys, "frozen", False) else Path(get_user_workspace_dir()) / "computer_use/runtime"
    build_log = get_logs_dir() / "computer_use_build.log" if cache is not None else None
    return prepare_engine(engine_root(), cache=cache, build_log=build_log, build=build)


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-native", action="store_true", help="Resolve an installed driver without compiling")
    action.add_argument("--build-native", action="store_true", help="Explicitly compile the optional source driver")
    args = parser.parse_args()
    if not (args.prepare_native or args.build_native) and sys.version_info < (3, 11):
        raise RuntimeError("cua_repl requires Python 3.11+.")
    try:
        executable = prepare_native(build=True) if args.build_native else prepare_native()
    except Exception as error:
        logging.getLogger(__name__).error("Computer Use unavailable: %s", error)
        # Console entry point; never mix diagnostics into the MCP response stream.
        # pylint: disable-next=avoid-using-exit
        raise SystemExit(1) from None
    if args.prepare_native or args.build_native:
        # The native adapter parses this single JSON response at subprocess EOF.
        json.dump({"executable": str(executable)}, sys.stdout)
        return
    from computer_use.mcp.server import cli

    cli([])


if __name__ == "__main__":
    main()
