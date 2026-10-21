"""Host entry point for the engine's prepared runtime and stdio MCP."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from jiuwenswarm.cua_engine.hosting import prepare_native as prepare_engine

from . import engine_root


def prepare_native() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("The bundled Computer Use backend is available only in desktop installers.")
    return prepare_engine(engine_root())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-native", action="store_true", help="Resolve the installed desktop driver")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise RuntimeError("cua_repl requires Python 3.11+.")
    try:
        executable = prepare_native()
    except Exception as error:
        logging.getLogger(__name__).error("Computer Use unavailable: %s", error)
        # Console entry point; never mix diagnostics into the MCP response stream.
        # pylint: disable-next=avoid-using-exit
        raise SystemExit(1) from None
    if args.prepare_native:
        # The native adapter parses this single JSON response at subprocess EOF.
        json.dump({"executable": str(executable)}, sys.stdout)
        return
    from computer_use.mcp.server import cli

    cli([])


if __name__ == "__main__":
    main()
