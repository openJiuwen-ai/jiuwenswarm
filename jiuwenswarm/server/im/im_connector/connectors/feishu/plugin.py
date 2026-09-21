"""Feishu ChannelPlugin backed by lark-cli."""

from __future__ import annotations

import logging
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.backed import CliBackedConnector
from jiuwenswarm.server.im.im_connector.connectors.feishu.parser import (
    parse_history_messages,
    parse_history_page,
    parse_identity,
    parse_person_search_results,
    parse_recent_conversations,
    parse_self_display_name,
)
from jiuwenswarm.server.im.im_connector.types import Identity

LOGGER = logging.getLogger(__name__)


class FeishuConnector(CliBackedConnector):
    CHANNEL_ID = "feishu"
    CHANNEL_LABEL = "飞书"
    CLI_NAME = "lark-cli"

    def parse_history_messages(self, stdout: str) -> list[dict[str, Any]]:
        return parse_history_messages(stdout)

    def parse_history_page(self, stdout: str) -> tuple[list[dict[str, Any]], Optional[str], bool]:
        return parse_history_page(stdout)

    def parse_person_search_results(self, stdout: str) -> list[dict[str, Any]]:
        return parse_person_search_results(stdout)

    def parse_recent_conversations(self, stdout: str) -> list[dict[str, Any]]:
        return parse_recent_conversations(stdout)

    def mention_open_ids(self, sender_account: str) -> list[str]:
        text = (sender_account or "").strip()
        if text.startswith("ou_"):
            return [text]
        return []

    def mention_token(self, sender_account: str) -> Optional[str]:
        ids = self.mention_open_ids(sender_account)
        if not ids:
            return None
        return "".join(f'<at user_id="{item}"></at> ' for item in ids)

    def person_extra_key(self) -> str:
        return "open_id"

    async def resolve_identity(self) -> Optional[Identity]:
        auth = await self._cli.auth_status()
        if auth.exit_code != 0:
            LOGGER.warning("lark-cli auth status failed: %s", auth.stderr)
            return None
        parsed = parse_identity(auth.stdout)
        if parsed is None:
            return None
        account = parsed["user_account"]
        name = parsed["name"]
        lookup = await self._cli.lookup_self_person()
        if lookup.exit_code == 0:
            nick = parse_self_display_name(lookup.stdout, open_id=account)
            if nick:
                name = nick
        else:
            LOGGER.warning("lark-cli self person lookup failed: %s", lookup.stderr)
        self.apply_self_ids(account, parsed.get("open_id"), primary=account, display_name=name)
        return Identity(
            account=account,
            display_name=name,
            extra={"open_id": account, "open_ids": [account]},
        )
