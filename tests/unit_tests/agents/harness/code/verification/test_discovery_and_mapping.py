# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""discover_tests and map_files_to_tests."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.code.verification.discovery import (
    GO,
    NATIVE_RUST,
    PYTHON,
    TS_JS,
    discover_tests,
    language_of,
)
from jiuwenswarm.agents.harness.code.verification.mapping import (
    is_test_file,
    map_files_to_tests,
)

from .conftest import write


@pytest.mark.parametrize(
    ("rel", "text", "source"),
    [
        ("pytest.ini", "[pytest]\n", "pytest.ini"),
        ("pyproject.toml", "[project]\nname='x'\n[tool.pytest.ini_options]\naddopts='-q'\n", "pyproject.toml"),
        ("tox.ini", "[pytest]\n", "tox.ini"),
        ("setup.cfg", "[tool:pytest]\n", "setup.cfg"),
        ("conftest.py", "", "conftest.py"),
    ],
)
def test_pytest_detected_from_config(tmp_path, rel, text, source):
    write(tmp_path, rel, text)
    fw = discover_tests(tmp_path).for_language(PYTHON)
    assert fw is not None and fw.name == "pytest" and fw.config_file == source


def test_pytest_detected_from_test_files_and_unittest_fallback(tmp_path):
    a = tmp_path / "a"
    write(a, "tests/test_x.py", "def test_x():\n    pass\n")
    assert discover_tests(a).for_language(PYTHON).name == "pytest"
    b = tmp_path / "b"
    write(b, "setup.py", "from setuptools import setup\nsetup()\n")
    assert discover_tests(b).for_language(PYTHON).name == "unittest"


@pytest.mark.parametrize(
    ("pkg", "name"),
    [
        ({"devDependencies": {"vitest": "1"}}, "vitest"),
        ({"devDependencies": {"jest": "29"}}, "jest"),
        ({"scripts": {"test": "mocha"}}, "npm"),
    ],
)
def test_node_frameworks(tmp_path, pkg, name):
    write(tmp_path, "package.json", json.dumps(pkg))
    fw = discover_tests(tmp_path).for_language(TS_JS)
    assert fw is not None and fw.name == name


def test_placeholder_npm_test_script_is_not_a_framework(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert discover_tests(tmp_path).frameworks == ()


def test_go_cargo_and_empty(tmp_path):
    write(tmp_path / "g", "go.mod", "module x\n")
    write(tmp_path / "r", "Cargo.toml", "[package]\nname='x'\n")
    assert discover_tests(tmp_path / "g").for_language(GO).name == "go"
    assert discover_tests(tmp_path / "r").for_language(NATIVE_RUST).name == "cargo"
    (tmp_path / "e").mkdir()
    assert discover_tests(tmp_path / "e").frameworks == ()


def test_discovery_cache_invalidates_on_config_change(tmp_path):
    write(tmp_path, "setup.py", "")
    assert discover_tests(tmp_path).for_language(PYTHON).name == "unittest"
    write(tmp_path, "pytest.ini", "[pytest]\n")
    assert discover_tests(tmp_path).for_language(PYTHON).name == "pytest"


def test_language_of_and_is_test_file():
    assert language_of("a/b.py") == PYTHON and language_of("x.tsx") == TS_JS
    assert language_of("README.md") is None
    assert is_test_file("tests/test_a.py") and is_test_file("a_test.py")
    assert is_test_file("pkg/x_test.go") and is_test_file("src/a.spec.ts")
    assert is_test_file("src/__tests__/a.ts") and not is_test_file("src/a.ts")


def test_map_same_name_and_reverse_imports(py_repo):
    write(py_repo, "tests/test_other.py", "from calc import ops\n\ndef test_o():\n    assert ops.add(1, 1) == 2\n")
    write(py_repo, "tests/test_unrelated.py", "def test_u():\n    pass\n")
    mapped = map_files_to_tests(py_repo, ["calc/ops.py"])
    assert set(mapped[PYTHON]) == {"tests/test_ops.py", "tests/test_other.py"}


def test_map_src_layout_and_absolute_paths(tmp_path):
    write(tmp_path, "src/pkg/core.py", "X = 1\n")
    write(tmp_path, "tests/test_things.py", "from pkg.core import X\n")
    mapped = map_files_to_tests(tmp_path, [str(tmp_path / "src/pkg/core.py"), "/elsewhere/outside.py"])
    assert mapped == {PYTHON: ["tests/test_things.py"]}


def test_map_test_file_itself_ts_spec_and_go_package(tmp_path):
    write(tmp_path, "tests/test_a.py", "")
    write(tmp_path, "web/button.ts", "")
    write(tmp_path, "web/button.spec.ts", "")
    write(tmp_path, "svc/handler.go", "package svc\n")
    mapped = map_files_to_tests(tmp_path, ["tests/test_a.py", "web/button.ts", "svc/handler.go", "main.go"])
    assert mapped[PYTHON] == ["tests/test_a.py"]
    assert mapped[TS_JS] == ["web/button.spec.ts"]
    assert mapped[GO] == ["./svc", "./"]


def test_map_nothing_for_uncovered_and_non_source(py_repo):
    assert map_files_to_tests(py_repo, ["calc/util.py", "README.md"]) == {}
