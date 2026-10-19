# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run the complete XiaoYi AgentGroup to WorkSwarm migration pipeline.

The pipeline intentionally composes the independently testable structure
converter and Skill adapter, then packages the validated result.  Callers do
not need to manually unzip, mutate, or re-archive intermediate packages.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from scripts import convert_xiaoyi_agent_groups as converter
from scripts.expert_team_migration import adapt_workswarm_skills as adapter


REPORT_SCHEMA_VERSION = "workswarm.agent-group-migration.v1"


class MigrationError(ValueError):
    """Raised when a delivery cannot be migrated without partial output."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _replace_directory(source: Path, destination: Path) -> None:
    """Rename one directory within a filesystem.

    Kept as a narrow seam so commit-failure rollback can be tested without
    mocking package conversion or filesystem staging.
    """
    source.replace(destination)


def _commit_output_directory(
    candidate: Path,
    destination: Path,
) -> None:
    """Install a complete candidate directory and restore the old one on failure."""
    if not destination.exists():
        try:
            _replace_directory(candidate, destination)
        except OSError as exc:
            raise MigrationError(
                f"failed to commit migrated AgentGroup directory: {exc}"
            ) from exc
        return

    backup = destination.parent / (
        f".{destination.name}.workswarm-backup-{uuid.uuid4().hex}"
    )
    try:
        _replace_directory(destination, backup)
        try:
            _replace_directory(candidate, destination)
        except OSError as commit_exc:
            try:
                _replace_directory(backup, destination)
            except OSError as rollback_exc:
                raise MigrationError(
                    "failed to commit migrated AgentGroup directory and could not "
                    f"restore the previous output; recovery copy remains at {backup}: "
                    f"commit={commit_exc}; rollback={rollback_exc}"
                ) from commit_exc
            raise MigrationError(
                "failed to commit migrated AgentGroup directory; previous output "
                f"was restored: {commit_exc}"
            ) from commit_exc
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(
            f"failed to prepare migrated AgentGroup directory commit: {exc}"
        ) from exc

    try:
        shutil.rmtree(backup)
    except OSError as exc:
        raise MigrationError(
            "migrated AgentGroup directory was committed, but the previous output "
            f"could not be removed and remains at {backup}: {exc}"
        ) from exc


def _stage_output_directory(
    *,
    destination: Path,
    candidate: Path,
    pending_outputs: Sequence[tuple[Path, Path]],
    report: dict[str, Any],
) -> None:
    """Build the complete next output directory without mutating destination."""
    try:
        if destination.exists():
            if not destination.is_dir():
                raise MigrationError(f"output path is not a directory: {destination}")
            shutil.copytree(destination, candidate, symlinks=True)
        else:
            candidate.mkdir()

        for staged_archive, final_archive in pending_outputs:
            shutil.copyfile(staged_archive, candidate / final_archive.name)
        _write_json(candidate / "migration-report.json", report)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(
            f"failed to stage migrated AgentGroup output directory: {exc}"
        ) from exc


def migrate_sources(
    inputs: Iterable[Path],
    *,
    output_dir: Path,
    version: str = converter.DEFAULT_VERSION,
    overwrite: bool = False,
    validate_runtime: bool = True,
) -> dict[str, Any]:
    """Convert, adapt, validate, and deterministically package all inputs."""
    source_paths = [Path(path).expanduser().resolve() for path in inputs]
    if not source_paths:
        raise MigrationError("at least one input path is required")
    destination = output_dir.expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(
            f"failed to create output parent directory: {exc}"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="workswarm_agent_group_migrate_") as raw:
        workspace = Path(raw)
        converted_dir = workspace / "converted"
        staged_dir = workspace / "staged"
        converted_dir.mkdir()
        staged_dir.mkdir()
        conversion = converter.convert_sources(
            source_paths,
            output_dir=converted_dir,
            version=version,
            overwrite=True,
            validate=validate_runtime,
        )

        cases: list[dict[str, Any]] = []
        pending_outputs: list[tuple[Path, Path]] = []
        for item in conversion["groups"]:
            package_id = str(item["id"])
            archive = converted_dir / f"{package_id}.zip"
            extract_root = staged_dir / package_id
            extract_root.mkdir()
            converter._safe_extract_zip(archive, extract_root)
            package_root = extract_root / package_id
            adaptation = adapter.adapt_package(
                package_root,
                validate_runtime=validate_runtime,
            )
            final_archive = destination / f"{package_id}.zip"
            if final_archive.exists() and not overwrite:
                raise MigrationError(f"output already exists: {final_archive}")
            staged_archive = workspace / "final" / f"{package_id}.zip"
            staged_archive.parent.mkdir(exist_ok=True)
            converter._write_deterministic_zip(package_root, staged_archive)
            pending_outputs.append((staged_archive, final_archive))
            cases.append(
                {
                    "id": package_id,
                    "source": item["source"],
                    "output": str(final_archive),
                    "memberCount": item["memberCount"],
                    "adaptation": adaptation,
                }
            )

        report = {
            "schemaVersion": REPORT_SCHEMA_VERSION,
            "version": version,
            "inputCount": len(source_paths),
            "groupCount": len(cases),
            "runtimeValidated": validate_runtime,
            "groups": cases,
        }

        # Build a complete candidate beside the destination so the final rename
        # stays on one filesystem.  The destination is swapped only after every
        # package and the report are ready; a failed swap restores the old tree.
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.workswarm-commit-",
            dir=destination.parent,
        ) as raw_commit:
            candidate = Path(raw_commit) / "candidate"
            _stage_output_directory(
                destination=destination,
                candidate=candidate,
                pending_outputs=pending_outputs,
                report=report,
            )
            _commit_output_directory(candidate, destination)

    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert XiaoYi AgentGroups, replace platform-specific Skills, "
            "validate with the WorkSwarm loader, and emit final ZIPs."
        )
    )
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", default=converter.DEFAULT_VERSION)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--skip-runtime-validation",
        action="store_true",
        help="Skip current-develop loader validation (diagnostics only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = migrate_sources(
            args.input,
            output_dir=args.output_dir,
            version=args.version,
            overwrite=args.overwrite,
            validate_runtime=not args.skip_runtime_validation,
        )
    except (MigrationError, converter.ConversionError, adapter.AdaptationError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "groupCount": report["groupCount"],
                "outputDir": str(args.output_dir.expanduser().resolve()),
                "report": str(
                    args.output_dir.expanduser().resolve() / "migration-report.json"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
