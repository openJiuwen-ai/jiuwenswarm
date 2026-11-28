"""Volcengine Seed-Realtime connection settings."""

import os

DOUBAO_PROXY_PATH = "/ws/video/doubao"
DOUBAO_UPSTREAM_URL = "wss://openspeech.bytedance.com/api/v3/realtime"
DOUBAO_MODEL = "1.3.0.0"
DOUBAO_VOICE = "zh_female_vv_jupiter_bigtts"


def api_key() -> str:
    key = os.environ.get("DOUBAO_REALTIME_API_KEY", "").strip()
    if not key:
        raise ValueError("请配置 DOUBAO_REALTIME_API_KEY")
    return key


def session_create() -> dict:
    """Minimal session for checking credentials and model access."""
    return {
        "type": "session.create",
        "session": {
            "model": model(),
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": {"format": {"type": "pcm_s16le", "rate": 24000}, "voice": voice()},
            },
        },
    }


def upstream_url() -> str:
    return os.environ.get("DOUBAO_REALTIME_URL", "").strip() or DOUBAO_UPSTREAM_URL


def model() -> str:
    return os.environ.get("DOUBAO_REALTIME_MODEL", "").strip() or DOUBAO_MODEL


def voice() -> str:
    return os.environ.get("DOUBAO_REALTIME_VOICE", "").strip() or DOUBAO_VOICE
