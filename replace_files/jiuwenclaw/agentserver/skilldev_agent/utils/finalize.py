# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Post-run finalization helpers: report loading, stale-package detection, and repackaging."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev_agent.utils.direct_import import find_skill_root

logger = logging.getLogger(__name__)

_PKG_EXCLUDE_DIRS: frozenset[str] = frozenset({"__pycache__", "node_modules", ".trash"})
_PKG_EXCLUDE_GLOBS: frozenset[str] = frozenset({"*.pyc", "*.swp", "*.bak-*"})
_PKG_EXCLUDE_FILES: frozenset[str] = frozenset({".DS_Store"})
_PKG_ROOT_EXCLUDE_DIRS: frozenset[str] = frozenset({"evals", "output"})
_EXECUTABLE_CODE_FENCE_LANGS = frozenset({
    "bash",
    "cjs",
    "cmd",
    "javascript",
    "js",
    "node",
    "perl",
    "powershell",
    "ps1",
    "pwsh",
    "py",
    "python",
    "python3",
    "ruby",
    "shell",
    "sh",
    "ts",
    "typescript",
    "zsh",
})
_EXECUTABLE_FILE_SUFFIXES = frozenset({
    ".appimage",
    ".bat",
    ".bin",
    ".cmd",
    ".com",
    ".command",
    ".cjs",
    ".exe",
    ".jar",
    ".js",
    ".mjs",
    ".msi",
    ".pl",
    ".ps1",
    ".py",
    ".pyw",
    ".rb",
    ".run",
    ".sh",
    ".ts",
})
_CODE_FENCE_PATTERN = re.compile(
    r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def _should_exclude_from_package(rel_path: Path) -> bool:
    parts = rel_path.parts
    if any(part in _PKG_EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) > 1 and parts[1] in _PKG_ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in _PKG_EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in _PKG_EXCLUDE_GLOBS)


def collect_output_packages(output_dir: Path) -> list[Path]:
    """Return .skill/.zip files under *output_dir*, or an empty list if it does not exist."""
    if not output_dir.exists():
        return []
    return [f for f in output_dir.iterdir() if f.is_file() and f.suffix in (".skill", ".zip")]


def _normalize_code_fence_lang(lang: str) -> str:
    stripped = lang.strip().lower()
    if not stripped:
        return ""
    return re.split(r"[\s,\{\[]", stripped, maxsplit=1)[0]


def _looks_like_executable_code_block(lang: str, body: str) -> bool:
    normalized_lang = _normalize_code_fence_lang(lang)
    if normalized_lang in _EXECUTABLE_CODE_FENCE_LANGS:
        return True
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return first_line.startswith("#!")


def _skill_md_content_has_executable_code_block(content: str) -> bool:
    return any(
        _looks_like_executable_code_block(match.group("lang") or "", match.group("body") or "")
        for match in _CODE_FENCE_PATTERN.finditer(content)
    )


def _archive_member_is_executable(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False

    member_path = Path(info.filename)
    if member_path.suffix.lower() in _EXECUTABLE_FILE_SUFFIXES:
        return True

    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISREG(mode) and mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return True

    try:
        with archive.open(info, "r") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _package_has_executable_file(package_path: Path) -> bool:
    if not package_path.is_file():
        return False
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            for info in archive.infolist():
                if _archive_member_is_executable(archive, info):
                    return True
                if Path(info.filename).name != "SKILL.md":
                    continue
                try:
                    with archive.open(info, "r") as fh:
                        content = fh.read().decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "[SkillDevDeepAdapter] Failed to read SKILL.md from %s: %s",
                        package_path,
                        exc,
                    )
                    return True
                if _skill_md_content_has_executable_code_block(content):
                    return True
            return False
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning(
            "[SkillDevDeepAdapter] Failed to inspect packaged skill %s: %s",
            package_path,
            exc,
        )
        return True


def packaged_skill_requires_sandbox(packaged_files: list[Path]) -> bool:
    return any(_package_has_executable_file(package_path) for package_path in packaged_files)


def repackage_if_stale(task_workspace: Path, existing_packages: list[Path]) -> list[Path]:
    """Re-run packaging when skill source is newer than the existing archive.

    Returns the updated package list (new package on repack, original list otherwise).
    """
    skill_dir = task_workspace / "skill"
    if not skill_dir.is_dir():
        return existing_packages

    skill_root = find_skill_root(skill_dir)
    if skill_root is None:
        return existing_packages

    pkg_mtime = min(f.stat().st_mtime for f in existing_packages)
    src_mtime = max(
        (f.stat().st_mtime for f in skill_root.rglob("*") if f.is_file()),
        default=0.0,
    )
    if src_mtime <= pkg_mtime:
        return existing_packages

    logger.info("[SkillDevDeepAdapter] Skill source is newer than existing package, repackaging …")
    for stale in existing_packages:
        stale.unlink(missing_ok=True)
        logger.info("[SkillDevDeepAdapter] Removed stale package: %s", stale.name)

    output_dir = task_workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    skill_name = skill_root.name
    zip_path = output_dir / f"{skill_name}.zip"

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in skill_root.rglob("*"):
                if not file_path.is_file():
                    continue
                arcname = Path(skill_name) / file_path.relative_to(skill_root)
                if _should_exclude_from_package(arcname):
                    continue
                zf.write(file_path, arcname)
        logger.info("[SkillDevDeepAdapter] Repackaged skill to %s", zip_path)
        return [zip_path]
    except Exception:
        logger.exception("[SkillDevDeepAdapter] Repackaging failed, falling back to stale packages")
        return existing_packages


def get_static_review_report(
    task_workspace: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load static_report.json / static_report.md from evals/static.

    Returns (static_result_dict, report_markdown).
    Marks the report as reviewed on success so it is not surfaced again.
    """
    static_dir = task_workspace / "evals" / "static"
    report_json = static_dir / "static_report.json"
    if not report_json.is_file():
        return None, None

    try:
        static_result = json.loads(report_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[SkillDevDeepAdapter] static report load failed: %s", exc)
        return None, None

    if not isinstance(static_result, dict) or static_result.get("reviewed", False):
        return None, None

    updated = {**static_result, "reviewed": True}
    try:
        report_json.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("[SkillDevDeepAdapter] static report mark reviewed failed: %s", exc)
        return None, None

    report_md = static_dir / "static_report.md"
    report = report_md.read_text(encoding="utf-8") if report_md.is_file() else ""
    return static_result, report


def get_review_benchmark(
    task_workspace: Path,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Load the latest benchmark.json / benchmark.md from the evals dir.

    Returns (benchmark_dict, report_markdown, iteration_number).
    Marks the benchmark as reviewed on success so it is not surfaced again.
    """
    evals_dir = task_workspace / "evals"

    iter_dirs = [
        d for d in evals_dir.iterdir()
        if d.is_dir() and d.name.startswith("iteration-")
    ] if evals_dir.is_dir() else []

    if iter_dirs:
        def _iter_num(d: Path) -> int:
            try:
                return int(d.name.split("-", 1)[1])
            except (IndexError, ValueError):
                return -1

        latest = max(iter_dirs, key=_iter_num)
        iteration = _iter_num(latest)
        target_dir = latest
    else:
        iteration = 0
        target_dir = evals_dir

    benchmark: dict[str, Any] | None = None
    report: str | None = None

    bm_json = target_dir / "benchmark.json"
    if bm_json.is_file():
        benchmark = json.loads(bm_json.read_text(encoding="utf-8"))
        try:
            run_summary = benchmark["run_summary"]
            with_pass_rate = run_summary["with_skill"]["pass_rate"]
            without_pass_rate = run_summary["without_skill"]["pass_rate"]
            delta = run_summary["delta"]

            with_mean = with_pass_rate["mean"]
            without_mean = without_pass_rate["mean"]
            delta_pass_rate = delta["pass_rate"]
            if with_mean > 1 or without_mean > 1:
                with_pass_rate["mean"] = with_mean / 100
                without_pass_rate["mean"] = without_mean / 100
                delta["pass_rate"] = f"{float(delta_pass_rate) / 100:+g}"
            has_required_summary = True
        except (KeyError, TypeError, ValueError):
            has_required_summary = False
        if not has_required_summary or benchmark.get("reviewed"):
            return None, None, -1
        updated = {**benchmark, "reviewed": True}
        bm_json.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    bm_md = target_dir / "benchmark.md"
    if bm_md.is_file():
        report = bm_md.read_text(encoding="utf-8")

    return benchmark, report, iteration
