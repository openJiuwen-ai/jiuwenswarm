 #!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = ROOT / "packages" / "jiuwenclaw-tui"
SIDE_CAR_DIST = SIDECAR_ROOT / "dist"
CLI_ROOT = ROOT / "jiuwenclaw" / "cli"

TUI_TARGETS = {
    #"linux-x64": "linux_x86_64",
    #"linux-arm64": "linux_aarch64",
    #"macos-x64": "macosx_10_15_x86_64",
    "macos-arm64": "macosx_11_0_arm64",
    "windows-x64": "win_amd64",
    #"windows-arm64": "win_arm64",
}


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"[build] ({cwd.relative_to(ROOT) if cwd != ROOT else '.'}) {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True, env=env)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        return
    if path.exists():
        path.unlink()


def clean_root() -> None:
    for relative in ("build", "jiuwenclaw.egg-info"):
        remove_path(ROOT / relative)


def clean_sidecar() -> None:
    for relative in ("dist", "build", "jiuwenclaw_tui.egg-info"):
        remove_path(SIDECAR_ROOT / relative)


def build_root_wheel() -> None:
    SIDE_CAR_DIST.mkdir(parents=True, exist_ok=True)
    run(["uv", "build", "--wheel", "--out-dir", str(SIDE_CAR_DIST)], ROOT)


def build_sidecar_wheel(platform_tag: str | None = None) -> None:
    for relative in ("build", "jiuwenclaw_tui.egg-info"):
        remove_path(SIDECAR_ROOT / relative)
    env = os.environ.copy()
    if platform_tag:
        env["JWC_TUI_WHEEL_PLATFORM"] = platform_tag
    run(["uv", "build", "--wheel"], SIDECAR_ROOT, env=env)


def build_tui_binary(target: str, clean: bool) -> None:
    cmd = [sys.executable, "scripts/build_tui.py", "--target", target]
    if clean:
        cmd.append("--clean")
    run(cmd, ROOT)


def ensure_js_dependencies(install: bool) -> None:
    node_modules = CLI_ROOT / "node_modules"
    if node_modules.exists():
        return

    if not install:
        raise SystemExit(
            "\n".join(
                [
                    "missing JavaScript dependencies for jiuwenclaw/cli",
                    f"expected: {node_modules}",
                    "run one of:",
                    "  cd jiuwenclaw/cli && npm install",
                    "  python scripts/build_python_packages.py --install-js-deps",
                ]
            )
        )

    if (CLI_ROOT / "package-lock.json").exists():
        run(["npm", "install"], CLI_ROOT)
        return

    if (CLI_ROOT / "bun.lock").exists() or (CLI_ROOT / "bun.lockb").exists():
        run(["bun", "install"], CLI_ROOT)
        return

    run(["npm", "install"], CLI_ROOT)


def resolve_requested_targets(raw: str) -> list[str]:
    if raw == "all":
        return list(TUI_TARGETS.keys())
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if raw == "current":
        return ["current"]
    unknown = [value for value in values if value not in TUI_TARGETS]
    if unknown:
        valid = ", ".join(["current", "all", *TUI_TARGETS.keys()])
        raise SystemExit(f"unknown target(s): {', '.join(unknown)}; valid: {valid}")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build JiuwenClaw Python distributions (main package and optional TUI sidecar).",
    )
    parser.add_argument(
        "--target",
        default="current",
        help="TUI binary target passed to build_tui.py (default: current)",
    )
    parser.add_argument(
        "--out-dir",
        default="./packages/jiuwenclaw-tui/dist",
        help="Directory to output the built TUI binary (default: ./packages/jiuwenclaw-tui/dist)",
    )
    parser.add_argument(
        "--skip-binary",
        action="store_true",
        help="Skip building the native TUI binary before building the sidecar wheel",
    )
    parser.add_argument(
        "--skip-root",
        action="store_true",
        help="Skip building the main jiuwenclaw wheel",
    )
    parser.add_argument(
        "--skip-sidecar",
        action="store_true",
        help="Skip building the jiuwenclaw-tui sidecar wheel",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean dist/build/egg-info before building",
    )
    parser.add_argument(
        "--install-js-deps",
        action="store_true",
        help="Install jiuwenclaw/cli JavaScript dependencies if node_modules is missing",
    )
    args = parser.parse_args()
    targets = resolve_requested_targets(args.target)

    if args.clean:
        if not args.skip_root:
            clean_root()
        if not args.skip_sidecar:
            clean_sidecar()

    if args.skip_sidecar and args.skip_root:
        raise SystemExit("nothing to build: both --skip-root and --skip-sidecar were set")

    if args.skip_binary and not args.skip_sidecar and len(targets) > 1:
        raise SystemExit("--skip-binary only supports a single sidecar wheel target")

    if not args.skip_sidecar and not args.skip_binary:
        ensure_js_dependencies(args.install_js_deps)

    if not args.skip_root:
        build_root_wheel()

    if not args.skip_sidecar:
        for index, target in enumerate(targets):
            if not args.skip_binary:
                build_tui_binary(target, clean=args.clean or index > 0)
            platform_tag = None if target == "current" else TUI_TARGETS[target]
            build_sidecar_wheel(platform_tag=platform_tag)

    print("\n[build] done")
    if not args.skip_root:
        print(f"[build] main wheel: {SIDE_CAR_DIST}")
    if not args.skip_sidecar:
        print(f"[build] tui wheel(s): {SIDE_CAR_DIST}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc