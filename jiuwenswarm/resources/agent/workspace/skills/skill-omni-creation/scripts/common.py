#!/usr/bin/env python3
import base64
import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
from functools import reduce
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MIME_TO_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MIN_DIMENSION = 80
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FETCH_WORKERS = 10
FILTER_BATCH = 5
FILTER_WORKERS = 3


# ── JSON helpers ─────────────────────────────────────────────────────────────

def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_json_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def encode_b64(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.standard_b64encode(data).decode()}"


# ── Path / slug helpers ───────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:80]


def url_to_slug(url: str) -> str:
    parsed = urlparse(url)
    raw = (parsed.netloc + parsed.path).strip("/")
    if parsed.netloc in ("www.youtube.com", "youtube.com") and parsed.path == "/watch":
        qs = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        if "v" in qs:
            raw = raw + "_" + qs["v"]
    return slugify(raw)


def work_path(slug: str, filename: str) -> pathlib.Path:
    return pathlib.Path("work") / slug / filename


def image_ext(url: str, mime: str) -> str:
    ext = pathlib.Path(urlparse(url).path).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        ext = MIME_TO_EXT.get(mime, ".png")
    return ext


# ── Asset helpers ─────────────────────────────────────────────────────────────

def save_fetched_assets(
    fetched: dict[str, tuple[bytes, str]],
    asset_dir: pathlib.Path,
    prefix: str,
) -> dict[str, dict]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for idx, (url, (data, mime)) in enumerate(fetched.items()):
        rel_path = pathlib.Path(f"{prefix}_{idx:03d}{image_ext(url, mime)}")
        out_path = asset_dir / rel_path
        out_path.write_bytes(data)
        manifest[url] = {"path": rel_path.as_posix(), "mime": mime}
    return manifest


def load_fetched_assets(asset_dir: pathlib.Path, manifest: dict[str, dict]) -> dict[str, tuple[bytes, str]]:
    fetched: dict[str, tuple[bytes, str]] = {}
    for url, meta in manifest.items():
        path = asset_dir / meta["path"]
        fetched[url] = (path.read_bytes(), meta["mime"])
    return fetched


# ── Blocks helpers ────────────────────────────────────────────────────────────

def blocks_with_paths_as_str(blocks: list[dict]) -> list[dict]:
    result = []
    for b in blocks:
        if b.get("type") == "image" and b.get("path") is not None:
            result.append({**b, "path": str(b["path"])})
        else:
            result.append(b)
    return result


def blocks_with_paths_as_path(blocks: list[dict]) -> list[dict]:
    result = []
    for b in blocks:
        if b.get("type") == "image" and b.get("path") is not None:
            result.append({**b, "path": pathlib.Path(str(b["path"]))})
        else:
            result.append(b)
    return result


def strip_hallucinated_images(md: str, valid_paths: set[str]) -> str:
    """Remove any ![...](path) lines where path is not in valid_paths."""
    def _check(match: re.Match) -> str:
        path = match.group(2).strip()
        return match.group(0) if path in valid_paths else ""

    cleaned = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _check, md)
    lines = []
    for line in cleaned.splitlines():
        if line.strip():
            lines.append(line)
        elif lines and lines[-1].strip():
            lines.append(line)
    return "\n".join(lines).strip()


# ── Video download ────────────────────────────────────────────────────────────

def _download_bilibili_wbi(bvid: str, tmp_dir: pathlib.Path) -> pathlib.Path:
    """Download a Bilibili video via public API with WBI signing."""
    logger.info("[video] Bilibili detected, using public API with WBI signing (bvid=%s)", bvid)
    _bili_headers = {
        "User-Agent": STEALTH_UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    nav = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=_bili_headers, timeout=10,
    ).json()
    wbi_img = nav.get("data", {}).get("wbi_img", {})
    img_key = wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    _mixin_tab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]
    mixin_key = reduce(lambda s, i: s + (img_key + sub_key)[i], _mixin_tab, "")[:32]

    def _wbi_sign(params: dict) -> dict:
        p = dict(params)
        p["wts"] = int(time.time())
        p = dict(sorted(p.items()))
        qs = urllib.parse.urlencode(
            {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
        )
        p["w_rid"] = hashlib.md5((qs + mixin_key).encode()).hexdigest()
        return p

    view = requests.get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers=_bili_headers, timeout=30,
    ).json()
    cid = (view.get("data") or {}).get("cid")
    if not cid:
        code = view.get("code", "?")
        msg = view.get("message", "?")
        hint = " (视频需要登录/大会员，暂不支持)" if code in (62012, -101, -400) else ""
        raise RuntimeError(f"Bilibili view API error code={code} message={msg}{hint}")

    for qn in (80, 64, 32, 16):
        signed = _wbi_sign({"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1})
        play = requests.get(
            "https://api.bilibili.com/x/player/playurl",
            params=signed, headers=_bili_headers, timeout=30,
        ).json()
        play_data = play.get("data", {})
        if play_data.get("durl") or play_data.get("dash", {}).get("video"):
            break
    else:
        raise RuntimeError(f"Bilibili playurl API returned no streams: {play.get('message')}")

    if play_data.get("durl"):
        cdn_url = play_data["durl"][0]["url"]
    else:
        cdn_url = play_data["dash"]["video"][0]["baseUrl"]

    _dl_headers = {**_bili_headers, "Accept-Encoding": "identity"}
    out = tmp_dir / "video.mp4"
    for attempt in range(3):
        try:
            resp = requests.get(cdn_url, headers=_dl_headers, timeout=300, stream=True)
            resp.raise_for_status()
            with open(out, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            if out.stat().st_size > 0:
                logger.info("[video] Bilibili API OK (%d bytes)", out.stat().st_size)
                return out
        except Exception:
            if attempt == 2:
                raise
    raise RuntimeError("Bilibili download failed after 3 attempts")


_YT_DLP_BASE = [
    "--js-runtimes", "node",
    "--no-playlist",
]

_YT_DLP_BROWSERS = ["safari", "chrome", "firefox", "edge"]

_YT_DLP_QUALITY_TIERS = [
    "worst[ext=mp4]/worst",
    "bestvideo[height<=360]+bestaudio/best[height<=360]/best[height<=360]",
    "bestvideo[height<=144]+bestaudio/best[height<=144]/best[height<=144]",
]


def download_video(url: str, tmp_dir: pathlib.Path, max_minutes: int | None = None) -> pathlib.Path:
    """Download video via yt-dlp. Returns path to downloaded file."""
    xhs_match = re.search(r"xiaohongshu\.com/discovery/item/([a-f0-9]+)", url)
    if xhs_match:
        url = f"https://www.xiaohongshu.com/explore/{xhs_match.group(1)}"
        logger.info("[video] normalized XiaoHongShu URL to explore format: %s", url)

    logger.info("[video] Downloading: %s", url)

    bvid_match = re.search(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", url)
    if bvid_match:
        return _download_bilibili_wbi(bvid_match.group(1), tmp_dir)

    out_template = str(tmp_dir / "video.%(ext)s")
    last_err = ""

    extra_flags: list[str] = []
    if max_minutes is not None:
        extra_flags = ["--download-sections", f"*0:00-{max_minutes}:00"]

    _ytdlp = [sys.executable, "-m", "yt_dlp"]

    for browser in _YT_DLP_BROWSERS:
        for fmt in _YT_DLP_QUALITY_TIERS:
            cmd = _ytdlp + _YT_DLP_BASE + ["--cookies-from-browser", browser] + extra_flags + [
                "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", out_template,
                url,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
            if result.returncode == 0:
                videos = list(tmp_dir.glob("video.*"))
                if videos:
                    return videos[0]
            last_err = result.stderr[-300:]
            if "cookie database" in last_err.lower() or "could not copy" in last_err.lower():
                logger.info("[video] %s cookies locked, trying next browser...", browser)
                break

    for fmt in _YT_DLP_QUALITY_TIERS:
        cmd = _ytdlp + _YT_DLP_BASE + extra_flags + [
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", out_template,
            url,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        if result.returncode == 0:
            videos = list(tmp_dir.glob("video.*"))
            if videos:
                logger.info("[video] yt-dlp (no cookies) OK")
                return videos[0]
        last_err = result.stderr[-300:]

    logger.info("[video] trying direct HTTP...")
    try:
        r = requests.get(url, timeout=60, stream=True, headers={"User-Agent": STEALTH_UA})
        r.raise_for_status()
        out = tmp_dir / "video.mp4"
        out.write_bytes(r.content)
        return out
    except Exception as exc:
        raise RuntimeError(
            f"Cannot download video: yt-dlp failed ({last_err[-100:]}) "
            f"and all fallbacks also failed ({exc})"
        ) from exc
