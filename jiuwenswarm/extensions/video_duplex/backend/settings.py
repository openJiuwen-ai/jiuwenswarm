"""Plugin-owned configuration persistence for the full-duplex application."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from jiuwenswarm.common.utils import get_env_file
from jiuwenswarm.dotenv_early import get_parsed_dotenv


SETTING_ENV_KEYS = {
    "joyai_api_base": "JOYAI_API_BASE",
    "joyai_api_key": "JOYAI_API_KEY",
    "joyai_model": "JOYAI_MODEL_NAME",
    "qwen_omni_realtime_url": "QWEN_OMNI_REALTIME_URL",
    "qwen_omni_api_key": "QWEN_OMNI_API_KEY",
    "qwen_omni_model": "QWEN_OMNI_MODEL_NAME",
    "qwen_omni_voice": "QWEN_OMNI_VOICE",
    "voice_protocol": "VOICE_PROTOCOL",
    "voice_asr_endpoint": "VOICE_ASR_ENDPOINT",
    "voice_tts_endpoint": "VOICE_TTS_ENDPOINT",
    "voice_api_key": "VOICE_API_KEY",
    "voice_asr_model": "VOICE_ASR_MODEL",
    "voice_tts_model": "VOICE_TTS_MODEL",
    "voice_tts_voice": "VOICE_TTS_VOICE",
}
SECRET_SETTINGS = {"joyai_api_key", "qwen_omni_api_key", "voice_api_key"}
DEFAULTS = {
    "joyai_api_base": "",
    "joyai_api_key": "",
    "joyai_model": "jdopensource/JoyAI-VL-Interaction",
    "qwen_omni_realtime_url": "",
    "qwen_omni_api_key": "",
    "qwen_omni_model": "qwen3.5-omni-flash-realtime",
    "qwen_omni_voice": "Cherry",
    "voice_protocol": "native_ws",
    "voice_asr_endpoint": "ws://127.0.0.1:8994/ws/asr",
    "voice_tts_endpoint": "ws://127.0.0.1:8992/ws/tts",
    "voice_api_key": "",
    "voice_asr_model": "",
    "voice_tts_model": "",
    "voice_tts_voice": "vivian",
}


def _active_env_file() -> Path:
    return get_parsed_dotenv() or get_env_file()


def _persist_env_updates(updates: Mapping[str, str]) -> None:
    if not updates:
        return
    path = _active_env_file()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = next(
            (candidate for candidate in remaining if stripped.startswith(candidate + "=")),
            None,
        )
        if key is None:
            output.append(line)
            continue
        value = remaining.pop(key)
        output.append(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" if value else f"{key}=\n"
        )
    for key, value in remaining.items():
        output.append(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" if value else f"{key}=\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    os.replace(temporary, path)


def _provider() -> str:
    mode = (os.getenv("VIDEO_LIVE_MODE") or "joyai").strip().casefold()
    realtime_provider = (os.getenv("VIDEO_REALTIME_PROVIDER") or "").strip().casefold()
    return "qwen_omni" if mode == "realtime" and realtime_provider == "qwen_omni" else "joyai"


def settings_payload(*, enabled: bool) -> dict[str, Any]:
    values = dict(DEFAULTS)
    values["video_live_provider"] = _provider()
    configured_secret_lengths: dict[str, int] = {}
    for key, env_key in SETTING_ENV_KEYS.items():
        raw = os.getenv(env_key)
        if raw is None or not raw.strip():
            continue
        if key in SECRET_SETTINGS:
            configured_secret_lengths[key] = len(raw.strip())
            values[key] = ""
        else:
            values[key] = raw.strip()
    return {
        "enabled": enabled,
        "values": values,
        "configured_secret_lengths": configured_secret_lengths,
        "restart_required": False,
    }


def _validated_values(values: Mapping[str, Any]) -> dict[str, str]:
    allowed = {"video_live_provider", *SETTING_ENV_KEYS}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("unknown full-duplex settings: " + ", ".join(sorted(unknown)))

    normalized: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str):
            raise ValueError(f"setting {key!r} must be a string")
        normalized[key] = value.strip()

    provider = normalized.get("video_live_provider", _provider())
    if provider not in {"joyai", "qwen_omni"}:
        raise ValueError("video_live_provider must be joyai or qwen_omni")
    protocol = normalized.get(
        "voice_protocol",
        (os.getenv("VOICE_PROTOCOL") or "native_ws").strip().casefold(),
    )
    if protocol not in {"native_ws", "openai_http"}:
        raise ValueError("voice_protocol must be native_ws or openai_http")
    for key, value in normalized.items():
        if len(value) > 4096:
            raise ValueError(f"setting {key!r} is too long")
    return normalized


def update_settings(values: Mapping[str, Any]) -> None:
    normalized = _validated_values(values)
    provider = normalized.pop("video_live_provider", _provider())
    updates = {
        "VIDEO_LIVE_MODE": "realtime" if provider == "qwen_omni" else "joyai",
        "VIDEO_REALTIME_PROVIDER": "qwen_omni" if provider == "qwen_omni" else "",
    }
    for key, value in normalized.items():
        if key in SECRET_SETTINGS and not value:
            continue
        updates[SETTING_ENV_KEYS[key]] = value
    os.environ.update(updates)
    _persist_env_updates(updates)


def set_enabled(enabled: bool) -> None:
    value = "true" if enabled else "false"
    os.environ["VIDEO_DUPLEX_ENABLED"] = value
    _persist_env_updates({"VIDEO_DUPLEX_ENABLED": value})
