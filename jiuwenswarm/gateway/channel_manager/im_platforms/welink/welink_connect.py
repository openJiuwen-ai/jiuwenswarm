# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WeLink channel: the employee account only reads shared groups; the assistant sends replies."""

from __future__ import annotations

import asyncio
import logging
import hashlib
import json
import os
import re
import time
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_link import AssistantLink, Mention
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCli
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import (
    WelinkHistoryMessage,
    WelinkMedia,
    is_image_media,
    parse_auth_status,
    contact_mail_note,
    mention_accounts_in_history,
    parse_cli_json,
    parse_history_messages,
    parse_group_members,
    reader_assistant_accounts,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import (
    MEMBERSHIP_TTL_SECONDS, CursorStore, MembershipStore, WelinkPoller,
)

logger = logging.getLogger(__name__)

WelinkInboundHandler = Callable[[dict[str, Any]], Awaitable[Any]]
_AUTH_RECHECK_SECONDS = 600


def auth_recheck_due(last_success: float, now: float, *, interval: float = _AUTH_RECHECK_SECONDS) -> bool:
    """Login status is checked after a success goes stale, and again on every failure."""
    if last_success <= 0:
        return True
    return now - last_success >= interval


@dataclass
class WelinkConfig:
    enabled: bool = False
    account: str = ""
    agent_id: str = ""
    cli_path: str = "welink-cli"
    env_name: str = "pro"
    credential_dir: str = ""
    poll_interval_seconds: float = 4
    channel_id: str = "welink"
    assistant_credential_id: str = ""
    assistant_secret: str = field(default="", repr=False)
    reader_credential_dir: str = ""
    assistant_account: str = ""
    shared_reader: bool = False
    legacy_identity_fallback: bool = False


class WelinkChannel(BaseChannel):
    name = "welink"

    def __init__(self, config: WelinkConfig, router: RobotMessageRouter) -> None:
        super().__init__(config, router)
        self.config = config
        self._delivery_lock = asyncio.Lock()
        self._journal = None
        self._preflight_retry_at = 0.0
        self._inbound_handler: WelinkInboundHandler | None = None
        self._cli = WelinkCli(config.cli_path, Path(config.reader_credential_dir or config.credential_dir), env_name=config.env_name)
        self._link: AssistantLink | None = None
        self.remember_account: Callable[[str], bool] | None = None

    @property
    def channel_id(self) -> str:
        return self.config.channel_id

    @property
    def app_id(self) -> str:
        return self.config.assistant_credential_id

    def set_inbound_handler(self, handler: WelinkInboundHandler | None) -> None:
        self._inbound_handler = handler

    def on_message(self, callback) -> None:
        """ChannelManager 注册时会挂上入站回调。群消息走独立的同事入站，不经过这条个人消息回调。"""
        self._message_callback = callback

    async def start(self) -> None:
        if not self.config.enabled or not self.config.account or not self.config.agent_id:
            logger.info("[Welink] channel disabled: account or colleague is empty")
            return
        if not self.config.credential_dir:
            logger.info("[Welink] channel disabled: credential_dir is empty")
            return
        self._running = True
        self._auth_ok_at = 0.0
        assistant_ready = bool(self.config.assistant_credential_id.strip() and self.config.assistant_secret.strip())
        logger.info(
            "[Welink] channel account=%s agent=%s assistant_reply=%s",
            self.config.account,
            self.config.agent_id,
            assistant_ready,
        )
        if assistant_ready:
            self._link = AssistantLink(
                self.config.assistant_credential_id, self.config.assistant_secret,
                on_delivery_error=self._delivery_journal().record_error,
            )
            self._link.start(asyncio.get_running_loop(), self._on_mention)
        try:
            if self.config.shared_reader:
                await asyncio.Event().wait()
            else:
                cursors = CursorStore(Path(self.config.credential_dir) / "cursors.json")
                from .welink_subscriptions import MentionSubscriptions
                subscriptions = MentionSubscriptions(
                    Path(self.config.reader_credential_dir or self.config.credential_dir) / "mention-subscriptions.sqlite3",
                    self.config.account,
                )
                await self._poll_loop(WelinkPoller(
                    self._cli, self.config.account, cursors,
                    registered_groups=lambda: subscriptions.groups({self.config.assistant_credential_id: self.config.agent_id}),
                    revoke_group=subscriptions.revoke,
                ))
        finally:
            link = self._link
            self._link = None
            if link is not None:
                await asyncio.to_thread(link.stop)

    async def _poll_loop(self, poller: WelinkPoller) -> None:
        # Do not replay discovery-era or differently owned pending messages.
        owner = json.dumps([self.config.account, self.config.agent_id, self.config.assistant_credential_id])
        pending_path = Path(self.config.credential_dir) / (
            "registered-pending-" + hashlib.sha256(owner.encode()).hexdigest()[:24] + ".json"
        )
        pending: list[WelinkHistoryMessage] = []
        if pending_path.is_file():
            for raw in json.loads(pending_path.read_text(encoding="utf-8")):
                raw["media"] = tuple(WelinkMedia(**item) for item in raw.get("media", ()))
                pending.append(WelinkHistoryMessage(**raw))
        while self._running:
            try:
                now = time.monotonic()
                if auth_recheck_due(self._auth_ok_at, now):
                    self._auth_ok_at = now if await asyncio.to_thread(self._logged_in) else 0.0
                if self._auth_ok_at > 0:
                    if not pending:
                        pending = await asyncio.to_thread(poller.poll)
                        await asyncio.to_thread(self._save_pending, pending_path, pending)
                    while pending:
                        message = pending[0]
                        if not self._running:
                            break
                        registered = getattr(poller, "_registered_groups", None)
                        if registered is not None and message.group_id not in registered():
                            pending.pop(0)
                            await asyncio.to_thread(self._save_pending, pending_path, pending)
                            continue
                        accepted = await self._deliver(message)
                        if accepted:
                            poller.commit(message.group_id, message.message_id)
                            pending.pop(0)
                            await asyncio.to_thread(self._save_pending, pending_path, pending)
                        else:
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[Welink] poll failed", exc_info=True)
            await asyncio.sleep(max(self.config.poll_interval_seconds, 1))

    @staticmethod
    def _save_pending(path: Path, messages: list[WelinkHistoryMessage]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump([asdict(message) for message in messages], stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    async def stop(self) -> None:
        self._running = False
        link = self._link
        if link is not None:
            await asyncio.to_thread(link.stop)

    async def send_group_text(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
        source_message_id: str = "",
        mention_targets: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Reply as the assistant. The logged-in employee account is never the sender."""
        for target in mention_targets or ():
            text = text.replace("[[agent:" + str(target.get("agent_id", "")) + "]]", str(target.get("display_name") or target.get("name") or target.get("agent_id") or ""))
        if not chat_id or not text or not idempotency_key:
            return {"outcome": "failed", "error_code": "INVALID_DELIVERY_ROUTE"}
        if chat_id.startswith("p2p:"):
            return await self.send_direct_text(
                chat_id=chat_id, text=text, source_message_id=source_message_id,
                idempotency_key=idempotency_key,
            )
        async with self._delivery_lock:
            journal = self._delivery_journal()
            if journal.get(idempotency_key) is not None:
                return await self.reconcile_group_text(chat_id=chat_id, idempotency_key=idempotency_key)
            if not self.config.assistant_credential_id.strip() or not self.config.assistant_secret.strip():
                return {"outcome": "failed", "error_code": "WELINK_ASSISTANT_NOT_CONFIGURED"}
            link = self._link
            if link is None:
                return {"outcome": "pending", "error_code": "WELINK_ACCOUNT_NOT_CONNECTED"}
            if time.monotonic() < self._preflight_retry_at:
                return {"outcome": "pending", "error_code": "WELINK_SEND_PREFLIGHT_BACKOFF"}
            try:
                assistants = await asyncio.to_thread(self._send_candidates, chat_id, refresh_unknown=True)
                if not assistants:
                    raise ValueError("assistant membership unavailable")
                history = await asyncio.to_thread(self._history, chat_id)
            except Exception:
                self._preflight_retry_at = time.monotonic() + 30
                return {"outcome": "pending", "error_code": "WELINK_SEND_PREFLIGHT_UNAVAILABLE"}
            numeric = [int(item.message_id) for item in history if item.message_id.isdigit()]
            baseline = str(max(numeric)) if numeric else ""
            prepared = await asyncio.to_thread(journal.prepare, idempotency_key, chat_id, text, baseline, assistants)
            if not prepared:
                if journal.get(idempotency_key) is not None:
                    return journal.outcome(idempotency_key)
                return {"outcome": "pending", "error_code": "WELINK_IDENTICAL_REPLY_UNRESOLVED"}
            try:
                await asyncio.to_thread(
                    link.send_text, chat_id, text, source_message_id,
                    on_prepared=lambda message_id, session_id, trace_id: journal.mark_wire(
                        idempotency_key, message_id, session_id, trace_id),
                )
            except Exception:
                logger.warning("[Welink] send awaiting reconciliation chat_id=%s", chat_id)
            return journal.outcome(idempotency_key)

    async def send_persistent_result(
        self, *, chat_id: str, text: str, source_message_id: str | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Deliver host notices through the assistant and preserve the actual outcome."""
        if chat_id.startswith("p2p:"):
            return await self.send_direct_text(
                chat_id=chat_id, text=text, source_message_id=source_message_id or "", keep_task_open=True,
                idempotency_key=idempotency_key,
            )
        return await self.send_group_text(
            chat_id=chat_id, text=text, source_message_id=source_message_id or "",
            idempotency_key=idempotency_key,
        )

    async def send_direct_text(self, *, chat_id: str, text: str, source_message_id: str = "",
                               idempotency_key: str = "", keep_task_open: bool = False) -> dict[str, Any]:
        """Persist private sends; a socket write alone cannot confirm delivery."""
        if not chat_id.startswith("p2p:") or not text or not idempotency_key:
            return {"outcome": "failed", "error_code": "INVALID_DELIVERY_ROUTE"}
        async with self._delivery_lock:
            journal = self._delivery_journal()
            if journal.get(idempotency_key) is not None:
                return await self.reconcile_group_text(chat_id=chat_id, idempotency_key=idempotency_key)
            link = self._link
            if link is None:
                return {"outcome": "pending", "error_code": "WELINK_ACCOUNT_NOT_CONNECTED"}
            # Private replies use an exact task slot, not text-based history matching.
            prepared = await asyncio.to_thread(
                journal.prepare, idempotency_key, chat_id, text, "", set(), serialize_identical=False,
            )
            if not prepared:
                return await self.reconcile_group_text(chat_id=chat_id, idempotency_key=idempotency_key)
            try:
                await asyncio.to_thread(
                    link.send_text, chat_id, text, source_message_id, finish_task=not keep_task_open,
                    on_prepared=lambda message_id, session_id, trace_id: journal.mark_wire(
                        idempotency_key, message_id, session_id, trace_id),
                )
            except Exception:
                logger.warning("[Welink] private send awaiting confirmation chat_id=%s", chat_id)
            return await self.reconcile_group_text(chat_id=chat_id, idempotency_key=idempotency_key)

    def _delivery_journal(self):
        from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_delivery import DeliveryJournal

        if self._journal is None:
            # Separate account and credential identities when authorization changes.
            identity = hashlib.sha256(
                f"{self.config.account}:{self.config.assistant_credential_id}".encode()
            ).hexdigest()[:24]
            self._journal = DeliveryJournal(Path(self.config.credential_dir) / f"deliveries-{identity}.sqlite3")
        return self._journal

    def _reader_assistants(self, chat_id: str, *, force_refresh: bool = False) -> set[str]:
        """Reader-owned assistant member ids currently in the group. Empty when the group is not shared."""
        membership = MembershipStore(Path(self.config.reader_credential_dir or self.config.credential_dir) / "membership.json")
        record = membership.get(chat_id)
        age = time.time() - membership.checked_at(chat_id)
        if not force_refresh and record is not None and 0 <= age < MEMBERSHIP_TTL_SECONDS:
            return set(record[1]) if record[0] else set()
        result = self._cli.query_group_members(chat_id)
        if not result.ok:
            raise ValueError("membership query unavailable")
        payload = parse_cli_json(result.stdout)
        if not isinstance(payload, (dict, list)):
            raise ValueError("invalid membership response")
        if isinstance(payload, dict) and payload.get("resultCode") not in (None, 0, "0"):
            raise ValueError("membership query rejected")
        members = parse_group_members(payload)
        if not any(member.account == self.config.account for member in members):
            return set()
        return reader_assistant_accounts(members, self.config.account)

    def _allowed_assistants(self, chat_id: str, *, force_refresh: bool = False) -> set[str]:
        return self._own_assistants(self._reader_assistants(chat_id, force_refresh=force_refresh))

    def _send_candidates(self, chat_id: str, *, refresh_unknown: bool = False) -> set[str]:
        """Accounts that may confirm this send.

        A known member id must be in the group. Before it is known, every reader-owned
        assistant in the group is a candidate; an exact outgoing message id identifies it.
        A fresh "not shared" cache still blocks reconciliation. Sending may refresh it,
        because an @ can arrive before the reader has classified the new member.
        """
        accounts = self._reader_assistants(chat_id)
        if refresh_unknown and not accounts and not self.config.assistant_account.strip():
            accounts = self._reader_assistants(chat_id, force_refresh=True)
        owned = self._own_assistants(accounts)
        if owned or self.config.assistant_account.strip():
            return owned
        return accounts

    def _remember_assistant_account(self, account: str) -> None:
        learned = account.strip()
        current = self.config.assistant_account.strip()
        if not learned or learned == current or (current and current != learned):
            return
        sink = self.remember_account
        if sink is not None and not sink(learned):
            return
        self.config.assistant_account = learned
        logger.info("[Welink] learned assistant member mapping agent=%s", self.config.agent_id)

    def _own_assistants(self, accounts: set[str]) -> set[str]:
        identity = self.config.assistant_account or self.config.assistant_credential_id
        if identity in accounts:
            return {identity}
        # Legacy standalone channels had no explicit member identity.
        if (not self.config.shared_reader or (self.config.legacy_identity_fallback
                and getattr(self, "_shared_binding_count", 0) == 1)) and len(accounts) == 1:
            return accounts
        return set()

    def _history(self, chat_id: str) -> list[WelinkHistoryMessage]:
        result = self._cli.query_history(chat_id, None)
        if not result.ok:
            raise ValueError("history unavailable")
        payload = parse_cli_json(result.stdout)
        if not isinstance(payload, (dict, list)):
            raise ValueError("invalid history response")
        if isinstance(payload, dict) and payload.get("resultCode") not in (None, 0, "0"):
            raise ValueError("history rejected")
        return parse_history_messages(payload, group_id=chat_id)

    async def reconcile_group_text(self, *, chat_id: str, idempotency_key: str) -> dict[str, Any]:
        """Read history only. A missing receipt or history gap never authorizes another send."""
        journal = self._delivery_journal()
        row = await asyncio.to_thread(journal.get, idempotency_key)
        if row is None or row["group_id"] != chat_id:
            return {"outcome": "unknown", "error_code": "WELINK_RECEIPT_UNAVAILABLE"}
        if row["status"] != "unknown":
            return journal.outcome(idempotency_key)
        if chat_id.startswith("p2p:"):
            # The group-history API cannot verify private messages. Only correlated
            # rejection is conclusive; absent an acknowledgement, do not resend.
            result = await asyncio.to_thread(journal.reconcile, idempotency_key, [])
            if result.get("outcome") == "unknown":
                result["error_code"] = "WELINK_PRIVATE_DELIVERY_UNCONFIRMED"
            return result
        try:
            if not await asyncio.to_thread(self._send_candidates, chat_id):
                return {"outcome": "unknown", "error_code": "WELINK_MEMBERSHIP_UNAVAILABLE"}
            history = await asyncio.to_thread(self._history, chat_id)
            result = await asyncio.to_thread(journal.reconcile, idempotency_key, history)
        except Exception:
            return {"outcome": "unknown", "error_code": "WELINK_HISTORY_UNAVAILABLE"}
        learned = str(result.pop("learned_sender", "") or "")
        if learned:
            self._remember_assistant_account(learned)
        return result

    def _logged_in(self) -> bool:
        try:
            status = self._cli.auth_status()
        except Exception:
            return False
        logged_in, _account = parse_auth_status(status.stdout, status.stderr)
        return bool(status.ok and logged_in)

    async def _on_mention(self, mention: Mention) -> bool:
        if not mention.direct:
            from .welink_subscriptions import MentionSubscriptions
            directory = Path(self.config.reader_credential_dir or self.config.credential_dir)
            subscriptions = MentionSubscriptions(directory / "mention-subscriptions.sqlite3", self.config.account)
            subscriptions.register(self.config.assistant_credential_id, self.config.agent_id, mention.group_id)
        message = WelinkHistoryMessage(
            message_id=mention.message_id,
            sender_id=mention.sender_id,
            sender_name=mention.sender_name,
            text=mention.text,
            group_id=mention.group_id,
            media=tuple(item for item in mention.media if isinstance(item, WelinkMedia)),
            mention_accounts=mention.mention_accounts,
        )
        return await self._deliver(message, from_mention=True, direct=mention.direct)

    async def _deliver(self, message: Any, *, from_mention: bool = False,
                       first_observation: bool = False, visible_from: str = "",
                       direct: bool = False) -> bool:
        link = self._link
        if (
            not from_mention
            and link is not None
            and link.claim_polled(message.group_id, message.message_id, message.text, sender_id=message.sender_id)
        ):
            logger.info("[Welink] skip polled mention already accepted group_id=%s", message.group_id)
            return True
        handler = self._inbound_handler
        if handler is None:
            logger.warning("[Welink] inbound handler is not wired")
            return False
        event = {
            "account": self.config.account,
            "expected_agent_id": self.config.agent_id,
            "assistant_credential_id": self.config.assistant_credential_id,
            "mentioned": from_mention,
            "first_observation": first_observation,
            "visible_from": visible_from or None,
            "message_id": (link.canonical_message_id(message.group_id, message.message_id)
                           if link is not None else message.message_id),
            "chat_id": message.group_id,
            "chat_type": "p2p" if direct else "group",
            "sender_id": message.sender_id,
            "sender_name": message.sender_name or message.sender_id,
            "received_at": getattr(message, "received_at", "") or datetime.now(timezone.utc).isoformat(),
        }
        text, local_files = self._text_with_local_media(message)
        note = await asyncio.to_thread(self._mention_mail_note, message, from_mention, direct)
        if note:
            text = f"{text}\n{note}"
        event["text"] = text
        if local_files:
            event["local_files"] = local_files
        try:
            disposition = await handler(event)
        except Exception:
            logger.warning("[Welink] inbound handler failed: message_id=%s", message.message_id, exc_info=True)
            return False
        value = getattr(disposition, "value", disposition)
        if value == "handled":
            return True
        logger.warning("[Welink] inbound rejected: message_id=%s disposition=%s", message.message_id, value)
        return value == "rejected"

    def _mention_mail_note(self, message: Any, from_mention: bool, direct: bool) -> str:
        """Resolve @ accounts to personMail. History supplies atAccountList; contact detail supplies the mailbox."""
        accounts = [item.strip() for item in getattr(message, "mention_accounts", ()) or ()
                    if isinstance(item, str) and item.strip()]
        if from_mention and not direct:
            try:
                page = self._cli.query_history(message.group_id, None)
                accounts.extend(mention_accounts_in_history(
                    parse_cli_json(getattr(page, "stdout", "") or ""), message.message_id))
            except Exception:
                logger.info("[Welink] mention account lookup unavailable group_id=%s", message.group_id)
        unique = tuple(dict.fromkeys(accounts))
        if not unique:
            return ""
        try:
            detail = self._cli.run(["contact", "detail", *unique], timeout=30)
        except Exception:
            logger.info("[Welink] contact detail unavailable")
            return ""
        return contact_mail_note(unique, parse_cli_json(getattr(detail, "stdout", "") or ""))

    def _text_with_local_media(self, message: WelinkHistoryMessage) -> tuple[str, tuple[str, ...]]:
        """Download cloud-drive shares and point the colleague at the local file."""
        notes: list[str] = []
        saved_paths: list[str] = []
        saw_image = False
        cache = self._cli.credential_dir / "incoming" / _safe_id(message.message_id)
        for item in message.media:
            if not _share_link(item.url):
                continue
            saved = _download_share(self._cli, item.url, cache, item.access_code)
            if saved is None:
                logger.info("[Welink] share file not saved group_id=%s kind=%s", message.group_id, item.kind)
                notes.append(
                    "云盘分享下载被服务端拒绝，文件没有保存到本地，看不到图片内容。"
                    "图片和附件用的是同一条分享链接，让用户改成附件再发也拿不到文件。"
                )
                continue
            logger.info("[Welink] share file saved group_id=%s kind=%s", message.group_id, item.kind)
            saved_paths.append(str(saved))
            notes.append(f"本地文件：{saved}")
            saw_image = saw_image or is_image_media(item)
        if not notes:
            return message.text, ()
        if saw_image:
            notes.append(
                "请使用 visual_question_answering，image_path_or_url 填上面的工作空间相对路径，question 填用户的问题。"
            )
        return "\n".join([message.text, *notes]).strip(), tuple(saved_paths)


def _share_link(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc.endswith("clouddrive.huawei.com") and "/f/" in parsed.path


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:80] or "message"


def _download_share(cli: WelinkCli, link: str, output_dir: Path, access_code: str = "") -> Path | None:
    before = {item.name for item in output_dir.iterdir()} if output_dir.exists() else set()
    try:
        result = cli.download_share_link(link, output_dir, access_code)
    except Exception:
        logger.info("[Welink] share download failed")
        return None
    if not result.ok:
        return None
    created = [path for path in output_dir.iterdir() if path.name not in before and path.is_file() and _usable_file(path)]
    if not created:
        created = [path for path in output_dir.iterdir() if path.is_file() and _usable_file(path)]
    return created[0] if created else None


def _usable_file(path: Path) -> bool:
    try:
        size = path.stat().st_size
        head = path.read_bytes()[:8]
    except OSError:
        return False
    if size <= 0 or head.startswith((b"{", b"<", b"<!", b"<!DOC")):
        return False
    return True
