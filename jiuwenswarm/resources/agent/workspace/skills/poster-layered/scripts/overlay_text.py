#!/usr/bin/env python3
"""Overlay text onto a poster base image using layout.json (Pillow)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Common CJK-capable fonts on macOS / Linux / Windows (first existing wins).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _resolve_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths: list[str] = []
    if font_path:
        paths.append(font_path)
    paths.extend(_FONT_CANDIDATES)
    for p in paths:
        if p and Path(p).is_file():
            try:
                return ImageFont.truetype(p, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    if max_width <= 0:
        return text.splitlines() or [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            trial = current + ch
            bbox = draw.textbbox((0, 0), trial, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines or [""]


def overlay(base_path: Path, layout_path: Path, out_path: Path) -> None:
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    blocks = layout.get("blocks") or []
    default_font = layout.get("font_path")

    img = Image.open(base_path).convert("RGBA")
    draw = ImageDraw.Draw(img)

    for block in blocks:
        text = str(block.get("text") or "")
        if not text.strip():
            continue
        x = int(block.get("x", 0))
        y = int(block.get("y", 0))
        size = int(block.get("font_size", 48))
        fill = block.get("fill") or "#FFFFFF"
        stroke_fill = block.get("stroke_fill")
        stroke_width = int(block.get("stroke_width") or 0)
        max_width = int(block.get("max_width") or 0)
        align = str(block.get("align") or "left")
        line_gap_raw = block.get("line_gap")
        line_gap = int(line_gap_raw) if line_gap_raw is not None else max(4, size // 6)
        font = _resolve_font(block.get("font_path") or default_font, size)

        lines = _wrap_text(draw, text, font, max_width)
        cy = y
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
            if align == "center" and max_width > 0:
                lx = x + (max_width - w) // 2
            elif align == "right" and max_width > 0:
                lx = x + max_width - w
            else:
                lx = x
            kwargs: dict = {"font": font, "fill": fill}
            if stroke_width > 0 and stroke_fill:
                kwargs["stroke_width"] = stroke_width
                kwargs["stroke_fill"] = stroke_fill
            draw.text((lx, cy), line, **kwargs)
            cy += (bbox[3] - bbox[1]) + line_gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgb = img.convert("RGB")
    rgb.save(out_path, format="PNG")
    logger.info("OK wrote %s", out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Overlay text on poster base image")
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--layout", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not args.base.is_file():
        logger.error("base not found: %s", args.base)
        return 1
    if not args.layout.is_file():
        logger.error("layout not found: %s", args.layout)
        return 1
    overlay(args.base, args.layout, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
