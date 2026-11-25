"""主题包清单 theme.json（主题接口 v1）的读取与校验。

校验器只接受声明过的字段，所有文件引用都必须是主题包内存在的相对路径；
错误信息使用中文，直接展示在主题管理页。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import THEME_API_VERSION
from .sanitize import UnsafeContentError, check_svg

MANIFEST_NAME = "theme.json"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_SEQUENCE_BYTES = 128 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PACK_BYTES = 256 * 1024 * 1024
MAX_PACK_FILES = 600

ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,5})$")
COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}"
    r"|(?:rgb|rgba|hsl|hsla)\(\s*[0-9.%\s,/+-]+\)|transparent)$"
)
GRADIENT_RE = re.compile(
    r"^(?:repeating-)?(?:linear|radial|conic)-gradient\([#0-9a-zA-Z.,%\s()/+-]{1,400}\)$"
)
POSITION_RE = re.compile(r"^[a-z0-9 %.-]{1,32}$")
TOKEN_NAME_RE = re.compile(r"^--(?:color|theme|font|radius|shadow)-[a-z0-9-]{1,60}$")
TOKEN_VALUE_RE = re.compile(r"^[#0-9a-zA-Z.,%\s()/+-]{1,160}$")
FONT_VALUE_RE = re.compile(r"^[\w\s,\"'.-]{1,160}$")
PHASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
TEMPLATE_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

TEMPLATE_VARIABLES = frozenset({
    "operator",
    "operator_code",
    "date",
    "time",
    "weekday",
    "theme_name",
    "theme_version",
    "engine_version",
    "session",
})

# 主题包允许包含的文件类型。不在表中的文件不会被发布。
FILE_KINDS: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".avif": "image",
    ".svg": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mp3": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".oga": "audio",
    ".opus": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".html": "html",
    ".htm": "html",
    ".js": "script",
    ".css": "style",
    ".json": "data",
    ".woff2": "font",
    ".woff": "font",
    ".ttf": "font",
    ".otf": "font",
    ".txt": "text",
    ".md": "text",
}
NAMED_TEXT_FILES = frozenset({"LICENSE", "NOTICE", "COPYING", "AUTHORS"})
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

BACKGROUND_TYPES = ("none", "color", "gradient", "image", "video", "html")
BOOT_TYPES = ("none", "sequence", "html", "video")
BOOT_POLICIES = ("session", "every_load", "daily", "version", "manual")
FIT_VALUES = ("cover", "contain", "fill")
REDUCED_MOTION_MODES = ("static", "shorten", "skip")
EXIT_MODES = ("fade", "wipe")
# 开场声音：file 为主题包内的音频文件，video 为视频开场的原声，synth 为运行时实时合成（无需文件）。
AUDIO_TYPES = ("file", "video", "synth")
SYNTH_PRESETS = ("lab-terminal",)
SEQUENCE_RENDERERS = ("lab-terminal",)
PANEL_TYPES = ("meters", "archive", "identity", "grant", "text")
LOG_TONES = ("normal", "accent", "warn", "danger", "muted")
PALETTE_KEYS = (
    "paper",
    "ink",
    "accent",
    "accent2",
    "muted",
    "console",
    "console_ink",
    "warn",
    "danger",
)


class ThemeValidationError(ValueError):
    """主题包无法通过校验，``errors`` 为全部中文错误。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors) or ["主题包无效"]
        super().__init__("；".join(self.errors))


def file_kind(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in NAMED_TEXT_FILES:
        return "text"
    return FILE_KINDS.get(PurePosixPath(path).suffix.lower())


@dataclass
class PackFile:
    path: str
    kind: str
    size: int


@dataclass
class ThemePack:
    id: str
    name: str
    version: str
    root: Path
    manifest: dict[str, Any]
    files: list[PackFile]
    warnings: list[str] = field(default_factory=list)
    source: str = "extension"

    @property
    def features(self) -> dict[str, Any]:
        appearance = self.manifest.get("appearance", {})
        return {
            "background": self.manifest["background"]["type"],
            "boot": self.manifest["boot"]["type"],
            "skin": bool(appearance.get("accent") or appearance.get("tokens") or appearance.get("stylesheet")),
            "audio": bool(self.manifest["boot"].get("audio")),
            "mascot": bool(appearance.get("mascot")),
        }


class _Checker:
    def __init__(self, root: Path):
        self.root = root
        self.root_resolved = root.resolve()
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    # ---- 基础类型 -------------------------------------------------------

    def obj(self, value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.error(f"{where} 必须是对象")
            return {}
        unknown = sorted(set(value) - allowed)
        if unknown:
            self.error(f"{where} 含有未知字段：{', '.join(unknown)}")
        return value

    def text(self, value: Any, where: str, *, max_len: int, required: bool = False) -> str | None:
        if value is None:
            if required:
                self.error(f"缺少 {where}")
            return None
        if not isinstance(value, str):
            self.error(f"{where} 必须是字符串")
            return None
        value = value.strip()
        if required and not value:
            self.error(f"{where} 不能为空")
            return None
        if len(value) > max_len:
            self.error(f"{where} 超过 {max_len} 个字符")
            return None
        if any(ord(ch) < 32 and ch not in "\t" for ch in value):
            self.error(f"{where} 含有控制字符")
            return None
        for name in TEMPLATE_RE.findall(value):
            if name not in TEMPLATE_VARIABLES:
                self.warnings.append(f"{where} 使用了未知模板变量 {{{{{name}}}}}，将原样显示")
        return value

    def number(self, value: Any, where: str, *, lo: float, hi: float, integer: bool = False) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.error(f"{where} 必须是数字")
            return None
        if integer and not float(value).is_integer():
            self.error(f"{where} 必须是整数")
            return None
        if not lo <= value <= hi:
            self.error(f"{where} 必须在 {lo:g} 到 {hi:g} 之间")
            return None
        return int(value) if integer else float(value)

    def boolean(self, value: Any, where: str) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            self.error(f"{where} 必须是 true 或 false")
            return None
        return value

    def choice(self, value: Any, where: str, choices: tuple[str, ...], default: str | None = None) -> str | None:
        if value is None:
            return default
        if value not in choices:
            self.error(f"{where} 只能是：{' / '.join(choices)}")
            return default
        return value

    def color(self, value: Any, where: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not COLOR_RE.match(value.strip()):
            self.error(f"{where} 不是有效的颜色值")
            return None
        return value.strip()

    def strings(self, value: Any, where: str, *, max_items: int, max_len: int) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.error(f"{where} 必须是字符串数组")
            return []
        if len(value) > max_items:
            self.error(f"{where} 最多 {max_items} 项")
            value = value[:max_items]
        out = []
        for index, item in enumerate(value):
            text = self.text(item, f"{where}[{index}]", max_len=max_len)
            if text:
                out.append(text)
        return out

    def items(self, value: Any, where: str, *, max_items: int) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.error(f"{where} 必须是数组")
            return []
        if len(value) > max_items:
            self.error(f"{where} 最多 {max_items} 项")
            return value[:max_items]
        return value

    # ---- 文件引用 -------------------------------------------------------

    def path(self, value: Any, where: str, kinds: tuple[str, ...], *, required: bool = False) -> str | None:
        if value is None:
            if required:
                self.error(f"缺少 {where}")
            return None
        if not isinstance(value, str) or not value.strip():
            self.error(f"{where} 必须是主题包内的相对路径")
            return None
        rel = value.strip()
        if rel.startswith(("/", "\\")) or "\\" in rel or ":" in rel or "\x00" in rel:
            self.error(f"{where} 只能是主题包内的相对路径：{rel}")
            return None
        parts = PurePosixPath(rel).parts
        if not parts or any(part in ("..", ".") or part.startswith(".") for part in parts):
            self.error(f"{where} 路径无效：{rel}")
            return None
        kind = file_kind(rel)
        if kind not in kinds:
            self.error(f"{where} 的文件类型不允许（需要 {' / '.join(kinds)}）：{rel}")
            return None
        full = self.root
        for part in parts:
            full = full / part
            if full.is_symlink():
                self.error(f"{where} 不能经过符号链接：{rel}")
                return None
        try:
            full.resolve().relative_to(self.root_resolved)
        except ValueError:
            self.error(f"{where} 指向主题包之外：{rel}")
            return None
        if not full.is_file():
            self.error(f"{where} 引用的文件不存在：{rel}")
            return None
        return PurePosixPath(*parts).as_posix()


def _validate_tokens(check: _Checker, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        check.error("appearance.tokens 必须是对象")
        return {}
    if len(value) > 120:
        check.error("appearance.tokens 最多 120 项")
        return {}
    tokens: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not TOKEN_NAME_RE.match(name):
            check.error(f"appearance.tokens 的变量名无效：{name}（仅允许 --color-、--theme-、--font-、--radius-、--shadow- 开头）")
            continue
        if not isinstance(raw, str):
            check.error(f"appearance.tokens.{name} 必须是字符串")
            continue
        text = raw.strip()
        pattern = FONT_VALUE_RE if name.startswith("--font-") else TOKEN_VALUE_RE
        lowered = text.lower()
        if not pattern.match(text) or "url(" in lowered or "expression" in lowered:
            check.error(f"appearance.tokens.{name} 的值不安全或格式无效")
            continue
        tokens[name] = text
    return tokens


def _validate_appearance(check: _Checker, value: Any) -> dict[str, Any]:
    raw = check.obj(value, "appearance", {"accent", "surface_opacity", "tokens", "stylesheet", "mascot"})
    out: dict[str, Any] = {}
    mascot = _validate_mascot(check, raw.get("mascot"))
    if mascot:
        out["mascot"] = mascot
    accent = check.color(raw.get("accent"), "appearance.accent")
    if accent:
        out["accent"] = accent
    opacity = check.number(raw.get("surface_opacity"), "appearance.surface_opacity", lo=0.3, hi=1.0)
    if opacity is not None:
        out["surface_opacity"] = opacity
    tokens = _validate_tokens(check, raw.get("tokens"))
    if tokens:
        out["tokens"] = tokens
    sheet = check.path(raw.get("stylesheet"), "appearance.stylesheet", ("style",))
    if sheet:
        out["stylesheet"] = sheet
    return out


def _validate_mascot(check: _Checker, value: Any) -> dict[str, Any] | None:
    """The home-page mascot: a still image, optionally a hover animation and its length (host S_08 section 2.3)."""
    if value is None:
        return None
    raw = check.obj(value, "appearance.mascot", {"src", "hover_src", "hover_ms"})
    if not raw:
        return None
    src = check.path(raw.get("src"), "appearance.mascot.src", ("image",), required=True)
    hover_src = check.path(raw.get("hover_src"), "appearance.mascot.hover_src", ("image",))
    hover_ms = check.number(raw.get("hover_ms"), "appearance.mascot.hover_ms", lo=500, hi=10000, integer=True)
    if ("hover_src" in raw) != ("hover_ms" in raw):
        check.error("appearance.mascot.hover_src 与 hover_ms 需要同时提供")
        return None
    if not src:
        return None
    out: dict[str, Any] = {"src": src}
    if hover_src and hover_ms is not None:
        out.update(hover_src=hover_src, hover_ms=hover_ms)
    return out


def _validate_background(check: _Checker, value: Any) -> dict[str, Any]:
    raw = check.obj(value, "background", {
        "type", "color", "gradient", "src", "poster", "reduced_motion_src",
        "fit", "position", "blur", "dim", "opacity", "playback_rate",
    })
    kind = check.choice(raw.get("type"), "background.type", BACKGROUND_TYPES, "none")
    out: dict[str, Any] = {"type": kind}
    color = check.color(raw.get("color"), "background.color")
    if color:
        out["color"] = color
    if kind == "gradient":
        gradient = raw.get("gradient")
        lowered = gradient.lower() if isinstance(gradient, str) else ""
        if not isinstance(gradient, str) or not GRADIENT_RE.match(gradient.strip()) or "url" in lowered:
            check.error("background.gradient 必须是 linear/radial/conic-gradient(...) 渐变")
        else:
            out["gradient"] = gradient.strip()
    if kind == "color" and not color:
        check.error("background.type 为 color 时必须提供 background.color")
    src_kinds = {"image": ("image",), "video": ("video",), "html": ("html",)}.get(kind)
    if src_kinds:
        src = check.path(raw.get("src"), "background.src", src_kinds, required=True)
        if src:
            out["src"] = src
    elif raw.get("src") is not None:
        check.error(f"background.type 为 {kind} 时不使用 background.src")
    for key in ("poster", "reduced_motion_src"):
        ref = check.path(raw.get(key), f"background.{key}", ("image",))
        if ref:
            out[key] = ref
    out["fit"] = check.choice(raw.get("fit"), "background.fit", FIT_VALUES, "cover")
    position = raw.get("position")
    if position is not None:
        if not isinstance(position, str) or not POSITION_RE.match(position):
            check.error("background.position 格式无效，例如 \"center\" 或 \"50% 30%\"")
        else:
            out["position"] = position
    for key, lo, hi in (("blur", 0, 40), ("dim", 0, 0.9), ("opacity", 0.1, 1), ("playback_rate", 0.25, 2)):
        number = check.number(raw.get(key), f"background.{key}", lo=lo, hi=hi)
        if number is not None:
            out[key] = number
    return out


def _validate_log(check: _Checker, value: Any, where: str) -> list[dict[str, str]]:
    lines = []
    for index, item in enumerate(check.items(value, where, max_items=16)):
        at = f"{where}[{index}]"
        if isinstance(item, str):
            item = {"text": item}
        raw = check.obj(item, at, {"text", "status", "tone"})
        text = check.text(raw.get("text"), f"{at}.text", max_len=80, required=True)
        if not text:
            continue
        line = {"text": text}
        status = check.text(raw.get("status"), f"{at}.status", max_len=6)
        if status:
            line["status"] = status
        line["tone"] = check.choice(raw.get("tone"), f"{at}.tone", LOG_TONES, "normal")
        lines.append(line)
    return lines


def _validate_panel(check: _Checker, value: Any, where: str) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = check.obj(value, where, {
        "type", "title", "items", "entries", "fields", "checks", "result",
        "monogram", "subtitle", "stamp", "lines",
    })
    kind = check.choice(raw.get("type"), f"{where}.type", PANEL_TYPES)
    if kind is None:
        return None
    panel: dict[str, Any] = {"type": kind}
    title = check.text(raw.get("title"), f"{where}.title", max_len=24)
    if title:
        panel["title"] = title
    if kind == "meters":
        items = []
        for index, item in enumerate(check.items(raw.get("items"), f"{where}.items", max_items=6)):
            at = f"{where}.items[{index}]"
            entry = check.obj(item, at, {"label", "value", "text"})
            label = check.text(entry.get("label"), f"{at}.label", max_len=12, required=True)
            amount = check.number(entry.get("value"), f"{at}.value", lo=0, hi=1)
            text = check.text(entry.get("text"), f"{at}.text", max_len=12)
            if label is not None:
                items.append({"label": label, "value": 0.5 if amount is None else amount, "text": text or ""})
        panel["items"] = items
    elif kind == "archive":
        entries = []
        for index, item in enumerate(check.items(raw.get("entries"), f"{where}.entries", max_items=8)):
            at = f"{where}.entries[{index}]"
            entry = check.obj(item, at, {"code", "label", "level", "state"})
            code = check.text(entry.get("code"), f"{at}.code", max_len=20, required=True)
            label = check.text(entry.get("label"), f"{at}.label", max_len=24, required=True)
            if code and label:
                entries.append({
                    "code": code,
                    "label": label,
                    "level": check.text(entry.get("level"), f"{at}.level", max_len=6) or "",
                    "state": check.text(entry.get("state"), f"{at}.state", max_len=8) or "已解密",
                })
        panel["entries"] = entries
    elif kind == "identity":
        fields = []
        for index, item in enumerate(check.items(raw.get("fields"), f"{where}.fields", max_items=6)):
            at = f"{where}.fields[{index}]"
            entry = check.obj(item, at, {"label", "value"})
            label = check.text(entry.get("label"), f"{at}.label", max_len=8, required=True)
            text = check.text(entry.get("value"), f"{at}.value", max_len=32, required=True)
            if label and text:
                fields.append({"label": label, "value": text})
        checks = []
        for index, item in enumerate(check.items(raw.get("checks"), f"{where}.checks", max_items=4)):
            at = f"{where}.checks[{index}]"
            entry = check.obj(item, at, {"label", "result"})
            label = check.text(entry.get("label"), f"{at}.label", max_len=12, required=True)
            if label:
                checks.append({"label": label, "result": check.text(entry.get("result"), f"{at}.result", max_len=8) or "通过"})
        panel["fields"] = fields
        panel["checks"] = checks
        panel["result"] = check.text(raw.get("result"), f"{where}.result", max_len=24) or "身份确认完毕"
        monogram = check.text(raw.get("monogram"), f"{where}.monogram", max_len=16)
        if monogram:
            panel["monogram"] = monogram
    elif kind == "grant":
        panel["title"] = title or "访问已授权"
        for key, max_len in (("subtitle", 40), ("stamp", 24)):
            text = check.text(raw.get(key), f"{where}.{key}", max_len=max_len)
            if text:
                panel[key] = text
    elif kind == "text":
        panel["lines"] = check.strings(raw.get("lines"), f"{where}.lines", max_items=8, max_len=60)
    return panel


def _validate_sequence(check: _Checker, value: Any) -> dict[str, Any] | None:
    where = "boot.sequence"
    if isinstance(value, str):
        rel = check.path(value, where, ("data",))
        if rel is None:
            return None
        full = check.root.joinpath(*PurePosixPath(rel).parts)
        if full.stat().st_size > MAX_SEQUENCE_BYTES:
            check.error(f"{where} 文件超过 128 KiB")
            return None
        try:
            value = json.loads(full.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            check.error(f"{where} 不是有效的 UTF-8 JSON：{exc}")
            return None
        where = rel
    raw = check.obj(value, where, {"renderer", "palette", "brand", "hud", "phases"})
    if not raw:
        check.error("boot.type 为 sequence 时必须提供 boot.sequence")
        return None
    sequence: dict[str, Any] = {
        "renderer": check.choice(raw.get("renderer"), f"{where}.renderer", SEQUENCE_RENDERERS, "lab-terminal"),
    }
    palette_raw = check.obj(raw.get("palette"), f"{where}.palette", set(PALETTE_KEYS))
    palette = {}
    for key in PALETTE_KEYS:
        color = check.color(palette_raw.get(key), f"{where}.palette.{key}")
        if color:
            palette[key] = color
    sequence["palette"] = palette

    brand_raw = check.obj(raw.get("brand"), f"{where}.brand", {"mark", "title", "subtitle", "tag"})
    brand: dict[str, str] = {"title": check.text(brand_raw.get("title"), f"{where}.brand.title", max_len=40) or "研究终端"}
    mark = check.path(brand_raw.get("mark"), f"{where}.brand.mark", ("image",))
    if mark:
        brand["mark"] = mark
    for key, max_len in (("subtitle", 60), ("tag", 40)):
        text = check.text(brand_raw.get(key), f"{where}.brand.{key}", max_len=max_len)
        if text:
            brand[key] = text
    sequence["brand"] = brand

    hud_raw = check.obj(raw.get("hud"), f"{where}.hud", {"tl", "tr", "bl", "br"})
    sequence["hud"] = {
        key: text
        for key in ("tl", "tr", "bl", "br")
        if (text := check.text(hud_raw.get(key), f"{where}.hud.{key}", max_len=64))
    }

    phases = []
    total = 0
    for index, item in enumerate(check.items(raw.get("phases"), f"{where}.phases", max_items=8)):
        at = f"{where}.phases[{index}]"
        entry = check.obj(item, at, {"id", "label", "duration_ms", "log", "panel"})
        phase_id = entry.get("id")
        if not isinstance(phase_id, str) or not PHASE_ID_RE.match(phase_id):
            check.error(f"{at}.id 必须是小写字母、数字、- 或 _，最长 24 位")
            continue
        duration = check.number(entry.get("duration_ms"), f"{at}.duration_ms", lo=300, hi=8000, integer=True)
        duration = 1500 if duration is None else duration
        total += duration
        phases.append({
            "id": phase_id,
            "label": check.text(entry.get("label"), f"{at}.label", max_len=12) or phase_id,
            "duration_ms": duration,
            "log": _validate_log(check, entry.get("log"), f"{at}.log"),
            "panel": _validate_panel(check, entry.get("panel"), f"{at}.panel"),
        })
    if not phases:
        check.error(f"{where}.phases 至少需要一个阶段")
    if total > 30000:
        check.error(f"{where} 各阶段总时长不能超过 30 秒（当前 {total / 1000:.1f} 秒）")
    sequence["phases"] = phases
    sequence["total_ms"] = total
    return sequence


def _validate_boot(check: _Checker, value: Any) -> dict[str, Any]:
    raw = check.obj(value, "boot", {
        "type", "sequence", "src", "poster", "background", "policy",
        "min_duration_ms", "max_duration_ms", "skippable", "skip_after_ms",
        "wait_for_host", "reduced_motion", "exit", "audio",
    })
    kind = check.choice(raw.get("type"), "boot.type", BOOT_TYPES, "none")
    boot: dict[str, Any] = {"type": kind}
    if kind == "none":
        return boot
    if kind == "sequence":
        sequence = _validate_sequence(check, raw.get("sequence"))
        if sequence:
            boot["sequence"] = sequence
    elif raw.get("sequence") is not None:
        check.error(f"boot.type 为 {kind} 时不使用 boot.sequence")
    if kind in ("html", "video"):
        src = check.path(raw.get("src"), "boot.src", ("html",) if kind == "html" else ("video",), required=True)
        if src:
            boot["src"] = src
    poster = check.path(raw.get("poster"), "boot.poster", ("image",))
    if poster:
        boot["poster"] = poster
    background = check.color(raw.get("background"), "boot.background")
    if background:
        boot["background"] = background
    boot["policy"] = check.choice(raw.get("policy"), "boot.policy", BOOT_POLICIES, "session")
    minimum = check.number(raw.get("min_duration_ms"), "boot.min_duration_ms", lo=0, hi=20000, integer=True)
    maximum = check.number(raw.get("max_duration_ms"), "boot.max_duration_ms", lo=2000, hi=60000, integer=True)
    boot["min_duration_ms"] = 0 if minimum is None else minimum
    boot["max_duration_ms"] = 15000 if maximum is None else maximum
    if boot["min_duration_ms"] > boot["max_duration_ms"]:
        check.error("boot.min_duration_ms 不能大于 boot.max_duration_ms")
    skippable = check.boolean(raw.get("skippable"), "boot.skippable")
    boot["skippable"] = True if skippable is None else skippable
    skip_after = check.number(raw.get("skip_after_ms"), "boot.skip_after_ms", lo=0, hi=5000, integer=True)
    boot["skip_after_ms"] = 600 if skip_after is None else skip_after
    wait = check.boolean(raw.get("wait_for_host"), "boot.wait_for_host")
    boot["wait_for_host"] = True if wait is None else wait
    boot["reduced_motion"] = check.choice(raw.get("reduced_motion"), "boot.reduced_motion", REDUCED_MOTION_MODES, "shorten")
    boot["exit"] = check.choice(raw.get("exit"), "boot.exit", EXIT_MODES, "fade")
    audio = _validate_boot_audio(check, raw.get("audio"), kind)
    if audio:
        boot["audio"] = audio
    return boot


def _validate_boot_audio(check: _Checker, value: Any, boot_kind: str) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = check.obj(value, "boot.audio", {"type", "src", "preset", "volume", "fade_in_ms", "fade_out_ms", "loop"})
    if not isinstance(value, dict):
        return None
    kind = check.choice(raw.get("type"), "boot.audio.type", AUDIO_TYPES)
    if kind is None:
        if raw.get("type") is None:
            check.error("缺少 boot.audio.type")
        return None
    audio: dict[str, Any] = {"type": kind}
    if kind == "file":
        src = check.path(raw.get("src"), "boot.audio.src", ("audio",), required=True)
        if src:
            audio["src"] = src
        loop = check.boolean(raw.get("loop"), "boot.audio.loop")
        audio["loop"] = bool(loop)
    else:
        if raw.get("src") is not None:
            check.error(f"boot.audio.type 为 {kind} 时不使用 boot.audio.src")
        if raw.get("loop") is not None:
            check.error("boot.audio.loop 只用于 type 为 file 的声音")
    if kind == "video" and boot_kind != "video":
        check.error("boot.audio.type 为 video 时 boot.type 必须是 video")
    if kind == "synth":
        audio["preset"] = check.choice(raw.get("preset"), "boot.audio.preset", SYNTH_PRESETS, SYNTH_PRESETS[0])
    elif raw.get("preset") is not None:
        check.error("boot.audio.preset 只用于 type 为 synth 的声音")
    volume = check.number(raw.get("volume"), "boot.audio.volume", lo=0, hi=1)
    audio["volume"] = 0.6 if volume is None else volume
    fade_in = check.number(raw.get("fade_in_ms"), "boot.audio.fade_in_ms", lo=0, hi=5000, integer=True)
    audio["fade_in_ms"] = 300 if fade_in is None else fade_in
    fade_out = check.number(raw.get("fade_out_ms"), "boot.audio.fade_out_ms", lo=0, hi=5000, integer=True)
    audio["fade_out_ms"] = 900 if fade_out is None else fade_out
    return audio


def scan_pack_files(root: Path) -> tuple[list[PackFile], list[str], list[str]]:
    """列出主题包内可发布的文件，返回 (文件, 错误, 警告)。"""

    files: list[PackFile] = []
    errors: list[str] = []
    warnings: list[str] = []
    total = 0
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept = []
        for name in sorted(dirnames):
            child = current / name
            if name.startswith(".") or name == "__MACOSX" or name == "__pycache__":
                continue
            if child.is_symlink():
                warnings.append(f"已忽略符号链接目录：{child.relative_to(root).as_posix()}")
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            full = current / name
            rel = full.relative_to(root).as_posix()
            if name in IGNORED_NAMES or name.startswith("."):
                continue
            if full.is_symlink():
                warnings.append(f"已忽略符号链接：{rel}")
                continue
            kind = file_kind(rel)
            if kind is None:
                warnings.append(f"文件类型不受支持，未发布：{rel}")
                continue
            size = full.stat().st_size
            if size > MAX_FILE_BYTES:
                errors.append(f"文件超过 64 MiB：{rel}")
                continue
            if kind == "image" and full.suffix.lower() == ".svg":
                try:
                    check_svg(full.read_bytes())
                except UnsafeContentError as exc:
                    errors.append(f"{rel}：{exc}")
                    continue
            try:
                full.resolve().relative_to(root_resolved)
            except ValueError:
                errors.append(f"文件指向主题包之外：{rel}")
                continue
            total += size
            files.append(PackFile(rel, kind, size))
    if len(files) > MAX_PACK_FILES:
        errors.append(f"主题包文件数超过 {MAX_PACK_FILES}")
    if total > MAX_PACK_BYTES:
        errors.append("主题包总大小超过 256 MiB")
    return files, errors, warnings


def load_theme_pack(root: Path, *, source: str = "extension") -> ThemePack:
    """读取并校验一个主题包目录；失败时抛出 ThemeValidationError。"""

    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ThemeValidationError([f"缺少 {MANIFEST_NAME}"])
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ThemeValidationError([f"{MANIFEST_NAME} 超过 256 KiB"])
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ThemeValidationError([f"{MANIFEST_NAME} 必须使用 UTF-8 编码"]) from exc
    except json.JSONDecodeError as exc:
        raise ThemeValidationError([f"{MANIFEST_NAME} 不是有效的 JSON：第 {exc.lineno} 行 {exc.msg}"]) from exc

    check = _Checker(root)
    raw = check.obj(data, MANIFEST_NAME, {
        "$schema", "theme_api", "id", "name", "version", "description", "author",
        "license", "homepage", "tags", "preview", "appearance", "background", "boot",
    })
    if raw.get("theme_api") != THEME_API_VERSION:
        check.error(f"theme_api 必须为 {THEME_API_VERSION}（当前主题接口版本）")
    theme_id = raw.get("id")
    if not isinstance(theme_id, str) or len(theme_id) > 64 or not ID_RE.match(theme_id):
        check.error("id 必须以小写字母开头，只含小写字母、数字和单个连字符，最长 64 位")
        theme_id = None
    name = check.text(raw.get("name"), "name", max_len=40, required=True)
    version = raw.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        check.error("version 必须是 x.y.z 格式")
        version = None
    manifest: dict[str, Any] = {
        "description": check.text(raw.get("description"), "description", max_len=200) or "",
        "author": check.text(raw.get("author"), "author", max_len=60) or "",
        "license": check.text(raw.get("license"), "license", max_len=60) or "",
        "tags": check.strings(raw.get("tags"), "tags", max_items=8, max_len=16),
    }
    homepage = raw.get("homepage")
    if homepage is not None:
        if isinstance(homepage, str) and re.match(r"^https://[^\s\"'<>]{1,200}$", homepage):
            manifest["homepage"] = homepage
        else:
            check.error("homepage 必须是 https:// 地址")
    preview = check.path(raw.get("preview"), "preview", ("image",))
    if preview:
        manifest["preview"] = preview
    manifest["appearance"] = _validate_appearance(check, raw.get("appearance"))
    manifest["background"] = _validate_background(check, raw.get("background"))
    manifest["boot"] = _validate_boot(check, raw.get("boot"))

    if manifest["background"]["type"] == "none" and manifest["boot"]["type"] == "none" and not manifest["appearance"]:
        check.error("主题至少需要提供背景、开场动画或界面配色中的一项")

    files, file_errors, file_warnings = scan_pack_files(root)
    check.errors.extend(file_errors)
    check.warnings.extend(file_warnings)
    if check.errors:
        raise ThemeValidationError(check.errors)
    return ThemePack(
        id=theme_id,
        name=name,
        version=version,
        root=root,
        manifest=manifest,
        files=files,
        warnings=check.warnings,
        source=source,
    )
