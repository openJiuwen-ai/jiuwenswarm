"""Published local copies are readable; direct foreign Session reads still need consent."""

from datetime import timedelta
import hashlib
from types import SimpleNamespace

import pytest

from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing, NOW


@pytest.fixture
def artifact_host(sharing, tmp_path):
    digest = "sha256:" + hashlib.sha256(b"private file").hexdigest()
    source = dict(namespace_id="local", agent_id="agent-1", session_id="source-session",
                  source_group_id="source-group", artifact_id="art_original", artifact_kind="file",
                  title="report.txt", file_name="report.txt", file_path="sessions/source-session/output/report.txt",
                  file_ref=digest, file_size=12, created_at=(NOW - timedelta(hours=1)).isoformat(), snippet="Report")
    cached = {**source, "session_id": "session-1", "source_group_id": "target-group", "artifact_id": "art_cached",
              "file_path": "sessions/session-1/output/report.txt", "created_at": NOW.isoformat()}
    rows = (source, cached)
    sharing.sessions.session_artifact_candidates = lambda *args: rows
    sharing.sessions.search_session_artifacts = lambda ns, agent, groups, **kwargs: tuple(
        row for row in rows if row["source_group_id"] in groups)
    file = tmp_path / "report.txt"
    file.write_bytes(b"private file")
    sharing.gate._files = SimpleNamespace(resolve=lambda *args, **kwargs: file, search_text=lambda *args: "private file",
        import_approved=lambda *args: "sessions/session-1/imports/report.txt")
    sharing.delegate.sharing_sources.return_value = ({"group_id": "source-group", "name": "Source group",
        "app_id": "app-1", "visible_from": (NOW - timedelta(days=1)).isoformat()},)
    return SimpleNamespace(**vars(sharing), source=source, cached=cached)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("share_file", {"file_path": "sessions/session-1/output/report.txt"}),
    ("read_file", {"file_path": "sessions/session-1/output/report.txt"}),
    ("bash", {"command": 'python -c "print(open(\'sessions/session-1/output/report.txt\').read())"'}),
])
async def test_published_current_group_copy_does_not_need_source_approval(artifact_host, tool, args):
    from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing

    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    assert await guard.prepare(artifact_host.execution, tool, args) is None
    assert not artifact_host.repo.pending_for_request("local", "request-1")
    artifact_host.delegate.sharing_sources.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["share_file", "read_file", "bash"])
async def test_direct_foreign_session_access_still_requires_approval(artifact_host, tool):
    from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing

    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    path = artifact_host.source["file_path"]
    args = {"command": f'cat "{path}"'} if tool == "bash" else {"file_path": path}
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(artifact_host.execution, tool, args)
    row, = artifact_host.repo.pending_for_request("local", "request-1")
    assert row["source_groups"] == [{"group_id": "source-group", "name": "Source group"}]
    artifact_host.repo.bind_native("local", "request-1", "native")
    artifact_host.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1",
                             owner_principal="local", decision="once", now=NOW)
    result = await guard.prepare(artifact_host.execution, tool, args)
    assert result.success and "imports/" in str(result.data)


@pytest.mark.asyncio
async def test_cached_file_rejection_never_allows_original_read(artifact_host):
    from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing

    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    args = {"file_path": artifact_host.source["file_path"]}
    with pytest.raises(core.GroupContentApprovalRequired):
        await guard.prepare(artifact_host.execution, "share_file", args)
    row, = artifact_host.repo.pending_for_request("local", "request-1")
    artifact_host.repo.bind_native("local", "request-1", "native")
    artifact_host.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1",
                             owner_principal="local", decision="reject", now=NOW)
    result = await guard.prepare(artifact_host.execution, "share_file", args)
    assert not result.success and "private file" not in str(result.data)
    assert await guard.prepare(artifact_host.execution, "share_file", {
        "file_path": artifact_host.cached["file_path"],
    }) is None


@pytest.mark.asyncio
async def test_released_snapshot_must_match_the_requested_artifact(artifact_host):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing

    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    hit = core.GroupArtifactSearchHit(
        group_id="source-group", group_name="Source group", artifact_id="art_unapproved_version",
        artifact_kind="file", title="report.txt", file_name="report.txt", file_size=12,
        file_digest="sha256:" + "a" * 64, source_session_id="source-session",
        source_file_path="sessions/source-session/output/report.txt", created_at=NOW,
        imported_path="sessions/session-1/imports/report.txt")
    artifact_host.gate.authorize = AsyncMock(return_value=core.GroupMessageSearchResult(
        **core.InvocationRef.from_execution(artifact_host.execution).model_dump(),
        status="found", artifacts=(hit,), query_complete=True))
    result = await guard.prepare(artifact_host.execution, "share_file", {"file_path": artifact_host.source["file_path"]})
    assert result is not None and not result.success
