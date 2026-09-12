#!/usr/bin/env python3
import hashlib
import json
import locale
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
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

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SKILLS_ROOT = SCRIPT_DIR.parent.parent
OPERATION_TIMEOUT_SECONDS = 600
BILIBILI_DOWNLOAD_ATTEMPTS = 3
BILIBILI_CHUNK_SIZE = 64 * 1024


def utf8_subprocess_env() -> dict[str, str]:
    """Force Python child processes (notably yt-dlp) to use UTF-8 stdio on every OS."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def configure_console_output() -> str:
    """Match CLI stdout to the host console codec without changing UTF-8 file/subprocess protocols."""
    if os.name == "nt":
        getencoding = getattr(locale, "getencoding", None)
        encoding = getencoding() if getencoding else locale.getpreferredencoding(False)
    else:
        encoding = getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False)
    encoding = encoding or "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding=encoding, errors="backslashreplace")
    return encoding


# ── JSON helpers ─────────────────────────────────────────────────────────────

def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, data: dict) -> None:
    """Atomically publish stage JSON so downstream readers never see partial data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


# ── Runtime / package / final Skill paths ────────────────────────────────────

RUNTIME_ROOT = SKILLS_ROOT / ".skill-omni-creation"
KEBAB_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def skill_dir(name: str) -> pathlib.Path:
    """Return the final generated Skill directory. Final folder == SKILL.md name."""
    return SKILLS_ROOT / name


def run_dir(run_id: str) -> pathlib.Path:
    """Return one UUID-scoped temporary run directory."""
    return RUNTIME_ROOT / run_id


def runtime_path(run_id: str, filename: str) -> pathlib.Path:
    """Return runtime-only state under <skills>/.skill-omni-creation/<run_id>/runtime/."""
    return run_dir(run_id) / "runtime" / filename


def package_dir(run_id: str) -> pathlib.Path:
    """Return the final-package staging directory for one run."""
    return run_dir(run_id) / "package"


def work_path(run_id: str, filename: str) -> pathlib.Path:
    """Compatibility alias used by existing stage scripts; the identifier is a run_id, not a final Skill name."""
    return runtime_path(run_id, filename)


def _run_matches_url(run_id: str, url: str) -> bool:
    normalized = url.rstrip("/")
    for filename in ("run.json", "stage01.json"):
        path = runtime_path(run_id, filename)
        if not path.is_file():
            continue
        try:
            data = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        candidates = [data.get("url", ""), *(data.get("video_urls") or [])]
        if any(str(candidate).rstrip("/") == normalized for candidate in candidates):
            return True
    return False


def find_run_id_for_url(url: str) -> str | None:
    """Return the newest unfinished UUID workspace for this URL, if any."""
    matches: list[tuple[float, str]] = []
    if RUNTIME_ROOT.exists():
        for candidate in RUNTIME_ROOT.iterdir():
            if not candidate.is_dir() or not _run_matches_url(candidate.name, url):
                continue
            marker = runtime_path(candidate.name, "stage01.json")
            if not marker.exists():
                marker = runtime_path(candidate.name, "run.json")
            try:
                modified = marker.stat().st_mtime
            except OSError:
                modified = 0.0
            matches.append((modified, candidate.name))
    return max(matches)[1] if matches else None


def resolve_run_id(url: str, requested_run_id: str | None = None) -> str:
    """Reuse an unfinished run for the URL, otherwise allocate a new UUID."""
    if requested_run_id:
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", requested_run_id):
            raise ValueError("run_id must be a UUID-like identifier")
        return requested_run_id.lower()
    return find_run_id_for_url(url) or uuid.uuid4().hex


def ensure_run_metadata(run_id: str, url: str) -> pathlib.Path:
    """Persist minimal recovery metadata before content extraction starts."""
    path = runtime_path(run_id, "run.json")
    if not path.exists():
        write_json(path, {"run_id": run_id, "url": url, "status": "running"})
    return path


def resolve_run_id_for_url(url: str) -> str:
    run_id = find_run_id_for_url(url)
    if not run_id:
        raise FileNotFoundError(f"no unfinished run found for URL: {url}")
    return run_id


def validate_skill_name(name: str) -> str:
    """Validate the one authoritative final identity from SKILL.md frontmatter."""
    value = name.strip()
    if not value or len(value) > 80 or not KEBAB_NAME_RE.fullmatch(value):
        raise ValueError(
            "SKILL.md name must be lowercase kebab-case: [a-z0-9]+(?:-[a-z0-9]+)*"
        )
    if value in {"skill-omni-creation", ".skill-omni-creation"}:
        raise ValueError(f"reserved Skill name: {value}")
    return value


# ── Asset helpers ─────────────────────────────────────────────────────────────

def image_ext(url: str, mime: str) -> str:
    """Return a safe image suffix, preferring the validated MIME type."""
    normalized_mime = (mime or "").split(";", 1)[0].strip().lower()
    if normalized_mime in MIME_TO_EXT:
        return MIME_TO_EXT[normalized_mime]

    suffix = pathlib.Path(urlparse(url).path).suffix.lower()
    if suffix in SUPPORTED_EXTS:
        return suffix
    return ".jpg"


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
                env=utf8_subprocess_env(),
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
            env=utf8_subprocess_env(),
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
