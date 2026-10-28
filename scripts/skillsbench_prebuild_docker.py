# coding: utf-8
"""Pre-pull base images and pre-build Docker images for SkillsBench tasks that
need Docker verification.

Image tags match the skill-train evaluator::

    skillsbench-<task_id>:skilltrain

Usage (from jiuwenswarm repo)::

    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_prebuild_docker.py
    .\\.venv\\Scripts\\python.exe scripts\\skillsbench_prebuild_docker.py --only lean4-proof,flink-query
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """INFO to stdout, WARNING+ to stderr, message-only format (matches the old prints).

    Handlers live on this script's logger only (propagate off) so library logs
    keep flowing through their own configuration, unduplicated.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    logger.addHandler(out)
    logger.addHandler(err)


_SYNTAX_DIRECTIVE = re.compile(r"^#\s*syntax\s*=\s*docker/dockerfile:[^\n]*\n", re.I)
_APP_RE = re.compile(r"""['\"]/app/|/app/workspace|/app/baseline""")
_TOOL_RES = [
    re.compile(r"\b(lake env lean|lake build|\blean\b|elan\b)\b", re.I),
    re.compile(r"\b(mvn\b|maven\b)\b", re.I),
    re.compile(r"\b(gradle\b|\./gradlew)\b", re.I),
    re.compile(r"\bsbt\b", re.I),
    re.compile(r"\b(npm (ci|install|run)|npx |yarn |pnpm )\b", re.I),
    re.compile(r"\b(erl\b|rebar3\b)\b", re.I),
    re.compile(r"playwright install", re.I),
    re.compile(r"\b(javac\b|java -jar)\b", re.I),
]


def _skillsbench_root() -> Path | None:
    """SkillsBench root (parent of ``tasks/``); ``SKILLSBENCH_ROOT`` only, no fallback."""
    raw = os.environ.get("SKILLSBENCH_ROOT", "").strip()
    return Path(raw) if raw else None


def _modalities(md: str) -> list[str]:
    if not md.startswith("---"):
        return []
    end = md.find("\n---", 3)
    block = md[3:end] if end != -1 else md[:3000]
    m = re.search(r"(?m)^\s*modality:\s*\n((?:\s*-\s*.+\n)+)", block)
    if not m:
        return []
    return [x.strip() for x in re.findall(r"-\s*([^\n#]+)", m.group(1))]


def _build_timeout(md: str, default: int = 1800) -> int:
    m = re.search(r"(?m)^\s*build_timeout_sec:\s*([0-9.]+)", md)
    if not m:
        return default
    try:
        return max(300, int(float(m.group(1))))
    except ValueError:
        return default


def _must_docker(task_dir: Path) -> bool:
    md = (task_dir / "task.md").read_text(encoding="utf-8", errors="replace")
    is_src = "source-code" in _modalities(md)
    blob = ""
    for f in (task_dir / "verifier").rglob("*"):
        if f.is_file() and f.suffix.lower() in {".py", ".sh"}:
            blob += f.read_text(encoding="utf-8", errors="replace")
    has_app = bool(_APP_RE.search(blob))
    tools = any(rx.search(blob) for rx in _TOOL_RES)
    return has_app or tools or is_src


def _drop_syntax(text: str) -> str | None:
    match = _SYNTAX_DIRECTIVE.match(text)
    if match is None:
        return None
    if "<<" in text or "--mount=" in text:
        return None
    start = match.end()
    return text[start:]


def _image_tag(task_id: str) -> str:
    return f"skillsbench-{task_id}:skilltrain".lower()


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill") or "taskkill"
        subprocess.run(
            [taskkill, "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _image_exists(docker: str, tag: str) -> bool:
    try:
        run = _run([docker, "image", "inspect", tag], timeout=60)
    except subprocess.TimeoutExpired:
        # Unverifiable existence -> assume missing and rebuild (idempotent, matches --force).
        logger.warning(f"[prebuild] image inspect timed out for {tag}; assuming missing")
        return False
    return run.returncode == 0


def _collect_from_images(tasks_dir: Path, task_ids: list[str]) -> list[str]:
    images: set[str] = set()
    for tid in task_ids:
        df = tasks_dir / tid / "environment" / "Dockerfile"
        if not df.is_file():
            continue
        text = df.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"(?mi)^FROM\s+(?:--\S+\s+)*(\S+)", text):
            img = m.group(1)
            if img.upper() == "SCRATCH" or img.startswith("$") or "${" in img:
                continue
            images.add(img)
    return sorted(images)


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skillsbench-root", type=Path, default=None)
    parser.add_argument("--only", type=str, default="", help="Comma-separated task ids")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when skillsbench-<id>:skilltrain already exists locally.",
    )
    parser.add_argument("--continue-on-fail", action="store_true", default=True)
    parser.add_argument("--stop-on-fail", action="store_true")
    parser.add_argument("--default-timeout", type=int, default=1800)
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="JSONL log path (default: jiuwenswarm/outputs/outputs/_docker_prebuild_log.jsonl)",
    )
    args = parser.parse_args()

    root = args.skillsbench_root or _skillsbench_root()
    if root is None:
        logger.error("[prebuild] SKILLSBENCH_ROOT not set; pass --skillsbench-root or set it to the parent of tasks/")
        return 2
    root = root.resolve()
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        logger.error(f"[prebuild] tasks dir missing: {tasks_dir}")
        return 2

    docker = shutil.which("docker")
    if not docker:
        logger.error("[prebuild] docker not found in PATH")
        return 2

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    task_ids: list[str] = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir() or not (child / "task.md").is_file():
            continue
        if only and child.name not in only:
            continue
        if only or _must_docker(child):
            if (child / "environment" / "Dockerfile").is_file():
                task_ids.append(child.name)
            else:
                logger.info(f"[prebuild] skip {child.name}: no Dockerfile")

    if not task_ids:
        logger.info("[prebuild] no tasks to build")
        return 0

    log_path = args.log
    if log_path is None:
        log_path = Path(__file__).resolve().parents[1] / "outputs" / "outputs" / "_docker_prebuild_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[prebuild] tasks={len(task_ids)} root={root}")
    logger.info(f"[prebuild] log={log_path}")

    if not args.skip_pull:
        bases = _collect_from_images(tasks_dir, task_ids)
        logger.info(f"[prebuild] pulling {len(bases)} base image(s)")
        for img in bases:
            logger.info(f"[prebuild] docker pull {img}")
            try:
                run = _run([docker, "pull", img], timeout=1200)
                ok = run.returncode == 0
            except subprocess.TimeoutExpired:
                ok = False
                run = None
            logger.info(f"[prebuild] pull {'ok' if ok else 'FAIL'}: {img}")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "op": "pull",
                            "image": img,
                            "ok": ok,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    failed = 0
    for index, tid in enumerate(task_ids, 1):
        task_dir = tasks_dir / tid
        md = (task_dir / "task.md").read_text(encoding="utf-8", errors="replace")
        timeout = _build_timeout(md, args.default_timeout)
        # Heavy stacks get extra budget.
        if any(k in tid for k in ("druid", "lean4", "flink", "spring", "erlang", "agentops")):
            timeout = max(timeout, 3600)
        tag = _image_tag(tid)
        if not args.force and _image_exists(docker, tag):
            logger.info(f"[prebuild] ({index}/{len(task_ids)}) skip existing {tag}")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "op": "build",
                            "task_id": tid,
                            "tag": tag,
                            "ok": True,
                            "skipped": True,
                            "elapsed_sec": 0,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            continue
        context = (task_dir / "environment").resolve()
        dockerfile = context / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        rewritten = _drop_syntax(text)
        logger.info(f"[prebuild] ({index}/{len(task_ids)}) build {tag} timeout={timeout}s")
        started = time.time()
        ok = False
        detail = ""
        try:
            with tempfile.TemporaryDirectory(prefix="skillsbench-prebuild-") as tmp:
                build_file = dockerfile
                if rewritten is not None:
                    build_file = Path(tmp) / "Dockerfile"
                    build_file.write_text(rewritten, encoding="utf-8")
                run = _run(
                    [docker, "build", "-f", str(build_file), "-t", tag, str(context)],
                    timeout=timeout,
                )
                ok = run.returncode == 0
                detail = ((run.stderr or "") + (run.stdout or ""))[-2000:]
        except subprocess.TimeoutExpired:
            detail = f"timeout after {timeout}s"
            ok = False
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
        elapsed = round(time.time() - started, 1)
        logger.info(f"[prebuild] {'ok' if ok else 'FAIL'} {tid} elapsed={elapsed}s")
        if not ok and detail:
            logger.info(detail[-800:])
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "op": "build",
                        "task_id": tid,
                        "tag": tag,
                        "ok": ok,
                        "elapsed_sec": elapsed,
                        "timeout_sec": timeout,
                        "detail_tail": detail[-1500:],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if not ok:
            failed += 1
            if args.stop_on_fail:
                logger.info("[prebuild] stop-on-fail")
                return 1

    logger.info(f"[prebuild] finished ok={len(task_ids) - failed} fail={failed} total={len(task_ids)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
