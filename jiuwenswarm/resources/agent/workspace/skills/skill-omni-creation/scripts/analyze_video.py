#!/usr/bin/env python3
"""
analyze_video.py — 下载视频并进行单阶段粗扫抽帧，供 agent 按顺序分析

用法：
  python analyze_video.py <video_url_or_slug> [--title "视频标题"]

  传视频 URL  → 自动复用 stage01 的 slug，下载保存到 work/<slug>/downloads/，帧保存到 work/<slug>/frames/
  传 slug    → 直接读取 work/<slug>/video.mp4 或 work/<slug>/downloads/video.*（跳过下载）

抽帧规则：
  - 短视频按 0.5fps（每 2 秒 1 帧）抽取
  - 长视频自动降低抽帧频率，均匀覆盖全片且最多保留 90 帧
  - 仅进行一次粗扫，不执行细扫

输出：
  原始 PNG 帧保存到 work/<slug>/frames/，供最终 Skill 使用；
  低分辨率 JPEG 审核帧保存到 work/<slug>/review_frames/，只供 agent 按固定 5 帧批次顺序查看。
"""
import argparse
import json
import logging
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

BASE_FPS = 0.5
MAX_FRAMES = 90
BATCH_SIZE = 5
REVIEW_BATCH_STATE = "review_batch_state.json"

# 仅供模型理解的审核帧预算。原始 PNG 帧保持不变，最终仍从 frames/ 保存。
REVIEW_PRIMARY_SIZE = (480, 270)
REVIEW_FALLBACK_SIZE = (384, 216)
REVIEW_JPEG_QUALITY = 60
REVIEW_MIN_QUALITY = 50
REVIEW_MAX_BYTES = 45 * 1024
REVIEW_BATCH_MAX_BYTES = 225 * 1024


def probe_duration(video_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=common.OPERATION_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe not found; install ffmpeg and ensure ffprobe is in PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-300:]}")

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned invalid duration: {result.stdout!r}") from exc

    if duration <= 0:
        raise RuntimeError(f"invalid video duration: {duration}")
    return duration


def choose_fps(duration_seconds: float) -> float:
    """Use 0.5fps for short videos; lower it for a 90-frame maximum."""
    return min(BASE_FPS, MAX_FRAMES / duration_seconds)


def _uniformly_cap_frames(frames: list[Path], max_frames: int) -> list[Path]:
    """Uniformly retain at most max_frames and renumber them sequentially."""
    if len(frames) <= max_frames:
        return frames

    selected_indices = {
        round(i * (len(frames) - 1) / (max_frames - 1))
        for i in range(max_frames)
    }
    selected = [frame for index, frame in enumerate(frames) if index in selected_indices]

    for frame in frames:
        if frame not in selected:
            frame.unlink()

    temporary_paths: list[Path] = []
    for index, frame in enumerate(selected, start=1):
        temporary = frame.with_name(f".selected_{index:04d}.png")
        frame.replace(temporary)
        temporary_paths.append(temporary)

    capped: list[Path] = []
    for index, temporary in enumerate(temporary_paths, start=1):
        final_path = temporary.with_name(f"frame_{index:04d}.png")
        temporary.replace(final_path)
        capped.append(final_path)
    return capped


def extract_frames(video_path: Path, frames_dir: Path, fps: float) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)

    # 防止重复执行时旧帧残留，破坏 90 帧上限和批次顺序。
    for old_frame in frames_dir.glob("frame_*.png"):
        old_frame.unlink()

    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps={fps:.12g}",
        str(frames_dir / "frame_%04d.png"),
        "-loglevel", "error",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=common.OPERATION_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found; install ffmpeg and ensure it is in PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-300:]}")

    frames = sorted(frames_dir.glob("frame_*.png"))
    return _uniformly_cap_frames(frames, MAX_FRAMES)


def _encode_review_jpeg(source: Path) -> bytes:
    """Create a compact JPEG review proxy while preserving the original PNG."""
    with Image.open(source) as image:
        image = image.convert("RGB")

        attempts = [
            (REVIEW_PRIMARY_SIZE, REVIEW_JPEG_QUALITY),
            (REVIEW_PRIMARY_SIZE, REVIEW_MIN_QUALITY),
            (REVIEW_FALLBACK_SIZE, REVIEW_JPEG_QUALITY),
            (REVIEW_FALLBACK_SIZE, REVIEW_MIN_QUALITY),
        ]
        best: bytes | None = None
        for size, quality in attempts:
            resized = image.resize(size, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            resized.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=2,
            )
            encoded = buffer.getvalue()
            if best is None or len(encoded) < len(best):
                best = encoded
            if len(encoded) <= REVIEW_MAX_BYTES:
                return encoded

    if best is None:
        raise RuntimeError(f"failed to encode review frame: {source}")
    if len(best) > REVIEW_MAX_BYTES:
        raise RuntimeError(
            f"review frame exceeds {REVIEW_MAX_BYTES} bytes after 384x216 quality-50 encoding: "
            f"{source} ({len(best)} bytes)"
        )
    return best


def build_review_frames(frames: list[Path], review_dir: Path) -> list[Path]:
    """Generate low-resolution JPEG proxies and enforce per-frame/batch byte budgets."""
    review_dir.mkdir(parents=True, exist_ok=True)
    for old_frame in review_dir.glob("frame_*.jpg"):
        old_frame.unlink()

    review_frames: list[Path] = []
    for source in frames:
        target = review_dir / f"{source.stem}.jpg"
        target.write_bytes(_encode_review_jpeg(source))
        review_frames.append(target)

    for batch_start in range(0, len(review_frames), BATCH_SIZE):
        batch = review_frames[batch_start:batch_start + BATCH_SIZE]
        batch_bytes = sum(path.stat().st_size for path in batch)
        if batch_bytes > REVIEW_BATCH_MAX_BYTES:
            raise RuntimeError(
                f"review batch exceeds {REVIEW_BATCH_MAX_BYTES} bytes: "
                f"batch {batch_start // BATCH_SIZE + 1} ({batch_bytes} bytes)"
            )
    return review_frames


def _review_state_path(slug: str) -> Path:
    return common.work_path(slug, REVIEW_BATCH_STATE)


def _write_review_state(slug: str, current_batch: int, total_frames: int) -> None:
    state = {
        "version": 1,
        "current_batch": current_batch,
        "batch_size": BATCH_SIZE,
        "total_frames": total_frames,
    }
    path = _review_state_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_review_state(slug: str) -> dict:
    path = _review_state_path(slug)
    if not path.is_file():
        raise RuntimeError(
            f"review batch state not found for slug {slug!r}; run analyze_video.py normally first"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid review batch state: {path}") from exc

    if state.get("batch_size") != BATCH_SIZE:
        raise RuntimeError(
            f"review batch state uses batch_size={state.get('batch_size')}; expected {BATCH_SIZE}; "
            "run analyze_video.py normally to rebuild review frames"
        )
    return state


def _print_review_batch(slug: str, batch_number: int, total_frames: int) -> None:
    review_dir = common.work_path(slug, "review_frames").resolve()
    review_frames = sorted(review_dir.glob("frame_*.jpg"))
    if len(review_frames) != total_frames:
        raise RuntimeError(
            f"review frame count changed for slug {slug!r}: state={total_frames}, files={len(review_frames)}"
        )

    total_batches = (total_frames + BATCH_SIZE - 1) // BATCH_SIZE
    if batch_number < 1 or batch_number > total_batches:
        raise RuntimeError(f"review batch out of range: {batch_number}/{total_batches}")

    start_index = (batch_number - 1) * BATCH_SIZE
    batch = review_frames[start_index:start_index + BATCH_SIZE]
    batch_bytes = sum(path.stat().st_size for path in batch)

    logger.info("\n%s", "=" * 60)
    logger.info(
        "当前审核批次 %d/%d（%d 帧，%.1f KB）",
        batch_number,
        total_batches,
        len(batch),
        batch_bytes / 1024,
    )
    logger.info("只读取下面列出的 %d 个文件；不要扫描目录或读取其他批次：", len(batch))
    for path in batch:
        logger.info("  %s", path)

    if batch_number < total_batches:
        logger.info(
            "完成本批理解后，运行：%s %s %s --next-review-batch",
            sys.executable,
            Path(__file__).resolve(),
            slug,
        )
    else:
        logger.info("这是最后一个审核批次。")
    logger.info("选择关键帧时，映射到相同编号的 frames/frame_NNNN.png。")
    logger.info("%s", "=" * 60)


def _show_or_advance_review_batch(slug: str, advance: bool) -> None:
    state = _load_review_state(slug)
    current_batch = int(state["current_batch"])
    total_frames = int(state["total_frames"])
    total_batches = (total_frames + BATCH_SIZE - 1) // BATCH_SIZE

    if advance:
        if current_batch >= total_batches:
            logger.info("[analyze_video] review batches already complete: %d/%d", current_batch, total_batches)
            _print_review_batch(slug, current_batch, total_frames)
            return
        current_batch += 1
        _write_review_state(slug, current_batch, total_frames)

    _print_review_batch(slug, current_batch, total_frames)


def main() -> None:
    parser = argparse.ArgumentParser(description="视频 → 最多 90 帧的单阶段粗扫抽帧")
    parser.add_argument("target", help="视频 URL 或已下载视频的 slug")
    parser.add_argument("--title", default=None, help="视频标题（可选，默认用 slug）")
    parser.add_argument("--slug", default=None, help="显式复用 stage01 的 slug；省略时按 URL 自动查找")
    batch_group = parser.add_mutually_exclusive_group()
    batch_group.add_argument(
        "--current-review-batch",
        action="store_true",
        help="只重新打印当前审核批次，不重新下载或抽帧",
    )
    batch_group.add_argument(
        "--next-review-batch",
        action="store_true",
        help="确认当前批次已完成并只暴露下一个审核批次",
    )
    args = parser.parse_args()

    is_url = args.target.startswith("http://") or args.target.startswith("https://")
    resolved_slug = args.slug or (common.resolve_work_slug_for_url(args.target) if is_url else args.target)

    if args.current_review_batch or args.next_review_batch:
        _show_or_advance_review_batch(resolved_slug, advance=args.next_review_batch)
        return

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)

        if is_url:
            slug = resolved_slug
            logger.info("[analyze_video] resolved slug: %s", slug)
            logger.info("[analyze_video] downloading: %s", args.target)
            download_dir = common.work_path(slug, "downloads")
            video_path = common.download_video(args.target, download_dir)
        else:
            slug = resolved_slug
            candidates = [
                common.work_path(slug, "video.mp4"),
                common.work_path(slug, "downloads/video.mp4"),
                *sorted(common.work_path(slug, "downloads").glob("video.*")),
            ]
            video_path = next(
                (candidate for candidate in candidates if candidate.is_file() and not candidate.name.endswith(".part")),
                candidates[0],
            )
            if not video_path.exists():
                logger.error("[analyze_video] ERROR: no completed video found for slug %s", slug)
                sys.exit(1)

        title = args.title or slug.replace("_", " ")
        logger.info("[analyze_video] title: %r", title)

        duration = probe_duration(video_path)
        fps = choose_fps(duration)
        interval = 1.0 / fps

        frames_dir = common.work_path(slug, "frames")
        abs_frames_dir = frames_dir.resolve()
        logger.info(
            "[analyze_video] coarse scan at %.6gfps (about 1 frame every %.2fs) → %s",
            fps,
            interval,
            abs_frames_dir,
        )
        frames = extract_frames(video_path, frames_dir, fps)
        n = len(frames)
        if n == 0:
            logger.error("[analyze_video] ERROR: no frames extracted from %s", video_path)
            sys.exit(1)

        review_dir = common.work_path(slug, "review_frames")
        review_frames = build_review_frames(frames, review_dir)
        abs_review_dir = review_dir.resolve()
        n_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE

        logger.info("\n%s", "=" * 60)
        logger.info("视频粗扫帧提取完成")
        logger.info("  视频时长:  %.2f 秒", duration)
        logger.info("  抽帧频率:  %.6g fps（约每 %.2f 秒 1 帧）", fps, interval)
        logger.info("  总帧数:    %d 帧（上限 %d）", n, MAX_FRAMES)
        logger.info("  原始帧:    %s", abs_frames_dir)
        logger.info("  审核帧:    %s", abs_review_dir)
        logger.info("  审核格式:  JPEG，480x270/384x216，quality 50-60")
        logger.info("  审核预算:  单张最多 %d KB；每批最多 %d KB", REVIEW_MAX_BYTES // 1024, REVIEW_BATCH_MAX_BYTES // 1024)
        logger.info("  固定批次:  每批最多 %d 帧，共 %d 批", BATCH_SIZE, n_batches)
        logger.info("  批次门禁:  一次只暴露当前批次的精确文件路径")
        logger.info("%s", "=" * 60)

        _write_review_state(slug, current_batch=1, total_frames=n)
        _print_review_batch(slug, batch_number=1, total_frames=n)


if __name__ == "__main__":
    main()
