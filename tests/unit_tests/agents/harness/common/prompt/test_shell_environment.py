# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""shell_environment：运行时库探测、双语文案与 heredoc 条件化。"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.prompt import shell_environment as se
from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail as rpr


@pytest.fixture(autouse=True)
def _clear_library_cache():
    se.build_python_library_prompt.cache_clear()
    yield
    se.build_python_library_prompt.cache_clear()


def _patch_probes(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    monkeypatch.setattr(se, "_spec_available", lambda module: module in available)


def _patch_windows_shells(
    monkeypatch: pytest.MonkeyPatch, git_bash: str | None
) -> None:
    monkeypatch.setattr(se.shutil, "which", lambda _name: None)
    monkeypatch.setattr(se, "_available_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(se, "_available_git_bash", lambda: git_bash)


def test_spec_available_probes_real_modules() -> None:
    assert se._spec_available("json") is True
    assert se._spec_available("definitely_not_a_module_xyz") is False


def test_cn_lists_only_detected_libraries(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, {"docx", "openpyxl", "fitz"})

    text = se.build_python_library_prompt("cn")

    assert "python-docx" in text
    assert "openpyxl" in text
    assert "PyMuPDF 可用" in text
    assert "pandas" not in text


def test_cn_fitz_missing_recommends_only_detected_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-1：兜底建议必须来自探测结果，不能无条件推荐 pypdf/pdfplumber。"""
    _patch_probes(monkeypatch, {"docx", "pypdf"})

    text = se.build_python_library_prompt("cn")

    assert "改用 pypdf" in text
    assert "pdfplumber" not in text


def test_cn_nothing_detected_makes_no_promise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, set())

    text = se.build_python_library_prompt("cn")

    assert "未探测到 fitz/PyMuPDF、pypdf、pdfplumber" in text
    assert "未探测到预置的常用库" in text
    assert "改用" not in text


def test_en_empty_probe_is_self_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, set())

    text = se.build_python_library_prompt("en")

    assert "No preset Office library detected" in text
    assert "list above" not in text


def test_shell_prompt_can_drop_library_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-4：库清单只走一条注入链路，运行时链路可显式关闭。"""
    _patch_probes(monkeypatch, {"docx"})
    _patch_windows_shells(monkeypatch, git_bash=None)

    with_libs = se.build_shell_environment_prompt("cn", "win32")
    without_libs = se.build_shell_environment_prompt("cn", "win32", include_libraries=False)

    assert "python-docx" in with_libs
    assert "python-docx" not in without_libs
    assert "Shell 能力" in without_libs


def test_heredoc_rule_follows_git_bash_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-5：有 Git Bash 时不应一刀切禁止 heredoc。"""
    _patch_windows_shells(monkeypatch, git_bash=None)
    assert "不要用 heredoc 写文件" in se.build_shell_environment_prompt("cn", "win32")

    _patch_windows_shells(monkeypatch, git_bash="C:/Git/bin/bash.exe")
    text = se.build_shell_environment_prompt("cn", "win32")

    assert "heredoc 仅在 bash/Git Bash 中使用" in text
    assert "不要用 heredoc 写文件" not in text


class _FakeSection:
    def __init__(self, content: object) -> None:
        self.content = content


class _FakeBuilder:
    def __init__(self, sections: dict[str, _FakeSection]) -> None:
        self._sections = sections

    def get_section(self, name: str) -> _FakeSection | None:
        return self._sections.get(name)


def test_identity_section_keeps_library_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-4：identity 已带清单时，运行时链路不再重复注入。"""
    _patch_probes(monkeypatch, {"docx"})
    builder = _FakeBuilder({
        "identity": _FakeSection({"cn": "…… Python 库（运行时探测，仅列已探测到的）： python-docx ……"}),
    })

    assert rpr._prompt_has_library_list(builder) is True


def test_missing_identity_section_lets_env_section_add_list() -> None:
    """code 模式没有 identity 段，运行时链路必须补注入。"""
    assert rpr._prompt_has_library_list(_FakeBuilder({})) is False
    assert rpr._prompt_has_library_list(object()) is False
