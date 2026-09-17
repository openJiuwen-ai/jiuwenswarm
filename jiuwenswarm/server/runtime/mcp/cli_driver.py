# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CLI driver.

Orchestrates the lifecycle of a CLI-based MCP per its ``cli.json`` manifest:
install the runtime, check the version, run the OAuth auth steps, poll auth
status, and logout (unauth).

Manifest variants supported:
  * feishu-style: ``auth`` is a list of steps, ``status.statusMatchJson`` is a
    dict (exact field match).
  * awesun-style: ``auth`` is a single object, ``statusMatch`` is a substring,
    ``authUrlDomain``/``authWaitForExit`` live at the top level.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.common.utils import get_workspace_dir  # re-export for test patches

logger = logging.getLogger(__name__)


def _packages_dir() -> Path:
    """Marketplace 包目录：<workspace>/mcp/mcp_builtins/."""
    return get_workspace_dir() / "mcp" / "mcp_builtins"


# Auth processes started in non-blocking mode (authWaitForExit). Keyed by MCP
# name; the value is the running Popen. Pollable from complete_cli_auth.
_PENDING_AUTH_PROCS: dict[str, subprocess.Popen] = {}


def _cleanup_stale_auth_proc(name: str) -> None:
    """Kill a prior pending auth process for *name* (idempotent)."""
    old = _PENDING_AUTH_PROCS.pop(name, None)
    if old is not None:
        try:
            old.kill()
        except Exception:  # noqa: BLE001
            pass


def _platform_key() -> str:
    sysname = platform.system()
    if sysname == "Windows":
        return "win32"
    if sysname == "Darwin":
        return "darwin"
    if sysname == "Linux":
        return "linux"
    return sysname.lower()


def _pick_per_platform(spec: Any) -> str | None:
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return None
    pk = _platform_key()
    val = spec.get(pk) or spec.get("linux") or spec.get("darwin") or spec.get("win32")
    return val if isinstance(val, str) else None


CommandRunner = Callable[[str], "CommandResult"]


# Injector for authWaitForExit non-blocking start (tests). Returns (proc, initial_output).
ProcRunner = Callable[[str], tuple[Any, str]]


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


_SHELL_FORBIDDEN_FIRST = {"bash", "cmd", "/bin/sh", "sh"}
_SHELL_FORBIDDEN_SECOND = "-c"


def _safe_split_command(command: str) -> list[str]:
    """Split a command string into an argv list for ``shell=False``.

    Enforces the two hard rules G.EDV.04 requires for shell=False to be safe:
    the first element must NOT be a shell binary (bash/cmd/sh/...), and the
    second must NOT be ``-c``. cli.json commands are trusted (bin name +
    args), but we assert this explicitly rather than relying on the data.

    On Windows, resolves a bare first arg (e.g. ``npm``) to its full path via
    ``shutil.which`` (which searches PATHEXT — finds ``npm.CMD``). CreateProcess
    does NOT do PATHEXT resolution, so a bare ``npm`` fails with WinError 2
    even though ``npm.CMD`` is on PATH. This is the price of ``shell=False``;
    the lookup here restores what the cmd shell used to do. No-op on POSIX
    (execvp already searches PATH). The shell-binary ban above runs on the
    bare name, before resolution, so ``cmd``/``sh`` are still refused.
    """
    parts = shlex.split(command)
    if not parts:
        raise ValueError("empty command")
    first = parts[0].lower()
    if first in _SHELL_FORBIDDEN_FIRST:
        raise ValueError(f"refusing to run shell binary '{first}' as first arg")
    if len(parts) > 1 and parts[1] == _SHELL_FORBIDDEN_SECOND:
        raise ValueError("refusing '-c' as second arg (shell invocation)")
    if sys.platform == "win32":
        import shutil
        resolved = shutil.which(parts[0])
        if resolved:
            parts[0] = resolved
    return parts


def default_runner(command: str, timeout: float = 120.0, env: dict[str, str] | None = None) -> CommandResult:
    try:
        proc = subprocess.run(  # noqa: S603 - command from trusted cli.json
            _safe_split_command(command),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            timed_out=True,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(command=command, returncode=-1, stderr=str(exc))


def _parse_version(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    return match.group(1) if match else None


def _version_ge(a: str, b: str) -> bool:
    try:
        from packaging.version import parse as _parse
        return _parse(a) >= _parse(b)
    except Exception:  # noqa: BLE001
        return a == b


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_url(text: str, domain_hint: str = "") -> str | None:
    if not text:
        return None
    matches = _URL_RE.findall(text)
    if not matches:
        return None
    if domain_hint:
        for url in matches:
            if domain_hint in url:
                return url
    return matches[0]


def _derive_bin_name(command: str) -> str | None:
    """Derive the CLI binary name from a versionCheck command string.

    Pure CLI (feishu/dingtalk/zsxq/...) have no ``mcp.json``; their
    stdio MCP entry runs ``<bin> mcp`` where <bin> comes from cli.json's
    versionCheck command (e.g. ``lark-cli.cmd --version`` -> ``lark-cli``).

    The versionCheck command is the ONLY reliable source — npm package names
    in the init command (``@larksuite/cli`` / ``dingtalk-workspace-cli``) do
    NOT map to bin names (bin is ``lark-cli`` / ``dws``), so init is not used.

    Strips ``.cmd``/``.exe`` suffixes (Windows) so the registered bare name
    resolves cross-platform via PATH (node CLIs install a .cmd shim on Windows).
    """
    if not command:
        return None
    cmd = command.strip()
    if not cmd:
        return None
    # "<bin>[.cmd|.exe] [args...]" — take the first token, strip shim suffix.
    first = cmd.split()[0] if " " in cmd else cmd
    for suf in (".cmd", ".exe", ".bat"):
        if first.lower().endswith(suf):
            first = first[: -len(suf)]
            break
    return first or None


def build_stdio_entry(name: str, manifest: "CliManifest") -> dict[str, Any] | None:
    """Build a state.json ``mcp.servers`` stdio entry running ``<bin> mcp``.

    For pure-CLI MCPs with no mcp.json, the MCP server is the CLI's own
    ``mcp`` subcommand (e.g. ``lark-cli mcp`` / ``dws mcp``). <bin> is derived
    from the manifest's versionCheck command. Returns None if versionCheck is
    absent (hybrid packages with a real mcp.json use that instead).
    """
    n = str(name or "").strip()
    if not n:
        return None
    bin_name = _derive_bin_name(manifest.version_cmd)
    if not bin_name:
        return None
    return {
        "name": n,
        "transport": "stdio",
        "command": bin_name,
        "args": ["mcp"],
        "enabled": True,
        "server_id_scope": f"mcp:{n}",
    }


@dataclass
class CliManifest:
    runtime_type: str = ""
    runtime_version: str = ""
    init_cmd: str = ""
    version_cmd: str = ""
    min_version: str = ""
    auth_steps: list[dict[str, Any]] = field(default_factory=list)
    unauth_cmd: str = ""
    status_cmd: str = ""
    status_match: dict[str, str] = field(default_factory=dict)
    status_match_str: str = ""
    # cli.json top-level ``authSuppressBrowser`` (bool): when true, the CLI
    # binary opens the browser itself (e.g. dingtalk's ``dws auth login -y``);
    # when false/absent, the CLI only prints the URL and the frontend must open
    # it. Used to decide whether to surface auth_url to the frontend.
    auth_suppress_browser: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CliManifest":
        runtime = data.get("runtime") or {}
        ver_check = data.get("versionCheck") or {}
        init = data.get("init") or {}
        auth = data.get("auth") or []
        unauth = data.get("unAuth") or {}
        status = data.get("status") or {}
        top_domain = str(data.get("authUrlDomain", "")).strip()
        top_wait = bool(data.get("authWaitForExit", False))
        # auth: list (feishu multi-step) or single object (awesun).
        if isinstance(auth, dict):
            auth = [
                {
                    **auth,
                    "authUrlDomain": auth.get("authUrlDomain", top_domain),
                    "authWaitForExit": auth.get("authWaitForExit", top_wait)
                }
            ]
        auth_steps = [s for s in auth if isinstance(s, dict)]
        # status: {plat: cmd} map or plain string.
        status_cmd = _pick_per_platform(status) \
            if isinstance(status, dict) else \
                (str(status) if isinstance(status, str) else "")
        # match: feishu statusMatchJson (dict, top-level) or awesun
        # statusMatch (substring). statusMatchJson lives at the cli.json top
        # level (sibling of ``status``), not inside the ``status`` object.
        sm = data.get("statusMatch") or (status.get("statusMatch") if isinstance(status, dict) else None)
        status_match_str = str(sm) if isinstance(sm, str) and sm else ""
        status_match_json = data.get("statusMatchJson") or {}
        if not isinstance(status_match_json, dict):
            status_match_json = {}
        return cls(
            runtime_type=str(runtime.get("type", "")).strip(),
            runtime_version=str(runtime.get("version", "")).strip(),
            init_cmd=_pick_per_platform(init) or "",
            version_cmd=_pick_per_platform(ver_check.get("command")) or "",
            min_version=str(ver_check.get("minVersion", "")).strip(),
            auth_steps=auth_steps,
            unauth_cmd=_pick_per_platform(unauth) or "",
            status_cmd=status_cmd,
            status_match=status_match_json,
            status_match_str=status_match_str,
            auth_suppress_browser=bool(data.get("authSuppressBrowser", False)),
        )


def load_cli_manifest(name: str) -> CliManifest | None:
    import json
    n = str(name or "").strip()
    if not n:
        return None
    path = _packages_dir() / n / "cli.json"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[cli_driver] failed to read %s: %s", path, exc)
        return None
    return CliManifest.from_dict(data if isinstance(data, dict) else {})


@dataclass
class InstallResult:
    name: str
    installed: bool
    version: str | None
    min_version: str
    version_ok: bool
    error: str = ""


@dataclass
class AuthStepResult:
    name: str
    step_index: int
    command: str
    succeeded: bool
    needs_user_action: bool
    auth_url: str | None = None
    auth_domain: str = ""
    output: str = ""
    error: str = ""


@dataclass
class StatusResult:
    name: str
    authenticated: bool
    matched: dict[str, str] = field(default_factory=dict)
    output: str = ""


class CliDriver:
    def __init__(
        self,
        name: str,
        manifest: CliManifest | None = None,
        runner: CommandRunner | None = None,
        proc_runner: "ProcRunner | None" = None,
    ) -> None:
        self.name = str(name or "").strip()
        self.manifest = manifest or load_cli_manifest(self.name) or CliManifest()
        # Credential-derived env to inject into spawned CLI subprocesses. Some
        # CLI MCPs (gitcode) authenticate via an env-var token the CLI's own
        # ``auth status`` reads (gitcode reads GITCODE_TOKEN/GC_TOKEN from env)
        # — NOT via OAuth. token-schema.json declares the required env keys;
        # CliDriver merges the stored tokens into the subprocess env so
        # ``status``/``install`` run with the token visible, and the absent
        # ``auth login`` step never needs to run. None when the MCP has no
        # token-schema (feishu/dingtalk/wecom — pure OAuth) → env inherits the
        # parent process unchanged, so OAuth CLIs are unaffected.
        self._cred_env = self._build_cred_env()
        if runner is not None:
            # Test path: caller injects its own runner; don't touch env (tests
            # are pure logic and don't stand up a real workspace/credential store).
            self._runner = runner
        else:
            cred_env = self._cred_env
            self._runner = (
                (lambda cmd: default_runner(cmd, env=cred_env))
                if cred_env is not None
                else (lambda cmd: default_runner(cmd))
            )
        # Optional injector for authWaitForExit start (tests); default uses real Popen.
        self._proc_runner = proc_runner

    def _build_cred_env(self) -> dict[str, str] | None:
        """Merge this MCP's CredentialStore tokens (keyed by its token-schema
        required fields) onto os.environ for the CLI subprocess.

        Returns None when the MCP has no token-schema (no env injection —
        OAuth CLIs) or the store is unreadable; None means "inherit parent
        env", the pre-existing behavior.
        """
        if not self.name:
            return None
        try:
            from jiuwenswarm.server.runtime.mcp.credential import (
                CredentialStore,
                required_tokens_from_schema,
            )
        except Exception:  # noqa: BLE001
            return None
        keys = required_tokens_from_schema(self.name)
        if not keys:
            return None
        try:
            stored = CredentialStore().get_all(self.name)
        except Exception:  # noqa: BLE001
            return None
        if not stored:
            return None
        env = dict(os.environ)
        for k in keys:
            v = stored.get(k)
            if v is not None:
                env[k] = str(v)
        return env

    def install(self) -> InstallResult:
        m = self.manifest
        err = ""
        version: str | None = None
        version_ok = True
        # Version-check first: if the CLI is already installed at a sufficient
        # version, skip the (potentially slow, network-bound) init/install step
        # entirely. Only fall back to init when the version check fails or
        # cannot be parsed.
        if m.version_cmd:
            res = self._runner(m.version_cmd)
            version = _parse_version(res.combined_output)
            if m.min_version and version:
                version_ok = _version_ge(version, m.min_version)
                if not version_ok:
                    logger.warning("[cli_driver] %s version %s < min %s", self.name, version, m.min_version)
            elif m.min_version and not version:
                version_ok = False
                err = f"could not parse version from: {res.combined_output}"
        if version_ok and version:
            # CLI already present and recent enough — skip init.
            logger.info("[cli_driver] %s skip init (version %s ok)", self.name, version)
        elif m.init_cmd:
            res = self._runner(m.init_cmd)
            if not res.succeeded:
                err = f"init failed (rc={res.returncode}): {res.combined_output}"
                logger.warning("[cli_driver] %s init failed: %s", self.name, err)
            # re-check version after install
            if m.version_cmd:
                res2 = self._runner(m.version_cmd)
                version = _parse_version(res2.combined_output)
                if m.min_version and version:
                    version_ok = _version_ge(version, m.min_version)
                elif m.min_version:
                    version_ok = False
                    err = (err + "; " if err else "") + f"could not parse version after init: {res2.combined_output}"
        return InstallResult(
            name=self.name, installed=True,
            version=version, min_version=m.min_version,
            version_ok=version_ok, error=err
        )

    def auth_step(self, index: int = 0) -> AuthStepResult:
        m = self.manifest
        steps = m.auth_steps
        if index < 0 or index >= len(steps):
            return AuthStepResult(
                name=self.name,
                step_index=index,
                command="",
                succeeded=False,
                needs_user_action=False,
                error=f"auth step {index} out of range (have {len(steps)})"
            )
        step = steps[index]
        # feishu: step = {"command": {plat:cmd}, "skipIf":...};
        # awesun: step itself is the {plat:cmd} map (no "command" key).
        cmd_spec = step.get("command") if "command" in step else step
        cmd = _pick_per_platform(cmd_spec) or ""
        if not cmd:
            return AuthStepResult(
                name=self.name, step_index=index, command="",
                succeeded=False, needs_user_action=False,
                error="no command for this platform"
            )
        skip_cmd = _pick_per_platform(step.get("skipIf")) or ""
        if skip_cmd:
            skip_res = self._runner(skip_cmd)
            if skip_res.succeeded:
                return AuthStepResult(
                    name=self.name, step_index=index, command=skip_cmd,
                    succeeded=True, needs_user_action=False,
                    output=skip_res.combined_output
                )
        domain = str(step.get("authUrlDomain", "")).strip()
        wait_for_exit = bool(step.get("authWaitForExit"))
        # authWaitForExit commands (e.g. awesun login) block until the user
        # completes OAuth. Start them non-blocking, read the auth URL from
        # early stdout, and return immediately so the event loop is not held.
        if wait_for_exit:
            return self._start_auth_proc(index, cmd, domain)
        res = self._runner(cmd)
        url = _extract_url(res.combined_output, domain) if domain else None
        return AuthStepResult(
            name=self.name, step_index=index, command=cmd,
            succeeded=res.succeeded, needs_user_action=bool(domain),
            auth_url=url, auth_domain=domain,
            output=res.combined_output,
            error="" if res.succeeded else f"rc={res.returncode}: {res.combined_output}"
        )

    def _start_auth_proc(self, index: int, cmd: str, domain: str) -> AuthStepResult:
        """Start an authWaitForExit command non-blocking, capture its auth URL.

        The subprocess keeps running (blocking until OAuth completes); we read
        stdout for up to a few seconds to surface the auth URL, then return an
        auth_required sentinel. The proc is stashed in ``_PENDING_AUTH_PROCS``
        for :meth:`auth_proc_done` / :meth:`status` to poll.
        """
        if self._proc_runner is not None:
            # Test path: injector returns (proc, initial_output_string).
            proc, out = self._proc_runner(cmd)
            _cleanup_stale_auth_proc(self.name)
            _PENDING_AUTH_PROCS[self.name] = proc
            url = _extract_url(out, domain) if domain else None
            return AuthStepResult(
                name=self.name, step_index=index, command=cmd,
                succeeded=True, needs_user_action=True,
                auth_url=url, auth_domain=domain, output=out
            )
        import time
        try:
            proc = subprocess.Popen(  # noqa: S603 - command from trusted cli.json
                _safe_split_command(cmd),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._cred_env,
            )
        except (OSError, ValueError) as exc:
            return AuthStepResult(
                name=self.name, step_index=index, command=cmd,
                succeeded=False, needs_user_action=False,
                error=f"failed to start auth: {exc}"
            )
        _cleanup_stale_auth_proc(self.name)
        _PENDING_AUTH_PROCS[self.name] = proc
        url: str | None = None
        deadline = time.time() + 8.0
        chunks: list[str] = []
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                chunks.append(line)
                if domain:
                    found = _extract_url(line, domain)
                    if found:
                        url = found
                        break
            else:
                rc = proc.poll()
                if rc is not None:
                    break
                time.sleep(0.2)
        out = "".join(chunks)
        return AuthStepResult(
            name=self.name, step_index=index, command=cmd,
            succeeded=True, needs_user_action=True,
            auth_url=url, auth_domain=domain, output=out
        )

    def auth_proc_done(self) -> bool | None:
        """True if the pending auth proc exited, False if still running, None if none."""
        proc = _PENDING_AUTH_PROCS.get(self.name)
        if proc is None:
            return None
        rc = proc.poll()
        if rc is None:
            return False
        _PENDING_AUTH_PROCS.pop(self.name, None)
        return True

    def auth_steps_count(self) -> int:
        return len(self.manifest.auth_steps)

    def status(self) -> StatusResult:
        m = self.manifest
        if not m.status_cmd:
            return StatusResult(name=self.name, authenticated=False, output="no status command")
        res = self._runner(m.status_cmd)
        out = res.combined_output
        if m.status_match_str:
            # statusMatch in cli.json is a regex pattern (e.g.
            # "authenticated"\s*:\s*true for dingtalk, "id"\s*:\s*" for wecom).
            # Use re.search; fall back to literal `in` if the pattern isn't a
            # valid regex.
            import re as _re
            try:
                authenticated = _re.search(m.status_match_str, out) is not None
            except _re.error:
                authenticated = m.status_match_str in out
            return StatusResult(
                name=self.name, authenticated=authenticated,
                matched={"substring": m.status_match_str}, output=out
            )
        matched: dict[str, str] = {}
        authenticated = True
        if m.status_match:
            import json as _json
            payload: dict | None = None
            try:
                payload = _json.loads(out)
            except Exception:  # noqa: BLE001
                # CLI status output may have extra log lines/warnings mixed in
                # (stderr merged via combined_output). Try to extract the JSON
                # object substring (first { to last }) before giving up.
                start = out.find("{")
                end = out.rfind("}")
                if start >= 0 and end > start:
                    try:
                        payload = _json.loads(out[start:end + 1])
                    except Exception:  # noqa: BLE001
                        payload = None
                if payload is None:
                    logger.debug("[cli_driver] %s status JSON parse failed: %r", self.name, out[:200])
                    payload = {}
            if isinstance(payload, dict):
                for key, expected in m.status_match.items():
                    actual = payload.get(key)
                    matched[str(key)] = str(actual) if actual is not None else ""
                    # cli.json's statusMatchJson is a JSON object with string
                    # values (e.g. {"authenticated": "true"}), but the CLI's
                    # status JSON often has native booleans (authenticated:
                    # true). Normalize: compare lowercased string forms, and
                    # treat True/False as "true"/"false".

                    def _norm(v: Any) -> str:
                        if isinstance(v, bool):
                            return "true" if v else "false"
                        return str(v).strip().lower()
                    if _norm(actual) != _norm(expected):
                        authenticated = False
            else:
                authenticated = False
        else:
            authenticated = res.succeeded
        return StatusResult(name=self.name, authenticated=authenticated, matched=matched, output=out)

    def unauth(self) -> CommandResult:
        if not self.manifest.unauth_cmd:
            return CommandResult(command="", returncode=0, stdout="no unauth command")
        return self._runner(self.manifest.unauth_cmd)

    def build_stdio_entry(self, name: str, manifest: "CliManifest" | None = None) -> dict[str, Any] | None:
        """Derive the ``<bin> mcp`` stdio config entry from the manifest.

        Thin wrapper over the module-level :func:`build_stdio_entry` so callers
        holding a CliDriver instance can ask for the stdio entry directly.
        """
        return build_stdio_entry(name, manifest or self.manifest)


__all__ = [
    "CliDriver",
    "CliManifest",
    "CommandResult",
    "CommandRunner",
    "InstallResult",
    "AuthStepResult",
    "StatusResult",
    "default_runner",
    "load_cli_manifest",
]
