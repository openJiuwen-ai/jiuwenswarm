from __future__ import annotations

from pathlib import Path

import pytest

from scripts import generate_build_info


COMMIT = "a" * 40
CORE_COMMIT = "b" * 40


def test_ci_merge_uses_target_branch_instead_of_pull_request_branch(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_UPSTREAM_BRANCH", raising=False)
    target_commit = "c" * 40

    def fake_git(*args: str) -> str:
        if args[:3] == ("show", "-s", "--format=%P"):
            return f"{target_commit} {COMMIT}"
        if args[0] == "for-each-ref":
            return f"origin/develop {target_commit}\norigin/HEAD {target_commit}"
        if args[:2] == ("check-ref-format", "--branch"):
            return "develop"
        raise AssertionError(f"Unexpected Git call: {args}")

    monkeypatch.setattr(generate_build_info, "_git", fake_git)
    assert generate_build_info.upstream_branch() == "develop"


def test_generate_reads_upstream_branch_and_locked_core(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.2.8"\n', encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openjiuwen"\nversion = "0.1.20"\n'
        f'source = {{ git = "https://example.test/agent-core.git?rev={CORE_COMMIT}#{CORE_COMMIT}" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_build_info, "ROOT", tmp_path)
    monkeypatch.setenv("JIUWENSWARM_UPSTREAM_BRANCH", "0.2.8")
    monkeypatch.setenv("JIUWENSWARM_AGENT_CORE_BRANCH", "release/v0.1.20")
    commands = []

    def fake_git(*args: str) -> str:
        commands.append(args)
        if args[:2] == ("check-ref-format", "--branch"):
            return "0.2.8"
        if args[0] == "ls-remote":
            return f"{COMMIT}\trefs/heads/0.2.8"
        raise AssertionError(f"Unexpected Git call: {args}")

    monkeypatch.setattr(generate_build_info, "_git", fake_git)

    payload = generate_build_info.generate()

    assert payload["jiuwenswarm"]["commit_id"] == COMMIT
    assert "repository" not in payload["jiuwenswarm"]
    assert payload["agent_core"] == {
        "source": "git",
        "version": "0.1.20",
        "commit_id": CORE_COMMIT,
        "branch": "release/v0.1.20",
    }
    assert all(args[0] != "rev-parse" for args in commands)


def test_git_core_without_branch_fails_instead_of_omitting_it(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openjiuwen"\nversion = "0.1.19"\n'
        f'source = {{ git = "https://example.test/agent-core.git?rev={CORE_COMMIT}#{CORE_COMMIT}" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_build_info, "ROOT", tmp_path)
    monkeypatch.delenv("JIUWENSWARM_AGENT_CORE_BRANCH", raising=False)

    with pytest.raises(ValueError, match="JIUWENSWARM_AGENT_CORE_BRANCH"):
        generate_build_info.locked_agent_core(require_branch=True)

    assert "branch" not in generate_build_info.locked_agent_core()


def test_failed_generation_removes_previous_manifest(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "build-info.json"
    output.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(generate_build_info, "OUTPUT", output)
    monkeypatch.setattr(
        generate_build_info, "generate", lambda **_kwargs: (_ for _ in ()).throw(ValueError("upstream unavailable")),
    )

    with pytest.raises(ValueError, match="upstream unavailable"):
        generate_build_info.main()

    assert not output.exists()


def test_electron_macos_build_generates_manifest_before_pyinstaller() -> None:
    script = (generate_build_info.ROOT / "scripts" / "build-electron-exe.sh").read_text(encoding="utf-8")
    full_build = script.split('if [ "$FRONTEND_ONLY" = false ]; then\n    echo ""\n    echo "[4/6]', 1)[1]
    assert full_build.index("--require-agent-core-branch") < full_build.index("uv run pyinstaller")


def test_electron_windows_build_requires_core_branch() -> None:
    script = (generate_build_info.ROOT / "scripts" / "build-electron-exe.ps1").read_text(encoding="utf-8")
    assert 'if (-not $FrontendOnly) {\n    uv run --no-project --python 3.11 python "$ProjectRoot\\scripts\\generate_build_info.py" --require-agent-core-branch' in script
