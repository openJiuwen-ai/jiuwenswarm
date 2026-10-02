"""Integration tests running GoogleDocsProvider against a real Google Doc.

Each case corresponds to one platform behaviour we measured and then built on. Their
purpose is not coverage but knowing immediately when the platform changes -- several of
these assertions cover behaviour the platform's own documentation does not state, and
that could only be established by measurement.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import ProviderError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_capabilities_reflect_writer_role(provider, doc_id):
    caps = await provider.capabilities(doc_id)
    assert caps.can_read
    assert caps.can_comment
    # A failure here means the service account was downgraded to commenter. The
    # revisionId then disappears, concurrency protection quietly evaporates, and the
    # admission check exists precisely to catch that.
    assert caps.can_edit, "SA 需要编辑权；仅评论权会让 revisionId 消失"
    assert caps.has_revision_control


async def test_read_returns_revision_and_consistent_segments(provider, doc_id):
    snap = await provider.read(doc_id)
    assert snap.revision_id, "有编辑权时必须返回 revisionId"
    for seg in snap.segments:
        assert seg.char_end > seg.char_start
        assert seg.index_end > seg.index_start


async def test_edit_batch_applies_multiple_edits_atomically(provider, doc_id, sandbox):
    sandbox.append("ALPHA one. BRAVO two. CHARLIE three.\n")
    snap = await provider.read(doc_id)
    result = await provider.edit_batch(
        doc_id,
        [("ALPHA", "A1"), ("CHARLIE", "C3")],
        required_revision_id=snap.revision_id,
    )
    assert result.status == "applied"
    # The response carries the new revision, so there is no need to re-read
    assert result.new_revision_id and result.new_revision_id != snap.revision_id

    after = await provider.read(doc_id)
    assert "A1 one. BRAVO two. C3 three." in after.text


async def test_edit_batch_across_surrogate_pair_does_not_shift(provider, doc_id, sandbox):
    """A surrogate pair counts as one character and two indices; get the conversion
    wrong and everything after it shifts."""
    sandbox.append("头部 中文🎯尾巴 结束标记.\n")
    snap = await provider.read(doc_id)
    result = await provider.edit_batch(
        doc_id, [("中文🎯尾巴", "CJK")], required_revision_id=snap.revision_id
    )
    assert result.status == "applied"
    after = await provider.read(doc_id)
    assert "头部 CJK 结束标记." in after.text, "emoji 之后的内容错位了"


async def test_stale_revision_returns_conflict_and_changes_nothing(provider, doc_id, sandbox):
    sandbox.append("STALE_PROBE 原样保留.\n")
    snap = await provider.read(doc_id)
    # Make a real edit first, so snap.revision_id goes stale
    await provider.edit_batch(
        doc_id, [("STALE_PROBE", "STALE_MARK")], required_revision_id=snap.revision_id
    )
    # Then submit against the stale revision
    result = await provider.edit_batch(
        doc_id, [("原样保留", "被改了")], required_revision_id=snap.revision_id
    )
    assert result.status == "conflict", "过期 revision 必须归为 conflict 而不是 invalid"
    after = await provider.read(doc_id)
    assert "原样保留" in after.text, "冲突时不得部分施加"


async def test_locate_failure_is_invalid_not_conflict(provider, doc_id):
    """A failed location and a revision conflict must stay distinct.

    Confusing the two issues a false "applied" confirmation for an edit that never
    landed, through the startup sweep's third class.
    """
    snap = await provider.read(doc_id)
    result = await provider.edit_batch(
        doc_id, [("这段文字必定不存在于文档中", "X")], required_revision_id=snap.revision_id
    )
    assert result.status == "invalid"


async def test_comment_lifecycle(provider, doc_id):
    """The whole chain: comment, reply, update the reply, close the thread."""
    _, drive = provider._clients()
    created = drive.comments().create(
        fileId=doc_id, body={"content": "[it] 生命周期宿主"}, fields="id"
    ).execute()
    cid = created["id"]
    try:
        rid = await provider.reply_comment(doc_id, cid, "⏳ 正在处理…")
        await provider.update_reply(doc_id, cid, rid, "💬 已回复")

        comments = await provider.list_comments(doc_id, include_resolved=True)
        mine = [c for c in comments if c.comment_id == cid][0]
        assert mine.author_is_self, "SA 自建的评论必须 author.me 为真"
        assert any(r.reply_id == rid and r.content == "💬 已回复" for r in mine.replies)

        # Through the API, not the provider: resolving is refused there by invariant
        # ③, and this fixture is the reader, not the agent.
        drive.replies().create(
            fileId=doc_id, commentId=cid,
            body={"content": "结帖", "action": "resolve"}, fields="id",
        ).execute()
        again = await provider.list_comments(doc_id, include_resolved=True)
        assert [c for c in again if c.comment_id == cid][0].resolved

        # A placeholder is still updatable after its host comment is resolved, which is
        # what lets the watcher write the final state once a person has accepted
        await provider.update_reply(doc_id, cid, rid, "✅ 已处理")
    finally:
        drive.comments().delete(fileId=doc_id, commentId=cid).execute()


async def test_update_reply_after_host_deleted_is_not_found(provider, doc_id):
    _, drive = provider._clients()
    created = drive.comments().create(
        fileId=doc_id, body={"content": "[it] 待删宿主"}, fields="id"
    ).execute()
    cid = created["id"]
    rid = await provider.reply_comment(doc_id, cid, "占位")
    drive.comments().delete(fileId=doc_id, commentId=cid).execute()

    with pytest.raises(ProviderError) as ei:
        await provider.update_reply(doc_id, cid, rid, "还能改吗")
    assert ei.value.kind == "not_found", "§4.3.4 据此停止重试"


async def test_list_comments_filters_resolved_client_side(provider, doc_id):
    """Drive has no resolved filter, so filtering is client-side -- and it has to
    actually filter."""
    _, drive = provider._clients()
    created = drive.comments().create(
        fileId=doc_id, body={"content": "[it] 已解决探针"}, fields="id"
    ).execute()
    cid = created["id"]
    try:
        drive.replies().create(
            fileId=doc_id, commentId=cid,
            body={"content": "结", "action": "resolve"}, fields="id",
        ).execute()
        assert cid not in {c.comment_id for c in await provider.list_comments(doc_id)}
        assert cid in {
            c.comment_id for c in await provider.list_comments(doc_id, include_resolved=True)
        }
    finally:
        drive.comments().delete(fileId=doc_id, commentId=cid).execute()


async def test_sharing_posture_is_readable(provider, doc_id):
    """The link-sharing warning depends on this; with comment-only access it returns
    403."""
    posture = await provider.sharing_posture(doc_id)
    assert posture, "至少应有 owner 一条"
    assert all(isinstance(t, str) and isinstance(r, str) for t, r in posture)


async def test_self_identity_matches_service_account(provider):
    ident = await provider.self_identity()
    assert ident.address and ident.address.endswith(".iam.gserviceaccount.com")
