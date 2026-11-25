"""Preferences, playback records and the splash decision (DESIGN.md sections 4.2 and 5).

Validation follows the V1 engine's ``state.py``; settings V2 no longer has (the skip switch and the
desktop bridge) are dropped, and the motion setting keeps "follow the system" and "reduce".
"""

from __future__ import annotations

from typing import Any

from . import DEFAULT_THEME
from .schema import BOOT_POLICIES, ID_RE

PREFS_KEY = "prefs"
PLAYED_KEY = "played"
MOTION_MODES = ("system", "reduced")

DEFAULT_SETTINGS: dict[str, Any] = {
    "boot_enabled": True,
    # None keeps the theme's own value.
    "boot_policy": None,
    "boot_audio": True,
    "boot_volume": None,
    "operator_name": "研究员",
    "surface_opacity": None,
    "background_dim": None,
    "background_blur": None,
    "motion": "system",
}
_BOUNDS = {
    "surface_opacity": (0.3, 1.0),
    "background_dim": (0.0, 0.9),
    "background_blur": (0.0, 40.0),
    "boot_volume": (0.0, 1.0),
}


def validate_setting(key: str, value: Any) -> Any:
    """Return the normalized value or raise ``ValueError`` with a message for the page."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"未知设置项：{key}")
    if key in ("boot_enabled", "boot_audio"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value
    if key == "boot_policy":
        if value is not None and value not in BOOT_POLICIES:
            raise ValueError("播放时机无效")
        return value
    if key == "motion":
        if value not in MOTION_MODES:
            raise ValueError("动态效果模式无效")
        return value
    if key == "operator_name":
        if value is None:
            return DEFAULT_SETTINGS["operator_name"]
        if not isinstance(value, str):
            raise ValueError("研究员代号必须是文字")
        text = value.strip()
        if not text:
            return DEFAULT_SETTINGS["operator_name"]
        if len(text) > 24 or any(ord(character) < 32 for character in text):
            raise ValueError("研究员代号最多 24 个字符，且不能包含控制字符")
        return text
    low, high = _BOUNDS[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 必须是数字")
    if not low <= value <= high:
        raise ValueError(f"{key} 超出范围 {low:g}–{high:g}")
    return round(float(value), 3)


def normalize(raw: Any) -> dict[str, Any]:
    """Read stored preferences; unknown or invalid entries fall back to defaults."""
    data = raw if isinstance(raw, dict) else {}
    active = data.get("active_theme", DEFAULT_THEME)
    settings = dict(DEFAULT_SETTINGS)
    stored = data.get("settings")
    if isinstance(stored, dict):
        for key, value in stored.items():
            try:
                settings[key] = validate_setting(key, value)
            except ValueError:
                continue
    return {
        "active_theme": active if active is None or (isinstance(active, str) and ID_RE.match(active)) else DEFAULT_THEME,
        "settings": settings,
    }


def defaults() -> dict[str, Any]:
    return normalize({})


def effective_policy(boot: dict[str, Any], settings: dict[str, Any]) -> str:
    """The playback policy; ``every_load`` plays once per launch in V2 like ``session``."""
    policy = settings.get("boot_policy") or boot.get("policy") or "session"
    return "session" if policy == "every_load" else policy


def splash_decision(boot: dict[str, Any], theme_id: str, version: str, settings: dict[str, Any],
                    played: dict[str, Any], *, today: str, preview: bool, reduced: bool) -> str:
    """Return ``play``, ``static`` or ``skip`` for one splash ``ui.ready``."""
    if boot.get("type", "none") == "none":
        return "skip"
    if reduced:
        mode = boot.get("reduced_motion", "shorten")
        if mode == "skip" and not preview:
            return "skip"
        if mode == "static":
            return "static"
    if preview:
        return "play"
    if not settings.get("boot_enabled", True):
        return "skip"
    policy = effective_policy(boot, settings)
    record = played.get(theme_id) if isinstance(played.get(theme_id), dict) else {}
    if policy == "manual":
        return "skip"
    if policy == "daily" and record.get("day") == today:
        return "skip"
    if policy == "version" and record.get("version") == version:
        return "skip"
    return "play"


def record_played(played: dict[str, Any], theme_id: str, version: str, today: str) -> dict[str, Any]:
    out = {key: dict(value) for key, value in played.items() if isinstance(value, dict)}
    out[theme_id] = {"day": today, "version": version}
    return out
