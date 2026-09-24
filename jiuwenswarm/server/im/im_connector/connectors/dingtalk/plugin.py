"""DingTalk ChannelPlugin backed by dws."""

from __future__ import annotations

import logging
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.backed import CliBackedConnector
from jiuwenswarm.server.im.im_connector.connectors.dingtalk.parser import (
    parse_history_messages,
    parse_history_page,
    parse_identity,
    parse_person_search_results,
    parse_recent_conversations,
)
from jiuwenswarm.server.im.im_connector.types import Identity

LOGGER = logging.getLogger(__name__)


class DingTalkConnector(CliBackedConnector):
    CHANNEL_ID = "dingtalk"
    CHANNEL_LABEL = "钉钉"
    CLI_NAME = "dws"

    def parse_history_messages(self, stdout: str) -> list[dict[str, Any]]:
        return parse_history_messages(stdout)

    def parse_history_page(self, stdout: str) -> tuple[list[dict[str, Any]], Optional[str], bool]:
        return parse_history_page(stdout)

    def mention_open_ids(self, sender_account: str) -> list[str]:
        text = (sender_account or "").strip()
        if text.startswith("DGU"):
            return [text]
        return []

    def mention_token(self, sender_account: str) -> Optional[str]:
        ids = self.mention_open_ids(sender_account)
        if not ids:
            return None
        return "".join(f"<@{item}> " for item in ids)

    def parse_person_search_results(self, stdout: str) -> list[dict[str, Any]]:
        return parse_person_search_results(stdout)

    def parse_recent_conversations(self, stdout: str) -> list[dict[str, Any]]:
        return parse_recent_conversations(stdout)

    def person_extra_key(self) -> str:
        return "open_dingtalk_id"

    async def resolve_identity(self) -> Optional[Identity]:
        auth = await self._cli.auth_status()
        if auth.exit_code != 0:
            LOGGER.warning("dws get-self failed: %s", auth.stderr)
            return None
        parsed = parse_identity(auth.stdout)
        if parsed is None:
            return None
        user_id = parsed["user_id"]
        name = parsed["name"]
        open_id = ""
        search = await self._cli.search_persons(text=user_id)
        if search.exit_code == 0:
            match = next(
                (
                    row
                    for row in parse_person_search_results(search.stdout)
                    if row.get("user_id") == user_id
                ),
                None,
            )
            if match:
                open_id = str(match.get("open_dingtalk_id") or "").strip()
        account = open_id or user_id
        self.apply_self_ids(user_id, open_id, primary=account, display_name=name)
        open_ids = []
        for item in (open_id, user_id):
            if item and item not in open_ids:
                open_ids.append(item)
        return Identity(
            account=account,
            display_name=name,
            extra={
                "user_id": user_id,
                "open_dingtalk_id": open_id or None,
                "open_ids": open_ids,
            },
        )
