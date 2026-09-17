from __future__ import annotations

from typing import Any


class AgenticToolResult(dict[str, Any]):
    """LLM-facing dict result with optional full output for frontend rendering."""

    def __init__(
        self,
        llm_payload: dict[str, Any],
        *,
        detailed_output: Any | None = None,
    ) -> None:
        super().__init__(llm_payload)
        self.detailed_output = detailed_output
