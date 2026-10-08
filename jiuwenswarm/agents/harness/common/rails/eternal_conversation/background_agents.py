"""Isolated model runners for Extractor (background 1) and Builder (background 2)."""

from __future__ import annotations

import json
from typing import Any, Callable

from openjiuwen.core.foundation.llm.schema.message import SystemMessage, UserMessage

from .evidence import EvidenceWriter, jsonable


MAX_BACKGROUND_ATTEMPTS = 6
MAX_RETRY_RESPONSE_CHARS = 16_000

RETRY_CHECKLIST = """
Rebuild the complete JSON object from scratch; do not patch only the field named in the error.
Before returning, validate every contract rule again:
- emit exactly one strict JSON object with double-quoted keys and strings;
- JSON-escape backslashes (especially Windows paths), quotes, and control characters;
- include all six Snapshot arrays; use at most 4/4/6/4/4/6 items respectively;
- keep every Snapshot item at most 220 characters (a safety margin below the hard limit);
- use at most four changed_uts; every upsert content must be at most 650 characters;
- every changed_uts action is exactly "upsert" or "retire" (never "update");
- every upsert has 1-4 queries and 1-3 must_include strings;
- every must_include string appears verbatim in its UT content;
- priority is an integer from 0 through 100.
Do not add Markdown fences or commentary outside the JSON object.
""".strip()


def _content_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("background Agent did not return a JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("background Agent JSON must be an object")
    return value


class BackgroundAgentRunner:
    def __init__(self, model_supplier: Callable[[], Any], evidence: EvidenceWriter) -> None:
        self._model_supplier = model_supplier
        self._evidence = evidence

    def set_model_supplier(self, model_supplier: Callable[[], Any]) -> None:
        """Rebind after a channel recreates its session-scoped Adapter."""
        self._model_supplier = model_supplier

    async def call_json(
        self,
        *,
        role: str,
        system_prompt: str,
        request: dict[str, Any],
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        model = self._model_supplier()
        if model is None:
            raise RuntimeError("eternal-conversation background model is unavailable")
        original_prompt = json.dumps(request, ensure_ascii=False)
        prompt = original_prompt
        last_error: Exception | None = None
        for attempt in range(1, MAX_BACKGROUND_ATTEMPTS + 1):
            response_text: str | None = None
            usage: Any = None
            try:
                response = await model.invoke(
                    [SystemMessage(content=system_prompt), UserMessage(content=prompt)],
                    temperature=0,
                )
                response_text = _content_text(response)
                usage = jsonable(getattr(response, "usage_metadata", None))
                parsed = _json_object(response_text)
                if validate is not None:
                    validate(parsed)
                await self._evidence.append_agent_history(
                    role,
                    {
                        "attempt": attempt,
                        "system_prompt": system_prompt,
                        "request": request,
                        "response": response_text,
                        "usage": usage,
                        "status": "accepted",
                    },
                )
                return parsed
            except Exception as exc:
                last_error = exc
                await self._evidence.append_agent_history(
                    role,
                    {
                        "attempt": attempt,
                        "system_prompt": system_prompt,
                        "request": request,
                        "response": response_text,
                        "usage": usage,
                        "status": "rejected",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                prompt = (
                    original_prompt
                    + "\n\nYour previous response was invalid. Here is the exact previous response"
                    + " (possibly truncated):\n"
                    + (response_text or "<no response>")[:MAX_RETRY_RESPONSE_CHARS]
                    + "\n\nValidation error: "
                    + str(exc)
                    + "\n"
                    + RETRY_CHECKLIST
                )
        if last_error is None:
            raise RuntimeError("background Agent exhausted retries without an error")
        raise last_error


__all__ = ["BackgroundAgentRunner"]
