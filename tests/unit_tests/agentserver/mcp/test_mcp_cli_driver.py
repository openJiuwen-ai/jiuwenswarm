# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CliDriver + SkillInstaller (form C, CLI connectors).

The CliDriver flow (install/version/auth/status) is validated with an
injectable fake runner so no npm/network/OAuth is required. The
SkillInstaller is validated against a temp workspace marketplace so file
copying + enable flipping is exercised for real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp.cli_driver import (
    CliDriver,
    CliManifest,
    CommandResult,
    ERR_BINARY_NOT_FOUND,
    _extract_url,
    _is_binary_not_found,
    _parse_version,
    _version_ge,
)
from tests.unit_tests.agentserver.mcp.manifest_helpers import write_manifest


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

    def test_install_binary_not_found_classified(self) -> None:
        """Runtime/CLI binary missing (node/npm/dws not on PATH) surfaces as
        error_kind=binary_not_found on InstallResult, so _connect_cli can map
        it to MCP_RUNTIME_MISSING instead of leaking "[WinError 2]". The fake
        runner returns a CommandResult carrying the kind that default_runner
        would set when subprocess raises FileNotFoundError."""
        runner = _FakeRunner({
            "lark-cli.cmd --version": CommandResult(
                "lark-cli.cmd --version", -1,
                stderr="[WinError 2] 系统找不到指定的文件",
                error_kind=ERR_BINARY_NOT_FOUND,
            ),
            "npm install -g @larksuite/cli": CommandResult(
                "npm install -g @larksuite/cli", -1,
                stderr="[WinError 2]",
                error_kind=ERR_BINARY_NOT_FOUND,
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.install()
        assert res.version_ok is False
        assert res.error_kind == ERR_BINARY_NOT_FOUND
        assert res.runtime == "node"

    def test_install_network_failure_not_classified_as_binary(self) -> None:
        """npm install ran (rc!=0) with a network error stays error_kind=''
        — it is NOT binary_not_found, so _classify_install_failure maps it to
        MCP_INSTALL_NETWORK via stderr markers, not MCP_RUNTIME_MISSING."""
        runner = _FakeRunner({
            "lark-cli.cmd --version": CommandResult(
                "lark-cli.cmd --version", -1, stderr="not found",
            ),
            "npm install -g @larksuite/cli": CommandResult(
                "npm install -g @larksuite/cli", 1,
                stderr="npm ERR! code ETIMEDOUT registry timeout",
            ),
        })
        drv = CliDriver("feishu", _mkmanifest(), runner)
        res = drv.install()
        assert res.version_ok is False
        assert res.error_kind == ""
        assert "ETIMEDOUT" in res.error

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
        r"""dingtalk/wecom's statusMatch is a regex (e.g. "authenticated"\s*:\s*true),
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
        r"""wecom's statusMatch is "id"\s*:\s*" — matches when the status JSON
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
        package = pkg.parents[1]
        (package / "cli.json").write_text("{}", encoding="utf-8")
        write_manifest(package, "cli", credentials_type="cli-oauth", skills=True)
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
        package = skills_dir.parent
        (package / "cli.json").write_text("{}", encoding="utf-8")
        write_manifest(package, "cli", credentials_type="cli-oauth", skills=True)
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


# ---------------------------------------------------------------------------
# Boundary-failure classification: _is_binary_not_found + _classify_install_failure
# ---------------------------------------------------------------------------

class TestBinaryNotFoundDetection:
    def test_file_not_found_error_is_binary_missing(self) -> None:
        assert _is_binary_not_found(FileNotFoundError("[WinError 2] x")) is True

    def test_errno_enoent_is_binary_missing(self) -> None:
        class _Err(OSError):
            def __init__(self) -> None:
                super().__init__(2, "no such file")

        assert _is_binary_not_found(_Err()) is True

    def test_permission_error_is_not_binary_missing(self) -> None:
        assert _is_binary_not_found(PermissionError("denied")) is False

    def test_generic_oserror_not_binary_missing(self) -> None:
        assert _is_binary_not_found(OSError("broken pipe")) is False


class TestClassifyInstallFailure:
    """_classify_install_failure maps an InstallResult onto a CliConnectError
    carrying a structured code + runtime, so mcp.connect can surface an
    actionable i18n hint instead of raw WinError."""

    def _mk(self, **kw):
        from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult
        base = dict(
            name="dingtalk", installed=True, version=None,
            min_version="1.0.54", version_ok=False, error="boom", error_kind="",
            runtime="node", install_cmd="npm install -g dingtalk-workspace-cli",
        )
        base.update(kw)
        return InstallResult(**base)

    def test_binary_not_found_maps_to_runtime_missing(self) -> None:
        from jiuwenswarm.server.runtime.mcp.registry import (
            CODE_RUNTIME_MISSING, _classify_install_failure,
        )
        exc = _classify_install_failure("dingtalk", self._mk(error_kind="binary_not_found"))
        assert exc.code == CODE_RUNTIME_MISSING
        assert exc.runtime == "node"
        assert exc.install_cmd == "npm install -g dingtalk-workspace-cli"

    def test_network_error_maps_to_install_network(self) -> None:
        from jiuwenswarm.server.runtime.mcp.registry import (
            CODE_INSTALL_NETWORK, _classify_install_failure,
        )
        exc = _classify_install_failure(
            "dingtalk", self._mk(error="npm ERR! ETIMEDOUT registry timeout"),
        )
        assert exc.code == CODE_INSTALL_NETWORK

    def test_network_error_uppercase_code_matches_case_insensitively(self) -> None:
        """npm emits uppercase error codes (ETIMEDOUT/ECONNREFUSED). The
        match runs against a lowercased error string, so markers must be
        lowercase too — a regression where markers stayed uppercase would
        silently fall through to MCP_CLI_INCOMPLETE. Use an error containing
        ONLY an uppercase code (no lowercase 'registry'/'network' word) so
        the marker case-normalization is actually exercised."""
        from jiuwenswarm.server.runtime.mcp.registry import (
            CODE_INSTALL_NETWORK, _classify_install_failure,
        )
        exc = _classify_install_failure(
            "dingtalk", self._mk(error="request failed: ECONNREFUSED 127.0.0.1:443"),
        )
        assert exc.code == CODE_INSTALL_NETWORK

    def test_other_failure_maps_to_cli_incomplete(self) -> None:
        from jiuwenswarm.server.runtime.mcp.registry import (
            CODE_CLI_INCOMPLETE, _classify_install_failure,
        )
        exc = _classify_install_failure("dingtalk", self._mk(error="version too low"))
        assert exc.code == CODE_CLI_INCOMPLETE
        # C scenario surfaces the install/upgrade command so the frontend can
        # show it — not a vague "uninstall and retry".
        assert exc.install_cmd == "npm install -g dingtalk-workspace-cli"

    def test_empty_runtime_does_not_crash(self) -> None:
        from jiuwenswarm.server.runtime.mcp.registry import (
            CODE_RUNTIME_MISSING, _classify_install_failure,
        )
        exc = _classify_install_failure(
            "dingtalk", self._mk(error_kind="binary_not_found", runtime=""),
        )
        assert exc.code == CODE_RUNTIME_MISSING
        assert exc.runtime == ""

    def test_cli_connect_error_is_value_error_subclass(self) -> None:
        """except ValueError in _handle_mcp_connect still catches CliConnectError
        as a fallback (it's caught by the dedicated except CliConnectError first,
        but the subclass relationship guarantees no NameError fallback gap)."""
        from jiuwenswarm.server.runtime.mcp.registry import (
            CliConnectError, _classify_install_failure,
        )
        exc = _classify_install_failure("dingtalk", self._mk())
        assert isinstance(exc, CliConnectError)
        assert isinstance(exc, ValueError)
