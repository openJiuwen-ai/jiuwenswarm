#!/usr/bin/env python3
"""Run the dependent web-image stages sequentially with the current interpreter."""
import argparse
import subprocess
import sys
from pathlib import Path

from environment_gate import ensure_environment


def main() -> None:
    parser = argparse.ArgumentParser(description="download_images -> print_blocks(stage02), strictly sequential")
    parser.add_argument("slug")
    args = parser.parse_args()

    # Re-exec this orchestrator under the selected project interpreter before
    # launching either child, so download_images.py and print_blocks.py cannot
    # accidentally run in different Python environments.
    ensure_environment("images")

    script_dir = Path(__file__).resolve().parent
    commands = [
        [sys.executable, str(script_dir / "download_images.py"), args.slug],
        [sys.executable, str(script_dir / "print_blocks.py"), args.slug, "--stage", "stage02"],
    ]
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
