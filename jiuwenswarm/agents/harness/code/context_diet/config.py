# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration schema for ``modes.code.context_diet``.

The YAML block is optional and every key has a default, so an existing
``config.yaml`` without the section behaves exactly like the defaults listed
here. Values are clamped to sane ranges so a typo cannot disable a safety
margin (for example a zero head window on shell output).

Default policy
--------------
* Shell (``bash``) and ``grep`` trimming is **on**: the full output is always
  spilled to disk and quoted back, so the model loses nothing it cannot fetch.
* ``read_file`` trimming and the ``read_file`` compaction window are **off**
  (``0``) in the ``default`` preset: a file the model has just read is usually
  what it edits next, and shortening it can change the edit it produces.
* Shell / grep / glob compaction is **on** with a window of 4 results.
* Shell commands that print file contents or diffs (``sed -n``, ``cat``,
  ``git diff``, ``inspect.getsource``… see ``source_print.py``) are treated like
  ``read_file``: not trimmed below ``source_print_max_lines`` and not folded by
  the shell compaction window. In the 2026-10-02 benchmark 35 of 79 shell trims
  hit such commands, so "shell output is never an edit source" did not hold.

Presets
-------
``modes.code.context_diet.preset`` selects a named set of defaults
(:data:`PRESETS`); any key written explicitly under ``trim`` / ``compaction``
still wins over the preset. The ``default`` thresholds come from the wave-1
SWE-bench Pro A/B transcripts (docs/en/ContextDiet.md §3.1): they trim roughly
the largest third of ``bash`` / ``grep`` results, where the first thresholds
(120 lines / 6000 chars) fired on 13 of 475 tool calls. ``aggressive`` is meant
for headless runs and also trims and windows ``read_file`` results.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SHELL_TOOL_NAMES: tuple[str, ...] = ("bash", "shell", "execute_cmd", "powershell")
"""Tool names whose results are shell output (trimmed head/tail + signal lines)."""

GREP_TOOL_NAMES: tuple[str, ...] = ("grep", "glob")
"""Search tools whose results are line lists (trimmed head only)."""

READ_TOOL_NAMES: tuple[str, ...] = ("read_file",)
"""File reads; trimming is opt-in because the content feeds edits."""


def _int(value: Any, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(parsed, minimum)
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "default": {
        "trim": {
            "bash_max_lines": 45,
            "bash_max_chars": 2500,
            "bash_head_lines": 20,
            "bash_tail_lines": 15,
            "max_signal_lines": 25,
            "grep_max_lines": 40,
            "grep_head_lines": 30,
            "read_file_max_lines": 0,
        },
        "compaction": {
            "shell_keep_last_k": 4,
            "read_keep_last_k": 0,
            "min_offload_chars": 800,
        },
    },
    "aggressive": {
        "trim": {
            "bash_max_lines": 30,
            "bash_max_chars": 1800,
            "bash_head_lines": 15,
            "bash_tail_lines": 12,
            "max_signal_lines": 20,
            "grep_max_lines": 30,
            "grep_head_lines": 25,
            "read_file_max_lines": 250,
        },
        "compaction": {
            "shell_keep_last_k": 3,
            "read_keep_last_k": 6,
            "min_offload_chars": 600,
        },
    },
}
"""Named default sets for ``modes.code.context_diet.preset``. Explicit keys override them."""

DEFAULT_PRESET = "default"


def resolve_preset(name: Any) -> str:
    """Return a known preset name; unknown values fall back to ``default`` with a warning."""
    key = str(name or "").strip().lower()
    if not key:
        return DEFAULT_PRESET
    if key not in PRESETS:
        logger.warning(
            "[context_diet] unknown preset %r; using %r (known: %s)",
            name, DEFAULT_PRESET, ", ".join(sorted(PRESETS)),
        )
        return DEFAULT_PRESET
    return key


def _merged(section: str, preset: str, raw: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(PRESETS[preset].get(section, {}))
    if isinstance(raw, Mapping):
        data.update({k: v for k, v in raw.items() if v is not None})
    return data


@dataclass(frozen=True)
class TrimConfig:
    """Ingestion trimming thresholds (``modes.code.context_diet.trim``)."""

    enabled: bool = True
    """Master switch for the trim rail. ``false`` mounts nothing."""

    bash_max_lines: int = 45
    """Shell output longer than this many lines is trimmed."""
    bash_max_chars: int = 2500
    """Shell output longer than this many characters is trimmed even if short in lines."""
    bash_head_lines: int = 20
    """Lines kept from the start of trimmed shell output."""
    bash_tail_lines: int = 15
    """Lines kept from the end of trimmed shell output (exit code lives here)."""
    keep_signal_lines: bool = True
    """Also keep middle lines that look like errors / failures / tracebacks."""
    max_signal_lines: int = 25
    """Upper bound on kept signal lines so a noisy log cannot defeat the trim."""

    grep_max_lines: int = 40
    """grep / glob results longer than this are cut to ``grep_head_lines``."""
    grep_head_lines: int = 30
    """Lines kept from grep / glob results when trimmed."""

    read_file_max_lines: int = 0
    """``read_file`` results longer than this are cut with a resume hint. ``0`` disables (default)."""

    exempt_source_prints: bool = True
    """Treat shell commands that print file contents or diffs like file reads (see ``source_print.py``).
    ``false`` restores the earlier behaviour (trimmed like any shell output)."""
    source_print_max_lines: int = 2000
    """Safety cap for exempt source prints: longer output keeps its first lines and spills the rest.
    ``0`` = never trim them."""

    spill_dir: str = ""
    """Directory for full spilled outputs. Empty = ``<agent workspace>/context_diet/<session>``
    (inside the Code-mode filesystem sandbox, outside the project)."""
    spill_max_bytes: int = 50 * 1024 * 1024
    """Per-session cap on spilled bytes; oldest files are deleted first. ``0`` disables the cap."""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> TrimConfig:
        data = raw if isinstance(raw, Mapping) else {}
        defaults = cls()
        return cls(
            enabled=_bool(data.get("enabled"), defaults.enabled),
            bash_max_lines=_int(data.get("bash_max_lines"), defaults.bash_max_lines, minimum=20),
            bash_max_chars=_int(data.get("bash_max_chars"), defaults.bash_max_chars, minimum=1000),
            bash_head_lines=_int(data.get("bash_head_lines"), defaults.bash_head_lines, minimum=5),
            bash_tail_lines=_int(data.get("bash_tail_lines"), defaults.bash_tail_lines, minimum=5),
            keep_signal_lines=_bool(data.get("keep_signal_lines"), defaults.keep_signal_lines),
            max_signal_lines=_int(data.get("max_signal_lines"), defaults.max_signal_lines, minimum=0),
            grep_max_lines=_int(data.get("grep_max_lines"), defaults.grep_max_lines, minimum=20),
            grep_head_lines=_int(data.get("grep_head_lines"), defaults.grep_head_lines, minimum=10),
            read_file_max_lines=_int(data.get("read_file_max_lines"), defaults.read_file_max_lines, minimum=0),
            exempt_source_prints=_bool(data.get("exempt_source_prints"), defaults.exempt_source_prints),
            source_print_max_lines=_int(
                data.get("source_print_max_lines"), defaults.source_print_max_lines, minimum=0
            ),
            spill_dir=str(data.get("spill_dir") or ""),
            spill_max_bytes=_int(data.get("spill_max_bytes"), defaults.spill_max_bytes, minimum=0),
        )


@dataclass(frozen=True)
class CompactionConfig:
    """Mid-turn compaction windows (``modes.code.context_diet.compaction``)."""

    enabled: bool = True
    """Master switch. ``false`` registers no processors."""

    shell_keep_last_k: int = 4
    """Newest shell / grep / glob results kept verbatim; older ones become one-line summaries. ``0`` disables."""
    read_keep_last_k: int = 0
    """Same window for ``read_file`` results. ``0`` disables (default, see module doc)."""
    min_offload_chars: int = 800
    """Results smaller than this are summarized in place without writing an offload file."""
    summary_chars: int = 240
    """Maximum characters of the one-line summary placeholder."""
    exempt_source_prints: bool = True
    """Keep shell results that print file contents or diffs out of the shell window (they are
    edit sources, like ``read_file``). ``false`` restores the earlier behaviour."""

    shell_tools: tuple[str, ...] = field(default=SHELL_TOOL_NAMES + GREP_TOOL_NAMES)
    read_tools: tuple[str, ...] = field(default=READ_TOOL_NAMES)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> CompactionConfig:
        data = raw if isinstance(raw, Mapping) else {}
        defaults = cls()
        return cls(
            enabled=_bool(data.get("enabled"), defaults.enabled),
            shell_keep_last_k=_int(data.get("shell_keep_last_k"), defaults.shell_keep_last_k, minimum=0),
            read_keep_last_k=_int(data.get("read_keep_last_k"), defaults.read_keep_last_k, minimum=0),
            min_offload_chars=_int(data.get("min_offload_chars"), defaults.min_offload_chars, minimum=0),
            summary_chars=_int(data.get("summary_chars"), defaults.summary_chars, minimum=80, maximum=2000),
            exempt_source_prints=_bool(data.get("exempt_source_prints"), defaults.exempt_source_prints),
        )


@dataclass(frozen=True)
class ContextDietConfig:
    """Root of ``modes.code.context_diet``."""

    trim: TrimConfig = field(default_factory=TrimConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    preset: str = DEFAULT_PRESET

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ContextDietConfig:
        data = raw if isinstance(raw, Mapping) else {}
        preset = resolve_preset(data.get("preset"))
        return cls(
            trim=TrimConfig.from_mapping(_merged("trim", preset, data.get("trim"))),
            compaction=CompactionConfig.from_mapping(_merged("compaction", preset, data.get("compaction"))),
            preset=preset,
        )


def load_context_diet_config(config_base: Mapping[str, Any] | None) -> ContextDietConfig:
    """Read ``modes.code.context_diet`` from a full config mapping (missing = defaults)."""
    modes = (config_base or {}).get("modes") if isinstance(config_base, Mapping) else None
    code = modes.get("code") if isinstance(modes, Mapping) else None
    raw = code.get("context_diet") if isinstance(code, Mapping) else None
    return ContextDietConfig.from_mapping(raw)


__all__ = [
    "DEFAULT_PRESET",
    "GREP_TOOL_NAMES",
    "PRESETS",
    "READ_TOOL_NAMES",
    "SHELL_TOOL_NAMES",
    "CompactionConfig",
    "ContextDietConfig",
    "TrimConfig",
    "load_context_diet_config",
    "resolve_preset",
]
