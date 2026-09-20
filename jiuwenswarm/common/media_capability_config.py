# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Startup migration for explicit multimodal capability switches."""

from __future__ import annotations

import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

import portalocker
from dotenv import dotenv_values


MEDIA_CAPABILITY_ENV_FIELDS = {
    "VISION_ENABLED": (
        "VISION_API_BASE",
        "VISION_API_KEY",
        "VISION_MODEL_NAME",
        "VISION_PROVIDER",
    ),
    "AUDIO_ENABLED": (
        "AUDIO_API_BASE",
        "AUDIO_API_KEY",
        "AUDIO_MODEL_NAME",
        "AUDIO_PROVIDER",
    ),
    "VIDEO_ENABLED": (
        "VIDEO_API_BASE",
        "VIDEO_API_KEY",
        "VIDEO_MODEL_NAME",
        "VIDEO_PROVIDER",
    ),
}


def _configured_value(
    key: str,
    environ: MutableMapping[str, str],
    persisted: dict[str, str | None],
) -> str:
    if key in environ:
        return str(environ[key]).strip()
    return str(persisted.get(key) or "").strip()


def _append_env_updates_atomic(env_path: Path, updates: dict[str, str]) -> None:
    """Append missing keys while the caller holds the migration file lock."""
    original = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    content = original
    if content and not content.endswith(("\n", "\r")):
        content += "\n"
    content += "".join(f'{key}="{value}"\n' for key, value in updates.items())

    env_path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = stat.S_IMODE(env_path.stat().st_mode) if env_path.exists() else None
    staged_path = env_path.with_name(f".{env_path.name}.media-capability.pending")
    try:
        staged_path.write_text(content, encoding="utf-8")
        if original_mode is not None:
            staged_path.chmod(original_mode)
        staged_path.replace(env_path)
    finally:
        staged_path.unlink(missing_ok=True)


def migrate_media_capability_switches(
    env_path: str | Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    lock_timeout: float = 10.0,
) -> dict[str, str]:
    """Persist missing capability switches once, before runtime construction.

    Existing explicit switches are never overwritten. A legacy capability is
    enabled only when its API base, API key, model name, and provider are all
    non-empty; otherwise the new explicit switch is persisted as disabled.
    """
    target = Path(env_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime_env = os.environ if environ is None else environ
    lock_path = target.with_name(f"{target.name}.media-capability.lock")

    with portalocker.Lock(str(lock_path), timeout=lock_timeout):
        persisted = dict(dotenv_values(target)) if target.is_file() else {}
        updates: dict[str, str] = {}
        for enabled_key, config_fields in MEDIA_CAPABILITY_ENV_FIELDS.items():
            if enabled_key in runtime_env:
                continue
            if enabled_key in persisted:
                runtime_env[enabled_key] = str(persisted[enabled_key] or "")
                continue
            updates[enabled_key] = (
                "true"
                if all(
                    _configured_value(field, runtime_env, persisted)
                    for field in config_fields
                )
                else "false"
            )

        if updates:
            _append_env_updates_atomic(target, updates)
            runtime_env.update(updates)
        return updates
