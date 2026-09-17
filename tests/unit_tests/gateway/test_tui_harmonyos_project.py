from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.gateway.channel_manager.tui import harmonyos_project
from jiuwenswarm.gateway.channel_manager.tui.harmonyos_project import (
    HarmonyOSProjectError,
    inspect_harmonyos_project,
    load_harmonyos_project_context,
    persist_harmonyos_project_context,
)


def _write_project(root: Path) -> None:
    (root / "AppScope").mkdir(parents=True)
    (root / "entry" / "src" / "main").mkdir(parents=True)
    (root / "feature" / "src" / "main").mkdir(parents=True)
    (root / "build-profile.json5").write_text(
        """
        {
          // JSON5 comments and unquoted keys are supported.
          app: {
            products: [{ name: 'default', }],
            buildModeSet: [{ name: 'debug' }, { name: 'release' }],
          },
          modules: [
            { name: 'entry', srcPath: './entry', targets: [{ name: 'default' }] },
            { name: 'feature', srcPath: './feature' },
          ],
        }
        """,
        encoding="utf-8",
    )
    (root / "oh-package.json5").write_text(
        "{ name: 'sample_app', version: '1.0.0' }", encoding="utf-8"
    )
    (root / "AppScope" / "app.json5").write_text(
        "{ app: { bundleName: 'com.example.sample' } }", encoding="utf-8"
    )
    (root / "entry" / "src" / "main" / "module.json5").write_text(
        """
        { module: {
          name: 'entry', type: 'entry', mainElement: 'EntryAbility',
          abilities: [{ name: 'EntryAbility', srcEntry: './ets/entryability/EntryAbility.ets' }],
        } }
        """,
        encoding="utf-8",
    )
    (root / "feature" / "src" / "main" / "module.json5").write_text(
        "{ module: { name: 'feature', type: 'har' } }", encoding="utf-8"
    )


def test_inspect_project_extracts_products_modules_and_ability(tmp_path: Path):
    project = tmp_path / "Sample"
    project.mkdir()
    _write_project(project)

    context = inspect_harmonyos_project(project)

    assert context["project"]["name"] == "sample_app"
    assert context["project"]["bundleName"] == "com.example.sample"
    assert context["defaultProduct"] == "default"
    assert context["buildModes"] == ["debug", "release"]
    assert [item["name"] for item in context["modules"]] == ["entry", "feature"]
    assert context["selected"] == {
        "product": "default",
        "module": "entry",
        "ability": "EntryAbility",
    }
    assert "entry/src/main/module.json5" in context["sourceFiles"]
    assert "AppScope/app.json5" in context["watchedFiles"]
    assert context["sourceFingerprint"].startswith("sha256:")


def test_context_round_trip_persists_tui_project_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)

    context = inspect_harmonyos_project(project)
    state_path = persist_harmonyos_project_context(context)
    loaded = load_harmonyos_project_context(project)
    assert state_path.is_file()
    assert loaded is not None
    assert loaded["project"]["path"] == str(project.resolve())
    assert loaded["selected"]["module"] == "entry"


def test_load_refreshes_context_when_descriptor_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)

    original = inspect_harmonyos_project(project)
    persist_harmonyos_project_context(original)
    descriptor = project / "entry" / "src" / "main" / "module.json5"
    descriptor.write_text(
        "{ module: { name: 'entry', type: 'entry', mainElement: 'NewAbility', "
        "abilities: [{ name: 'NewAbility' }] } }",
        encoding="utf-8",
    )

    refreshed = load_harmonyos_project_context(project)
    loaded_again = load_harmonyos_project_context(project)

    assert refreshed is not None
    assert refreshed["selected"]["ability"] == "NewAbility"
    assert refreshed["sourceFingerprint"] != original["sourceFingerprint"]
    assert loaded_again is not None
    assert loaded_again["selected"] == refreshed["selected"]
    assert loaded_again["sourceFingerprint"] == refreshed["sourceFingerprint"]


def test_inspect_fingerprint_uses_the_same_bytes_as_descriptor_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    descriptor = project / "entry" / "src" / "main" / "module.json5"
    descriptor.write_text(
        "{ module: { name: 'entry', type: 'entry', mainElement: 'OldAbility', "
        "abilities: [{ name: 'OldAbility' }] } }",
        encoding="utf-8",
    )
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)
    real_loads = harmonyos_project.json_repair_loads
    changed_during_parse = False

    def loads_and_change_descriptor(raw: str):
        nonlocal changed_during_parse
        parsed = real_loads(raw)
        if "OldAbility" in raw and not changed_during_parse:
            descriptor.write_text(
                "{ module: { name: 'entry', type: 'entry', mainElement: 'NewAbility', "
                "abilities: [{ name: 'NewAbility' }] } }",
                encoding="utf-8",
            )
            changed_during_parse = True
        return parsed

    monkeypatch.setattr(
        harmonyos_project, "json_repair_loads", loads_and_change_descriptor
    )

    inspected = inspect_harmonyos_project(project)
    persist_harmonyos_project_context(inspected)
    refreshed = load_harmonyos_project_context(project)
    loaded_again = load_harmonyos_project_context(project)

    assert inspected["selected"]["ability"] == "OldAbility"
    assert refreshed is not None
    assert refreshed["selected"]["ability"] == "NewAbility"
    assert refreshed["sourceFingerprint"] != inspected["sourceFingerprint"]
    assert loaded_again is not None
    assert loaded_again["selected"]["ability"] == "NewAbility"


def test_stale_refresh_failure_is_fail_closed_for_control_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)

    original = inspect_harmonyos_project(project)
    state_path = persist_harmonyos_project_context(original)
    before = state_path.read_text(encoding="utf-8")
    descriptor = project / "entry" / "src" / "main" / "module.json5"
    descriptor.unlink()
    try:
        descriptor.symlink_to(tmp_path / "missing-descriptor")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")

    control_result = load_harmonyos_project_context(project)

    assert control_result is None
    assert state_path.read_text(encoding="utf-8") == before


def test_stale_refresh_failure_allows_snapshot_fallback_without_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)

    original = inspect_harmonyos_project(project)
    persist_harmonyos_project_context(original)
    state_dir = state_root / "agent" / "workspace" / "harmonyos-projects"
    state_files = list(state_dir.glob("*.json"))
    assert len(state_files) == 1
    before = state_files[0].read_text(encoding="utf-8")

    descriptor = project / "entry" / "src" / "main" / "module.json5"
    descriptor.unlink()
    try:
        descriptor.symlink_to(tmp_path / "missing-descriptor")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")

    stale = load_harmonyos_project_context(project, allow_stale=True)
    assert stale is not None
    assert stale["stale"] is True
    assert stale["refreshError"]
    assert stale["selected"]["ability"] == "EntryAbility"
    assert state_files[0].read_text(encoding="utf-8") == before
    with pytest.raises(HarmonyOSProjectError, match="refusing to persist stale"):
        persist_harmonyos_project_context(stale)


def test_half_written_json5_does_not_overwrite_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """json_repair must not salvage truncated descriptors into incomplete state."""
    project = tmp_path / "Sample"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_project(project)
    monkeypatch.setattr(harmonyos_project, "get_user_workspace_dir", lambda: state_root)

    original = inspect_harmonyos_project(project)
    assert original["selected"]["ability"] == "EntryAbility"
    state_path = persist_harmonyos_project_context(original)
    before = state_path.read_text(encoding="utf-8")

    # Simulate an editor mid-write: truncated string + missing closers. json_repair
    # alone would invent a partial object (ability=None / truncated name).
    (project / "entry" / "src" / "main" / "module.json5").write_text(
        "{ module: { name: 'entry', type: 'entry', mainElement: 'EntryAbi",
        encoding="utf-8",
    )

    with pytest.raises(HarmonyOSProjectError, match="structurally incomplete"):
        inspect_harmonyos_project(project)

    control = load_harmonyos_project_context(project)
    prompt_ctx = load_harmonyos_project_context(project, allow_stale=True)
    stored = json.loads(state_path.read_text(encoding="utf-8"))

    assert control is None
    assert prompt_ctx is not None
    assert prompt_ctx["stale"] is True
    assert prompt_ctx["refreshError"]
    assert "structurally incomplete" in prompt_ctx["refreshError"]
    assert prompt_ctx["selected"]["ability"] == "EntryAbility"
    assert state_path.read_text(encoding="utf-8") == before
    assert stored["selected"]["ability"] == "EntryAbility"
    assert stored.get("stale") is not True


def test_valid_json5_quirks_still_parse(tmp_path: Path):
    project = tmp_path / "Sample"
    project.mkdir()
    _write_project(project)
    (project / "entry" / "src" / "main" / "module.json5").write_text(
        """
        {
          // comment and trailing comma are valid JSON5
          module: {
            name: 'entry',
            type: 'entry',
            mainElement: 'EntryAbility',
            abilities: [{ name: 'EntryAbility', },],
          },
        }
        """,
        encoding="utf-8",
    )

    context = inspect_harmonyos_project(project)

    assert context["selected"]["ability"] == "EntryAbility"


def test_assert_json5_structurally_complete_rejects_truncation():
    complete = (
        "{ module: { name: 'entry', type: 'entry', mainElement: 'EntryAbility', "
        "abilities: [{ name: 'EntryAbility' }] } }"
    )
    harmonyos_project._assert_json5_structurally_complete(complete, "ok.json5")

    with pytest.raises(HarmonyOSProjectError, match="truncated string"):
        harmonyos_project._assert_json5_structurally_complete(
            "{ module: { name: 'entry', mainElement: 'EntryAbi",
            "half.json5",
        )
    with pytest.raises(HarmonyOSProjectError, match="unclosed"):
        harmonyos_project._assert_json5_structurally_complete(
            "{ module: { name: 'entry', abilities: [{ name: 'EntryAbility' ",
            "half.json5",
        )


def test_inspect_rejects_module_path_escape(tmp_path: Path):
    project = tmp_path / "Sample"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "build-profile.json5").write_text(
        "{ modules: [{ name: 'escape', srcPath: '../outside' }] }",
        encoding="utf-8",
    )

    with pytest.raises(HarmonyOSProjectError, match="escapes project root"):
        inspect_harmonyos_project(project)


def test_inspect_rejects_symlinked_module(tmp_path: Path):
    project = tmp_path / "Sample"
    real_module = project / "real-entry"
    project.mkdir()
    real_module.mkdir()
    try:
        (project / "entry").symlink_to(real_module, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    (project / "build-profile.json5").write_text(
        "{ modules: [{ name: 'entry', srcPath: './entry' }] }", encoding="utf-8"
    )

    with pytest.raises(HarmonyOSProjectError, match="symbolic links are not allowed"):
        inspect_harmonyos_project(project)


def test_inspect_rejects_non_harmonyos_directory(tmp_path: Path):
    with pytest.raises(HarmonyOSProjectError, match="not a HarmonyOS project"):
        inspect_harmonyos_project(tmp_path)
