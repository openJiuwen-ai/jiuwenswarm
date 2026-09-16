#!/usr/bin/env python3
"""Finalize generated scripts after verification without blocking SKILL.md creation.

Only paths listed with --keep survive. Every other generated file under this
run's package/scripts/ directory is deleted. The command always emits the mode
the final SKILL.md must use: with_scripts or text_images_only.
"""
import argparse
import logging
import sys
from pathlib import Path

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


def _relative_script_path(value: str, script_dir: Path) -> Path:
    """Normalize an input path to a path relative to package/scripts/.

    The public contract is package-relative ``scripts/...``. Absolute paths and
    legacy bare paths are still accepted as input for robustness, but every
    emitted path is canonicalized by ``_canonical_script_path``.
    """
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        parts = candidate.parts
        if parts and parts[0].lower() == "scripts":
            candidate = Path(*parts[1:])
        resolved = (script_dir / candidate).resolve()
    try:
        return resolved.relative_to(script_dir)
    except ValueError as exc:
        raise ValueError(f"script path escapes target scripts directory: {value}") from exc


def _canonical_script_path(relative_path: Path) -> str:
    """Return the one public path form used by agents and SKILL.md."""
    return (Path("scripts") / relative_path).as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep only verified scripts; zero survivors selects text+images-only finalization."
    )
    parser.add_argument("run_id", help="UUID run_id")
    parser.add_argument(
        "--keep",
        nargs="*",
        required=True,
        help=(
            "Verified package-relative script paths, canonical form scripts/.... "
            "Pass --keep with no values when none passed."
        ),
    )
    default_skills_dir = Path(__file__).resolve().parent.parent.parent
    parser.add_argument("--skills-dir", default=str(default_skills_dir))
    args = parser.parse_args()

    skills_root = Path(args.skills_dir).expanduser().resolve()
    run_root = skills_root / ".skill-omni-creation" / args.run_id
    script_dir = (run_root / "package" / "scripts").resolve()
    keep_rel: set[Path] = set()
    for value in args.keep:
        try:
            keep_rel.add(_relative_script_path(value, script_dir))
        except ValueError as exc:
            logger.error("[finalize_scripts] ERROR: %s", exc)
            raise RuntimeError("Invalid script path.") from exc

    deleted: list[str] = []
    kept: list[str] = []
    if script_dir.exists():
        for path in sorted(p for p in script_dir.rglob("*") if p.is_file()):
            rel = path.resolve().relative_to(script_dir)
            if rel in keep_rel:
                kept.append(_canonical_script_path(rel))
            else:
                path.unlink()
                deleted.append(_canonical_script_path(rel))

        for directory in sorted(
            (p for p in script_dir.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            script_dir.rmdir()
        except OSError:
            pass

    missing = sorted(
        _canonical_script_path(rel)
        for rel in keep_rel
        if _canonical_script_path(rel) not in kept
    )
    for rel in missing:
        logger.warning("[finalize_scripts] WARNING: verified path not found and was not kept: %s", rel)

    mode = "with_scripts" if kept else "text_images_only"
    state = {
        "mode": mode,
        "kept_scripts": kept,
        "deleted_scripts": deleted,
        "missing_verified_paths": missing,
    }
    common.write_json(run_root / "runtime" / "code_validation.json", state)

    logger.info("[finalize_scripts] KEPT: %s", kept)
    logger.info("[finalize_scripts] DELETED: %s", deleted)
    logger.info("[finalize_scripts] SKILL_SCRIPT_MODE: %s", mode)
    logger.info("[finalize_scripts] SKILL_MD_ALLOWED: true")


if __name__ == "__main__":
    main()
