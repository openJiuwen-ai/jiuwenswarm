# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One in-flight welink-cli OAuth login, surfaced as a QR for the office UI."""

from __future__ import annotations

import logging
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import (
    WelinkCli,
    build_command,
    child_env,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import (
    extract_login_prompt,
    parse_auth_status,
    redact_secrets,
)

logger = logging.getLogger(__name__)


@dataclass
class WelinkLoginSession:
    session_id: str
    agent_id: str
    account: str
    credential_dir: Path
    status: str = "pending"
    qr_url: str = ""
    qr_text: str = ""
    output: str = ""
    error: str = ""
    resolved_account: str = ""
    replaced_account: str = ""
    cli_path: str = ""
    env_name: str = "pro"
    restarts: int = 0
    saved: bool = False
    _process: subprocess.Popen[str] | None = field(default=None, repr=False)
    _stopped: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "status": self.status,
                "qr_url": self.qr_url,
                "qr_text": self.qr_text,
                "output": self.output[-4000:],
                "error": self.error,
                "account": self.resolved_account or self.account,
            }


class WelinkLoginManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WelinkLoginSession] = {}
        self._lock = threading.Lock()

    def start(self, *, agent_id: str, account: str, credential_dir: Path, cli_path: str, env_name: str) -> WelinkLoginSession:
        with self._lock:
            for existing in list(self._sessions.values()):
                if existing.agent_id != agent_id or existing.status != "pending":
                    continue
                # A second click used to kill the QR process. On Windows that
                # wait()s with exit code 1, which the dialog showed as a failure
                # even though the code was still on screen.
                if _same_account(existing.account, account):
                    return existing
                self._stop_process(existing)
                existing.status = "cancelled"
            session = WelinkLoginSession(
                session_id=uuid.uuid4().hex,
                agent_id=agent_id,
                account=account,
                credential_dir=credential_dir,
                cli_path=cli_path,
                env_name=env_name,
            )
            self._sessions[session.session_id] = session
        self._logout_other_account(session, cli_path, env_name)
        with self._lock:
            if session.status != "pending":
                return session
        self._spawn(session, cli_path, env_name)
        return session

    def get(self, session_id: str) -> WelinkLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def refresh(self, session: WelinkLoginSession, cli_path: str, env_name: str) -> dict[str, str]:
        if session.status == "pending":
            self._read_output(session)
            try:
                status = WelinkCli(cli_path, session.credential_dir, env_name=env_name).auth_status()
            except Exception as exc:
                logger.warning("[Welink] auth status failed: %s", type(exc).__name__)
                status = None
            if status is not None and status.ok:
                logged_in, resolved = parse_auth_status(status.stdout, status.stderr)
                if logged_in:
                    process = session._process
                    if (
                        session.replaced_account
                        and _same_account(resolved, session.replaced_account)
                        and process is not None
                        and process.poll() is None
                    ):
                        return session.snapshot()
                    _accept_login(session, True, resolved)
                    self._stop_process(session)
        return session.snapshot()

    def _logout_other_account(self, session: WelinkLoginSession, cli_path: str, env_name: str) -> None:
        """Drop a previous login before showing a QR for a different account.

        ``auth status`` otherwise reports the account already stored in this
        colleague's directory, and the new login is rejected before a QR exists.
        """
        cli = WelinkCli(cli_path, session.credential_dir, env_name=env_name)
        try:
            status = cli.auth_status()
        except Exception as exc:
            logger.warning("[Welink] auth status before login failed: %s", type(exc).__name__)
            return
        if not status.ok:
            return
        logged_in, resolved = parse_auth_status(status.stdout, status.stderr)
        if not logged_in or not resolved or _same_account(resolved, session.account):
            return
        session.replaced_account = resolved
        try:
            logout = cli.run(["auth", "logout"], timeout=20)
        except Exception as exc:
            logger.warning("[Welink] auth logout failed: %s", type(exc).__name__)
            session.status = "failed"
            session.error = f"当前登录的是 {resolved}，注销失败，无法换成 {session.account}"
            return
        if not logout.ok:
            session.status = "failed"
            session.error = f"当前登录的是 {resolved}，注销失败，无法换成 {session.account}"
            return
        logger.info("[Welink] logged out previous account before a new QR login")

    def _spawn(self, session: WelinkLoginSession, cli_path: str, env_name: str) -> None:
        session.credential_dir.mkdir(parents=True, exist_ok=True)
        command = build_command(cli_path, ["auth", "login", "--env", env_name])
        try:
            process = subprocess.Popen(
                command,
                cwd=str(session.credential_dir),
                env=child_env(session.credential_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            session.status = "failed"
            session.error = f"无法启动 welink-cli：{exc}"
            return
        session._process = process
        thread = threading.Thread(target=self._drain, args=(session,), name=f"welink-login-{session.session_id[:8]}", daemon=True)
        thread.start()

    def _drain(self, session: WelinkLoginSession) -> None:
        process = session._process
        if process is None or process.stdout is None:
            return
        chunks: list[str] = []
        try:
            for line in process.stdout:
                chunks.append(line)
                text = redact_secrets("".join(chunks))
                qr_url, qr_text = extract_login_prompt(text)
                with session._lock:
                    session.output = text
                    if qr_url:
                        session.qr_url = qr_url
                    if qr_text:
                        session.qr_text = qr_text
        except Exception:
            logger.warning("[Welink] login output reader stopped")
        code = process.wait()
        with session._lock:
            if session._stopped or session.status != "pending":
                return
            output = session.output
        if code in (0, None):
            self._finish_from_auth_status(session)
            return
        logged_in, resolved = self._probe_auth(session)
        with session._lock:
            if session._stopped or session.status != "pending":
                return
            if _accept_login(session, logged_in, resolved):
                return
            message = _login_failure_message(output, code)
            if _should_retry_login(output, message) and session.restarts < 2:
                session.restarts += 1
                session.qr_url = ""
                session.qr_text = ""
                session.error = ""
                session._stopped = False
                retry = True
            else:
                session.status = "failed"
                session.error = message
                logger.warning("[Welink] auth login failed: %s", message)
                retry = False
        if retry:
            logger.info("[Welink] auth login ended before the scan completed, requesting a new QR")
            self._spawn(session, session.cli_path, session.env_name)

    def _finish_from_auth_status(self, session: WelinkLoginSession) -> None:
        logged_in, resolved = self._probe_auth(session)
        with session._lock:
            if session._stopped or session.status != "pending":
                return
            if _accept_login(session, logged_in, resolved):
                return
            session.status = "failed"
            session.error = "扫码进程已结束，但没有拿到登录状态"

    def _probe_auth(self, session: WelinkLoginSession) -> tuple[bool, str]:
        try:
            status = WelinkCli(session.cli_path or "welink-cli", session.credential_dir, env_name=session.env_name).auth_status()
        except Exception as exc:
            logger.warning("[Welink] auth status after login failed: %s", type(exc).__name__)
            return False, ""
        if not status.ok:
            return False, ""
        return parse_auth_status(status.stdout, status.stderr)

    def _read_output(self, session: WelinkLoginSession) -> None:
        with session._lock:
            qr_url, qr_text = extract_login_prompt(session.output)
            if qr_url:
                session.qr_url = qr_url
            if qr_text:
                session.qr_text = qr_text

    @staticmethod
    def _stop_process(session: WelinkLoginSession) -> None:
        with session._lock:
            session._stopped = True
            process = session._process
        if process is None or process.poll() is not None:
            return
        process.terminate()


def _same_account(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _accept_login(session: WelinkLoginSession, logged_in: bool, resolved: str) -> bool:
    """Record the scanned account. Caller holds ``session._lock``.

    A declared account must match the scan. With no declaration, the scan itself
    is the account. A login that does not identify anyone is not saved.
    """
    if not logged_in:
        return False
    expected = session.account.strip()
    scanned = resolved.strip()
    if expected and scanned and not _same_account(scanned, expected):
        session.status = "failed"
        session.error = f"扫码账号是 {scanned}，与填写的 {expected} 不一致"
        return True
    if not scanned and not expected:
        session.status = "failed"
        session.error = "扫码成功，但没有识别到 WeLink 账号"
        return True
    session.status = "success"
    session.resolved_account = scanned or expected
    session.error = ""
    return True


def _login_failure_message(output: str, code: int) -> str:
    reason = _last_cli_reason(output)
    lowered = reason.lower()
    if "scan timeout" in lowered or "please try again" in lowered:
        return "授权码已过期，请再次点击弹出扫码登录"
    if "polling error" in lowered:
        return f"授权码状态查询失败：{reason}"
    if "scan successful" in (output or "").lower() or "agent login failed" in lowered or "crypt_token" in lowered:
        detail = reason or f"退出码 {code}"
        return f"扫码成功，但换取登录凭证失败：{detail}"
    if reason:
        return reason
    return f"welink-cli auth login 退出码 {code}"


def _should_retry_login(output: str, message: str) -> bool:
    """A dead QR can be replaced. A finished scan must not be thrown away."""
    if "换取登录凭证失败" in message:
        return False
    if "scan successful" in (output or "").lower():
        return False
    return True


def _last_cli_reason(output: str) -> str:
    skip_prefixes = (
        "waiting for scan",
        "obtaining qr code",
        "welink qr code login",
        "please scan qr code",
        "if qr code is not displayed",
        "http://",
        "https://",
    )
    kept: list[str] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or any(char in line for char in "█▀▄▌▐"):
            continue
        if line[:1] in {"+", "|", "=", "-"}:
            continue
        if line.lower().startswith(skip_prefixes):
            continue
        kept.append(line)
    return kept[-1][:240] if kept else ""


LOGIN_MANAGER = WelinkLoginManager()
