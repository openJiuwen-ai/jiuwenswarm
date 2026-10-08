# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process A2A-style collaboration for Designer specialist roles.

OpenJiuwen's Gateway ``A2AChannel`` is inbound HTTP (external client → chat
agent). Outbound A2A is not wired. Designer nodes need peer talk without a
chat session or ``A2A_SERVER_ENABLED``.

This module keeps the A2A message shape (``message_id`` / ``context_id`` /
``task_id`` / ``parts``) and runs specialist personas over
``complete_designer_text``. The executor calls it before a wave that contains
two or more collaboratable roles (character + scene). Handlers then read the
aligned cards instead of each inventing the world from Brief alone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    DesignerExecutionGraph,
    DesignerExecutionRun,
    node_config,
    node_pipeline,
)
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

COLLAB_ROLES: frozenset[str] = frozenset(
    {
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_SCENE,
    }
)

_SPECIALISTS: dict[str, tuple[str, str]] = {
    NODE_ROLE_CHARACTER_DESIGN: (
        "character designer",
        "You are a film character designer. Write look, costume, materials, posture, and identifying traits only. "
        "Do not write a storyboard. Do not plan shots. Short English Markdown.",
    ),
    NODE_ROLE_SCENE: (
        "production designer",
        "You are a production designer. Write space, weather, lighting, signage, ground, and props only. "
        "No people. Do not write a storyboard. Short English Markdown.",
    ),
    NODE_ROLE_STORYBOARD: (
        "storyboard director",
        "You are a storyboard director. Keep character consistency and scene geography. "
        "Shots must cover the duration stated in the Brief or user request.",
    ),
}


@dataclass
class DesignerA2APart:
    text: str


@dataclass
class DesignerA2AMessage:
    """Subset of A2A SendMessage used inside Designer."""

    message_id: str
    context_id: str
    task_id: str
    sender: str
    recipient: str
    parts: list[DesignerA2APart] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if part.text).strip()


class DesignerA2ABus:
    """In-process mailbox. One bus per run (``context_id`` = ``run_id``)."""

    def __init__(self, context_id: str) -> None:
        self.context_id = context_id
        self.messages: list[DesignerA2AMessage] = []

    def record(self, message: DesignerA2AMessage) -> DesignerA2AMessage:
        self.messages.append(message)
        return message

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        text: str,
        task_id: str,
    ) -> DesignerA2AMessage:
        return self.record(
            DesignerA2AMessage(
                message_id=f"a2a_{uuid4().hex[:12]}",
                context_id=self.context_id,
                task_id=task_id,
                sender=sender,
                recipient=recipient,
                parts=[DesignerA2APart(text=text)],
            )
        )

    def transcript_markdown(self) -> str:
        lines = ["# Designer A2A transcript", ""]
        for item in self.messages:
            lines.append(f"## {item.sender} → {item.recipient}")
            lines.append("")
            lines.append(item.text() or "_(empty)_")
            lines.append("")
        return "\n".join(lines).strip() + "\n"


def collaboration_card_path(run_id: str, role: str) -> Path:
    return get_agent_workspace_dir() / f"designer_a2a_{run_id}_{role}.md"


def collaboration_transcript_path(run_id: str) -> Path:
    return get_agent_workspace_dir() / f"designer_a2a_{run_id}_transcript.md"


def collaboration_card(run_id: str, role: str) -> str:
    path = collaboration_card_path(run_id, role)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path.resolve()


async def _complete(prompt: str, *, max_tokens: int = 8192) -> str:
    from jiuwenswarm.server.runtime.designer.handlers.common import complete_designer_text

    try:
        text = await complete_designer_text(prompt, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.debug("designer A2A complete failed: %s", exc)
        return ""
    return str(text or "").strip()


async def ask_specialist(role: str, task: str, *, max_tokens: int = 8192) -> str:
    name, system = _SPECIALISTS.get(role, (role, "You are a Designer specialist."))
    return await _complete(
        f"{system}\n\nYour identity: {name} (role={role}).\n\n{task}\n",
        max_tokens=max_tokens,
    )


def _brief_text(graph: DesignerExecutionGraph, run: DesignerExecutionRun) -> str:
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

    states = run.get("node_states") or {}
    for node in graph.get("nodes") or []:
        if node_pipeline(node) != NODE_ROLE_BRIEF:
            continue
        ref = (states.get(node["id"]) or {}).get("output_ref") or {}
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is not None and path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                text = ""
            if text:
                return text
        prompt = str((node.get("config") or {}).get("prompt") or "").strip()
        if prompt:
            return prompt
    return str(graph.get("description") or "").strip()


def _collab_roles_for_wave(
    graph: DesignerExecutionGraph,
    ready_ids: Iterable[str],
) -> list[str]:
    ready = set(ready_ids)
    roles: list[str] = []
    for node in graph.get("nodes") or []:
        if node["id"] not in ready:
            continue
        role = node_pipeline(node)
        if role not in COLLAB_ROLES:
            continue
        if node_config(node).get("collaborate") is False:
            continue
        if role not in roles:
            roles.append(role)
    return roles


async def align_specialists(
    brief: str,
    roles: list[str],
    *,
    run_id: str,
) -> dict[str, str]:
    """Draft in parallel, then one A2A cross-talk round per pair."""
    bus = DesignerA2ABus(context_id=run_id)
    task_id = f"align_{run_id}"
    brief = brief.strip() or "No Brief provided"
    drafts: dict[str, str] = {}

    async def _draft(role: str) -> tuple[str, str]:
        text = await ask_specialist(
            role,
            "Write an executable design card from the Brief below, 10-20 lines.\n\nBrief:\n" + brief,
        )
        bus.send(sender=role, recipient="director", text=text or f"({role} draft empty)", task_id=task_id)
        return role, text

    for role, text in await asyncio.gather(*(_draft(role) for role in roles)):
        drafts[role] = text

    for sender, recipient in combinations(roles, 2):
        for src, dst in ((sender, recipient), (recipient, sender)):
            peer = drafts.get(src) or ""
            mine = drafts.get(dst) or ""
            reply = await ask_specialist(
                dst,
                "A colleague sent this design over A2A. List conflicts with your card and 3-6 constraints you must keep. "
                "Do not rewrite the whole Brief.\n\n"
                f"Colleague ({src}) card:\n{peer}\n\nYour card:\n{mine}\n",
                max_tokens=8192,
            )
            bus.send(sender=src, recipient=dst, text=peer, task_id=task_id)
            bus.send(sender=dst, recipient=src, text=reply or "no conflict", task_id=task_id)
            if reply:
                drafts[dst] = (
                    f"{mine}\n\n## Constraints after aligning with {src}\n\n{reply}".strip()
                    if mine
                    else reply
                )

    for role, text in drafts.items():
        if text:
            _write_text(collaboration_card_path(run_id, role), text)
    _write_text(collaboration_transcript_path(run_id), bus.transcript_markdown())
    return {role: text for role, text in drafts.items() if text}


async def collaborate_ready_wave(
    graph: DesignerExecutionGraph,
    run: DesignerExecutionRun,
    ready_ids: list[str],
) -> dict[str, str]:
    """Align specialist peers before a parallel wave generates media."""
    roles = _collab_roles_for_wave(graph, ready_ids)
    if len(roles) < 2:
        return {}
    run_id = str(run.get("run_id") or "")
    if not run_id:
        return {}
    existing = {role: collaboration_card(run_id, role) for role in roles}
    if all(existing.values()):
        return existing
    brief = _brief_text(graph, run)
    try:
        return await align_specialists(brief, roles, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("designer A2A collaboration skipped: %s", exc)
        return {}


async def review_storyboard_with_peers(
    table: str, *, run_id: str, brief: str = ""
) -> str:
    """Let character / scene agents comment on a drafted storyboard table."""
    if not table.strip():
        return table
    character = collaboration_card(run_id, NODE_ROLE_CHARACTER_DESIGN)
    scene = collaboration_card(run_id, NODE_ROLE_SCENE)
    if not character and not scene:
        return table
    notes: list[str] = []
    if character:
        reply = await ask_specialist(
            NODE_ROLE_CHARACTER_DESIGN,
            "Review the Character action column of this storyboard. Flag costume changes, swapped people, or look mismatches. "
            "Reply OK if none.\n\n"
            f"Your character specs:\n{character}\n\nStoryboard:\n{table}\n",
            max_tokens=4096,
        )
        if reply and reply.strip().upper() != "OK":
            notes.append("character designer:\n" + reply)
    if scene:
        reply = await ask_specialist(
            NODE_ROLE_SCENE,
            "Review the Scene change column of this storyboard. Flag location changes, weather swaps, or broken lighting. "
            "Reply OK if none.\n\n"
            f"Your scene card:\n{scene}\n\nStoryboard:\n{table}\n",
            max_tokens=4096,
        )
        if reply and reply.strip().upper() != "OK":
            notes.append("production designer:\n" + reply)
    if not notes:
        return table
    brief_block = (brief or "").strip()
    brief_section = f"Brief (keep this world; do not replace it):\n{brief_block}\n\n" if brief_block else ""
    revised = await ask_specialist(
        NODE_ROLE_STORYBOARD,
        "Revise the storyboard from colleague A2A notes. Keep this header: "
        "Shot | Timeline | Camera | Move | Character action | Scene change | Comment.\n"
        "Every row must still match the Brief. Output the Markdown table only, no explanation.\n\n"
        f"{brief_section}"
        f"Original:\n{table}\n\nNotes:\n" + "\n\n".join(notes),
        max_tokens=16384,
    )
    return revised or table
