"""WEB-03 profiles list / preview / activate (Host disk scan + Core activate)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openjiuwen.harness.personal_context import PersonalContext

from jiuwenswarm.server.personal_context import host_api as host_module
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI
from jiuwenswarm.server.personal_context.profiles_ops import (
    activate_profile,
    get_profile_version,
    list_profile_versions,
)


def _pin_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "get_default_models", list)
    monkeypatch.setattr(
        host_module, "_global_embedding_values", lambda: (None, None, None)
    )


def _write_version(home: Path, job_id: str, *, published_at_ms: int = 1000) -> None:
    root = home / "im" / "profiles" / "versions" / job_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "persona.md").write_text(f"# persona {job_id}\n", encoding="utf-8")
    (root / "work.md").write_text(f"# work {job_id}\n", encoding="utf-8")
    (root / "meta.json").write_text(
        json.dumps(
            {
                "published_at_ms": published_at_ms,
                "source": "distill",
                "message_count": 5,
                "analyzer": "LlmAnalyzer",
            }
        ),
        encoding="utf-8",
    )


def _write_current(home: Path, job_id: str, *, published_at_ms: int = 1000) -> None:
    path = home / "im" / "profiles" / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "source": "distill",
                "published_at_ms": published_at_ms,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def profile_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PersonalContextHostAPI:
    _pin_models(monkeypatch)
    return PersonalContextHostAPI(home=tmp_path / "personal_context")


def test_list_profile_versions_marks_current(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_version(home, "job-a", published_at_ms=100)
    _write_version(home, "job-b", published_at_ms=200)
    _write_current(home, "job-a", published_at_ms=100)
    items = list_profile_versions(str(home))
    assert [item["job_id"] for item in items] == ["job-a", "job-b"]
    assert items[0]["is_current"] is True
    assert items[1]["is_current"] is False


@pytest.mark.asyncio
async def test_host_list_get_activate(profile_host: PersonalContextHostAPI) -> None:
    home = profile_host._home  # pylint: disable=protected-access
    _write_version(home, "job-old", published_at_ms=100)
    _write_version(home, "job-new", published_at_ms=200)
    _write_current(home, "job-old", published_at_ms=100)

    listed = await profile_host.list_profiles()
    assert len(listed["versions"]) == 2

    current = await profile_host.get_current_profile_payload()
    assert current["current"] is not None
    assert current["current"]["job_id"] == "job-old"

    preview = await profile_host.get_profile_version_payload("job-new")
    assert "persona job-new" in preview["persona_md"]
    assert preview["truncated"] is False

    activated = await profile_host.activate_profile_version_payload("job-new")
    assert activated["current"]["job_id"] == "job-new"
    assert activated["current"]["source"] == "manual_switch"
    assert any(
        item["job_id"] == "job-new" and item["is_current"]
        for item in activated["versions"]
    )

    current_after = await profile_host.get_current_profile_payload()
    assert current_after["current"]["job_id"] == "job-new"


@pytest.mark.asyncio
async def test_activate_missing_version_fails(
    profile_host: PersonalContextHostAPI,
) -> None:
    with pytest.raises(PersonalContext.Error):
        await profile_host.activate_profile_version_payload("missing-job")


@pytest.mark.parametrize(
    "job_id",
    [
        "../x",
        "..\\x",
        "a/b",
        "a\\b",
        "..",
        ".",
        "",
        "   ",
        "job/../other",
    ],
)
def test_unsafe_job_id_rejected_by_ops(tmp_path: Path, job_id: str) -> None:
    home = tmp_path / "home"
    _write_version(home, "legit-job", published_at_ms=1)
    # Plant a decoy outside versions that traversal would otherwise reach.
    decoy = home / "im" / "profiles" / "x"
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "persona.md").write_text("# decoy\n", encoding="utf-8")
    (decoy / "work.md").write_text("# decoy\n", encoding="utf-8")
    (decoy / "meta.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        get_profile_version(str(home), job_id)
    with pytest.raises(ValueError):
        activate_profile(str(home), job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id",
    ["../x", "..\\x", "a/b", "..", "job/../other"],
)
async def test_host_rejects_path_traversal_job_id(
    profile_host: PersonalContextHostAPI,
    job_id: str,
) -> None:
    home = profile_host._home  # pylint: disable=protected-access
    _write_version(home, "legit-job", published_at_ms=1)
    decoy = home / "im" / "profiles" / "x"
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "persona.md").write_text("# decoy\n", encoding="utf-8")
    (decoy / "work.md").write_text("# decoy\n", encoding="utf-8")
    (decoy / "meta.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PersonalContext.Error):
        await profile_host.get_profile_version_payload(job_id)
    with pytest.raises(PersonalContext.Error):
        await profile_host.activate_profile_version_payload(job_id)

    # Pointer must remain untouched when activate is rejected.
    assert not (home / "im" / "profiles" / "current.json").exists()


def test_list_skips_unsafe_directory_names(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_version(home, "legit-job", published_at_ms=1)
    # Create a nested illegal layout under versions (should be ignored).
    nested = home / "im" / "profiles" / "versions" / "nested" / "child"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "persona.md").write_text("# nested\n", encoding="utf-8")
    (nested / "work.md").write_text("# nested\n", encoding="utf-8")
    (nested / "meta.json").write_text("{}", encoding="utf-8")

    items = list_profile_versions(str(home))
    assert [item["job_id"] for item in items] == ["legit-job"]
