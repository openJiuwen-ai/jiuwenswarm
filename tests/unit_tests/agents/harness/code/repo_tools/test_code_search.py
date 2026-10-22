# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the ``code_search`` tool: engines, ranking, ignores, limits."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from openjiuwen.core.foundation.tool import ToolExposure

from jiuwenswarm.agents.harness.code.repo_tools import CodeSearchTool
from jiuwenswarm.agents.harness.code.repo_tools._common import (
    FileHits,
    LineHit,
    build_rg_args,
    contain_path,
    parse_rg_json,
    path_penalty,
    rank_files,
    render_hits,
    truncate_hits,
)
from jiuwenswarm.agents.harness.code.repo_tools.code_search import (
    build_code_search_card,
    code_search_input_params,
)


def _run(tool: CodeSearchTool, **inputs):
    return asyncio.run(tool.invoke(inputs))


# ─── card ───────────────────────────────────────────────────


def test_card_is_direct_idempotent_and_named():
    card = build_code_search_card(agent_id="a1", language="en")
    assert card.name == "code_search"
    assert card.id == "code_search_a1"
    assert card.exposure == ToolExposure.DIRECT
    assert "exposure" in card.model_fields_set  # explicit → survives the progressive policy
    assert card.idempotent is True and card.parallel_safe is True
    assert "pattern" in code_search_input_params()["properties"]
    assert code_search_input_params()["required"] == ["pattern"]
    assert "git" in card.description.lower()  # the no-history rule is in the description


def test_card_has_chinese_description():
    assert "工作区" in build_code_search_card(language="cn").description


# ─── engine-independent behaviour (parametrised over both engines) ───


@pytest.fixture(params=["ripgrep", "python"])
def tool(request, fake_repo: Path, rg_bin, no_rg_factory):
    if request.param == "ripgrep":
        if not rg_bin:
            pytest.skip("ripgrep not available")
        return CodeSearchTool(lambda: str(fake_repo), agent_id="t", rg_path=rg_bin), "ripgrep"
    no_rg_factory()
    return CodeSearchTool(lambda: str(fake_repo), agent_id="t"), "python"


@pytest.fixture
def no_rg_factory(monkeypatch, request):
    def _apply():
        request.getfixturevalue("no_rg")

    return _apply


def test_search_groups_ranks_and_ignores(tool):
    search, engine = tool
    out = _run(search, pattern="compute_total", context=1)
    assert out.success and out.data["engine"] == engine
    paths = [f["path"] for f in out.data["files"]]
    # Source before tests before vendored, .gitignore + node_modules honoured.
    assert paths[0] == "src/pkg/cart.py"
    assert "tests/test_cart.py" in paths
    assert paths.index("src/pkg/pricing.py") < paths.index("tests/test_cart.py")
    assert paths.index("tests/test_cart.py") < paths.index("vendor/lib.py")
    assert not any(p.startswith("node_modules") for p in paths)
    assert not any(p.endswith(".log") for p in paths)
    assert not any(p.startswith("secret") for p in paths)
    text = search.render_for_llm(out)
    assert text.startswith(f"{out.data['total_matches']} matches in {out.data['total_files']} files")
    assert "== src/pkg/cart.py" in text
    assert ">    6: def compute_total(items):" in text
    assert "     7:     return sum(items)" in text  # context line


def test_language_filter_and_glob(tool):
    search, _ = tool
    ts_only = _run(search, pattern="Total", language="typescript")
    assert ts_only.success
    assert [f["path"] for f in ts_only.data["files"]] == ["src/web/app.ts"]
    excluded = _run(search, pattern="compute_total", glob="!**/tests/**")
    assert excluded.success
    assert not any(p.startswith("tests/") for p in (f["path"] for f in excluded.data["files"]))
    only_tests = _run(search, pattern="compute_total", glob=["tests/*.py"])
    assert [f["path"] for f in only_tests.data["files"]] == ["tests/test_cart.py"]


def test_literal_and_case_modes(tool):
    search, _ = tool
    regex_hit = _run(search, pattern=r"def \w+\(")
    assert regex_hit.success and regex_hit.data["total_matches"] >= 3
    literal_miss = _run(search, pattern=r"def \w+\(", literal=True)
    assert literal_miss.success and literal_miss.data["total_matches"] == 0
    assert "No matches" in search.render_for_llm(literal_miss)
    smart = _run(search, pattern="cart", case="smart")  # lowercase → insensitive
    assert smart.data["total_matches"] > 0
    sensitive = _run(search, pattern="cart", case="sensitive", language="python")
    sensitive_paths = {f["path"] for f in sensitive.data["files"]}
    assert "src/pkg/cart.py" not in sensitive_paths or all(
        "cart" in m["text"] for f in sensitive.data["files"] for m in f["matches"]
    )
    bad_case = _run(search, pattern="x", case="loud")
    assert not bad_case.success and "case must be one of" in bad_case.error


def test_path_scoping_and_containment(tool):
    search, _ = tool
    scoped = _run(search, pattern="compute_total", path="src/pkg")
    assert scoped.success
    assert all(f["path"].startswith("src/pkg/") for f in scoped.data["files"])
    single = _run(search, pattern="compute_total", path="tests/test_cart.py")
    assert [f["path"] for f in single.data["files"]] == ["tests/test_cart.py"]
    outside = _run(search, pattern="compute_total", path="../")
    assert not outside.success and "outside the workspace root" in outside.error
    missing = _run(search, pattern="compute_total", path="nope/")
    assert not missing.success and "does not exist" in missing.error


def test_max_results_truncates_in_rank_order(tool):
    search, _ = tool
    out = _run(search, pattern="compute_total", max_results=2)
    assert out.success and out.data["truncated"] is True
    assert out.data["shown_matches"] == 2
    assert out.data["files"][0]["path"] == "src/pkg/cart.py"
    assert "showing the top 2" in search.render_for_llm(out)
    clamped = _run(search, pattern="compute_total", max_results=99999)
    assert clamped.success and clamped.data["truncated"] is False


def test_missing_pattern_is_rejected(fake_repo: Path):
    search = CodeSearchTool(lambda: str(fake_repo))
    out = _run(search, pattern="   ")
    assert not out.success and out.error == "pattern is required"


def test_invalid_regex_python_engine(fake_repo: Path, no_rg):
    search = CodeSearchTool(lambda: str(fake_repo))
    out = _run(search, pattern="(unclosed")
    assert not out.success and "invalid pattern" in out.error


# ─── pure helpers ───────────────────────────────────────────


def test_parse_rg_json_groups_matches_with_context():
    events = [
        {"type": "begin", "data": {"path": {"text": "a.py"}}},
        {"type": "context", "data": {"path": {"text": "a.py"}, "lines": {"text": "x = 1\n"}, "line_number": 1}},
        {"type": "match", "data": {"path": {"text": "a.py"}, "lines": {"text": "def f():\n"}, "line_number": 2}},
        {"type": "context", "data": {"path": {"text": "a.py"}, "lines": {"text": "    pass\n"}, "line_number": 3}},
        {"type": "end", "data": {"path": {"text": "a.py"}}},
        {"type": "begin", "data": {"path": {"text": "b.py"}}},
        {"type": "match", "data": {"path": {"text": "b.py"}, "lines": {"text": "def g():\n"}, "line_number": 9}},
        {"type": "end", "data": {"path": {"text": "b.py"}}},
        {"type": "summary", "data": {}},
    ]
    files = parse_rg_json("\n".join(json.dumps(e) for e in events))
    assert [f.path for f in files] == ["a.py", "b.py"]
    hit = files[0].hits[0]
    assert hit.line == 2 and hit.text == "def f():"
    assert hit.context_before == [(1, "x = 1")]
    assert hit.context_after == [(3, "    pass")]
    assert files[1].hits[0].line == 9


def test_parse_rg_json_tolerates_garbage_and_bytes_paths():
    payload = 'not json\n{"type":"begin","data":{"path":{"bytes":"YS5weQ=="}}}\n' + json.dumps(
        {"type": "match", "data": {"lines": {"text": "hit\n"}, "line_number": 1}}
    )
    files = parse_rg_json(payload)
    assert [f.path for f in files] == ["a.py"] and files[0].hits[0].text == "hit"


def test_build_rg_args_flags():
    args = build_rg_args(
        pattern="foo", literal=True, case="insensitive", context=2, globs=["*.py", "!tests/**"],
        file_type="py", max_count_per_file=7,
    )
    assert "--json" in args and "--no-require-git" in args
    assert args[args.index("-C") + 1] == "2"
    assert "--fixed-strings" in args and "-i" in args
    assert args[args.index("--type") + 1] == "py"
    assert args[args.index("--max-count") + 1] == "7"
    assert args[-2:] == ["--", "foo"]
    assert args.count("--glob") >= 2 and "!**/node_modules/**" in args
    smart = build_rg_args(pattern="Foo", literal=False, case="smart", context=0, globs=(), file_type=None, max_count_per_file=1)
    assert "-S" in smart and "-C" not in smart


def test_rank_and_truncate_and_render(tmp_path: Path):
    root = tmp_path
    files = [
        FileHits(path=str(root / "tests" / "test_a.py"), hits=[LineHit(3, "a"), LineHit(9, "b")]),
        FileHits(path=str(root / "src" / "a.py"), hits=[LineHit(1, "c"), LineHit(2, "d")]),
        FileHits(path=str(root / "vendor" / "v.py"), hits=[LineHit(1, "e")] * 5),
        FileHits(path=str(root / "src" / "deep" / "x.py"), hits=[LineHit(1, "f"), LineHit(2, "g")]),
    ]
    ranked = rank_files(root, files)
    assert [f.path for f in ranked] == ["vendor/v.py", "src/a.py", "src/deep/x.py", "tests/test_a.py"]
    kept, total, truncated = truncate_hits(ranked, 6)
    assert total == 11 and truncated and sum(f.count for f in kept) == 6
    text = render_hits(kept)
    assert text.startswith("== vendor/v.py (5 matches)")
    assert path_penalty("src/x.py") == 0 and path_penalty("tests/x.py") == 1
    assert path_penalty("third_party/x.py") == 2 and path_penalty("pkg/test_x.py") == 1


def test_contain_path_rules(tmp_path: Path):
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "f.py").write_text("x", encoding="utf-8")
    assert contain_path(tmp_path, None) == tmp_path
    assert contain_path(tmp_path, "inner") == tmp_path / "inner"
    assert contain_path(tmp_path, str(tmp_path / "inner" / "f.py")) == tmp_path / "inner" / "f.py"
    with pytest.raises(ValueError, match="outside the workspace root"):
        contain_path(tmp_path, "../")
    with pytest.raises(ValueError, match="does not exist"):
        contain_path(tmp_path, "ghost")


# ─── review fixes: rg errors, ignore list, language aliases ────


def test_invalid_regex_ripgrep_engine(fake_repo: Path, rg_bin):
    """ripgrep exit code 2 for a bad regex is an error, not "0 matches"."""
    if not rg_bin:
        pytest.skip("ripgrep not available")
    search = CodeSearchTool(lambda: str(fake_repo), rg_path=rg_bin)
    out = _run(search, pattern="(")
    assert not out.success and "invalid pattern" in out.error
    # Same wording as the Python engine so the model sees one behaviour.
    assert "invalid pattern" in _run(CodeSearchTool(lambda: str(fake_repo), rg_path=rg_bin), pattern="(unclosed").error


def _repo_with_build_dirs(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for sub, name, body in (
        ("build", "b.py", "def in_build():\n    pass\n"),
        ("logs", "c.py", "def in_logs():\n    pass\n"),
        ("dist", "d.py", "def in_dist():\n    pass\n"),
        ("node_modules/x", "e.py", "def in_node_modules():\n    pass\n"),
        ("src", "a.py", "def in_src():\n    pass\n"),
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / name).write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_build_and_logs_dirs_are_searchable_unless_gitignored(tmp_path: Path, rg_bin, no_rg_factory, engine):
    root = _repo_with_build_dirs(tmp_path)
    if engine == "ripgrep":
        if not rg_bin:
            pytest.skip("ripgrep not available")
        tool = CodeSearchTool(lambda: str(root), rg_path=rg_bin)
    else:
        no_rg_factory()
        tool = CodeSearchTool(lambda: str(root))
    out = _run(tool, pattern=r"def in_\w+")
    assert out.success and out.data["engine"] == engine
    found = {Path(f["path"]).parts[0] for f in out.data["files"]}
    assert {"build", "logs", "dist", "src"} <= found
    assert "node_modules" not in found  # still on the built-in ignore list

    # A .gitignore takes them out again, even without a .git directory.
    (root / ".gitignore").write_text("build/\nlogs/\n", encoding="utf-8")
    out = _run(tool, pattern=r"def in_\w+")
    found = {Path(f["path"]).parts[0] for f in out.data["files"]}
    assert "build" not in found and "logs" not in found and "dist" in found and "src" in found


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_configured_ignored_dirs_override_the_default_list(tmp_path: Path, rg_bin, no_rg_factory, engine):
    root = _repo_with_build_dirs(tmp_path)
    if engine == "ripgrep":
        if not rg_bin:
            pytest.skip("ripgrep not available")
        tool = CodeSearchTool(lambda: str(root), rg_path=rg_bin, ignored_dirs=["dist", "logs"])
    else:
        no_rg_factory()
        tool = CodeSearchTool(lambda: str(root), ignored_dirs=["dist", "logs"])
    out = _run(tool, pattern=r"def in_\w+")
    found = {Path(f["path"]).parts[0] for f in out.data["files"]}
    assert found == {"build", "src", "node_modules"}


def test_normalize_ignored_dirs_rules():
    from jiuwenswarm.agents.harness.code.repo_tools._common import (
        IGNORED_DIRS,
        normalize_ignored_dirs,
    )

    assert normalize_ignored_dirs(None) == IGNORED_DIRS
    assert normalize_ignored_dirs("weird-type") == frozenset({"weird-type"})
    assert normalize_ignored_dirs(42) == IGNORED_DIRS
    assert normalize_ignored_dirs([" build/ ", "", "a/b", "..", "logs"]) == frozenset({"build", "logs"})
    for name in ("dist", "build", "target", "logs"):
        assert name not in IGNORED_DIRS
    assert {".git", "node_modules", "__pycache__", ".venv"} <= IGNORED_DIRS


def test_language_aliases_and_unknown_language(tool):
    search, _engine = tool
    aliased = _run(search, pattern="compute", language="py")
    full = _run(search, pattern="compute", language="python")
    assert aliased.success and full.success
    assert {f["path"] for f in aliased.data["files"]} == {f["path"] for f in full.data["files"]}
    assert all(f["path"].endswith(".py") for f in aliased.data["files"])
    bad = _run(search, pattern="compute", language="klingon")
    assert not bad.success and "unknown language 'klingon'" in bad.error and "python" in bad.error


def test_fallback_smart_case_ignores_regex_escapes(tmp_path: Path, no_rg):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("value = 1\nVALUE = 2\n", encoding="utf-8")
    tool = CodeSearchTool(lambda: str(root))
    # \S has an uppercase letter in the escape only: stays case-insensitive like ripgrep.
    out = _run(tool, pattern=r"value\S*")
    assert out.data["total_matches"] == 2
    out = _run(tool, pattern="VALUE")
    assert out.data["total_matches"] == 1
