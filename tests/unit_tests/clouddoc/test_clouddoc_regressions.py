# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 3 small test modules)."""

from __future__ import annotations

import pathlib
import ast
import pytest
from jiuwenswarm.clouddoc.providers.base import DocProvider


# --- from test_dark.py ---

REPO = pathlib.Path(__file__).resolve().parents[3]

def test_no_caller_outside_the_package_yet():
    offenders = []
    for root in (REPO / "jiuwenswarm", REPO / "tests"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(("jiuwenswarm/clouddoc/", "tests/unit_tests/clouddoc/")):
                continue
            if "jiuwenswarm.clouddoc" in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(rel)
    assert offenders == []

# --- from test_layering.py ---

ROOT = pathlib.Path(__file__).resolve().parents[3] / "jiuwenswarm" / "clouddoc"

PKG = "jiuwenswarm.clouddoc."

LAYERS = {
    "wording": 0, "settings": 0,
    "providers": 1,
    "edits": 2, "receipts": 2, "workmode": 2,
    "tools": 3,
    "authority": 4, "state": 4,
    "watch": 5,
    "panel": 6,
    "host": 7,
}

PROVIDER_SUBLAYERS = {"base": 0, "kinds": 0, "textmap": 0, "formats": 0, "google": 1, "feishu": 1, "factory": 2, "routing": 2}

HOST_PREFIXES = ("jiuwenswarm.", "openjiuwen")

MAY_IMPORT_HOST = ("settings", "host")

def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
        if rel.endswith("__init__"):
            rel = rel[: -len(".__init__")] or "__init__"
        yield rel, path

def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name

def _layer(rel):
    return LAYERS.get(rel.split(".")[0])

def _provider_sublayer(rel):
    parts = rel.split(".")
    return PROVIDER_SUBLAYERS.get(parts[1]) if parts[0] == "providers" and len(parts) > 1 else None

@pytest.mark.parametrize("rel, path", [m for m in _modules() if m[0] != "__init__"])
def test_a_module_imports_only_from_its_layer_or_below(rel, path):
    mine = _layer(rel)
    assert mine is not None, f"{rel}: not assigned to a layer in this test"
    for mod in _imports(path):
        if mod.startswith(PKG):
            target = mod[len(PKG):]
            theirs = _layer(target)
            assert theirs is not None, f"{rel} imports {mod}, which has no layer"
            assert theirs <= mine, f"{rel} (layer {mine}) imports upward from {target} (layer {theirs})"
            if mine == theirs == LAYERS["providers"]:
                a, b = _provider_sublayer(rel), _provider_sublayer(target)
                if a is not None and b is not None:
                    assert b <= a, f"{rel} imports upward within providers from {target}"
        elif mod.startswith(HOST_PREFIXES) and not mod.startswith("jiuwenswarm.clouddoc"):
            assert rel.split(".")[0] in MAY_IMPORT_HOST, f"{rel} imports the host application ({mod}); only settings and host may"

# --- from test_provider_contract.py ---

def test_the_abstract_base_still_declares_what_it_used_to():
    """A method quietly dropped from DocProvider would take its implementations'
    obligation with it, and nothing would fail.
    """
    for name in ("read", "edit_batch", "list_comments", "reply_comment", "capabilities"):
        assert hasattr(DocProvider, name), f"DocProvider 少了 {name}"

