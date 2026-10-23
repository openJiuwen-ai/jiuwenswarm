"""Non-Web A2UI payload hook owned by Gateway.

Copied from ``apply_non_web_text_fallback_to_payload``.  Web keeps raw A2UI
blocks; other channels bypass A2UI, including the previous text fallback.
"""

from __future__ import annotations


def apply_non_web_text_fallback_to_payload(
    payload: dict[str, object],
    *,
    channel_id: str,
) -> dict[str, object]:
    """Retain the legacy gateway hook while keeping A2UI Web-only.

    Web payloads keep raw A2UI blocks for the frontend renderer. Non-Web
    channels bypass A2UI entirely, including the previous text fallback path.
    """
    return payload
