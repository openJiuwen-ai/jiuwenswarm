"""Structured audit logging in JSONL format.

Each sandbox gets its own log file under ~/.jiuwenbox/logs/.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from jiuwenbox.models.common import AuditEvent, AuditEventType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AuditLogger:
    """Append-only JSONL audit logger, one file per sandbox."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or Path.home() / ".jiuwenbox" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, event: AuditEvent) -> None:
        log_file = self.log_dir / f"{event.sandbox_id}.log"
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with open(log_file, "a") as f:
            f.write(line + "\n")
        logger.debug("Audit: %s %s", event.event_type.value, event.sandbox_id)

    def log(
        self,
        event_type: AuditEventType,
        sandbox_id: str,
        **details: object,
    ) -> None:
        """Convenience helper to create and log an event."""
        event = AuditEvent(
            event_type=event_type,
            sandbox_id=sandbox_id,
            details=details,
        )
        self.log_event(event)

    def read_logs(self, sandbox_id: str) -> list[AuditEvent]:
        """Read all audit events for a sandbox."""
        log_file = self.log_dir / f"{sandbox_id}.log"
        if not log_file.exists():
            return []
        events: list[AuditEvent] = []
        for line in log_file.read_text().splitlines():
            if line.strip():
                events.append(AuditEvent.model_validate_json(line))
        return events

    def read_logs_raw(self, sandbox_id: str) -> str:
        """Read raw log text for a sandbox."""
        log_file = self.log_dir / f"{sandbox_id}.log"
        if not log_file.exists():
            return ""
        return log_file.read_text()

    def delete_logs(self, sandbox_id: str) -> None:
        """Delete logs for a sandbox."""
        log_file = self.log_dir / f"{sandbox_id}.log"
        log_file.unlink(missing_ok=True)
