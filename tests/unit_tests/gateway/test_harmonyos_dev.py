from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from jiuwenswarm.gateway.channel_manager.tui import harmonyos_dev
from jiuwenswarm.gateway.channel_manager.tui.harmonyos_dev import (
    CommandResult,
    _parse_node_major,
    detect_executable,
    run_harmonyos_dev_init,
    run_harmonyos_project_init,
)


@pytest.mark.asyncio
async def test_detect_executable_reports_missing_binary():
    result = await detect_executable(
        "definitely-not-a-real-harmonyos-tool",
        ["definitely-not-a-real-harmonyos-tool", "--version"],
    )

    assert result["ok"] is False
    assert result["path"] is None
    assert "not found" in result["error"]


def test_parse_node_major():
    assert _parse_node_major("v20.11.1") == 20
    assert _parse_node_major("18.0.0") == 18
    assert _parse_node_major("unknown") is None


def _write_base_skill(skills_dir: Path) -> None:
    skill_dir = skills_dir / "deveco-cli"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deveco-cli\n---\n", encoding="utf-8"
    )


def _write_harmonyos_project(project: Path) -> None:
    (project / "entry" / "src" / "main").mkdir(parents=True)
    (project / "build-profile.json5").write_text(
        "{ app: { products: [{ name: 'default' }] }, "
        "modules: [{ name: 'entry', srcPath: './entry' }] }",
        encoding="utf-8",
    )
    (project / "oh-package.json5").write_text("{ name: 'sample' }", encoding="utf-8")
    (project / "entry" / "src" / "main" / "module.json5").write_text(
        "{ module: { name: 'entry', type: 'entry', mainElement: 'EntryAbility', "
        "abilities: [{ name: 'EntryAbility' }] } }",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_project_init_returns_tui_context_without_shared_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "project"
    state_root = tmp_path / "state"
    project.mkdir()
    _write_harmonyos_project(project)

    async def fake_detect(name: str, command: list[str]) -> dict[str, object]:
        assert name == "devecocli"
        assert command == ["devecocli", "--version"]
        return {
            "ok": True,
            "path": "/opt/homebrew/bin/devecocli",
            "version": "1.2.3",
        }

    monkeypatch.setattr(harmonyos_dev, "detect_executable", fake_detect)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.tui.harmonyos_project.get_user_workspace_dir",
        lambda: state_root,
    )

    result = await run_harmonyos_project_init({"path": str(project)})

    assert result["ok"] is True
    assert result["context"]["selected"]["module"] == "entry"
    assert result["runtime"]["devecocli"]["path"] == "/opt/homebrew/bin/devecocli"
    assert "mcp" not in result
    assert Path(result["statePath"]).is_file()


@pytest.mark.asyncio
async def test_project_init_succeeds_when_devecocli_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    _write_harmonyos_project(project)

    async def fake_detect(name: str, command: list[str]) -> dict[str, object]:
        del name, command
        return {"ok": False, "path": None, "error": "devecocli not found in PATH"}

    monkeypatch.setattr(harmonyos_dev, "detect_executable", fake_detect)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.tui.harmonyos_project.get_user_workspace_dir",
        lambda: tmp_path / "state",
    )

    result = await run_harmonyos_project_init({"path": str(project)})

    assert result["ok"] is True
    assert result["runtime"]["devecocli"]["ok"] is False
    assert "mcp" not in result


@pytest.mark.asyncio
async def test_dev_init_installs_when_devecocli_missing_and_verifies_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []
    which_values = {
        "devecocli": [None, "/usr/local/bin/devecocli"],
        "node": ["/usr/local/bin/node"],
        "npm": ["/usr/local/bin/npm"],
    }

    def fake_which(name: str) -> str | None:
        values = which_values.get(name, [])
        if len(values) > 1:
            return values.pop(0)
        return values[0] if values else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/node", "--version"]:
            return CommandResult(True, command, 0, "v20.11.1\n", "")
        if command == ["/usr/local/bin/npm", "--version"]:
            return CommandResult(True, command, 0, "10.2.0\n", "")
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        if command[:3] == ["/usr/local/bin/devecocli", "init", "--skill"]:
            _write_base_skill(tmp_path)
        return CommandResult(True, command, 0, "ok\n", "")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init(
        {
            "skills_path": str(tmp_path / "ignored"),
            "installDevecocliConfirmed": True,
        }
    )

    assert result["ok"] is True
    assert result["actions"]["skillsPath"] == str(tmp_path)
    assert result["actions"]["skillsPathSource"].endswith("get_agent_skills_dir")
    assert result["actions"]["installDevecocliAttempted"] is True
    assert result["actions"]["installSuite"]["ok"] is True
    assert harmonyos_dev._devecocli_install_command("/usr/local/bin/npm") in calls
    assert [
        "/usr/local/bin/devecocli",
        "init",
        "--skill",
        "--path",
        str(tmp_path),
        "--force",
    ] in calls
    assert not (tmp_path / "ignored").exists()
    assert result["skillVerification"]["baseSkillFound"] is True
    assert result["skillVerification"]["suiteSkillFound"] is True
    assert "deveco-cli/SKILL.md" in result["skillVerification"]["skillFiles"]
    assert "harmonyos-dev-suite/SKILL.md" in result["skillVerification"]["skillFiles"]
    assert result["knowledgeMcp"] == {
        "status": "available",
        "config": {
            "name": "harmonyos_developer_knowledge",
            "enabled": True,
            "transport": "streamable-http",
            "url": "https://connect-api.cloud.huawei.com/api/developerknowledge/mcp",
            "timeout_s": 60,
        },
        "expectedTools": ["searchDocuments", "getDocumentsById"],
    }


@pytest.mark.asyncio
async def test_dev_init_requires_confirmation_before_installing_devecocli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        paths = {
            "node": "/usr/local/bin/node",
            "npm": "/usr/local/bin/npm",
        }
        return paths.get(name)

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/node", "--version"]:
            return CommandResult(True, command, 0, "v20.11.1\n", "")
        if command == ["/usr/local/bin/npm", "--version"]:
            return CommandResult(True, command, 0, "10.2.0\n", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({})

    assert result["ok"] is False
    assert result["needsConfirmation"] is True
    assert result["actions"]["installDevecocliAttempted"] is False
    assert result["actions"]["initSkillAttempted"] is False
    assert result["actions"]["installDevecocli"] == {
        "ok": False,
        "skipped": True,
        "requiresConfirmation": True,
        "reason": "user confirmation is required before global npm install",
        "command": harmonyos_dev._devecocli_install_command("/usr/local/bin/npm"),
    }
    assert all("install" not in command for command in calls)


@pytest.mark.asyncio
async def test_dev_init_does_not_continue_after_timed_out_install_even_if_cli_appears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []
    which_values = {
        "devecocli": [None, "/usr/local/bin/devecocli"],
        "node": ["/usr/local/bin/node"],
        "npm": ["/usr/local/bin/npm"],
    }

    def fake_which(name: str) -> str | None:
        values = which_values.get(name, [])
        if len(values) > 1:
            return values.pop(0)
        return values[0] if values else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/node", "--version"]:
            return CommandResult(True, command, 0, "v20.11.1\n", "")
        if command == ["/usr/local/bin/npm", "--version"]:
            return CommandResult(True, command, 0, "10.2.0\n", "")
        if command == harmonyos_dev._devecocli_install_command("/usr/local/bin/npm"):
            return CommandResult(
                False,
                command,
                None,
                "",
                "",
                "command timed out",
                timed_out=True,
            )
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({"installDevecocliConfirmed": True})

    assert result["ok"] is False
    assert result["actions"]["installDevecocli"]["timed_out"] is True
    assert (
        "timed out after 300s and was stopped"
        in result["actions"]["installDevecocli"]["error"]
    )
    assert result["actions"]["initSkillAttempted"] is False
    assert (
        "did not finish successfully" in result["actions"]["updateDevecocli"]["reason"]
    )
    assert not any(command[1:3] == ["init", "--skill"] for command in calls)


@pytest.mark.asyncio
async def test_dev_init_requires_update_confirmation_when_devecocli_is_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/devecocli" if name == "devecocli" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        return CommandResult(True, command, 0, "ok\n", "")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({})

    assert result["ok"] is False
    assert result["needsUpdateConfirmation"] is True
    assert result["actions"]["installDevecocliAttempted"] is False
    assert result["actions"]["updateDevecocliAttempted"] is False
    assert result["actions"]["updateDevecocli"] == {
        "ok": False,
        "skipped": True,
        "requiresConfirmation": True,
        "reason": "user confirmation is required before updating devecocli",
        "command": ["/usr/local/bin/devecocli", "update"],
    }
    assert result["actions"]["initSkillAttempted"] is False
    assert "node" not in result["runtime"]
    assert all(command[0] != "/usr/local/bin/npm" for command in calls)


@pytest.mark.asyncio
async def test_dev_init_updates_existing_cli_then_force_refreshes_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []
    version_calls = 0

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/devecocli" if name == "devecocli" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        nonlocal version_calls
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/devecocli", "--version"]:
            version_calls += 1
            version = "1.0.0" if version_calls == 1 else "1.1.0"
            return CommandResult(True, command, 0, f"{version}\n", "")
        if command == ["/usr/local/bin/devecocli", "update"]:
            return CommandResult(True, command, 0, "updated\n", "")
        if command[:3] == ["/usr/local/bin/devecocli", "init", "--skill"]:
            _write_base_skill(tmp_path)
            return CommandResult(True, command, 0, "installed\n", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({"updateDevecocliConfirmed": True})

    assert result["ok"] is True
    assert result["needsUpdateConfirmation"] is False
    assert result["actions"]["updateDevecocliAttempted"] is True
    assert result["runtime"]["devecocli"]["version"] == "1.1.0"
    assert ["/usr/local/bin/devecocli", "update"] in calls
    assert [
        "/usr/local/bin/devecocli",
        "init",
        "--skill",
        "--path",
        str(tmp_path),
        "--force",
    ] in calls


@pytest.mark.asyncio
async def test_dev_init_does_not_refresh_skill_when_cli_update_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/devecocli" if name == "devecocli" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        calls.append(command)
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        if command == ["/usr/local/bin/devecocli", "update"]:
            return CommandResult(False, command, 1, "", "network unavailable")
        raise AssertionError(
            f"Skill refresh must not run after update failure: {command}"
        )

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({"updateDevecocliConfirmed": True})

    assert result["ok"] is False
    assert result["actions"]["updateDevecocliAttempted"] is True
    assert result["actions"]["updateDevecocli"]["ok"] is False
    assert result["actions"]["initSkillAttempted"] is False
    assert result["actions"]["initSkill"]["skipped"] is True
    assert "update failed" in result["actions"]["initSkill"]["reason"]
    assert all("init" not in command for command in calls)


@pytest.mark.asyncio
async def test_dev_init_requires_post_update_version_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    devecocli_paths: list[str | None] = ["/usr/local/bin/devecocli", None]

    def fake_which(name: str) -> str | None:
        if name != "devecocli":
            return None
        return devecocli_paths.pop(0)

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        if command == ["/usr/local/bin/devecocli", "update"]:
            return CommandResult(True, command, 0, "updated\n", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({"updateDevecocliConfirmed": True})

    update = result["actions"]["updateDevecocli"]
    assert result["ok"] is False
    assert update["ok"] is False
    assert "could not be verified" in update["error"]
    assert update["postUpdateVerification"]["ok"] is False
    assert result["actions"]["initSkillAttempted"] is False


@pytest.mark.asyncio
async def test_dev_init_does_not_install_when_node_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", lambda name: None)

    result = await run_harmonyos_dev_init({})

    assert result["ok"] is False
    assert result["runtime"]["node"]["supported"] is False
    assert result["actions"]["installDevecocliAttempted"] is False
    assert result["actions"]["installDevecocli"]["skipped"] is True
    assert "Node.js >= 18" in result["actions"]["installDevecocli"]["reason"]


@pytest.mark.asyncio
async def test_dev_init_does_not_install_when_node_is_too_old(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/node" if name == "node" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        return CommandResult(True, command, 0, "v16.20.0\n", "")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({})

    assert result["ok"] is False
    assert result["runtime"]["node"]["major"] == 16
    assert result["runtime"]["node"]["supported"] is False
    assert "too old" in result["runtime"]["node"]["error"]


@pytest.mark.asyncio
async def test_dev_init_rejects_false_success_without_base_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/devecocli" if name == "devecocli" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        if command == ["/usr/local/bin/devecocli", "--version"]:
            return CommandResult(True, command, 0, "1.0.0\n", "")
        return CommandResult(True, command, 0, "ok\n", "")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    result = await run_harmonyos_dev_init({"skipDevecocliUpdate": True})

    assert result["ok"] is False
    assert result["actions"]["initSkill"]["ok"] is True
    assert result["skillVerification"]["baseSkillFound"] is False
    assert result["actions"]["installSuite"]["skipped"] is True


@pytest.mark.asyncio
async def test_dev_init_is_idempotent_for_existing_suite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/devecocli" if name == "devecocli" else None

    async def fake_run_command(command: list[str], *, timeout: float) -> CommandResult:
        del timeout
        if command[:3] == ["/usr/local/bin/devecocli", "init", "--skill"]:
            _write_base_skill(tmp_path)
        return CommandResult(True, command, 0, "1.0.0\n", "")

    monkeypatch.setattr(harmonyos_dev, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(harmonyos_dev.shutil, "which", fake_which)
    monkeypatch.setattr(harmonyos_dev, "run_command", fake_run_command)

    first = await run_harmonyos_dev_init({"skipDevecocliUpdate": True})
    second = await run_harmonyos_dev_init({"skipDevecocliUpdate": True})

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["actions"]["installSuite"]["alreadyInstalled"] is True
    assert second["actions"]["installSuite"]["managed"] is True
    assert second["actions"]["installSuite"]["sourceDigest"]
    assert second["skillVerification"]["newSkillFiles"] == []


def test_install_suite_rejects_unmanaged_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    target_root = tmp_path / "skills"
    target = target_root / "harmonyos-dev-suite"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "SKILL.md").write_text("# managed source\n", encoding="utf-8")
    (target / "SKILL.md").write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setattr(
        harmonyos_dev, "_builtin_harmonyos_dev_suite_dir", lambda: source
    )

    result = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)

    assert result["ok"] is False
    assert result["conflict"] is True
    assert "refusing to overwrite" in result["error"]


def test_install_suite_atomically_upgrades_unmodified_managed_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    target_root = tmp_path / "skills"
    source.mkdir()
    target_root.mkdir()
    (source / "SKILL.md").write_text("# version one\n", encoding="utf-8")
    monkeypatch.setattr(
        harmonyos_dev, "_builtin_harmonyos_dev_suite_dir", lambda: source
    )

    first = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)
    (source / "SKILL.md").write_text("# version two\n", encoding="utf-8")
    second = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["updated"] is True
    assert second["previousDigest"] != second["sourceDigest"]
    assert (target_root / "harmonyos-dev-suite" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# version two\n"


def test_install_suite_rejects_modified_managed_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    target_root = tmp_path / "skills"
    source.mkdir()
    target_root.mkdir()
    (source / "SKILL.md").write_text("# managed source\n", encoding="utf-8")
    monkeypatch.setattr(
        harmonyos_dev, "_builtin_harmonyos_dev_suite_dir", lambda: source
    )

    first = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)
    target_skill = target_root / "harmonyos-dev-suite" / "SKILL.md"
    target_skill.write_text("# user-modified copy\n", encoding="utf-8")
    second = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["conflict"] is True
    assert target_skill.read_text(encoding="utf-8") == "# user-modified copy\n"


def test_install_suite_upgrades_historical_official_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source_v1 = tmp_path / "source-v1"
    source_v2 = tmp_path / "source-v2"
    target_root = tmp_path / "skills"
    target = target_root / "harmonyos-dev-suite"
    source_v1.mkdir()
    source_v2.mkdir()
    target_root.mkdir()
    (source_v1 / "SKILL.md").write_text("# official v1\n", encoding="utf-8")
    (source_v1 / "notes.txt").write_text("v1 payload\n", encoding="utf-8")
    (source_v2 / "SKILL.md").write_text("# official v2\n", encoding="utf-8")
    (source_v2 / "notes.txt").write_text("v2 payload\n", encoding="utf-8")

    # Simulate a pre-metadata official install (e.g. 88aafed users who skipped
    # intermediate managed releases).
    shutil.copytree(source_v1, target)
    v1_digest = harmonyos_dev._suite_tree_digest(target)
    monkeypatch.setattr(
        harmonyos_dev,
        "HARMONYOS_DEV_SUITE_HISTORICAL_OFFICIAL_DIGESTS",
        frozenset({v1_digest}),
    )
    monkeypatch.setattr(
        harmonyos_dev, "_builtin_harmonyos_dev_suite_dir", lambda: source_v2
    )

    result = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["historicalOfficial"] is True
    assert result["previousDigest"] == v1_digest
    assert result["sourceDigest"] != v1_digest
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# official v2\n"
    metadata = json.loads(
        (target / harmonyos_dev.HARMONYOS_DEV_SUITE_METADATA).read_text(
            encoding="utf-8"
        )
    )
    assert metadata["sourceDigest"] == result["sourceDigest"]
    assert metadata["name"] == "harmonyos-dev-suite"


def test_install_suite_rejects_unknown_unmanaged_tree_even_with_skill_md(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    target_root = tmp_path / "skills"
    target = target_root / "harmonyos-dev-suite"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "SKILL.md").write_text("# built-in v2\n", encoding="utf-8")
    (target / "SKILL.md").write_text("# user custom suite\n", encoding="utf-8")
    monkeypatch.setattr(
        harmonyos_dev, "HARMONYOS_DEV_SUITE_HISTORICAL_OFFICIAL_DIGESTS", frozenset()
    )
    monkeypatch.setattr(
        harmonyos_dev, "_builtin_harmonyos_dev_suite_dir", lambda: source
    )

    result = harmonyos_dev.install_builtin_harmonyos_dev_suite(target_root)

    assert result["ok"] is False
    assert result["conflict"] is True
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# user custom suite\n"


@pytest.mark.asyncio
async def test_run_command_bounds_captured_output():
    output_size = harmonyos_dev.MAX_COMMAND_OUTPUT_BYTES + 4096

    result = await harmonyos_dev.run_command(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {output_size})"],
        timeout=10,
    )

    assert result.ok is True
    assert len(result.stdout.encode("utf-8")) < output_size
    assert "output bytes truncated" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX fake npm executable")
@pytest.mark.asyncio
async def test_install_devecocli_stops_silent_fake_npm_with_actionable_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_npm = tmp_path / "npm"
    fake_npm.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_npm.chmod(0o755)
    monkeypatch.setattr(harmonyos_dev, "INSTALL_TIMEOUT_SECONDS", 0.2)

    result = await harmonyos_dev.install_devecocli(str(fake_npm))

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["command"] == harmonyos_dev._devecocli_install_command(str(fake_npm))
    assert "was stopped" in result["error"]
    assert "npm ping" in result["error"]


@pytest.mark.asyncio
async def test_run_command_timeout_terminates_descendants(tmp_path: Path):
    sentinel = tmp_path / "orphan-finished"
    child_code = (
        "import pathlib,time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(sentinel)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )

    result = await harmonyos_dev.run_command(
        [sys.executable, "-c", parent_code],
        timeout=0.5,
    )
    await asyncio.sleep(1.1)

    assert result.ok is False
    assert result.timed_out is True
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_run_command_cancellation_terminates_descendants(tmp_path: Path):
    sentinel = tmp_path / "cancelled-orphan-finished"
    child_code = (
        "import pathlib,time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(sentinel)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    command_task = asyncio.create_task(
        harmonyos_dev.run_command(
            [sys.executable, "-c", parent_code],
            timeout=30,
        )
    )
    await asyncio.sleep(0.2)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command_task
    await asyncio.sleep(1.1)

    assert not sentinel.exists()
