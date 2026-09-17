import importlib
import json
from pathlib import Path

import pytest


MODULE = "jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.artifact_naming"


def _api():
    return importlib.import_module(MODULE)


def _provenance(
    document_id: str = "document-a",
    *,
    version_number: int | None = 1,
    version_base_stem: str | None = "report",
    rewrite_history: object = None,
) -> dict:
    provenance = {"document_id": document_id, "rewrite_history": [] if rewrite_history is None else rewrite_history}
    if version_number is not None:
        provenance["version_number"] = version_number
    if version_base_stem is not None:
        provenance["version_base_stem"] = version_base_stem
    return provenance


def _write_sidecar(root: Path, name: str, provenance: dict, markdown: str = "# Report\n") -> Path:
    report = root / f"{name}.md"
    report.write_text(markdown, encoding="utf-8")
    report.with_suffix(".provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False), encoding="utf-8"
    )
    return report


def test_public_dataclasses_are_frozen_slots_and_initial_name_normalizes_terminal_version():
    api = _api()

    version = api.initial_version("Quarterly / report-v17.md")

    assert version == api.ArtifactVersion("Quarterly_report", 1)
    assert getattr(api.ArtifactVersion, "__slots__", None) == ("base_stem", "version_number")
    with pytest.raises(AttributeError):
        version.base_stem = "other"


def test_artifact_paths_is_frozen(tmp_path):
    api = _api()
    paths = api.allocate_initial_paths(tmp_path, "report.md")

    with pytest.raises(AttributeError):
        paths.markdown_path = Path("other.md")


@pytest.mark.parametrize(
    ("provenance", "code"),
    [
        (_provenance(version_number=None), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_base_stem=None), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_number=True), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_number=1.0), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_number=0), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_number=10**100), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_base_stem="unsafe/name"), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_base_stem=""), "ARTIFACT_NAMING_INVALID"),
        (_provenance(version_base_stem="report-v2"), "ARTIFACT_NAMING_INVALID"),
    ],
)
def test_explicit_provenance_rejects_partial_or_invalid_version_metadata(tmp_path, provenance, code):
    api = _api()

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.resolve_artifact_version(provenance, tmp_path / "report.md", "# Report\n")

    assert caught.value.code == code


def test_explicit_provenance_is_authoritative_over_filename_and_markdown(tmp_path):
    api = _api()

    version = api.resolve_artifact_version(
        _provenance(version_number=7, version_base_stem="official"),
        tmp_path / "legacy-name-v2.md",
        "# Different heading\n",
    )

    assert version == api.ArtifactVersion("official", 7)


@pytest.mark.parametrize(
    ("markdown", "report_name", "expected_base"),
    [
        ("# First heading!\n# Later heading\n", "report.md", "First_heading"),
        ("## Not an H1\n", "legacy report-v9.md", "legacy_report"),
        ("# !!!\n", "report.md", "report"),
        ("# !!!\n", "-v9.md", "深度研究报告"),
    ],
)
def test_legacy_resolution_uses_first_h1_then_report_stem_then_default(
    tmp_path, markdown, report_name, expected_base
):
    api = _api()

    version = api.resolve_artifact_version(
        {"rewrite_history": []}, tmp_path / report_name, markdown
    )

    assert version == api.ArtifactVersion(expected_base, 1)


@pytest.mark.parametrize("history", [None, "not-a-list", ["not-a-mapping"]])
def test_legacy_resolution_rejects_malformed_rewrite_history(tmp_path, history):
    api = _api()

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.resolve_artifact_version(
            {"rewrite_history": history}, tmp_path / "report.md", "# Report\n"
        )

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_legacy_resolution_derives_logical_version_from_rewrite_history(tmp_path):
    api = _api()

    version = api.resolve_artifact_version(
        {"rewrite_history": [{"revision_id": "1"}, {"revision_id": "2"}]},
        tmp_path / "old.md",
        "# Legacy title\n",
    )

    assert version == api.ArtifactVersion("Legacy_title", 3)


def test_legacy_resolution_rejects_derived_version_above_limit(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "MAX_VERSION_NUMBER", 1)

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.resolve_artifact_version(
            {"rewrite_history": [{}]}, tmp_path / "report.md", "# Report\n"
        )

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_initial_paths_adds_same_title_suffix_for_markdown_and_sidecar_collisions(tmp_path):
    api = _api()

    first = api.allocate_initial_paths(tmp_path, "report.md")
    first.markdown_path.write_text("report", encoding="utf-8")
    second = api.allocate_initial_paths(tmp_path, "report.md")
    second.provenance_path.write_text("{}", encoding="utf-8")
    third = api.allocate_initial_paths(tmp_path, "report.md")
    third.final_result_path.write_text("{}", encoding="utf-8")
    fourth = api.allocate_initial_paths(tmp_path, "report.md")

    assert first.markdown_path.name == "report-v1.md"
    assert second.markdown_path.name == "report-2-v1.md"
    assert third.markdown_path.name == "report-3-v1.md"
    assert fourth.markdown_path.name == "report-4-v1.md"
    assert second.provenance_path.name == "report-2-v1.provenance.json"
    assert second.final_result_path.name == "report-2-v1.final-result.json"
    assert getattr(api.ArtifactPaths, "__slots__", None) == (
        "version", "markdown_path", "provenance_path", "final_result_path"
    )


@pytest.mark.parametrize("requested_name", ["report", "report.md", "report.html"])
def test_allocate_initial_html_path_strips_known_suffixes(requested_name, tmp_path):
    api = _api()

    allocated = api.allocate_initial_html_path(tmp_path, requested_name)

    assert allocated.name == "report-v1.html"


def test_allocate_initial_html_path_checks_html_collision_without_markdown_reservation(
    tmp_path,
):
    api = _api()
    (tmp_path / "report-v1.md").write_text("professional", encoding="utf-8")

    first = api.allocate_initial_html_path(tmp_path, "report.md")
    first.write_text("brief", encoding="utf-8")
    second = api.allocate_initial_html_path(tmp_path, "report.html")

    assert first.name == "report-v1.html"
    assert second.name == "report-2-v1.html"
    assert not (tmp_path / "report-2-v1.md").exists()


@pytest.mark.parametrize("path_field", ["markdown_path", "provenance_path", "final_result_path"])
def test_allocate_initial_paths_treats_hidden_atomic_target_as_collision(tmp_path, path_field):
    api = _api()
    first = api.allocate_initial_paths(tmp_path, "report.md")
    target = getattr(first, path_field)
    (tmp_path / f".{target.name}.tmp").write_text("in progress", encoding="utf-8")

    allocated = api.allocate_initial_paths(tmp_path, "report.md")

    assert allocated.markdown_path.name == "report-2-v1.md"


def test_allocate_initial_paths_caps_base_before_appending_same_title_ordinal(tmp_path):
    api = _api()
    requested_name = "x" * 120
    first = api.allocate_initial_paths(tmp_path, requested_name)
    first.markdown_path.write_text("report", encoding="utf-8")

    allocated = api.allocate_initial_paths(tmp_path, requested_name)

    assert allocated.version.base_stem == f"{'x' * 118}-2"
    assert len(allocated.version.base_stem) == 120


def test_allocate_initial_paths_bounds_complete_utf8_filenames_for_cjk_title(tmp_path):
    api = _api()
    requested_name = "深" * 100
    first = api.allocate_initial_paths(tmp_path, requested_name)
    first.markdown_path.write_text("report", encoding="utf-8")

    allocated = api.allocate_initial_paths(tmp_path, requested_name)

    assert allocated.version.base_stem.endswith("-2")
    assert all(
        len(path.name.encode("utf-8")) <= getattr(api, "MAX_FILENAME_BYTES", 240)
        for path in (
            allocated.markdown_path,
            allocated.provenance_path,
            allocated.final_result_path,
        )
    )


@pytest.mark.parametrize("dangling", [False, True])
def test_allocate_initial_paths_treats_direct_and_dangling_candidate_symlinks_as_occupied(
    tmp_path, dangling
):
    api = _api()
    first = api.allocate_initial_paths(tmp_path, "report.md")
    target = tmp_path / "target.md"
    if not dangling:
        target.write_text("target", encoding="utf-8")
    first.markdown_path.symlink_to(target)

    allocated = api.allocate_initial_paths(tmp_path, "report.md")

    assert allocated.markdown_path.name == "report-2-v1.md"


def test_allocate_initial_paths_fails_with_domain_error_after_bounded_attempts(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "MAX_ALLOCATION_ATTEMPTS", 2)
    (tmp_path / "report-v1.md").write_text("occupied", encoding="utf-8")
    (tmp_path / "report-2-v1.md").write_text("occupied", encoding="utf-8")

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_initial_paths(tmp_path, "report.md")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_initial_paths_fails_at_total_directory_entry_bound(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "MAX_DIRECTORY_ENTRIES", 3, raising=False)
    for index in range(10):
        (tmp_path / f"unrelated-{index}").write_text("noise", encoding="utf-8")

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_initial_paths(tmp_path, "report.md")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_next_paths_uses_global_same_document_max_across_branches(tmp_path):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    _write_sidecar(tmp_path, "report-v2", _provenance(version_number=2))

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 3)
    assert allocated.markdown_path.name == "report-v3.md"


def test_allocate_next_paths_ignores_unrelated_document_sidecars(tmp_path):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    _write_sidecar(tmp_path, "other-v99", _provenance("document-b", version_number=99))

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 2)


@pytest.mark.parametrize(
    "invalid_provenance",
    [
        _provenance(version_number=True),
        _provenance(version_base_stem=None),
    ],
)
def test_allocate_next_paths_fails_closed_for_invalid_same_document_sibling(
    tmp_path, invalid_provenance
):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    _write_sidecar(tmp_path, "report-v2", invalid_provenance)

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_next_paths_counts_legacy_sibling_and_advances_past_existing_targets(tmp_path):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    _write_sidecar(
        tmp_path,
        "legacy-v5",
        {"document_id": "document-a", "rewrite_history": [{}, {}, {}]},
        "# Old report\n",
    )
    (tmp_path / "report-v5.md").write_text("reserved", encoding="utf-8")

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 6)
    assert allocated.markdown_path.name == "report-v6.md"


def test_allocate_next_paths_advances_when_candidate_sidecar_already_exists(tmp_path):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    (tmp_path / "report-v2.provenance.json").write_text("{}", encoding="utf-8")

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 3)


@pytest.mark.parametrize(
    ("base_stem", "occupied_name"),
    [
        ("Report", "REPORT-V2.MD"),
        ("Report", "REPORT-V2.PROVENANCE.JSON"),
        ("Caf\u00e9", "Cafe\u0301-v2.md"),
        ("Caf\u00e9", "Cafe\u0301-v2.provenance.json"),
    ],
)
def test_allocate_next_paths_conservatively_skips_case_and_unicode_variants(
    tmp_path, base_stem, occupied_name
):
    api = _api()
    parent = _write_sidecar(
        tmp_path,
        f"{base_stem}-v1",
        _provenance(version_number=1, version_base_stem=base_stem),
    )
    (tmp_path / occupied_name).write_text("occupied", encoding="utf-8")

    allocated = api.allocate_next_paths(
        parent,
        _provenance(version_number=1, version_base_stem=base_stem),
        "# Report\n",
    )

    assert allocated.version == api.ArtifactVersion(base_stem, 3)


@pytest.mark.parametrize(
    "hidden_name",
    [".REPORT-V2.MD.tmp", ".Cafe\u0301-v2.provenance.json.pending"],
)
def test_allocate_next_paths_conservatively_skips_hidden_target_variants(
    tmp_path, hidden_name
):
    api = _api()
    base_stem = "Report" if "REPORT" in hidden_name else "Caf\u00e9"
    parent = _write_sidecar(
        tmp_path,
        f"{base_stem}-v1",
        _provenance(version_number=1, version_base_stem=base_stem),
    )
    (tmp_path / hidden_name).write_text("in progress", encoding="utf-8")

    allocated = api.allocate_next_paths(
        parent,
        _provenance(version_number=1, version_base_stem=base_stem),
        "# Report\n",
    )

    assert allocated.version == api.ArtifactVersion(base_stem, 3)


def test_allocate_next_paths_rejects_non_mapping_parent_provenance(tmp_path):
    api = _api()
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_next_paths(parent, [], "# Report\n")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_next_paths_skips_symlink_sidecars_without_following_them(tmp_path):
    api = _api()
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_provenance(version_number=99)), encoding="utf-8")
    (tmp_path / "linked.provenance.json").symlink_to(target)

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 2)


def test_allocate_next_paths_ignores_malformed_unrelated_sidecar(tmp_path):
    api = _api()
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    (tmp_path / "unrelated.provenance.json").write_text(
        "{malformed", encoding="utf-8"
    )

    allocated = api.allocate_next_paths(
        parent, _provenance(version_number=1), "# Report\n"
    )

    assert allocated.version == api.ArtifactVersion("report", 2)


def test_allocate_next_paths_ignores_unreadable_unrelated_sidecar(
    tmp_path, monkeypatch
):
    api = _api()
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    unreadable = tmp_path / "unrelated.provenance.json"
    unreadable.write_text(
        json.dumps(_provenance("document-b", version_number=99)),
        encoding="utf-8",
    )
    real_open = api.os.open

    def deny_unrelated_sidecar(path, *args, **kwargs):
        if Path(path) == unreadable:
            raise PermissionError("unreadable sidecar")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(api.os, "open", deny_unrelated_sidecar)

    allocated = api.allocate_next_paths(
        parent, _provenance(version_number=1), "# Report\n"
    )

    assert allocated.version == api.ArtifactVersion("report", 2)


def test_allocate_next_paths_rejects_same_document_legacy_symlink_markdown(tmp_path):
    api = _api()
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    legacy = tmp_path / "legacy-v2.md"
    legacy.symlink_to(tmp_path / "outside.md")
    legacy.with_suffix(".provenance.json").write_text(
        json.dumps({"document_id": "document-a", "rewrite_history": [{}]}),
        encoding="utf-8",
    )

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"


def test_allocate_next_paths_ignores_oversized_unrelated_sidecar(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "MAX_PROVENANCE_BYTES", 32)
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    (tmp_path / "unrelated.provenance.json").write_text(
        json.dumps(_provenance("document-b", version_number=99)) + " " * 64,
        encoding="utf-8",
    )

    allocated = api.allocate_next_paths(
        parent, _provenance(version_number=1), "# Report\n"
    )

    assert allocated.version == api.ArtifactVersion("report", 2)


def test_allocate_next_paths_does_not_read_markdown_for_explicit_sibling(tmp_path, monkeypatch):
    api = _api()
    parent = _write_sidecar(tmp_path, "report-v1", _provenance(version_number=1))
    _write_sidecar(tmp_path, "report-v2", _provenance(version_number=2))

    def reject_markdown(*_args, **_kwargs):
        raise AssertionError("explicit version must not read markdown")

    monkeypatch.setattr(api, "_sibling_markdown", reject_markdown)

    allocated = api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert allocated.version == api.ArtifactVersion("report", 3)


def test_allocate_next_paths_enforces_bounded_sidecar_scan(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, "MAX_SIDECARS_SCANNED", 1)
    parent = tmp_path / "report-v1.md"
    parent.write_text("# Report\n", encoding="utf-8")
    for name in ("unrelated-a", "unrelated-b"):
        (tmp_path / f"{name}.provenance.json").write_text(
            json.dumps(_provenance("document-b", version_number=1)), encoding="utf-8"
        )

    with pytest.raises(api.ArtifactNamingError) as caught:
        api.allocate_next_paths(parent, _provenance(version_number=1), "# Report\n")

    assert caught.value.code == "ARTIFACT_NAMING_INVALID"
