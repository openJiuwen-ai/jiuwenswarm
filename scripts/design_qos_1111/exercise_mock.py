#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise gen_toolkits against the local mock vLLM-Omni / MiniMax-H3 server."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("DESIGN_QOS_MOCK_URL", "http://127.0.0.1:18091/v1"))
    parser.add_argument("--out", default=str(HERE / "out"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Allow running from a checkout without installing the package: add repo root.
    repo = HERE.parents[1]
    sys.path.insert(0, str(repo))

    from jiuwenswarm.agents.harness.common.tools import gen_toolkits as gt

    image_dir = out / "images"
    video_dir = out / "videos"
    image_dir.mkdir(exist_ok=True)
    video_dir.mkdir(exist_ok=True)

    # vLLM-Omni path used by the design page when protocol=vllm-omni.
    omni = gt.GenerationTarget(gt.VLLM_OMNI, "", args.base, "")
    image = await gt.generate_image(omni, "a kitchen scene", "16:9", str(image_dir), gt.ImageOptions())
    print("vllm-omni image:", image)
    if image.startswith("[ERROR]"):
        return 1

    # MiniMax-H3 JSON path (same /v1/images/generations shape).
    mm = gt.GenerationTarget(gt.MINIMAX, "mock-key", args.base, "MiniMax-H3-Image")
    mm_image = await gt.generate_image(mm, "a kitchen scene", "16:9", str(image_dir), gt.ImageOptions())
    print("minimax-h3 image:", mm_image)
    if mm_image.startswith("[ERROR]"):
        return 1

    # Custom model name must be forwarded, not rejected client-side.
    custom = gt.GenerationTarget(gt.MINIMAX, "mock-key", args.base, "some-brand-new-image")
    custom_image = await gt.generate_image(custom, "custom model", "1:1", str(image_dir))
    print("custom-model image:", custom_image)
    if custom_image.startswith("[ERROR]"):
        return 1

    ref = HERE / "fixtures" / "image.png"
    edited = await gt.generate_image(
        mm,
        "edit the still",
        "16:9",
        str(image_dir),
        gt.ImageOptions(reference_image_uris=(str(ref),)),
    )
    print("minimax-h3 edit:", edited)
    if edited.startswith("[ERROR]"):
        return 1

    video = await gt.submit_video(
        omni,
        gt.VideoRequest("a short clip", "16:9", "720P", 5, False, None),
        str(video_dir),
    )
    print("vllm-omni video:", video)
    if video.startswith("[ERROR]"):
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
