# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BGM cue sheet (story curve → timed prompt) and compose-time alignment to the cuts."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from jiuwenswarm.server.runtime.designer.music_align import (
    MusicFeatures,
    Piece,
    align_music,
    analyze,
    decode_mono,
    plan_alignment,
)
from jiuwenswarm.server.runtime.designer.music_cues import (
    bpm_range,
    build_cue_sheet,
    cue_prompt,
    pick_bpm,
    story_curve,
)

_SR = 22050


def test_story_curve_fills_a_single_climax() -> None:
    assert story_curve(["", "", "", ""]) == ["setup", "rise", "climax", "release"]
    assert story_curve(["climax", "", "climax", ""]) == ["rise", "rise", "climax", "release"]
    assert story_curve(["setup", "rise"]) == ["setup", "climax"]


def test_tempo_window_and_bar_grid() -> None:
    assert bpm_range({"tempo_bpm": "110-128"}) == (110, 128)
    assert bpm_range({"tempo_bpm": "around 120 BPM"}) == (108, 132)
    assert bpm_range({}) == (84, 132)
    # 8 s clips: 90 and 120 BPM both put every cut on a bar line; 120 is closer to the middle.
    assert pick_bpm([0, 8, 16, 24], 84, 132) == 120


def test_cue_prompt_merges_sections_and_marks_cuts() -> None:
    sheet = build_cue_sheet([8, 8, 8, 6], ["setup", "rise", "climax", "release"], tempo=(120, 120))
    assert sheet is not None
    assert sheet.total == 30 and sheet.climax_start == 16 and sheet.bpm == 120
    prompt = cue_prompt(sheet)
    assert prompt.startswith("120 BPM, 4/4. Total length 0:30; the piece ends at 0:30.")
    assert "[0:16 - 0:24] Peak: full arrangement, the biggest hit lands right at 0:16." in prompt
    assert "[0:24 - 0:30] Resolve: energy falls away, final chord rings out and ends at 0:30." in prompt
    assert prompt.endswith("Accent the downbeats at the picture cuts: 0:08, 0:16, 0:24.")

    merged = build_cue_sheet([4, 4, 8], ["setup", "setup", "climax"], tempo=(120, 120))
    assert merged is not None
    assert [(s.role, s.start, s.end) for s in merged.sections] == [("setup", 0, 8), ("climax", 8, 16)]


def _beats(end: float) -> tuple[float, ...]:
    return tuple(i * 0.5 for i in range(int(end / 0.5) + 1))


def test_plan_splices_a_late_peak_and_the_ending_on_beats() -> None:
    sheet = build_cue_sheet([8, 8, 8, 6], ["setup", "rise", "climax", "release"], tempo=(120, 120))
    assert sheet is not None
    features = MusicFeatures(beats=_beats(60), music_end=60.0, climax_hit=36.0, bpm=120.0)
    plan = plan_alignment(features, sheet)
    assert plan.natural_end
    assert plan.pieces == [
        Piece(0.0, 8.0, 0.0, 8.0),
        Piece(8.0, 12.0, 8.0, 12.0),
        Piece(32.0, 36.0, 12.0, 16.0, splice=True),
        Piece(36.0, 44.0, 16.0, 24.0),
        Piece(54.0, 60.0, 24.0, 30.0, splice=True),
    ]


def test_plan_stretches_when_the_music_is_close() -> None:
    sheet = build_cue_sheet([8, 8, 8, 6], ["setup", "rise", "climax", "release"], tempo=(120, 120))
    assert sheet is not None
    features = MusicFeatures(beats=_beats(31), music_end=31.0, climax_hit=16.5, bpm=120.0)
    plan = plan_alignment(features, sheet)
    assert plan.natural_end
    assert not any(piece.splice for piece in plan.pieces)
    assert plan.pieces[0].dst_start == 0 and plan.pieces[-1].dst_end == 30
    assert any(p.src_end == 16.5 and p.dst_end == 16 for p in plan.pieces)
    assert all(abs(p.rate - 1) <= 0.08 for p in plan.pieces)


def test_plan_trims_with_a_fade_when_nothing_fits() -> None:
    sheet = build_cue_sheet([10, 10], ["setup", "climax"], tempo=(120, 120))
    assert sheet is not None
    features = MusicFeatures(beats=(), music_end=60.0, climax_hit=None, bpm=120.0)
    plan = plan_alignment(features, sheet)
    assert not plan.natural_end
    assert plan.pieces == [Piece(0.0, 20.0, 0.0, 20.0)]


def test_plan_fades_a_cut_off_track_from_a_beat_two_bars_out() -> None:
    sheet = build_cue_sheet([10, 10, 10], ["setup", "climax", "release"], tempo=(120, 120))
    assert sheet is not None
    features = MusicFeatures(beats=_beats(45), music_end=45.0, climax_hit=None, bpm=120.0, truncated=True)
    plan = plan_alignment(features, sheet)
    assert not plan.natural_end
    assert plan.pieces[-1].dst_end == 30 and plan.pieces[-1].src_end == 30
    assert not any(piece.splice for piece in plan.pieces)
    assert plan.fade == 4.0


def _ffmpeg() -> str:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(_SR)
        out.writeframes(pcm.tobytes())


def _synthetic_track(seconds: float, peak_at: float, fade_from: float) -> np.ndarray:
    """120 BPM clicks over a pad: quiet until ``peak_at``, loud after, fading out from ``fade_from``."""
    t = np.arange(int(seconds * _SR)) / _SR
    rng = np.random.default_rng(7)
    level = np.where(t < peak_at, 0.12, 0.6) * np.clip((seconds - t) / (seconds - fade_from), 0, 1)
    pad = 0.4 * np.sin(2 * np.pi * 220 * t)
    clicks = np.zeros_like(t)
    burst = int(0.03 * _SR)
    for beat in np.arange(0, seconds, 0.5):
        span = clicks[int(beat * _SR) : int(beat * _SR) + burst]
        span += rng.uniform(-1, 1, span.size)
    return level * (pad + clicks)


def _db_between(samples: np.ndarray, start: float, end: float) -> float:
    chunk = samples[int(start * _SR) : int(end * _SR)]
    return float(20 * np.log10(np.sqrt(np.mean(chunk**2)) + 1e-9))


def test_analyze_tells_a_cut_off_track_from_an_ending_one() -> None:
    track = _synthetic_track(60.0, peak_at=36.0, fade_from=57.0)
    ended = analyze(track, bpm=120, expected_climax=None, film_end=60.0)
    cut_off = analyze(track[: 45 * _SR], bpm=120, expected_climax=None, film_end=45.0)
    assert not ended.truncated
    assert cut_off.truncated


def test_align_music_puts_the_peak_on_the_climax_cut(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    source = tmp_path / "bed.wav"
    _write_wav(source, _synthetic_track(60.0, peak_at=36.0, fade_from=57.0))
    sheet = build_cue_sheet([8, 8, 8, 6], ["setup", "rise", "climax", "release"], tempo=(120, 120))
    assert sheet is not None

    aligned, notes = align_music(ffmpeg, source, sheet, tmp_path / "bed_aligned")
    assert aligned is not None and aligned.suffix == ".m4a", notes
    out = decode_mono(ffmpeg, aligned)
    assert out is not None
    assert abs(out.size / _SR - 30.0) < 0.15
    before, after = _db_between(out, 14.0, 15.8), _db_between(out, 16.2, 18.0)
    assert after - before > 10, notes
    assert any("lands on 16.00s" in note for note in notes), notes
