"""Web cron notifications are separate from the Session's chat output."""

import hashlib
import json

CRON_RUN_UPDATED_EVENT = "cron.run.updated"
CRON_SESSION_STREAM_KEY = "cron_session_stream"


def cron_notification_scope(*, user_id: str = "", group_id: str = "", bot_id: str = "", app_id: str = "") -> str:
    """Build an opaque tenant/application key for online run notifications."""
    values = [str(value or "").strip() or "default" for value in (user_id, group_id, bot_id, app_id)]
    digest = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"cron-notifications-{digest}"
