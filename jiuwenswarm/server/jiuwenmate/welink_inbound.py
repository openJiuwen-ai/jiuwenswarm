# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Accept one WeLink group message into the persistent colleague Request path."""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "account": self.account,
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
        )


@dataclass(frozen=True, kw_only=True)
class WelinkBinding:
    account: str
    agent_id: str
    credential_dir: str = ""
    cli_path: str = "welink-cli"
    env_name: str = "pro"
    poll_interval_seconds: float = 4
    namespace_id: str = "local"


def configured_welink_binding(config: Mapping[str, Any] | None = None) -> WelinkBinding | None:
    if config is None:
        from jiuwenswarm.common.config import get_config

        config = get_config()
    channels = config.get("channels") if isinstance(config, Mapping) else None
    welink = channels.get("welink") if isinstance(channels, Mapping) else None
    if not isinstance(welink, Mapping) or welink.get("enabled", True) is False:
        return None
    account = str(welink.get("account") or "").strip()
    agent_id = str(welink.get("persistent_agent_id") or welink.get("agent_id") or "").strip()
    if not account or not agent_id:
        return None
    interval = welink.get("poll_interval_seconds", 4)
    try:
        interval_value = float(interval)
    except (TypeError, ValueError):
        interval_value = 4
    return WelinkBinding(
        account=account,
        agent_id=agent_id,
        credential_dir=str(welink.get("credential_dir") or "").strip(),
        cli_path=str(welink.get("cli_path") or "welink-cli").strip() or "welink-cli",
        env_name=str(welink.get("env") or "pro").strip() or "pro",
        poll_interval_seconds=interval_value if interval_value > 0 else 4,
    )


def credential_dir_for(agent_id: str, configured: str = "") -> Path:
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", agent_id)[:80] or "account"
    return Path.home() / ".jiuwenswarm" / "welink" / safe


def configured_welink_agent_ids(config: Mapping[str, Any] | None = None) -> list[str]:
    binding = configured_welink_binding(config)
    return [binding.agent_id] if binding is not None else []


async def accept_welink_message(fanout: Any, event: WelinkInboundEvent) -> InboundDisposition:
    """Claim a group text message for the single bound WeLink colleague."""
    if (
        not isinstance(event, WelinkInboundEvent)
        or event.chat_type != "group"
        or not _present(event.account)
        or not _present(event.expected_agent_id)
        or not _present(event.message_id)
        or not _present(event.chat_id)
        or not _present(event.sender_id)
        or not _present(event.sender_name)
        or not _present(event.text)
        or not _valid_received_at(event.received_at)
    ):
        return InboundDisposition.REJECTED

    binding = configured_welink_binding()
    if binding is None or binding.account != event.account.strip() or binding.agent_id != event.expected_agent_id.strip():
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
        from jiuwenswarm.server.jiuwenmate.group_collaboration import waits_for_named_colleague
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

        view = await asyncio.to_thread(fanout._service.get_colleague, binding.namespace_id, binding.agent_id)
        if getattr(view, "enabled", True) is False:
            return InboundDisposition.HANDLED
        snapshot = await asyncio.to_thread(fanout._service.current_snapshot, binding.namespace_id, binding.agent_id)
        if snapshot.agent_id != binding.agent_id:
            return InboundDisposition.REJECTED

        now = fanout._clock()
        received_at = event.received_at.astimezone(timezone.utc)
        group = await asyncio.to_thread(
            fanout._groups.resolve,
            binding.namespace_id,
            "welink",
            event.chat_id,
            now=now,
        )
        await asyncio.to_thread(
            fanout._groups.observe_access,
            binding.namespace_id,
            binding.agent_id,
            group.group_chat_id,
            binding.account,
            observed_at=received_at,
            source_ref=f"welink:message:{event.message_id}",
        )
        text = await asyncio.to_thread(_text_with_workspace_media, fanout, binding, event)
        source = GroupMessage(
            message_id=event.message_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            sender_kind="human",
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
                changed_at=received_at,
            )
        except PersistentAgentError as exc:
            if getattr(exc, "code", "") == "CONFLICT":
                return InboundDisposition.HANDLED
            raise
        try:
            roster = await asyncio.to_thread(
                fanout._agent_manager.group_participant_roster,
                binding.namespace_id,
                group.group_chat_id,
            )
        except Exception:
            roster = ()
        if waits_for_named_colleague(event.text, roster, binding.agent_id):
            return InboundDisposition.HANDLED
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
                app_id=binding.account,
                target_id=event.chat_id,
                source_message_id=event.message_id,
            ),
        )
    except Exception as exc:
        logger.warning("[JiuwenMate] WeLink event rejected: error_type=%s error=%s", type(exc).__name__, exc)
        return InboundDisposition.REJECTED
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
