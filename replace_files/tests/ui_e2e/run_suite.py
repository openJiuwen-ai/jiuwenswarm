from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from .runtime_openjiuwen import DEFAULT_RUNTIME_ENV_VAR, resolve_runtime_python
except ImportError:
    from runtime_openjiuwen import DEFAULT_RUNTIME_ENV_VAR, resolve_runtime_python


UI_E2E_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "jiuwenclaw" / "web"

CASE_SCRIPTS = {
    "todo": UI_E2E_ROOT / "todo_ui_report.py",
    "cron": UI_E2E_ROOT / "cron_ui_report.py",
}

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run JiuwenClaw Web UI E2E reports and collect their output directories.",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=tuple(CASE_SCRIPTS.keys()),
        default=["todo", "cron"],
        help="Cases to run. Default runs all Web UI E2E cases.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build the frontend once before running the selected cases.",
    )
    parser.add_argument(
        "--runtime-python",
        default=resolve_runtime_python(REPO_ROOT),
        help=(
            "Python used to launch the case scripts. "
            f"Default resolution order: ${DEFAULT_RUNTIME_ENV_VAR}, .venv, current interpreter."
        ),
    )
    parser.add_argument(
        "--home",
        default=str(Path.home()),
        help="HOME passed through to the case scripts.",
    )
    parser.add_argument(
        "--report-root",
        default="",
        help="Optional root directory used to store per-case report directories.",
    )
    parser.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="Stop after the first failing case.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.build:
        subprocess.run(["npm", "run", "build"], cwd=str(WEB_DIR), check=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_root = (
        Path(args.report_root).expanduser().resolve()
        if args.report_root
        else UI_E2E_ROOT / "artifacts" / "suite" / timestamp
    )
    report_root.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    summaries: list[tuple[str, int, Path]] = []
    for case in args.cases:
        script = CASE_SCRIPTS[case]
        report_dir = report_root / case
        cmd = [
            args.runtime_python,
            str(script),
            "--runtime-python",
            args.runtime_python,
            "--home",
            args.home,
            "--report-dir",
            str(report_dir),
        ]
        print(f"[ui_e2e] running {case}: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        summaries.append((case, int(result.returncode), report_dir))
        if result.returncode != 0:
            exit_code = result.returncode
            if args.stop_on_fail:
                break

    print()
    print("[ui_e2e] summary")
    for case, code, report_dir in summaries:
        status = "PASS" if code == 0 else f"FAIL({code})"
        print(f"- {case}: {status} -> {report_dir}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
