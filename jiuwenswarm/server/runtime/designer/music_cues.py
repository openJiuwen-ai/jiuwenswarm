# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Film cue sheet for the BGM bed: music sections timed to the clip cuts.

Each clip carries one story-curve emotion (setup / rise / climax / release, see
``storyboard_clip_state.apply_story_curve``). Consecutive clips with the same
emotion merge into one music section, and the tempo is chosen so the cuts fall
on 4/4 bar lines. The sheet is written into the music prompt as a timestamped
arrangement and reused at compose to align the generated track to the real cuts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import emotion_label

SETUP = "setup"
RISE = "rise"
CLIMAX = "climax"
RELEASE = "release"

_DEFAULT_BPM_RANGE = (84, 132)
_BPM_SPREAD = 12
_MIN_BPM = 60
_MAX_BPM = 180
_BEATS_PER_BAR = 4

_SECTION_TEXT = {
    SETUP: "Intro: sparse, establish the main motif",
    RISE: "Build: layers enter one by one, energy rising",
    CLIMAX: "Peak: full arrangement, the biggest hit lands right at {start}",
    RELEASE: "Resolve: energy falls away, final chord rings out and ends at {end}",
}


@dataclass(frozen=True)
class CueSection:
    role: str
    start: float
    end: float


@dataclass(frozen=True)
class CueSheet:
    """Clip boundaries (0 … film end), the merged sections and the tempo."""

    cuts: tuple[float, ...]
    sections: tuple[CueSection, ...]
    bpm: int

    @property
    def total(self) -> float:
        return self.cuts[-1] if self.cuts else 0.0

    @property
    def climax_start(self) -> float | None:
        return next((s.start for s in self.sections if s.role == CLIMAX), None)

    @property
    def bar_seconds(self) -> float:
        return 60.0 * _BEATS_PER_BAR / self.bpm


def story_curve(emotions: list[str]) -> list[str]:
    """Normalize per-clip emotions to one curve with exactly one climax (when 2+ clips)."""
    rows = [emotion_label(e) for e in emotions]
    count = len(rows)
    climaxes = [i for i, e in enumerate(rows) if e == CLIMAX]
    for index in climaxes[:-1]:
        rows[index] = RISE
    if not climaxes and count >= 2:
        rows[count - 2 if count >= 3 else count - 1] = CLIMAX
    climax = next((i for i, e in enumerate(rows) if e == CLIMAX), None)
    for index, emotion in enumerate(rows):
        if emotion:
            continue
        if index == 0:
            rows[index] = SETUP
        elif climax is not None and index > climax:
            rows[index] = RELEASE
        else:
            rows[index] = RISE
    return rows


def storyboard_emotions(graph: dict[str, Any]) -> dict[int, str]:
    """``clip_index`` → story-curve emotion from the storyboard analysis."""
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    out: dict[int, str] = {}
    for clip in analysis.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        try:
            index = int(clip.get("clip_index") or 0)
        except (TypeError, ValueError):
            continue
        out[index] = str(clip.get("emotion") or clip.get("beat") or "")
    return out


def bpm_range(bgm_lock: dict[str, Any] | None) -> tuple[int, int]:
    """Tempo window from ``bgm_lock.tempo_bpm`` ("120" or "110-130"), else a broad default."""
    raw = str((bgm_lock or {}).get("tempo_bpm") or "")
    numbers = [int(n) for n in re.findall(r"\d{2,3}", raw) if _MIN_BPM <= int(n) <= _MAX_BPM]
    if len(numbers) >= 2:
        return min(numbers[:2]), max(numbers[:2])
    if numbers:
        return max(_MIN_BPM, numbers[0] - _BPM_SPREAD), min(_MAX_BPM, numbers[0] + _BPM_SPREAD)
    return _DEFAULT_BPM_RANGE


def pick_bpm(cuts: list[float], low: int, high: int) -> int:
    """Integer tempo in [low, high] whose 4/4 bar grid best covers the inner cuts."""
    inner = [c for c in cuts[1:] if c > 0]
    middle = (low + high) / 2

    def _miss(bpm: int) -> tuple[float, float]:
        bar = 60.0 * _BEATS_PER_BAR / bpm
        error = 0.0
        for cut in inner:
            offset = cut % bar
            error += min(offset, bar - offset) / bar
        return round(error, 6), abs(bpm - middle)

    return min(range(low, high + 1), key=_miss)


def build_cue_sheet(
    durations: list[float],
    emotions: list[str],
    *,
    tempo: tuple[int, int] = _DEFAULT_BPM_RANGE,
) -> CueSheet | None:
    lengths = [max(0.0, float(d)) for d in durations]
    if not lengths or sum(lengths) <= 0:
        return None
    curve = story_curve([*emotions, *[""] * (len(lengths) - len(emotions))][: len(lengths)])
    cuts = [0.0]
    for length in lengths:
        cuts.append(round(cuts[-1] + length, 3))
    sections: list[CueSection] = []
    for index, role in enumerate(curve):
        start, end = cuts[index], cuts[index + 1]
        if sections and sections[-1].role == role:
            sections[-1] = CueSection(role, sections[-1].start, end)
        else:
            sections.append(CueSection(role, start, end))
    return CueSheet(cuts=tuple(cuts), sections=tuple(sections), bpm=pick_bpm(cuts, *tempo))


def _clock(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def cue_prompt(sheet: CueSheet) -> str:
    """Timestamped arrangement for the music model."""
    lines = [
        f"{sheet.bpm} BPM, 4/4. Total length {_clock(sheet.total)}; the piece ends at {_clock(sheet.total)}.",
        "Arrangement timed to the picture edit:",
    ]
    for section in sheet.sections:
        text = _SECTION_TEXT[section.role].format(start=_clock(section.start), end=_clock(section.end))
        lines.append(f"[{_clock(section.start)} - {_clock(section.end)}] {text}.")
    cut_marks = ", ".join(_clock(c) for c in sheet.cuts[1:-1])
    if cut_marks:
        lines.append(f"Accent the downbeats at the picture cuts: {cut_marks}.")
    return "\n".join(lines)
