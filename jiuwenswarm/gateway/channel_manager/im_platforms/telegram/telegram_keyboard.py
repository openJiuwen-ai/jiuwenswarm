# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inline keyboard rendering for interactive cards (E4 - Telegram).

Telegram supports real buttons through ``InlineKeyboardMarkup``, but the
adapter only ever sends plain text: an ``interactive_card`` from the E2
pipeline reaches a Telegram user as numbered-reply text even though the
platform could show the buttons natively.

Pure helpers over dictionaries, so the rendering is unit-tested without
the Telegram SDK - the adapter turns the returned rows into
``InlineKeyboardButton`` objects.
"""
from __future__ import annotations

CALLBACK_PREFIX = "jiuwen_card"
CALLBACK_LIMIT = 64
BUTTONS_PER_ROW = 2


def callback_data(index: int, action: str) -> str:
    """Build the callback payload Telegram sends back on a click.

    Telegram caps callback data at 64 bytes, so the action is truncated
    rather than silently rejected by the API.
    """
    prefix = f"{CALLBACK_PREFIX}:{int(index)}:"
    room = CALLBACK_LIMIT - len(prefix.encode("utf-8"))
    return prefix + str(action or "")[:max(room, 0)]


def render_keyboard(card_spec: dict) -> list[list[dict]]:
    """Translate an ``interactive_card`` payload into inline keyboard rows."""
    buttons = [
        {"text": str(button.get("label", "")),
         "callback_data": callback_data(index, button.get("action", ""))}
        for index, button in enumerate(card_spec.get("buttons", ()))
        if button.get("label")
    ]
    return [
        buttons[start:start + BUTTONS_PER_ROW]
        for start in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def card_text(card_spec: dict) -> str:
    """The message body that carries the buttons."""
    return str(card_spec.get("text", "")).strip()


def extract_action(raw_callback: str) -> str | None:
    """Read the card action back from a callback payload, or None."""
    data = str(raw_callback or "")
    if not data.startswith(f"{CALLBACK_PREFIX}:"):
        return None
    parts = data.split(":", 2)
    if len(parts) < 3 or not parts[2]:
        return None
    return parts[2]