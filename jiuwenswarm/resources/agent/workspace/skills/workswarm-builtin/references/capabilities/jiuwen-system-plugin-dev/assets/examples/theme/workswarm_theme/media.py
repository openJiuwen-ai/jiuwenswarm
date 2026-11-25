"""Background, splash and appearance for one theme (DESIGN.md sections 3 and 4).

Everything here maps a validated V1 manifest onto the V2 host vocabulary: ``MediaSurface`` props
and the ``ops.appearance.apply`` object. Pages receive what the host does not know through the
query string read by the compatible SDK.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

from agent_gateway.extensions_v2.webui.appearance import TOKEN_NAMES, valid_color
from agent_gateway.extensions_v2.webui.models import RenderNode

from . import ENGINE_VERSION
from .catalog import Theme
from .convert import ENGINE_DIR

UrlFor = Callable[[str], Awaitable[str]]
DEFAULT_SURFACE_OPACITY = 0.86
_POSITIONS = ("center", "top", "bottom", "left", "right")
_HEX3 = re.compile(r"^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])$")
_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})")


def operator_code(operator: str) -> str:
    """The 4-digit code V1 derived from the operator name (same hash as the page runtime)."""
    value = 0
    for character in operator:
        value = (value * 31 + ord(character)) & 0xFFFFFFFF
    return str(1000 + value % 9000)


def accent_hex(value: Any) -> str | None:
    """V2 accepts only #RRGGBB accents; convert the other V1 color forms when they are opaque."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _HEX6.match(text):
        return text.lower()
    match = _HEX3.match(text)
    if match:
        return "#" + "".join(part * 2 for part in match.groups()).lower()
    match = _RGB.match(text)
    if match and all(int(part) <= 255 for part in match.groups()):
        return "#" + "".join(f"{int(part):02x}" for part in match.groups())
    return None


def position(value: Any) -> str:
    """Nearest V2 position for a V1 CSS position such as ``50% 30%``."""
    text = str(value or "center").strip().lower()
    if text in _POSITIONS:
        return text
    words = text.split()
    for word in words:
        if word in ("top", "bottom", "left", "right"):
            return word
    numbers = [float(word[:-1]) for word in words if word.endswith("%") and word[:-1].replace(".", "", 1).isdigit()]
    if len(numbers) == 2:
        x, y = numbers
        if abs(y - 50) >= abs(x - 50) and abs(y - 50) > 15:
            return "top" if y < 50 else "bottom"
        if abs(x - 50) > 15:
            return "left" if x < 50 else "right"
    return "center"


def page_query(theme: Theme, settings: dict[str, Any], role: str) -> dict[str, str]:
    operator = settings.get("operator_name") or "研究员"
    return {
        "role": role, "operator": operator, "operator_code": operator_code(operator),
        "theme_id": theme.pack.id, "theme_name": theme.pack.name, "theme_version": theme.pack.version,
        "engine": ENGINE_VERSION,
    }


def with_query(url: str, query: dict[str, str]) -> str:
    if not query:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode(query, quote_via=quote)}"


def appearance(theme: Theme | None, settings: dict[str, Any], *, has_background: bool) -> dict[str, Any] | None:
    """The ``ops.appearance.apply`` object, or None to revoke.

    A pack's ``appearance.mascot`` is not passed on: the released host (0.2.9.beta3) rejects that field.
    """
    if theme is None:
        return None
    source = theme.pack.manifest.get("appearance", {})
    out: dict[str, Any] = {}
    accent = accent_hex(source.get("accent"))
    if accent:
        out["accent"] = accent
    tokens = {name: value for name, value in (source.get("tokens") or {}).items()
              if name in TOKEN_NAMES and valid_color(value)}
    if tokens:
        out["tokens"] = tokens
    if has_background:
        opacity = settings.get("surface_opacity")
        if opacity is None:
            opacity = source.get("surface_opacity", DEFAULT_SURFACE_OPACITY)
        out["surface"] = "translucent"
        out["surfaceOpacity"] = max(0.3, min(1.0, float(opacity)))
    return out or None


async def background_node(theme: Theme | None, settings: dict[str, Any], url_for: UrlFor) -> RenderNode | None:
    """The ``application.background`` root, or None when the theme has no background."""
    if theme is None:
        return None
    background = theme.pack.manifest["background"]
    kind = background["type"]
    if kind == "none":
        return None
    poster_path = background.get("poster") or background.get("reduced_motion_src")
    props: dict[str, Any] = {"title": theme.pack.name}
    if kind in ("color", "gradient"):
        props.update(kind="html", src=with_query(await url_for(f"{ENGINE_DIR}/fill.html"),
                                                 page_query(theme, settings, "background")))
    elif kind == "image" or (settings.get("motion") == "reduced" and poster_path):
        props.update(kind="image", src=await url_for(background["src"] if kind == "image" else poster_path))
    elif kind == "video":
        props.update(kind="video", src=await url_for(background["src"]), loop=True, muted=True)
    else:
        query = page_query(theme, settings, "background")
        if settings.get("motion") == "reduced":
            query["reduced"] = "1"
        props.update(kind="html", src=with_query(await url_for(background["src"]), query))
    if poster_path and props["kind"] != "image":
        props["poster"] = await url_for(poster_path)
    props["fit"] = "contain" if background.get("fit") == "contain" else "cover"
    props["position"] = position(background.get("position"))
    blur = settings.get("background_blur")
    dim = settings.get("background_dim")
    blur = background.get("blur") if blur is None else blur
    dim = background.get("dim") if dim is None else dim
    if blur:
        props["blur"] = blur
    if dim:
        props["dim"] = dim
    if background.get("opacity") is not None and background["opacity"] < 1:
        props["opacity"] = background["opacity"]
    return RenderNode(component="MediaSurface", props=props)


async def splash_node(theme: Theme, settings: dict[str, Any], url_for: UrlFor, *, decision: str,
                      reduced: bool) -> RenderNode | None:
    """The ``application.splash`` root for ``play`` or ``static``; None when nothing can be shown."""
    manifest = theme.pack.manifest
    boot = manifest["boot"]
    if decision == "static":
        still = boot.get("poster") or manifest.get("preview")
        if not still:
            return None
        return RenderNode(component="MediaSurface", props={
            "kind": "image", "src": await url_for(still), "title": theme.pack.name, "fit": "cover"})
    audio = boot.get("audio") or {}
    query = page_query(theme, settings, "boot")
    query["max"] = str(boot.get("max_duration_ms", 15000))
    if reduced and boot.get("reduced_motion") == "shorten":
        query["reduced"] = "1"
    play_audio = bool(audio) and settings.get("boot_audio", True)
    volume = settings.get("boot_volume")
    volume = audio.get("volume", 0.6) if volume is None else volume
    if play_audio and volume > 0 and audio["type"] in ("file", "synth"):
        query.update(audio=audio["type"], volume=f"{volume:g}", fade_in=str(audio.get("fade_in_ms", 300)),
                     fade_out=str(audio.get("fade_out_ms", 900)))
        if audio["type"] == "file":
            query["audio_src"] = await url_for(audio["src"])
            if audio.get("loop"):
                query["loop"] = "1"
    props: dict[str, Any] = {"title": theme.pack.name, "fit": "cover"}
    if boot["type"] == "sequence":
        props.update(kind="html", src=with_query(await url_for(f"{ENGINE_DIR}/lab-terminal.html"), query))
    elif boot["type"] == "html":
        props.update(kind="html", src=with_query(await url_for(boot["src"]), query))
    elif "audio" in query:
        props.update(kind="html", src=with_query(await url_for(f"{ENGINE_DIR}/video-boot.html"), query))
    else:
        props.update(kind="video", src=await url_for(boot["src"]), loop=False,
                     muted=not (play_audio and audio.get("type") == "video"))
    if boot.get("poster"):
        props["poster"] = await url_for(boot["poster"])
    return RenderNode(component="MediaSurface", props=props)
