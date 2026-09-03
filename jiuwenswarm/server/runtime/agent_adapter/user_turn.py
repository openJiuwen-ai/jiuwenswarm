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

# The envelope's clock. It is stated here, in the newest message at the end of
# the context, and deliberately not in the system prompt: a value that changes
# between calls at the head of the context invalidates the whole KV-cache
# prefix, so every turn would pay to re-encode the entire conversation.
_ENVELOPE_TZ_NAME = "Asia/Shanghai"
_ENVELOPE_TZ = timezone(timedelta(hours=8))


def envelope_clock_fields() -> dict[str, str]:
    """Return the ``timezone`` / ``timestamp`` pair every rendered turn carries.

    Exposed so other renderers that bypass :meth:`UserTurn._build_envelope` --
    the A2UI client-event payload is the one that does -- can state the same
    clock in the same fields rather than reaching the model with no date at any
    position.
    """
    return {
        "timezone": _ENVELOPE_TZ_NAME,
        "timestamp": datetime.now(_ENVELOPE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
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
        # Kept function-level. ``a2ui.protocol`` imports ``envelope_clock_fields``
        # from this module at import time, so hoisting this to module scope
        # closes a cycle and both modules stop importing.
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
            # own payload and must reach the agent untouched. They carry no
            # clock and are not given one: the turn being resumed already stated
            # one, moments earlier and still in context, and there is no field
            # to add one to without changing a payload the agent framework
            # matches on structurally.
            return self.text

        content = self.text
        if isinstance(content, str):
            # /statusline <prompt> is a prompt-type command (mirrors Claude Code);
            # it never goes through /skills. The rewritten content instructs
            # the parent to invoke the dedicated built-in subagent.
            statusline_dispatch, _description = _handle_statusline_prompt_command(content)
            if statusline_dispatch:
                content = statusline_dispatch

        envelope = self._build_envelope(content)
        rendered = self._interaction_prefix() + _lead_in(self.channel, self.language)
        rendered += json.dumps(envelope, ensure_ascii=False)
        return rendered

    def _build_envelope(self, content: Any) -> dict[str, Any]:
        """Assemble the JSON envelope body for ``content``."""
        is_system = self.channel in _SYSTEM_CHANNELS
        envelope: dict[str, Any] = {
            "source": "system" if is_system else self.channel,
            **envelope_clock_fields(),
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
        envelope.update(self._skill_scene_fields())
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

    def _skill_scene_fields(self) -> dict[str, str]:
        """Return create/edit skill scene fields from request metadata."""
        if not self.metadata:
            return {}
        fields: dict[str, str] = {}
        scene = str(self.metadata.get("scene") or "").strip()
        if scene:
            fields["scene"] = scene
        target_skill = str(self.metadata.get("target_skill") or "").strip()
        if target_skill:
            fields["target_skill"] = target_skill
        target_skill_type = str(self.metadata.get("target_skill_type") or "").strip()
        if target_skill_type:
            fields["target_skill_type"] = target_skill_type
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
