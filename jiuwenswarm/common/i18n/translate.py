# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Message catalog lookup for AgentServer user-facing tool copy.

Callers pass an explicit ``language`` that maps to ``zh`` / ``en``. Missing or
illegal values resolve to ``en``. This module does not look up preferred
language from enterprise memo / yaml.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Mapping

import yaml

logger = logging.getLogger(__name__)

_ZH_ALIASES = frozenset({"zh", "cn", "zh_cn", "zh-cn"})
_EN_ALIASES = frozenset({"en", "en_us", "en-us"})
_CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
_LOCK = threading.Lock()
_CATALOGS: dict[str, dict[str, str]] | None = None


def try_normalize_preferred_language(raw: object) -> str | None:
    """Map a raw language token to ``zh`` / ``en``, or ``None`` if unrecognized."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    compact = text.replace("-", "_")
    if text in _ZH_ALIASES or compact in {"zh", "cn", "zh_cn"}:
        return "zh"
    if text in _EN_ALIASES or compact in {"en", "en_us"}:
        return "en"
    return None


def normalize_preferred_language(raw: object) -> str:
    """Always return ``zh`` or ``en``; empty / illegal / errors → ``en``."""
    return try_normalize_preferred_language(raw) or "en"


def to_runtime_language(raw: object) -> str | None:
    """Map a UI / preference token to harness runtime ``cn`` / ``en``.

    ``zh`` / ``cn`` aliases become ``cn``; ``en`` stays ``en``. Unrecognized or
    empty input returns ``None`` so the caller can fall back to
    ``resolve_preferred_language()`` or an explicit value — this helper does
    not default to ``en``.
    """
    mapped = try_normalize_preferred_language(raw)
    if mapped == "zh":
        return "cn"
    if mapped == "en":
        return "en"
    return None


def _load_catalog_file(path: Path) -> dict[str, str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - catalog failure must not break requests
        logger.warning(
            "failed to load i18n catalog %s; treating as empty",
            path,
            exc_info=True,
        )
        return {}
    if not isinstance(raw, Mapping):
        logger.warning("i18n catalog %s is not a mapping; treating as empty", path)
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def _ensure_catalogs() -> dict[str, dict[str, str]]:
    global _CATALOGS
    if _CATALOGS is not None:
        return _CATALOGS
    with _LOCK:
        if _CATALOGS is not None:
            return _CATALOGS
        _CATALOGS = {
            "en": _load_catalog_file(_CATALOG_DIR / "en.yaml"),
            "zh": _load_catalog_file(_CATALOG_DIR / "zh.yaml"),
        }
        return _CATALOGS


def _resolve_language(language: object | None) -> str:
    if language is not None:
        mapped = try_normalize_preferred_language(language)
        if mapped is not None:
            return mapped
    return "en"


def _interpolate(template: str, params: Mapping[str, Any]) -> str:
    result = template
    for key, value in params.items():
        placeholder = "{" + str(key) + "}"
        try:
            result = result.replace(placeholder, str(value))
        except Exception:  # noqa: BLE001 - never raise into chat
            logger.warning(
                "i18n interpolate failed for placeholder %s",
                key,
                exc_info=True,
            )
    return result


def t(message_id: str, /, *, language: object | None = None, **params: Any) -> str:
    """翻译函数：按消息 id 取本地化文案（translate 的惯用短名）。

    语言仅来自显式 ``language``（能归一成 ``zh`` / ``en``）；缺省或非法 →
    ``en``。中文模板缺失时回退英文；英文也缺失或词表读失败时返回
    ``message_id`` 本身，并打英文 warning。需要跟用户偏好时，由调用方先
    ``resolve_preferred_language()`` 再传入。

    ``params`` 按 ``{name}`` 字面替换；缺参保留占位符，参数值里的花括号
    不再二次解析。调用方只传字面量 id，不要拼接动态 id。
    """
    if not isinstance(message_id, str) or not message_id:
        return str(message_id or "")
    lang = _resolve_language(language)
    catalogs = _ensure_catalogs()
    template = catalogs.get(lang, {}).get(message_id)
    if template is None and lang != "en":
        template = catalogs.get("en", {}).get(message_id)
    if template is None:
        logger.warning("missing i18n message id=%s language=%s", message_id, lang)
        return message_id
    if not params:
        return template
    return _interpolate(template, params)


def reset_catalog_cache_for_tests() -> None:
    """Clear the in-process catalog cache (unit tests only)."""
    global _CATALOGS
    with _LOCK:
        _CATALOGS = None
