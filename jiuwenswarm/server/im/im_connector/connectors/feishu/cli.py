"""lark-cli user-state IM argv (always --as user)."""

from __future__ import annotations

from typing import Optional

from jiuwenswarm.server.im.im_connector.cli_runtime import ChannelCli


class FeishuCli(ChannelCli):
    CLI_NAME = "lark-cli"

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
        args = [
            "im",
            "+chat-messages-list",
            "--as",
            "user",
            "--format",
            "json",
            "--page-size",
            str(min(max(int(query_count or 50), 1), 50)),
        ]
        if target_kind == "user":
            args += ["--user-id", external_id or ""]
        else:
            args += ["--chat-id", external_id or ""]
        token = (page_token or "").strip()
        if token:
            args += ["--page-token", token]
        return args

    def send_group_args(
        self,
        *,
        group_id: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        args = [
            "im",
            "+messages-send",
            "--as",
            "user",
            "--format",
            "json",
            "--chat-id",
            group_id,
        ]
        ids = [item.strip() for item in (at_open_ids or []) if item and item.strip()]
        if ids:
            args += ["--markdown", text]
        else:
            args += ["--text", text]
        return args

    def send_user_args(
        self,
        *,
        user_account: str,
        text: str,
        at_open_ids: Optional[list[str]] = None,
    ) -> list[str]:
        args = [
            "im",
            "+messages-send",
            "--as",
            "user",
            "--format",
            "json",
            "--user-id",
            user_account,
        ]
        ids = [item.strip() for item in (at_open_ids or []) if item and item.strip()]
        if ids:
            args += ["--markdown", text]
        else:
            args += ["--text", text]
        return args

    def auth_status_args(self) -> list[str]:
        return ["auth", "status", "--json", "--verify"]

    def search_persons_args(self, *, text: str) -> list[str]:
        return ["contact", "+search-user", "--as", "user", "--format", "json", "--query", text]

    def self_person_args(self) -> list[str]:
        return [
            "contact",
            "+search-user",
            "--as",
            "user",
            "--format",
            "json",
            "--user-ids",
            "me",
            "--lang",
            "zh_cn",
        ]

    def recent_conversations_args(self, *, query_count: int) -> list[str]:
        return [
            "im",
            "+chat-list",
            "--as",
            "user",
            "--format",
            "json",
            "--types",
            "p2p,group",
            "--sort",
            "active_time",
            "--page-size",
            str(min(max(int(query_count or 20), 1), 100)),
        ]
