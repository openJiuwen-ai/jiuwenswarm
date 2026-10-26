# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise document authorship, relationships and generated navigation."""

import importlib
from pathlib import Path
from types import ModuleType

import pytest
import yaml


@pytest.fixture
def sync_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the actual document-maintenance entrypoint."""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return importlib.import_module("sync_documents")


def write_doc(root: Path, relative: str, content: str) -> Path:
    """Write a document inside an isolated asset root."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def metadata(path: Path) -> dict:
    """Read emitted frontmatter as a document consumer would."""
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])


def test_explicit_git_author_is_preserved_on_later_sync(sync_module: ModuleType, tmp_path: Path) -> None:
    """Capture the creator once without reassigning later editors."""
    doc = write_doc(tmp_path, "specs/S_01_contract.md", "---\nauthor: null\n---\n\n# Contract\n\nOriginal body.\n")
    identity = sync_module.GitIdentity("first-writer", "first@example.invalid")
    sync_module.synchronize(tmp_path, identity, doc)
    assert metadata(doc)["author"] == "first-writer"
    second = sync_module.GitIdentity("later-editor", "later@example.invalid")
    sync_module.synchronize(tmp_path, second, doc)
    assert metadata(doc)["author"] == "first-writer"
    assert "Original body." in doc.read_text(encoding="utf-8")


def test_historical_owner_is_preserved_as_author(sync_module: ModuleType, tmp_path: Path) -> None:
    """Rename historical owner to author while retaining its exact value."""
    doc = write_doc(tmp_path, "specs/S_01_legacy.md", "---\nowner: '@old-dri'\n---\n\n# Legacy\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("importer", "importer@example.invalid"))
    data = metadata(doc)
    assert "owner" not in data
    assert data["author"] == "@old-dri"
    assert data["author_email"] is None
    assert data["author_note"] == "由历史 owner 字段原值迁移"


def test_indexes_link_features_and_bugs_to_designs(sync_module: ModuleType, tmp_path: Path) -> None:
    """Expose module, file and design relationships through generated indexes."""
    write_doc(tmp_path, "designs/proposals/D-20261026-1200-topic/design.md", "---\nid: D-20261026-1200-topic\ntitle: Topic\nstage: proposals\nstatus: draft\nmodules: [runtime]\n---\n\n# Topic\n")
    for folder, prefix in (("features", "F"), ("bugfix", "B")):
        write_doc(tmp_path, f"{folder}/{prefix}_20261026_1200_topic.md", "---\nmodules: [runtime]\nfiles: [src/runtime.py]\ndesigns: [D-20261026-1200-topic]\n---\n\n# Change\n")
    identity = sync_module.GitIdentity("writer", "writer@example.invalid")
    sync_module.synchronize(tmp_path, identity)
    for section in ("features", "bugfix"):
        index = tmp_path / section / "INDEX.md"
        text = index.read_text(encoding="utf-8")
        assert "../designs/proposals/D-20261026-1200-topic/design.md" in text
        assert "src/runtime.py" in text and "runtime" in text
        assert metadata(index)["author"] == "writer"
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*.md")}
    sync_module.synchronize(tmp_path, identity)
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*.md")}
    assert before == after


def test_explicit_no_design_and_unknown_history_are_distinct(sync_module: ModuleType, tmp_path: Path) -> None:
    """An unknown relationship cannot silently become an assertion of no design."""
    unknown = write_doc(tmp_path, "features/F_01_old.md", "# Old\n")
    current = write_doc(tmp_path, "features/F_02_fix.md", "---\ndesigns: []\ndesign_note: Small correction to existing contract\n---\n\n# Fix\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"), current)
    assert metadata(unknown)["designs"] is None
    assert metadata(current)["designs"] == []
    text = (tmp_path / "features/INDEX.md").read_text(encoding="utf-8")
    assert "待核实" in text and "Small correction to existing contract" in text


def test_invalid_design_declaration_does_not_write_partial_metadata(sync_module: ModuleType, tmp_path: Path) -> None:
    """Validate the whole input set before mutating any source document."""
    first = write_doc(tmp_path, "features/F_01_valid.md", "# Valid\n")
    invalid = write_doc(tmp_path, "features/F_02_invalid.md", "---\ndesigns: []\n---\n\n# Invalid\n")
    original = first.read_bytes()
    with pytest.raises(ValueError, match="design_note"):
        sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"), invalid)
    assert first.read_bytes() == original
    assert not (tmp_path / "INDEX.md").exists()


def test_legacy_nested_features_remain_discoverable(sync_module: ModuleType, tmp_path: Path) -> None:
    """Keep historical paths while new feature records use the flat root."""
    legacy = write_doc(tmp_path, "features/harness/F_01_old.md", "# Legacy\n")
    current = write_doc(tmp_path, "features/F_20261026_1200_new.md", "---\nmodules: [openjiuwen/harness]\ndesigns: []\ndesign_note: Direct implementation of existing contract\n---\n\n# New\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"), current)
    text = (tmp_path / "features/INDEX.md").read_text(encoding="utf-8")
    assert "harness/F_01_old.md" in text
    assert "F_20261026_1200_new.md" in text
    assert legacy.is_file()
    assert metadata(current)["author"] == "writer"


def test_explicit_root_is_independent_of_current_directory(sync_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve documents and links from the asset root rather than caller cwd."""
    assets = tmp_path / "assets"
    doc = write_doc(assets, "specs/S_01_contract.md", "# Contract\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    sync_module.synchronize(assets, sync_module.GitIdentity("writer", "writer@example.invalid"), doc)
    assert (assets / "INDEX.md").is_file()
    assert not (elsewhere / "INDEX.md").exists()


def test_testing_document_gets_author_but_stays_out_of_formal_indexes(sync_module: ModuleType, tmp_path: Path) -> None:
    """Apply authorship to local handoff documents without exposing them in indexes."""
    doc = write_doc(tmp_path, "designs/testing/topic/testing.md", "---\nauthor: null\n---\n\n# Local handoff\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"), doc)
    assert metadata(doc)["author"] == "writer"
    assert "Local handoff" not in (tmp_path / "INDEX.md").read_text(encoding="utf-8")
    assert "Local handoff" not in (tmp_path / "designs/INDEX.md").read_text(encoding="utf-8")


def test_cli_reads_git_identity_for_the_target_document_root(sync_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit asset root uses its own repository-effective identity."""
    doc = write_doc(tmp_path, "specs/S_01_contract.md", "# Contract\n")
    observed: list[Path] = []

    def target_identity(path: Path) -> object:
        """Record the identity lookup context without changing Git configuration."""
        observed.append(path)
        return sync_module.GitIdentity("target-writer", "target@example.invalid")

    monkeypatch.setattr(sync_module, "git_identity", target_identity)
    assert sync_module.main(["--root", str(tmp_path), "--document", str(doc)]) == 0
    assert observed == [tmp_path.resolve()]
    assert metadata(doc)["author"] == "target-writer"


def test_history_without_owner_or_author_remains_unconfirmed(sync_module: ModuleType, tmp_path: Path) -> None:
    """Leave genuinely unattributed history explicit instead of assigning an importer."""
    doc = write_doc(tmp_path, "specs/S_01_unknown.md", "# Unknown\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("importer", "importer@example.invalid"))
    assert metadata(doc)["author"] is None
    assert metadata(doc)["author_note"]


def test_conflicting_author_and_legacy_owner_do_not_lose_either_identity(sync_module: ModuleType, tmp_path: Path) -> None:
    """Require resolution before removing a conflicting historical identity."""
    doc = write_doc(tmp_path, "specs/S_01_conflict.md", "---\nowner: '@original'\nauthor: explicit-author\n---\n\n# Conflict\n")
    before = doc.read_bytes()
    with pytest.raises(ValueError, match="conflicts"):
        sync_module.synchronize(tmp_path, sync_module.GitIdentity("importer", "importer@example.invalid"))
    assert doc.read_bytes() == before


def test_module_groups_count_cross_module_documents_once_globally(sync_module: ModuleType, tmp_path: Path) -> None:
    """List shared documents under each module without inflating the global count."""
    write_doc(tmp_path, "features/F_01_shared.md", "---\nmodules: [runtime, gateway, runtime]\nfiles: [src/shared.py]\ndesigns: []\ndesign_note: Existing contract\n---\n\n# Shared\n")
    write_doc(tmp_path, "features/F_02_runtime.md", "---\nmodules: [runtime]\n---\n\n# Runtime\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"))
    text = (tmp_path / "features/INDEX.md").read_text(encoding="utf-8")
    assert "共 2 篇文档" in text
    assert "| [gateway](#module-1) | 1 |" in text
    assert "| [runtime](#module-2) | 2 |" in text
    assert text.count("[F_01_shared]") == 2
    assert text.count("[F_02_runtime]") == 1
    assert text.count("src/shared.py") == 2


def test_missing_module_is_visible_in_last_group(sync_module: ModuleType, tmp_path: Path) -> None:
    """Keep unclassified documents discoverable without guessing a module."""
    write_doc(tmp_path, "specs/S_01_runtime.md", "---\nmodules: [runtime]\n---\n\n# Runtime\n")
    write_doc(tmp_path, "specs/S_02_unknown.md", "# Unknown\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"))
    text = (tmp_path / "specs/INDEX.md").read_text(encoding="utf-8")
    assert text.index("## runtime") < text.index("## 未分类")
    assert "[S_02_unknown]" in text.split("## 未分类", 1)[1]


def test_grouping_keeps_author_and_design_links(sync_module: ModuleType, tmp_path: Path) -> None:
    """Changing navigation must retain attribution and exact design targets."""
    write_doc(tmp_path, "designs/proposals/D-20261026-1200-topic/design.md", "---\nid: D-20261026-1200-topic\nstatus: draft\nstage: proposals\nmodules: [runtime]\n---\n\n# Topic\n")
    doc = write_doc(tmp_path, "features/F_01_change.md", "---\nauthor: '@original'\nmodules: [runtime]\ndesigns: [D-20261026-1200-topic]\n---\n\n# Change\n")
    sync_module.synchronize(tmp_path, sync_module.GitIdentity("writer", "writer@example.invalid"))
    text = (tmp_path / "features/INDEX.md").read_text(encoding="utf-8")
    assert "## runtime" in text
    assert "@original" in text
    assert "../designs/proposals/D-20261026-1200-topic/design.md" in text
    assert metadata(doc)["author"] == "@original"
