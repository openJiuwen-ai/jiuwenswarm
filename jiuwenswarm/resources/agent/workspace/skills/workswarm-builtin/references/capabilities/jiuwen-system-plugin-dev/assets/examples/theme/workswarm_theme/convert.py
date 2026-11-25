"""Turn a validated V1 theme pack into a directory the V2 host can serve (DESIGN.md section 3).

Built-in themes are converted when the package is built; imported themes when they are
imported. The output is a self-contained copy: every page finds the compatible SDK and the
engine pages under ``_workswarm/`` next to it, so a theme never depends on where the package
assets or the data directory are published.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import shutil
from pathlib import Path, PurePosixPath

from .schema import ThemePack

ENGINE_DIR = "_workswarm"
#: Where V1 pages loaded the SDK from; rewritten to the copy inside the theme.
V1_SDK_PATH = "/api/application-plugins/workswarm-theme/assets/sdk/theme-sdk.js"
_SDK_SCRIPT = re.compile(
    r"""(?P<head><script\b[^>]*?\bsrc\s*=\s*)(?P<quote>["'])(?P<src>[^"']*)(?P=quote)(?P<tail>[^>]*>\s*</script>)""",
    re.IGNORECASE,
)
_UNSAFE_CSS = re.compile(r"[<>{};\\]")


def engine_assets() -> Path:
    """Directory holding the SDK and the engine page templates."""
    return Path(__file__).resolve().parents[1] / "assets" / "engine"


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _relative(target: str, page: str) -> str:
    """Relative URL from the directory of ``page`` to ``target`` (both pack-relative)."""
    return posixpath.relpath(target, posixpath.dirname(page) or ".")


def _rewrite_page(text: str, page: str, *, synth: bool) -> tuple[str, bool]:
    """Point V1 SDK script tags at the bundled SDK; add the synth library before it when needed."""
    sdk = _relative(f"{ENGINE_DIR}/theme-sdk.js", page)
    synth_src = _relative(f"{ENGINE_DIR}/lab-synth.js", page)
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        if match.group("src").split("?", 1)[0] != V1_SDK_PATH:
            return match.group(0)
        found = True
        tag = f"{match.group('head')}{match.group('quote')}{sdk}{match.group('quote')}{match.group('tail')}"
        return f'<script src="{synth_src}"></script>\n  {tag}' if synth else tag

    return _SDK_SCRIPT.sub(replace, text), found


def _fill_page(value: str) -> str:
    if _UNSAFE_CSS.search(value):
        raise ValueError("background color or gradient contains characters a page cannot embed")
    return (engine_assets() / "fill.html").read_text(encoding="utf-8").replace("{{BACKGROUND}}", value)


def _video_page(boot: dict) -> str:
    template = (engine_assets() / "video-boot.html").read_text(encoding="utf-8")
    poster = f"../{boot['poster']}" if boot.get("poster") else ""
    return (template.replace("{{VIDEO}}", html.escape(f"../{boot['src']}", quote=True))
            .replace("{{POSTER}}", html.escape(poster, quote=True)))


def convert_pack(pack: ThemePack, destination: Path) -> list[str]:
    """Write a servable copy of ``pack`` to ``destination`` (which must not exist).

    Returns notes for the user about V1 features V2 does not keep.
    """
    destination = Path(destination)
    destination.mkdir(parents=True)
    manifest = pack.manifest
    boot = manifest["boot"]
    background = manifest["background"]
    appearance = manifest.get("appearance", {})
    audio = boot.get("audio") or {}
    synth = audio.get("type") == "synth" or boot["type"] == "sequence"
    engine = engine_assets()
    notes: list[str] = []

    for item in pack.files:
        _copy(pack.root / item.path, destination / item.path)
    out = destination / ENGINE_DIR
    _copy(engine / "theme-sdk.js", out / "theme-sdk.js")
    if synth:
        _copy(engine / "lab-synth.js", out / "lab-synth.js")

    pages = {page for page in (background.get("src") if background["type"] == "html" else None,
                               boot.get("src") if boot["type"] == "html" else None) if page}
    for item in pack.files:
        if item.kind != "html":
            continue
        target = destination / item.path
        text = target.read_text(encoding="utf-8", errors="replace")
        rewritten, found = _rewrite_page(text, item.path, synth=synth and item.path == boot.get("src"))
        if rewritten != text:
            target.write_text(rewritten, encoding="utf-8")
        if item.path in pages and not found:
            notes.append(f"页面 {item.path} 没有引用主题 SDK，可能无法通知 WorkSwarm 已就绪或已播完")

    if boot["type"] == "sequence":
        sequence = json.loads(json.dumps(boot["sequence"]))
        mark = (sequence.get("brand") or {}).get("mark")
        if mark:
            sequence["brand"]["mark"] = f"../{mark}"
        _copy(engine / "lab-terminal.html", out / "lab-terminal.html")
        _copy(engine / "lab-terminal.js", out / "lab-terminal.js")
        (out / "sequence.js").write_text(
            "window.WORKSWARM_SEQUENCE = " + json.dumps(sequence, ensure_ascii=False) + ";\n", encoding="utf-8")
    if boot["type"] == "video" and audio.get("type") in ("file", "synth"):
        (out / "video-boot.html").write_text(_video_page(boot), encoding="utf-8")
    if background["type"] in ("color", "gradient"):
        value = background.get("gradient") if background["type"] == "gradient" else background["color"]
        (out / "fill.html").write_text(_fill_page(value), encoding="utf-8")

    if appearance.get("stylesheet"):
        notes.append(f"补充样式表 {appearance['stylesheet']} 在 V2 中不再生效，强调色与装饰变量照常应用")
    if boot["type"] != "none" and (boot.get("exit") == "wipe" or boot.get("min_duration_ms")):
        notes.append("上卷退场与最短显示时长由 WorkSwarm 统一处理，开场改为淡出并按内容结束")
    return notes


def page_path(relative: str) -> str:
    """Normalize a pack-relative path for asset URLs."""
    return PurePosixPath(relative).as_posix()
