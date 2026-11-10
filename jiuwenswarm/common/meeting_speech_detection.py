"""Persisted settings for the backend Silero detector used by meetings."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping


SPEECH_START_DEFAULTS = {"meeting_vad_start_threshold": "0.5", "meeting_vad_start_frames": "5"}
SPEECH_START_ENV = {
    "meeting_vad_start_threshold": "MEETING_VAD_START_THRESHOLD",
    "meeting_vad_start_frames": "MEETING_VAD_START_FRAMES",
}


def validate_speech_start_setting(key: str, value: object) -> str:
    """Validate before saving; an empty value restores the default."""
    text = "" if value is None else str(value).strip()
    if not text:
        return SPEECH_START_DEFAULTS[key]
    try:
        number = float(text)
    except (TypeError, ValueError):
        number = math.nan
    if key == "meeting_vad_start_threshold":
        if math.isfinite(number) and 0 <= number <= 1:
            return str(number)
        raise ValueError("Speech-start confidence must be a number between 0 and 1")
    if math.isfinite(number) and number.is_integer() and 1 <= number <= 10:
        return str(int(number))
    raise ValueError("Start confirmation must be a whole number between 1 and 10 frames")


def speech_start_settings(environment: Mapping[str, str] | None = None) -> dict[str, float | int]:
    """Read at session start; malformed environment values use safe defaults."""
    environment = os.environ if environment is None else environment
    values = {}
    for key, env_key in SPEECH_START_ENV.items():
        try:
            values[key] = validate_speech_start_setting(key, environment.get(env_key))
        except ValueError:
            values[key] = SPEECH_START_DEFAULTS[key]
    return {
        "startThreshold": float(values["meeting_vad_start_threshold"]),
        "startFrames": int(values["meeting_vad_start_frames"]),
    }
