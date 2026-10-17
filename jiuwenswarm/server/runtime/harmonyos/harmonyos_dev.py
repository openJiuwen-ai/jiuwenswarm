# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""DevEco CLI bootstrap support for the TUI channel."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.harmonyos.harmonyos_project import (
    HarmonyOSProjectError,
    inspect_harmonyos_project,
    persist_harmonyos_project_context,
)
from jiuwenswarm.common.utils import get_agent_skills_dir


DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
INSTALL_TIMEOUT_SECONDS = 5 * 60.0
UPDATE_TIMEOUT_SECONDS = 3 * 60.0
INIT_TIMEOUT_SECONDS = 2 * 60.0
COMMAND_TERMINATION_TIMEOUT_SECONDS = 5.0
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
COMMAND_OUTPUT_READ_CHUNK_BYTES = 64 * 1024
MIN_NODE_MAJOR_VERSION = 18
DEVECOCLI_NPM_PACKAGE = "@deveco/deveco-cli@latest"
NPM_FETCH_TIMEOUT_MILLISECONDS = 30_000
NPM_FETCH_RETRIES = 1
HARMONYOS_DEV_SUITE_NAME = "harmonyos-dev-suite"
HARMONYOS_DEV_SUITE_VERSION = "1"
HARMONYOS_DEV_SUITE_METADATA = ".jiuwenswarm-managed.json"
HARMONYOS_DEV_SUITE_METADATA_SCHEMA = 1
# Content digests of previously shipped official suite trees (metadata file
# excluded). Used to safely migrate installs that predate managed metadata when
# the built-in suite later advances to a new digest. Never treat "has SKILL.md"
# alone as proof of an official tree.
HARMONYOS_DEV_SUITE_HISTORICAL_OFFICIAL_DIGESTS: frozenset[str] = frozenset(
    {
        # Official suite content at 88aafedca (pre-managed-metadata baseline).
        "9b6658ba256a3b24ae0c5c8c23b4055993cb7d69fe1cc8eab18a515632681470",
    }
)
DEVECO_BASE_SKILL_NAME = "deveco-cli"
HARMONYOS_KNOWLEDGE_MCP_NAME = "harmonyos_developer_knowledge"
HARMONYOS_KNOWLEDGE_MCP_URL = (
    "https://connect-api.cloud.huawei.com/api/developerknowledge/mcp"
)
HARMONYOS_KNOWLEDGE_MCP_TOOLS = ["searchDocuments", "getDocumentsById"]


async def run_harmonyos_project_init(params: dict[str, Any]) -> dict[str, Any]:
    """Inspect a HarmonyOS project for the TUI initialization command."""
    raw_path = params.get("path") or params.get("project_dir") or params.get("cwd")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HarmonyOSProjectError("project path is required")

    context = inspect_harmonyos_project(raw_path)
    runtime = await detect_executable("devecocli", ["devecocli", "--version"])
    state_path = persist_harmonyos_project_context(context)
    return {
        "ok": True,
        "context": context,
        "runtime": {"devecocli": runtime},
        "statePath": str(state_path),
    }


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": self.command,
            "returncode": self.returncode,
            "stdout": _clip(self.stdout),
            "stderr": _clip(self.stderr),
            "error": self.error,
            "timed_out": self.timed_out,
        }


async def run_harmonyos_dev_init(params: dict[str, Any]) -> dict[str, Any]:
    """Ensure devecocli is installed and initialize the HarmonyOS Skills."""
    # The target is deliberately not configurable through RPC parameters. It is
    # JiuwenSwarm-owned state, not a user project path.
    install_confirmed = params.get("installDevecocliConfirmed") is True
    update_confirmed = params.get("updateDevecocliConfirmed") is True
    update_skipped = params.get("skipDevecocliUpdate") is True
    if update_confirmed and update_skipped:
        raise ValueError(
            "updateDevecocliConfirmed and skipDevecocliUpdate are mutually exclusive"
        )
    skills_dir = get_agent_skills_dir().expanduser().resolve()
    skills_dir.mkdir(parents=True, exist_ok=True)
    before_skill_files = _find_skill_files(skills_dir)

    runtime: dict[str, Any] = {
        "devecocli": await detect_executable("devecocli", ["devecocli", "--version"]),
    }
    actions: dict[str, Any] = {
        "skillsPath": str(skills_dir),
        "skillsPathSource": "jiuwenswarm.common.utils.get_agent_skills_dir",
        "installDevecocliAttempted": False,
        "updateDevecocliAttempted": False,
        "initSkillAttempted": False,
        "installSuiteAttempted": False,
    }
    needs_confirmation = False
    needs_update_confirmation = False
    devecocli_was_available = bool(runtime["devecocli"].get("ok"))

    if not runtime["devecocli"].get("ok"):
        runtime["node"] = await detect_node()
        if not runtime["node"].get("supported"):
            actions["installDevecocli"] = {
                "ok": False,
                "skipped": True,
                "reason": (
                    f"Node.js >= {MIN_NODE_MAJOR_VERSION} is required; "
                    "install or upgrade Node.js manually"
                ),
            }
        else:
            runtime["npm"] = await detect_executable("npm", ["npm", "--version"])
            if runtime["npm"].get("ok"):
                install_command = _devecocli_install_command(
                    str(runtime["npm"]["path"])
                )
                if install_confirmed:
                    actions["installDevecocliAttempted"] = True
                    actions["installDevecocli"] = await install_devecocli(
                        str(runtime["npm"]["path"])
                    )
                    runtime["devecocli"] = await detect_executable(
                        "devecocli", ["devecocli", "--version"]
                    )
                else:
                    needs_confirmation = True
                    actions["installDevecocli"] = {
                        "ok": False,
                        "skipped": True,
                        "requiresConfirmation": True,
                        "reason": "user confirmation is required before global npm install",
                        "command": install_command,
                    }
            else:
                actions["installDevecocli"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "npm is not available",
                }

    if devecocli_was_available:
        devecocli_path = str(runtime["devecocli"]["path"])
        if update_confirmed:
            actions["updateDevecocliAttempted"] = True
            actions["updateDevecocli"] = await update_devecocli(devecocli_path)
            if actions["updateDevecocli"].get("ok") is True:
                runtime["devecocli"] = await detect_executable(
                    "devecocli", ["devecocli", "--version"]
                )
                if runtime["devecocli"].get("ok") is not True:
                    actions["updateDevecocli"] = {
                        **actions["updateDevecocli"],
                        "ok": False,
                        "error": (
                            "devecocli update completed, but the updated executable "
                            "could not be verified"
                        ),
                        "postUpdateVerification": runtime["devecocli"],
                    }
        elif update_skipped:
            actions["updateDevecocli"] = {
                "ok": True,
                "skipped": True,
                "reason": "user chose to continue without updating devecocli",
                "command": [devecocli_path, "update"],
            }
        else:
            needs_update_confirmation = True
            actions["updateDevecocli"] = {
                "ok": False,
                "skipped": True,
                "requiresConfirmation": True,
                "reason": "user confirmation is required before updating devecocli",
                "command": [devecocli_path, "update"],
            }
    elif (
        runtime["devecocli"].get("ok")
        and isinstance(actions.get("installDevecocli"), dict)
        and actions["installDevecocli"].get("ok") is True
    ):
        actions["updateDevecocli"] = {
            "ok": True,
            "skipped": True,
            "reason": "freshly installed @latest; no additional update is required",
        }
    elif runtime["devecocli"].get("ok"):
        actions["updateDevecocli"] = {
            "ok": False,
            "skipped": True,
            "reason": (
                "devecocli became visible, but the confirmed installation did not "
                "finish successfully; Skill initialization was not attempted"
            ),
        }
    else:
        actions["updateDevecocli"] = {
            "ok": False,
            "skipped": True,
            "reason": "devecocli is not available",
        }

    update_action = actions["updateDevecocli"]
    update_action_ok = (
        isinstance(update_action, dict) and update_action.get("ok") is True
    )
    update_ok = update_action_ok and not needs_update_confirmation
    update_failed_after_confirm = (
        devecocli_was_available and update_confirmed and not update_action_ok
    )
    if update_failed_after_confirm:
        actions["initSkill"] = {
            "ok": False,
            "skipped": True,
            "reason": "devecocli update failed; Skill refresh was not attempted",
        }

    if runtime["devecocli"].get("ok") and update_ok:
        actions["initSkillAttempted"] = True
        devecocli_path = str(runtime["devecocli"]["path"])
        actions["initSkill"] = (
            await run_command(
                [
                    devecocli_path,
                    "init",
                    "--skill",
                    "--path",
                    str(skills_dir),
                    "--force",
                ],
                timeout=INIT_TIMEOUT_SECONDS,
            )
        ).to_dict()
    elif "initSkill" not in actions:
        actions["initSkill"] = {
            "ok": False,
            "skipped": True,
            "reason": (
                "waiting for devecocli update confirmation"
                if needs_update_confirmation
                else "devecocli is not available"
            ),
        }

    init_action = actions["initSkill"]
    init_ok = isinstance(init_action, dict) and init_action.get("ok") is True
    base_skill_path = skills_dir / DEVECO_BASE_SKILL_NAME / "SKILL.md"
    base_skill_found = init_ok and base_skill_path.is_file()

    if base_skill_found:
        actions["installSuiteAttempted"] = True
        actions["installSuite"] = install_builtin_harmonyos_dev_suite(skills_dir)
    else:
        actions["installSuite"] = {
            "ok": False,
            "skipped": True,
            "reason": "devecocli base Skill was not verified",
        }

    after_skill_files = _find_skill_files(skills_dir)
    suite_action = actions["installSuite"]
    suite_ok = isinstance(suite_action, dict) and suite_action.get("ok") is True
    suite_skill_path = skills_dir / HARMONYOS_DEV_SUITE_NAME / "SKILL.md"
    suite_skill_found = suite_skill_path.is_file()
    verification_ok = base_skill_found and suite_ok and suite_skill_found
    skill_verification = {
        "checked": init_ok,
        "ok": verification_ok,
        "skillsPath": str(skills_dir),
        "skillCount": len(after_skill_files),
        "skillFiles": sorted(after_skill_files),
        "newSkillFiles": sorted(after_skill_files - before_skill_files),
        "baseSkillFound": base_skill_found,
        "baseSkillPath": str(base_skill_path),
        "suiteSkillFound": suite_skill_found,
        "suiteSkillPath": str(suite_skill_path),
        "reason": (
            "devecocli base Skill and HarmonyOS Dev Suite verified"
            if verification_ok
            else "required HarmonyOS Skills were not fully verified"
        ),
    }
    return {
        "ok": bool(
            runtime["devecocli"].get("ok") and update_ok and init_ok and verification_ok
        ),
        "needsConfirmation": needs_confirmation,
        "needsUpdateConfirmation": needs_update_confirmation,
        "runtime": runtime,
        "actions": actions,
        "skillVerification": skill_verification,
        "knowledgeMcp": {
            "status": "available",
            "config": {
                "name": HARMONYOS_KNOWLEDGE_MCP_NAME,
                "enabled": True,
                "transport": "streamable-http",
                "url": HARMONYOS_KNOWLEDGE_MCP_URL,
                "timeout_s": 60,
            },
            "expectedTools": list(HARMONYOS_KNOWLEDGE_MCP_TOOLS),
        },
    }


async def detect_executable(name: str, version_command: list[str]) -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        return {
            "ok": False,
            "name": name,
            "path": None,
            "version": None,
            "error": f"{name} not found in PATH",
        }

    full_command = [path] + version_command[1:]
    result = await run_command(full_command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
    version_lines = (result.stdout or result.stderr).strip().splitlines()
    return {
        "ok": result.ok,
        "name": name,
        "path": path,
        "version": version_lines[0].strip() if version_lines else None,
        "error": result.error,
        "command": result.to_dict(),
    }


async def detect_node() -> dict[str, Any]:
    result = await detect_executable("node", ["node", "--version"])
    major = _parse_node_major(result.get("version"))
    result["major"] = major
    result["minimumMajor"] = MIN_NODE_MAJOR_VERSION
    result["supported"] = bool(
        result.get("ok") and major is not None and major >= MIN_NODE_MAJOR_VERSION
    )
    if result.get("ok") and major is None:
        result["error"] = f"unable to parse Node.js version: {result.get('version')!r}"
    elif result.get("ok") and not result["supported"]:
        result["error"] = (
            f"Node.js {major} is too old; version >= {MIN_NODE_MAJOR_VERSION} is required"
        )
    return result


async def install_devecocli(npm_path: str) -> dict[str, Any]:
    result = await run_command(
        _devecocli_install_command(npm_path),
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    payload = result.to_dict()
    if result.ok:
        return payload

    detail = f"{result.error or ''}\n{result.stderr}\n{result.stdout}".lower()
    network_markers = (
        "eai_again",
        "econnrefused",
        "econnreset",
        "enetunreach",
        "etimedout",
        "network",
    )
    if result.timed_out:
        payload["error"] = (
            "devecocli installation timed out after "
            f"{INSTALL_TIMEOUT_SECONDS:g}s and was stopped. Check npm registry "
            "access with `npm ping`, then retry /harmonyos-dev-init; run the "
            "displayed install command in a terminal if full npm logs are needed."
        )
    elif any(marker in detail for marker in ("eacces", "eperm", "permission denied")):
        payload["error"] = (
            f"{result.error or 'npm global install failed'}. Check the writable "
            "global npm prefix with `npm config get prefix`, then retry "
            "/harmonyos-dev-init."
        )
    elif any(marker in detail for marker in network_markers):
        payload["error"] = (
            f"{result.error or 'npm global install failed'}. Check npm registry "
            "access with `npm ping`, then retry /harmonyos-dev-init."
        )
    return payload


def _devecocli_install_command(npm_path: str) -> list[str]:
    return [
        npm_path,
        "install",
        "-g",
        DEVECOCLI_NPM_PACKAGE,
        "--no-audit",
        "--no-fund",
        f"--fetch-timeout={NPM_FETCH_TIMEOUT_MILLISECONDS}",
        f"--fetch-retries={NPM_FETCH_RETRIES}",
    ]


async def update_devecocli(devecocli_path: str) -> dict[str, Any]:
    return (
        await run_command(
            [devecocli_path, "update"],
            timeout=UPDATE_TIMEOUT_SECONDS,
        )
    ).to_dict()


def install_builtin_harmonyos_dev_suite(skills_dir: Path) -> dict[str, Any]:
    source = _builtin_harmonyos_dev_suite_dir()
    target = skills_dir / HARMONYOS_DEV_SUITE_NAME
    if not (source / "SKILL.md").is_file():
        return {
            "ok": False,
            "name": HARMONYOS_DEV_SUITE_NAME,
            "error": f"built-in suite is missing SKILL.md: {source}",
        }
    if any(path.is_symlink() for path in source.rglob("*")):
        return {
            "ok": False,
            "name": HARMONYOS_DEV_SUITE_NAME,
            "error": f"built-in suite contains a symbolic link: {source}",
        }
    source_digest = _suite_tree_digest(source)
    if target.is_symlink():
        return {
            "ok": False,
            "name": HARMONYOS_DEV_SUITE_NAME,
            "error": f"suite target must not be a symbolic link: {target}",
        }
    if target.exists():
        if not target.is_dir() or not (target / "SKILL.md").is_file():
            return {
                "ok": False,
                "name": HARMONYOS_DEV_SUITE_NAME,
                "error": f"invalid existing suite target: {target}",
            }
        if any(path.is_symlink() for path in target.rglob("*")):
            return {
                "ok": False,
                "name": HARMONYOS_DEV_SUITE_NAME,
                "error": f"existing suite contains a symbolic link: {target}",
            }

        target_digest = _suite_tree_digest(target)
        metadata = _read_suite_metadata(target)
        if target_digest == source_digest:
            _write_suite_metadata(target, source_digest)
            return {
                "ok": True,
                "name": HARMONYOS_DEV_SUITE_NAME,
                "sourcePath": str(source),
                "targetPath": str(target),
                "alreadyInstalled": True,
                "managed": True,
                "version": HARMONYOS_DEV_SUITE_VERSION,
                "sourceDigest": source_digest,
            }

        managed_match = _metadata_matches_installed_tree(metadata, target_digest)
        historical_official = (
            target_digest in HARMONYOS_DEV_SUITE_HISTORICAL_OFFICIAL_DIGESTS
        )
        if managed_match or historical_official:
            _replace_suite_atomically(source, target, source_digest)
            return {
                "ok": True,
                "name": HARMONYOS_DEV_SUITE_NAME,
                "sourcePath": str(source),
                "targetPath": str(target),
                "alreadyInstalled": False,
                "updated": True,
                "managed": True,
                "historicalOfficial": historical_official and not managed_match,
                "version": HARMONYOS_DEV_SUITE_VERSION,
                "previousDigest": target_digest,
                "sourceDigest": source_digest,
            }
        return {
            "ok": False,
            "name": HARMONYOS_DEV_SUITE_NAME,
            "targetPath": str(target),
            "conflict": True,
            "error": (
                "existing suite is unmanaged or was modified; refusing to overwrite: "
                f"{target}"
            ),
        }

    with tempfile.TemporaryDirectory(
        prefix=f".{HARMONYOS_DEV_SUITE_NAME}.", dir=str(skills_dir)
    ) as tmp:
        staged = Path(tmp) / HARMONYOS_DEV_SUITE_NAME
        shutil.copytree(source, staged, symlinks=False)
        if not (staged / "SKILL.md").is_file():
            return {
                "ok": False,
                "name": HARMONYOS_DEV_SUITE_NAME,
                "error": "staged suite is missing SKILL.md",
            }
        _write_suite_metadata(staged, source_digest)
        staged.rename(target)

    return {
        "ok": True,
        "name": HARMONYOS_DEV_SUITE_NAME,
        "sourcePath": str(source),
        "targetPath": str(target),
        "alreadyInstalled": False,
        "managed": True,
        "version": HARMONYOS_DEV_SUITE_VERSION,
        "sourceDigest": source_digest,
    }


def _suite_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        if path.is_symlink():
            raise ValueError(f"suite tree contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == HARMONYOS_DEV_SUITE_METADATA:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_suite_metadata(target: Path) -> dict[str, Any] | None:
    metadata_path = target / HARMONYOS_DEV_SUITE_METADATA
    if metadata_path.is_symlink() or not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _metadata_matches_installed_tree(
    metadata: dict[str, Any] | None, target_digest: str
) -> bool:
    return bool(
        metadata
        and metadata.get("schemaVersion") == HARMONYOS_DEV_SUITE_METADATA_SCHEMA
        and metadata.get("name") == HARMONYOS_DEV_SUITE_NAME
        and metadata.get("sourceDigest") == target_digest
    )


def _write_suite_metadata(target: Path, source_digest: str) -> None:
    payload = {
        "schemaVersion": HARMONYOS_DEV_SUITE_METADATA_SCHEMA,
        "name": HARMONYOS_DEV_SUITE_NAME,
        "version": HARMONYOS_DEV_SUITE_VERSION,
        "sourceDigest": source_digest,
    }
    metadata_path = target / HARMONYOS_DEV_SUITE_METADATA
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{HARMONYOS_DEV_SUITE_METADATA}.", dir=str(target)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, metadata_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _replace_suite_atomically(source: Path, target: Path, source_digest: str) -> None:
    tmp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{HARMONYOS_DEV_SUITE_NAME}.upgrade.",
            dir=str(target.parent),
        )
    )
    preserve_recovery_copy = False
    try:
        staged = tmp_dir / HARMONYOS_DEV_SUITE_NAME
        backup = tmp_dir / f"{HARMONYOS_DEV_SUITE_NAME}.previous"
        shutil.copytree(source, staged, symlinks=False)
        _write_suite_metadata(staged, source_digest)
        target.rename(backup)
        try:
            staged.rename(target)
        except Exception as replace_error:
            if target.exists():
                preserve_recovery_copy = True
                raise RuntimeError(
                    "suite upgrade target reappeared; the previous managed copy "
                    f"was preserved at {backup}"
                ) from replace_error
            try:
                backup.rename(target)
            except OSError as restore_error:
                preserve_recovery_copy = True
                raise RuntimeError(
                    "suite upgrade failed and automatic rollback failed; the "
                    f"previous managed copy remains at {backup}"
                ) from restore_error
            raise
    finally:
        if not preserve_recovery_copy:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _read_stream_bounded(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    captured = bytearray()
    omitted = 0
    while True:
        chunk = await stream.read(COMMAND_OUTPUT_READ_CHUNK_BYTES)
        if not chunk:
            break
        remaining = max(0, MAX_COMMAND_OUTPUT_BYTES - len(captured))
        if remaining:
            captured.extend(chunk[:remaining])
        omitted += max(0, len(chunk) - remaining)
    if omitted:
        captured.extend(f"\n...[{omitted} output bytes truncated]".encode("utf-8"))
    return bytes(captured)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    terminated = False
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill")
        if taskkill and proc.pid is not None:
            try:
                killer = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(
                    killer.wait(), timeout=COMMAND_TERMINATION_TIMEOUT_SECONDS
                )
                terminated = killer.returncode == 0
            except (OSError, asyncio.TimeoutError):
                terminated = False
    elif proc.pid is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            terminated = True
        except (ProcessLookupError, PermissionError):
            terminated = False

    if not terminated and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=COMMAND_TERMINATION_TIMEOUT_SECONDS)


def _completed_stream_bytes(task: asyncio.Task[bytes]) -> bytes:
    if not task.done() or task.cancelled():
        return b""
    try:
        return task.result()
    except Exception:
        return b""


async def run_command(
    command: list[str],
    *,
    timeout: float,
) -> CommandResult:
    # Windows: .cmd/.bat files cannot be executed directly by CreateProcessW;
    # wrap with cmd.exe /c so the Windows shell resolves and executes them.
    if sys.platform == "win32" and command:
        exe = command[0].lower()
        if exe.endswith((".cmd", ".bat")):
            command = ["cmd.exe", "/c"] + command

    spawn_kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        spawn_kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
    except OSError as exc:
        return CommandResult(
            ok=False,
            command=command,
            returncode=None,
            stdout="",
            stderr="",
            error=str(exc),
        )

    stdout_task = asyncio.create_task(_read_stream_bounded(proc.stdout))
    stderr_task = asyncio.create_task(_read_stream_bounded(proc.stderr))
    wait_task = asyncio.create_task(proc.wait())
    completion = asyncio.gather(wait_task, stdout_task, stderr_task)
    try:
        returncode, stdout_b, stderr_b = await asyncio.wait_for(
            asyncio.shield(completion), timeout=timeout
        )
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        try:
            _, stdout_b, stderr_b = await asyncio.wait_for(
                asyncio.shield(completion),
                timeout=COMMAND_TERMINATION_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, OSError):
            stdout_b = _completed_stream_bytes(stdout_task)
            stderr_b = _completed_stream_bytes(stderr_task)
            completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)
        return CommandResult(
            ok=False,
            command=command,
            returncode=None,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            error=f"command timed out after {timeout:.0f}s",
            timed_out=True,
        )
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)
        raise
    except OSError as exc:
        await _terminate_process_tree(proc)
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)
        return CommandResult(
            ok=False,
            command=command,
            returncode=proc.returncode,
            stdout=_completed_stream_bytes(stdout_task).decode(
                "utf-8", errors="replace"
            ),
            stderr=_completed_stream_bytes(stderr_task).decode(
                "utf-8", errors="replace"
            ),
            error=str(exc),
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return CommandResult(
        ok=returncode == 0,
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error=None if returncode == 0 else _clip(stderr or stdout),
    )


def _parse_node_major(version: Any) -> int | None:
    match = re.match(r"^v?(\d+)", str(version or "").strip())
    return int(match.group(1)) if match else None


def _builtin_harmonyos_dev_suite_dir() -> Path:
    package_root = Path(__file__).resolve().parents[3]
    return (
        package_root
        / "resources"
        / "agent"
        / "workspace"
        / "skills"
        / HARMONYOS_DEV_SUITE_NAME
    )


def _find_skill_files(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()
    return {
        str(path.relative_to(skills_dir))
        for path in skills_dir.rglob("SKILL.md")
        if path.is_file()
    }


def _clip(value: str, max_chars: int = 4000) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...[truncated]"
