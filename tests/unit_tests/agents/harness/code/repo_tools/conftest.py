# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared fixtures for the repo-aware tool tests.

``fake_repo`` builds a small multi-language checkout (no ``.git``) with a
``.gitignore``, a vendored directory and a test directory so ranking, ignore
handling and language filters can be asserted deterministically.

``rg_bin`` locates a ripgrep binary the same way the tools do; tests that need
ripgrep skip when none is available, and the pure-Python engine is exercised
regardless by hiding ripgrep.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.repo_tools._common import find_rg_binary


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "web").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "vendor").mkdir()
    (root / "docs").mkdir()

    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("*.log\nsecret/\n", encoding="utf-8")
    (root / "debug.log").write_text("compute_total leaked into a log\n", encoding="utf-8")
    (root / "secret").mkdir()
    (root / "secret" / "keys.py").write_text("compute_total = 'nope'\n", encoding="utf-8")
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "cart.py").write_text(
        "import math\n"
        "\n"
        "MAX_ITEMS = 10\n"
        "\n"
        "\n"
        "def compute_total(items):\n"
        "    return sum(items)\n"
        "\n"
        "\n"
        "class Cart:\n"
        "    def __init__(self):\n"
        "        self.items = []\n"
        "\n"
        "    def total(self):\n"
        "        return compute_total(self.items)\n"
        "\n"
        "    async def checkout(self):\n"
        "        return self.total()\n",
        encoding="utf-8",
    )
    (root / "src" / "pkg" / "pricing.py").write_text(
        "from src.pkg.cart import compute_total\n\n\ndef price(items):\n    return compute_total(items) * 1.2\n",
        encoding="utf-8",
    )
    (root / "src" / "web" / "app.ts").write_text(
        "export interface Item { price: number }\n"
        "export function computeTotal(items: Item[]): number {\n"
        "  return items.reduce((a, b) => a + b.price, 0);\n"
        "}\n"
        "export const TAX = 0.2;\n"
        "export class Cart {\n"
        "  items: Item[] = [];\n"
        "  total(): number { return computeTotal(this.items); }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "src" / "web" / "package.json").write_text(
        '{"name": "web", "scripts": {"test": "vitest run", "build": "tsc"}}\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_cart.py").write_text(
        "from src.pkg.cart import compute_total\n\n\ndef test_total():\n    assert compute_total([1, 2]) == 3\n",
        encoding="utf-8",
    )
    (root / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = function compute_total() {}\n", encoding="utf-8"
    )
    (root / "vendor" / "lib.py").write_text("def compute_total():\n    pass\n", encoding="utf-8")
    (root / "docs" / "README.md").write_text("# Demo\n\ncompute_total docs\n", encoding="utf-8")
    (root / "Makefile").write_text("test:\n\tpytest -q\n\nlint:\n\truff check .\n", encoding="utf-8")
    return root


@pytest.fixture
def rg_bin() -> str | None:
    return find_rg_binary()


@pytest.fixture
def no_rg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide every ripgrep so the pure-Python engine is used."""
    monkeypatch.delenv("OPENJIUWEN_RG", raising=False)
    monkeypatch.delenv("ICODE_RG", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(["/nonexistent-bin"]))
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.repo_tools._common.find_rg_binary",
        lambda explicit=None: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.repo_tools.code_search.find_rg_binary",
        lambda explicit=None: None,
    )
