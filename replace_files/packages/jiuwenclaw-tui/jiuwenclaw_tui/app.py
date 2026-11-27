from __future__ import annotations
    
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path


def _normalize_machine(machine: str) -> str:
    value = machine.strip().lower()
    if value in {"x86_64", "amd64"}:
        return "x64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    return value


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = _normalize_machine(platform.machine())
    if system == "linux":
        return f"linux-{machine}"
    if system == "darwin":
        return f"macos-{machine}"
    if system == "windows":
        return f"windows-{machine}"
    return f"{system}-{machine}"


def _binary_name() -> str:
    return "jiuwenclaw-tui.exe" if os.name == "nt" else "jiuwenclaw-tui"


def _resource_binary_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "resources"
        / "tui-bin"
        / _platform_tag()
        / _binary_name()
    )


def _ensure_executable(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_binary(binary: Path, argv: list[str]) -> int:
    _ensure_executable(binary)
    completed = subprocess.run([str(binary), *argv], check=False)
    return int(completed.returncode)


def main() -> None:
    binary = _resource_binary_path()
    if not binary.exists():
        raise SystemExit(
            "\n".join(
                [
                    f"jiuwenclaw-tui binary not found for platform: {_platform_tag()}",
                    f"expected path: {binary}",
                    "build it with: python scripts/build_tui.py --target current",
                ]
            )
        )
    raise SystemExit(_run_binary(binary, sys.argv[1:]))


if __name__ == "__main__":
    main()