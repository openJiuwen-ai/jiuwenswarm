# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CliDriver + SkillInstaller (form C, CLI connectors).

The CliDriver flow (install/version/auth/status) is validated with an
injectable fake runner so no npm/network/OAuth is required. The
SkillInstaller is validated against a temp workspace marketplace so file
copying + enable flipping is exercised for real.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp.cli_driver import (
    AuthStepResult,
    CliDriver,
    CliManifest,
    CommandResult,
    StatusResult,
    _extract_url,
    _parse_version,
    _version_ge,
)


def _mkmanifest() -> CliManifest:
    return CliManifest(
        runtime_type="node",
        runtime_version=">=18",
        init_cmd="npm install -g @larksuite/cli",
        version_cmd="lark-cli.cmd --version",
        min_version="1.0.79",
        auth_steps=[
            {
                "command": {"win32": "lark-cli.cmd config init --new --lang en"},
                "skipIf": {"win32": "lark-cli.cmd config show"},
                "authWaitForExit": True,
                "authUrlDomain": "open.feishu.cn",
            },
            {
                "command": {"win32": "lark-cli.cmd auth login --recommend"},
                "authWaitForExit": True,
                "authUrlDomain": "accounts.feishu.cn",
            },
        ],
        unauth_cmd="lark-cli.cmd auth logout",
        status_cmd="lark-cli.cmd auth status",
        status_match={"identity": "user"},
    )


class _FakeRunner:
    def __init__(self, responses: dict[str, CommandResult]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def __call__(self, command: str) -> CommandResult:
        self.calls.append(command)
        return self.responses.get(
            command, CommandResult(command=command, returncode=1, stderr="unknown")
        )



class _FakeAuthProc:
    """Fake auth proc: running (poll None) with stashed initial output."""
    def __init__(self, initial_output: str) -> None:
        self._initial = initial_output
        self._rc: int | None = None

    def poll(self) -> int | None:
        return self._rc

    def set_done(self) -> None:
        self._rc = 0

class TestVersionUtils:
    def test_parse_version(self) -> None:
        assert _parse_version("lark-cli 1.0.79 build 123") == "1.0.79"
        assert _parse_version("no version here") is None

    def test_version_ge(self) -> None:
        assert _version_ge("1.0.79", "1.0.77") is True
        assert _version_ge("1.0.70", "1.0.77") is False


class TestExtractUrl:
    def test_prefers_domain_hint(self) -> None:
        text = "visit https://accounts.feishu.cn/abc and also https://open.feishu.cn/x"
        assert _extract_url(text, "accounts.feishu.cn") == "https://accounts.feishu.cn/abc"

    def test_no_url(self) -> None:
        assert _extract_url("plain text", "x") is None


class TestCliDriverInstall:
    def test_install_success_version_ok(self) -> None:
        runner = _FakeRunner({
            "npm install -g @larksuite/cli": CommandResult(
                "npm install -g @larksuite/cli", 0, stdout="installed"
            ),
            "lark-cli.cmd --version": CommandResult(
                "lark-cli.cmd --version", 0, stdout="lark-cli 1.0.79"
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.install()
        assert res.installed is True
        assert res.version == "1.0.79"
        assert res.version_ok is True
        assert res.error == ""

    def test_install_version_too_low(self) -> None:
        runner = _FakeRunner({
            "npm install -g @larksuite/cli": CommandResult(
                "npm install -g @larksuite/cli", 0
            ),
            "lark-cli.cmd --version": CommandResult(
                "lark-cli.cmd --version", 0, stdout="lark-cli 1.0.70"
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.install()
        assert res.version_ok is False


    def test_install_skips_init_when_version_ok(self) -> None:
        runner = _FakeRunner({
            "lark-cli.cmd --version": CommandResult(
                "lark-cli.cmd --version", 0, stdout="lark-cli 1.0.90"
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.install()
        assert res.version_ok is True
        assert res.version == "1.0.90"
        # init (npm install) must NOT run when versionCheck already passes
        assert "npm install -g @larksuite/cli" not in runner.calls

class TestCliDriverAuth:
    def test_auth_step_skipif_skips(self) -> None:
        runner = _FakeRunner({
            "lark-cli.cmd config show": CommandResult("lark-cli.cmd config show", 0),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        r = drv.auth_step(0)
        assert r.succeeded is True
        assert r.needs_user_action is False

    def test_auth_step_extracts_url(self) -> None:
        out = "open https://accounts.feishu.cn/login?token=abc to authorize"
        proc = _FakeAuthProc(out)
        drv = CliDriver("feishu", _mkmanifest(), _FakeRunner({}), proc_runner=lambda cmd: (proc, out))
        r = drv.auth_step(1)
        assert r.succeeded is True
        assert r.needs_user_action is True
        assert r.auth_url == "https://accounts.feishu.cn/login?token=abc"
        assert r.auth_domain == "accounts.feishu.cn"

    def test_auth_step_out_of_range(self) -> None:
        drv = CliDriver("feishu", _mkmanifest(), _FakeRunner({}))
        r = drv.auth_step(99)
        assert r.succeeded is False
        assert "out of range" in r.error


class TestCliDriverStatus:
    def test_status_authenticated(self) -> None:
        runner = _FakeRunner({
            "lark-cli.cmd auth status": CommandResult(
                "lark-cli.cmd auth status", 0, stdout='{"identity": "user"}'
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        r = drv.status()
        assert r.authenticated is True
        assert r.matched.get("identity") == "user"

    def test_status_not_authenticated(self) -> None:
        runner = _FakeRunner({
            "lark-cli.cmd auth status": CommandResult(
                "lark-cli.cmd auth status", 0, stdout='{"identity": "guest"}'
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        r = drv.status()
        assert r.authenticated is False

    def test_status_bool_true_matches_string_true(self) -> None:
        """dingtalk's statusMatchJson is {"authenticated": "true"} (string),
        but `dws auth status` returns native bool (authenticated: true).
        str(True)=="True" != str("true")=="true" used to make a fully-
        authenticated MCP report pending forever. Normalize."""
        m = CliManifest(
            status_cmd="dws.cmd auth status",
            status_match={"authenticated": "true"},
        )
        runner = _FakeRunner({
            "dws.cmd auth status": CommandResult(
                "dws.cmd auth status", 0,
                stdout='{"authenticated": true, "user_name": "李雷"}',
            ),
        })
        drv = CliDriver("dingtalk", m, runner)
        r = drv.status()
        assert r.authenticated is True

    def test_status_no_command(self) -> None:
        m = CliManifest()
        drv = CliDriver("x", m, _FakeRunner({}))
        r = drv.status()
        assert r.authenticated is False

    def test_status_match_regex_authenticated(self) -> None:
        """dingtalk/wecom's statusMatch is a regex (e.g. "authenticated"\s*:\s*true),
        not a literal substring. A literal `in` check never matched because \s*
        was treated as literal text — authenticated stayed False forever.
        re.search matches the pattern against the status output."""
        m = CliManifest(
            status_cmd="dws.cmd auth status",
            status_match_str=r'"authenticated"\s*:\s*true',
        )
        runner = _FakeRunner({
            "dws.cmd auth status": CommandResult(
                "dws.cmd auth status", 0,
                stdout='{"success": true, "authenticated": true, "user": "李雷"}',
            ),
        })
        drv = CliDriver("dingtalk", m, runner)
        r = drv.status()
        assert r.authenticated is True

    def test_status_match_regex_not_authenticated(self) -> None:
        """The regex must not match when authenticated is false/absent."""
        m = CliManifest(
            status_cmd="dws.cmd auth status",
            status_match_str=r'"authenticated"\s*:\s*true',
        )
        runner = _FakeRunner({
            "dws.cmd auth status": CommandResult(
                "dws.cmd auth status", 0,
                stdout='{"success": false, "authenticated": false}',
            ),
        })
        drv = CliDriver("dingtalk", m, runner)
        r = drv.status()
        assert r.authenticated is False

    def test_status_match_regex_wecom_id(self) -> None:
        """wecom's statusMatch is "id"\s*:\s*" — matches when the status JSON
        has an id field (present only when authenticated)."""
        m = CliManifest(
            status_cmd="wecom-cli.cmd auth show",
            status_match_str=r'"id"\s*:\s*"',
        )
        runner = _FakeRunner({
            "wecom-cli.cmd auth show": CommandResult(
                "wecom-cli.cmd auth show", 0,
                stdout='{"create_time": 1785835061, "id": "aibZ8-8BABctqH8GwkBTBKPlqGxXABv9gwa"}',
            ),
        })
        drv = CliDriver("wecom", m, runner)
        r = drv.status()
        assert r.authenticated is True


class TestCliDriverUnauth:
    def test_unauth_runs_logout(self) -> None:
        runner = _FakeRunner({
            "lark-cli.cmd auth logout": CommandResult("lark-cli.cmd auth logout", 0)
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.unauth()
        assert res.succeeded is True


# ---------------------------------------------------------------------------
# SkillInstaller
# ---------------------------------------------------------------------------

class TestSkillInstaller:
    def _setup_marketplace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "workspace"
        pkg = ws / "mcp" / "mcp_builtins" / "feishu" / "skills" / "lark-approval"
        pkg.mkdir(parents=True)
        (pkg / "SKILL.md").write_text(
            "---\nname: lark-approval\ndescription: x\n---\nbody", encoding="utf-8"
        )
        (pkg / "references").mkdir()
        (pkg / "references" / "ref.md").write_text("ref", encoding="utf-8")
        return ws

    def test_install_copies_and_enables(self, tmp_path: Path) -> None:
        ws = self._setup_marketplace(tmp_path)

        class FakeMgr:
            def __init__(self, *a, **kw) -> None:
                pass

            def set_skill_enabled(self, name: str, enabled: bool) -> None:
                pass  # install no longer calls this (default True; redundant call removed)

            def remove_skill_config(self, name: str) -> None:
                pass

        with (
            patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=ws),
            patch(
                "jiuwenswarm.server.runtime.skill.skill_manager.SkillManager",
                FakeMgr,
            ),
        ):
            from jiuwenswarm.server.runtime.mcp.skill_installer import (
                install_mcp_skills,
            )

            r = install_mcp_skills("feishu")
        assert r["name"] == "feishu"
        assert r["installed"] == ["lark-approval"]
        # Skills now live under mcp/skills/feishu/<skill>/
        dest = ws / "mcp" / "skills" / "feishu" / "lark-approval"
        assert (dest / "SKILL.md").exists()
        assert (dest / "references" / "ref.md").exists()

    def test_install_unknown_connector(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        with (
            patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=ws),
            patch(
                "jiuwenswarm.server.runtime.skill.skill_manager.SkillManager",
                lambda *a, **k: None,
            ),
        ):
            from jiuwenswarm.server.runtime.mcp.skill_installer import (
                install_mcp_skills,
            )

            with pytest.raises(KeyError):
                install_mcp_skills("nope")

# ---------------------------------------------------------------------------
# awesun-style manifest: single-object auth + statusMatch substring
# ---------------------------------------------------------------------------

def _awesun_manifest_dict() -> dict:
    return {
        "runtime": {"type": "node", "version": ">=18"},
        "init": {"win32": "npm install -g @aweray/awesun-cli@latest"},
        "versionCheck": {
            "command": {"win32": "awesun-cli --version"},
            "minVersion": "1.0.1",
        },
        "auth": {"win32": "awesun-cli login --qrcode --url"},
        "unAuth": {"win32": "awesun-cli logout --clean"},
        "status": {"win32": "awesun-cli login status"},
        "statusMatch": "Logged in as",
        "authUrlDomain": "cc.sunlogin.oray.com",
        "authWaitForExit": True,
    }


class TestAwesunManifest:
    def test_single_auth_object_wrapped_to_list(self) -> None:
        m = CliManifest.from_dict(_awesun_manifest_dict())
        assert len(m.auth_steps) == 1
        assert m.auth_steps[0]["authUrlDomain"] == "cc.sunlogin.oray.com"
        assert m.auth_steps[0]["authWaitForExit"] is True

    def test_status_match_str_authenticated(self) -> None:
        m = CliManifest.from_dict(_awesun_manifest_dict())
        runner = _FakeRunner({
            "awesun-cli login status": CommandResult(
                "awesun-cli login status", 0, stdout="Logged in as user@example"
            ),
        })
        drv = CliDriver("awesun", m, runner)
        r = drv.status()
        assert r.authenticated is True
        assert r.matched.get("substring") == "Logged in as"

    def test_status_match_str_not_authenticated(self) -> None:
        m = CliManifest.from_dict(_awesun_manifest_dict())
        runner = _FakeRunner({
            "awesun-cli login status": CommandResult(
                "awesun-cli login status", 0, stdout="not logged in"
            ),
        })
        drv = CliDriver("awesun", m, runner)
        r = drv.status()
        assert r.authenticated is False

    def test_auth_step_extracts_url(self) -> None:
        m = CliManifest.from_dict(_awesun_manifest_dict())
        out = "open https://cc.sunlogin.oray.com/qrcode/xyz to scan"
        proc = _FakeAuthProc(out)
        drv = CliDriver("awesun", m, _FakeRunner({}), proc_runner=lambda cmd: (proc, out))
        r = drv.auth_step(0)
        assert r.succeeded is True
        assert r.needs_user_action is True
        assert r.auth_url == "https://cc.sunlogin.oray.com/qrcode/xyz"


# ---------------------------------------------------------------------------
# flat skill layout (skills/SKILL.md directly under skills/)
# ---------------------------------------------------------------------------

class TestFlatSkillLayout:
    def _setup_flat_marketplace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "workspace"
        skills_dir = ws / "mcp" / "mcp_builtins" / "awesun" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: awesun\ndescription: x\n---\nbody", encoding="utf-8"
        )
        (skills_dir / "references").mkdir()
        (skills_dir / "references" / "ui-locator.md").write_text("ref", encoding="utf-8")
        return ws

    def test_install_flat_skill_named_after_connector(self, tmp_path: Path) -> None:
        ws = self._setup_flat_marketplace(tmp_path)
        enabled: list[tuple[str, bool]] = []

        class FakeMgr:
            def __init__(self, *a, **kw) -> None:
                pass

            def set_skill_enabled(self, name: str, flag: bool) -> None:
                enabled.append((name, flag))

            def remove_skill_config(self, name: str) -> None:
                enabled.append((name, False))

        with (
            patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=ws),
            patch(
                "jiuwenswarm.server.runtime.skill.skill_manager.SkillManager",
                FakeMgr,
            ),
        ):
            from jiuwenswarm.server.runtime.mcp.skill_installer import (
                install_mcp_skills,
            )
            r = install_mcp_skills("awesun")
        assert r["installed"] == ["awesun"]
        # flat layout is normalized to the same nested shape as multi-skill
        # packages: the single skill lands in mcp/skills/<name>/<name>/ so
        # SkillUseRail (which scans root.iterdir() children only) finds it.
        dest = ws / "mcp" / "skills" / "awesun" / "awesun"
        assert (dest / "SKILL.md").exists()
        assert (dest / "references" / "ui-locator.md").exists()

    def test_flat_layout_skill_is_discoverable_by_skill_use_rail(
        self, tmp_path: Path
    ) -> None:
        """Flat-layout skill must be discoverable by SkillUseRail after install.

        Regression: install_mcp_skills used to copy pkg/skills/ (flat) to
        mcp/skills/<name>/ (SKILL.md at root). SkillUseRail only scans
        root.iterdir() children for SKILL.md, never the root itself, so the
        flat skill was invisible. Fix: normalize flat to the nested shape —
        copy to mcp/skills/<name>/<name>/ so the skill sits one level down
        where SkillUseRail looks.
        """
        ws = self._setup_flat_marketplace(tmp_path)

        class FakeMgr:
            def __init__(self, *a, **kw) -> None:
                pass

            def set_skill_enabled(self, name: str, flag: bool) -> None:
                pass

            def remove_skill_config(self, name: str) -> None:
                pass

        with (
            patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=ws),
            patch("jiuwenswarm.server.runtime.skill.skill_manager.SkillManager", FakeMgr),
        ):
            from jiuwenswarm.server.runtime.mcp.skill_installer import (
                install_mcp_skills,
            )
            install_mcp_skills("awesun")

        # The skill dir passed to SkillUseRail is mcp/skills/<name>/; the SKILL.md
        # must sit in a child dir of it (nested normalization), not at its root.
        scan_root = ws / "mcp" / "skills" / "awesun"
        assert not (scan_root / "SKILL.md").exists(), "SKILL.md must not sit at scan root"
        child = scan_root / "awesun" / "SKILL.md"
        assert child.exists(), f"flat skill must be nested under <name>/<name>; got {child}"

        # SkillUseRail scans scan_root.iterdir() children for <child>/SKILL.md.
        from openjiuwen.harness.rails import SkillUseRail

        async def _run() -> None:
            rail = SkillUseRail(
                skills_dir=[str(scan_root)],
                skill_mode=SkillUseRail.SKILL_MODE_ALL,
                include_tools=False,
            )
            await rail.reload_skills()
            names = [s.name for s in rail.skills_meta]
            assert "awesun" in names, f"flat skill not discovered by SkillUseRail: {names}"

        import asyncio
        asyncio.run(_run())
