# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Align a generated BGM bed to the real picture cuts (numpy + ffmpeg).

Music models only roughly follow a timestamped arrangement, and Lyria returns
about a minute for any short request, so compose re-times the track:

1. Decode to mono PCM; compute an onset envelope and a loudness curve.
2. Fit a steady beat grid near the cue sheet tempo.
3. Anchor the biggest loudness rise to the climax cut and the music's own ending
   to the film end: stretched when the rate stays within ``MAX_STRETCH``,
   otherwise spliced on a beat (two bars before a late peak; at the release cut
   into the ending tail) with a short crossfade.
4. Snap the other cuts to the nearest beat when that keeps rates in range.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jiuwenswarm.server.runtime.designer.ffmpeg_runner import (
    ENCODE_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    FfmpegCancelled,
    run_ffmpeg,
)
from jiuwenswarm.server.runtime.designer.music_cues import RELEASE, CueSheet

logger = logging.getLogger(__name__)

MAX_STRETCH = 0.08
_SAMPLE_RATE = 22050
_FRAME = 1024
_HOP = 512
_TEMPO_SEARCH = 0.10
_CLIMAX_WINDOW_SECONDS = 2.0
_CLIMAX_ATTACK_SECONDS = 0.5
_CLIMAX_TIE_DB = 1.0
_MIN_CLIMAX_RISE_DB = 3.0
_END_FLOOR_DB = 30.0
_TRUNCATED_TAIL_SECONDS = 1.0
_TRUNCATED_WITHIN_DB = 12.0
_CROSSFADE_SECONDS = 0.4
_MIN_TAIL_SECONDS = 4.0
_NATURAL_FADE_SECONDS = 0.3
_TRIM_FADE_SECONDS = 1.5
_ENDING_FADE_BEATS = 8
_ENDING_FADE_RANGE = (1.5, 5.0)


@dataclass(frozen=True)
class MusicFeatures:
    """What the aligner needs from the generated track, in source seconds."""

    beats: tuple[float, ...]
    music_end: float
    climax_hit: float | None
    bpm: float
    # Still loud in its last second: cut off by a length budget rather than ended.
    truncated: bool = False


@dataclass(frozen=True)
class Piece:
    """Source span ``[src_start, src_end]`` rendered onto ``[dst_start, dst_end]``."""

    src_start: float
    src_end: float
    dst_start: float
    dst_end: float
    splice: bool = False

    @property
    def rate(self) -> float:
        return (self.src_end - self.src_start) / max(1e-6, self.dst_end - self.dst_start)


@dataclass
class AlignPlan:
    pieces: list[Piece]
    natural_end: bool
    notes: list[str] = field(default_factory=list)
    fade: float = _TRIM_FADE_SECONDS


def _frames(samples: np.ndarray) -> np.ndarray:
    if samples.size < _FRAME:
        samples = np.pad(samples, (0, _FRAME - samples.size))
    return np.lib.stride_tricks.sliding_window_view(samples, _FRAME)[::_HOP]


def _seconds_to_frame(seconds: float) -> int:
    return int(round(seconds * _SAMPLE_RATE / _HOP))


def _frame_to_seconds(frame: float) -> float:
    return float(frame) * _HOP / _SAMPLE_RATE


def _beat_grid(onset: np.ndarray, bpm: float) -> tuple[np.ndarray, float]:
    """Steady beat times near ``bpm``: the fractional period and phase whose comb collects most onset."""
    fps = _SAMPLE_RATE / _HOP
    # Integer-lag autocorrelation cannot tell 115 from 120 BPM at this hop; a comb
    # with a fractional period over the whole track can.
    envelope = np.convolve(onset, np.ones(3) / 3, mode="same")
    best_period, best_phase, best_score = fps * 60.0 / bpm, 0.0, -np.inf
    for candidate in np.arange(bpm * (1 - _TEMPO_SEARCH), bpm * (1 + _TEMPO_SEARCH) + 0.1, 0.1):
        period = fps * 60.0 / candidate
        count = int(envelope.size / period)
        if count < 2:
            continue
        phases = np.arange(0.0, period, 1.0)
        idx = np.round(phases[:, None] + period * np.arange(count)[None, :]).astype(int)
        scores = envelope[np.minimum(idx, envelope.size - 1)].mean(axis=1)
        at = int(np.argmax(scores))
        if scores[at] > best_score:
            best_period, best_phase, best_score = period, float(phases[at]), float(scores[at])
    beats = best_phase + best_period * np.arange(int(envelope.size / best_period) + 1)
    beats = beats[beats < envelope.size]
    return np.array([_frame_to_seconds(b) for b in beats]), fps * 60.0 / best_period


def analyze(samples: np.ndarray, *, bpm: float, expected_climax: float | None, film_end: float) -> MusicFeatures:
    frames = _frames(samples.astype(np.float32)) * np.hanning(_FRAME).astype(np.float32)
    magnitude = np.log1p(10.0 * np.abs(np.fft.rfft(frames, axis=1)))
    onset = np.concatenate([[0.0], np.maximum(0.0, np.diff(magnitude, axis=0)).sum(axis=1)])
    beats, tempo = _beat_grid(onset, bpm)

    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    smooth = np.convolve(rms, np.ones(9) / 9, mode="same")
    db = 20 * np.log10(smooth + 1e-9)
    loud = np.nonzero(db > db.max() - _END_FLOOR_DB)[0]
    music_end = _frame_to_seconds(loud[-1] + 1) if loud.size else _frame_to_seconds(db.size)
    tail = db[-max(1, _seconds_to_frame(_TRUNCATED_TAIL_SECONDS)):]
    truncated = bool(tail.mean() > np.percentile(db, 90) - _TRUNCATED_WITHIN_DB)

    climax_hit = None
    if expected_climax is not None and film_end > 0:
        scaled = expected_climax * music_end / film_end
        low, high = 0.6 * min(expected_climax, scaled), 1.4 * max(expected_climax, scaled)
        before = _seconds_to_frame(_CLIMAX_WINDOW_SECONDS)
        after = _seconds_to_frame(_CLIMAX_ATTACK_SECONDS)
        rises: list[tuple[float, float]] = []
        for beat in beats:
            at = _seconds_to_frame(beat)
            if low <= beat <= high and at - before >= 0 and at + after <= db.size:
                rises.append((float(beat), float(db[at:at + after].mean() - db[at - before:at].mean())))
        best_rise = max((rise for _, rise in rises), default=0.0)
        if best_rise >= _MIN_CLIMAX_RISE_DB:
            # Near-equal rises: trust the beat closest to where the prompt asked for the hit.
            close = [beat for beat, rise in rises if rise >= best_rise - _CLIMAX_TIE_DB]
            climax_hit = min(close, key=lambda beat: abs(beat - expected_climax))
    return MusicFeatures(
        beats=tuple(float(b) for b in beats),
        music_end=music_end,
        climax_hit=climax_hit,
        bpm=tempo,
        truncated=truncated,
    )


def _nearest(values: tuple[float, ...], target: float) -> float:
    return min(values, key=lambda v: abs(v - target)) if values else target


def _in_range(rate: float) -> bool:
    return abs(rate - 1.0) <= MAX_STRETCH


Anchor = tuple[float, float]


def plan_alignment(features: MusicFeatures, sheet: CueSheet) -> AlignPlan:
    """Map source time onto film time; each segment after the first starts with a crossfaded splice."""
    film_end = sheet.total
    notes: list[str] = []
    segments: list[list[Anchor]] = [[(0.0, 0.0)]]
    climax = sheet.climax_start
    hit = features.climax_hit
    if climax is not None and hit is not None and climax > 0:
        if _in_range(hit / climax):
            segments[-1].append((hit, climax))
            notes.append(f"climax hit {hit:.2f}s -> {climax:.2f}s")
        else:
            jump = _splice_climax(features, climax, hit)
            if jump is None:
                notes.append(f"climax hit {hit:.2f}s too far from {climax:.2f}s; not anchored")
            else:
                segments[-1].append(jump[0])
                segments.append(list(jump[1:]))
                notes.append(
                    f"spliced {jump[1][0]:.2f}s in at {jump[0][1]:.2f}s so the hit {hit:.2f}s lands on {climax:.2f}s"
                )

    src_a, dst_a = segments[-1][-1]
    remaining_src = features.music_end - src_a
    remaining_dst = film_end - dst_a
    natural_end = False
    fade = _TRIM_FADE_SECONDS
    if features.truncated and remaining_dst > 0 and remaining_src >= remaining_dst:
        segments[-1].append((src_a + remaining_dst, film_end))
        fade = _ending_fade(features, src_a, dst_a, film_end)
        notes.append(f"music was cut off by its length budget; faded out over the last {fade:.2f}s from a beat")
    elif remaining_dst > 0 and _in_range(remaining_src / remaining_dst):
        segments[-1].append((features.music_end, film_end))
        natural_end = True
        notes.append(f"music end {features.music_end:.2f}s stretched onto {film_end:.2f}s")
    elif remaining_dst > 0 and remaining_src > remaining_dst:
        tail = _splice_tail(features, sheet, src_a, dst_a)
        if tail is None:
            segments[-1].append((src_a + remaining_dst, film_end))
            notes.append("ending could not be spliced on a beat; trimmed with a fade")
        else:
            body_end, tail_start, tail_end = tail
            if body_end[1] > dst_a:
                segments[-1].append(body_end)
            segments.append([tail_start, tail_end])
            natural_end = True
            notes.append(f"spliced ending tail {tail_start[0]:.2f}-{tail_end[0]:.2f}s at {tail_start[1]:.2f}s")
    elif remaining_dst > 0:
        segments[-1].append((features.music_end, dst_a + remaining_src))
        notes.append("music shorter than the film; padded with a fade")

    pieces: list[Piece] = []
    for index, segment in enumerate(segments):
        anchors = _snap_cuts(segment, features, sheet)
        for j, ((s0, d0), (s1, d1)) in enumerate(zip(anchors, anchors[1:])):
            pieces.append(Piece(s0, s1, d0, d1, splice=index > 0 and j == 0))
    if natural_end:
        fade = _NATURAL_FADE_SECONDS
    return AlignPlan(pieces=pieces, natural_end=natural_end, notes=notes, fade=fade)


def _ending_fade(features: MusicFeatures, src_a: float, dst_a: float, film_end: float) -> float:
    """Fade length so the fade-out starts on a beat about two bars before the film end."""
    low, high = _ENDING_FADE_RANGE
    target = film_end - _ENDING_FADE_BEATS * 60.0 / features.bpm
    starts = [b - src_a + dst_a for b in features.beats if low <= film_end - (b - src_a + dst_a) <= high]
    if not starts:
        return min(high, max(low, film_end - target))
    return film_end - min(starts, key=lambda start: abs(start - target))


def _splice_climax(features: MusicFeatures, climax: float, hit: float) -> tuple[Anchor, Anchor, Anchor] | None:
    """Jump forward on a beat two bars before the cut so a late peak lands on the climax cut."""
    lead = 8 * 60.0 / features.bpm
    cut_dst = climax - lead
    if hit <= climax or cut_dst < lead or not features.beats:
        return None
    cut_src = _nearest(features.beats, cut_dst)
    if not _in_range(cut_src / cut_dst) or hit - lead <= cut_src + lead:
        return None
    return (cut_src, cut_dst), (hit - lead, cut_dst), (hit, climax)


def _splice_tail(
    features: MusicFeatures, sheet: CueSheet, src_a: float, dst_a: float
) -> tuple[Anchor, Anchor, Anchor] | None:
    """Body end, tail start and tail end: cut from the release cut into the music's own ending."""
    film_end = sheet.total
    release = next((s.start for s in sheet.sections if s.role == RELEASE and s.start > dst_a), None)
    splice_dst = release if release is not None else film_end - _MIN_TAIL_SECONDS
    splice_dst = max(dst_a, min(splice_dst, film_end - _MIN_TAIL_SECONDS))
    tail_len = film_end - splice_dst
    if tail_len <= 0 or not features.beats:
        return None
    tail_src = _nearest(features.beats, features.music_end - tail_len)
    body_src = _nearest(features.beats, src_a + (splice_dst - dst_a)) if splice_dst > dst_a else src_a
    if tail_src <= body_src or not _in_range((features.music_end - tail_src) / tail_len):
        return None
    if splice_dst > dst_a and not _in_range((body_src - src_a) / (splice_dst - dst_a)):
        return None
    return (body_src, splice_dst), (tail_src, splice_dst), (features.music_end, film_end)


def _snap_cuts(anchors: list[Anchor], features: MusicFeatures, sheet: CueSheet) -> list[Anchor]:
    """Add a beat anchor at each free cut when both neighbouring rates stay in range."""
    out = sorted(anchors, key=lambda a: a[1])
    if not features.beats:
        return out
    taken = {round(d, 3) for _, d in out}
    for cut in sheet.cuts[1:-1]:
        if round(cut, 3) in taken:
            continue
        left = max((a for a in out if a[1] < cut), key=lambda a: a[1], default=None)
        right = min((a for a in out if a[1] > cut), key=lambda a: a[1], default=None)
        if left is None or right is None:
            continue
        src_guess = left[0] + (cut - left[1]) * (right[0] - left[0]) / (right[1] - left[1])
        beat = _nearest(features.beats, src_guess)
        if not left[0] < beat < right[0]:
            continue
        if _in_range((beat - left[0]) / (cut - left[1])) and _in_range((right[0] - beat) / (right[1] - cut)):
            out.append((beat, cut))
            out.sort(key=lambda a: a[1])
            taken.add(round(cut, 3))
    return out


def _run(ffmpeg: str, args: list[str], *, timeout: float, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    cmd = [ffmpeg, *args]
    try:
        return run_ffmpeg(cmd, timeout=timeout, binary=binary)
    except FfmpegCancelled:
        raise
    except RuntimeError as exc:
        empty = b"" if binary else ""
        return subprocess.CompletedProcess(cmd, -1, empty, str(exc))


def decode_mono(ffmpeg: str, path: Path) -> np.ndarray | None:
    proc = _run(
        ffmpeg,
        ["-v", "error", "-i", str(path), "-ac", "1", "-ar", str(_SAMPLE_RATE), "-f", "f32le", "-"],
        timeout=PROBE_TIMEOUT_S,
        binary=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def track_seconds(ffmpeg: str, path: Path) -> float | None:
    samples = decode_mono(ffmpeg, path)
    return None if samples is None else samples.size / _SAMPLE_RATE


def _atempo(rate: float) -> str:
    return f"atempo={rate:.5f}" if abs(rate - 1.0) > 1e-4 else "anull"


def render(ffmpeg: str, source: Path, plan: AlignPlan, film_end: float, dest: Path) -> Path | None:
    filters: list[str] = []
    labels: list[str] = []
    for index, piece in enumerate(plan.pieces):
        # The crossfade overlaps the join, so the tail starts that much earlier in source.
        extra = _CROSSFADE_SECONDS * piece.rate if piece.splice and index else 0.0
        start = max(0.0, piece.src_start - extra)
        filters.append(
            f"[0:a]atrim={start:.4f}:{piece.src_end:.4f},asetpts=PTS-STARTPTS,"
            f"{_atempo(piece.rate)}[p{index}]"
        )
        labels.append(f"p{index}")
    if not labels:
        return None
    current = labels[0]
    for index, piece in enumerate(plan.pieces[1:], start=1):
        out = f"j{index}"
        if piece.splice:
            filters.append(f"[{current}][{labels[index]}]acrossfade=d={_CROSSFADE_SECONDS}:c1=tri:c2=tri[{out}]")
        else:
            filters.append(f"[{current}][{labels[index]}]concat=n=2:v=0:a=1[{out}]")
        current = out
    fade = plan.fade
    filters.append(
        f"[{current}]apad=whole_dur={film_end:.3f},atrim=0:{film_end:.3f},"
        f"afade=t=out:st={max(0.0, film_end - fade):.3f}:d={fade}[out]"
    )
    dest = Path(dest).with_suffix(".m4a")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ffmpeg,
        ["-y", "-i", str(source), "-filter_complex", ";".join(filters), "-map", "[out]",
         "-c:a", "aac", "-b:a", "192k", "-t", f"{film_end:.3f}", str(dest)],
        timeout=ENCODE_TIMEOUT_S,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 512:
        logger.warning("BGM align render failed: %s", (proc.stderr or "")[-800:])
        dest.unlink(missing_ok=True)
        return None
    return dest.resolve()


def align_music(ffmpeg: str, source: Path, sheet: CueSheet, dest: Path) -> tuple[Path | None, list[str]]:
    """Aligned copy of ``source`` (or None to keep the raw track) plus what was done."""
    samples = decode_mono(ffmpeg, source)
    if samples is None or samples.size < _FRAME * 4:
        return None, ["could not decode the BGM track"]
    features = analyze(samples, bpm=sheet.bpm, expected_climax=sheet.climax_start, film_end=sheet.total)
    plan = plan_alignment(features, sheet)
    notes = [
        f"tempo {features.bpm:.1f} BPM (asked {sheet.bpm}), source {samples.size / _SAMPLE_RATE:.1f}s",
        *plan.notes,
    ]
    aligned = render(ffmpeg, source, plan, sheet.total, dest)
    return aligned, notes
