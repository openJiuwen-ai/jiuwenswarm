from __future__ import annotations

from jiuwenswarm.common.media_capability_config import (
    migrate_media_capability_switches,
)


def test_startup_migration_persists_complete_and_incomplete_capabilities(
    tmp_path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=value\n", encoding="utf-8")
    environ = {
        "VISION_API_BASE": "https://vision.example/v1",
        "VISION_API_KEY": "secret",
        "VISION_MODEL_NAME": "vision-model",
        "VISION_PROVIDER": "OpenAI",
        "AUDIO_API_BASE": "https://audio.example/v1",
        "AUDIO_API_KEY": "secret",
        "AUDIO_MODEL_NAME": "audio-model",
        "AUDIO_PROVIDER": "",
    }

    updates = migrate_media_capability_switches(env_path, environ=environ)

    assert updates == {
        "VISION_ENABLED": "true",
        "AUDIO_ENABLED": "false",
        "VIDEO_ENABLED": "false",
        "IMAGE_GEN_ENABLED": "false",
    }
    assert {key: environ[key] for key in updates} == updates
    assert env_path.read_text(encoding="utf-8") == (
        "KEEP=value\n"
        'VISION_ENABLED="true"\n'
        'AUDIO_ENABLED="false"\n'
        'VIDEO_ENABLED="false"\n'
        'IMAGE_GEN_ENABLED="false"\n'
    )


def test_startup_migration_preserves_explicit_switches_and_is_idempotent(
    tmp_path,
) -> None:
    env_path = tmp_path / ".env"
    original = (
        "# explicit user choice\n"
        'VISION_ENABLED="false"\n'
        'AUDIO_ENABLED="true"\n'
        'VIDEO_ENABLED="false"\n'
        'IMAGE_GEN_ENABLED="true"\n'
    )
    env_path.write_text(original, encoding="utf-8")
    environ: dict[str, str] = {}

    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert environ == {
        "VISION_ENABLED": "false",
        "AUDIO_ENABLED": "true",
        "VIDEO_ENABLED": "false",
        "IMAGE_GEN_ENABLED": "true",
    }
    assert env_path.read_text(encoding="utf-8") == original


def test_startup_migration_respects_process_environment_without_persisting_it(
    tmp_path,
) -> None:
    env_path = tmp_path / ".env"
    environ = {
        "VISION_ENABLED": "false",
        "AUDIO_ENABLED": "true",
        "VIDEO_ENABLED": "false",
        "IMAGE_GEN_ENABLED": "false",
    }

    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert not env_path.exists()
