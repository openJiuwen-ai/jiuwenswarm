"""dws (dingtalk-workspace-cli) user-state IM argv."""

from __future__ import annotations

from typing import Optional

from jiuwenswarm.server.im.im_connector.cli_runtime import ChannelCli


class DingTalkCli(ChannelCli):
    CLI_NAME = "dws"

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
        del message_id
        del query_direction
        args = ["chat", "+chat-messages", "--format", "json"]
        if target_kind == "user":
            args += ["--user", external_id or ""]
        else:
            args += ["--group", external_id or ""]
        token = (page_token or "").strip()
        if token:
            args += ["--page-token", token]
        if query_count:
            args += ["--limit", str(query_count)]
        return args

    def send_group_args(
        self,
        *,
        group_id: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        args = [
            "chat",
            "+messages-send",
            "--as",
            "user",
            "--format",
            "json",
            "--group",
            group_id,
            "--text",
            text,
            "--yes",
        ]
        ids = [item.strip() for item in (at_open_ids or []) if item and item.strip()]
        if ids:
            args += ["--at-open-dingtalk-ids", ",".join(ids)]
        return args

    def send_user_args(
        self,
        *,
        user_account: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        args = [
            "chat",
            "+messages-send",
            "--as",
            "user",
            "--format",
            "json",
            "--user",
            user_account,
            "--text",
            text,
            "--yes",
        ]
        ids = [item.strip() for item in (at_open_ids or []) if item and item.strip()]
        if ids:
            args += ["--at-open-dingtalk-ids", ",".join(ids)]
        return args

    def auth_status_args(self) -> list[str]:
        return ["contact", "user", "get-self", "--format", "json"]

    def search_persons_args(self, *, text: str) -> list[str]:
        return ["contact", "user", "search", "--query", text, "--format", "json"]

    def recent_conversations_args(self, *, query_count: int) -> list[str]:
        page_size = min(max(int(query_count or 20), 1), 100)
        return [
            "chat",
            "+chat-list",
            "--format",
            "json",
            "--page-size",
            str(page_size),
            "--types",
            "group,p2p",
            "--page-all",
            "--page-limit",
            "20",
            "--yes",
        ]
