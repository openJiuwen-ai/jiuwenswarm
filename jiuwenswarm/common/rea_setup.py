# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""REA base environment detection and setup helpers for Workswarm.

The module intentionally owns only the minimum runnable REA set:
compatible Node.js/npx, the pinned REA npm package, and an MCP registration.
Enhanced analysis dependencies are reported when REA can discover them, but
are not required for base readiness.
"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.common.utils import get_workspace_dir

REA_PACKAGE_NAME = "rea-agents"
REA_PACKAGE_VERSION = "6.8.0"
REA_MCP_NAME = "rea"
REA_SKILL_NAME = "reverse-engineer-anything"
REA_SKILL_LABEL = "rea"
REA_FRIDA_VERSION = "17.23.2"
_COMPONENT_MANIFEST = Path(__file__).resolve().parent.parent / "resources" / "rea" / "component-manifest.json"
_NODE_RUNTIME_MANIFEST = Path(__file__).resolve().parent.parent / "resources" / "rea" / "node-runtime-manifest.json"
_CAPABILITY_CARD_MANIFEST = (
    Path(__file__).resolve().parent.parent / "resources" / "rea" / "capability-cards.json"
)
_CUTTER_BRIDGE_BOOTSTRAP = (
    Path(__file__).resolve().parent.parent
    / "resources"
    / "rea"
    / "cutter"
    / "workswarm_rea_bridge.py"
)
_CUTTER_LIBFFI_MEMBER = "libffi-8.dll"
_CUTTER_LIBFFI_SHA256 = "eff52743773eb550fcc6ce3efc37c85724502233b6b002a35496d828bd7b280a"
_CUTTER_PYTHON_RUNTIME_ARTIFACT = {
    "version": "3.12.10",
    "url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
    "fallback_urls": [
        "https://registry.npmmirror.com/-/binary/python/3.12.10/python-3.12.10-embed-amd64.zip",
        "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-embed-amd64.zip",
    ],
    "archive_type": "zip",
    "size_bytes": 11133606,
    "sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
    "destination": "rea/cache/python-3.12.10-embed-amd64.zip",
    "max_bytes": 16 * 1024 * 1024,
}
_REA_FRIDA_PREBUILDS = {
    "macos-arm64": {
        "url": (
            "https://gh-proxy.com/https://github.com/frida/frida/releases/download/"
            f"{REA_FRIDA_VERSION}/frida-v{REA_FRIDA_VERSION}-napi-v8-darwin-arm64.tar.gz"
        ),
        "fallback_urls": [
            (
                "https://ghfast.top/https://github.com/frida/frida/releases/download/"
                f"{REA_FRIDA_VERSION}/frida-v{REA_FRIDA_VERSION}-napi-v8-darwin-arm64.tar.gz"
            )
        ],
        "size_bytes": 38219481,
        "sha256": "850fba220a2d9b6aae282ac6dde37f48847e73597b7a06d648737ff9addc3340",
        "destination": f"rea/cache/frida-{REA_FRIDA_VERSION}-napi-v8-darwin-arm64.tar.gz",
        "max_bytes": 64 * 1024 * 1024,
    },
    "macos-x64": {
        "url": (
            "https://gh-proxy.com/https://github.com/frida/frida/releases/download/"
            f"{REA_FRIDA_VERSION}/frida-v{REA_FRIDA_VERSION}-napi-v8-darwin-x64.tar.gz"
        ),
        "fallback_urls": [
            (
                "https://ghfast.top/https://github.com/frida/frida/releases/download/"
                f"{REA_FRIDA_VERSION}/frida-v{REA_FRIDA_VERSION}-napi-v8-darwin-x64.tar.gz"
            )
        ],
        "size_bytes": 29267518,
        "sha256": "f8953b09720cbdccd271c1d6caa4fff085474a9f24def3fe46160526aacdb40b",
        "destination": f"rea/cache/frida-{REA_FRIDA_VERSION}-napi-v8-darwin-x64.tar.gz",
        "max_bytes": 64 * 1024 * 1024,
    },
}
_HOPPER_RELEASES_URL = (
    "https://www.hopperapp.com/include/files-api.php?request=releases&public=true"
)
_HOPPER_DOWNLOAD_PREFIX = "https://www.hopperapp.com:443/downloader/public/"
_HOPPER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
)
_HOPPER_PUBLISHER_REQUIREMENT = (
    '= identifier "com.cryptic-apps.hopper-web-4" and anchor apple generic '
    'and certificate leaf[subject.OU] = "2AMA2753NF"'
)
_GHIDRA_GRADLE_MIRROR = (
    "https://mirrors.cloud.tencent.com/gradle/"
    "gradle-{version}-bin.zip"
)
UV_RUNTIME_VERSION = "0.13.7"
UV_RUNTIME_ARTIFACTS = {
    "windows-x64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-pc-windows-msvc.zip",
        "sha256": "0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021",
        "size_bytes": 15236317,
        "destination": "rea/cache/uv-0.13.7-windows-x64.zip",
        "archive_type": "zip",
        "executable": "uv.exe",
    },
    "linux-x64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-unknown-linux-gnu.tar.gz",
        "sha256": "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83",
        "size_bytes": 16671812,
        "destination": "rea/cache/uv-0.13.7-linux-x64.tar.gz",
        "archive_type": "tar.gz",
        "executable": "uv",
    },
    "linux-arm64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-unknown-linux-gnu.tar.gz",
        "sha256": "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc",
        "size_bytes": 15896996,
        "destination": "rea/cache/uv-0.13.7-linux-arm64.tar.gz",
        "archive_type": "tar.gz",
        "executable": "uv",
    },
    "macos-x64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-apple-darwin.tar.gz",
        "sha256": "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7",
        "size_bytes": 17026382,
        "destination": "rea/cache/uv-0.13.7-macos-x64.tar.gz",
        "archive_type": "tar.gz",
        "executable": "uv",
    },
    "macos-arm64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz",
        "sha256": "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731",
        "size_bytes": 14347867,
        "destination": "rea/cache/uv-0.13.7-macos-arm64.tar.gz",
        "archive_type": "tar.gz",
        "executable": "uv",
    },
}
ProgressCallback = Callable[[str, float, str, dict[str, Any] | None], None]
DownloadProgressCallback = Callable[[float, dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class InstallCancelled(Exception):
    """Raised cooperatively when a setup job is cancelled."""


def _report_progress(
    callback: ProgressCallback | None,
    stage: str,
    percent: float,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    if callback is not None:
        callback(stage, percent, message, details)


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise InstallCancelled("安装已取消")


@dataclass(frozen=True)
class NodeRuntime:
    """A compatible Node.js and npx pair."""

    node: Path
    npx: Path
    npx_cli: Path
    version: str
    npm_version: str | None = None

    @property
    def bin_dir(self) -> Path:
        return self.node.parent


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts[:3])


def supports_rea_node(version: str) -> bool:
    """Return whether Node.js satisfies REA's published engine range."""

    parsed = _version_tuple(version)
    if len(parsed) < 2:
        return False
    major, minor = parsed[:2]
    patch = parsed[2] if len(parsed) > 2 else 0
    if major == 22:
        return minor > 23 or (minor == 23 and patch >= 4)
    if major == 24:
        return minor > 22 or (minor == 22 and patch >= 1)
    if major == 26:
        return minor > 12 or (minor == 12 and patch >= 1)
    return major > 26


def _candidate_node_paths() -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []

    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    explicit_home = os.getenv("JIUWENSWARM_NODE_HOME", "").strip()
    if explicit_home:
        candidates.append(Path(explicit_home) / ("node.exe" if os.name == "nt" else "node"))

    patterns: list[Path] = []
    if os.name == "nt":
        patterns.extend(
            (
                home / ".workbuddy" / "binaries" / "node" / "versions" / "*" / "node.exe",
                home / ".codex" / "binaries" / "node" / "versions" / "*" / "node.exe",
            )
        )
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        program_files = os.getenv("ProgramFiles", "").strip()
        if local_app_data:
            patterns.append(Path(local_app_data) / "Programs" / "nodejs" / "node.exe")
        if program_files:
            patterns.append(Path(program_files) / "nodejs" / "node.exe")
    else:
        patterns.extend(
            (
                Path("/opt/homebrew/bin/node"),
                Path("/usr/local/bin/node"),
                Path("/usr/bin/node"),
                home / ".volta" / "bin" / "node",
                home / ".local" / "bin" / "node",
            )
        )

    for pattern in patterns:
        text = str(pattern)
        if "*" in text:
            candidates.extend(Path(item) for item in sorted(Path(pattern.anchor).glob(text.removeprefix(pattern.anchor))))
        elif pattern.exists():
            candidates.append(pattern)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _process_env(runtime: NodeRuntime | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if runtime is not None:
        old_path = env.get("PATH", "")
        env["PATH"] = str(runtime.bin_dir) + (os.pathsep + old_path if old_path else "")
    return env


def _merge_process_env(runtime: NodeRuntime | None, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = _process_env(runtime)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items() if value is not None})
    return env


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": env,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, check=False, **kwargs)  # noqa: S603


def _node_version(node: Path) -> str | None:
    try:
        result = _run([str(node), "--version"], timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().removeprefix("v")


def _npm_version(node: Path) -> str | None:
    package_json = node.parent / "node_modules" / "npm" / "package.json"
    if not package_json.is_file():
        return None
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version).strip() if version else None


def resolve_node_runtime(preferred_node: str | Path | None = None) -> NodeRuntime | None:
    """Find a compatible Node.js/npx pair, preferring an already-bound runtime."""

    compatible: list[NodeRuntime] = []
    preferred_key = ""
    candidates = _candidate_node_paths()
    if preferred_node:
        try:
            preferred_path = Path(preferred_node).expanduser().resolve()
            preferred_key = os.path.normcase(str(preferred_path))
            if preferred_path.is_file():
                resolved_node = preferred_path
                if preferred_path.name.casefold() in {"npx", "npx.cmd"}:
                    resolved_node = preferred_path.with_name(
                        "node.exe" if os.name == "nt" else "node"
                    )
                    preferred_key = os.path.normcase(str(resolved_node))
                elif preferred_path.name.casefold() == "npx-cli.js":
                    node_name = "node.exe" if os.name == "nt" else "node"
                    resolved_node = preferred_path
                    for ancestor in preferred_path.parents:
                        candidates_for_node = (
                            ancestor / "bin" / node_name,
                            ancestor / node_name,
                        )
                        found_node = next(
                            (
                                candidate
                                for candidate in candidates_for_node
                                if candidate.is_file()
                            ),
                            None,
                        )
                        if found_node is not None:
                            resolved_node = found_node
                            break
                    preferred_key = os.path.normcase(str(resolved_node))
                candidates.insert(0, resolved_node)
        except OSError:
            preferred_key = ""
    for node in candidates:
        version = _node_version(node)
        if not version or not supports_rea_node(version):
            continue
        npx = node.parent / ("npx.cmd" if os.name == "nt" else "npx")
        npx_cli = node.parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if not npx.is_file() and not npx_cli.is_file():
            continue
        compatible.append(
            NodeRuntime(
                node=node,
                npx=npx,
                npx_cli=npx_cli,
                version=version,
            )
        )
    compatible.sort(
        key=lambda item: (
            os.path.normcase(str(item.node)) == preferred_key,
            _version_tuple(item.version),
        ),
        reverse=True,
    )
    if not compatible:
        return None
    selected = compatible[0]
    return replace(selected, npm_version=_npm_version(selected.node))


def _npx_argv(runtime: NodeRuntime, args: list[str]) -> list[str]:
    if runtime.npx_cli.is_file():
        return [str(runtime.node), str(runtime.npx_cli), *args]
    return [str(runtime.npx), *args]


def _npx_command(
    runtime: NodeRuntime,
    args: list[str],
    *,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = _npx_argv(runtime, args)
    if os.name == "nt" and Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
        argv = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return _run(argv, env=_merge_process_env(runtime, extra_env), timeout=timeout)


def _installed_rea_version(runtime: NodeRuntime) -> str | None:
    try:
        result = _npx_command(
            runtime,
            ["--no-install", f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}", "--version"],
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    return version or None


def _workspace_path(workspace: str | Path | None = None) -> Path:
    return Path(workspace).expanduser().resolve() if workspace is not None else get_workspace_dir()


def _state_file(workspace: str | Path | None = None) -> Path:
    return _workspace_path(workspace) / "mcp" / "state.json"


def _read_state(workspace: str | Path | None = None) -> dict[str, Any]:
    path = _state_file(workspace)
    if not path.is_file():
        return {"version": 1, "mcp": {}, "mounts": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "mcp": {}, "mounts": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "mcp": {}, "mounts": {}}
    payload.setdefault("version", 1)
    payload.setdefault("mcp", {})
    payload.setdefault("mounts", {})
    return payload


def _write_state(state: dict[str, Any], workspace: str | Path | None = None) -> None:
    path = _state_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.with_name(f"state.json.rea-setup-{int(time.time())}.bak")
        shutil.copy2(path, backup)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _rea_record(
    runtime: NodeRuntime,
    *,
    state: str,
) -> dict[str, Any]:
    env: dict[str, str] = {
        "PATH": _process_env(runtime)["PATH"],
    }

    if runtime.npx_cli.is_file():
        command = str(runtime.node)
        args = [str(runtime.npx_cli), "-y", f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}", "mcp"]
    else:
        command = str(runtime.npx)
        args = ["-y", f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}", "mcp"]
    return {
        "transport": "stdio",
        "command": command,
        "args": args,
        "env": env,
        "timeout_s": 600,
        "integration_type": "stdio-mcp",
        "server_id_scope": "mcp:rea",
        "state": state,
        "enabled": False,
    }


def rea_mcp_record(workspace: str | Path | None = None) -> dict[str, Any] | None:
    record = _read_state(workspace).get("mcp", {}).get(REA_MCP_NAME)
    return dict(record) if isinstance(record, dict) else None


@lru_cache(maxsize=1)
def load_component_manifest() -> dict[str, Any]:
    """Load Workswarm's local component install manifest."""

    try:
        payload = json.loads(_COMPONENT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "components": []}
    return payload if isinstance(payload, dict) else {"schema_version": 1, "components": []}


def _manifest_components() -> dict[str, dict[str, Any]]:
    components = load_component_manifest().get("components")
    if not isinstance(components, list):
        return {}
    return {
        str(item.get("id")): item
        for item in components
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _read_skill_metadata_version(skill_dir: Path) -> str | None:
    skill_file = skill_dir / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^\s{2}version:\s*[\"']?([^\"'\r\n]+)", text)
    return match.group(1).strip() if match else None


def _rea_skill_source_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.getenv("JIUWENSWARM_REA_SKILL_SOURCE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())

    npx_roots: list[Path] = []
    npm_cache = os.getenv("npm_config_cache", "").strip()
    if npm_cache:
        npx_roots.append(Path(npm_cache) / "_npx")
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        npx_roots.append(Path(local_app_data) / "npm-cache" / "_npx")
    npx_roots.append(Path.home() / ".npm" / "_npx")

    for root in npx_roots:
        if not root.is_dir():
            continue
        for package_json in sorted(root.glob("*/node_modules/rea-agents/package.json")):
            candidates.append(package_json.parent)

    candidates.extend(
        (
            Path.home() / ".rea" / "node_modules" / "rea-agents",
            Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "rea-agents",
        )
    )
    return candidates


def _find_rea_skill_source() -> tuple[Path, str | None] | None:
    for candidate in _rea_skill_source_candidates():
        if not candidate.exists():
            continue
        skill_dir = candidate
        if skill_dir.name != REA_SKILL_NAME:
            skill_dir = candidate / "skills" / REA_SKILL_NAME
        if not (skill_dir / "SKILL.md").is_file():
            continue
        package_json = candidate / "package.json"
        package_version: str | None = None
        if package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
                package_version = str((payload or {}).get("version") or "").strip() or None
            except (OSError, json.JSONDecodeError):
                package_version = None
        if package_version and package_version != REA_PACKAGE_VERSION:
            continue
        return skill_dir.resolve(), package_version
    return None


def _find_rea_cutter_bridge_source() -> Path:
    for candidate in _rea_skill_source_candidates():
        package_json = candidate / "package.json"
        bridge = candidate / "bridge" / "cutter" / "rea_bridge.py"
        if not package_json.is_file() or not bridge.is_file():
            continue
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str((payload or {}).get("version") or "").strip() != REA_PACKAGE_VERSION:
            continue
        return bridge.resolve()
    raise RuntimeError(
        f"未找到 rea-agents@{REA_PACKAGE_VERSION} 自带的 Cutter 桥接插件"
    )


def _cutter_user_plugin_dir() -> Path:
    if os.name == "nt":
        app_data = str(os.environ.get("APPDATA") or "").strip()
        base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        data_home = str(os.environ.get("XDG_DATA_HOME") or "").strip()
        base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "rizin" / "cutter" / "plugins" / "python"


def _cutter_bundle_version(executable: Path) -> str | None:
    for parent in executable.parents:
        if parent.suffix.casefold() != ".app":
            continue
        info_plist = parent / "Contents" / "Info.plist"
        try:
            with info_plist.open("rb") as stream:
                payload = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        version = str((payload or {}).get("CFBundleShortVersionString") or "").strip()
        return version or None
    return None


def _cutter_uses_lifecycle_bootstrap(executable: Path) -> bool:
    platform_id = _platform_id()
    normalized = str(executable).casefold()
    if platform_id == "windows-x64":
        return "cutter-v2.5.0-windows" in normalized
    if platform_id in {"macos-x64", "macos-arm64"}:
        return (_cutter_bundle_version(executable) or "").startswith("2.5.0")
    return False


def _cutter_bridge_paths(executable: Path) -> dict[str, Any]:
    executable = executable.expanduser().resolve()
    plugin_dir = _cutter_user_plugin_dir()
    uses_bootstrap = _cutter_uses_lifecycle_bootstrap(executable)
    uses_windows_runtime = uses_bootstrap and _platform_id() == "windows-x64"
    return {
        "executable": executable,
        "root": executable.parent,
        "plugin_dir": plugin_dir,
        "bridge": plugin_dir / "rea_bridge.py",
        "bootstrap": plugin_dir / "workswarm_rea_bridge.py",
        "libffi": executable.parent / "libffi-8.dll",
        "uses_bootstrap": uses_bootstrap,
        "uses_windows_runtime": uses_windows_runtime,
    }


def _copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_zip_member_atomically(
    archive: Path,
    member: str,
    destination: Path,
    expected_sha256: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
    digest = hashlib.sha256()
    try:
        with (
            zipfile.ZipFile(archive) as zipped,
            zipped.open(member) as source,
            temporary.open("wb") as output,
        ):
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(f"Cutter Python 依赖哈希不匹配: {actual}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_tar_member_atomically(
    archive: Path,
    member: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
    try:
        with tarfile.open(archive, "r:gz") as tarball:
            source = tarball.extractfile(member)
            if source is None:
                raise RuntimeError(f"预编译包缺少文件: {member}")
            with temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_rea_macos_frida_prebuild(workspace: Path) -> None:
    artifact = _REA_FRIDA_PREBUILDS.get(_platform_id())
    if artifact is None:
        return
    frida_package: Path | None = None
    for package in _rea_skill_source_candidates():
        frida_candidate = package.parent / "frida"
        package_json = frida_candidate / "package.json"
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str((payload or {}).get("version") or "").strip() == REA_FRIDA_VERSION:
            frida_package = frida_candidate
            break
    if frida_package is None:
        raise RuntimeError(
            f"REA 依赖安装后未找到 frida@{REA_FRIDA_VERSION}"
        )
    binding = frida_package / "build" / "frida_binding.node"
    if binding.is_file() and binding.stat().st_size > 0:
        return
    archive = _download_component_artifact(
        {"artifact": artifact},
        workspace,
        artifact,
    )
    _extract_tar_member_atomically(
        archive,
        "build/frida_binding.node",
        binding,
    )


def _ensure_cutter_libffi(workspace: Path, cutter_root: Path) -> Path:
    destination = cutter_root / _CUTTER_LIBFFI_MEMBER
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    artifact = dict(_CUTTER_PYTHON_RUNTIME_ARTIFACT)
    archive = _download_component_artifact(
        {"artifact": artifact},
        workspace,
        artifact,
    )
    _extract_zip_member_atomically(
        archive,
        _CUTTER_LIBFFI_MEMBER,
        destination,
        _CUTTER_LIBFFI_SHA256,
    )
    return destination


def _install_cutter_bridge(
    executable: Path,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    paths = _cutter_bridge_paths(executable)
    bridge_source = _find_rea_cutter_bridge_source()
    _copy_file_atomically(bridge_source, paths["bridge"])
    installed: dict[str, Any] = {
        "plugin_dir": str(paths["plugin_dir"]),
        "bridge": str(paths["bridge"]),
        "bootstrap": None,
        "libffi": None,
    }
    if paths["uses_bootstrap"]:
        if not _CUTTER_BRIDGE_BOOTSTRAP.is_file():
            raise RuntimeError("安装包缺少 Cutter 2.5.0 生命周期兼容插件")
        _copy_file_atomically(_CUTTER_BRIDGE_BOOTSTRAP, paths["bootstrap"])
        installed["bootstrap"] = str(paths["bootstrap"])
        if paths["uses_windows_runtime"]:
            libffi = _ensure_cutter_libffi(
                _workspace_path(workspace),
                Path(paths["root"]),
            )
            installed["libffi"] = str(libffi)
    return installed


def _cutter_bridge_readiness(executable: Path) -> tuple[bool, str]:
    paths = _cutter_bridge_paths(executable)
    if not paths["bridge"].is_file() or paths["bridge"].stat().st_size <= 0:
        return False, "REA Cutter 桥接插件尚未安装"
    if paths["uses_bootstrap"]:
        if (
            not paths["bootstrap"].is_file()
            or paths["bootstrap"].stat().st_size <= 0
        ):
            return False, "Cutter 2.5.0 的桥接启动兼容插件尚未安装"
        if paths["uses_windows_runtime"] and (
            not paths["libffi"].is_file() or paths["libffi"].stat().st_size <= 0
        ):
            return False, "Cutter 便携包缺少 libffi-8.dll，Python 插件无法加载"
    return True, "REA Cutter 桥接插件已安装；启动 Cutter 后可连接"


def ensure_rea_skill(workspace: str | Path | None = None) -> dict[str, Any]:
    """Install the REA npm package's bundled skill into the active workspace."""

    workspace_path = _workspace_path(workspace)
    destination = workspace_path / "skills" / REA_SKILL_NAME
    source = _find_rea_skill_source()
    if source is None:
        return {
            "name": REA_SKILL_NAME,
            "label": REA_SKILL_LABEL,
            "installed": (destination / "SKILL.md").is_file(),
            "updated": False,
            "path": str(destination) if (destination / "SKILL.md").is_file() else None,
            "version": _read_skill_metadata_version(destination),
            "error": "未找到 REA 6.8.0 自带的 Skill 文件",
        }

    source_dir, package_version = source
    source_version = _read_skill_metadata_version(source_dir)
    destination_version = _read_skill_metadata_version(destination)
    if destination_version == source_version and destination_version is not None:
        return {
            "name": REA_SKILL_NAME,
            "label": REA_SKILL_LABEL,
            "installed": True,
            "updated": False,
            "path": str(destination),
            "version": destination_version,
            "package_version": package_version,
            "error": None,
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = workspace_path / "rea" / "skill-staging" / f"{REA_SKILL_NAME}-{int(time.time())}"
    backup_root = workspace_path / "rea" / "skill-backups"
    backup: Path | None = None
    try:
        if staging.exists():
            shutil.rmtree(staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, staging)
        if destination.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / f"{REA_SKILL_NAME}-{int(time.time())}"
            shutil.copytree(destination, backup)
            shutil.rmtree(destination)
        staging.replace(destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.is_dir() and not destination.exists():
            shutil.copytree(backup, destination)
        return {
            "name": REA_SKILL_NAME,
            "label": REA_SKILL_LABEL,
            "installed": (destination / "SKILL.md").is_file(),
            "updated": False,
            "path": str(destination) if (destination / "SKILL.md").is_file() else None,
            "version": destination_version,
            "package_version": package_version,
            "error": str(exc),
        }
    return {
        "name": REA_SKILL_NAME,
        "label": REA_SKILL_LABEL,
        "installed": True,
        "updated": True,
        "path": str(destination),
        "version": source_version,
        "package_version": package_version,
        "error": None,
    }


def _platform_id() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine_name = "x64" if machine in {"amd64", "x86_64"} else "arm64" if machine in {"arm64", "aarch64"} else machine
    if system == "darwin":
        system = "macos"
    return f"{system}-{machine_name}"


def _platform_supported(component: dict[str, Any]) -> bool:
    values = component.get("platforms")
    return not isinstance(values, list) or _platform_id() in {str(item) for item in values}


def _auto_install_supported(component: dict[str, Any]) -> bool:
    values = component.get("auto_platforms")
    return not isinstance(values, list) or _platform_id() in {str(item) for item in values}


def _component_installer(component: dict[str, Any]) -> dict[str, Any] | None:
    installers = component.get("installer_by_platform")
    if isinstance(installers, dict):
        installer = installers.get(_platform_id())
        return installer if isinstance(installer, dict) else None
    installer = component.get("installer")
    return installer if isinstance(installer, dict) else None


def _record_env(record: dict[str, Any] | None) -> dict[str, str]:
    env = record.get("env") if record else None
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if value is not None}


def _java_full_jdk_home(
    extra_env: dict[str, str] | None = None,
    *,
    minimum_major: int = 17,
) -> Path | None:
    runtime_env = os.environ | (extra_env or {})
    java_home = str(runtime_env.get("JAVA_HOME") or "").strip()
    candidates: list[Path] = []
    if java_home:
        candidates.append(Path(java_home))
    found_java = shutil.which("java", path=runtime_env.get("PATH"))
    if found_java:
        candidates.append(Path(found_java).resolve().parent.parent)
    home = Path.home()
    patterns: list[Path] = [
        home / ".jdks" / "jdk-*",
        home / "tools" / "jdk*",
        Path("C:/tools/jdk*"),
        Path("D:/tools/jdk*"),
        Path("C:/apps/jdk*"),
        Path("D:/apps/jdk*"),
        Path("C:/software/java/jdk*"),
        Path("D:/software/java/jdk*"),
        Path("/usr/lib/jvm/*"),
        Path("/Library/Java/JavaVirtualMachines/*/Contents/Home"),
    ]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = str(runtime_env.get(env_name) or "").strip()
        if not root:
            continue
        patterns.extend(
            (
                Path(root) / "Java" / "jdk*",
                Path(root) / "Eclipse Adoptium" / "jdk-*",
                Path(root) / "Microsoft" / "jdk-*",
            )
        )
    for pattern in patterns:
        try:
            candidates.extend(Path(item) for item in sorted(pattern.parent.glob(pattern.name), reverse=True))
        except OSError:
            continue
    seen: set[str] = set()
    for home in candidates:
        resolved_home = home.expanduser().resolve()
        key = os.path.normcase(str(resolved_home))
        if key in seen:
            continue
        seen.add(key)
        if _java_home_is_full_jdk(resolved_home, minimum_major=minimum_major):
            return resolved_home
    return None


def _java_home_is_full_jdk(home: Path, *, minimum_major: int = 17) -> bool:
    java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not java.is_file():
        return False
    try:
        modules = _run([str(java), "--list-modules"], timeout=10)
        version = _run([str(java), "-version"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    combined = f"{modules.stdout}\n{modules.stderr}"
    if modules.returncode != 0 or "jdk.compiler" not in combined:
        return False
    version_text = f"{version.stdout}\n{version.stderr}"
    match = re.search(r'version "(?P<version>[^"]+)"', version_text)
    return bool(match and _version_tuple(match.group("version")) >= (minimum_major,))


def _java_full_jdk(
    extra_env: dict[str, str] | None = None,
    *,
    minimum_major: int = 17,
) -> bool:
    return _java_full_jdk_home(extra_env, minimum_major=minimum_major) is not None


def _requirement_satisfied(name: str, extra_env: dict[str, str] | None = None) -> bool:
    if name == "java-full-jdk":
        return _java_full_jdk(extra_env)
    if name == "java-full-jdk-21":
        return _java_full_jdk(extra_env, minimum_major=21)
    return False


def _filter_assisted_setup(
    assisted_setup: dict[str, Any],
    platform_id: str,
) -> dict[str, Any]:
    """Copy assisted setup data while removing entries for other hosts."""

    filtered = copy.deepcopy(assisted_setup)
    for key in ("links", "commands"):
        entries = filtered.get(key)
        if not isinstance(entries, list):
            continue
        filtered[key] = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and (
                not isinstance(entry.get("platforms"), list)
                or platform_id in {str(value) for value in entry["platforms"]}
            )
        ]
    return filtered


def _package_manager_config(component: dict[str, Any]) -> dict[str, Any] | None:
    managers = component.get("package_manager_by_platform")
    if not isinstance(managers, dict):
        return None
    value = managers.get(_platform_id())
    return value if isinstance(value, dict) else None


def _apply_manifest_overlay(
    item: dict[str, Any],
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    component = _manifest_components().get(str(item.get("id") or ""))
    if component is None:
        generic = dict(item)
        generic.setdefault("install_mode", "unknown")
        generic.setdefault("install_supported", False)
        generic.setdefault("default_selected", False)
        return generic
    merged = dict(item)
    merged["label"] = str(component.get("display_name") or merged.get("label") or merged.get("id"))
    merged["group"] = str(component.get("group") or merged.get("group") or "")
    merged["install_mode"] = str(component.get("install_mode") or "unknown")
    merged["install_hint"] = str(component.get("install_hint") or "")
    merged["default_selected"] = bool(component.get("default_selected", False))
    if isinstance(component.get("assisted_setup"), dict):
        merged["assisted_setup"] = _filter_assisted_setup(
            component["assisted_setup"],
            _platform_id(),
        )
    platform_supported = _platform_supported(component)
    requirements = component.get("requires")
    requirement_ok = (
        not isinstance(requirements, list)
        or all(
            _requirement_satisfied(str(name), extra_env)
            for name in requirements
        )
    )
    package_manager = _package_manager_config(component)
    auto_modes = {"managed", "rea_setup", "python_venv", "python_tool"}
    merged["install_supported"] = (
        (
            merged["install_mode"] in auto_modes
            or (
                merged["install_mode"] == "package_manager"
                and package_manager is not None
            )
        )
        and platform_supported
        and _auto_install_supported(component)
        and requirement_ok
    )
    if not platform_supported:
        merged["status"] = "unsupported_host"
        merged["code"] = "unsupported_host"
        merged["reason"] = "当前平台不支持此能力"
    elif merged["install_mode"] in auto_modes and not requirement_ok:
        merged["install_reason"] = "缺少自动安装所需依赖"
    elif (
        merged["install_mode"] in auto_modes
        and platform_supported
        and not _auto_install_supported(component)
        and not merged.get("detected")
    ):
        merged["install_reason"] = "当前平台支持使用该能力，但尚未提供自动安装适配器"
        hint = merged["install_hint"]
        if hint:
            merged["reason"] = f"{merged.get('reason') or ''}；{hint}".strip("；")
    elif not merged.get("detected") and merged["install_mode"] not in auto_modes:
        hint = merged["install_hint"]
        base_reason = str(merged.get("reason") or "").strip()
        if hint:
            merged["reason"] = f"{base_reason}；{hint}".strip("；")
    if merged["install_mode"] in auto_modes and not platform_supported:
        merged["install_reason"] = "当前平台不支持自动安装"
    return merged


def _static_enhancements(extra_env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    runtime_env = os.environ | (extra_env or {})
    java_home = _java_full_jdk_home(extra_env, minimum_major=21)
    checks = (
        ("jadx", "JADX / Android", "REA_JADX_MCP_JAR", ()),
        ("adb", "ADB / Android 设备", "REA_ADB_PATH", ("adb",)),
        ("apktool", "Apktool / Android 资源", "REA_APKTOOL_COMMAND", ("apktool", "apktool.bat")),
        ("firmware", "固件提取", "REA_BINWALK_COMMAND", ("binwalk",)),
        ("pcap", "PCAP 分析", "REA_TSHARK_COMMAND", ("tshark",)),
        ("wakaru", "JavaScript 恢复", "REA_WAKARU_COMMAND", ("wakaru",)),
        ("jeb", "JEB / Android 反编译", "REA_JEB_MCP_URL", ()),
    )
    output: list[dict[str, Any]] = [
        _apply_manifest_overlay(
            {
                "id": "java",
                "label": "Java 21 / JDK",
                "group": "specialized_tool",
                "detected": java_home is not None,
                "available": java_home is not None,
                "status": "configured" if java_home is not None else "not_configured",
                "code": None if java_home is not None else "not_configured",
                "reason": None if java_home is not None else "未找到 JDK 21 或更高版本",
                "env_key": "JAVA_HOME",
                "version": java_home.name if java_home is not None else None,
            },
            extra_env,
        )
    ]
    for provider_id, label, env_key, command_names in checks:
        value = str(runtime_env.get(env_key) or "").strip()
        if not value:
            value = next((found for name in command_names if (found := shutil.which(name))), "")
        detected = (
            value.startswith(("http://", "https://"))
            if provider_id == "jeb"
            else bool(value and Path(value).expanduser().is_file())
        )
        if provider_id == "pcap":
            capinfos = str(runtime_env.get("REA_CAPINFOS_COMMAND") or "").strip()
            if not capinfos:
                capinfos = shutil.which("capinfos") or ""
            detected = detected and bool(capinfos and Path(capinfos).expanduser().is_file())
        output.append(
            _apply_manifest_overlay(
                {
                    "id": provider_id,
                    "label": label,
                    "group": "specialized_tool",
                    "detected": detected,
                    "available": None,
                    "status": "configured" if detected else "not_configured",
                    "code": None if detected else "not_configured",
                    "reason": (
                        None
                        if detected
                        else (
                            "未同时找到 tshark 与 capinfos"
                            if provider_id == "pcap"
                            else f"未设置或找不到 {env_key}"
                        )
                    ),
                    "env_key": env_key,
                },
                extra_env,
            )
        )
    return output


def mark_rea_mcp_state(
    state: str,
    workspace: str | Path | None = None,
    *,
    tool_count: int | None = None,
) -> None:
    payload = _read_state(workspace)
    record = payload.get("mcp", {}).get(REA_MCP_NAME)
    if not isinstance(record, dict):
        return
    record["state"] = state
    if tool_count is not None:
        record["tool_count"] = max(0, int(tool_count))
    _write_state(payload, workspace)


def _mcp_registration_check(
    runtime: NodeRuntime | None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    record = rea_mcp_record(workspace)
    if not record:
        return {
            "id": "rea_mcp",
            "label": "REA MCP",
            "ok": False,
            "required": True,
            "state": "missing",
            "detail": "未注册 REA MCP",
        }
    command = str(record.get("command") or "")
    args = [str(item) for item in record.get("args") or []]
    joined = " ".join([command, *args])
    ok = (
        record.get("transport") == "stdio"
        and record.get("state") == "connected"
        and f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}" in joined
        and "mcp" in args
        and (runtime is None or command == str(runtime.node))
    )
    return {
        "id": "rea_mcp",
        "label": "REA MCP",
        "ok": ok,
        "required": True,
        "state": str(record.get("state") or "registered") if ok else "config_drift",
        "detail": "已注册固定版本 REA MCP" if ok else "MCP 注册不是当前安装器写入的固定配置",
        "command": command,
        "args": args,
        "tool_count": int(record.get("tool_count") or 0),
    }


def _enhancement_from_provider(
    item: dict[str, Any],
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    provider = item.get("provider") if isinstance(item.get("provider"), dict) else {}
    availability = item.get("availability") if isinstance(item.get("availability"), dict) else {}
    provider_id = str(provider.get("id") or "").strip()
    if not provider_id:
        return {}
    return _apply_manifest_overlay(
        {
            "id": provider_id,
            "label": str(provider.get("name") or provider_id),
            "group": "analysis_engine",
            "detected": bool(item.get("configured") or availability.get("status") == "available"),
            "available": availability.get("status") == "available",
            "status": str(availability.get("status") or "unknown"),
            "code": availability.get("code"),
            "reason": availability.get("reason"),
        },
        extra_env,
    )


def _scan_enhancements(
    runtime: NodeRuntime | None,
    extra_env: dict[str, str] | None = None,
    workspace: str | Path | None = None,
) -> list[dict[str, Any]]:
    workspace_path = _workspace_path(workspace)
    if runtime is None:
        items = _static_enhancements(extra_env)
    else:
        items = _scan_enhancements_via_rea(runtime, extra_env)
    known = {str(item.get("id") or "") for item in items}
    for component_id, component in _manifest_components().items():
        if component_id in known:
            continue
        mode = str(component.get("install_mode") or "manual")
        items.append(
            _apply_manifest_overlay(
                {
                    "id": component_id,
                    "label": str(component.get("display_name") or component_id),
                    "group": str(component.get("group") or ""),
                    "detected": False,
                    "available": None,
                    "status": "not_configured",
                    "code": "not_configured",
                    "reason": str(component.get("install_hint") or "需要安装或配置"),
                    "install_mode": mode,
                },
                extra_env,
            )
        )
    return [
        _with_component_inventory(item, extra_env, workspace_path)
        for item in items
    ]


def _scan_enhancements_via_rea(
    runtime: NodeRuntime,
    extra_env: dict[str, str] | None,
) -> list[dict[str, Any]]:
    try:
        result = _npx_command(
            runtime,
            ["--no-install", f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}", "providers", "--json"],
            timeout=20,
            extra_env=extra_env,
        )
    except (OSError, subprocess.SubprocessError):
        return _static_enhancements(extra_env)
    if result.returncode != 0 or not result.stdout.strip():
        return _static_enhancements(extra_env)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _static_enhancements(extra_env)
    providers = payload.get("analysis_provider_candidates")
    dynamic: list[dict[str, Any]] = []
    if isinstance(providers, list):
        for item in providers:
            if isinstance(item, dict):
                normalized = _enhancement_from_provider(item, extra_env)
                if normalized:
                    dynamic.append(normalized)
    known = {item.get("id") for item in dynamic}
    dynamic.extend(item for item in _static_enhancements(extra_env) if item["id"] not in known)
    return dynamic


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _human_bytes(value: int | float) -> str:
    size = max(0.0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def _download_detail(
    *,
    phase: str,
    source: str,
    downloaded_bytes: int,
    total_bytes: int,
    started_at: float,
    initial_bytes: int = 0,
    source_index: int = 0,
    source_count: int = 1,
) -> dict[str, Any]:
    elapsed = max(0.001, time.monotonic() - started_at)
    transferred = max(0, downloaded_bytes - initial_bytes)
    rate = transferred / elapsed
    remaining = max(0, total_bytes - downloaded_bytes) if total_bytes else None
    eta = int(remaining / rate) if remaining is not None and rate > 0 else None
    return {
        "phase": phase,
        "source": source,
        "downloaded_bytes": downloaded_bytes,
        "total_bytes": total_bytes or None,
        "bytes_per_second": int(rate),
        "eta_seconds": eta,
        "source_index": source_index,
        "source_count": source_count,
        "mirror_fallback": source_index > 0,
    }


def _preferred_download_sources(sources: list[str]) -> list[str]:
    """Try verified domestic mirrors and proxies before foreign origins."""

    ranked_prefixes = (
        ("https://mirrors.tuna.tsinghua.edu.cn/", 0),
        ("https://mirrors.cloud.tencent.com/", 1),
        ("https://mirrors.huaweicloud.com/", 1),
        ("https://registry.npmmirror.com/", 1),
        ("https://gh-proxy.com/", 2),
        ("https://ghfast.top/", 2),
    )
    indexed = list(enumerate(sources))

    def rank(item: tuple[int, str]) -> tuple[int, int]:
        index, source = item
        for prefix, priority in ranked_prefixes:
            if source.startswith(prefix):
                return priority, index
        return 3, index

    return [source for _, source in sorted(indexed, key=rank)]


def _download_component_artifact(
    component: dict[str, Any],
    workspace: Path,
    artifact: dict[str, Any] | None = None,
    progress_callback: DownloadProgressCallback | None = None,
) -> Path:
    artifact = artifact or component.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("组件安装清单缺少 artifact")
    url = str(artifact.get("url") or "").strip()
    expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
    expected_sha1 = str(artifact.get("sha1") or "").strip().lower()
    hash_name = "sha256" if expected_sha256 else "sha1"
    expected = expected_sha256 or expected_sha1
    destination = str(artifact.get("destination") or "").strip()
    if not url or not expected or not destination:
        raise RuntimeError("组件安装清单中的下载信息不完整")
    target = workspace / destination
    expected_size = int(artifact.get("size_bytes") or 0)
    if target.is_file():
        digest = _file_digest(target, hash_name)
        if digest == expected:
            if progress_callback:
                progress_callback(
                    1.0,
                    _download_detail(
                        phase="cached",
                        source="local-cache",
                        downloaded_bytes=target.stat().st_size,
                        total_bytes=expected_size or target.stat().st_size,
                        started_at=time.monotonic(),
                    ),
                )
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    configured_max_bytes = int(artifact.get("max_bytes") or 0)
    expected_cap = max(expected_size + 1024 * 1024, 16 * 1024 * 1024)
    max_bytes = max(configured_max_bytes, expected_cap)
    fallback_urls = artifact.get("fallback_urls")
    sources = [url]
    fallback_timeout = int(artifact.get("fallback_timeout_seconds") or 300)
    primary_timeout = int(artifact.get("primary_timeout_seconds") or 45)
    user_agent = str(artifact.get("user_agent") or "").strip()
    source_timeouts = {url: primary_timeout}
    resume = artifact.get("resume", True) is not False
    if isinstance(fallback_urls, list):
        for item in fallback_urls:
            source_url = str(item).strip()
            if source_url:
                sources.append(source_url)
                source_timeouts[source_url] = fallback_timeout
    github_proxy = os.getenv("JIUWENSWARM_REA_GITHUB_PROXY", "").strip().rstrip("/")
    if github_proxy:
        for source in list(sources):
            if source.startswith("https://github.com/"):
                proxied = f"{github_proxy}/{source}"
                if proxied not in sources:
                    sources.append(proxied)
                    source_timeouts[proxied] = fallback_timeout
    sources = _preferred_download_sources(sources)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        last_error = "组件下载失败"
        for source_index, source in enumerate(sources):
            max_time = source_timeouts.get(source, fallback_timeout)
            process: subprocess.Popen[str] | None = None
            try:
                if not resume:
                    temp.unlink(missing_ok=True)
                initial_bytes = (
                    temp.stat().st_size
                    if resume and temp.is_file()
                    else 0
                )
                if expected_size and initial_bytes == expected_size:
                    if _file_digest(temp, hash_name) == expected:
                        temp.replace(target)
                        return target
                    temp.unlink(missing_ok=True)
                    initial_bytes = 0
                started_at = time.monotonic()
                if progress_callback:
                    progress_callback(
                        0.0,
                        _download_detail(
                            phase="connecting",
                            source=source,
                            downloaded_bytes=initial_bytes,
                            total_bytes=expected_size,
                            started_at=started_at,
                            initial_bytes=initial_bytes,
                            source_index=source_index,
                            source_count=len(sources),
                        ),
                    )
                command = [
                    curl,
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    str(max_time),
                    "--output",
                    str(temp),
                ]
                if artifact.get("http1_1") is True:
                    command.insert(2, "--http1.1")
                if resume:
                    command[-2:-2] = ["--continue-at", "-"]
                if user_agent:
                    command.extend(["--user-agent", user_agent])
                command.append(source)
                process = subprocess.Popen(  # noqa: S603
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
                last_report = 0.0
                while process.poll() is None:
                    now = time.monotonic()
                    downloaded = temp.stat().st_size if temp.is_file() else 0
                    if progress_callback and now - last_report >= 0.5:
                        progress_callback(
                            min(0.995, downloaded / expected_size) if expected_size else 0.05,
                            _download_detail(
                                phase="downloading",
                                source=source,
                                downloaded_bytes=downloaded,
                                total_bytes=expected_size,
                                started_at=started_at,
                                initial_bytes=initial_bytes,
                                source_index=source_index,
                                source_count=len(sources),
                            ),
                        )
                        last_report = now
                    if now - started_at > max_time + 8:
                        process.kill()
                        break
                    time.sleep(0.25)
                stdout, stderr = process.communicate()
                if process.returncode != 0:
                    last_error = (stderr or stdout or "组件下载失败").strip()[-2000:]
                    continue
                if temp.stat().st_size > max_bytes:
                    last_error = "组件下载超过允许的最大体积"
                    temp.unlink(missing_ok=True)
                    continue
                if progress_callback:
                    progress_callback(
                        0.998,
                        _download_detail(
                            phase="verifying",
                            source=source,
                            downloaded_bytes=temp.stat().st_size,
                            total_bytes=expected_size or temp.stat().st_size,
                            started_at=started_at,
                            initial_bytes=initial_bytes,
                            source_index=source_index,
                            source_count=len(sources),
                        ),
                    )
                actual = _file_digest(temp, hash_name)
                if actual != expected:
                    last_error = f"组件哈希不匹配: {actual}"
                    temp.unlink(missing_ok=True)
                    continue
                temp.replace(target)
                return target
            except InstallCancelled:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
        raise RuntimeError(last_error)

    last_error = "组件下载失败"
    for source_index, source in enumerate(sources):
        digest = hashlib.new(hash_name)
        total = 0
        started_at = time.monotonic()
        temp.unlink(missing_ok=True)
        try:
            if progress_callback:
                progress_callback(
                    0.0,
                    _download_detail(
                        phase="connecting",
                        source=source,
                        downloaded_bytes=0,
                        total_bytes=expected_size,
                        started_at=started_at,
                        source_index=source_index,
                        source_count=len(sources),
                    ),
                )
            request = urllib.request.Request(
                source,
                headers={"User-Agent": user_agent or "Workswarm-REA/1.0"},
            )
            with urllib.request.urlopen(request, timeout=15) as response, temp.open("wb") as output:
                content_length = int(response.headers.get("Content-Length") or expected_size or 0)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError("组件下载超过允许的最大体积")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress_callback:
                        progress_callback(
                            min(0.995, total / content_length) if content_length else 0.05,
                            _download_detail(
                                phase="downloading",
                                source=source,
                                downloaded_bytes=total,
                                total_bytes=content_length,
                                started_at=started_at,
                                source_index=source_index,
                                source_count=len(sources),
                            ),
                        )
            if progress_callback:
                progress_callback(
                    0.998,
                    _download_detail(
                        phase="verifying",
                        source=source,
                        downloaded_bytes=total,
                        total_bytes=total,
                        started_at=started_at,
                        source_index=source_index,
                        source_count=len(sources),
                    ),
                )
            actual = digest.hexdigest()
            if actual != expected:
                raise RuntimeError(f"组件哈希不匹配: {actual}")
            temp.replace(target)
            return target
        except InstallCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            temp.unlink(missing_ok=True)
    raise RuntimeError(last_error)


def _component_artifact(component: dict[str, Any]) -> dict[str, Any]:
    artifacts = component.get("artifact_by_platform")
    if isinstance(artifacts, dict):
        artifact = artifacts.get(_platform_id())
        if isinstance(artifact, dict):
            merged = dict(artifact)
            for key in ("primary_timeout_seconds", "fallback_timeout_seconds"):
                if key not in merged and key in component:
                    merged[key] = component[key]
            return merged
        raise RuntimeError(f"组件没有适用于 {_platform_id()} 的下载包")
    artifact = component.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("组件安装清单缺少 artifact")
    return artifact


def _extract_zip_safely(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    modes: list[tuple[Path, int]] = []
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            member = destination / info.filename
            resolved = member.resolve()
            if resolved != root and root not in resolved.parents:
                raise RuntimeError(f"压缩包包含越界路径: {info.filename}")
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                modes.append((resolved, mode))
        zipped.extractall(destination)
    if os.name != "nt":
        for member, mode in modes:
            if member.exists() and not member.is_symlink():
                member.chmod(mode)


def _restore_zip_file_modes(archive: Path, destination: Path) -> None:
    if os.name == "nt" or not archive.is_file() or not destination.is_dir():
        return
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            mode = (info.external_attr >> 16) & 0o777
            if not mode:
                continue
            member = destination / info.filename
            resolved = member.resolve()
            if resolved != root and root not in resolved.parents:
                raise RuntimeError(f"压缩包包含越界路径: {info.filename}")
            if resolved.exists() and not resolved.is_symlink():
                resolved.chmod(mode)


def _copy_dmg_app_bundles(mount: Path, destination: Path) -> None:
    bundles = [
        item
        for item in mount.iterdir()
        if item.suffix.casefold() == ".app" and not item.is_symlink()
    ]
    if not bundles:
        raise RuntimeError("DMG 中没有可安装的 .app bundle")
    for bundle in bundles:
        target = destination / bundle.name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        result = _run(["ditto", str(bundle), str(target)], timeout=600)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ditto app bundle 复制失败").strip()
            raise RuntimeError(detail[-4000:])


def _extract_archive_safely(
    archive: Path,
    destination: Path,
    archive_type: str,
) -> None:
    if archive_type in {"tar.gz", "tar.xz"}:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        mode = "r:gz" if archive_type == "tar.gz" else "r:xz"
        with tarfile.open(archive, mode) as tarball:
            for member in tarball.getmembers():
                resolved = (destination / member.name).resolve()
                if resolved != root and root not in resolved.parents:
                    raise RuntimeError(f"压缩包包含越界路径: {member.name}")
            tarball.extractall(destination, filter="data")
        return
    if archive_type == "pkg":
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.extract-",
                dir=destination.parent,
            )
        )
        staging.rmdir()
        backup: Path | None = None
        try:
            result = _run(
                ["pkgutil", "--expand-full", str(archive), str(staging)],
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "PKG 展开失败").strip())
            if destination.exists():
                backup = destination.with_name(
                    f".{destination.name}.backup-{time.time_ns()}"
                )
                destination.replace(backup)
            staging.replace(destination)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if backup is not None and backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return
    if archive_type == "dmg":
        destination.mkdir(parents=True, exist_ok=True)
        mount = destination / ".mount"
        mount.mkdir(parents=True, exist_ok=True)
        attached = False
        try:
            result = _run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(archive)],
                timeout=180,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "DMG 挂载失败").strip())
            attached = True
            _copy_dmg_app_bundles(mount, destination)
        finally:
            if attached:
                _run(["hdiutil", "detach", str(mount), "-force"], timeout=60)
            shutil.rmtree(mount, ignore_errors=True)
        return
    if archive_type == "appimage":
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / archive.name
        shutil.copy2(archive, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return
    _extract_zip_safely(archive, destination)


def load_node_runtime_manifest() -> dict[str, Any]:
    """Load the pinned managed Node runtime manifest."""

    return json.loads(_NODE_RUNTIME_MANIFEST.read_text(encoding="utf-8"))


def ensure_node_runtime(
    workspace: str | Path | None = None,
    preferred_node: str | Path | None = None,
    progress_callback: DownloadProgressCallback | None = None,
) -> NodeRuntime:
    """Resolve a compatible local Node runtime or install the pinned fallback."""

    runtime = resolve_node_runtime(preferred_node)
    if runtime is not None:
        return runtime

    workspace_path = _workspace_path(workspace)
    manifest = load_node_runtime_manifest()
    platform_data = (manifest.get("platforms") or {}).get(_platform_id())
    if not isinstance(platform_data, dict):
        raise RuntimeError(f"没有适用于 {_platform_id()} 的受管 Node 运行时")

    version = str(manifest.get("version") or "").strip()
    extract_to = str(platform_data.get("extract_to") or "").strip()
    if not version or not extract_to:
        raise RuntimeError("受管 Node 运行时清单不完整")

    root = workspace_path / extract_to
    node = root / str(platform_data.get("node") or "")
    npx = root / str(platform_data.get("npx") or "")
    npx_cli = root / str(platform_data.get("npx_cli") or "")
    if not node.is_file():
        artifact = {
            "url": platform_data.get("url"),
            "fallback_urls": platform_data.get("fallback_urls") or [],
            "size_bytes": platform_data.get("size_bytes"),
            "sha256": platform_data.get("sha256"),
            "destination": platform_data.get("destination"),
            "max_bytes": 128 * 1024 * 1024,
            "primary_timeout_seconds": 25,
            "fallback_timeout_seconds": 900,
        }
        archive = _download_component_artifact(
            {"artifact": artifact},
            workspace_path,
            artifact,
            progress_callback,
        )
        _extract_archive_safely(
            archive,
            root,
            str(platform_data.get("archive_type") or "zip"),
        )
    if not node.is_file() or not npx.is_file() or not npx_cli.is_file():
        raise RuntimeError("受管 Node 运行时解压后缺少 node、npx 或 npx-cli")
    if os.name != "nt":
        for executable in (node, npx):
            executable.chmod(
                executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
    detected = _node_version(node)
    if detected is None or not supports_rea_node(detected):
        raise RuntimeError(f"受管 Node {detected or version} 不满足 REA 版本要求")
    return NodeRuntime(
        node=node.resolve(),
        npx=npx.resolve(),
        npx_cli=npx_cli.resolve(),
        version=detected,
        npm_version=str(manifest.get("npm_version") or "").strip() or None,
    )


def ensure_uv_runtime(
    workspace: str | Path,
    progress_callback: DownloadProgressCallback | None = None,
) -> Path:
    """Install and return the pinned uv executable."""

    workspace_path = _workspace_path(workspace)
    data = UV_RUNTIME_ARTIFACTS.get(_platform_id())
    if not isinstance(data, dict):
        raise RuntimeError(f"没有适用于 {_platform_id()} 的受管 uv")
    root = workspace_path / "rea" / "tools" / "uv" / UV_RUNTIME_VERSION
    executable = root / str(data["executable"])
    if not executable.is_file():
        artifact = {
            "url": data["url"],
            "fallback_urls": [
                f"https://gh-proxy.com/{data['url']}",
                f"https://ghfast.top/{data['url']}",
            ],
            "sha256": data["sha256"],
            "size_bytes": data["size_bytes"],
            "destination": data["destination"],
            "max_bytes": 64 * 1024 * 1024,
            "primary_timeout_seconds": 25,
            "fallback_timeout_seconds": 900,
        }
        archive = _download_component_artifact(
            {"artifact": artifact},
            workspace_path,
            artifact,
            progress_callback,
        )
        _extract_archive_safely(
            archive,
            root,
            str(data["archive_type"]),
        )
        found = next(
            (
                candidate
                for candidate in root.rglob(str(data["executable"]))
                if candidate.is_file()
            ),
            None,
        )
        if found is None:
            raise RuntimeError("uv 解压后缺少可执行文件")
        if found != executable:
            executable.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, executable)
    if os.name != "nt":
        executable.chmod(
            executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
    return executable


def _python_venv_executables(root: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "mitmdump.exe"
    return root / "bin" / "python", root / "bin" / "mitmdump"


def _find_component_executable(root: Path, names: list[str] | tuple[str, ...]) -> Path | None:
    name_order = {
        str(name).casefold(): index
        for index, name in enumerate(names)
        if str(name).strip()
    }
    if not name_order or not root.is_dir():
        return None
    matches = [
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.name.casefold() in name_order
    ]
    return (
        min(
            matches,
            key=lambda candidate: (
                name_order.get(candidate.name.casefold(), len(name_order)),
                len(candidate.relative_to(root).parts),
                os.path.normcase(str(candidate.relative_to(root))),
            ),
        )
        if matches
        else None
    )


def _install_python_component(
    component_id: str,
    component: dict[str, Any],
    workspace: Path,
    progress_callback: ProgressCallback | None,
    *,
    component_start: float,
    component_end: float,
    cancel_check: CancelCheck | None,
) -> dict[str, str]:
    """Create an isolated uv environment for a manifest Python tool."""

    packages = component.get("python_packages")
    if not isinstance(packages, dict) or not packages:
        raise RuntimeError("Python 组件缺少固定包版本")
    root = workspace / "rea" / "tools" / "python" / component_id
    python, mitmdump = _python_venv_executables(root)
    if not python.is_file():
        uv = ensure_uv_runtime(
            workspace,
            lambda fraction, detail: _report_progress(
                progress_callback,
                f"component_{component_id}_download",
                component_start + max(0.0, min(1.0, fraction)) * (component_end - component_start) * 0.25,
                "准备受管 uv",
                {"component_id": component_id, **detail},
            ),
        )
        _report_progress(
            progress_callback,
            f"component_{component_id}_install",
            component_start + (component_end - component_start) * 0.3,
            "创建独立 Python 环境",
            {"component_id": component_id, "phase": "installing"},
        )
        result = _run(
            [str(uv), "venv", "--python", "3.12", str(root)],
            env=os.environ
            | {
                "UV_PYTHON_INSTALL_DIR": str(workspace / "rea" / "runtimes" / "python"),
            },
            timeout=900,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "创建 Python 环境失败").strip())

    requirements = [f"{name}=={version}" for name, version in packages.items()]
    uv = ensure_uv_runtime(workspace)
    indexes = [
        "https://pypi.org/simple",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
    ]
    last_error = "Python 包安装失败"
    for index, url in enumerate(indexes):
        _raise_if_cancelled(cancel_check)
        _report_progress(
            progress_callback,
            f"component_{component_id}_download",
            component_start + (component_end - component_start) * (0.4 + index * 0.15),
            f"安装固定 Python 包（来源 {url}）",
            {
                "component_id": component_id,
                "phase": "downloading",
                "source": url,
                "source_index": index,
                "source_count": len(indexes),
                "mirror_fallback": index > 0,
            },
        )
        result = _run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(python),
                "--index-url",
                url,
                *requirements,
            ],
            env=os.environ,
            timeout=1200,
        )
        if result.returncode == 0:
            last_error = ""
            break
        last_error = (result.stderr or result.stdout or last_error).strip()
    if last_error:
        raise RuntimeError(last_error[-4000:])
    if not python.is_file():
        raise RuntimeError("受管 Python 环境缺少解释器")
    if component_id == "pwntools":
        return {"REA_PWNTOOLS_PYTHON": str(python)}
    if component_id == "mitmproxy" and mitmdump.is_file():
        if os.name != "nt":
            mitmdump.chmod(
                mitmdump.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        return {"REA_MITMDUMP_COMMAND": str(mitmdump)}
    raise RuntimeError(f"{component_id} Python 环境没有生成预期执行文件")


def _java_executable(extra_env: dict[str, str] | None = None) -> Path | None:
    runtime_env = os.environ | (extra_env or {})
    java_home = str(runtime_env.get("JAVA_HOME") or "").strip()
    if java_home:
        candidate = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return candidate
    found = shutil.which("java", path=runtime_env.get("PATH"))
    return Path(found) if found else None


def _write_java_launcher(
    component: dict[str, Any],
    workspace: Path,
    extra_env: dict[str, str] | None = None,
) -> str:
    launcher = component.get("java_launcher")
    if not isinstance(launcher, dict):
        raise RuntimeError("组件安装清单缺少 java_launcher")
    launcher_dir = workspace / str(launcher.get("path") or "")
    jar_name = str(launcher.get("jar") or "").strip()
    java = _java_executable(extra_env)
    if java is None or not jar_name:
        raise RuntimeError("生成 Apktool 启动器需要完整 Java")
    jar = launcher_dir / jar_name
    launcher_path = launcher_dir / ("apktool.cmd" if os.name == "nt" else "apktool")
    if os.name == "nt":
        content = f'@echo off\r\n"{java}" -jar "{jar}" %*\r\n'
    else:
        content = f'#!/bin/sh\nexec "{java}" -jar "{jar}" "$@"\n'
    launcher_path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        launcher_path.chmod(launcher_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(launcher_path)


def _write_macos_rizin_launcher(
    workspace: Path,
    extract_to: str,
    executable: Path,
) -> Path:
    library_dir = executable.parent.parent / "lib"
    launcher = workspace / extract_to / "workswarm-rizin"
    launcher.write_text(
        "#!/bin/sh\n"
        f"DYLD_LIBRARY_PATH={shlex.quote(str(library_dir))}"
        '${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}\n'
        "export DYLD_LIBRARY_PATH\n"
        f'exec {shlex.quote(str(executable))} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(
        launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return launcher


def _build_ghidra_macos_natives(
    install_dir: Path,
    extra_env: dict[str, str],
) -> None:
    platform_dir = {
        "macos-x64": "mac_x86_64",
        "macos-arm64": "mac_arm_64",
    }.get(_platform_id())
    if platform_dir is None:
        return
    decompiler = (
        install_dir
        / "Ghidra"
        / "Features"
        / "Decompiler"
        / "build"
        / "os"
        / platform_dir
        / "decompile"
    )
    if decompiler.is_file() and os.access(decompiler, os.X_OK):
        return
    gradle_dir = install_dir / "support" / "gradle"
    gradlew = gradle_dir / "gradlew"
    wrapper = gradle_dir / "gradle-wrapper.properties"
    if not gradlew.is_file() or not wrapper.is_file():
        raise RuntimeError("Ghidra 发布包缺少用于构建 macOS 原生组件的 Gradle wrapper")
    text = wrapper.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^distributionUrl=.*gradle-([0-9.]+)-bin\.zip$",
        text,
    )
    if match is None:
        raise RuntimeError("无法从 Ghidra Gradle wrapper 读取 Gradle 版本")
    distribution_url = _GHIDRA_GRADLE_MIRROR.format(version=match.group(1))
    escaped_url = distribution_url.replace(":", "\\:")
    text = re.sub(
        r"(?m)^distributionUrl=.*$",
        f"distributionUrl={escaped_url}",
        text,
    )
    text = re.sub(r"(?m)^networkTimeout=.*$", "networkTimeout=60000", text)
    wrapper.write_text(text, encoding="utf-8")
    if os.name != "nt":
        gradlew.chmod(
            gradlew.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
    result = _run(
        [str(gradlew), "buildNatives"],
        cwd=gradle_dir,
        env=os.environ | extra_env,
        timeout=1800,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Ghidra 原生组件构建失败").strip()
        raise RuntimeError(detail[-4000:])
    if not decompiler.is_file() or not os.access(decompiler, os.X_OK):
        raise RuntimeError("Ghidra 原生组件构建结束，但未生成 macOS decompiler")


def _apply_component_files(
    component: dict[str, Any],
    artifact: dict[str, Any],
    artifact_path: Path,
    workspace: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    extract_to = str(component.get("extract_to") or "").strip()
    if extract_to:
        _extract_archive_safely(
            artifact_path,
            workspace / extract_to,
            str(artifact.get("archive_type") or "zip"),
        )
    result: dict[str, str] = {}
    executable_map = component.get("executables")
    env_key = str(component.get("env_key") or "").strip()
    if isinstance(executable_map, dict) and env_key:
        relative = str(executable_map.get(_platform_id()) or "").strip()
        if not relative:
            raise RuntimeError(f"组件没有适用于 {_platform_id()} 的可执行文件")
        executable = workspace / extract_to / relative
        if not executable.is_file():
            raise RuntimeError(f"组件安装后缺少可执行文件: {executable}")
        if os.name != "nt":
            executable.chmod(
                executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        result[env_key] = str(
            executable.parent if component.get("env_directory") is True else executable
        )
    elif env_key:
        executable_names = [
            str(name)
            for name in component.get("executable_names") or []
            if str(name).strip()
        ]
        if not executable_names:
            raise RuntimeError(f"组件 {component.get('id')} 缺少可执行文件映射")
        root = workspace / extract_to
        executable = _find_component_executable(root, executable_names)
        if executable is None:
            raise RuntimeError(
                f"组件安装后缺少可执行文件: {', '.join(sorted(executable_names))}"
            )
        if os.name != "nt":
            executable.chmod(
                executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        result[env_key] = str(
            executable.parent if component.get("env_directory") is True else executable
        )
    if component.get("java_launcher") is not None:
        launcher = _write_java_launcher(component, workspace, extra_env)
        launcher_env = str((component.get("java_launcher") or {}).get("env_key") or "").strip()
        if launcher_env:
            result[launcher_env] = launcher
    component_env = _component_env(component)
    if isinstance(component_env, dict):
        for key, value in component_env.items():
            result[str(key)] = str(value).replace("${install_root}", str(workspace))
    if (
        component.get("id") == "rizin"
        and _platform_id() in {"macos-x64", "macos-arm64"}
        and extract_to
    ):
        executable = str(result.get(env_key) or "").strip()
        if executable:
            result[env_key] = str(
                _write_macos_rizin_launcher(
                    workspace,
                    extract_to,
                    Path(executable),
                )
            )
    if component.get("id") == "ghidra":
        install_dir = str(result.get("GHIDRA_INSTALL_DIR") or "").strip()
        if install_dir:
            _build_ghidra_macos_natives(Path(install_dir), extra_env or {})
    if component.get("setup_bridge") == "rea-cutter":
        executable = str(result.get("REA_CUTTER_COMMAND") or "").strip()
        if executable:
            _install_cutter_bridge(Path(executable), workspace)
    return result


def _ghidra_version(install_dir: Path) -> str | None:
    properties = install_dir / "Ghidra" / "application.properties"
    try:
        text = properties.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^application\.version=([^\r\n]+)", text)
    return match.group(1).strip() if match else None


def _find_ghidra_install(extra_env: dict[str, str] | None = None) -> tuple[Path, str] | None:
    runtime_env = os.environ | (extra_env or {})
    candidates: list[Path] = []
    configured = str(runtime_env.get("GHIDRA_INSTALL_DIR") or "").strip()
    if configured:
        candidates.append(Path(configured))
    home = Path.home()
    roots = [
        Path("C:/tools/ghidra"),
        Path("D:/tools/ghidra"),
        Path("C:/apps/ghidra"),
        Path("D:/apps/ghidra"),
        Path("C:/software/ghidra"),
        Path("D:/software/ghidra"),
        Path("C:/Program Files/ghidra"),
        Path("D:/Program Files/ghidra"),
        Path("C:/"),
        Path("D:/"),
        home / "tools" / "ghidra",
        home / "Applications",
        home / "Downloads",
        Path("/Applications"),
        Path("/opt"),
        Path("/usr/local"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates.extend(sorted(root.glob("ghidra_*"), reverse=True))
        except OSError:
            continue
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        probes = (
            resolved / "support" / "analyzeHeadless",
            resolved / "support" / "analyzeHeadless.bat",
        )
        version = _ghidra_version(resolved)
        if version and any(path.is_file() for path in probes) and _version_tuple(version) >= (11,):
            return resolved, version
    return None


def _find_hopper_launcher(extra_env: dict[str, str] | None = None) -> Path | None:
    runtime_env = os.environ | (extra_env or {})
    configured = str(runtime_env.get("HOPPER_LAUNCHER_PATH") or "").strip()
    candidates: list[Path] = [Path(configured)] if configured else []
    home = Path.home()
    if sys.platform == "darwin":
        candidates.extend(
            (
                Path("/Applications/Hopper Disassembler.app/Contents/MacOS/Hopper"),
                home / "Applications" / "Hopper Disassembler.app" / "Contents" / "MacOS" / "Hopper",
            )
        )
    elif sys.platform.startswith("linux"):
        candidates.extend(
            (
                Path("/opt/hopper/bin/Hopper"),
                home / ".local" / "share" / "rea" / "hopper" / "bin" / "Hopper",
                Path("/opt/hopper-v4/bin/hopper"),
                Path("/usr/bin/hopper"),
            )
        )
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    return None


def _find_pcap_tools(
    extra_env: dict[str, str] | None = None,
) -> tuple[Path, Path] | None:
    runtime_env = os.environ | (extra_env or {})
    candidates = [
        Path("C:/Program Files/Wireshark"),
        Path("C:/Program Files (x86)/Wireshark"),
        Path("C:/tools/Wireshark"),
        Path("D:/tools/Wireshark"),
        Path("C:/apps/Wireshark"),
        Path("D:/apps/Wireshark"),
        Path("C:/software/Wireshark"),
        Path("D:/software/Wireshark"),
        Path("/Applications/Wireshark.app/Contents/MacOS"),
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    for directory in candidates:
        tshark = directory / ("tshark.exe" if os.name == "nt" else "tshark")
        capinfos = directory / ("capinfos.exe" if os.name == "nt" else "capinfos")
        if tshark.is_file() and capinfos.is_file():
            return tshark, capinfos
    tshark = shutil.which("tshark", path=runtime_env.get("PATH"))
    capinfos = shutil.which("capinfos", path=runtime_env.get("PATH"))
    if tshark and capinfos:
        return Path(tshark).resolve(), Path(capinfos).resolve()
    return None


def _find_pcap_partial(
    extra_env: dict[str, str] | None = None,
) -> tuple[Path | None, Path | None]:
    runtime_env = os.environ | (extra_env or {})
    tshark = str(runtime_env.get("REA_TSHARK_COMMAND") or "").strip()
    capinfos = str(runtime_env.get("REA_CAPINFOS_COMMAND") or "").strip()
    tshark_path = Path(tshark).expanduser() if tshark else None
    capinfos_path = Path(capinfos).expanduser() if capinfos else None
    if tshark_path is None or not tshark_path.is_file():
        found = shutil.which("tshark", path=runtime_env.get("PATH"))
        tshark_path = Path(found) if found else None
    if capinfos_path is None or not capinfos_path.is_file():
        found = shutil.which("capinfos", path=runtime_env.get("PATH"))
        capinfos_path = Path(found) if found else None
    return tshark_path, capinfos_path


def _find_browser_executable(extra_env: dict[str, str] | None = None) -> Path | None:
    runtime_env = os.environ | (extra_env or {})
    configured = str(runtime_env.get("REA_BROWSER_EXECUTABLE") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    if os.name == "nt":
        candidates = [
            Path(str(runtime_env.get("ProgramFiles") or "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(str(runtime_env.get("ProgramFiles(x86)") or "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(str(runtime_env.get("LOCALAPPDATA") or "")) / "Google/Chrome/Application/chrome.exe",
            Path(str(runtime_env.get("ProgramFiles(x86)") or "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(str(runtime_env.get("ProgramFiles") or "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name, path=runtime_env.get("PATH"))
            if found:
                candidates.append(Path(found))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _component_env_keys(component: dict[str, Any]) -> set[str]:
    keys = {
        str(value).strip()
        for value in component.get("env_keys") or []
        if str(value).strip()
    }
    env_key = str(component.get("env_key") or "").strip()
    if env_key:
        keys.add(env_key)
    env = _component_env(component)
    keys.update(str(key).strip() for key in env if str(key).strip())
    launcher = component.get("java_launcher")
    if isinstance(launcher, dict) and str(launcher.get("env_key") or "").strip():
        keys.add(str(launcher["env_key"]).strip())
    return keys


def _configured_component(
    component_id: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, dict[str, str], str | None]:
    component = _manifest_components().get(component_id)
    if component is None:
        return False, {}, None
    runtime_env = os.environ | (extra_env or {})
    values = {
        key: str(runtime_env.get(key) or "").strip()
        for key in _component_env_keys(component)
        if str(runtime_env.get(key) or "").strip()
    }
    if component_id == "java":
        configured_home = values.get("JAVA_HOME")
        if not configured_home:
            return False, values, None
        home = Path(configured_home).expanduser().resolve()
        if not _java_home_is_full_jdk(home, minimum_major=21):
            return False, values, configured_home
        return True, {"JAVA_HOME": str(home)}, str(home)
    if component_id == "ghidra":
        value = values.get("GHIDRA_INSTALL_DIR")
        if value:
            home = Path(value).expanduser()
            probes = (home / "support" / "analyzeHeadless", home / "support" / "analyzeHeadless.bat")
            if _ghidra_version(home) is not None and any(path.is_file() for path in probes):
                return True, values, value
        return False, values, value or None
    if component_id in {"pwntools", "mitmproxy"}:
        env_key = "REA_PWNTOOLS_PYTHON" if component_id == "pwntools" else "REA_MITMDUMP_COMMAND"
        value = next((values.get(key, "") for key in _component_env_keys(component) if values.get(key)), "")
        if value and Path(value).expanduser().is_file() and _python_component_compatible(
            component_id,
            Path(value).expanduser(),
        ):
            return True, {env_key: value}, value
        return False, values, value or None
    if component_id in {
        "jadx",
        "adb",
        "apktool",
        "hopper",
        "kuna",
        "wakaru",
        "rizin",
        "cutter",
        "chrome",
        "gdb",
        "objdump",
        "sigrok",
    }:
        env_key = next((key for key in _component_env_keys(component) if key in values), "")
        value = values.get(env_key, "")
        if value and (value.startswith(("http://", "https://")) or Path(value).expanduser().is_file()):
            if component_id == "cutter":
                ready, _reason = _cutter_bridge_readiness(Path(value).expanduser())
                if not ready:
                    return False, values, value
            return True, values, value
        return False, values, value or None
    if component_id == "wabt":
        value = values.get("REA_WABT_BIN_DIRECTORY")
        if value:
            root = Path(value).expanduser()
            executable = root / ("wasm-validate.exe" if os.name == "nt" else "wasm-validate")
            if root.is_dir() and executable.is_file():
                return True, values, value
        return False, values, value or None
    if component_id == "pcap":
        tshark = values.get("REA_TSHARK_COMMAND")
        capinfos = values.get("REA_CAPINFOS_COMMAND")
        if tshark and capinfos and Path(tshark).is_file() and Path(capinfos).is_file():
            return True, values, tshark
        return False, values, tshark or capinfos or None
    if component_id == "ida":
        value = values.get("REA_IDA_MCP_CONFIG")
        if value and Path(value).expanduser().is_file():
            return True, values, value
        return False, values, value or None
    if component_id == "jeb":
        value = values.get("REA_JEB_MCP_URL")
        if value and value.startswith(("http://", "https://")):
            return True, values, value
        return False, values, value or None
    if component_id == "firmware":
        return bool(values), values, next(iter(values.values()), None)
    return False, values, None


def _component_env(component: dict[str, Any]) -> dict[str, Any]:
    by_platform = component.get("env_by_platform")
    if isinstance(by_platform, dict):
        selected = by_platform.get(_platform_id())
        if isinstance(selected, dict):
            return selected
    env = component.get("env")
    return env if isinstance(env, dict) else {}


def _python_component_compatible(component_id: str, executable: Path) -> bool:
    component = _manifest_components().get(component_id, {})
    packages = component.get("python_packages")
    if component_id == "pwntools":
        if not isinstance(packages, dict) or not packages:
            return False
        result = _run(
            [
                str(executable),
                "-c",
                (
                    "import importlib.metadata as m, json; "
                    f"print(json.dumps({{name: m.version(name) for name in {tuple(packages)!r}}}))"
                ),
            ],
            timeout=20,
        )
        if result.returncode != 0:
            return False
        try:
            installed = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return False
        return all(
            str(installed.get(name) or "") == str(version)
            for name, version in packages.items()
        )
    if component_id == "mitmproxy":
        expected = str((packages or {}).get("mitmproxy") or "")
        result = _run([str(executable), "--version"], timeout=20)
        return result.returncode == 0 and expected in f"{result.stdout}\n{result.stderr}"
    return False


def _reuse_existing_component(
    component_id: str,
    extra_env: dict[str, str] | None = None,
    workspace: str | Path | None = None,
) -> tuple[dict[str, str], str] | None:
    workspace_path = _workspace_path(workspace)
    if component_id == "java":
        java_home = _java_full_jdk_home(extra_env, minimum_major=21)
        if java_home is None:
            component = _manifest_components().get(component_id, {})
            env = _component_env(component)
            for value in env.values():
                rendered = str(value).replace("${install_root}", str(workspace_path))
                candidate = Path(rendered).expanduser()
                if "${" not in rendered and _java_home_is_full_jdk(candidate, minimum_major=21):
                    java_home = candidate.resolve()
                    break
        if java_home is not None:
            return {"JAVA_HOME": str(java_home)}, java_home.name
    if component_id == "ghidra":
        found = _find_ghidra_install(extra_env)
        if found is None:
            managed_root = workspace_path / "rea" / "tools" / "ghidra"
            if managed_root.is_dir():
                for candidate in sorted(managed_root.glob("**/ghidra_*"), reverse=True):
                    found = _find_ghidra_install({"GHIDRA_INSTALL_DIR": str(candidate)})
                    if found is not None:
                        break
        if found is not None:
            path, version = found
            if workspace_path.resolve() in path.parents:
                component = _manifest_components().get(component_id, {})
                try:
                    artifact = _component_artifact(component)
                except RuntimeError:
                    artifact = {}
                cache = workspace_path / str(artifact.get("destination") or "")
                extract_to = str(component.get("extract_to") or "").strip()
                if cache.is_file() and extract_to:
                    _restore_zip_file_modes(
                        cache,
                        workspace_path / extract_to,
                    )
            return {"GHIDRA_INSTALL_DIR": str(path)}, version
    if component_id == "hopper":
        launcher = _find_hopper_launcher(extra_env)
        if launcher is not None:
            return {"HOPPER_LAUNCHER_PATH": str(launcher)}, "installed"
    if component_id == "pcap":
        tools = _find_pcap_tools(extra_env)
        if tools is not None:
            tshark, capinfos = tools
            return {
                "REA_TSHARK_COMMAND": str(tshark),
                "REA_CAPINFOS_COMMAND": str(capinfos),
            }, "installed"
        managed_root = workspace_path / "rea" / "tools" / "pcap"
        managed_tshark = sorted(
            (*managed_root.glob("**/tshark"), *managed_root.glob("**/tshark.exe")),
            reverse=True,
        )
        for tshark in managed_tshark:
            capinfos = tshark.with_name(
                "capinfos.exe" if tshark.suffix.casefold() == ".exe" else "capinfos"
            )
            if tshark.is_file() and capinfos.is_file():
                return {
                    "REA_TSHARK_COMMAND": str(tshark),
                    "REA_CAPINFOS_COMMAND": str(capinfos),
                }, "installed"
    if component_id == "jadx":
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get("REA_JADX_MCP_JAR") or "").strip()
        if not candidate:
            cached = Path.home() / ".rea" / "tools" / "jadx-headless-mcp-0.7.1-all.jar"
            if cached.is_file():
                candidate = str(cached)
        if not candidate:
            roots = (
                workspace_path / "rea" / "tools" / "jadx",
                Path.home() / ".rea" / "tools",
                Path("C:/tools/jadx"),
                Path("D:/tools/jadx"),
                Path("C:/apps/jadx"),
                Path("D:/apps/jadx"),
                Path("C:/software/jadx"),
                Path("D:/software/jadx"),
                Path.home() / "Downloads",
            )
            for root in roots:
                if not root.is_dir():
                    continue
                pattern = "jadx-headless-mcp*.jar" if root == Path.home() / "Downloads" else "**/jadx-headless-mcp*.jar"
                found = next(iter(sorted(root.glob(pattern), reverse=True)), None)
                if found is not None:
                    candidate = str(found)
                    break
        if candidate and Path(candidate).expanduser().is_file():
            return {"REA_JADX_MCP_JAR": str(Path(candidate).expanduser().resolve())}, "0.7.1"
    if component_id in {"adb", "apktool"}:
        env_key = "REA_ADB_PATH" if component_id == "adb" else "REA_APKTOOL_COMMAND"
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get(env_key) or "").strip()
        names = ("adb", "adb.exe") if component_id == "adb" else ("apktool", "apktool.bat", "apktool.cmd")
        if not candidate:
            candidate = next(
                (
                    found
                    for name in names
                    if (found := shutil.which(name, path=runtime_env.get("PATH")))
                ),
                "",
            )
        if not candidate and component_id == "adb":
            managed_root = workspace_path / "rea" / "tools" / "adb"
            if managed_root.is_dir():
                found = next(iter(sorted(managed_root.glob("**/adb.exe"), reverse=True)), None)
                if found is None:
                    found = next(iter(sorted(managed_root.glob("**/adb"), reverse=True)), None)
                if found is not None:
                    candidate = str(found)
        if not candidate and component_id == "adb":
            sdk_roots = [
                str(runtime_env.get("ANDROID_HOME") or "").strip(),
                str(runtime_env.get("ANDROID_SDK_ROOT") or "").strip(),
                str(runtime_env.get("LOCALAPPDATA") or "") + "/Android/Sdk",
            ]
            for sdk_root in sdk_roots:
                if not sdk_root:
                    continue
                for name in names:
                    found = Path(sdk_root) / "platform-tools" / name
                    if found.is_file():
                        candidate = str(found)
                        break
                if candidate:
                    break
        if not candidate and component_id == "apktool":
            for root in (
                workspace_path / "rea" / "tools" / "apktool",
                Path("C:/tools/apktool"),
                Path("D:/tools/apktool"),
                Path("C:/apps/apktool"),
                Path("D:/apps/apktool"),
                Path("C:/software/apktool"),
                Path("D:/software/apktool"),
            ):
                if not root.is_dir():
                    continue
                found_launcher = next(
                    iter(sorted(root.glob("**/apktool.cmd"), reverse=True)),
                    None,
                )
                if found_launcher is not None:
                    candidate = str(found_launcher)
                    break
        if candidate and Path(candidate).expanduser().is_file():
            resolved = Path(candidate).expanduser().resolve()
            if os.name != "nt" and workspace_path.resolve() in resolved.parents:
                if not os.access(resolved, os.X_OK):
                    resolved.chmod(
                        resolved.stat().st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                if not os.access(resolved, os.X_OK):
                    return None
            return {env_key: str(resolved)}, "installed"
    if component_id in {"pwntools", "mitmproxy"}:
        env_key = "REA_PWNTOOLS_PYTHON" if component_id == "pwntools" else "REA_MITMDUMP_COMMAND"
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get(env_key) or "").strip()
        candidates = [Path(candidate).expanduser()] if candidate else []
        if component_id == "pwntools":
            managed_python, _ = _python_venv_executables(
                workspace_path / "rea" / "tools" / "python" / "pwntools"
            )
            candidates.append(managed_python)
            candidates.extend(
                Path(found)
                for name in ("python3", "python", "python.exe")
                if (found := shutil.which(name, path=runtime_env.get("PATH")))
            )
        else:
            _, managed_mitmdump = _python_venv_executables(
                workspace_path / "rea" / "tools" / "python" / "mitmproxy"
            )
            candidates.append(managed_mitmdump)
            found = shutil.which("mitmdump", path=runtime_env.get("PATH"))
            if found:
                candidates.append(Path(found))
        for executable in candidates:
            if executable.is_file() and _python_component_compatible(
                component_id,
                executable,
            ):
                return {env_key: str(executable.resolve())}, "installed"
    if component_id == "wabt":
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get("REA_WABT_BIN_DIRECTORY") or "").strip()
        if candidate and (Path(candidate) / ("wasm-validate.exe" if os.name == "nt" else "wasm-validate")).is_file():
            return {"REA_WABT_BIN_DIRECTORY": str(Path(candidate).expanduser().resolve())}, "1.0.42"
        found = shutil.which("wasm-validate", path=runtime_env.get("PATH"))
        if found:
            return {"REA_WABT_BIN_DIRECTORY": str(Path(found).resolve().parent)}, "1.0.42"
        for root in (
            workspace_path / "rea" / "tools" / "wabt",
            Path.home() / ".rea" / "tools" / "wabt",
            Path("C:/tools/wabt"),
            Path("D:/tools/wabt"),
            Path("/opt/wabt"),
            Path("/usr/local/wabt"),
        ):
            executable = _find_component_executable(
                root,
                ("wasm-validate.exe", "wasm-validate"),
            )
            if executable is not None:
                return {"REA_WABT_BIN_DIRECTORY": str(executable.parent)}, "1.0.42"
    if component_id in {"kuna", "rizin", "cutter", "wakaru"}:
        env_key = {
            "kuna": "REA_KUNA_COMMAND",
            "rizin": "REA_RIZIN_COMMAND",
            "cutter": "REA_CUTTER_COMMAND",
            "wakaru": "REA_WAKARU_COMMAND",
        }[component_id]
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get(env_key) or "").strip()
        names = {
            "kuna": ("kuna", "kuna.exe"),
            "rizin": ("rizin", "rizin.exe"),
            "cutter": ("cutter", "Cutter", "Cutter.exe"),
            "wakaru": ("wakaru", "wakaru.exe"),
        }[component_id]
        if not candidate:
            candidate = next(
                (
                    found
                    for name in names
                    if (found := shutil.which(name, path=runtime_env.get("PATH")))
                ),
                "",
            )
        if candidate and Path(candidate).expanduser().is_file():
            resolved = Path(candidate).expanduser().resolve()
            if os.name != "nt" and workspace_path.resolve() in resolved.parents:
                if not os.access(resolved, os.X_OK):
                    resolved.chmod(
                        resolved.stat().st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                if not os.access(resolved, os.X_OK):
                    return None
            if (
                component_id == "rizin"
                and resolved.name == "rizin"
                and _platform_id() in {"macos-x64", "macos-arm64"}
            ):
                component = _manifest_components().get(component_id, {})
                resolved = _write_macos_rizin_launcher(
                    workspace_path,
                    str(component.get("extract_to") or ""),
                    resolved,
                )
            return {env_key: str(resolved)}, "installed"
        for root in (
            workspace_path / "rea" / "tools" / component_id,
            Path.home() / ".rea" / "tools" / component_id,
            Path("C:/tools") / component_id,
            Path("D:/tools") / component_id,
            Path("C:/Program Files") / component_id,
            Path("D:/Program Files") / component_id,
            Path("/opt") / component_id,
            Path("/usr/local") / component_id,
        ):
            executable = _find_component_executable(root, names)
            if executable is not None:
                if (
                    os.name != "nt"
                    and workspace_path.resolve() in executable.resolve().parents
                    and not os.access(executable, os.X_OK)
                ):
                    executable.chmod(
                        executable.stat().st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                if (
                    component_id == "rizin"
                    and _platform_id() in {"macos-x64", "macos-arm64"}
                ):
                    component = _manifest_components().get(component_id, {})
                    executable = _write_macos_rizin_launcher(
                        workspace_path,
                        str(component.get("extract_to") or ""),
                        executable,
                    )
                return {env_key: str(executable)}, "installed"
    if component_id == "chrome":
        browser = _find_browser_executable(extra_env)
        if browser is not None:
            return {"REA_BROWSER_EXECUTABLE": str(browser)}, "installed"
    if component_id in {"gdb", "objdump", "sigrok"}:
        env_key = {
            "gdb": "REA_GDB_COMMAND",
            "objdump": "REA_OBJDUMP_COMMAND",
            "sigrok": "REA_SIGROK_COMMAND",
        }[component_id]
        runtime_env = os.environ | (extra_env or {})
        candidate = str(runtime_env.get(env_key) or "").strip()
        names = {
            "gdb": ("gdb", "gdb.exe"),
            "objdump": ("objdump", "objdump.exe"),
            "sigrok": ("sigrok-cli", "sigrok-cli.exe"),
        }[component_id]
        if not candidate:
            candidate = next(
                (
                    found
                    for name in names
                    if (found := shutil.which(name, path=runtime_env.get("PATH")))
                ),
                "",
            )
        if candidate and Path(candidate).expanduser().is_file():
            return {env_key: str(Path(candidate).expanduser().resolve())}, "installed"
    return None


def _component_cache_path(
    component: dict[str, Any],
    workspace: Path,
) -> Path | None:
    try:
        artifact = _component_artifact(component)
    except RuntimeError:
        return None
    destination = str(artifact.get("destination") or "").strip()
    if not destination:
        return None
    candidate = workspace / destination
    return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None


def _manifest_expected_paths(
    component: dict[str, Any],
    workspace: Path,
) -> list[Path]:
    paths: list[Path] = []
    env = _component_env(component)
    for value in env.values():
        rendered = str(value).replace("${install_root}", str(workspace))
        if rendered and "${" not in rendered:
            paths.append(Path(rendered))
    launcher = component.get("java_launcher")
    if isinstance(launcher, dict):
        launcher_dir = workspace / str(launcher.get("path") or "")
        jar_name = str(launcher.get("jar") or "").strip()
        if jar_name:
            paths.append(launcher_dir / jar_name)
        paths.append(launcher_dir / ("apktool.cmd" if os.name == "nt" else "apktool"))
    extract_to = str(component.get("extract_to") or "").strip()
    if extract_to:
        paths.append(workspace / extract_to)
    return paths


def _partial_component_path(
    component_id: str,
    extra_env: dict[str, str] | None,
) -> tuple[str, str] | None:
    runtime_env = os.environ | (extra_env or {})
    if component_id == "pcap":
        tshark, capinfos = _find_pcap_partial(extra_env)
        if tshark is not None or capinfos is not None:
            missing = "capinfos" if capinfos is None else "tshark"
            found = str(tshark or capinfos)
            return found, f"已检测到部分 PCAP 工具，但缺少 {missing}"
    if component_id == "apktool":
        roots = (
            Path("C:/tools/apktool"),
            Path("D:/tools/apktool"),
            Path("C:/apps/apktool"),
            Path("D:/apps/apktool"),
            Path("C:/software/apktool"),
            Path("D:/software/apktool"),
            Path.home() / ".apktool",
            Path.home() / "Downloads",
        )
        for root in roots:
            if not root.is_dir():
                continue
            pattern = "apktool*.jar" if root == Path.home() / "Downloads" else "**/apktool*.jar"
            jar = next(iter(sorted(root.glob(pattern), reverse=True)), None)
            if jar is not None:
                return str(jar), "已检测到 Apktool JAR，但还没有可用的启动器或 REA 配置"
    if component_id == "jadx":
        found = shutil.which("jadx", path=runtime_env.get("PATH"))
        if found:
            return found, "已检测到 JADX 客户端，但未找到 REA Headless MCP Bridge"
    if component_id == "ida":
        roots = (
            Path("C:/Program Files"),
            Path("C:/Program Files (x86)"),
            Path("D:/tools"),
            Path("D:/apps"),
            Path("D:/software"),
            Path("C:/apps"),
            Path("C:/software"),
            Path.home() / "Applications",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for install_dir in (
                *sorted(root.glob("IDA*")),
                *sorted(root.glob("Hex-Rays*")),
            ):
                candidate = next(iter(sorted(install_dir.glob("**/ida*.exe"))), None)
                if candidate is not None:
                    return str(candidate), "已检测到 IDA 本体，但尚未配置上游 IDA MCP"
    if component_id == "jeb":
        roots = (
            Path("C:/Program Files"),
            Path("C:/Program Files (x86)"),
            Path("D:/tools"),
            Path("D:/apps"),
            Path("D:/software"),
            Path("C:/apps"),
            Path("C:/software"),
            Path.home() / "Applications",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for install_dir in (
                *sorted(root.glob("JEB*")),
                *sorted(root.glob("jeb*")),
            ):
                candidate = next(iter(sorted(install_dir.glob("**/jeb*.exe"))), None)
                if candidate is not None:
                    return str(candidate), "已检测到 JEB 客户端，但尚未配置 JEB MCP 地址"
    if component_id == "cutter":
        found = shutil.which("cutter", path=runtime_env.get("PATH"))
        if found:
            ready, reason = _cutter_bridge_readiness(Path(found))
            if not ready:
                return found, reason
    return None


def _component_inventory(
    component_id: str,
    extra_env: dict[str, str] | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    component = _manifest_components().get(component_id)
    if component is None:
        return {
            "state": "missing",
            "detected": False,
            "configured": False,
            "path": None,
            "paths": [],
            "reason": "组件不在安装清单中",
        }
    if not _platform_supported(component):
        return {
            "state": "unsupported",
            "detected": False,
            "configured": False,
            "path": None,
            "paths": [],
            "reason": "当前平台不支持此能力",
        }
    workspace_path = _workspace_path(workspace)
    configured, configured_values, configured_path = _configured_component(
        component_id,
        extra_env,
    )
    if configured:
        return {
            "state": "configured",
            "detected": True,
            "configured": True,
            "path": configured_path,
            "paths": list(configured_values.values()),
            "reason": "已检测到本机能力，且已写入 REA 配置",
            "source": "env",
        }
    reused = _reuse_existing_component(component_id, extra_env, workspace_path)
    if reused is not None:
        reused_env, version = reused
        if component_id == "cutter":
            executable = str(reused_env.get("REA_CUTTER_COMMAND") or "").strip()
            if executable:
                ready, reason = _cutter_bridge_readiness(Path(executable))
                if not ready:
                    return {
                        "state": "partial",
                        "detected": True,
                        "configured": False,
                        "path": executable,
                        "paths": [executable],
                        "env": reused_env,
                        "version": version,
                        "reason": reason,
                        "source": "local",
                    }
        return {
            "state": "discovered",
            "detected": True,
            "configured": False,
            "path": next(iter(reused_env.values()), None),
            "paths": list(reused_env.values()),
            "env": reused_env,
            "version": version,
            "reason": "已检测到本机可用版本，但尚未写入 REA 配置",
            "source": "local",
        }
    expected_paths = _manifest_expected_paths(component, workspace_path)
    existing_expected = [path for path in expected_paths if path.exists()]
    if existing_expected:
        return {
            "state": "partial",
            "detected": False,
            "configured": False,
            "path": str(existing_expected[0]),
            "paths": [str(path) for path in existing_expected],
            "reason": "已找到部分安装文件，但能力所需的文件不完整或尚未配置",
            "source": "managed",
        }
    partial = _partial_component_path(component_id, extra_env)
    if partial is not None:
        path, reason = partial
        return {
            "state": "partial",
            "detected": False,
            "configured": False,
            "path": path,
            "paths": [path],
            "reason": reason,
            "source": "local",
        }
    cache_path = _component_cache_path(component, workspace_path)
    if cache_path is not None:
        return {
            "state": "cached",
            "detected": False,
            "configured": False,
            "path": str(cache_path),
            "paths": [str(cache_path)],
            "reason": "安装包已下载到本地，但尚未安装和配置",
            "source": "cache",
        }
    return {
        "state": "missing",
        "detected": False,
        "configured": False,
        "path": None,
        "paths": [],
        "reason": "本机未找到安装文件或可用程序",
    }


def _with_component_inventory(
    item: dict[str, Any],
    extra_env: dict[str, str] | None,
    workspace: Path,
) -> dict[str, Any]:
    component_id = str(item.get("id") or "")
    enriched = dict(item)
    inventory = _component_inventory(component_id, extra_env, workspace)
    enriched["inventory"] = inventory
    enriched["configured"] = bool(inventory.get("configured"))
    enriched["local_state"] = str(inventory.get("state") or "missing")
    enriched["local_path"] = inventory.get("path")
    enriched["local_paths"] = inventory.get("paths") or []
    if inventory.get("detected"):
        enriched["detected"] = True
    if inventory.get("state") in {"discovered", "partial", "cached"}:
        enriched["detected"] = bool(inventory.get("state") == "discovered")
        if not enriched.get("reason"):
            enriched["reason"] = inventory.get("reason")
        if enriched.get("available") is None:
            enriched["available"] = False
        if inventory.get("state") == "discovered":
            enriched["status"] = "discovered"
            enriched["code"] = "not_configured"
        elif inventory.get("state") == "partial":
            enriched["status"] = "partial"
            enriched["code"] = "partial"
        else:
            enriched["status"] = "downloaded_not_installed"
            enriched["code"] = "downloaded_not_installed"
    elif inventory.get("state") == "configured":
        if enriched.get("available") is None:
            enriched["available"] = True
        if not enriched.get("status") or enriched.get("status") == "not_configured":
            enriched["status"] = "configured"
        enriched["code"] = None
    return enriched


def _install_macos_hopper(workspace: Path) -> dict[str, str]:
    request = urllib.request.Request(
        _HOPPER_RELEASES_URL,
        headers={"User-Agent": _HOPPER_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        metadata = response.read(1024 * 1024 + 1)
    if len(metadata) > 1024 * 1024:
        raise RuntimeError("Hopper 发布元数据超过允许大小")
    releases = json.loads(metadata.decode("utf-8"))
    release = releases.get("OS X") if isinstance(releases, dict) else None
    if not isinstance(release, dict):
        raise RuntimeError("Hopper 官方发布元数据缺少 macOS 版本")
    url = str(release.get("filename") or "").strip()
    version = str(release.get("version") or "").strip()
    expected_size = int(release.get("file_length") or 0)
    expected_sha1 = str(release.get("file_hash") or "").strip().lower()
    if (
        not version
        or not url.startswith(_HOPPER_DOWNLOAD_PREFIX)
        or expected_size <= 0
        or expected_size > 100 * 1024 * 1024
        or not re.fullmatch(r"[0-9a-f]{40}", expected_sha1)
    ):
        raise RuntimeError("Hopper 官方发布元数据不完整或超出允许范围")
    artifact = {
        "url": url,
        "sha1": expected_sha1,
        "size_bytes": expected_size,
        "destination": f"rea/cache/Hopper-{version}-demo.dmg",
        "max_bytes": 100 * 1024 * 1024,
        "primary_timeout_seconds": 1800,
        "fallback_timeout_seconds": 1800,
        "user_agent": _HOPPER_USER_AGENT,
        "resume": False,
        "http1_1": True,
    }
    package = _download_component_artifact(
        {"artifact": artifact},
        workspace,
        artifact,
    )
    temporary = Path(tempfile.mkdtemp(prefix="workswarm-hopper-"))
    mount = temporary / "mounted"
    mount.mkdir()
    attached = False
    stage_root: Path | None = None
    backup: Path | None = None
    destination = Path.home() / "Applications" / "Hopper Disassembler.app"
    try:
        mounted = _run(
            [
                "hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-mountpoint",
                str(mount),
                str(package),
            ],
            timeout=180,
        )
        if mounted.returncode != 0:
            raise RuntimeError(
                (mounted.stderr or mounted.stdout or "Hopper DMG 挂载失败").strip()
            )
        attached = True
        bundles = [
            item
            for item in mount.iterdir()
            if item.suffix.casefold() == ".app"
            and re.match(r"^Hopper.*\.app$", item.name, re.IGNORECASE)
            and not item.is_symlink()
        ]
        if len(bundles) != 1:
            raise RuntimeError("Hopper DMG 中未找到唯一的官方 app bundle")
        source = bundles[0]
        verification = _run(
            ["codesign", "--verify", "--deep", "--strict", str(source)],
            timeout=120,
        )
        if verification.returncode != 0:
            raise RuntimeError(
                (verification.stderr or verification.stdout or "Hopper 代码签名无效").strip()
            )
        publisher = _run(
            [
                "codesign",
                "--verify",
                "--strict",
                f"-R{_HOPPER_PUBLISHER_REQUIREMENT}",
                str(source),
            ],
            timeout=120,
        )
        if publisher.returncode != 0:
            raise RuntimeError(
                (publisher.stderr or publisher.stdout or "Hopper 发布者签名不匹配").strip()
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage_root = Path(
            tempfile.mkdtemp(
                prefix=".rea-hopper-stage-",
                dir=destination.parent,
            )
        )
        staged = stage_root / "new.app"
        copied = _run(["ditto", str(source), str(staged)], timeout=300)
        if copied.returncode != 0:
            raise RuntimeError(
                (copied.stderr or copied.stdout or "Hopper app bundle 复制失败").strip()
            )
        launcher = staged / "Contents" / "MacOS" / "hopper"
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            raise RuntimeError("Hopper app bundle 缺少可执行 launcher")
        if destination.exists():
            backup = stage_root / "previous.app"
            destination.replace(backup)
        staged.replace(destination)
        backup = None
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    finally:
        if attached:
            _run(["hdiutil", "detach", str(mount), "-force"], timeout=60)
        shutil.rmtree(temporary, ignore_errors=True)
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    launcher = destination / "Contents" / "MacOS" / "hopper"
    _run(["open", str(destination)], timeout=30)
    return {"HOPPER_LAUNCHER_PATH": str(launcher)}


def _install_rea_hopper(
    extra_env: dict[str, str],
    workspace: Path,
    progress_callback: ProgressCallback | None,
    *,
    component_start: float,
    component_end: float,
    label: str,
) -> dict[str, str]:
    _report_progress(
        progress_callback,
        "component_hopper_connect",
        component_start,
        f"{label}：检查 REA 官方安装计划",
        {"component_id": "hopper", "component_label": label, "phase": "connecting"},
    )
    _report_progress(
        progress_callback,
        "component_hopper_download",
        component_start + (component_end - component_start) * 0.15,
        f"{label}：下载并校验 Hopper 官方 Demo",
        {"component_id": "hopper", "component_label": label, "phase": "downloading"},
    )
    if _platform_id() in {"macos-x64", "macos-arm64"}:
        return _install_macos_hopper(workspace)
    preferred_node = shutil.which("node", path=extra_env.get("PATH"))
    runtime = resolve_node_runtime(preferred_node)
    if runtime is None:
        raise RuntimeError("安装 Hopper 需要兼容的 Node.js")
    result = _npx_command(
        runtime,
        [
            "--no-install",
            f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}",
            "setup",
            "--yes",
            "--install-hopper",
            "--format",
            "json",
            "--full-output",
        ],
        timeout=1800,
        extra_env=extra_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "REA Hopper 安装失败").strip()
        raise RuntimeError(detail[-4000:])
    launcher = _find_hopper_launcher(extra_env)
    if launcher is None:
        raise RuntimeError("REA 安装流程结束，但仍未找到 HOPPER_LAUNCHER_PATH")
    return {"HOPPER_LAUNCHER_PATH": str(launcher)}


def _install_package_manager_component(
    component: dict[str, Any],
    extra_env: dict[str, str],
    progress_callback: ProgressCallback | None,
    *,
    component_start: float,
    component_end: float,
    label: str,
) -> dict[str, str]:
    manager = _package_manager_config(component)
    if not manager:
        raise RuntimeError(f"{label} 当前平台没有可用的自动包管理器配置")
    command = manager.get("command")
    if not isinstance(command, list) or not command:
        raise RuntimeError(f"{label} 的包管理器命令无效")
    argv = [str(item) for item in command]
    executable = shutil.which(argv[0], path=(os.environ | extra_env).get("PATH"))
    if executable is None:
        raise RuntimeError(f"找不到包管理器: {argv[0]}")
    argv[0] = executable
    _report_progress(
        progress_callback,
        "component_pcap_connect",
        component_start,
        f"{label}：准备调用 {manager.get('display_name') or argv[0]}",
        {"component_id": "pcap", "component_label": label, "phase": "connecting"},
    )
    _report_progress(
        progress_callback,
        "component_pcap_install",
        component_start + (component_end - component_start) * 0.1,
        f"{label}：通过 {manager.get('display_name') or argv[0]} 安装",
        {"component_id": "pcap", "component_label": label, "phase": "installing"},
    )
    result = _run(
        argv,
        env=os.environ | extra_env,
        timeout=float(manager.get("timeout_seconds") or 900),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "系统包管理器安装失败").strip()
        raise RuntimeError(detail[-4000:])
    tools = _find_pcap_tools(extra_env)
    if tools is None:
        raise RuntimeError("安装命令成功结束，但仍未找到 tshark/capinfos")
    tshark, capinfos = tools
    return {
        "REA_TSHARK_COMMAND": str(tshark),
        "REA_CAPINFOS_COMMAND": str(capinfos),
    }


def _install_msi_admin_component(
    component: dict[str, Any],
    artifact_path: Path,
    workspace: Path,
    progress_callback: ProgressCallback | None,
    *,
    component_id: str,
    component_start: float,
    component_end: float,
    label: str,
) -> dict[str, str]:
    installer = _component_installer(component)
    if not isinstance(installer, dict) or installer.get("type") != "msi_admin":
        raise RuntimeError(f"{label} 缺少 MSI 管理安装配置")
    target = workspace / str(installer.get("target") or f"rea/tools/{component_id}")
    target.mkdir(parents=True, exist_ok=True)
    _report_progress(
        progress_callback,
        f"component_{component_id}_install",
        component_start + (component_end - component_start) * 0.1,
        f"{label}：正在展开 MSI 到 Workswarm 管理目录",
        {"component_id": component_id, "component_label": label, "phase": "installing"},
    )
    result = _run(
        [
            "msiexec.exe",
            "/a",
            str(artifact_path),
            "/qn",
            f"TARGETDIR={target}",
        ],
        timeout=float(installer.get("timeout_seconds") or 600),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "MSI 管理安装失败").strip()
        raise RuntimeError(detail[-4000:])
    tshark = next(target.rglob("tshark.exe"), None)
    capinfos = next(target.rglob("capinfos.exe"), None)
    if tshark is None or capinfos is None:
        raise RuntimeError("MSI 已展开，但没有找到 tshark/capinfos")
    return {
        "REA_TSHARK_COMMAND": str(tshark),
        "REA_CAPINFOS_COMMAND": str(capinfos),
    }


def install_managed_enhancements(
    workspace: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    selected_ids: list[str] | None = None,
    seed_env: dict[str, str] | None = None,
    progress_start: float = 18.0,
    progress_end: float = 90.0,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Install manifest-declared managed components and return their env."""

    workspace_path = _workspace_path(workspace)
    record_env = _record_env(rea_mcp_record(workspace_path)) | (seed_env or {})
    env: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    selected = set(selected_ids) if selected_ids is not None else None
    managed_components = [
        (component_id, component)
        for component_id, component in _manifest_components().items()
        if component.get("install_mode")
        in {"managed", "rea_setup", "package_manager", "python_venv", "python_tool"}
        and (
            component.get("install_mode") != "package_manager"
            or _package_manager_config(component) is not None
        )
        and (selected is not None and component_id in selected or selected is None and bool(component.get("default_selected")))
    ]
    component_span = max(0.0, progress_end - progress_start) / max(1, len(managed_components))
    for index, (component_id, component) in enumerate(managed_components):
        _raise_if_cancelled(cancel_check)
        if component.get("install_mode") not in {
            "managed",
            "rea_setup",
            "package_manager",
            "python_venv",
            "python_tool",
        } or not bool(
            component.get("default_selected") or selected is not None and component_id in selected
        ):
            continue
        effective_env = record_env | env
        requirements = component.get("requires")
        requirement_ok = (
            not isinstance(requirements, list)
            or all(
                _requirement_satisfied(str(name), effective_env)
                for name in requirements
            )
        )
        platform_ok = _platform_supported(component)
        auto_platform_ok = _auto_install_supported(component)
        reused = (
            _reuse_existing_component(component_id, effective_env, workspace_path)
            if platform_ok and auto_platform_ok and requirement_ok
            else None
        )
        if reused is not None:
            reused_env, version = reused
            bridge_info: dict[str, Any] | None = None
            if component.get("setup_bridge") == "rea-cutter":
                executable = str(reused_env.get("REA_CUTTER_COMMAND") or "").strip()
                try:
                    if not executable:
                        raise RuntimeError("复用 Cutter 时没有返回 REA_CUTTER_COMMAND")
                    bridge_info = _install_cutter_bridge(
                        Path(executable),
                        workspace_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        {
                            "id": component_id,
                            "ok": False,
                            "state": "failed",
                            "path": executable or None,
                            "version": version,
                            "reused": True,
                            "source": "local-reuse",
                            "mirror_fallback": False,
                            "error": str(exc),
                        }
                    )
                    _report_progress(
                        progress_callback,
                        f"component_{component_id}_failed",
                        progress_start + (index + 1) * component_span,
                        f"{component.get('display_name') or component_id}：桥接插件安装失败，{exc}",
                        {
                            "component_id": component_id,
                            "component_label": component.get("display_name") or component_id,
                            "phase": "failed",
                            "error": str(exc),
                        },
                    )
                    continue
            env.update(reused_env)
            results.append(
                {
                    "id": component_id,
                    "ok": True,
                    "state": "reused",
                    "path": next(iter(reused_env.values()), None),
                    "version": version,
                    "reused": True,
                    "source": "local-reuse",
                    "mirror_fallback": False,
                    "error": None,
                    "bridge": bridge_info,
                }
            )
            _report_progress(
                progress_callback,
                f"component_{component_id}_complete",
                progress_start + (index + 1) * component_span,
                f"{component.get('display_name') or component_id}：检测到兼容本地版本 {version}，已复用",
                {
                    "component_id": component_id,
                    "component_label": component.get("display_name") or component_id,
                    "phase": "complete",
                    "reused": True,
                    "version": version,
                },
            )
            continue
        if not platform_ok or not auto_platform_ok or not requirement_ok:
            state = "unsupported" if not platform_ok or not auto_platform_ok else "needs_action"
            reason = (
                "当前平台不支持此能力"
                if not platform_ok
                else "当前平台尚未提供自动安装适配器"
                if not auto_platform_ok
                else "缺少安装前置条件"
            )
            results.append(
                {
                    "id": component_id,
                    "ok": state == "unsupported",
                    "state": state,
                    "path": None,
                    "error": reason,
                    "reason": reason,
                }
            )
            _report_progress(
                progress_callback,
                f"component_{component_id}_skipped",
                progress_start + index * component_span,
                f"{component.get('display_name') or component_id}：{reason}",
                {
                    "component_id": component_id,
                    "phase": "skipped",
                    "reason": reason,
                },
            )
            continue
        label = str(component.get("display_name") or component_id)
        component_start = progress_start + index * component_span
        component_end = component_start + component_span
        prefix = f"component_{component_id}"
        download_source: str | None = None
        download_fallback = False
        _report_progress(
            progress_callback,
            f"{prefix}_planning",
            component_start,
            f"{label}：读取安装清单并检查本地缓存",
            {"component_id": component_id, "component_label": label, "phase": "planning"},
        )

        def report_download(fraction: float, details: dict[str, Any]) -> None:
            nonlocal download_source, download_fallback
            phase = str(details.get("phase") or "downloading")
            if details.get("source"):
                download_source = str(details["source"])
                download_fallback = bool(details.get("mirror_fallback"))
            stage = {
                "connecting": f"{prefix}_connect",
                "downloading": f"{prefix}_download",
                "verifying": f"{prefix}_verify",
                "cached": f"{prefix}_cached",
            }.get(phase, f"{prefix}_download")
            downloaded = int(details.get("downloaded_bytes") or 0)
            total = int(details.get("total_bytes") or 0)
            rate = int(details.get("bytes_per_second") or 0)
            eta = details.get("eta_seconds")
            if phase == "connecting":
                message = f"{label}：正在连接下载源"
            elif phase == "cached":
                message = f"{label}：本地缓存已通过哈希校验，跳过下载"
            elif phase == "verifying":
                message = f"{label}：下载完成，正在校验文件哈希"
            else:
                size_text = (
                    f"{_human_bytes(downloaded)} / {_human_bytes(total)}"
                    if total
                    else _human_bytes(downloaded)
                )
                eta_text = f"，预计剩余 {eta} 秒" if isinstance(eta, int) else ""
                message = f"{label}：已下载 {size_text}，{_human_bytes(rate)}/s{eta_text}"
            _report_progress(
                progress_callback,
                stage,
                component_start + max(0.0, min(1.0, fraction)) * component_span,
                message,
                {
                    "component_id": component_id,
                    "component_label": label,
                    **details,
                },
            )

        try:
            _raise_if_cancelled(cancel_check)
            artifact: dict[str, Any] | None = None
            artifact_path: Path | None = None
            if component.get("install_mode") == "rea_setup":
                env.update(
                    _install_rea_hopper(
                        effective_env,
                        workspace_path,
                        progress_callback,
                        component_start=component_start,
                        component_end=component_end,
                        label=label,
                    )
                )
            elif component.get("install_mode") == "package_manager":
                env.update(
                    _install_package_manager_component(
                        component,
                        effective_env,
                        progress_callback,
                        component_start=component_start,
                        component_end=component_end,
                        label=label,
                    )
                )
            elif component.get("install_mode") in {"python_venv", "python_tool"}:
                env.update(
                    _install_python_component(
                        component_id,
                        component,
                        workspace_path,
                        progress_callback,
                        component_start=component_start,
                        component_end=component_end,
                        cancel_check=cancel_check,
                    )
                )
            else:
                artifact = _component_artifact(component)
                artifact_path = _download_component_artifact(
                    component,
                    workspace_path,
                    artifact,
                    report_download,
                )
                component_installer = _component_installer(component)
                if isinstance(component_installer, dict):
                    installer_type = str(component_installer.get("type") or "")
                    if installer_type != "msi_admin":
                        raise RuntimeError(f"{label} 的安装器类型不受支持: {installer_type}")
                    env.update(
                        _install_msi_admin_component(
                            component,
                            artifact_path,
                            workspace_path,
                            progress_callback,
                            component_id=component_id,
                            component_start=component_start,
                            component_end=component_end,
                            label=label,
                        )
                    )
            _report_progress(
                progress_callback,
                f"{prefix}_install",
                component_end - component_span * 0.1,
                f"{label}：正在安装文件并生成 Provider 配置",
                {"component_id": component_id, "component_label": label, "phase": "installing"},
            )
            if (
                component.get("install_mode") == "managed"
                and _component_installer(component) is None
            ):
                if artifact is None or artifact_path is None:
                    raise RuntimeError("组件安装缺少下载结果")
                env.update(
                    _apply_component_files(
                        component,
                        artifact,
                        artifact_path,
                        workspace_path,
                        effective_env,
                    )
                )
            install_mode = str(component.get("install_mode") or "")
            source = download_source
            if source is None:
                source = {
                    "rea_setup": "rea-setup",
                    "package_manager": "system-package-manager",
                    "python_venv": "python-package-index",
                    "python_tool": "python-package-index",
                }.get(install_mode)
            results.append(
                {
                    "id": component_id,
                    "ok": True,
                    "state": "installed",
                    "path": str(artifact_path) if artifact_path is not None else None,
                    "version": str(artifact.get("version") or "") or None
                    if artifact is not None
                    else None,
                    "source": source,
                    "mirror_fallback": download_fallback,
                    "error": None,
                }
            )
            _report_progress(
                progress_callback,
                f"{prefix}_complete",
                component_end,
                f"{label}：安装完成",
                {"component_id": component_id, "component_label": label, "phase": "complete"},
            )
            _raise_if_cancelled(cancel_check)
        except InstallCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "id": component_id,
                    "ok": False,
                    "state": "failed",
                    "path": None,
                    "error": str(exc),
                }
            )
            _report_progress(
                progress_callback,
                f"{prefix}_failed",
                component_end,
                f"{label}：安装失败，{exc}",
                {
                    "component_id": component_id,
                    "component_label": label,
                    "phase": "failed",
                    "error": str(exc),
                },
            )
    return {"env": env, "results": results}


def _rea_cli_json(
    runtime: NodeRuntime,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run one REA CLI command and return its JSON envelope."""

    result = _npx_command(
        runtime,
        [
            "--no-install",
            f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}",
            *args,
            "--format",
            "json",
            "--full-output",
        ],
        timeout=timeout,
        extra_env=extra_env,
    )
    output = result.stdout.strip()
    if not output:
        detail = (result.stderr or "REA returned no JSON output").strip()
        raise RuntimeError(detail[-2000:])
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"REA 返回了非 JSON 输出: {output[-1000:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("REA 返回的 JSON 不是对象")
    return payload


def _verify_component(
    component_id: str,
    runtime: NodeRuntime | None,
    extra_env: dict[str, str],
) -> dict[str, Any]:
    """Verify one installed/reused component with REA-native probes."""

    if component_id == "java":
        home = _java_full_jdk_home(extra_env, minimum_major=21)
        if home is None:
            return {
                "ok": False,
                "code": "jdk_probe_failed",
                "detail": "未找到完整 JDK 21，或 java --list-modules 缺少 jdk.compiler",
            }
        java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
        version = _run([str(java), "-version"], timeout=10)
        version_text = f"{version.stdout}\n{version.stderr}".strip()
        match = re.search(r'version "([^"]+)"', version_text)
        return {
            "ok": version.returncode == 0,
            "code": None if version.returncode == 0 else "java_launch_failed",
            "detail": (
                f"完整 JDK {match.group(1) if match else '21+'} 可用: {home}"
                if version.returncode == 0
                else version_text[-1000:]
            ),
            "version": match.group(1) if match else None,
        }

    if runtime is None:
        return {
            "ok": False,
            "code": "rea_runtime_missing",
            "detail": "增强能力验证需要已安装的基础 REA Runtime",
        }

    if component_id == "hopper":
        try:
            payload = _rea_cli_json(
                runtime,
                ["doctor", "--provider", component_id],
                extra_env=extra_env,
                timeout=120,
            )
            data = (
                payload.get("data")
                if isinstance(payload.get("data"), dict)
                else payload.get("error")
                if isinstance(payload.get("error"), dict)
                else {}
            )
            scope_checks = data.get("scope_checks")
            check = next(
                (
                    item
                    for item in scope_checks or []
                    if isinstance(item, dict) and item.get("name") == component_id
                ),
                None,
            )
            available = bool(
                payload.get("ok")
                and isinstance(check, dict)
                and check.get("ok")
                and data.get("hopperPath")
            )
            return {
                "ok": available,
                "code": None if available else "hopper_probe_failed",
                "detail": (
                    f"REA Hopper Provider 验证通过: {data.get('hopperPath')}"
                    if available
                    else str(
                        (check or {}).get("detail")
                        or "REA doctor 未确认 Hopper Provider 可用"
                    )
                ),
                "checks": scope_checks or [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "code": "provider_probe_failed",
                "detail": str(exc),
            }

    if component_id in {"ghidra", "jadx", "kuna"}:
        try:
            payload = _rea_cli_json(
                runtime,
                ["doctor", "--provider", component_id],
                extra_env=extra_env,
                timeout=120,
            )
            data = (
                payload.get("data")
                if isinstance(payload.get("data"), dict)
                else payload.get("error")
                if isinstance(payload.get("error"), dict)
                else {}
            )
            inspections = data.get("providerInspections")
            inspection = next(
                (
                    item
                    for item in inspections or []
                    if isinstance(item, dict) and item.get("id") == component_id
                ),
                None,
            )
            if not isinstance(inspection, dict):
                return {
                    "ok": False,
                    "code": "provider_probe_missing",
                    "detail": "REA doctor 未返回该 Provider 的检查结果",
                }
            checks = inspection.get("checks")
            failed = next(
                (
                    item
                    for item in checks or []
                    if isinstance(item, dict) and not bool(item.get("ok"))
                ),
                None,
            )
            available = bool(inspection.get("available"))
            return {
                "ok": available,
                "code": None
                if available
                else str((failed or {}).get("code") or "provider_unavailable"),
                "detail": (
                    f"REA Provider 验证通过: {component_id}"
                    if available
                    else str(
                        (failed or {}).get("detail")
                        or (failed or {}).get("remediation")
                        or "REA Provider 不可用"
                    )
                ),
                "version": inspection.get("providerVersion"),
                "checks": checks or [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "code": "provider_probe_failed",
                "detail": str(exc),
            }

    if component_id == "pwntools":
        python = str(extra_env.get("REA_PWNTOOLS_PYTHON") or "").strip()
        if not python or not Path(python).is_file():
            return {
                "ok": False,
                "code": "pwntools_python_missing",
                "detail": "REA_PWNTOOLS_PYTHON 未指向有效解释器",
            }
        compatible = _python_component_compatible("pwntools", Path(python))
        return {
            "ok": compatible,
            "code": None if compatible else "pwntools_version_mismatch",
            "detail": (
                "pwntools 4.15.0、pyelftools 0.33 和 Unicorn 2.1.2 均可用"
                if compatible
                else "Python 环境缺少固定版本 pwntools/pyelftools/Unicorn"
            ),
        }

    if component_id == "wabt":
        directory = Path(str(extra_env.get("REA_WABT_BIN_DIRECTORY") or ""))
        executable = directory / ("wasm-validate.exe" if os.name == "nt" else "wasm-validate")
        if not executable.is_file():
            return {"ok": False, "code": "wabt_missing", "detail": "未找到 wasm-validate"}
        result = _run([str(executable), "--version"], timeout=20)
        version_text = f"{result.stdout}\n{result.stderr}".strip()
        return {
            "ok": result.returncode == 0 and "1.0.42" in version_text,
            "code": (
                None
                if result.returncode == 0 and "1.0.42" in version_text
                else "wabt_version_mismatch"
            ),
            "detail": version_text,
        }

    direct_commands = {
        "wakaru": ("REA_WAKARU_COMMAND", ["--version"]),
        "rizin": ("REA_RIZIN_COMMAND", ["-v"]),
        "gdb": ("REA_GDB_COMMAND", ["--version"]),
        "objdump": ("REA_OBJDUMP_COMMAND", ["--version"]),
        "sigrok": ("REA_SIGROK_COMMAND", ["--version"]),
    }
    direct = direct_commands.get(component_id)
    if direct is not None:
        env_key, args = direct
        if env_key:
            executable = str(extra_env.get(env_key) or "").strip()
        else:
            executable = ""
        if not executable or not Path(executable).is_file():
            return {
                "ok": False,
                "code": f"{component_id}_missing",
                "detail": f"未找到 {component_id} 可执行文件",
            }
        result = _run([executable, *args], timeout=30)
        version_text = f"{result.stdout}\n{result.stderr}".strip()
        expected_version = {
            "wakaru": "1.14.0",
            "rizin": "0.9.1",
        }.get(component_id)
        verified = result.returncode == 0 and (
            expected_version is None or expected_version in version_text
        )
        return {
            "ok": verified,
            "code": None if verified else f"{component_id}_probe_failed",
            "detail": version_text,
        }

    if component_id == "mitmproxy":
        executable = str(extra_env.get("REA_MITMDUMP_COMMAND") or "").strip()
        if not executable or not Path(executable).is_file():
            return {
                "ok": False,
                "code": "mitmproxy_missing",
                "detail": "未找到受管 mitmdump",
            }
        compatible = _python_component_compatible("mitmproxy", Path(executable))
        return {
            "ok": compatible,
            "code": None if compatible else "mitmproxy_version_mismatch",
            "detail": "mitmdump 12.2.3 可用" if compatible else "mitmdump 版本不是 12.2.3",
        }

    gui_executables = {
        "chrome": "REA_BROWSER_EXECUTABLE",
        "cutter": "REA_CUTTER_COMMAND",
    }
    gui_env_key = gui_executables.get(component_id)
    if gui_env_key is not None:
        executable = str(extra_env.get(gui_env_key) or "").strip()
        if executable and Path(executable).is_file():
            if component_id == "cutter":
                ready, reason = _cutter_bridge_readiness(Path(executable))
                return {
                    "ok": ready,
                    "code": None if ready else "cutter_bridge_incomplete",
                    "detail": (
                        f"{reason}；live 命令仍需用户先启动 Cutter 实例"
                        if ready
                        else reason
                    ),
                }
            return {
                "ok": True,
                "code": None,
                "detail": (
                    "浏览器可执行文件已安装；运行场景时仍需由 REA 启动或连接"
                    if component_id == "chrome"
                    else "Cutter 已安装；live 命令仍需用户先启动 Cutter 实例"
                ),
            }
        return {
            "ok": False,
            "code": f"{component_id}_missing",
            "detail": f"未找到 {component_id} 可执行文件",
        }

    client_commands = {
        "adb": "inspect-adb-client",
        "apktool": "inspect-apktool-client",
        "pcap": "inspect-pcap-client",
    }
    command = client_commands.get(component_id)
    if command is None:
        return {
            "ok": False,
            "code": "unsupported_verification",
            "detail": f"没有为 {component_id} 定义 REA 验证探针",
        }
    try:
        payload = _rea_cli_json(
            runtime,
            [command],
            extra_env=extra_env,
            timeout=120,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        available = payload.get("ok")
        if available is None:
            available = data.get("available")
        if available is None:
            available = True
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        remediation = error.get("remediation") if isinstance(error.get("remediation"), dict) else {}
        if bool(available):
            detail = (
                str(summary.get("description") or "").strip()
                or str(data.get("description") or "").strip()
                or f"REA {command} 验证通过"
            )
        else:
            detail = (
                str(error.get("message") or "").strip()
                or str(data.get("reason") or "").strip()
                or str(remediation.get("action") or "").strip()
                or f"REA {command} 验证失败"
            )
        return {
            "ok": bool(available),
            "code": (
                None
                if available
                else str(error.get("code") or data.get("code") or "client_probe_failed")
            ),
            "detail": detail,
            "version": summary.get("version") or data.get("version"),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "client_probe_failed",
            "detail": str(exc),
        }


def enhanced_component_plan(
    selected_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the manifest-backed install plan for the current host."""

    components = _manifest_components()
    selected = set(selected_ids) if selected_ids is not None else None
    plan: list[dict[str, Any]] = []
    for component_id, component in components.items():
        if selected is not None:
            if component_id not in selected:
                continue
        supported = _platform_supported(component)
        auto_supported = _auto_install_supported(component)
        mode = str(component.get("install_mode") or "manual")
        if not supported:
            state = "unsupported"
            reason = "当前操作系统或架构不支持此能力"
        elif mode in {"manual", "manual_license", "system"}:
            state = "needs_action"
            reason = str(component.get("install_hint") or "需要用户安装并完成授权或配置")
        elif not auto_supported:
            state = "needs_action"
            reason = str(component.get("install_hint") or "当前平台尚未提供自动安装适配器")
        else:
            state = "planned"
            reason = str(component.get("install_hint") or "可自动安装或复用本地兼容版本")
        plan.append(
            {
                "id": component_id,
                "label": str(component.get("display_name") or component_id),
                "group": str(component.get("group") or ""),
                "install_mode": mode,
                "state": state,
                "reason": reason,
                "auto_install": state == "planned",
                "platforms": component.get("platforms"),
                "default_selected": bool(component.get("default_selected")),
            }
        )
    return plan


def _persist_rea_env(
    workspace_path: Path,
    component_env: dict[str, str],
) -> None:
    """Merge one installed component environment into the registered MCP record."""

    if not component_env:
        return
    record = rea_mcp_record(workspace_path)
    if not record:
        return
    record["env"] = _record_env(record) | component_env
    record["state"] = "registered"
    payload = _read_state(workspace_path)
    payload.setdefault("mcp", {})[REA_MCP_NAME] = record
    _write_state(payload, workspace_path)


def install_enhanced_components(
    workspace: str | Path | None = None,
    selected_ids: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Install enhanced components without changing base installation semantics."""

    workspace_path = _workspace_path(workspace)
    record = rea_mcp_record(workspace_path)
    if not record:
        raise RuntimeError("请先完成基础 REA 安装和 MCP 验证")
    runtime = resolve_node_runtime(str(record.get("command") or ""))
    if runtime is None:
        raise RuntimeError("找不到用于验证增强能力的兼容 Node.js")

    plan = enhanced_component_plan(selected_ids)
    known = _manifest_components()
    if selected_ids is not None:
        unknown = sorted(set(selected_ids) - set(known))
        if unknown:
            raise ValueError(f"未知 REA 组件: {', '.join(unknown)}")
    record_env = _record_env(record)
    auto_ids = [
        str(item["id"])
        for item in plan
        if item.get("auto_install") is True
    ]

    _report_progress(
        progress_callback,
        "enhanced_plan",
        4,
        f"已生成 {len(plan)} 项增强能力计划，其中 {len(auto_ids)} 项可自动安装",
        {
            "phase": "planning",
            "component_count": len(plan),
            "auto_component_count": len(auto_ids),
        },
    )
    component_fractions = {component_id: 0.0 for component_id in auto_ids}
    component_downloaded_bytes = {component_id: 0 for component_id in auto_ids}
    component_total_bytes: dict[str, int] = {}
    for component_id in auto_ids:
        try:
            artifact = _component_artifact(_manifest_components()[component_id])
        except (KeyError, RuntimeError):
            continue
        component_total_bytes[component_id] = int(artifact.get("size_bytes") or 0)
    component_ranges = {
        component_id: (
            18.0 + index * (72.0 / max(1, len(auto_ids))),
            18.0 + (index + 1) * (72.0 / max(1, len(auto_ids))),
        )
        for index, component_id in enumerate(auto_ids)
    }
    fraction_lock = threading.Lock()

    def parallel_progress(
        stage: str,
        percent: float,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if progress_callback is None:
            return
        detail_payload = dict(details or {})
        component_id = str(detail_payload.get("component_id") or "").strip()
        if not component_id and stage.startswith("component_"):
            parts = stage.split("_", 2)
            if len(parts) == 3:
                component_id = parts[1]
        if component_id not in component_fractions:
            progress_callback(stage, percent, message, detail_payload or None)
            return
        start, end = component_ranges[component_id]
        local = max(0.0, min(1.0, (float(percent) - start) / max(1.0, end - start)))
        phase = str(detail_payload.get("phase") or "")
        if phase in {"complete", "cached", "verified", "failed", "skipped"}:
            local = 1.0 if phase != "skipped" else max(local, 0.5)
        with fraction_lock:
            component_fractions[component_id] = max(
                component_fractions[component_id],
                local,
            )
            if phase in {"cached", "reused"} or detail_payload.get("reused") is True:
                component_total_bytes[component_id] = 0
                component_downloaded_bytes[component_id] = 0
            downloaded = detail_payload.get("downloaded_bytes")
            if (
                phase not in {"cached", "reused"}
                and isinstance(downloaded, int)
            ):
                component_downloaded_bytes[component_id] = max(
                    component_downloaded_bytes[component_id],
                    downloaded,
                )
            aggregate = 5.0 + 86.0 * (
                sum(component_fractions.values()) / max(1, len(component_fractions))
            )
        detail_payload["component_progress"] = round(local, 4)
        detail_payload["component_total_bytes"] = component_total_bytes.get(component_id, 0)
        detail_payload["cumulative_downloaded_bytes"] = sum(
            component_downloaded_bytes.values()
        )
        detail_payload["cumulative_total_bytes"] = sum(component_total_bytes.values())
        detail_payload["parallel"] = True
        progress_callback(stage, aggregate, message, detail_payload)

    managed_env: dict[str, str] = {}
    managed_results: list[dict[str, Any]] = []
    pending_auto = {
        component_id: _manifest_components()[component_id]
        for component_id in auto_ids
    }
    while pending_auto:
        _raise_if_cancelled(cancel_check)
        ready_ids = [
            component_id
            for component_id, component in pending_auto.items()
            if all(
                _requirement_satisfied(str(requirement), record_env | managed_env)
                for requirement in component.get("requires") or []
            )
        ]
        if not ready_ids:
            for component_id, component in pending_auto.items():
                reason = "缺少安装前置条件"
                managed_results.append(
                    {
                        "id": component_id,
                        "label": str(component.get("display_name") or component_id),
                        "ok": False,
                        "state": "needs_action",
                        "verified": False,
                        "error": reason,
                        "reason": reason,
                    }
                )
                _report_progress(
                    parallel_progress if progress_callback is not None else None,
                    f"component_{component_id}_skipped",
                    92,
                    f"{component.get('display_name') or component_id}：{reason}",
                    {
                        "component_id": component_id,
                        "phase": "skipped",
                        "reason": reason,
                    },
                )
            pending_auto.clear()
            break

        results_by_id: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=min(4, len(ready_ids)),
            thread_name_prefix="rea-component",
        ) as executor:
            futures = {
                executor.submit(
                    install_managed_enhancements,
                    workspace_path,
                    parallel_progress if progress_callback is not None else None,
                    [component_id],
                    record_env | managed_env,
                    component_ranges[component_id][0],
                    component_ranges[component_id][1],
                    cancel_check,
                ): component_id
                for component_id in ready_ids
            }
            for future in as_completed(futures):
                component_id = futures[future]
                try:
                    phase_result = future.result()
                except InstallCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    results_by_id[component_id] = {
                        "id": component_id,
                        "ok": False,
                        "state": "failed",
                        "path": None,
                        "error": str(exc),
                    }
                    continue
                component_env = {
                    str(key): str(value)
                    for key, value in (phase_result.get("env") or {}).items()
                    if value is not None
                }
                managed_env.update(component_env)
                if component_env:
                    _persist_rea_env(workspace_path, component_env)
                component_results = [
                    item
                    for item in phase_result.get("results", [])
                    if isinstance(item, dict)
                ]
                if component_results:
                    results_by_id[component_id] = component_results[0]
                else:
                    results_by_id[component_id] = {
                        "id": component_id,
                        "ok": False,
                        "state": "failed",
                        "path": None,
                        "error": "组件安装器没有返回结果",
                    }
        managed_results.extend(results_by_id.values())
        for component_id in ready_ids:
            pending_auto.pop(component_id, None)
        _raise_if_cancelled(cancel_check)
    effective_env = record_env | managed_env
    results: list[dict[str, Any]] = []
    result_by_id = {
        str(item.get("id")): dict(item)
        for item in managed_results
        if isinstance(item, dict) and item.get("id")
    }

    for item in plan:
        component_id = str(item["id"])
        label = str(item["label"])
        planned_state = str(item["state"])
        managed_result = result_by_id.get(component_id)
        if planned_state != "planned":
            results.append(
                {
                    **item,
                    "ok": planned_state == "unsupported",
                    "state": planned_state,
                    "verified": False,
                    "error": None if planned_state == "unsupported" else item["reason"],
                }
            )
            continue
        if managed_result is None:
            results.append(
                {
                    **item,
                    "ok": False,
                    "state": "failed",
                    "verified": False,
                    "error": "安装器没有返回组件结果",
                }
            )
            continue
        state = str(managed_result.get("state") or "")
        if state in {"failed", "needs_action", "unsupported"} or not managed_result.get("ok"):
            results.append(
                {
                    **item,
                    **managed_result,
                    "verified": False,
                }
            )
            continue
        verification = _verify_component(component_id, runtime, effective_env)
        _raise_if_cancelled(cancel_check)
        verified = bool(verification.get("ok"))
        results.append(
            {
                **item,
                **managed_result,
                "state": "installed"
                if verified and state != "reused"
                else "reused"
                if verified
                else "failed",
                "ok": verified,
                "verified": verified,
                "verification": verification,
                "version": managed_result.get("version") or verification.get("version"),
                "error": None if verified else verification.get("detail"),
                "reason": None if verified else verification.get("detail"),
            }
        )
        _report_progress(
            parallel_progress if progress_callback is not None else None,
            f"component_{component_id}_verified",
            90 if verified else 92,
            f"{label}：{'REA 原生探测验证通过' if verified else 'REA 原生探测验证失败'}",
            {
                "component_id": component_id,
                "component_label": label,
                "phase": "verified" if verified else "verification_failed",
                "verified": verified,
                "detail": verification.get("detail"),
            },
        )

    installed_env = managed_env

    _report_progress(
        progress_callback,
        "enhanced_scan",
        96,
        "增强能力安装结束，正在刷新最终状态",
        {
            "phase": "scanning",
            "installed": sum(item.get("state") == "installed" for item in results),
            "reused": sum(item.get("state") == "reused" for item in results),
            "needs_action": sum(item.get("state") == "needs_action" for item in results),
            "unsupported": sum(item.get("state") == "unsupported" for item in results),
            "failed": sum(item.get("state") == "failed" for item in results),
        },
    )
    return {
        "env": installed_env,
        "plan": plan,
        "results": results,
        "scan": scan_rea_setup(workspace_path),
        "summary": {
            "installed": sum(item.get("state") == "installed" for item in results),
            "reused": sum(item.get("state") == "reused" for item in results),
            "needs_action": sum(item.get("state") == "needs_action" for item in results),
            "unsupported": sum(item.get("state") == "unsupported" for item in results),
            "failed": sum(item.get("state") == "failed" for item in results),
        },
    }


def scan_rea_setup(workspace: str | Path | None = None) -> dict[str, Any]:
    """Return the current base and enhanced REA capability snapshot."""

    record = rea_mcp_record(workspace)
    preferred_node = str(record.get("command") or "") if record else None
    runtime = resolve_node_runtime(preferred_node)
    rea_version = _installed_rea_version(runtime) if runtime is not None else None
    node_check = {
        "id": "node_npx",
        "label": "Node.js 与 npm/npx",
        "ok": runtime is not None,
        "required": True,
        "state": "ready" if runtime is not None else "missing",
        "detail": (
            f"Node.js {runtime.version} · {runtime.node}"
            if runtime is not None
            else "未找到满足 REA 要求的 Node.js ^22.23.4、^24.22.1 或 >=26.12.1"
        ),
        "version": runtime.version if runtime else None,
        "npm_version": runtime.npm_version if runtime else None,
        "path": str(runtime.node) if runtime else None,
    }

    dependencies_check = {
        "id": "rea_dependencies",
        "label": "REA 运行时",
        "ok": rea_version == REA_PACKAGE_VERSION,
        "required": True,
        "state": "ready" if rea_version == REA_PACKAGE_VERSION else "missing",
        "detail": (
            f"rea-agents {rea_version} 已下载"
            if rea_version == REA_PACKAGE_VERSION
            else f"未安装 {REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}"
        ),
        "version": rea_version,
    }
    mcp_check = _mcp_registration_check(runtime, workspace)
    base = [node_check, dependencies_check, mcp_check]
    ready = all(item["ok"] for item in base)
    platform_id = _platform_id()
    rea_skill = ensure_rea_skill(workspace)
    return {
        "ready": ready,
        "platform": f"{platform.system().lower()} {platform.machine().lower()}",
        "platform_id": platform_id,
        "rea_version": REA_PACKAGE_VERSION,
        "base": base,
        "enhanced": _scan_enhancements(runtime, _record_env(record), workspace) if ready else [],
        "enhanced_plan": enhanced_component_plan() if ready else [],
        "enhanced_blocked_by_base": not ready,
        "rea_skill": rea_skill,
        "generated_at": time.time(),
    }


def configure_component(
    component_id: str,
    values: dict[str, Any],
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Validate assisted input and write the resulting REA MCP env."""

    component = _manifest_components().get(str(component_id or "").strip())
    if component is None:
        raise ValueError(f"未知 REA 组件: {component_id}")
    if not _platform_supported(component):
        raise ValueError("当前平台不支持此能力")
    assisted = component.get("assisted_setup")
    inputs = assisted.get("inputs") if isinstance(assisted, dict) else None
    if not isinstance(inputs, list):
        raise ValueError("该组件没有可用的辅助配置输入")
    record = rea_mcp_record(workspace)
    if not record:
        raise ValueError("请先安装并验证基础 REA MCP")
    env = _record_env(record)
    workspace_path = _workspace_path(workspace)
    provided = {
        str(field.get("id") or ""): str(values.get(str(field.get("id") or "")) or "").strip()
        for field in inputs
        if isinstance(field, dict)
    }
    missing_required = [
        str(field.get("label") or field.get("id") or "输入项")
        for field in inputs
        if isinstance(field, dict)
        and bool(field.get("required"))
        and not provided.get(str(field.get("id") or ""))
    ]
    if missing_required:
        raise ValueError(f"请填写必填项: {', '.join(missing_required)}")
    required_any = assisted.get("required_any")
    if isinstance(required_any, list) and required_any:
        field_ids = {str(field_id) for field_id in required_any}
        if not any(provided.get(field_id) for field_id in field_ids):
            raise ValueError("请至少填写一个可用的配置项")

    for field in inputs:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        value = provided.get(field_id, "")
        if not value:
            continue
        field_type = str(field.get("type") or "path")
        env_key = str(field.get("env_key") or "").strip()
        target_rel = str(field.get("target") or "").strip()
        if not env_key:
            continue
        if field_type in {"path", "directory"}:
            selected = Path(value).expanduser()
            if not selected.is_absolute():
                raise ValueError(f"{field_id} 必须是绝对路径")
            try:
                selected = selected.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"{field_id} 指向的路径不存在") from exc
            if field_type == "directory" and not selected.is_dir():
                raise ValueError(f"{field_id} 必须是已存在的目录")
            if field_type == "path" and not selected.is_file():
                raise ValueError(f"{field_id} 必须是已存在的文件")
            if str(field.get("expect") or "") == "executable" and os.name != "nt":
                if not os.access(selected, os.X_OK):
                    raise ValueError(f"{field_id} 不是可执行文件")
            probes = field.get("probe_files")
            if isinstance(probes, list) and probes:
                if not any((selected / str(probe)).is_file() for probe in probes):
                    raise ValueError(
                        f"{field_id} 不像是正确的安装目录，未找到 {', '.join(map(str, probes))}"
                    )
            env[env_key] = str(selected)
            continue
        if field_type == "json":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field_id} 不是有效 JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{field_id} 必须是 JSON 对象")
            content = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
        else:
            if str(field.get("format") or "") == "url" and not value.startswith(
                ("http://", "https://")
            ):
                raise ValueError(f"{field_id} 必须是 http:// 或 https:// URL")
            content = value + ("" if value.endswith("\n") else "\n")
        if not target_rel:
            env[env_key] = value
            continue
        target = (workspace_path / target_rel).resolve()
        if workspace_path.resolve() not in target.parents:
            raise ValueError("组件配置目标路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        env[env_key] = str(target)
    record["env"] = env
    record["state"] = "registered"
    payload = _read_state(workspace)
    payload.setdefault("mcp", {})[REA_MCP_NAME] = record
    _write_state(payload, workspace)
    return scan_rea_setup(workspace)


def install_rea_base(
    workspace: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Install/cache the fixed REA package and write the Workswarm MCP record."""

    _raise_if_cancelled(cancel_check)
    _report_progress(progress_callback, "node_npx", 3, "检查或准备兼容的 Node.js")
    workspace_path = _workspace_path(workspace)
    record = rea_mcp_record(workspace_path)
    preferred_node = str(record.get("command") or "") if record else None

    def node_download_progress(fraction: float, detail: dict[str, Any]) -> None:
        _raise_if_cancelled(cancel_check)
        payload = dict(detail)
        downloaded = payload.get("downloaded_bytes")
        total = payload.get("total_bytes")
        if isinstance(downloaded, int):
            payload["cumulative_downloaded_bytes"] = downloaded
        if isinstance(total, int):
            payload["cumulative_total_bytes"] = total
        _report_progress(
            progress_callback,
            "node_runtime_download",
            3 + max(0.0, min(1.0, fraction)) * 5,
            "下载受管 Node.js 运行时",
            payload,
        )

    runtime = ensure_node_runtime(
        workspace_path,
        preferred_node,
        node_download_progress if progress_callback is not None else None,
    )

    _raise_if_cancelled(cancel_check)
    _report_progress(
        progress_callback,
        "rea_package",
        10,
        f"下载或核对 {REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION} 依赖",
        {"package": f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}"},
    )
    package_args = [
        "-y",
        f"{REA_PACKAGE_NAME}@{REA_PACKAGE_VERSION}",
        "--version",
    ]
    if _platform_id() in _REA_FRIDA_PREBUILDS:
        bootstrap = _npx_command(
            runtime,
            package_args,
            timeout=300,
            extra_env={"npm_config_ignore_scripts": "true"},
        )
        if bootstrap.returncode != 0:
            detail = (bootstrap.stderr or bootstrap.stdout or "REA 依赖下载失败").strip()
            raise RuntimeError(detail[-2000:])
        _install_rea_macos_frida_prebuild(workspace_path)
    result = _npx_command(
        runtime,
        package_args,
        timeout=300,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "REA 下载失败").strip()
        raise RuntimeError(detail[-2000:])
    _raise_if_cancelled(cancel_check)

    _report_progress(progress_callback, "write_config", 72, "写入固定版本 REA MCP 配置")
    existing_env = _record_env(record)
    record = _rea_record(runtime, state="registered")
    if isinstance(record.get("env"), dict):
        for key, value in existing_env.items():
            if key != "PATH":
                record["env"][key] = value
    payload = _read_state(workspace_path)
    payload.setdefault("mcp", {})[REA_MCP_NAME] = record
    _write_state(payload, workspace_path)
    _raise_if_cancelled(cancel_check)
    _report_progress(progress_callback, "base_snapshot", 84, "正在生成基础环境快照")
    scan = scan_rea_setup(workspace_path)
    scan["enhanced_install_results"] = []
    return scan


def reset_rea_setup(workspace: str | Path | None = None) -> dict[str, Any]:
    """Remove Workswarm-owned REA config and managed components."""

    workspace_path = _workspace_path(workspace)
    payload = _read_state(workspace_path)
    removed_record = payload.get("mcp", {}).pop(REA_MCP_NAME, None)
    _write_state(payload, workspace_path)

    for backup in (workspace_path / "mcp").glob("state.json.rea-setup-*.bak"):
        backup.unlink(missing_ok=True)

    managed_root = workspace_path / "rea"
    removed_managed_tools = False
    if managed_root.is_dir():
        shutil.rmtree(managed_root)
        removed_managed_tools = True
    return {
        "removed_record": isinstance(removed_record, dict),
        "removed_managed_tools": removed_managed_tools,
        "workspace": str(workspace_path),
    }


__all__ = [
    "REA_MCP_NAME",
    "REA_PACKAGE_NAME",
    "REA_PACKAGE_VERSION",
    "REA_SKILL_LABEL",
    "REA_SKILL_NAME",
    "InstallCancelled",
    "configure_component",
    "enhanced_component_plan",
    "ensure_rea_skill",
    "install_enhanced_components",
    "install_rea_base",
    "install_managed_enhancements",
    "load_component_manifest",
    "mark_rea_mcp_state",
    "rea_mcp_record",
    "reset_rea_setup",
    "resolve_node_runtime",
    "scan_rea_setup",
    "supports_rea_node",
]
