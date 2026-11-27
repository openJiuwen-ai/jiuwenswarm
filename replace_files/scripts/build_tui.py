 #!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI_ENTRY = ROOT / "jiuwenclaw" / "cli" / "src" / "index.ts"
OUTPUT_ROOT = ROOT / "packages" / "jiuwenclaw-tui" / "jiuwenclaw_tui" / "resources" / "tui-bin"

TARGETS = {
    "linux-x64": "bun-linux-x64",
    "linux-arm64": "bun-linux-arm64",
    "macos-x64": "bun-darwin-x64",
    "macos-arm64": "bun-darwin-arm64",
    "windows-x64": "bun-windows-x64",
    #"windows-arm64": "bun-windows-arm64",
}


def current_platform_key() -> str:
    import platform

    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    if system == "linux":
        return f"linux-{machine}"
    if system == "darwin":
        return f"macos-{machine}"
    if system == "windows":
        return f"windows-{machine}"
    raise SystemExit(f"unsupported platform for current target: {system}-{machine}")


def output_binary_name(platform_key: str) -> str:
    return "jiuwenclaw-tui.exe" if platform_key.startswith("windows-") else "jiuwenclaw-tui"


def resolve_requested_targets(raw: str) -> list[str]:
    if raw == "all":
        return list(TARGETS.keys())
    if raw == "current":
        return [current_platform_key()]
    values = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [value for value in values if value not in TARGETS]
    if unknown:
        valid = ", ".join(["current", "all", *TARGETS.keys()])
        raise SystemExit(f"unknown target(s): {', '.join(unknown)}; valid: {valid}")
    return values


def _fix_macos_signature(binary: Path) -> None:
    """Fix Bun's broken ad-hoc code signature on macOS.

    Bun --compile produces a Mach-O with an invalid LC_CODE_SIGNATURE segment,
    which causes macOS to kill the binary at launch and prevents codesign/ldid
    from working directly.  Stripping the bad signature first, then applying a
    fresh ad-hoc signature resolves this.
    """
    if os.name != "nt" and shutil.which("codesign") is not None:
        subprocess.run(["codesign", "--remove-signature", str(binary)], check=False)
        subprocess.run(["codesign", "-s", "-", str(binary)], check=True)


def build_target(platform_key: str) -> Path:
    bun_target = TARGETS[platform_key]
    output_dir = OUTPUT_ROOT / platform_key
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_binary_name(platform_key)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        "bun",
        "build",
        "--compile",
        "--minify",
        "--target",
        bun_target,
        "--outfile",
        str(output_path),
        str(CLI_ENTRY),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)

    if platform_key.startswith("macos-"):
        _fix_macos_signature(output_path)

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build jiuwenclaw-tui native binaries with Bun.")
    parser.add_argument(
        "--target",
        default="current",
        help="Build target: current, all, or comma-separated platform keys",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing packaged tui binaries before building",
    )
    args = parser.parse_args()

    if shutil.which("bun") is None:
        raise SystemExit("bun is required to build jiuwenclaw-tui binaries")
    if not CLI_ENTRY.exists():
        raise SystemExit(f"CLI entry not found: {CLI_ENTRY}")

    if args.clean and OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)

    targets = resolve_requested_targets(args.target)
    built: list[Path] = []
    for target in targets:
        built.append(build_target(target))

    for path in built:
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc