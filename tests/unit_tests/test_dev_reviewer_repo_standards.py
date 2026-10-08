"""Tests for repo-local review standards discovery on dev-reviewer's
``collect``: slot conventions, AGENTS.md/CLAUDE.md recording, dedup,
anti-gaming (``modified_by_pr``) flags, and the review.md standards line.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "jiuwenavatar"
    / "resources"
    / "avatar-skills"
    / "dev-reviewer"
    / "scripts"
    / "code_review_runner.py"
)

SKILL_MD_TEMPLATE = """---
name: {name}
description: {description}
---

# {name}

Review standards body.
"""


def load_runner():
    spec = importlib.util.spec_from_file_location("dev_review_runner_repo_standards", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_skill(repo: Path, rel_dir: str, name: str, *, references: list[str] | None = None) -> Path:
    skill_dir = repo / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        SKILL_MD_TEMPLATE.format(name=name, description=f"Repo review standards for {name}"),
        encoding="utf-8",
    )
    for ref in references or []:
        ref_path = skill_dir / ref
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text("# reference\n", encoding="utf-8")
    return skill_md


def _namespace(repo: Path, **kwargs):
    values = {
        "module": "demo",
        "repo": str(repo),
        "repo_root": str(repo),
        "out_dir": "",
        "review_skill": [],
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_discovers_skill_in_pkg_resources_workspace_slot(tmp_path: Path):
    """Primary convention: <pkg>/resources/agent/workspace/skills/<name>/SKILL.md."""
    runner = load_runner()
    repo = tmp_path / "repo"
    _write_skill(
        repo,
        "jiuwenswarm/resources/agent/workspace/skills/jiuwenswarm-pr-review",
        "jiuwenswarm-pr-review",
        references=["references/technical-route.md"],
    )

    entries, warnings = runner.discover_repo_review_standards(repo)

    assert warnings == []
    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"] == (
        "jiuwenswarm/resources/agent/workspace/skills/jiuwenswarm-pr-review/SKILL.md"
    )
    assert entry["slot"] == "resources-workspace-skills"
    assert entry["kind"] == "skill"
    assert entry["name"] == "jiuwenswarm-pr-review"
    assert entry["description"].startswith("Repo review standards for")
    assert entry["references"] == [
        "jiuwenswarm/resources/agent/workspace/skills/jiuwenswarm-pr-review/references/technical-route.md"
    ]
    assert entry["modified_by_pr"] is False


def test_discovers_root_resources_and_platform_slots_in_priority_order(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    _write_skill(repo, "resources/agent/workspace/skills/root-skill", "root-skill")
    _write_skill(repo, ".claude/skills/cc-skill", "cc-skill")
    _write_skill(repo, ".codex/skills/codex-skill", "codex-skill")

    entries, _ = runner.discover_repo_review_standards(repo)

    slots = [entry["slot"] for entry in entries]
    assert slots == ["resources-workspace-skills", "platform-project-skills", "platform-project-skills"]
    paths = [entry["path"] for entry in entries]
    assert paths[0] == "resources/agent/workspace/skills/root-skill/SKILL.md"
    assert paths[1] == ".claude/skills/cc-skill/SKILL.md"


def test_no_standards_returns_empty(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    entries, warnings = runner.discover_repo_review_standards(repo)

    assert entries == []
    assert warnings == []


def test_agents_and_claude_md_recorded_as_repo_instructions(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# claude\n", encoding="utf-8")

    entries, warnings = runner.discover_repo_review_standards(repo)

    assert warnings == []
    assert [entry["kind"] for entry in entries] == ["repo-instructions", "repo-instructions"]
    assert [entry["path"] for entry in entries] == ["AGENTS.md", "CLAUDE.md"]
    assert entries[0]["name"] == "AGENTS"
    assert entries[0]["references"] == []


def test_modified_by_pr_flags_skill_and_reference_touch(tmp_path: Path):
    """Anti-gaming: touching the SKILL.md or any file under its dir flags the standard."""
    runner = load_runner()
    repo = tmp_path / "repo"
    _write_skill(
        repo,
        "resources/agent/workspace/skills/std",
        "std",
        references=["references/deep/route.md"],
    )

    untouched, no_warnings = runner.discover_repo_review_standards(
        repo, changed_files=["src/app.py", "docs/guide.md"]
    )
    assert untouched[0]["modified_by_pr"] is False
    assert no_warnings == []

    touched, warnings = runner.discover_repo_review_standards(
        repo,
        changed_files=["src/app.py", "resources/agent/workspace/skills/std/references/deep/route.md"],
    )
    assert touched[0]["modified_by_pr"] is True
    assert len(warnings) == 1
    assert warnings[0].startswith("Repo review standard touched by this PR:")
    assert "base (target branch)" in warnings[0]


def test_agents_md_modified_by_pr_warns(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# agents\n", encoding="utf-8")

    entries, warnings = runner.discover_repo_review_standards(repo, changed_files=["AGENTS.md"])

    assert entries[0]["modified_by_pr"] is True
    assert len(warnings) == 1
    assert warnings[0].startswith("Repo instruction file touched by this PR:")


def test_dedup_by_name_keeps_priority_slot(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    _write_skill(repo, "resources/agent/workspace/skills/dup", "dup")
    _write_skill(repo, ".claude/skills/dup", "dup")

    entries, _ = runner.discover_repo_review_standards(repo)

    assert len(entries) == 1
    assert entries[0]["slot"] == "resources-workspace-skills"


def test_frontmatter_fallback_to_dir_name_and_missing_frontmatter(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    skill_dir = repo / ".claude" / "skills" / "no-frontmatter"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# no frontmatter here\n", encoding="utf-8")

    entries, _ = runner.discover_repo_review_standards(repo)

    assert entries[0]["name"] == "no-frontmatter"
    assert entries[0]["description"] == ""


def test_explicit_review_skill_takes_priority_and_missing_warns(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    _write_skill(repo, ".claude/skills/explicit-skill", "explicit-skill")
    _write_skill(repo, "resources/agent/workspace/skills/regular", "regular")

    entries, warnings = runner.discover_repo_review_standards(
        repo,
        extra_paths=[
            ".claude/skills/explicit-skill",  # dir form resolves to SKILL.md
            "does/not/exist/SKILL.md",
        ],
    )

    assert entries[0]["slot"] == "explicit"
    assert entries[0]["path"] == ".claude/skills/explicit-skill/SKILL.md"
    assert entries[1]["slot"] == "resources-workspace-skills"
    assert warnings == ["--review-skill path not found, skipped: does/not/exist/SKILL.md"]


def test_collect_records_standards_in_context(tmp_path: Path):
    """End-to-end collect on a real git repo: standards land in context.json."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "reviewer@example.com")
    git("config", "user.name", "reviewer")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")

    # PR head: change business code and touch the repo's review standard.
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    _write_skill(
        repo,
        "pkg/resources/agent/workspace/skills/pr-review",
        "pr-review",
        references=["references/route.md"],
    )
    (repo / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "head: change app + add review skill")

    code = runner.command_collect(
        _namespace(
            repo,
            pr="local",
            issue="",
            base="HEAD~1",
            head="HEAD",
            allow_working_tree=False,
            gitcode_token="",
            out_dir=str(repo / "doc" / "demo" / "review"),
        )
    )
    assert code == 0

    context = json.loads(
        (repo / "doc" / "demo" / "review" / "context.json").read_text(encoding="utf-8-sig")
    )
    standards = context["project_context"]["repo_review_standards"]
    by_slot = {entry["slot"]: entry for entry in standards}
    assert set(by_slot) == {"resources-workspace-skills", "repo-instructions"}
    assert by_slot["resources-workspace-skills"]["name"] == "pr-review"
    # The skill is introduced by this PR -> anti-gaming flag must be set.
    assert by_slot["resources-workspace-skills"]["modified_by_pr"] is True
    assert by_slot["resources-workspace-skills"]["last_commit"] != ""
    assert any(
        "Repo review standard touched by this PR" in warning
        for warning in context.get("warnings", [])
    )


def test_report_lists_repo_review_standards(tmp_path: Path):
    runner = load_runner()
    repo = tmp_path / "repo"
    review_dir = repo / "doc" / "demo" / "review"
    review_dir.mkdir(parents=True)
    context = {
        "schema_version": 1,
        "module": "demo",
        "pr": "local",
        "repo": str(repo),
        "repo_root": str(repo),
        "fetch_method": "local",
        "changed_files": ["src/app.py"],
        "project_context": {
            "repo_review_standards": [
                {"path": "resources/agent/workspace/skills/std/SKILL.md", "kind": "skill"},
                {"path": "AGENTS.md", "kind": "repo-instructions"},
            ]
        },
    }
    (review_dir / "context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # No result.json: report falls back to defaults; only the standards line matters.
    # Exit code is not asserted: the default verdict is not PASS.
    runner.command_report(_namespace(repo, skip_schema_validation=True))

    review_md = (repo / "doc" / "demo" / "review.md").read_text(encoding="utf-8-sig")
    assert "- Repo review standards: resources/agent/workspace/skills/std/SKILL.md, AGENTS.md" in review_md
    # Nonzero exit is expected (default verdict is not PASS); do not assert code.

    # Backward compatibility: contexts collected before this feature have no field.
    context["project_context"] = {}
    (review_dir / "context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    runner.command_report(_namespace(repo, skip_schema_validation=True))
    review_md = (repo / "doc" / "demo" / "review.md").read_text(encoding="utf-8-sig")
    assert "- Repo review standards: none discovered" in review_md
