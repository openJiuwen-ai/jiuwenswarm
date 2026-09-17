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
FILTER_BATCH = 3
FILTER_WORKERS = 3

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WORK_ROOT = SCRIPT_DIR / "work"
OPERATION_TIMEOUT_SECONDS = 600
BILIBILI_DOWNLOAD_ATTEMPTS = 3
BILIBILI_CHUNK_SIZE = 64 * 1024


# ── JSON helpers ─────────────────────────────────────────────────────────────

def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, data: dict) -> None:
    """Atomically publish stage JSON so downstream readers never see partial data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


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
    """Return a work path anchored to this skill, independent of shell cwd."""
    return WORK_ROOT / slug / filename


def resolve_work_slug_for_url(url: str) -> str:
    """Reuse the newest stage01 slug associated with url; otherwise derive one."""
    normalized = url.rstrip("/")
    matches: list[tuple[float, str]] = []
    if WORK_ROOT.exists():
        for stage01_path in WORK_ROOT.glob("*/stage01.json"):
            try:
                data = load_json(stage01_path)
            except (OSError, ValueError, TypeError):
                continue
            candidates = [data.get("url", ""), *(data.get("video_urls") or [])]
            if any(str(candidate).rstrip("/") == normalized for candidate in candidates):
                slug = str(data.get("slug") or stage01_path.parent.name)
                try:
                    modified = stage01_path.stat().st_mtime
                except OSError:
                    modified = 0.0
                matches.append((modified, slug))
    if matches:
        return max(matches)[1]
    return url_to_slug(url)


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
    """Download a Bilibili video via public API with persistent Range resume."""
    logger.info("[video] Bilibili detected, using public API with WBI signing (bvid=%s)", bvid)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / "video.mp4"
    partial = tmp_dir / "video.mp4.part"
    if out.exists() and out.stat().st_size > 0:
        logger.info("[video] reusing completed Bilibili download (%d bytes)", out.stat().st_size)
        return out

    _bili_headers = {
        "User-Agent": STEALTH_UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    nav = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=_bili_headers, timeout=OPERATION_TIMEOUT_SECONDS,
    ).json()
    wbi_img = nav.get("data", {}).get("wbi_img", {})
    img_key = wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    _mixin_tab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]
    mixin_key = reduce(lambda text, index: text + (img_key + sub_key)[index], _mixin_tab, "")[:32]

    def _wbi_sign(params: dict) -> dict:
        signed_params = dict(params)
        signed_params["wts"] = int(time.time())
        signed_params = dict(sorted(signed_params.items()))
        query = urllib.parse.urlencode(
            {key: "".join(char for char in str(value) if char not in "!'()*") for key, value in signed_params.items()}
        )
        signed_params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return signed_params

    view = requests.get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers=_bili_headers, timeout=OPERATION_TIMEOUT_SECONDS,
    ).json()
    cid = (view.get("data") or {}).get("cid")
    if not cid:
        code = view.get("code", "?")
        message = view.get("message", "?")
        hint = " (视频需要登录/大会员，暂不支持)" if code in (62012, -101, -400) else ""
        raise RuntimeError(f"Bilibili view API error code={code} message={message}{hint}")

    for quality in (80, 64, 32, 16):
        signed = _wbi_sign({"bvid": bvid, "cid": cid, "qn": quality, "fnval": 1})
        play = requests.get(
            "https://api.bilibili.com/x/player/playurl",
            params=signed, headers=_bili_headers, timeout=OPERATION_TIMEOUT_SECONDS,
        ).json()
        play_data = play.get("data", {})
        if play_data.get("durl") or play_data.get("dash", {}).get("video"):
            break
    else:
        raise RuntimeError(f"Bilibili playurl API returned no streams: {play.get('message')}")

    expected_size: int | None = None
    if play_data.get("durl"):
        stream = play_data["durl"][0]
        cdn_url = stream["url"]
        try:
            expected_size = int(stream.get("size") or 0) or None
        except (TypeError, ValueError):
            expected_size = None
    else:
        stream = play_data["dash"]["video"][0]
        cdn_url = stream["baseUrl"]

    download_headers = {**_bili_headers, "Accept-Encoding": "identity"}
    last_error: Exception | None = None
    for attempt in range(1, BILIBILI_DOWNLOAD_ATTEMPTS + 1):
        resume_from = partial.stat().st_size if partial.exists() else 0
        if expected_size and resume_from == expected_size:
            os.replace(partial, out)
            logger.info("[video] Bilibili resume already complete (%d bytes)", out.stat().st_size)
            return out
        if expected_size and resume_from > expected_size:
            partial.unlink()
            resume_from = 0

        headers = dict(download_headers)
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
            logger.info(
                "[video] Bilibili resume attempt %d/%d from byte %d",
                attempt,
                BILIBILI_DOWNLOAD_ATTEMPTS,
                resume_from,
            )
        else:
            logger.info("[video] Bilibili download attempt %d/%d", attempt, BILIBILI_DOWNLOAD_ATTEMPTS)

        try:
            with requests.get(
                cdn_url,
                headers=headers,
                timeout=OPERATION_TIMEOUT_SECONDS,
                stream=True,
            ) as response:
                if response.status_code == 416 and expected_size and resume_from >= expected_size:
                    os.replace(partial, out)
                    logger.info("[video] Bilibili API OK (%d bytes)", out.stat().st_size)
                    return out
                response.raise_for_status()

                append = resume_from > 0 and response.status_code == 206
                mode = "ab" if append else "wb"
                if not append:
                    resume_from = 0

                response_total: int | None = None
                content_range = response.headers.get("Content-Range", "")
                match = re.search(r"/(\d+)$", content_range)
                if match:
                    response_total = int(match.group(1))
                elif response.headers.get("Content-Length"):
                    try:
                        response_total = resume_from + int(response.headers["Content-Length"])
                    except ValueError:
                        response_total = None

                with open(partial, mode) as handle:
                    for chunk in response.iter_content(chunk_size=BILIBILI_CHUNK_SIZE):
                        if chunk:
                            handle.write(chunk)

            final_size = partial.stat().st_size
            required_size = expected_size or response_total
            if required_size and final_size < required_size:
                raise IOError(f"incomplete Bilibili download: {final_size}/{required_size} bytes")
            if final_size <= 0:
                raise IOError("Bilibili download produced an empty file")

            os.replace(partial, out)
            logger.info("[video] Bilibili API OK (%d bytes)", out.stat().st_size)
            return out
        except Exception as exc:
            last_error = exc
            saved = partial.stat().st_size if partial.exists() else 0
            logger.warning(
                "[video] Bilibili attempt %d/%d interrupted; preserved %d bytes for resume: %s",
                attempt,
                BILIBILI_DOWNLOAD_ATTEMPTS,
                saved,
                exc,
            )
            if attempt < BILIBILI_DOWNLOAD_ATTEMPTS:
                time.sleep(min(attempt * 2, 5))

    raise RuntimeError(
        f"Bilibili download failed after {BILIBILI_DOWNLOAD_ATTEMPTS} attempts; "
        f"partial file kept at {partial}"
    ) from last_error


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
    tmp_dir.mkdir(parents=True, exist_ok=True)

    bvid_match = re.search(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", url)
    if bvid_match:
        return _download_bilibili_wbi(bvid_match.group(1), tmp_dir)

    completed = sorted(
        path for path in tmp_dir.glob("video.*")
        if path.is_file() and not path.name.endswith(".part") and path.stat().st_size > 0
    )
    if completed:
        logger.info("[video] reusing completed download: %s", completed[0])
        return completed[0]

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
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OPERATION_TIMEOUT_SECONDS,
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
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=OPERATION_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            videos = list(tmp_dir.glob("video.*"))
            if videos:
                logger.info("[video] yt-dlp (no cookies) OK")
                return videos[0]
        last_err = result.stderr[-300:]

    logger.info("[video] trying direct HTTP...")
    try:
        r = requests.get(url, timeout=OPERATION_TIMEOUT_SECONDS, stream=True, headers={"User-Agent": STEALTH_UA})
        r.raise_for_status()
        out = tmp_dir / "video.mp4"
        out.write_bytes(r.content)
        return out
    except Exception as exc:
        raise RuntimeError(
            f"Cannot download video: yt-dlp failed ({last_err[-100:]}) "
            f"and all fallbacks also failed ({exc})"
        ) from exc
