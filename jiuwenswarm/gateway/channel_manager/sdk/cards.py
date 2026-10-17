# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic interactive card (E1 connector SDK).

The agent describes buttons ONCE in this neutral format; each adapter
translates it to its platform dialect (Block Kit, InlineKeyboardMarkup,
Components, Adaptive Cards). The card carries its own degradation for
buttonless channels, ready for the E2 capability-aware handler.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    """One tappable action."""

    label: str
    action: str  # opaque id returned to the agent when tapped


@dataclass(frozen=True)
class InteractiveCard:
    """Text plus an ordered list of buttons, platform-agnostic."""

    text: str
    buttons: tuple[Button, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, text: str, *buttons: Button) -> "InteractiveCard":
        return cls(text=text, buttons=tuple(buttons))

    def degrade_to_text(self) -> str:
        """Numbered-reply fallback for channels without buttons.

        Example output::

            Deploy v2.3?
            Reply 1: Approve · Reply 2: Deny
        """
        if not self.buttons:
            return self.text
        options = " · ".join(
            f"Reply {i}: {button.label}" for i, button in enumerate(self.buttons, start=1)
        )
        return f"{self.text}\n{options}"

    def action_for_reply(self, reply: str) -> str | None:
        """Map a degraded numbered reply ("1", "2", …) back to a button action."""
        reply = reply.strip()
        if not reply.isdigit():
            return None
        index = int(reply) - 1
        if 0 <= index < len(self.buttons):
            return self.buttons[index].action
        return None