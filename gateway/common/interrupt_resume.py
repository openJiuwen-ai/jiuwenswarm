# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``chat.send`` sources that answer a paused tool call."""

INTERRUPT_RESUME_SOURCES: frozenset[str] = frozenset(
    {
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
    }
)
"""``source`` values of a ``chat.send`` that answers a paused tool call.

Such a send resumes the suspended turn instead of starting one: the
Gateway keeps the running stream, the Runtime routes it to the paused
execution, and the adapter turns ``answers`` into an ``InteractiveInput``.
The chat frontends send the ``source`` of the ``chat.ask_user_question``
event they answer.
"""
