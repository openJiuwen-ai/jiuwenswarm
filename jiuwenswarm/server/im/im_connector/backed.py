"""CLI-backed ChannelPlugin: fetch/send/test/discover share one code path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.cli_runtime import format_cli_error
from jiuwenswarm.server.im.im_connector.messages import to_im_message
from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_connector.reply import format_outgoing_reply_text
from jiuwenswarm.server.im.im_connector.types import (
    ChannelMeta,
    ChannelPerson,
    ChannelTarget,
    FetchOptions,
    ImMessage,
    MessagePage,
    ReplyContext,
    SendOptions,
    SendResult,
    TestResult,
)

LOGGER = logging.getLogger(__name__)


class CliBackedConnector(ChannelPlugin):
    CHANNEL_ID = ""
    CHANNEL_LABEL = ""
    CLI_NAME = "cli"

    def __init__(
        self,
        cli,
        *,
        config: Optional[dict] = None,
        self_account: Optional[str] = None,
    ) -> None:
        self._cli = cli
        self._config = config or {}
        account = (self_account or "").strip() or None
        self._self_account = account
        self._self_accounts: set[str] = {account} if account else set()
        self._self_display_name = None
        self._identity_lock = asyncio.Lock()
        self._identity_next_retry_mono = 0.0

    @property
    def meta(self) -> ChannelMeta:
        return ChannelMeta(
            id=self.CHANNEL_ID,
            label=self.CHANNEL_LABEL,
            target_kinds=("group", "user"),
        )

    @property
    def self_account(self) -> str:
        return self._self_account or ""

    def reply_label(self) -> str:
        name = (self._self_display_name or "").strip()
        if name:
            return name
        account = (self._self_account or "").strip()
        if not account or account.startswith("DGU") or account.isdigit():
            return ""
        return account

    def parse_history_messages(self, stdout: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def parse_person_search_results(self, stdout: str) -> list[dict[str, Any]]:
        return []

    def parse_recent_conversations(self, stdout: str) -> list[dict[str, Any]]:
        return []

    def person_extra_key(self) -> str:
        return "external_id"

    def parse_history_page(self, stdout: str) -> tuple[list[dict[str, Any]], Optional[str], bool]:
        return self.parse_history_messages(stdout), None, False

    def mention_open_ids(self, sender_account: str) -> list[str]:
        return []

    def mention_token(self, sender_account: str) -> Optional[str]:
        return None

    def apply_self_ids(
        self,
        *accounts: Optional[str],
        primary: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        ids = []
        for item in accounts:
            text = str(item).strip() if item else ""
            if text and text not in ids:
                ids.append(text)
        self._self_accounts.update(ids)
        chosen = (primary or "").strip() or (ids[0] if ids else "")
        if chosen:
            self._self_account = chosen
            self._self_accounts.add(chosen)
        name = (display_name or "").strip()
        if name:
            self._self_display_name = name

    async def fetch_messages(self, target: ChannelTarget, options: FetchOptions) -> MessagePage:
        await self._ensure_self_account()
        kind = "个人消息" if target.kind == "user" else "群消息"
        result = await self._cli.query_history_message(
            target_kind=target.kind,
            external_id=target.external_id,
            query_count=options.count,
            message_id=options.message_id,
            page_token=options.page_token,
            query_direction=options.query_direction,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                format_cli_error(result, cli_name=self.CLI_NAME, action="拉取", kind=kind)
            )
        raw_rows, next_page_token, has_more = self.parse_history_page(result.stdout)
        messages = [
            to_im_message(
                raw,
                channel_id=self.CHANNEL_ID,
                conversation_external_id=target.external_id,
                is_self_account=self._self_account,
                is_self_accounts=self._self_accounts,
            )
            for raw in raw_rows
        ]
        return MessagePage(
            messages=messages,
            next_page_token=next_page_token,
            has_more=has_more,
        )

    async def _ensure_self_account(self) -> None:
        if self._self_account:
            return
        now = asyncio.get_running_loop().time()
        async with self._identity_lock:
            if self._self_account:
                return
            if now < self._identity_next_retry_mono:
                return
            await self.resolve_identity()
            if not self._self_account:
                self._identity_next_retry_mono = asyncio.get_running_loop().time() + 60.0

    async def send_message(
        self,
        target: ChannelTarget,
        content: str,
        options: Optional[SendOptions] = None,
    ) -> SendResult:
        extra = (options.extra if options is not None else {}) or {}
        group_config = extra.get("group_config")
        group_cfg = group_config if isinstance(group_config, dict) else {}
        await self._ensure_self_account()
        if options is not None and options.mention_sender is not None:
            mention_sender = bool(options.mention_sender)
        else:
            mention_sender = bool(group_cfg.get("mention_sender", target.kind == "group"))
        try:
            max_reply_chars = int(group_cfg.get("max_reply_chars") or 1000)
        except (TypeError, ValueError):
            max_reply_chars = 1000
        source_msg = extra.get("source_message")
        sender = ""
        if isinstance(source_msg, ImMessage):
            sender = str(source_msg.sender_account or source_msg.sender_name or "")
        elif isinstance(source_msg, dict):
            sender = str(source_msg.get("sender") or source_msg.get("sender_account") or "")
        if not sender and isinstance(extra.get("sender"), str):
            sender = str(extra.get("sender") or "")
        mention_ids = self.mention_open_ids(sender) if mention_sender else []
        mention_token = self.mention_token(sender) if mention_sender else None
        reply_prefix = options.reply_prefix if options is not None else None
        text = format_outgoing_reply_text(
            content,
            account=self.reply_label(),
            mention_sender=mention_sender,
            sender=sender,
            mention_token=mention_token,
            reply_prefix=reply_prefix,
            max_reply_chars=max_reply_chars,
        )
        if not text:
            return SendResult(ok=False, error_code="empty_content", error_message="回复内容不能为空")
        if target.kind == "group":
            if not target.external_id:
                return SendResult(ok=False, error_code="missing_group_id", error_message="群 ID 缺失")
            result = await self._cli.send_to_group(
                group_id=target.external_id,
                text=text,
                at_open_ids=mention_ids,
            )
        else:
            if not target.external_id:
                return SendResult(ok=False, error_code="missing_user_account", error_message="用户账号缺失")
            result = await self._cli.send_to_user(
                user_account=target.external_id,
                text=text,
                at_open_ids=mention_ids,
            )
        if result.exit_code != 0:
            kind = "个人消息" if target.kind == "user" else "群消息"
            return SendResult(
                ok=False,
                error_code=f"exit_{result.exit_code}",
                error_message=format_cli_error(
                    result, cli_name=self.CLI_NAME, action="发送", kind=kind
                ),
            )
        return SendResult(ok=True, external_message_id=None)

    async def test_connection(self) -> TestResult:
        result = await self._cli.help()
        if result.exit_code != 0:
            return TestResult(
                ok=False,
                message=f"{self.CLI_NAME} 不可用: {result.error or result.stderr or 'unknown'}",
                error_code=f"exit_{result.exit_code}",
            )
        return TestResult(ok=True, message=f"{self.CLI_NAME} 可用")

    async def search_persons(self, text: str) -> list[ChannelPerson]:
        query = (text or "").strip()
        if not query:
            return []
        result = await self._cli.search_persons(text=query)
        if result.exit_code != 0:
            return []
        extra_key = self.person_extra_key()
        persons = []
        for raw in self.parse_person_search_results(result.stdout):
            account = str(raw.get("user_account") or "")
            persons.append(
                ChannelPerson(
                    account=account,
                    display_name=str(raw.get("name") or account or ""),
                    extra={extra_key: raw.get(extra_key)},
                )
            )
        return persons

    async def discover_conversations(self, *, query_count: int) -> list[ChannelTarget]:
        result = await self._cli.query_recent_conversations(query_count=query_count)
        if result.exit_code != 0:
            LOGGER.warning(
                "%s recent conversations failed: %s",
                self.CLI_NAME,
                result.stderr or result.error,
            )
            return []
        return [
            ChannelTarget(kind=item["kind"], external_id=item["external_id"], title=item["title"] or None)
            for item in self.parse_recent_conversations(result.stdout)
        ]

    def format_outgoing_reply(self, content: str, context: ReplyContext) -> str:
        extra = context.extra or {}
        group_config = extra.get("group_config") if isinstance(extra, dict) else {}
        group_cfg = group_config if isinstance(group_config, dict) else {}
        mention_sender = bool(group_cfg.get("mention_sender", context.target.kind == "group"))
        try:
            max_reply_chars = int(group_cfg.get("max_reply_chars") or 1000)
        except (TypeError, ValueError):
            max_reply_chars = 1000
        sender = context.source_message.sender_account or context.source_message.sender_name
        mention_token = self.mention_token(str(sender or "")) if mention_sender else None
        return format_outgoing_reply_text(
            content,
            account=self.reply_label(),
            mention_sender=mention_sender,
            sender=sender,
            mention_token=mention_token,
            max_reply_chars=max_reply_chars,
        )
