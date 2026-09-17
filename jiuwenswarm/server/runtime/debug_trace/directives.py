# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Slash directive parsing — shared between Agent/Code and Team modes.

``strip_slash_directive`` is the generic primitive (strip a leading
``/<prefix> `` directive, requiring whitespace after the prefix). Team mode
reuses it for ``/debug`` and ``/hide_dm``; Agent/Code use the
``strip_debug_directive`` wrapper, which additionally tolerates a leading
``<system-reminder>`` block (Plan mode) and rejects a bare ``/debug`` so an
empty query never reaches the model.
"""

from __future__ import annotations

import re

DEBUG_PREFIX = "/debug"

# A leading <system-reminder>...</system-reminder> block may be prepended to
# the user query before it reaches the adapter — notably by Plan mode
# (``agent_ws_server._inject_plan_mode_activation_reminder`` prepends it to
# ``request.params["query"]`` for code.plan). The real user input follows it,
# so /debug is no longer at offset 0. Match (non-greedy, DOTALL) and skip it.
_LEADING_SYSTEM_REMINDER_RE = re.compile(
    r"^(\s*<system-reminder>.*?</system-reminder>)(.*)", re.DOTALL
)


def strip_slash_directive(query: str, prefix: str) -> tuple[str, bool]:
    """Strip a leading ``<prefix> `` directive from *query*.

    Returns ``(cleaned_query, was_present)``. Requires whitespace after the
    prefix (``/debugfoo x`` is NOT recognised for prefix ``/debug``). A bare
    prefix with no following text IS recognised and yields ``("", True)`` —
    callers that must reject a bare directive (e.g. Agent/Code ``/debug``)
    do so themselves.

    Non-str inputs are returned unchanged with ``was_present=False``.
    """
    if not isinstance(query, str):
        return query, False
    stripped = query.lstrip()
    if not stripped.startswith(prefix):
        return query, False
    remainder = stripped[len(prefix):]
    if remainder and not remainder[0].isspace():
        return query, False
    return remainder.lstrip(), True


def _split_leading_system_reminder(query: str) -> tuple[str, str]:
    """Split a leading ``<system-reminder>...</system-reminder>`` block off.

    Returns ``(prefix, body)`` where *prefix* includes the reminder (kept
    verbatim so Plan-mode guidance still reaches the model) and *body* is the
    remaining user text. If there is no leading reminder, returns ``("", query)``.
    """
    m = _LEADING_SYSTEM_REMINDER_RE.match(query)
    if m:
        return m.group(1), m.group(2)
    return "", query


def strip_debug_directive(query: str) -> tuple[str, bool]:
    """Strip a leading ``/debug `` directive from *query* (Agent/Code path).

    Unlike the bare :func:`strip_slash_directive`, this:

    * tolerates a leading ``<system-reminder>...</system-reminder>`` block
      (injected by Plan mode before the adapter sees the query) — the reminder
      is preserved and ``/debug`` is looked for in the user text after it;
    * rejects a bare ``/debug`` with no prompt, so an empty query is never sent
      to the model.

    Returns ``(cleaned_query, was_present)``.
    """
    if not isinstance(query, str):
        return query, False
    prefix, body = _split_leading_system_reminder(query)
    cleaned, present = strip_slash_directive(body, DEBUG_PREFIX)
    if not present:
        return query, False
    if not cleaned.strip():  # bare /debug — reject (Agent/Code semantics)
        return query, False
    return prefix + cleaned, True


__all__ = ["DEBUG_PREFIX", "strip_debug_directive", "strip_slash_directive"]
