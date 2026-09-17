# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""build_security_review_prompt 单元测试."""

import shutil
import subprocess

import pytest

from jiuwenswarm.gateway.message_handler.prompts.security_review_prompt import (
    GitPreExecError,
    _run_security_review_git,
    build_security_review_prompt,
)

# 用绝对路径调用 git，避免依赖 PATH 解析（G.EDV.05）。
_GIT = shutil.which("git") or "git"


@pytest.fixture(autouse=True)
def _english_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.prompts.get_config",
        lambda: {"preferred_language": "en"},
    )


def test_build_security_review_prompt_runs_git_commands_in_english() -> None:
    # cwd=None → 旧的“指令式” prompt（让 LLM 自己跑 git），行为不变。
    prompt = build_security_review_prompt("")
    assert "`git status`" in prompt
    assert "`git diff --name-only origin/HEAD...`" in prompt
    assert "`git log --no-decorate origin/HEAD...`" in prompt
    assert "`git diff origin/HEAD...`" in prompt
    assert "Respond in English." in prompt
    assert "HIGH-CONFIDENCE security vulnerabilities" in prompt
    assert "FALSE POSITIVE FILTERING" in prompt
    assert "Additional instructions:" not in prompt
    assert "`gh pr" not in prompt


def test_build_security_review_prompt_passes_through_args() -> None:
    prompt = build_security_review_prompt("focus on auth bypass")
    assert prompt.rstrip().endswith("Additional instructions: focus on auth bypass")
    assert "Respond in English." in prompt


def test_build_security_review_prompt_chinese_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.prompts.get_config",
        lambda: {"preferred_language": "zh"},
    )
    prompt = build_security_review_prompt("")
    assert "Respond in Chinese (simplified)." in prompt


# ---- cwd 启用预执行 + 失败中止 ----


def _make_repo(path: str) -> None:
    """建一个带 origin/HEAD 指向的本地 git 仓库（用本地 bare 仓库当 origin）。"""
    subprocess.run([_GIT, "init", "-q", path], check=True)
    subprocess.run([_GIT, "-C", path, "config", "user.email", "t@t.t"], check=True)
    subprocess.run([_GIT, "-C", path, "config", "user.name", "t"], check=True)
    subprocess.run([_GIT, "-C", path, "config", "commit.gpgsign", "false"], check=True)
    subprocess.run([_GIT, "-C", path, "commit", "--allow-empty", "-m", "init", "-q"], check=True)


def _add_origin_head(path: str, bare: str) -> None:
    """给仓库加一个 origin 远端（本地 bare）并把 HEAD 设上去。

    bare 仓库路径由调用方提供（建议放在 pytest 的 tmp_path 下，由其自动清理，
    避免 tempfile.mkdtemp 泄漏临时目录）。
    """
    subprocess.run([_GIT, "init", "--bare", "-q", bare], check=True)
    subprocess.run([_GIT, "-C", path, "remote", "add", "origin", bare], check=True)
    subprocess.run([_GIT, "-C", path, "push", "-q", "origin", "HEAD"], check=True)
    # 先 fetch 建立 refs/remotes/origin/* ，再用显式分支名 set-head
    # （`set-head origin HEAD` 在某些 git 版本会静默不写 symref）。
    subprocess.run([_GIT, "-C", path, "fetch", "-q", "origin"], check=True)
    subprocess.run([_GIT, "-C", path, "remote", "set-head", "origin", "master"], check=True)


def test_run_security_review_git_succeeds_when_origin_head_set(tmp_path):
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _add_origin_head(repo, str(tmp_path / "bare"))
    # 当前分支无改动：4 条命令仍应 exit 0（git diff A...B 有/无 diff 都 exit 0）。
    out = _run_security_review_git(repo)
    assert set(out) == {"GIT STATUS", "FILES MODIFIED", "COMMITS", "DIFF CONTENT"}


def test_run_security_review_git_aborts_without_origin_head(tmp_path):
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    # 不设 origin/HEAD → git diff origin/HEAD... exit 128 → 预执行中止。
    with pytest.raises(GitPreExecError):
        _run_security_review_git(repo)


def test_build_security_review_prompt_inlines_git_outputs(tmp_path):
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _add_origin_head(repo, str(tmp_path / "bare"))
    # 制造一个改动，让 DIFF CONTENT 非空。
    (tmp_path / "repo" / "f.txt").write_text("hello\n")
    subprocess.run([_GIT, "-C", repo, "add", "f.txt"], check=True)
    subprocess.run([_GIT, "-C", repo, "commit", "-m", "add f", "-q"], check=True)

    prompt = build_security_review_prompt("", cwd=repo)
    # 内联段落存在
    assert "GIT STATUS:" in prompt
    assert "FILES MODIFIED:" in prompt
    assert "COMMITS:" in prompt
    assert "DIFF CONTENT:" in prompt
    # 不再让 LLM 自己跑 git（输出已内联）
    assert "Run `git status`" not in prompt
    # diff 内容已内联
    assert "hello" in prompt
    # 仍带语言行与分析正文
    assert "Respond in English." in prompt
    assert "FALSE POSITIVE FILTERING" in prompt


def test_build_security_review_prompt_aborts_on_git_failure(tmp_path):
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    # 无 origin/HEAD → 预执行抛错，prompt 不会被构建。
    with pytest.raises(GitPreExecError):
        build_security_review_prompt("", cwd=repo)


def test_build_security_review_prompt_inlined_passes_through_args(tmp_path):
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _add_origin_head(repo, str(tmp_path / "bare"))
    prompt = build_security_review_prompt("focus on auth bypass", cwd=repo)
    assert prompt.rstrip().endswith("Additional instructions: focus on auth bypass")
    assert "GIT STATUS:" in prompt
