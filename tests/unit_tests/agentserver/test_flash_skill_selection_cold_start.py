"""Cold catalog preparation preserves retrieval and native-loading evidence."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import os

import pytest
import yaml

from jiuwenswarm.agents.harness.flash.skill_selection import catalog, execution, service
from jiuwenswarm.agents.harness.flash.skill_selection.config import SelectionSettings
from jiuwenswarm.agents.harness.flash.skill_selection.pipeline import SelectionPipeline
from jiuwenswarm.agents.harness.flash.skill_selection.presentation import candidate_view


def write_skill(root, name, *, reference=True, create_reference=True):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    metadata = {
        "name": name,
        "description": "Create PowerPoint presentation slides and export PPTX.",
        "description_zh": "制作演示文稿；不能编辑 XLSX。",
        "description_en": "Create editable slides for a science festival.",
    }
    body = "# Presentation workflow\nCreate PPTX from the supplied content.\n"
    if reference:
        body += "Read [format policy](references/formats.md).\n"
    (directory / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n" + body,
        encoding="utf-8",
    )
    if reference and create_reference:
        path = directory / "references" / "formats.md"
        path.parent.mkdir()
        path.write_text("Cannot create PDF. Outputs: PPTX only.\n", encoding="utf-8")
    return directory


def legacy_fields(document):
    """New cache bookkeeping must not change any existing public output field."""
    return tuple(getattr(document, name) for name in (
        "id", "name", "directory", "text", "fingerprint", "description", "capability_record",
    ))


def prepare(roots, *, cache=None, lightweight=False):
    return catalog.read_catalog(
        roots, text_max_chars=2000, text_mode="capability", cache=cache,
        lightweight=lightweight,
    )


def pipeline(documents):
    result = SelectionPipeline(SelectionSettings(enabled=True))
    result.prepare(documents)
    return result


def rank(index, allowed):
    return index.search("Create science festival presentation slides", ("PPTX", "演示文稿"),
                        allowed_ids=frozenset(allowed))


def observe_reads(monkeypatch):
    reads = Counter()
    read_text = Path.read_text

    def recorded(path, *args, **kwargs):
        reads[os.path.normcase(os.path.abspath(path))] += 1
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recorded)
    return reads


def read_count(reads, path):
    return reads[os.path.normcase(os.path.abspath(path))]


def changed_text(path, text):
    previous = path.stat()
    path.write_text(text, encoding="utf-8")
    # Explicitly advance the version; do not depend on filesystem clock resolution.
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))


def replace_preserving_mtime(path, text):
    previous = path.stat()
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_text(text, encoding="utf-8")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)


def make_symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"This host does not permit creating test symlinks: {exc}")


def test_cold_catalog_defers_references_and_contracts_until_allowed_top_five(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    directories = [write_skill(root, f"slides-{number:02d}") for number in range(9)]
    expected = prepare([root])
    original_contract = catalog.declared_contract
    contracts = []

    def counted_contract(*args, **kwargs):
        contracts.append(args)
        return original_contract(*args, **kwargs)

    monkeypatch.setattr(catalog, "declared_contract", counted_contract)
    reads = observe_reads(monkeypatch)
    cache = catalog.CatalogCache()
    lightweight = prepare([root], cache=cache, lightweight=True)

    assert contracts == []
    assert all(read_count(reads, directory / "references" / "formats.md") == 0
               for directory in directories)
    assert [(d.id, d.name, d.text, d.description) for d in lightweight] == [
        (d.id, d.name, d.text, d.description) for d in expected
    ]
    assert all(not document.fingerprint and not document.capability_record
               for document in lightweight)
    assert all(not document.root_raw for document in lightweight)

    # Exclude the first sorted skill, so a pre-filter top K would be incorrect.
    allowed = {document.id for document in expected[1:]}
    eager_result = rank(pipeline(expected), allowed)
    light_result = rank(pipeline(lightweight), allowed)
    assert [(d.id, score) for d, score in light_result.candidates] == [
        (d.id, score) for d, score in eager_result.candidates
    ]
    assert len(light_result.candidates) == 5
    hydrated = [catalog.hydrate_document(document, cache=cache)
                for document, _ in light_result.candidates]
    assert [legacy_fields(d) for d in hydrated] == [
        legacy_fields(d) for d, _ in eager_result.candidates
    ]
    assert [candidate_view(d) for d in hydrated] == [
        candidate_view(d) for d, _ in eager_result.candidates
    ]
    assert len(contracts) == 5
    selected_ids = {document.id for document in hydrated}
    for directory in directories:
        assert read_count(reads, directory / "references" / "formats.md") == (
            1 if catalog.directory_id(directory) in selected_ids else 0
        )

    counts = reads.copy()
    for document, _ in light_result.candidates:
        catalog.hydrate_document(document, cache=cache)
    assert len(contracts) == 5
    assert reads == counts


def test_unchanged_catalog_reuses_documents_and_only_changed_root_is_reparsed(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    directories = [write_skill(root, f"slides-{number}") for number in range(3)]
    cache = catalog.CatalogCache()
    first = prepare([root], cache=cache, lightweight=True)
    reads = observe_reads(monkeypatch)
    second = prepare([root], cache=cache, lightweight=True)
    assert [(d.id, d.text) for d in first] == [(d.id, d.text) for d in second]
    assert all(read_count(reads, directory / "SKILL.md") == 0 for directory in directories)

    changed = directories[1] / "SKILL.md"
    raw = changed.read_text(encoding="utf-8")
    changed_text(changed, raw.replace("science festival", "school exhibition"))
    reads.clear()
    third = prepare([root], cache=cache, lightweight=True)
    assert [read_count(reads, directory / "SKILL.md") for directory in directories] == [0, 1, 0]
    assert third[0].text == first[0].text and third[2].text == first[2].text
    assert third[1].text != first[1].text
    eager = prepare([root])
    assert [legacy_fields(catalog.hydrate_document(d, cache=cache)) for d in third] == [
        legacy_fields(d) for d in eager
    ]


@pytest.mark.parametrize("change", ["modify", "create", "replace_same_mtime"])
def test_reference_changes_refresh_details_without_changing_index_text(tmp_path, change):
    root = tmp_path / "skills"
    directory = write_skill(root, "slides", create_reference=change != "create")
    cache = catalog.CatalogCache()
    document, = prepare([root], cache=cache, lightweight=True)
    before = catalog.hydrate_document(document, cache=cache)
    reference = directory / "references" / "formats.md"
    new_policy = "Cannot create PNG. Outputs: PPTX only.\n"
    if change == "create":
        reference.parent.mkdir()
        reference.write_text(new_policy, encoding="utf-8")
    elif change == "replace_same_mtime":
        replace_preserving_mtime(reference, new_policy)
    else:
        changed_text(reference, new_policy)

    after = catalog.hydrate_document(document, cache=cache)
    eager, = prepare([root])
    assert after.text == before.text == document.text
    assert after.fingerprint != before.fingerprint
    assert after.capability_record != before.capability_record
    assert legacy_fields(after) == legacy_fields(eager)


@pytest.mark.parametrize("change", ["modify", "replace_same_mtime"])
def test_changed_root_cannot_mix_old_ranking_with_new_candidate_details(tmp_path, change):
    root = tmp_path / "skills"
    directory = write_skill(root, "slides")
    cache = catalog.CatalogCache()
    stale, = prepare([root], cache=cache, lightweight=True)
    # Exercise the rich-cache hit path too: it must still detect later changes.
    catalog.hydrate_document(stale, cache=cache)
    path = directory / "SKILL.md"
    updated = path.read_text(encoding="utf-8").replace("science festival", "science workshop")
    if change == "replace_same_mtime":
        replace_preserving_mtime(path, updated)
    else:
        changed_text(path, updated)
    with pytest.raises(catalog.CatalogChanged):
        catalog.hydrate_document(stale, cache=cache)

    fresh, = prepare([root], cache=cache, lightweight=True)
    eager, = prepare([root])
    assert legacy_fields(catalog.hydrate_document(fresh, cache=cache)) == legacy_fields(eager)


def test_deleted_root_does_not_return_cached_candidate(tmp_path):
    root = tmp_path / "skills"
    directory = write_skill(root, "slides")
    cache = catalog.CatalogCache()
    document, = prepare([root], cache=cache, lightweight=True)
    catalog.hydrate_document(document, cache=cache)
    (directory / "SKILL.md").unlink()
    with pytest.raises(catalog.CatalogChanged):
        catalog.hydrate_document(document, cache=cache)
    assert prepare([root], cache=cache, lightweight=True) == ()


def test_directory_links_cannot_import_a_skill_from_outside_root(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = write_skill(tmp_path / "outside", "slides")
    make_symlink(root / "escaped", outside, directory=True)
    assert prepare([root], cache=catalog.CatalogCache(), lightweight=True) == ()


def test_cached_root_cannot_be_replaced_with_an_escaping_file_link(tmp_path):
    root = tmp_path / "skills"
    directory = write_skill(root, "slides")
    cache = catalog.CatalogCache()
    document, = prepare([root], cache=cache, lightweight=True)
    catalog.hydrate_document(document, cache=cache)
    path = directory / "SKILL.md"
    outside = tmp_path / "outside-skill.md"
    # Identical contents are insufficient: the location is no longer allowed.
    outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    make_symlink(path, outside)
    with pytest.raises(catalog.CatalogChanged):
        catalog.hydrate_document(document, cache=cache)
    assert prepare([root], cache=cache, lightweight=True) == ()


@pytest.mark.parametrize("initially_outside", [False, True])
def test_reference_link_retarget_invalidates_details_and_cannot_escape_skill(tmp_path, initially_outside):
    root = tmp_path / "skills"
    directory = write_skill(root, "slides", create_reference=False)
    reference = directory / "references" / "formats.md"
    reference.parent.mkdir()
    permitted = directory / "local-policy.md"
    permitted.write_text("Cannot create PDF.\n", encoding="utf-8")
    outside = tmp_path / "outside-policy.md"
    outside.write_text("Create secret DOCX.\n", encoding="utf-8")
    make_symlink(reference, outside if initially_outside else permitted)
    cache = catalog.CatalogCache()
    document, = prepare([root], cache=cache, lightweight=True)
    before = catalog.hydrate_document(document, cache=cache)
    reference.unlink()
    make_symlink(reference, permitted if initially_outside else outside)
    after = catalog.hydrate_document(document, cache=cache)
    eager, = prepare([root])
    assert before.fingerprint != after.fingerprint
    assert legacy_fields(after) == legacy_fields(eager)
    assert ("create", "docx") not in after.capability_record.get("supported", ())


def test_catalog_preserves_platform_path_order(tmp_path):
    root = tmp_path / 'skills'
    for name in ('z-slides', 'B-slides', 'aa-slides'):
        write_skill(root, name)
    documents = prepare([root], lightweight=True)
    assert [d.name for d in documents] == [path.name for path in sorted(root.iterdir())]


def test_bounded_cache_eviction_preserves_catalog_and_details(tmp_path):
    root = tmp_path / "skills"
    for number in range(3):
        write_skill(root, f"slides-{number}")
    cache = catalog.CatalogCache(max_entries=1)
    documents = prepare([root], cache=cache, lightweight=True)
    assert all(not document.root_raw for document in documents)
    expected = {d.id: d for d in prepare([root])}
    for document in [*documents, documents[0]]:
        assert legacy_fields(catalog.hydrate_document(document, cache=cache)) == legacy_fields(
            expected[document.id]
        )


@pytest.mark.asyncio
async def test_service_first_search_returns_complete_authorized_candidates(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    directories = [write_skill(root, f"slides-{number:02d}") for number in range(8)]
    expected = prepare([root])
    allowed = frozenset(d.id for d in expected[1:])
    eager = rank(pipeline(expected), allowed)
    reads = observe_reads(monkeypatch)
    runtime = execution.SelectionRuntime()
    instance = service.SelectionService((root,), SelectionSettings(enabled=True), runtime=runtime)
    try:
        snapshot = await instance.current_snapshot()
        assert all(not d.root_raw for d in snapshot[0].documents.values())
        assert all(read_count(reads, directory / "references" / "formats.md") == 0
                   for directory in directories)
        result = await instance.search(
            "Create science festival presentation slides", ("PPTX", "演示文稿"),
            allowed_ids=allowed, snapshot=snapshot,
        )
        assert [(d.id, score) for d, score in result.candidates] == [
            (d.id, score) for d, score in eager.candidates
        ]
        assert [legacy_fields(d) for d, _ in result.candidates] == [
            legacy_fields(d) for d, _ in eager.candidates
        ]
        assert all(d.fingerprint and d.capability_record for d, _ in result.candidates)
        assert read_count(reads, directories[0] / "references" / "formats.md") == 0
        assert sum(read_count(reads, directory / "references" / "formats.md")
                   for directory in directories) == 5
    finally:
        runtime.shutdown()
