from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.common.media_capability_config import (
    media_capability_sidecar_paths,
    migrate_media_capability_switches,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


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
    }
    assert {key: environ[key] for key in updates} == updates
    assert env_path.read_text(encoding="utf-8") == (
        "KEEP=value\n"
        'VISION_ENABLED="true"\n'
        'AUDIO_ENABLED="false"\n'
        'VIDEO_ENABLED="false"\n'
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
    )
    env_path.write_text(original, encoding="utf-8")
    environ: dict[str, str] = {}

    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert environ == {
        "VISION_ENABLED": "false",
        "AUDIO_ENABLED": "true",
        "VIDEO_ENABLED": "false",
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
    }

    assert migrate_media_capability_switches(env_path, environ=environ) == {}
    assert not env_path.exists()


def test_sidecar_paths_name_the_files_the_migration_writes(tmp_path, monkeypatch) -> None:
    """The declared sidecar names must be the names that reach the disk.

    A name that the migration changes without a change to that function would
    leave an untracked file in every checkout again.
    """
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=value\n", encoding="utf-8")
    lock_path, staged_path = media_capability_sidecar_paths(env_path)
    replaced: list[Path] = []
    original_replace = Path.replace

    def _record_replace(self: Path, target):
        replaced.append(Path(self))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _record_replace)

    migrate_media_capability_switches(env_path, environ={})

    assert replaced == [staged_path]
    assert lock_path.is_file()
    assert set(tmp_path.iterdir()) == {env_path, lock_path}


def test_migration_sidecars_are_ignored_by_the_repository() -> None:
    """A checkout must stay clean after an import of an app module.

    app.py, app_agentserver.py and app_gateway.py each run this migration at
    import. With no data directory configured the config directory is the
    packaged resources directory. The migration then writes its sidecars into
    the checkout, and ``git add -A`` stages the lock file.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("the tests do not run from a git work tree")

    env_path = REPO_ROOT / "jiuwenswarm" / "resources" / ".env"
    for sidecar in media_capability_sidecar_paths(env_path):
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--", str(sidecar)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{sidecar.name} is not ignored (git exit {result.returncode})"
        )
