# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One user turn and the single way it is rendered into an agent prompt.

Single-agent and team runs share this module so a message reaches every agent
in the same envelope. ``ResponsePromptRail`` — mounted for the single agent and
for every team member alike — tells the model that a user message arrives as a
JSON envelope, so anything that hands an agent a bare string is breaking that
contract.

The split matters for teams: ``text`` keeps the user's own words, which the team
path must parse (``/debug`` directives, ``$member`` routing, slash commands),
while :meth:`UserTurn.render` produces the envelope that is actually delivered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ``inputs`` key carrying the UserTurn across the team dispatch boundary.
# Private to the adapter layer: DeepAgent's ``_normalize_inputs`` reads only
# query / conversation_id / parent_session_id / run / raw_query and ignores it.
TEAM_USER_TURN_KEY = "_user_turn"

# Channels whose turns are system-driven rather than typed by a person.
_SYSTEM_CHANNELS = frozenset({"cron", "heartbeat"})

_STATUSLINE_INSTRUCTIONS = {
    "zh": "\n\n你必须按照以下指令配置状态栏：\n",
    "en": "\n\nYou must follow these instructions to configure the status line:\n",
}


@dataclass(frozen=True)
class UserTurn:
    """A single inbound user message plus the context delivered with it.

    Attributes:
        text: The user's own words. ``str`` for ordinary turns, ``dict`` for an
            A2UI client event, or an ``InteractiveInput`` resuming an interrupt.
        channel: Originating channel id (``web`` / ``feishu`` / ``cron`` / ...).
        language: Preferred response language (``zh`` / ``en``).
        files: ``chat.send`` files mapping (``uploaded_documents`` / ``uploaded_images``).
        trusted_dirs: Directories the client declared as trusted, if any.
        skills: Skill names explicitly selected by the client, if any.
        metadata: Request metadata carrying sender / chat_type / interaction context.
    """

    text: Any
    channel: str
    language: str
    files: dict[str, Any]
    trusted_dirs: list[str] | None = None
    skills: list[str] | None = None
    metadata: dict[str, Any] | None = None

    def with_text(self, text: Any) -> "UserTurn":
        """Return a copy carrying rewritten user text, keeping all context."""
        return replace(self, text=text)

    def render(self) -> Any:
        """Render this turn into the prompt an agent receives.

        Returns:
            The JSON envelope for ordinary text, the A2UI prompt for a client
            event, or the value unchanged when it is not renderable text (an
            ``InteractiveInput`` resume carries its own structure).
        """
        from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

        a2ui_prompt = build_user_prompt_if_a2ui_event(
            self.text,
            channel=self.channel,
            language=self.language,
        )
        if a2ui_prompt is not None:
            return a2ui_prompt

        if not isinstance(self.text, (str, dict)):
            # InteractiveInput and friends resume an interrupt; they are their
            # own payload and must reach the agent untouched.
            return self.text

        content = self.text
        statusline_prompt = ""
        if isinstance(content, str):
            # /statusline <prompt> is a prompt-type command (mirrors Claude Code);
            # it never goes through /skills.
            statusline_prompt, statusline_content = _handle_statusline_prompt_command(content)
            if statusline_prompt:
                content = statusline_content

        envelope = self._build_envelope(content)
        rendered = self._interaction_prefix() + _lead_in(self.channel, self.language)
        rendered += json.dumps(envelope, ensure_ascii=False)
        if not statusline_prompt:
            return rendered

        instructions = _STATUSLINE_INSTRUCTIONS.get(self.language, _STATUSLINE_INSTRUCTIONS["en"])
        return rendered + instructions + statusline_prompt

    def _build_envelope(self, content: Any) -> dict[str, Any]:
        """Assemble the JSON envelope body for ``content``."""
        is_system = self.channel in _SYSTEM_CHANNELS
        now = datetime.now(timezone(timedelta(hours=8)))
        envelope: dict[str, Any] = {
            "source": "system" if is_system else self.channel,
            "timezone": "Asia/Shanghai",
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "preferred_response_language": self.language,
            "content": content,
            "type": self.channel if is_system else "user input",
        }
        # Scheduled and heartbeat turns carry no user upload.
        if not is_system:
            envelope["files_updated_by_user"] = json.dumps(self.files or {}, ensure_ascii=False)

        skills_to_use = self._resolve_skills(content)
        if skills_to_use:
            envelope["skills_to_use"] = skills_to_use
        if self.trusted_dirs:
            envelope["trusted_dirs"] = json.dumps(self.trusted_dirs, ensure_ascii=False)
        envelope.update(self._sender_fields())
        return envelope

    def _resolve_skills(self, content: Any) -> list[str]:
        """Resolve skill names from the explicit list or the message text.

        An explicit ``skills`` list (the Web composer extracts it from the
        message) wins. Otherwise ``/skills use`` is parsed out of the text for
        IM/CLI clients. Neither path strips the text — the names travel in
        ``skills_to_use`` and the message stays readable.
        """
        if self.skills:
            return list(self.skills)
        if not isinstance(content, str):
            return []
        parsed_skills, _stripped = _handle_skills_use_slash_command(content)
        return parsed_skills

    def _sender_fields(self) -> dict[str, str]:
        """Return sender / chat_type fields when the channel reports them."""
        if not self.metadata:
            return {}
        fields: dict[str, str] = {}
        chat_type = str(
            self.metadata.get("chat_type") or self.metadata.get("im_chat_type") or ""
        ).strip()
        if chat_type:
            fields["chat_type"] = chat_type
        sender_name = str(self.metadata.get("sender_name") or "").strip()
        if sender_name:
            fields["sender"] = sender_name
        return fields

    def _interaction_prefix(self) -> str:
        """Return the interaction-context preamble, or an empty string."""
        if not self.metadata:
            return ""
        interaction_ctx = str(self.metadata.get("interaction_context") or "").strip()
        if not interaction_ctx:
            return ""
        return f"\n{interaction_ctx}\n\n"


def _lead_in(channel: str, language: str) -> str:
    """Return the sentence introducing the envelope."""
    if language == "zh":
        if channel == "cron":
            return "你收到一条消息，对于查询类任务必须输出查询到的内容，不要只回复确认，不要记录到memory：\n"
        return "你收到一条消息：\n"
    if channel == "cron":
        return (
            "You receive a new message. For query tasks, you must output the queried content"
            "—don't just reply with confirmation, don't record to memory:\n"
        )
    return "You receive a new message:\n"


def _handle_skills_use_slash_command(content: str) -> tuple[list[str], str]:
    """Delegate to the facade parser (imported late to avoid a cycle)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _handle_skills_use_slash_command as parse_skills,
    )

    return parse_skills(content)


def _handle_statusline_prompt_command(content: str) -> tuple[str, str]:
    """Delegate to the facade parser (imported late to avoid a cycle)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _handle_statusline_prompt_command as parse_statusline,
    )

    return parse_statusline(content)
