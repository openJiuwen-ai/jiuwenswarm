# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Accept one WeLink group or private message into the persistent colleague Request path."""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition, _present, _valid_received_at

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WelinkInboundEvent:
    account: str
    expected_agent_id: str
    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    received_at: datetime
    chat_type: str = "group"
    local_files: tuple[str, ...] = ()
    assistant_credential_id: str = ""
    mentioned: bool = False
    sender_kind: str = "human"
    first_observation: bool = False
    visible_from: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "assistant_credential_id": self.assistant_credential_id,
            "mentioned": self.mentioned,
            "sender_kind": self.sender_kind,
            "first_observation": self.first_observation,
            "visible_from": self.visible_from.isoformat() if self.visible_from else None,
            "expected_agent_id": self.expected_agent_id,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "received_at": self.received_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WelinkInboundEvent:
        if not isinstance(payload, Mapping):
            raise ValueError("WeLink event payload must be a mapping")
        received_at = payload.get("received_at")
        if isinstance(received_at, str):
            received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if not isinstance(received_at, datetime):
            received_at = datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        visible_from = payload.get("visible_from")
        if isinstance(visible_from, str):
            visible_from = datetime.fromisoformat(visible_from.replace("Z", "+00:00"))
        if visible_from is not None and not _valid_received_at(visible_from):
            raise ValueError("invalid WeLink readable boundary")
        raw_files = payload.get("local_files") or ()
        if isinstance(raw_files, str):
            raw_files = (raw_files,)
        local_files = tuple(str(item) for item in raw_files if isinstance(item, str) and item.strip())
        return cls(
            account=str(payload.get("account") or ""),
            expected_agent_id=str(payload.get("expected_agent_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            sender_id=str(payload.get("sender_id") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            text=str(payload.get("text") or ""),
            received_at=received_at,
            chat_type=str(payload.get("chat_type") or "group"),
            local_files=local_files,
            assistant_credential_id=str(payload.get("assistant_credential_id") or ""),
            mentioned=payload.get("mentioned") is True,
            sender_kind=str(payload.get("sender_kind") or "human"),
            first_observation=payload.get("first_observation") is True,
            visible_from=visible_from,
        )


@dataclass(frozen=True, kw_only=True)
class WelinkReader:
    account: str
    reader_credential_dir: str
    cli_path: str = "welink-cli"
    env_name: str = "pro"
    poll_interval_seconds: float = 4


@dataclass(frozen=True, kw_only=True)
class WelinkBinding:
    account: str
    agent_id: str
    assistant_credential_id: str = ""
    assistant_secret: str = field(default="", repr=False)
    assistant_account: str = ""
    config_id: str = ""
    credential_dir: str = ""
    reader_credential_dir: str = ""
    cli_path: str = "welink-cli"
    env_name: str = "pro"
    poll_interval_seconds: float = 4
    namespace_id: str = "local"


def _welink_section(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import normalize_welink_section
    if config is None:
        from jiuwenswarm.common.config import get_config
        config = get_config()
    channels = config.get("channels") if isinstance(config, Mapping) else None
    section = channels.get("welink") if isinstance(channels, Mapping) else None
    return normalize_welink_section(section) if isinstance(section, Mapping) else {}


def configured_welink_reader(config: Mapping[str, Any] | None = None) -> WelinkReader | None:
    section = _welink_section(config)
    account = str(section.get("account") or "").strip()
    directory = str(section.get("reader_credential_dir") or "").strip()
    if not section.get("cli_authorized") or not account or not directory:
        return None
    try:
        interval = float(section.get("poll_interval_seconds", 4))
    except (TypeError, ValueError):
        interval = 4
    return WelinkReader(account=account, reader_credential_dir=directory,
        cli_path=str(section.get("cli_path") or "welink-cli"), env_name=str(section.get("env") or "pro"),
        poll_interval_seconds=interval if interval > 0 else 4)


def configured_welink_bindings(config: Mapping[str, Any] | None = None) -> tuple[WelinkBinding, ...]:
    section = _welink_section(config)
    reader = configured_welink_reader({"channels": {"welink": section}})
    if reader is None or section.get("enabled") is False:
        return ()
    entries = section.get("bindings") or []
    agents = [str(item.get("persistent_agent_id") or "").strip() for item in entries]
    credentials = [str(item.get("assistant_credential_id") or "").strip() for item in entries]
    result = []
    for item, agent, credential in zip(entries, agents, credentials):
        secret = str(item.get("assistant_secret") or "").strip()
        directory = str(item.get("credential_dir") or "").strip()
        if (not item.get("enabled") or not agent or not credential or not secret or not directory
                or agents.count(agent) != 1 or credentials.count(credential) != 1):
            continue
        result.append(WelinkBinding(account=reader.account, agent_id=agent,
            assistant_credential_id=credential, assistant_secret=secret,
            assistant_account=str(item.get("assistant_account") or "").strip(),
            config_id=str(item.get("config_id") or "").strip(),
            credential_dir=directory, reader_credential_dir=reader.reader_credential_dir,
            cli_path=reader.cli_path, env_name=reader.env_name,
            poll_interval_seconds=reader.poll_interval_seconds))
    return tuple(result)


def welink_binding_for_agent(agent_id: str, config: Mapping[str, Any] | None = None) -> WelinkBinding | None:
    return next((item for item in configured_welink_bindings(config) if item.agent_id == agent_id.strip()), None)


def configured_welink_binding(config: Mapping[str, Any] | None = None) -> WelinkBinding | None:
    bindings = configured_welink_bindings(config)
    return bindings[0] if len(bindings) == 1 else None


def welink_group_membership_status(access: Any, config: Mapping[str, Any] | None = None) -> str:
    """A bound colleague stays active in a group it is already receiving."""
    if getattr(access, "status", "") != "active":
        return "unknown"
    for item in configured_welink_bindings(config):
        if (item.namespace_id == getattr(access, "namespace_id", "")
                and item.agent_id == getattr(access, "agent_id", "")
                and item.account == getattr(access, "app_id", "")):
            return "active"
    return "unknown"


def welink_connection_key(account: str, agent_id: str = "") -> str:
    """The account as a connection key; login treats spellings that differ only in case as one account."""
    key = account.strip().casefold()
    return f"{key}:{agent_id.strip()}" if agent_id.strip() else key


def credential_dir_for(agent_id: str, configured: str = "") -> Path:
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", agent_id)[:80] or "account"
    return Path.home() / ".jiuwenswarm" / "welink" / safe


def configured_welink_agent_ids(config: Mapping[str, Any] | None = None) -> list[str]:
    return [binding.agent_id for binding in configured_welink_bindings(config)]


async def _accept_direct_message(fanout: Any, event: WelinkInboundEvent, binding: WelinkBinding, snapshot: Any) -> InboundDisposition:
    """Open one private colleague session. A private chat is not a group and always replies."""
    if event.sender_kind == "agent":
        return InboundDisposition.HANDLED
    try:
        import asyncio

        from openjiuwen.harness.persistent_agent import DirectMessageSource, RequestInput

        from jiuwenswarm.server.jiuwenmate.execution_models import (
            AccessSnapshot,
            ConversationRef,
            RequestRouteSnapshot,
        )
        from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_KNOWLEDGE_READ, WORKSPACE_READ

        now = fanout._clock()
        received_at = event.received_at.astimezone(timezone.utc)
        text = await asyncio.to_thread(_text_with_workspace_media, fanout, binding, event)
        scope = frozenset({"chat", WORKSPACE_READ, WORKSPACE_KNOWLEDGE_READ})
        session = await asyncio.to_thread(
            fanout._sessions.resolve_session,
            binding.namespace_id,
            binding.agent_id,
            ConversationRef.im_direct("welink", event.chat_id),
            now=now,
        )
        await fanout._agent_manager.scheduler.accept(
            session,
            snapshot=snapshot,
            source_ref=f"welink:message:{event.message_id}",
            access=AccessSnapshot(
                principal_ref=f"welink:human:{event.sender_id}",
                startup_scope=scope,
                source_scope=scope,
                policy_revision="welink-direct-v1",
            ),
            request_input=RequestInput(
                kind="direct_message",
                text=text,
                source=DirectMessageSource(
                    message_id=event.message_id,
                    sender_id=event.sender_id,
                    sender_name=event.sender_name,
                    received_at=received_at,
                ),
                reply_target=event.chat_id,
            ),
            received_at=received_at,
            route_snapshot=RequestRouteSnapshot(
                channel="welink",
                app_id=binding.assistant_credential_id,
                target_id=event.chat_id,
                source_message_id=event.message_id,
            ),
        )
    except Exception as exc:
        logger.warning("[JiuwenMate] WeLink private event rejected: error_type=%s", type(exc).__name__)
        return InboundDisposition.RETRYABLE
    return InboundDisposition.HANDLED


async def accept_welink_message(fanout: Any, event: WelinkInboundEvent) -> InboundDisposition:
    """Archive once and independently accept each bound colleague's request."""
    if (
        not isinstance(event, WelinkInboundEvent)
        or event.chat_type not in {"group", "p2p"}
        or not _present(event.account)
        or not _present(event.expected_agent_id)
        or not _present(event.message_id)
        or not _present(event.chat_id)
        or not _present(event.sender_id)
        or not _present(event.sender_name)
        or not _present(event.text)
        or event.sender_kind not in {"human", "agent"}
        or (event.visible_from is not None and not _valid_received_at(event.visible_from))
        or not _valid_received_at(event.received_at)
    ):
        return InboundDisposition.REJECTED

    binding = welink_binding_for_agent(event.expected_agent_id)
    if (binding is None or binding.account != event.account.strip()
            or binding.assistant_credential_id != event.assistant_credential_id.strip()):
        return InboundDisposition.REJECTED

    try:
        import asyncio

        from openjiuwen.harness.persistent_agent import GroupMessage, RequestInput

        from jiuwenswarm.server.jiuwenmate.execution_models import (
            AccessSnapshot,
            ConversationRef,
            RequestRouteSnapshot,
        )
        from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

        connection = await asyncio.to_thread(fanout._service.connection_of, binding.namespace_id, binding.agent_id)
        if connection is None or (connection.channel, connection.connection_key) != (
            "welink", welink_connection_key(binding.account, binding.config_id),
        ):
            return InboundDisposition.REJECTED
        view = await asyncio.to_thread(fanout._service.get_colleague, binding.namespace_id, binding.agent_id)
        if getattr(view, "enabled", True) is False:
            return InboundDisposition.HANDLED
        snapshot = await asyncio.to_thread(fanout._service.current_snapshot, binding.namespace_id, binding.agent_id)
        if snapshot.agent_id != binding.agent_id:
            return InboundDisposition.REJECTED

        if event.chat_type == "p2p":
            return await _accept_direct_message(fanout, event, binding, snapshot)

        now = fanout._clock()
        received_at = event.received_at.astimezone(timezone.utc)
        group = await asyncio.to_thread(
            fanout._groups.resolve,
            binding.namespace_id,
            "welink",
            event.chat_id,
            now=now,
        )
        text = await asyncio.to_thread(_text_with_workspace_media, fanout, binding, event)
        source = GroupMessage(
            message_id=event.message_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            sender_kind=event.sender_kind,
            received_at=received_at,
            text=text,
            mention_ids=(),
            status="normal",
        )
        request_input = RequestInput(
            kind="group_message",
            text=text,
            source=source,
            reply_target=event.chat_id,
        )
        try:
            await asyncio.to_thread(
                fanout._agent_manager.group_archive.append,
                binding.namespace_id,
                group.group_chat_id,
                source,
                source_ref=f"welink:message:{event.message_id}",
                changed_at=None,
            )
        except PersistentAgentError as exc:
            if getattr(exc, "code", "") == "CONFLICT":
                return InboundDisposition.HANDLED
            raise
        current, = await asyncio.to_thread(fanout._agent_manager.group_archive.current_messages,
            binding.namespace_id, group.group_chat_id, (event.message_id,))
        if current.status in {"recalled", "unavailable"}:
            return InboundDisposition.HANDLED
        boundary = current.received_at
        if event.first_observation and event.visible_from is not None:
            boundary = min(boundary, event.visible_from)
        await asyncio.to_thread(fanout._groups.observe_access,
            binding.namespace_id, binding.agent_id, group.group_chat_id, binding.account,
            observed_at=now, visible_from=boundary, source_ref=f"welink:message:{event.message_id}")
        access_record = await asyncio.to_thread(fanout._groups.access_for,
            binding.namespace_id, binding.agent_id, group.group_chat_id)
        if access_record.status != "active" or current.received_at < access_record.visible_from:
            return InboundDisposition.HANDLED
        source = current
        if event.mentioned and event.sender_kind == "agent":
            source = current.model_copy(update={"sender_id": event.sender_id, "sender_name": event.sender_name,
                                                "sender_kind": "agent"})
        if event.mentioned:
            source = source.model_copy(update={"mention_ids": tuple(dict.fromkeys((*current.mention_ids, binding.agent_id)))})
        else:
            if event.sender_kind == "agent":
                return InboundDisposition.HANDLED
            settings = await asyncio.to_thread(fanout._service.read_settings, binding.namespace_id, binding.agent_id)
            if getattr(settings, "group_reply_mode", "adaptive") != "adaptive":
                return InboundDisposition.HANDLED
        request_input = request_input.model_copy(update={"source": source, "text": source.text})
        session = await asyncio.to_thread(
            fanout._sessions.resolve_session,
            binding.namespace_id,
            binding.agent_id,
            ConversationRef.group(group.group_chat_id),
            now=now,
        )
        scope = frozenset({"chat", WORKSPACE_READ})
        access = AccessSnapshot(
            principal_ref=f"welink:human:{event.sender_id}",
            startup_scope=scope,
            source_scope=scope,
            policy_revision="welink-v1",
        )
        await fanout._agent_manager.scheduler.accept(
            session,
            snapshot=snapshot,
            source_ref=f"welink:message:{event.message_id}",
            access=access,
            request_input=request_input,
            received_at=received_at,
            route_snapshot=RequestRouteSnapshot(
                channel="welink",
                app_id=binding.assistant_credential_id,
                target_id=event.chat_id,
                source_message_id=event.message_id,
            ),
        )
    except Exception as exc:
        logger.warning("[JiuwenMate] WeLink event rejected: error_type=%s", type(exc).__name__)
        return InboundDisposition.RETRYABLE
    return InboundDisposition.HANDLED


def _text_with_workspace_media(fanout: Any, binding: WelinkBinding, event: WelinkInboundEvent) -> str:
    """Copy downloaded media into the office and hand the colleague a relative path."""
    if not event.local_files:
        return event.text
    try:
        office = fanout._service._workspaces.ensure(binding.namespace_id, binding.agent_id)
        placed = place_welink_media(office, event.message_id, event.local_files, event.text)
    except Exception:
        logger.info("[Welink] media was not placed in the workspace")
        return event.text
    if placed != event.text:
        logger.info("[Welink] media placed in workspace count=%s", len(event.local_files))
    return placed


def place_welink_media(
    office: Path,
    message_id: str,
    sources: tuple[str, ...],
    text: str,
    *,
    media_root: Path | None = None,
) -> str:
    """Copy WeLink incoming files into ``office/incoming`` and rewrite their paths in ``text``."""
    root = (media_root or (Path.home() / ".jiuwenswarm" / "welink")).resolve()
    office_root = office.resolve()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", message_id)[:80] or "message"
    dest_dir = (office_root / "incoming" / safe).resolve()
    if not _within(dest_dir, office_root / "incoming"):
        return text
    updated = text
    for raw in sources:
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        name = resolved.name
        if (
            not resolved.is_file()
            or not _within(resolved, root)
            or "incoming" not in resolved.parts
            or name in {"", ".", ".."}
            or any(char in name for char in ("/", "\\", ":"))
        ):
            continue
        dest = (dest_dir / name).resolve()
        if not _within(dest, dest_dir):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, dest)
        relative = f"incoming/{safe}/{name}"
        updated = updated.replace(raw, relative).replace(str(resolved), relative)
    return updated


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True
