"""WeLink ChannelPlugin: platform parsers + identity lookup only."""

from __future__ import annotations

import logging
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.backed import CliBackedConnector
from jiuwenswarm.server.im.im_connector.connectors.welink.parser import (
    parse_auth_status_uid,
    parse_history_messages,
    parse_person_search_results,
    parse_recent_conversations,
)
from jiuwenswarm.server.im.im_connector.reply import format_outgoing_reply_text
from jiuwenswarm.server.im.im_connector.types import Identity

LOGGER = logging.getLogger(__name__)


class WeLinkConnector(CliBackedConnector):
    CHANNEL_ID = "welink"
    CHANNEL_LABEL = "WeLink"
    CLI_NAME = "welink-cli"

    def parse_history_messages(self, stdout: str) -> list[dict[str, Any]]:
        return parse_history_messages(stdout)

    def parse_person_search_results(self, stdout: str) -> list[dict[str, Any]]:
        return parse_person_search_results(stdout)

    def parse_recent_conversations(self, stdout: str) -> list[dict[str, Any]]:
        return parse_recent_conversations(stdout)

    def person_extra_key(self) -> str:
        return "welink_id"

    async def resolve_identity(self) -> Optional[Identity]:
        auth = await self._cli.auth_status()
        if auth.exit_code != 0:
            LOGGER.warning("welink-cli auth status failed: %s", auth.stderr)
            return None
        uid = parse_auth_status_uid(auth.stdout)
        if not uid:
            return None
        search = await self._cli.search_persons(text=uid)
        if search.exit_code != 0:
            LOGGER.warning("welink-cli search person failed: %s", search.stderr)
            return Identity(account=None, display_name=uid, extra={"uid": uid})
        persons = parse_person_search_results(search.stdout)
        match = next((p for p in persons if p.get("welink_id") == uid), None)
        if match is None:
            return Identity(account=None, display_name=uid, extra={"uid": uid})
        account = (match.get("user_account") or "").strip() or None
        if account:
            self.apply_self_ids(account, uid, primary=account, display_name=match.get("name") or uid)
        return Identity(
            account=account,
            display_name=match.get("name") or uid,
            extra={
                "uid": uid,
                "welink_id": uid,
                "open_ids": [item for item in (account, uid) if item],
            },
        )


__all__ = ["WeLinkConnector", "format_outgoing_reply_text"]
