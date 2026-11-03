#!/usr/bin/env python3
"""Finalize generated scripts after verification without blocking SKILL.md creation.

Only paths listed with --keep survive. Every other generated file under the target
scripts/ directory is deleted. The command always emits the mode the final
SKILL.md must use: with_scripts or text_images_only.
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep only verified scripts; zero survivors selects text+images-only finalization."
    )
    parser.add_argument("slug")
    parser.add_argument(
        "--keep",
        nargs="*",
        required=True,
        help="Verified script paths relative to scripts/. Pass --keep with no values when none passed.",
    )
    default_skills_dir = Path(__file__).resolve().parent.parent.parent
    parser.add_argument("--skills-dir", default=str(default_skills_dir))
    args = parser.parse_args()

    script_dir = (Path(args.skills_dir) / args.slug / "scripts").resolve()
    keep_rel: set[Path] = set()
    for value in args.keep:
        try:
            keep_rel.add(_relative_script_path(value, script_dir))
        except ValueError as exc:
            logger.error("[finalize_scripts] ERROR: %s", exc)
            raise SystemExit(2) from exc

    deleted: list[str] = []
    kept: list[str] = []
    if script_dir.exists():
        for path in sorted(p for p in script_dir.rglob("*") if p.is_file()):
            rel = path.resolve().relative_to(script_dir)
            if rel in keep_rel:
                kept.append(rel.as_posix())
            else:
                path.unlink()
                deleted.append(rel.as_posix())

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

    missing = sorted(rel.as_posix() for rel in keep_rel if rel.as_posix() not in kept)
    for rel in missing:
        logger.warning("[finalize_scripts] WARNING: verified path not found and was not kept: %s", rel)

    mode = "with_scripts" if kept else "text_images_only"
    state = {
        "mode": mode,
        "kept_scripts": kept,
        "deleted_scripts": deleted,
        "missing_verified_paths": missing,
    }
    common.write_json(common.work_path(args.slug, "code_validation.json"), state)

    logger.info("[finalize_scripts] KEPT: %s", kept)
    logger.info("[finalize_scripts] DELETED: %s", deleted)
    logger.info("[finalize_scripts] SKILL_SCRIPT_MODE: %s", mode)
    logger.info("[finalize_scripts] SKILL_MD_ALLOWED: true")


if __name__ == "__main__":
    main()
