#!/usr/bin/env python3
"""
analyze_video.py — 下载视频并以 1fps 抽取帧图片，供 agent 自行分析

用法：
  python analyze_video.py <video_url_or_slug> [--title "视频标题"]

  传视频 URL  → 自动下载，帧保存到 work/<slug>/frames/
  传 slug    → 直接读 work/<slug>/video.mp4（跳过下载）

输出：
  打印帧总数、帧目录路径、建议批次大小（供 agent 用 read_file 分批读取）
"""
import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

FRAMES_PER_SEC = 1
BATCH_SIZE = 20


def extract_frames(video_path: Path, frames_dir: Path, fps: int = FRAMES_PER_SEC) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps={fps}",
        str(frames_dir / "frame_%04d.png"),
        "-loglevel", "error",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-300:]}")
    frames = sorted(frames_dir.glob("frame_*.png"))
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description="视频 → 抽帧（供 agent 自行分析）")
    parser.add_argument("target", help="视频 URL 或已下载视频的 slug")
    parser.add_argument("--title", default=None, help="视频标题（可选，默认用 slug）")
    args = parser.parse_args()

    is_url = args.target.startswith("http://") or args.target.startswith("https://")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)

        if is_url:
            slug = common.url_to_slug(args.target)
            logger.info("[analyze_video] downloading: %s", args.target)
            video_path = common.download_video(args.target, tmp_dir)
        else:
            slug = args.target
            video_path = common.work_path(slug, "video.mp4")
            if not video_path.exists():
                logger.error("[analyze_video] ERROR: %s not found.", video_path)
                sys.exit(1)

        title = args.title or slug.replace("_", " ")
        logger.info("[analyze_video] title: %r", title)

        frames_dir = common.work_path(slug, "frames")
        abs_frames_dir = frames_dir.resolve()
        logger.info("[analyze_video] extracting frames at %dfps → %s", FRAMES_PER_SEC, abs_frames_dir)
        frames = extract_frames(video_path, frames_dir)
        n = len(frames)

        batch_size = min(BATCH_SIZE, max(5, n // 3)) if n < BATCH_SIZE * 2 else BATCH_SIZE
        n_batches = (n + batch_size - 1) // batch_size

        logger.info("\n%s", "=" * 60)
        logger.info("帧提取完成")
        logger.info("  总帧数:    %d 帧（约 %d 秒）", n, n)
        logger.info("  帧目录:    %s", abs_frames_dir)
        logger.info("  建议批次:  每批 %d 帧，共 %d 批", batch_size, n_batches)
        logger.info("%s", "=" * 60)
        logger.info("\n请用 read_file 分批读取帧图片进行分析，每批 %d 张：", batch_size)
        for i in range(n_batches):
            start = i * batch_size + 1
            end = min((i + 1) * batch_size, n)
            logger.info("  批次 %d/%d: %s/frame_%04d.png → %s/frame_%04d.png",
                        i + 1, n_batches, abs_frames_dir, start, abs_frames_dir, end)


if __name__ == "__main__":
    main()
