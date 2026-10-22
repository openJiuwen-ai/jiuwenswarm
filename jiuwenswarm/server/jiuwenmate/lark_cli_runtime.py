"""Host-owned Feishu CLI installation and per-Agent authentication profile.

Each Agent keeps its own Lark CLI config, including any user login created by
the bundled CLI flow. Child environments avoid accidental identity sharing,
not malicious shell access; no credentials are supplied by the model.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import portalocker
import psutil

from jiuwenswarm.server.runtime.mcp.cli_driver import CliManifest
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package


def _part(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value in {".", ".."}:
        raise ValueError("Invalid runtime scope")
    return value


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json(path: Path, data: dict) -> None:
    _write_private_text(path, json.dumps(data, ensure_ascii=False))


@asynccontextmanager
async def _lock(path: Path):
    """Poll a cross-process lock without blocking the event loop or cancellation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = portalocker.Lock(str(path), mode="a", timeout=0)
    deadline = asyncio.get_running_loop().time() + 180
    while True:
        try:
            lock.acquire()
            break
        except portalocker.exceptions.LockException:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("CLI preparation is busy") from None
            await asyncio.sleep(0.1)
    try:
        yield
    finally:
        lock.release()


async def _run(argv: list[str], stdin: str, env: dict[str, str]) -> str:
    """Resolve launchers using the child PATH, including Windows npm launchers."""
    executable = shutil.which(argv[0], path=env.get("PATH"))
    if not executable:
        raise RuntimeError("CLI runtime missing; install Node.js/npm and retry preparation")

    command = [executable, *argv[1:]]
    batch_command = None
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        if any(any(char in value for char in '\"%\r\n') for value in command):
            raise ValueError("Unsupported Windows launcher argument")
        quoted = " ".join(f'"{value}"' for value in command)
        # Batch files use the active code page, including absolute Unicode paths.
        batch_command = f"chcp 65001>nul & call {quoted}"
    options = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
               "stderr": asyncio.subprocess.PIPE, "env": env}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    if batch_command is not None:
        # A raw command avoids list2cmdline's backslash-escaped quotes (not cmd syntax).
        process = await asyncio.create_subprocess_shell(batch_command, **options)
    else:
        process = await asyncio.create_subprocess_exec(*command, **options)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin.encode("utf-8")), timeout=120)
    except (asyncio.CancelledError, TimeoutError):
        await _stop_process(process)
        raise
    if process.returncode:
        # CLI diagnostics can contain credentials. Never expose raw output.
        raise RuntimeError("CLI command failed; check Node.js/npm, network and Feishu app settings")
    return stdout.decode("utf-8", errors="replace")


def _kill_children(pid: int) -> None:
    try:
        children = psutil.Process(pid).children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


async def _stop_process(process) -> None:
    """Reap children too: killing cmd.exe alone leaves Windows npm/CLI running."""
    if process.returncode is None:
        _kill_children(process.pid)
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await asyncio.wait_for(process.wait(), timeout=5)


async def _acquire_package(root: Path):
    from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import install_hub_asset_package
    from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubSearchRequest, create_default_hub_asset_port
    from jiuwenswarm.server.runtime.marketplace.hub_install_state import HubInstallStateStore

    destination = root / "packages"
    existing = destination / "feishu"
    if existing.is_dir():
        return load_mcp_package(existing, expected_id="feishu")
    port = create_default_hub_asset_port()
    page = await port.search_assets(HubSearchRequest(kind="mcp", query="feishu", page_size=100))
    matches = {item.asset_id for item in page.items if item.package_name == "feishu"}
    if len(matches) != 1:
        raise RuntimeError("A unique Feishu CLI package could not be found in the marketplace")

    def validate_name(name):
        if name != "feishu":
            raise ValueError("Unexpected Feishu package")
        return name

    result = await install_hub_asset_package(
        kind="mcp", asset_id=next(iter(matches)), destination_root=destination,
        state_store=HubInstallStateStore(root / "hub-state"),
        package_name_validator=validate_name,
        package_validator=lambda path, name: load_mcp_package(path, expected_id=name), hub_port=port,
    )
    return load_mcp_package(result.destination, expected_id="feishu")


class FeishuCliRuntime:
    """Prepare optional CLI resources; all persistent state belongs to JiuwenMate."""

    def __init__(self, root: Path, *, config_loader=None, package_loader=None, runner=None):
        if config_loader is None:
            from jiuwenswarm.common.config import get_config
            config_loader = get_config
        self.root = Path(root).resolve()
        self._config = config_loader
        self._package = package_loader or _acquire_package
        self._run = runner or _run

    def _directory(self, namespace_id: str, agent_id: str) -> Path:
        path = self.root / "runtime" / _part(namespace_id) / _part(agent_id) / "lark-cli"
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Runtime scope escapes its data directory")
        return path

    async def remove_agent(self, namespace_id: str, agent_id: str) -> None:
        """Remove only this Agent's private credentials/state; retain shared packages."""
        directory = self._directory(namespace_id, agent_id)
        if not directory.exists():
            return
        async with _lock(directory / ".prepare.lock"):
            self._clear_profile(directory)

    @staticmethod
    def _clear_profile(directory: Path) -> None:
        for name in ("config", "unbound", "data"):
            target = directory / name
            if not target.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Private CLI profile escapes its Agent directory")
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        (directory / "status.json").unlink(missing_ok=True)

    def bound_agents(self) -> tuple[tuple[str, str], ...]:
        from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import configured_feishu_bindings
        return tuple((item.namespace_id, item.agent_id) for item in configured_feishu_bindings(self._config()))

    def _binary(self) -> Path:
        return self.root / "cli" / "bin" / ("lark-cli.cmd" if os.name == "nt" else "lark-cli")

    def _binding(self, namespace_id: str, agent_id: str):
        if namespace_id != "local":
            return None
        channels = self._config().get("channels") or {}
        ordinary = (channels.get("feishu") or {}).get("apps") or []
        enterprise = channels.get("feishu_enterprise") or {}
        entries = [*ordinary, *enterprise.values()]
        entries = [item for item in entries if isinstance(item, dict) and item.get("enabled", True)]
        matches = [item for item in entries if item.get("persistent_agent_id") == agent_id]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Ambiguous Feishu binding")
        app = matches[0]
        app_id, secret = str(app.get("app_id", "")).strip(), str(app.get("app_secret", "")).strip()
        if not app_id or not secret or sum(item.get("app_id") == app_id for item in entries) != 1:
            raise ValueError("Feishu binding is incomplete or ambiguous")
        # A private revision detects same-app credential rotation without saving the Secret.
        revision = hashlib.sha256(json.dumps([app_id, secret]).encode()).hexdigest()
        return app_id, secret, revision

    def status(self, namespace_id: str, agent_id: str) -> dict[str, object]:
        directory = self._directory(namespace_id, agent_id)
        try:
            binding = self._binding(namespace_id, agent_id)
            if binding is None:
                return {"status": "unbound"}
            state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
            if state.get("revision") != binding[2]:
                return {"status": "pending"}
            if state.get("status") == "ready":
                if not self._matches(directory / "config", binding) or not self._binary().is_file():
                    return {"status": "pending"}
                if os.name == "nt" and not (self.root / "cli/lark-cli.cmd").is_file():
                    return {"status": "pending"}
            return {key: value for key, value in state.items() if key != "revision"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"status": "unavailable"}

    @staticmethod
    def _matches(directory: Path, binding: tuple[str, str, str]) -> bool:
        try:
            value = json.loads((directory / "config.json").read_text(encoding="utf-8"))
            apps = value.get("apps", [])
            secret_file = directory / "app-secret"
            return (
                len(apps) == 1 and apps[0].get("appId") == binding[0]
                and apps[0].get("appSecret") == {"source": "file", "id": str(secret_file)}
                and apps[0].get("defaultAs") == "bot"
                and "strictMode" not in apps[0] and "strictMode" not in value
                and isinstance(apps[0].get("users"), list)
                and secret_file.read_text(encoding="utf-8") == binding[1]
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def shell_environment(self, namespace_id: str, agent_id: str) -> dict[str, str]:
        directory = self._directory(namespace_id, agent_id)
        ready = self.status(namespace_id, agent_id).get("status") == "ready"
        env = dict(os.environ)
        # Do not inherit personal app credentials injected into the service environment.
        for key in tuple(env):
            if key.startswith(("LARK_", "LARKSUITE_", "FEISHU_")):
                env.pop(key)
        env["LARKSUITE_CLI_CONFIG_DIR"] = str(directory / ("config" if ready else "unbound"))
        env["LARKSUITE_CLI_DATA_DIR"] = str(directory / "data")
        env["PATH"] = str(self.root / "cli" / "bin") + os.pathsep + env.get("PATH", "")
        return env

    def skill_directories(self, namespace_id: str, agent_id: str) -> tuple[Path, ...]:
        if self.status(namespace_id, agent_id).get("status") != "ready":
            return ()
        try:
            state = json.loads((self._directory(namespace_id, agent_id) / "status.json").read_text(encoding="utf-8"))
            return tuple(Path(item) for item in state["skills"] if Path(item).is_dir())
        except (OSError, ValueError, KeyError):
            return ()

    async def prepare(self, namespace_id: str, agent_id: str) -> dict[str, object]:
        directory = self._directory(namespace_id, agent_id)
        async with _lock(directory / ".prepare.lock"):
            revision = ""
            try:
                binding = self._binding(namespace_id, agent_id)
                if binding is None:
                    self._clear_profile(directory)
                    return {"status": "unbound"}
                app_id, secret, revision = binding
                async with _lock(self.root / "cli" / ".install.lock"):
                    package = await self._package(self.root)
                    if package.integration_type != "cli" or package.integration_file is None:
                        raise ValueError("Feishu package must declare CLI integration")
                    manifest = CliManifest.from_dict(json.loads(package.integration_file.read_text(encoding="utf-8")))
                    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.min_version):
                        raise ValueError("Feishu CLI package must pin an exact version")
                    if not manifest.init_cmd or not manifest.version_cmd:
                        raise ValueError("Feishu CLI package is missing install/version metadata")
                    prefix = self.root / "cli"
                    binary = self._binary()
                    env = self.shell_environment(namespace_id, agent_id)
                    if not binary.is_file() or (os.name == "nt" and not (prefix / "lark-cli.cmd").is_file()):
                        # Explicit package/version/prefix: never execute an arbitrary manifest shell script.
                        await self._run(["npm", "install", "--global", "--prefix", str(prefix),
                                         f"@larksuite/cli@{manifest.min_version}"], "", env)
                        if os.name == "nt":
                            binary.parent.mkdir(parents=True, exist_ok=True)
                            binary.write_text('@echo off\n"%~dp0..\\lark-cli.cmd" %*\n', encoding="utf-8")
                    output = await self._run([str(binary), "--version"], "", env)
                    match = re.search(r"\b\d+\.\d+\.\d+\b", output)
                    if match is None or match.group() != manifest.min_version:
                        raise ValueError("Private Feishu CLI version does not match the package; remove its private installation and retry")
                if self._binding(namespace_id, agent_id) != binding:
                    raise ValueError("Feishu binding changed during preparation")
                if self.status(namespace_id, agent_id).get("status") == "ready":
                    return self.status(namespace_id, agent_id)
                config = directory / "config"
                # Drop obsolete local tokens when the app or credential revision changes.
                if config.exists():
                    if not config.resolve().is_relative_to(directory.resolve()):
                        raise ValueError("Private CLI profile escapes its Agent directory")
                    shutil.rmtree(config)
                config.mkdir(parents=True, mode=0o700)
                # config init writes the shared OS keychain by app_id. File SecretRef avoids it.
                _write_private_text(config / "app-secret", secret)
                _write_json(config / "config.json", {"currentApp": app_id, "apps": [{
                    "appId": app_id, "appSecret": {"source": "file", "id": str(config / "app-secret")},
                    "brand": "feishu", "lang": "zh", "defaultAs": "bot", "users": [],
                }]})
                if self._binding(namespace_id, agent_id) != binding or not self._matches(config, binding):
                    raise ValueError("Feishu binding changed or CLI identity could not be verified")
                _write_json(directory / "status.json", {"status": "ready", "app_id": app_id,
                    "revision": revision, "version": manifest.min_version,
                    "skills": [str(path) for path in package.skill_dirs]})
            except asyncio.CancelledError:
                _write_json(directory / "status.json", {"status": "pending", "revision": revision})
                raise
            except Exception:
                # Never persist subprocess output or exception text containing credentials.
                _write_json(directory / "status.json", {"status": "unavailable", "revision": revision,
                    "error": "飞书 CLI 准备失败，请检查 Node.js/npm、网络、应用绑定及固定版本包后重试。"})
            return self.status(namespace_id, agent_id)
