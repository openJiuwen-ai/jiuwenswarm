"""Delete trajectory data written under the previous trajectory contract.

The trajectory contract changed incompatibly (span schema version 2, session
store schema 4, archive version 2), and nothing reads the old data. This
removes what a workspace holds of it:

* the web trajectory session databases (``trajectory_ui.db_path``, by default
  ``<workspace>/.trace/sessions``), with their WAL sidecars;
* the file exporter's ``traces-*.jsonl`` under the default ``<workspace>/.trace``
  and under any ``agent_observability`` / ``team_observability`` ``traces_dir``.

Evolution trajectory archives and RSI role trajectories live outside the
workspace; pass them with ``--extra``. Langfuse data is never touched.

Usage::

    python scripts/reset_trajectory_data.py --dry-run
    python scripts/reset_trajectory_data.py --extra /path/to/trajectories_v1.jsonl
"""

from __future__ import annotations

import argparse
import glob
import shutil
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_OBSERVABILITY_SECTIONS = ("agent_observability", "team_observability")


def _load_config() -> Mapping[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
    except Exception as exc:  # noqa: BLE001 - defaults still cover the workspace
        print(f"warning: config not loaded, using default locations ({exc})", file=sys.stderr)
        return {}
    return config if isinstance(config, Mapping) else {}


def _default_workspace() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir()


def _session_root(config: Mapping[str, Any], workspace: Path) -> Path:
    from jiuwenswarm.observability.config import load_trajectory_store_settings

    return load_trajectory_store_settings(config, workspace=workspace).database_path


def _trace_dirs(config: Mapping[str, Any], workspace: Path) -> list[Path]:
    directories = [workspace / ".trace"]
    for section_name in _OBSERVABILITY_SECTIONS:
        section = config.get(section_name)
        configured = str(section.get("traces_dir") or "").strip() if isinstance(section, Mapping) else ""
        if configured:
            path = Path(configured).expanduser()
            directories.append(path if path.is_absolute() else workspace / path)
    return list(dict.fromkeys(directories))


def _targets(session_root: Path, trace_dirs: Iterable[Path], extras: Iterable[str]) -> list[Path]:
    targets: list[Path] = []
    if session_root.is_dir():
        # Delete the databases, not a directory that happens to be configured
        # as the root: db_path may point anywhere, including somewhere shared.
        targets.extend(sorted(session_root.glob("*/*.sqlite3*")))
    for directory in trace_dirs:
        targets.extend(sorted(directory.glob("traces-*.jsonl")))
    for pattern in extras:
        matches = glob.glob(str(Path(pattern).expanduser()))
        if not matches:
            print(f"warning: --extra matched nothing: {pattern}", file=sys.stderr)
        targets.extend(Path(match) for match in sorted(matches))
    return list(dict.fromkeys(targets))


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, help="workspace root (default: the JiuwenSwarm data dir)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted and stop")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="PATH",
        help="additional file, directory or glob to delete (repeatable)",
    )
    args = parser.parse_args(argv)

    workspace = (args.workspace or _default_workspace()).expanduser()
    config = _load_config()
    targets = _targets(_session_root(config, workspace), _trace_dirs(config, workspace), args.extra)
    if not targets:
        print("nothing to delete")
        return 0
    for target in targets:
        print(("would delete " if args.dry_run else "deleting ") + str(target))
        if not args.dry_run:
            _remove(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
