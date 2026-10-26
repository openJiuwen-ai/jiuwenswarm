"""Separate retrieval text from the application's user-message envelope."""

from __future__ import annotations

import json


# These headers are emitted by interface.build_user_prompt. Require both a
# known header and the envelope schema so ordinary user JSON is left intact.
_HEADERS = (
    "你收到一条消息：\n",
    "You receive a new message:\n",
    "你收到一条消息，对于查询类任务必须输出查询到的内容，不要只回复确认，不要记录到memory：\n",
    "You receive a new message. For query tasks, you must output the queried content"
    "—don't just reply with confirmation, don't record to memory:\n",
)


def retrieval_text(content: object) -> str:
    """Unwrap one application envelope, without modifying the model message.

    Plain text and malformed/unrecognized envelopes retain their original text.
    A valid empty content stays empty; metadata must not become a fallback query.
    Text blocks are handled individually so image payloads never enter retrieval.
    """
    if isinstance(content, list):
        return "\n".join(
            retrieval_text(part["text"])
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str):
        return ""
    envelope = application_envelope(content)
    if envelope is not None:
        query = envelope["content"]
        if isinstance(query, str):
            return query  # Never recursively interpret user-authored content.
        if isinstance(query, (dict, list)):
            return json.dumps(query, ensure_ascii=False)
        return "" if query is None else str(query)
    return content


def application_envelope(content: object) -> dict | None:
    """Read only the outer application envelope, including explicit Skill metadata."""
    if not isinstance(content, str):
        return None
    for header in _HEADERS:
        start = content.find(header)
        # An interaction-context prefix may precede the application header.
        if start < 0 or (start > 0 and content[start - 1] != "\n"):
            continue
        try:
            envelope, _ = json.JSONDecoder().raw_decode(
                content[start + len(header):].lstrip()
            )
        except (ValueError, RecursionError):
            continue
        if not isinstance(envelope, dict):
            continue
        if envelope.get("type") not in ("user input", "cron", "heartbeat") or "content" not in envelope:
            continue
        if (not isinstance(envelope.get("source"), str)
                or not isinstance(envelope.get("preferred_response_language"), str)):
            continue
        return envelope
    return None
