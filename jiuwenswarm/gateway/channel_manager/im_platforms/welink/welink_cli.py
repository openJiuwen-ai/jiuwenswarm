# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess wrapper for one WeLink account's welink-cli credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


# The logged-in employee account is a reader. These commands would speak as that person.
_PERSONAL_SEND_COMMANDS = frozenset({"send-to-group", "send-to-user"})


class WelinkPersonalSendForbidden(RuntimeError):
    """Raised when code tries to send a WeLink message as the logged-in person."""


def refuse_personal_send(args: list[str]) -> None:
    if any(str(arg) in _PERSONAL_SEND_COMMANDS for arg in args):
        raise WelinkPersonalSendForbidden("个人账号只能读取群记录，不能发送消息")


@dataclass(frozen=True, kw_only=True)
class WelinkCliResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class WelinkCli:
    """Run welink-cli with credentials isolated under one directory."""

    def __init__(self, cli_path: str, credential_dir: Path, *, env_name: str = "pro") -> None:
        self.cli_path = cli_path or "welink-cli"
        self.credential_dir = credential_dir
        self.env_name = env_name or "pro"

    def run(self, args: list[str], timeout: float = 30) -> WelinkCliResult:
        refuse_personal_send(args)
        self.credential_dir.mkdir(parents=True, exist_ok=True)
        command = build_command(self.cli_path, args)
        completed = subprocess.run(
            command,
            cwd=str(self.credential_dir),
            env=child_env(self.credential_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return WelinkCliResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def auth_status(self) -> WelinkCliResult:
        return self.run(["auth", "status"], timeout=20)

    def query_recent_conversations(self) -> WelinkCliResult:
        return self.run(["im", "query-recent-conversation", "--count", "20"], timeout=30)

    def query_history(self, group_id: str, message_id: str | None) -> WelinkCliResult:
        args = ["im", "query-history-message", "--group-id", group_id, "--query-count", "20"]
        if message_id:
            args.extend(["--message-id", message_id, "--query-direction", "1"])
        return self.run(args, timeout=30)

    def query_group_members(self, group_id: str) -> WelinkCliResult:
        return self.run(["im", "query-group-member", "--group-id", group_id], timeout=30)

    def download_share_link(self, link: str, output_dir: Path, access_code: str = "") -> WelinkCliResult:
        """Save one cloud-drive share into ``output_dir``. The link and access code are not logged."""
        output_dir.mkdir(parents=True, exist_ok=True)
        args = ["onebox", "file-share-link-download", "--link", link, "--output", str(output_dir), "--force"]
        code = access_code.strip()
        if code:
            args.extend(["--plain-access-code", code])
        return self.run(args, timeout=90)


def build_command(cli_path: str, args: list[str]) -> list[str]:
    resolved = shutil.which(cli_path) or cli_path
    native = _windows_native_cli(resolved)
    if native is not None:
        # cmd.exe ends the command at the first newline, so multiline text
        # passed through the npm .cmd shim keeps only its first line.
        return [native, *args]
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved, *args]
    return [resolved, *args]


def _windows_native_cli(resolved: str) -> str | None:
    """Resolve an npm welink-cli shim to the bundled executable, when it is installed beside it."""
    path = Path(resolved)
    stem = path.name
    for suffix in (".cmd", ".bat", ".exe"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem:
        return None
    candidate = path.parent / "node_modules" / "@welink" / "welink-cli" / f"{stem}.exe"
    if candidate.is_file():
        return str(candidate)
    return None


def child_env(credential_dir: Path) -> dict[str, str]:
    """Point the CLI at one account directory without touching the parent process."""
    env = os.environ.copy()
    root = str(credential_dir)
    env["WELINK_CONFIG_DIR"] = root
    env["WELINK_HOME"] = root
    env["XDG_CONFIG_HOME"] = root
    env["HOME"] = root
    env["USERPROFILE"] = root
    return env
