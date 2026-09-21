"""Shared CLI subprocess, pacing, and transient retries for user-state IM."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

LOGGER = logging.getLogger(__name__)

DEFAULT_FETCH_TIMEOUT_MS = 30_000
DEFAULT_SEND_TIMEOUT_MS = 15_000
DEFAULT_CLI_TEST_TIMEOUT_MS = 8_000
DEFAULT_MIN_INTERVAL_MS = 350
DEFAULT_MAX_INTERVAL_MS = 8_000
DEFAULT_RATE_LIMIT_RETRIES = 3
DEFAULT_BACKOFF_BASE_MS = 2_000
DEFAULT_BACKOFF_MAX_MS = 30_000

_TRANSIENT_RE = re.compile(
    r"(timeout|timed\s*out|deadline|temporar|connection\s*reset|connection\s*refused|"
    r"EOF|502|503|504|429|rate.?limit|too\s*many\s*requests|try\s*again|"
    r"i/o\s*timeout|broken\s*pipe|tls|network)",
    re.I,
)


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    stderr: str = ""
    error: Optional[str] = None


class CliRunner(Protocol):
    async def run(self, args: list[str], *, timeout_ms: int) -> CliResult:
        ...


class SubprocessCliRunner:
    def __init__(self, cli_path: str, *, cli_name: str = "cli") -> None:
        if not cli_path or not cli_path.strip():
            raise ValueError("cli_path is required")
        self._cli_path = cli_path
        self._cli_name = cli_name

    async def run(self, args: list[str], *, timeout_ms: int) -> CliResult:
        argv = [self._cli_path, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return CliResult(exit_code=-1, stdout="", stderr=str(exc), error=str(exc))
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000.0
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return CliResult(
                exit_code=-1,
                stdout="",
                stderr=f"{self._cli_name} timeout ({timeout_ms}ms)",
                error="timeout",
            )
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        exit_code = proc.returncode if proc.returncode is not None else -1
        return CliResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class ChannelCli:
    """Paced CLI client. Subclasses only declare argv for each operation."""

    CLI_NAME = "cli"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        runner: Optional[CliRunner] = None,
        fetch_timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS,
        send_timeout_ms: int = DEFAULT_SEND_TIMEOUT_MS,
        cli_test_timeout_ms: int = DEFAULT_CLI_TEST_TIMEOUT_MS,
        min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS,
        max_interval_ms: int = DEFAULT_MAX_INTERVAL_MS,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        backoff_base_ms: int = DEFAULT_BACKOFF_BASE_MS,
        backoff_max_ms: int = DEFAULT_BACKOFF_MAX_MS,
    ) -> None:
        self._cli_path = str(config.get("cli_path") or "").strip()
        self._fetch_timeout_ms = int(fetch_timeout_ms)
        self._send_timeout_ms = int(send_timeout_ms)
        self._cli_test_timeout_ms = int(cli_test_timeout_ms)
        self._min_interval_ms = max(0, int(min_interval_ms))
        self._max_interval_ms = int(max_interval_ms)
        self._rate_limit_retries = max(1, int(rate_limit_retries))
        self._backoff_base_ms = int(backoff_base_ms)
        self._backoff_max_ms = int(backoff_max_ms)
        self._pace = float(self._min_interval_ms)
        self._last_call_at = 0.0
        self._lock = asyncio.Lock()
        self._total_calls = 0
        self._total_retries = 0
        self._total_throttled = 0
        if runner is None:
            if not self._cli_path:
                raise ValueError("cli_path is required when no runner is injected")
            runner = SubprocessCliRunner(self._cli_path, cli_name=self.CLI_NAME)
        self._runner: CliRunner = runner

    def apply_config(self, config: dict[str, Any]) -> None:
        path = str(config.get("cli_path") or "").strip()
        if not path or path == self._cli_path:
            return
        self._cli_path = path
        self._runner = SubprocessCliRunner(path, cli_name=self.CLI_NAME)

    def history_args(
        self,
        *,
        target_kind: str,
        external_id: str,
        query_count: int = 50,
        message_id: Optional[str] = None,
        page_token: Optional[str] = None,
        query_direction: Optional[int] = None,
    ) -> list[str]:
        raise NotImplementedError

    def send_group_args(
        self,
        *,
        group_id: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        raise NotImplementedError

    def send_user_args(
        self,
        *,
        user_account: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        raise NotImplementedError

    def auth_status_args(self) -> list[str]:
        return ["auth", "status"]

    def search_persons_args(self, *, text: str) -> list[str]:
        raise NotImplementedError

    def self_person_args(self) -> list[str]:
        return []

    def recent_conversations_args(self, *, query_count: int) -> list[str]:
        raise NotImplementedError

    def help_args(self) -> list[str]:
        return ["--help"]

    async def _wait_pace(self) -> None:
        if self._pace <= 0:
            return
        gap_ms = (time.monotonic() - self._last_call_at) * 1000.0
        if gap_ms < self._pace:
            await asyncio.sleep((self._pace - gap_ms) / 1000.0)
        self._last_call_at = time.monotonic()

    def _is_transient(self, result: CliResult) -> bool:
        if result.exit_code == 0:
            return False
        text = f"{result.stderr} {result.stdout} {result.error or ''}"
        return bool(_TRANSIENT_RE.search(text))

    def _on_success(self) -> None:
        self._pace = max(float(self._min_interval_ms), self._pace * 0.8)

    def _on_throttled(self) -> None:
        self._pace = min(float(self._max_interval_ms), max(self._pace * 2.0, 1000.0))
        self._total_throttled += 1
        LOGGER.warning(
            "%s.rate_limited pace→%.0fms (total_throttled=%d)",
            self.CLI_NAME,
            self._pace,
            self._total_throttled,
        )

    async def run_cli(
        self,
        args: list[str],
        *,
        timeout_ms: int,
        retries: int | None = None,
    ) -> CliResult:
        max_retries = retries if retries is not None else self._rate_limit_retries
        async with self._lock:
            last_result = CliResult(exit_code=-1, stdout="", stderr="no attempt")
            for attempt in range(1, max_retries + 1):
                self._total_calls += 1
                await self._wait_pace()
                last_result = await self._runner.run(args, timeout_ms=timeout_ms)
                if last_result.exit_code == 0:
                    self._on_success()
                    return last_result
                if not self._is_transient(last_result) or attempt >= max_retries:
                    return last_result
                self._on_throttled()
                self._total_retries += 1
                backoff_ms = min(
                    self._backoff_max_ms,
                    self._backoff_base_ms * (2 ** (attempt - 1)),
                )
                LOGGER.warning(
                    "%s.retry attempt=%d/%d backoff=%dms exit=%d err=%.200s",
                    self.CLI_NAME,
                    attempt,
                    max_retries,
                    backoff_ms,
                    last_result.exit_code,
                    last_result.stderr or last_result.error,
                )
                await asyncio.sleep(backoff_ms / 1000.0)
            return last_result

    async def query_history_message(
        self,
        *,
        target_kind: str,
        external_id: str,
        query_count: int = 50,
        message_id: Optional[str] = None,
        page_token: Optional[str] = None,
        query_direction: Optional[int] = None,
    ) -> CliResult:
        return await self.run_cli(
            self.history_args(
                target_kind=target_kind,
                external_id=external_id,
                query_count=query_count,
                message_id=message_id,
                page_token=page_token,
                query_direction=query_direction,
            ),
            timeout_ms=self._fetch_timeout_ms,
        )

    async def send_to_group(
        self,
        *,
        group_id: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> CliResult:
        return await self.run_cli(
            self.send_group_args(group_id=group_id, text=text, at_open_ids=at_open_ids),
            timeout_ms=self._send_timeout_ms,
            retries=1,
        )

    async def send_to_user(
        self,
        *,
        user_account: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> CliResult:
        return await self.run_cli(
            self.send_user_args(
                user_account=user_account, text=text, at_open_ids=at_open_ids
            ),
            timeout_ms=self._send_timeout_ms,
            retries=1,
        )

    async def auth_status(self) -> CliResult:
        return await self.run_cli(self.auth_status_args(), timeout_ms=self._cli_test_timeout_ms)

    async def search_persons(self, *, text: str) -> CliResult:
        return await self.run_cli(
            self.search_persons_args(text=text),
            timeout_ms=self._cli_test_timeout_ms,
        )

    async def lookup_self_person(self) -> CliResult:
        args = self.self_person_args()
        if not args:
            return CliResult(exit_code=0, stdout="", stderr="")
        return await self.run_cli(args, timeout_ms=self._cli_test_timeout_ms)

    async def query_recent_conversations(self, *, query_count: int = 50) -> CliResult:
        return await self.run_cli(
            self.recent_conversations_args(query_count=query_count),
            timeout_ms=self._fetch_timeout_ms,
        )

    async def help(self) -> CliResult:
        return await self.run_cli(self.help_args(), timeout_ms=self._cli_test_timeout_ms)


class MockCliRunner:
    """Queue CLI results by matched command key."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self.sent_texts: list[str] = []
        self._queue: dict[str, list[CliResult]] = {}
        self._default_result = CliResult(exit_code=0, stdout="", stderr="")

    def enqueue(self, command: str, result: CliResult) -> None:
        self._queue.setdefault(command, []).append(result)

    def enqueue_history(self, stdout: str, *, exit_code: int = 0) -> None:
        self.enqueue("history", CliResult(exit_code=exit_code, stdout=stdout))

    def enqueue_send(self, *, ok: bool = True, error: str = "") -> None:
        result = CliResult(exit_code=0 if ok else 1, stdout="", stderr=error or "")
        self.enqueue("send", result)

    def enqueue_auth_status(self, stdout: str) -> None:
        self.enqueue("auth", CliResult(exit_code=0, stdout=stdout))

    def enqueue_search_persons(self, stdout: str) -> None:
        self.enqueue("search", CliResult(exit_code=0, stdout=stdout))

    def enqueue_self_person(self, stdout: str, *, exit_code: int = 0) -> None:
        self.enqueue("self_search", CliResult(exit_code=exit_code, stdout=stdout))

    def enqueue_recent_conversations(self, stdout: str, *, exit_code: int = 0) -> None:
        self.enqueue("conversations", CliResult(exit_code=exit_code, stdout=stdout))

    def enqueue_help(self, stdout: str = "cli mock") -> None:
        self.enqueue("help", CliResult(exit_code=0, stdout=stdout))

    def match_command(self, args: list[str]) -> Optional[str]:
        if not args:
            return None
        joined = " ".join(args)
        if args[0] == "--help":
            return "help"
        if "auth" in args[:2] and "status" in args:
            return "auth"
        if "get-self" in args:
            return "auth"
        if any("search-user" in token for token in args) and "--user-ids" in args:
            return "self_search"
        if any("search-user" in token or token == "search" for token in args) and (
            "person" in args or "user" in args or "--query" in args or "--text" in args or any("search-user" in token for token in args)
        ):
            return "search"
        if "query-history-message" in args or "+chat-messages-list" in args or "+chat-messages" in args:
            return "history"
        if "send-to-group" in args or "send-to-user" in args or "+messages-send" in args or "+send-to-group" in args or "+dm" in args:
            if "--text" in args or "--content" in args or "--markdown" in args:
                try:
                    flag = "--text" if "--text" in args else "--markdown" if "--markdown" in args else "--content"
                    self.sent_texts.append(args[args.index(flag) + 1])
                except (ValueError, IndexError):
                    pass
            return "send"
        if "query-recent-conversation" in args or "+chat-list" in args or "+chat-search" in args:
            return "conversations"
        if joined.strip() == "--help":
            return "help"
        return None

    async def run(self, args: list[str], *, timeout_ms: int) -> CliResult:
        self.calls.append((list(args), int(timeout_ms)))
        cmd = self.match_command(args)
        if cmd is None:
            return self._default_result
        bucket = self._queue.get(cmd)
        if not bucket:
            return self._default_result
        return bucket.pop(0)


def format_cli_error(result: CliResult, *, cli_name: str, action: str, kind: str) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        return f"{cli_name} {action}{kind}退出码 {result.exit_code}: {detail[:200]}"
    return f"{cli_name} {action}{kind}退出码 {result.exit_code}"
