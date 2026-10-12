# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for ``find_symbol`` / ``symbol_outline``: LSP present, LSP absent, both engines."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openjiuwen.core.foundation.tool import ToolExposure

from jiuwenswarm.agents.harness.code.repo_tools import (
    CodeSearchEngine,
    FindSymbolTool,
    LspBridge,
    SymbolOutlineTool,
    build_find_symbol_card,
    build_symbol_outline_card,
)
from jiuwenswarm.agents.harness.code.repo_tools.symbols import (
    definition_regex,
    looks_like_definition,
    outline_lines,
    reference_regex,
)


def _run(tool, **inputs):
    return asyncio.run(tool.invoke(inputs))


class NoLsp(LspBridge):
    async def available(self, file_path: Path) -> bool:
        return False


class FakeLsp(LspBridge):
    """LSP that answers for .py files only and records what it was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    async def available(self, file_path: Path) -> bool:
        return file_path.suffix == ".py"

    async def call(self, inputs, workspace):
        self.calls.append(inputs)
        op = inputs["operation"]
        if op == "workspaceSymbol":
            return True, "src/pkg/cart.py:\n  6: Function compute_total\nsrc/pkg/other.py:\n  1: Variable unrelated"
        if op == "findReferences":
            return True, "src/pkg/cart.py:\n  6:5\n  15:16\ntests/test_cart.py:\n  1:26"
        if op == "documentSymbol":
            return True, "Function compute_total\nClass Cart\n  Method total\n  Method checkout"
        return False, "unsupported"


class BrokenLsp(LspBridge):
    async def available(self, file_path: Path) -> bool:
        return True

    async def call(self, inputs, workspace):
        return False, "LSP request timed out"


# ─── cards ──────────────────────────────────────────────────


def test_cards_are_direct_and_idempotent():
    for card in (build_find_symbol_card(agent_id="x"), build_symbol_outline_card(agent_id="x")):
        assert card.exposure == ToolExposure.DIRECT and "exposure" in card.model_fields_set
        assert card.idempotent is True and card.id.endswith("_x")
    assert build_find_symbol_card().name == "find_symbol"
    assert build_symbol_outline_card().name == "symbol_outline"
    assert "工作树" in build_find_symbol_card(language="cn").description


# ─── regex heuristics ───────────────────────────────────────


@pytest.mark.parametrize(
    "language, line",
    [
        ("python", "def compute_total(items):"),
        ("python", "    async def compute_total(self):"),
        ("python", "class compute_total:"),
        ("python", "compute_total = 3"),
        ("typescript", "export function compute_total(a) {"),
        ("typescript", "const compute_total = (x) => x"),
        ("typescript", "  compute_total(): number {"),
        ("javascript", "class compute_total extends Base {"),
        ("go", "func (c *Cart) compute_total() int {"),
        ("go", "type compute_total struct {"),
        ("rust", "pub fn compute_total(&self) -> u32 {"),
        ("rust", "impl compute_total {"),
        ("java", "public static int compute_total(List<Item> items) throws Exception {"),
        ("java", "public class compute_total {"),
        ("c", "static int compute_total(int a, int b) {"),
        ("c", "#define compute_total 1"),
        ("ruby", "  def self.compute_total"),
        ("shell", "compute_total() {"),
        (None, "func compute_total() {"),
    ],
)
def test_definition_patterns_match(language, line):
    assert looks_like_definition(line, "compute_total", language)


@pytest.mark.parametrize(
    "language, line",
    [
        ("python", "    return compute_total(self.items)"),
        ("python", "from x import compute_total"),
        ("typescript", "  return compute_total(items);"),
        ("go", "\tv := compute_total()"),
        ("python", "def compute_total_v2():"),
    ],
)
def test_definition_patterns_reject_uses(language, line):
    assert not looks_like_definition(line, "compute_total", language)


def test_regex_builders_escape_names():
    assert "\\." in definition_regex("a.b", "python") or "a\\.b" in definition_regex("a.b", "python")
    assert reference_regex("x$y") == r"\bx\$y\b"


# ─── find_symbol without LSP (both engines) ────────────────


@pytest.fixture(params=["ripgrep", "python"])
def find_tool(request, fake_repo: Path, rg_bin, no_rg_factory):
    if request.param == "ripgrep":
        if not rg_bin:
            pytest.skip("ripgrep not available")
        engine = CodeSearchEngine(lambda: str(fake_repo), rg_path=rg_bin)
    else:
        no_rg_factory()
        engine = CodeSearchEngine(lambda: str(fake_repo))
    return FindSymbolTool(lambda: str(fake_repo), engine=engine, lsp=NoLsp(), agent_id="t"), request.param


@pytest.fixture
def no_rg_factory(request):
    def _apply():
        request.getfixturevalue("no_rg")

    return _apply


def test_find_definition_falls_back_to_regex(find_tool):
    tool, engine = find_tool
    out = _run(tool, name="compute_total")
    assert out.success and out.data["engine"] == engine
    defs = out.data["definitions"]
    assert [(d["path"], d["line"]) for d in defs] == [("src/pkg/cart.py", 6), ("vendor/lib.py", 1)]
    text = tool.render_for_llm(out)
    assert text.startswith(f"Definitions of compute_total ({engine}): 2")
    assert ">    6: def compute_total(items):" in text


def test_find_references_tags_definitions(find_tool):
    tool, _engine = find_tool
    out = _run(tool, name="compute_total", kind="references", language="python")
    assert out.success
    refs = out.data["references"]
    paths = [r["path"] for r in refs]
    assert "src/pkg/cart.py" in paths and "tests/test_cart.py" in paths
    assert not any(p.endswith(".md") for p in paths)  # language filter applied
    assert any(r["definition"] for r in refs if r["path"] == "src/pkg/cart.py")
    assert "[definition]" in tool.render_for_llm(out)


def test_find_all_and_scoping_and_file_language(find_tool):
    tool, _ = find_tool
    out = _run(tool, name="computeTotal", kind="all", file_path="src/web/app.ts")
    assert out.success and out.data["language"] == "typescript"
    assert [d["path"] for d in out.data["definitions"]] == ["src/web/app.ts"]
    assert "Definitions of computeTotal" in out.data["content"]
    assert "References to computeTotal" in out.data["content"]
    scoped = _run(tool, name="compute_total", path="tests")
    assert scoped.success and scoped.data["definitions"] == []
    assert "none found" in tool.render_for_llm(scoped)


def test_find_symbol_validation(fake_repo: Path):
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=NoLsp())
    assert not _run(tool, name="a b").success
    assert not _run(tool, name="").success
    bad = _run(tool, name="x", kind="usages")
    assert not bad.success and "kind must be one of" in bad.error
    outside = _run(tool, name="x", path="../")
    assert not outside.success and "outside" in outside.error


def test_max_results_clamps(find_tool):
    tool, _ = find_tool
    out = _run(tool, name="compute_total", kind="references", max_results=1)
    assert out.success and len(out.data["references"]) == 1
    assert "showing 1" in tool.render_for_llm(out)


# ─── find_symbol with LSP ───────────────────────────────────


def test_find_definition_prefers_lsp_when_server_available(fake_repo: Path, no_rg):
    lsp = FakeLsp()
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=lsp, agent_id="t")
    out = _run(tool, name="compute_total", file_path="src/pkg/cart.py")
    assert out.success and out.data["engine"] == "lsp"
    assert lsp.calls[0]["operation"] == "workspaceSymbol" and lsp.calls[0]["query"] == "compute_total"
    text = tool.render_for_llm(out)
    assert "(lsp)" in text and "compute_total" in text and "unrelated" not in text


def test_find_definition_lsp_anchor_is_picked_from_language(fake_repo: Path, no_rg):
    lsp = FakeLsp()
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=lsp)
    out = _run(tool, name="compute_total", language="python")
    assert out.data["engine"] == "lsp"
    assert lsp.calls[0]["file_path"].endswith(".py")


def test_find_symbol_skips_lsp_for_unsupported_language(fake_repo: Path, no_rg):
    lsp = FakeLsp()  # only answers .py
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=lsp)
    out = _run(tool, name="computeTotal", file_path="src/web/app.ts")
    assert out.data["engine"] == "python" and lsp.calls == []


def test_find_references_uses_lsp_only_with_position(fake_repo: Path, no_rg):
    lsp = FakeLsp()
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=lsp)
    without_line = _run(tool, name="compute_total", kind="references", file_path="src/pkg/cart.py")
    assert without_line.data["engine"] == "python"
    with_line = _run(tool, name="compute_total", kind="references", file_path="src/pkg/cart.py", line=6)
    assert with_line.data["engine"] == "lsp"
    assert lsp.calls[-1]["operation"] == "findReferences" and lsp.calls[-1]["line"] == 6


def test_find_symbol_falls_back_when_lsp_errors(fake_repo: Path, no_rg):
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=BrokenLsp())
    out = _run(tool, name="compute_total", file_path="src/pkg/cart.py")
    assert out.success and out.data["engine"] == "python"
    assert out.data["definitions"][0]["path"] == "src/pkg/cart.py"


# ─── symbol_outline ─────────────────────────────────────────


def test_outline_python_nesting(fake_repo: Path):
    tool = SymbolOutlineTool(lambda: str(fake_repo), lsp=NoLsp(), agent_id="t")
    out = _run(tool, file_path="src/pkg/cart.py")
    assert out.success and out.data["engine"] == "regex"
    syms = [(s["kind"], s["name"], s["depth"]) for s in out.data["symbols"]]
    assert syms == [
        ("function", "compute_total", 0),
        ("class", "Cart", 0),
        ("method", "__init__", 1),
        ("method", "total", 1),
        ("method", "checkout", 1),
    ]
    text = tool.render_for_llm(out)
    assert text.startswith("src/pkg/cart.py: 5 symbols (regex)")
    assert "  L6     function compute_total" in text
    assert "    method total" in text


def test_outline_typescript(fake_repo: Path):
    tool = SymbolOutlineTool(lambda: str(fake_repo), lsp=NoLsp())
    out = _run(tool, file_path="src/web/app.ts")
    names = [(s["kind"], s["name"]) for s in out.data["symbols"]]
    assert ("interface", "Item") in names
    assert ("function", "computeTotal") in names
    assert ("class", "Cart") in names
    assert ("method", "total") in names


def test_outline_other_languages(tmp_path: Path):
    go = "package main\n\ntype Cart struct{}\n\nfunc (c *Cart) Total() int { return 0 }\n\nfunc main() {}\n"
    rust = "pub struct Cart;\n\nimpl Cart {\n    pub fn total(&self) -> u32 { 0 }\n}\n\nfn main() {}\n"
    md = "# Title\n\ntext\n\n## Section\n\n### Sub\n"
    assert [(e.kind, e.name, e.depth) for e in outline_lines(go.splitlines(), "go")] == [
        ("struct", "Cart", 0),
        ("method", "Total", 1),
        ("function", "main", 0),
    ]
    assert [(e.kind, e.name) for e in outline_lines(rust.splitlines(), "rust")] == [
        ("struct", "Cart"),
        ("impl", "Cart"),
        ("method", "total"),
        ("function", "main"),
    ]
    assert [(e.kind, e.name, e.depth) for e in outline_lines(md.splitlines(), "markdown")] == [
        ("h1", "Title", 0),
        ("h2", "Section", 1),
        ("h3", "Sub", 2),
    ]
    generic = outline_lines(["module Foo", "  def bar"], None)
    assert [(e.kind, e.name) for e in generic] == [("module", "Foo"), ("method", "bar")]


def test_outline_uses_lsp_when_available(fake_repo: Path):
    lsp = FakeLsp()
    tool = SymbolOutlineTool(lambda: str(fake_repo), lsp=lsp)
    out = _run(tool, file_path="src/pkg/cart.py", max_symbols=2)
    assert out.success and out.data["engine"] == "lsp" and out.data["truncated"] is True
    assert lsp.calls[0]["operation"] == "documentSymbol"
    assert tool.render_for_llm(out).splitlines()[0].endswith("(lsp), truncated")
    ts = _run(tool, file_path="src/web/app.ts")  # FakeLsp does not serve .ts
    assert ts.data["engine"] == "regex"


def test_outline_validation(fake_repo: Path):
    tool = SymbolOutlineTool(lambda: str(fake_repo), lsp=NoLsp())
    assert not _run(tool, file_path="").success
    assert not _run(tool, file_path="src").success  # directory
    assert "outside" in _run(tool, file_path="../").error
    (fake_repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert "binary" in _run(tool, file_path="blob.bin").error
    empty = _run(tool, file_path="src/pkg/__init__.py")
    assert empty.success and "No symbols recognised" in tool.render_for_llm(empty)


# ─── review fixes: LSP probe never starts servers, bounded anchor walk ────


class _Status:
    def __init__(self, server_id: str, running: bool = True, root: str | None = None) -> None:
        self.server_id = server_id
        self.running = running
        self.root = root


class _FakeManager:
    """Stands in for LSPServerManager; fails the test if a server start is attempted."""

    def __init__(self, statuses, ext_map=None) -> None:
        self._statuses = statuses
        self.started: list[str] = []
        if ext_map is not None:
            self._extension_map = ext_map

    def get_status(self):
        return list(self._statuses)

    async def get_or_start_server(self, file_path: str):
        self.started.append(file_path)
        raise AssertionError("LspBridge must not start language servers")


def _install_manager(monkeypatch, manager):
    import openjiuwen.harness.lsp.core.manager as mod

    monkeypatch.setattr(mod.LSPServerManager, "get_instance", classmethod(lambda cls: manager))


def test_lsp_bridge_is_unavailable_without_running_servers_and_never_starts_one(monkeypatch, fake_repo: Path):
    manager = _FakeManager([], ext_map={".py": ["pyright"]})
    _install_manager(monkeypatch, manager)
    bridge = LspBridge()
    assert asyncio.run(bridge.available(fake_repo / "src" / "pkg" / "cart.py")) is False
    assert manager.started == []


def test_lsp_bridge_is_unavailable_when_no_manager(monkeypatch, fake_repo: Path):
    _install_manager(monkeypatch, None)
    assert asyncio.run(LspBridge().available(fake_repo / "src" / "pkg" / "cart.py")) is False


def test_lsp_bridge_matches_running_server_by_extension(monkeypatch, fake_repo: Path):
    manager = _FakeManager(
        [_Status("pyright", running=True), _Status("gopls", running=False)],
        ext_map={".py": ["pyright"], ".go": ["gopls"]},
    )
    _install_manager(monkeypatch, manager)
    bridge = LspBridge()
    assert asyncio.run(bridge.available(fake_repo / "src" / "pkg" / "cart.py")) is True
    assert asyncio.run(bridge.available(fake_repo / "src" / "web" / "app.ts")) is False  # no server for .ts
    go_file = fake_repo / "main.go"
    go_file.write_text("package main\n", encoding="utf-8")
    assert asyncio.run(bridge.available(go_file)) is False  # gopls known but not running
    assert manager.started == []


def test_lsp_bridge_falls_back_to_root_containment_without_extension_index(monkeypatch, fake_repo: Path, tmp_path: Path):
    manager = _FakeManager([_Status("pyright", running=True, root=str(fake_repo))])
    _install_manager(monkeypatch, manager)
    bridge = LspBridge()
    assert asyncio.run(bridge.available(fake_repo / "src" / "pkg" / "cart.py")) is True
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    assert asyncio.run(bridge.available(outside)) is False
    assert manager.started == []


def test_first_source_file_skips_ignored_dirs_and_uses_bounded_walk(fake_repo: Path):
    from jiuwenswarm.agents.harness.code.repo_tools.symbols import _first_source_file

    picked = _first_source_file(fake_repo, fake_repo, "javascript")
    # node_modules/left-pad/index.js is the only .js file and must not be chosen.
    assert picked is None
    picked = _first_source_file(fake_repo, fake_repo, "python")
    assert picked is not None and "node_modules" not in picked.parts and picked.suffix == ".py"
    assert _first_source_file(fake_repo, fake_repo, "klingon") is None


def test_find_symbol_rejects_unknown_language_and_accepts_alias(fake_repo: Path, no_rg):
    tool = FindSymbolTool(lambda: str(fake_repo), lsp=NoLsp())
    bad = _run(tool, name="compute_total", language="klingon")
    assert not bad.success and "unknown language" in bad.error
    ok = _run(tool, name="compute_total", language="py")
    assert ok.success and ok.data["language"] == "python"
    assert ok.data["definitions"][0]["path"] == "src/pkg/cart.py"
