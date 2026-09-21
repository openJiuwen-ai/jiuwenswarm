"""welink-cli argv + test mock. Pacing/retry live in ChannelCli."""

from __future__ import annotations

from typing import Optional

from jiuwenswarm.server.im.im_connector.cli_runtime import (
    DEFAULT_CLI_TEST_TIMEOUT_MS,
    DEFAULT_FETCH_TIMEOUT_MS,
    DEFAULT_SEND_TIMEOUT_MS,
    ChannelCli,
    CliResult,
    MockCliRunner,
)

class WelinkCli(ChannelCli):
    CLI_NAME = "welink-cli"

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
        del page_token
        args = ["im", "query-history-message"]
        if target_kind == "user":
            args += ["--user-account", external_id or "", "--query-count", str(query_count)]
        else:
            args += ["--group-id", external_id or "", "--query-count", str(query_count)]
        if message_id:
            args += ["--message-id", message_id]
        if query_direction is not None:
            args += ["--query-direction", str(query_direction)]
        return args

    def send_group_args(
        self,
        *,
        group_id: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        del at_open_ids
        return ["im", "send-to-group", "--group-id", group_id, "--text", text]

    def send_user_args(
        self,
        *,
        user_account: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        del at_open_ids
        return ["im", "send-to-user", "--receiver", user_account, "--text", text]

    def search_persons_args(self, *, text: str) -> list[str]:
        return ["search", "person", "--text", text]

    def recent_conversations_args(self, *, query_count: int) -> list[str]:
        return ["im", "query-recent-conversation", "--count", str(query_count)]


class MockWelinkCli:
    """Duck-typed WelinkCli for unit tests."""

    def __init__(self, *, cli_path: str = "mock-welink-cli") -> None:
        self._cli_path = cli_path
        self._runner = MockCliRunner()
        self._inner = WelinkCli({"cli_path": cli_path}, runner=self._runner)

    @property
    def cli_path(self) -> str:
        return self._cli_path

    @property
    def calls(self):
        return self._runner.calls

    @property
    def sent_texts(self):
        return self._runner.sent_texts

    def enqueue(self, command: str, result: CliResult) -> None:
        key = {
            "query-history-message": "history",
            "send-to-group": "send",
            "send-to-user": "send",
            "auth": "auth",
            "search": "search",
            "query-recent-conversation": "conversations",
            "--help": "help",
        }.get(command, command)
        self._runner.enqueue(key, result)

    def enqueue_history(self, stdout: str, *, exit_code: int = 0) -> None:
        self._runner.enqueue_history(stdout, exit_code=exit_code)

    def enqueue_send(self, *, ok: bool = True, error: str = "") -> None:
        self._runner.enqueue_send(ok=ok, error=error)

    def enqueue_auth_status(self, stdout: str) -> None:
        self._runner.enqueue_auth_status(stdout)

    def enqueue_search_persons(self, stdout: str) -> None:
        self._runner.enqueue_search_persons(stdout)

    def enqueue_recent_conversations(self, stdout: str, *, exit_code: int = 0) -> None:
        self._runner.enqueue_recent_conversations(stdout, exit_code=exit_code)

    def enqueue_help(self, stdout: str = "welink-cli mock") -> None:
        self._runner.enqueue_help(stdout)

    async def query_history_message(self, **kwargs):
        return await self._inner.query_history_message(**kwargs)

    async def send_to_group(self, **kwargs):
        return await self._inner.send_to_group(**kwargs)

    async def send_to_user(self, **kwargs):
        return await self._inner.send_to_user(**kwargs)

    async def auth_status(self):
        return await self._inner.auth_status()

    async def search_persons(self, **kwargs):
        return await self._inner.search_persons(**kwargs)

    async def query_recent_conversations(self, **kwargs):
        return await self._inner.query_recent_conversations(**kwargs)

    async def help(self):
        return await self._inner.help()

    async def run(self, args: list[str], *, timeout_ms: int) -> CliResult:
        return await self._runner.run(args, timeout_ms=timeout_ms)


__all__ = [
    "CliResult",
    "WelinkCli",
    "MockWelinkCli",
    "DEFAULT_FETCH_TIMEOUT_MS",
    "DEFAULT_SEND_TIMEOUT_MS",
    "DEFAULT_CLI_TEST_TIMEOUT_MS",
]
