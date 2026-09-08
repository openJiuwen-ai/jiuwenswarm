"""Optional bridge to Xiaoyi Work's private prompt-asset package.

The open-source runtime deliberately keeps the package optional.  Release
builds install ``xiaoyi-prompt-assets`` and set ``XIAOYI_PROMPT_ASSETS`` to
``required`` so a packaging error cannot silently ship generic prompts.
"""

from __future__ import annotations

import os
from typing import Any

from openjiuwen.harness.prompts import PromptSection

try:
    import xiaoyi_prompt_assets as _xiaoyi_prompt_assets
except ModuleNotFoundError as exc:
    if exc.name != "xiaoyi_prompt_assets":
        raise
    _xiaoyi_prompt_assets = None


_REQUIRED_ENV = "XIAOYI_PROMPT_ASSETS"


class PrivatePromptAssetsError(RuntimeError):
    """Raised when a release build cannot load its required private assets."""


def _required() -> bool:
    return os.getenv(_REQUIRED_ENV, "").strip().lower() == "required"


def _payload() -> dict[str, Any] | None:
    if _xiaoyi_prompt_assets is None:
        if _required():
            raise PrivatePromptAssetsError(
                "xiaoyi-prompt-assets is required for this release build"
            )
        return None

    payload = _xiaoyi_prompt_assets.load_prompt_assets()
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PrivatePromptAssetsError("unsupported xiaoyi prompt-asset schema")
    return payload


def load_mode_sections(mode: str) -> tuple[PromptSection, ...] | None:
    """Return private static sections for one mode, or ``None`` for OSS mode."""
    payload = _payload()
    if payload is None:
        return None

    modes = payload.get("modes")
    raw_sections = modes.get(mode) if isinstance(modes, dict) else None
    if not isinstance(raw_sections, list) or not raw_sections:
        raise PrivatePromptAssetsError(f"private prompt assets lack mode={mode!r}")

    sections: list[PromptSection] = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise PrivatePromptAssetsError(f"invalid private section for mode={mode!r}")
        name, priority, content = raw.get("id"), raw.get("priority"), raw.get("content")
        if not isinstance(name, str) or not isinstance(priority, int) or not isinstance(content, dict):
            raise PrivatePromptAssetsError(f"invalid private section fields for mode={mode!r}")
        if not all(isinstance(language, str) and isinstance(text, str) for language, text in content.items()):
            raise PrivatePromptAssetsError(f"invalid private section content for mode={mode!r}")
        sections.append(PromptSection(name=name, content=content, priority=priority))
    return tuple(sections)


def load_shared_text(name: str) -> Any | None:
    """Load a non-section private asset such as the safety or runtime block."""
    payload = _payload()
    if payload is None:
        return None
    shared = payload.get("shared")
    if not isinstance(shared, dict) or name not in shared:
        raise PrivatePromptAssetsError(f"private prompt assets lack shared item={name!r}")
    return shared[name]


__all__ = ["PrivatePromptAssetsError", "load_mode_sections", "load_shared_text"]
