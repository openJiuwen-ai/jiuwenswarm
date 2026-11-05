import json
import time

from jiuwenswarm.common import utils
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session import session_metadata as metadata
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


def test_background_writers_and_caches_keep_same_session_id_isolated(
    monkeypatch, tmp_path
):
    assert history.flush_pending_writes()
    assert metadata.flush_pending_writes()
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    sid = "shared-session-id"
    for user in ("u", "v"):
        with TenantContext.scoped(f"g::b::{user}"):
            metadata.update_session_metadata(
                session_id=sid, user_id=user, title=user, channel_id="web"
            )
            history.append_history_record(
                session_id=sid,
                request_id=f"r-{user}",
                channel_id="web",
                role="user",
                content=f"only-{user}",
                timestamp=time.time(),
            )
    # Both request scopes have ended before durability/read checks.
    assert history.flush_pending_writes()
    assert metadata.flush_pending_writes()
    for user in ("u", "v"):
        with TenantContext.scoped(f"g::b::{user}"):
            assert metadata.get_session_metadata(sid)["title"] == user
            directory = utils.get_agent_sessions_dir() / sid
            assert (
                json.loads((directory / "metadata.json").read_text())["user_id"] == user
            )
            records = [
                json.loads(line)
                for line in (directory / "history.jsonl").read_text().splitlines()
            ]
            assert [record["content"] for record in records] == [f"only-{user}"]
    assert not list(
        tmp_path.glob(
            "workspace_default*/agent/sessions/shared-session-id/history.jsonl"
        )
    )
    TenantContext.clear()
