"""Shared TTS settings and provider-specific protocol validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from urllib.parse import urlsplit, urlunsplit

from jiuwenswarm.common.voice_matching import load_tts_voice_catalog

TTS_PROVIDERS = ("cosyvoice", "qwen_omni")


def tts_panel_defaults(environment: Mapping[str, str] | None = None) -> dict:
    environment = os.environ if environment is None else environment
    provider = environment.get("TTS_PROVIDER", "").strip() or "cosyvoice"
    catalog = load_tts_voice_catalog(provider)
    values = {"tts_provider": provider}
    for suffix, env_suffix, default in (
        ("api_base", "API_BASE", catalog.default_url),
        ("model", "MODEL_NAME", catalog.default_model),
        ("voice", "VOICE", catalog.default_voice),
    ):
        values[f"tts_{suffix}"] = environment.get(f"TTS_{env_suffix}", "").strip() or default
    values["tts_key_configured"] = bool(environment.get("TTS_API_KEY", "").strip())
    catalogs = {}
    for name in TTS_PROVIDERS:
        catalog = load_tts_voice_catalog(name)
        catalogs[name] = {
            "default_url": catalog.default_url, "default_model": catalog.default_model, "default_voice": catalog.default_voice,
            "models": list(dict.fromkeys(model for voice in catalog.voices for model in voice.models)),
            "voices": [{"id": voice.provider_voice_id, "name": voice.id, "models": list(voice.models)} for voice in catalog.voices],
        }
    values["tts_voice_catalogs"] = catalogs
    return values


@dataclass(frozen=True)
class TtsConfig:
    upstream_url: str
    api_key: str
    model: str
    voice: str
    provider: str = "cosyvoice"

    @classmethod
    def from_environment(cls) -> TtsConfig:
        values = tts_panel_defaults()
        provider = values["tts_provider"]
        if provider not in TTS_PROVIDERS:
            raise ValueError(f"Unsupported TTS provider: {provider}")
        key = os.environ.get("TTS_API_KEY", "").strip()
        if key:
            from jiuwenswarm.common.security.base_crypto import get_crypto_provider

            crypto = get_crypto_provider()
            if crypto is not None:
                key = crypto.decrypt(key)
        return cls(values["tts_api_base"], key, values["tts_model"], values["tts_voice"], provider)

    def validate(self, *, require_key: bool = True) -> None:
        catalog = load_tts_voice_catalog(self.provider)
        parts = urlsplit(self.upstream_url)
        schemes = {"ws", "wss"} if self.provider == "qwen_omni" else {"http", "https"}
        if parts.scheme not in schemes or not parts.netloc or parts.username or parts.password or parts.fragment:
            raise ValueError(f"Configure a valid {'WebSocket' if self.provider == 'qwen_omni' else 'HTTP(S)'} TTS URL")
        if self.provider == "cosyvoice" and (parts.query or parts.path.rstrip("/").endswith("/audio/transcriptions")):
            raise ValueError("TTS requires a base URL or /audio/speech, not the ASR endpoint")
        if require_key and not self.api_key:
            raise ValueError("Configure the separate TTS API key for the selected provider")
        catalog.compatible(self.model)
        self.validate_voice(self.voice)

    def validate_voice(self, voice: str) -> None:
        if not any(entry.provider_voice_id == voice for entry in load_tts_voice_catalog(self.provider).compatible(self.model)):
            raise ValueError("Select a voice compatible with the TTS provider and model")

    @property
    def endpoint(self) -> str:
        self.validate()
        if self.provider == "qwen_omni":
            return self.upstream_url
        parts = urlsplit(self.upstream_url)
        path = parts.path.rstrip("/")
        if not path.endswith("/audio/speech"):
            path += "/audio/speech"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def validate_tts_update(params: dict) -> None:
    """Validate the complete shared configuration before writing any changes."""
    values = {**tts_panel_defaults(), **{key: str(value).strip() for key, value in params.items() if key.startswith("tts_")}}
    if values["tts_provider"] not in TTS_PROVIDERS:
        raise ValueError("Unsupported TTS provider")
    if {"tts_provider", "tts_api_base", "tts_model", "tts_voice"} & params.keys():
        TtsConfig(
            values["tts_api_base"], "", values["tts_model"], values["tts_voice"], values["tts_provider"],
        ).validate(require_key=False)
