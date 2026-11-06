"""Model-compatible voice matching, with no Gateway credentials or SDK imports.

The host owns one registry per native Team/session lifetime and may reuse it
across meetings on that same runtime. Assign using canonical member IDs, never
display names. Bindings are intentionally retained when members leave, because
their queued speech may still need them. Process-restart persistence is deferred.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from threading import RLock

import yaml


def _nonblank(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    return value.strip()


def _language(value: str) -> str:
    language = _nonblank(value, "Language").replace("_", "-").lower()
    if language in {"zh", "zh-cn", "zh-hans"}:
        return "zh-CN"
    if language == "en" or language.startswith("en-"):
        return "en"
    return language


@dataclass(frozen=True)
class VoiceDefinition:
    id: str
    provider_voice_id: str
    models: tuple[str, ...]
    languages: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class VoiceCatalog:
    version: str
    source: str
    voices: tuple[VoiceDefinition, ...]
    default_model: str = ""
    default_voice: str = ""
    default_url: str = ""

    def __post_init__(self) -> None:
        _nonblank(self.version, "Catalog version")
        _nonblank(self.source, "Catalog source")
        if not self.voices:
            raise ValueError("Voice catalog must contain at least one voice")
        ids, provider_ids = set(), set()
        for voice in self.voices:
            _nonblank(voice.id, "Voice ID")
            _nonblank(voice.provider_voice_id, "Provider voice ID")
            if voice.id in ids or voice.provider_voice_id in provider_ids:
                raise ValueError("Duplicate voice IDs in catalog")
            if not voice.models or not voice.languages:
                raise ValueError("Each voice requires explicit models and languages")
            for model in voice.models:
                _nonblank(model, "Voice model")
            for language in voice.languages:
                _nonblank(language, "Voice language")
            ids.add(voice.id)
            provider_ids.add(voice.provider_voice_id)

    def compatible(self, model: str, language: str | None = None) -> tuple[VoiceDefinition, ...]:
        voices = tuple(voice for voice in self.voices if model in voice.models)
        if not voices:
            raise ValueError(f"Unsupported voice model: {model}")
        if language is not None:
            selected = _language(language)
            voices = tuple(voice for voice in voices if selected in voice.languages)
        if not voices:
            raise ValueError(f"No compatible voices for model {model} and language {language}")
        return voices


def _load_voice_catalog(resource) -> VoiceCatalog:
    try:
        data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid voice catalog YAML") from exc
    if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
        raise ValueError("Invalid voice catalog")
    voices = []
    for entry in data["voices"]:
        if (
            not isinstance(entry, dict) or not isinstance(entry.get("models"), list)
            or not isinstance(entry.get("languages"), list)
            or not isinstance(entry.get("description", ""), str)
        ):
            raise ValueError("Invalid voice catalog entry")
        voices.append(VoiceDefinition(
            _nonblank(entry.get("id"), "Voice ID"),
            _nonblank(entry.get("provider_voice_id"), "Provider voice ID"),
            tuple(_nonblank(value, "Voice model") for value in entry["models"]),
            tuple(_language(value) for value in entry["languages"]),
            entry.get("description", ""),
        ))
    return VoiceCatalog(
        data.get("version"), data.get("source"), tuple(voices),
        data.get("default_model", ""), data.get("default_voice", ""), data.get("default_url", ""),
    )


def load_qwen_voice_catalog(path: Path | None = None) -> VoiceCatalog:
    return _load_voice_catalog(path if path is not None else files("jiuwenswarm").joinpath("resources/voices/qwen_omni.yaml"))


def load_tts_voice_catalog(provider: str) -> VoiceCatalog:
    if provider not in {"cosyvoice", "qwen_omni"}:
        raise ValueError(f"Unsupported TTS provider: {provider}")
    return _load_voice_catalog(files("jiuwenswarm").joinpath(f"resources/voices/{provider}.yaml"))


def load_voice_catalog(model: str) -> VoiceCatalog:
    for provider in ("cosyvoice", "qwen_omni"):
        catalog = load_tts_voice_catalog(provider)
        if any(model in voice.models for voice in catalog.voices):
            return catalog
    raise ValueError(f"Unsupported voice model: {model}")


@dataclass(frozen=True)
class VoiceAssignment:
    session_id: str
    team_name: str
    member_id: str
    voice_id: str
    provider_voice_id: str
    model: str
    catalog_version: str
    languages: tuple[str, ...]


class MemberVoiceRegistry:
    """Atomic deterministic assignments within one trusted native Team scope.

    ``assign_voice`` admits a member from the host's roster/creation path.
    ``get_voice`` only resolves existing bindings, including departed members
    with queued speech. Selection: explicit preference, unused, then least-used;
    catalog order breaks ties. Existing objects never change voice implicitly.
    """

    def __init__(
        self, session_id: str, team_name: str, model: str, *,
        catalog: VoiceCatalog | None = None,
    ) -> None:
        self._session_id = _nonblank(session_id, "Session ID")
        self._team_name = _nonblank(team_name, "Team name")
        self._model = _nonblank(model, "Model")
        self._catalog = catalog or load_voice_catalog(model)
        self._catalog.compatible(self.model)
        self._bindings: dict[str, VoiceAssignment] = {}
        self._lock = RLock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def team_name(self) -> str:
        return self._team_name

    @property
    def model(self) -> str:
        return self._model

    def assign_voice(
        self, member_id: str, preference: str | None = None, *, language: str | None = None,
    ) -> VoiceAssignment:
        member_id = _nonblank(member_id, "Member ID")
        if preference is not None:
            preference = _nonblank(preference, "Voice preference")
        with self._lock:
            existing = self._bindings.get(member_id)
            if existing is not None:
                if preference is not None and preference not in {existing.voice_id, existing.provider_voice_id}:
                    raise ValueError("Member already has a stable voice assignment")
                if language is not None and _language(language) not in existing.languages:
                    raise ValueError("Assigned voice does not support the requested language")
                return existing
            candidates = self._catalog.compatible(self.model, language)
            if preference is not None:
                candidates = tuple(voice for voice in candidates if preference in {voice.id, voice.provider_voice_id})
                if len(candidates) != 1:
                    raise ValueError("Explicit voice preference is unknown or incompatible")
            usage = Counter(binding.provider_voice_id for binding in self._bindings.values())
            voice = min(candidates, key=lambda candidate: usage[candidate.provider_voice_id])
            assignment = VoiceAssignment(
                self.session_id, self.team_name, member_id, voice.id,
                voice.provider_voice_id, self.model, self._catalog.version, voice.languages,
            )
            self._bindings[member_id] = assignment
            return assignment

    def assign_roster(self, member_ids: Iterable[str], *, leader_id: str) -> None:
        members = {_nonblank(member, "Member ID") for member in member_ids}
        if leader_id not in members:
            raise ValueError("Leader must belong to the canonical roster")
        with self._lock:
            for member in [leader_id, *sorted(members - {leader_id})]:
                self.assign_voice(member)

    def get_voice(self, member_id: str) -> VoiceAssignment:
        with self._lock:
            assignment = self._bindings.get(_nonblank(member_id, "Member ID"))
            if assignment is None:
                raise ValueError("Unknown member voice; assign through the trusted member path first")
            return assignment

    def update_catalog(self, catalog: VoiceCatalog) -> None:
        """Only future assignments see catalog edits; queued bindings stay valid."""
        catalog.compatible(self.model)
        with self._lock:
            self._catalog = catalog
