"""Offline tests for the clouddoc toolkit.

Runs against a fake provider and never touches the network.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
    UNATTENDED_ALLOWLIST,
    UNATTENDED_DENYLIST,
    CloudDocToolkit as _UnreadCloudDocToolkit,
)
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocCapabilities,
    DocComment,
    DocSnapshot,
    DocSummary,
    EditResult,
    ProviderError,
)

DOCID = "1AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"


class CloudDocToolkit(_UnreadCloudDocToolkit):
    """Test shim: the legacy suite stipulates the read-before-write ledger is
    satisfied, so it can keep exercising the behaviours downstream of the gate.
    The gate's own tests use ``_UnreadCloudDocToolkit`` and earn their entry."""

    class _EveryDoc(set):
        def __contains__(self, item):  # noqa: D401
            return True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_docs = self._EveryDoc()


DOC = "1AAAAAAAAAAAAAAAAAAAAA"
OTHER = "1BBBBBBBBBBBBBBBBBBBBB"


class FakeProvider:
    """Implements only the parts the toolkit uses."""

    def doc_url(self, doc_id, kind=""):
        # The link handed to a person comes from the provider; the toolkit used to
        # build a Google-document URL whatever the platform and format were.
        return f"https://example.test/{kind or 'doc'}/{doc_id}"

    def __init__(self) -> None:
        self.replies: list[tuple[str, str, str]] = []
        self.raise_on_reply: ProviderError | None = None
        self.edits: list = []

    @property
    def kind(self) -> str:
        return "fake"

    async def list_accessible_documents(self):
        return list(getattr(self, "accessible", []))

    def parse_doc_ref(self, url_or_id: str) -> str:
        s = (url_or_id or "").strip()
        if "/document/d/" in s:
            return s.split("/document/d/")[1].split("/")[0]
        if len(s) >= 10 and " " not in s:
            return s
        raise ProviderError("invalid", f"无法解析 {url_or_id!r}")

    async def read(self, doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="rev1", text="hello world")

    async def list_comments(self, doc_ref, *, include_resolved=False):
        return []

    async def edit(self, doc_ref, old_string, new_string, *, revision_id=None):
        return EditResult("applied", new_revision_id="rev2")

    async def reply_comment(self, doc_ref, comment_id, content):
        if self.raise_on_reply:
            raise self.raise_on_reply
        self.replies.append((doc_ref, comment_id, content))
        return "reply-1"

    async def resolve_comment(self, doc_ref, comment_id, content=None):
        return None

    async def title(self, doc_ref):
        return {DOC: "Rollout Plan", OTHER: "Q3 Launch Note"}.get(doc_ref, "")

    async def create_document(self, title):
        if getattr(self, "raise_on_create", None):
            raise self.raise_on_create
        self.created = getattr(self, "created", []) + [title]
        return "doc-CREATED-000000000000"

    async def share_document(self, doc_ref, email, *, role="writer"):
        if email in getattr(self, "share_fails", ()):  # noqa: SIM108
            raise ProviderError("invalid", f"bad address {email}")
        self.shares = getattr(self, "shares", []) + [(doc_ref, email, role)]

    async def edit_batch(self, doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        self.edits.append((doc_ref, list(pairs)))
        self.highlights = getattr(self, "highlights", []) + [highlight]
        return EditResult("applied", new_revision_id="rev2")


@pytest.fixture
def fake():
    return FakeProvider()


# ------------------------------------------------------------ doc_id authorization


@pytest.mark.asyncio
async def test_chat_path_imposes_no_doc_constraint(fake):
    """The chat path has no per-turn authorization scope, so any document is reachable and
    the standard ask flow applies."""
    kit = CloudDocToolkit(fake)  # turn_doc_id 默认返回 None
    assert (await kit.read(DOC))["ok"] is True
    assert (await kit.read(OTHER))["ok"] is True


@pytest.mark.asyncio
async def test_unattended_path_uses_the_bound_document_whatever_was_passed(fake):
    """The bound document wins outright; a mismatched argument is overridden, not refused.

    Refusing was the earlier rule and it killed the turns it was meant to protect. The id
    is 44 opaque characters the model cannot know, the generated schema marks the
    parameter required, and a model with nothing to put there invents one -- measured:
    two consecutive turns sent fabricated ids while the gateway had bound the real one,
    each was refused, each refusal said only that something was unauthorized, and both
    turns ended having written nothing.

    Overriding is **stricter** than refusing: the operation reaches OTHER under neither
    rule, and under this one being wrong is no longer fatal."""
    kit = CloudDocToolkit(fake, turn_doc_id=lambda: DOC)
    assert (await kit.read(DOC))["ok"] is True
    out = await kit.read(OTHER)
    assert out["ok"] is True
    assert out["doc_id"] == DOC, "必须读绑定的那篇，而不是模型点名的那篇"


@pytest.mark.asyncio
async def test_url_form_is_normalized_before_comparison(fake):
    """The comparison is against the normalized value.

    Comparing the raw argument would fail to match every time a user does the ordinary
    thing and pastes a link in chat.
    """
    kit = CloudDocToolkit(fake, turn_doc_id=lambda: DOC)
    url = f"https://docs.google.com/document/d/{DOC}/edit#heading=h.x"
    assert (await kit.read(url))["ok"] is True


@pytest.mark.asyncio
async def test_refusal_returns_never_raises(fake):
    """A refusal has to be a return value.

    Raising becomes a Tool execution error and trips ToolCallResilienceRail, dressing up
    a deterministic safety refusal as a transient fault and getting it retried.
    """
    # A clouddoc session whose authorization payload never arrived: bound is the empty
    # string, which means "no authorization" rather than "no constraint", and this is the
    # case that must still fail closed.
    kit = CloudDocToolkit(fake, turn_doc_id=lambda: "")
    out = await kit.read(OTHER)  # 不应抛
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_provider_error_becomes_structured_failure(fake):
    fake.raise_on_reply = ProviderError("not_found", "Comment not found: x")
    kit = CloudDocToolkit(fake, turn_doc_id=lambda: DOC)
    out = await kit.reply_comment(DOC, "c1", "hi")
    assert out["ok"] is False and "not_found" in out["detail"]


def test_tool_cards_match_the_settled_contract(fake):
    tools = CloudDocToolkit(fake).get_tools()
    by_name = {t.card.name: t for t in tools}

    # The allowlist and denylist **cover all tools exactly** -- no third category.
    # Resolving went away because closing a comment is the commenter's acceptance, not
    # the agent's to give; the single-edit tool went away because the batch tool does
    # the same job atomically with one confirmation. PR2a adds the two workmode tools
    # (deny-listed: conventions cannot rewrite conventions), and D15 adds the region
    # write (deny-listed too: the unattended path's bound is the range a comment
    # anchored, and whether a spreadsheet comment yields a region is still open):
    # 8 document-operation tools + 2 workmode tools, and the lifecycle increment adds
    # share and trash (deny-listed: a comment must not hand out access or make a
    # document disappear): 12. Then add_page makes 13 -- deny-listed for a reason
    # of its own: a comment authorizes the passage it marks, and a page that does not
    # exist yet is outside every passage. It exists because its absence was reaching
    # the platform anyway: unable to create a worksheet, the model made one with the
    # platform's CLI through a shell and filled it with no receipt (2026-09-10).
    assert set(by_name) == UNATTENDED_ALLOWLIST | UNATTENDED_DENYLIST
    assert len(tools) == 13

    # Writing tools must run serially: concurrent writes to one document shift each
    # other's indices
    for name in ("clouddoc_batch_edit", "clouddoc_write_region",
                 "clouddoc_apply_for_comment", "clouddoc_reply_comment"):
        assert by_name[name].card.parallel_safe is False, name

    # Reading tools may run in parallel
    for name in ("clouddoc_read", "clouddoc_list_comments"):
        assert by_name[name].card.parallel_safe is True, name

    # The parameter name is the safety contract: authorization compares by name.
    # clouddoc_list_documents is the one tool that names no document -- it is how the
    # chat path finds out which documents exist, so it cannot take one as input.
    # clouddoc_create_document names no document either: it brings one into existence.
    for name in (UNATTENDED_ALLOWLIST | UNATTENDED_DENYLIST) - {
        "clouddoc_list_documents",
        "clouddoc_create_document",
        # Deployment-level tools: workmode is per-deployment style, no document param.
        "clouddoc_workmode_get",
        "clouddoc_workmode_edit",
    }:
        props = by_name[name].card.input_params["properties"]
        assert "doc_id" in props, f"{name} 必须以 doc_id 为参数名，不能是 doc"

    # The timeout must be set explicitly and stay far below the turn timeout
    for t in tools:
        assert t.card.properties["resilience"]["timeout_s"] == 60


def test_allowlist_and_denylist_are_disjoint_and_literal():
    """The allowlist has to be a literal list.

    An early version wrote it as set subtraction -- the clouddoc tools minus workmode_*
    -- which let clouddoc_edit and clouddoc_resolve_comment back in.
    """
    assert not (UNATTENDED_ALLOWLIST & UNATTENDED_DENYLIST)
    assert "clouddoc_edit" not in UNATTENDED_ALLOWLIST
    assert "clouddoc_resolve_comment" not in UNATTENDED_ALLOWLIST


# ------------------------------------------------------------ argument fallback and comment binding
#
# Measured in a live environment: without the fallback the model replies asking for the
# document ID and stops. It has no way to know that 44-character string, and the prompt
# should not carry it either.


@pytest.mark.asyncio
async def test_missing_doc_id_falls_back_to_the_bound_document():
    tk = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.read(doc_id=None)
    assert out["ok"] and out["doc_id"] == "doc-AAAAAAAAAA"


@pytest.mark.asyncio
async def test_missing_doc_id_still_fails_on_the_chat_path():
    """The chat path has nothing bound, so a missing argument stays missing -- no document
    gets picked quietly."""
    tk = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None)
    out = await tk.read(doc_id=None)
    assert not out["ok"] and "doc_id" in out["detail"]


@pytest.mark.asyncio
async def test_a_wrong_explicit_doc_id_is_overridden_not_obeyed():
    """The one thing that must never happen is reading the document that was named."""
    tk = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.read(doc_id="doc-BBBBBBBBBB")
    assert out["ok"] and out["doc_id"] == "doc-AAAAAAAAAA"


@pytest.mark.asyncio
async def test_posting_is_confined_to_the_triggering_comment():
    """Without binding comment_id, an unattended agent could post a proposal under any
    comment in the document."""
    prov = FakeProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA", turn_comment_id=lambda: "c1")
    out = await tk.reply_comment(comment_id="c999", content="混进别人的线程")
    assert out["ok"]
    assert prov.replies and prov.replies[0][1] == "c1", "必须落在触发本轮的那条评论下"


@pytest.mark.asyncio
async def test_missing_comment_id_falls_back_to_the_bound_comment():
    prov = FakeProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA", turn_comment_id=lambda: "c1")
    out = await tk.reply_comment(content="正常回复")
    assert out["ok"]
    assert prov.replies and prov.replies[0][1] == "c1"


# ------------------------------------------------------------ the pre-post range check
#
# What happened: the model answered a 13-character quote by proposing a rewrite of an
# entire table, the tool posted it, a person read it and approved, and only then came
# "outside the allowed range". They spent a read and a decision on a proposal that could
# never have landed, and the model never got the feedback that would have let it propose
# something smaller in the same turn.


class _RangeProvider(FakeProvider):
    """Carries a body and one quoted comment, enough to drive the real range rail."""

    BODY = "填充。" * 40 + "这一句需要被改写。" + "尾巴。" * 40

    async def read(self, doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="r1", text=self.BODY)

    async def list_comments(self, doc_ref, *, include_resolved=False):
        return [DocComment(
            comment_id="c1", author_is_self=False, author_display_name="X",
            created_time="2026-01-01T00:00:00.000Z", content="改这句",
            quoted_text="这一句需要被改写。", resolved=False,
        )]





class _BatchProvider(FakeProvider):
    """Enough of a document to drive batch_edit: a body, comments, and a record of what
    was submitted."""

    def __init__(self, body="正文第一句。正文第二句。", comments=()):
        super().__init__()
        self.body = body
        self._comments = list(comments)
        self.submitted: list = []
        self.result = EditResult("applied", new_revision_id="r2")

    async def read(self, doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="r1",
                           text=self.body)

    async def list_comments(self, doc_ref, *, include_resolved=False):
        return [c for c in self._comments if include_resolved or not c.resolved]

    async def edit_batch(self, doc_ref, edits, *, required_revision_id, window=None, highlight=False):
        self.submitted.append((list(edits), required_revision_id))
        return self.result


def _kit(provider, address="co-scribe@x.iam.gserviceaccount.com", *, read=True):
    cls = CloudDocToolkit if read else _UnreadCloudDocToolkit
    return cls(provider, turn_address=lambda: address)


@pytest.mark.asyncio
async def test_batch_edit_submits_every_change_in_one_call():
    """Ten changes were ten confirmations and, on the fifth failure, four changes already
    in the document. One submission is the point of this tool."""
    prov = _BatchProvider()
    out = await _kit(prov).batch_edit(
        doc_id=DOCID,
        edits=[{"old_string": "第一句", "new_string": "改后一"},
               {"old_string": "第二句", "new_string": "改后二"}],
    )
    assert out["ok"], out
    assert len(prov.submitted) == 1, "必须是一次提交"
    assert len(prov.submitted[0][0]) == 2


@pytest.mark.asyncio
async def test_batch_edit_refuses_structural_markdown():
    """The body is plain text, so ``## Heading`` lands as those characters -- the same
    shape as the bold incident, on the path that has no range rail to catch it."""
    prov = _BatchProvider(body="")
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"new_string": "## 调研背景\n正文"}])
    assert not out["ok"]
    assert "记号" in out["detail"]
    assert prov.submitted == [], "拒绝时不得写入"


@pytest.mark.asyncio
async def test_batch_edit_allows_an_incidental_asterisk():
    """Only structural markers are refused. A report that mentions ``*args`` is ordinary;
    refusing every asterisk would make the tool unusable for technical prose."""
    prov = _BatchProvider(body="")
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"new_string": "函数签名里用 *args 收集位置参数。"}])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_preserving_markup_the_document_already_has_is_allowed():
    """The test is on what the edit **adds**, not on what the text contains.

    An absolute test refused any rewrite that merely carried existing markers along --
    and the live test document holds ``**Agent Identity and Workspace**``, so rewriting
    that sentence was impossible until this was narrowed to an increase.
    """
    prov = _BatchProvider(body="1. **Agent Identity**: 每个 agent 都有独立身份。")
    out = await _kit(prov).batch_edit(doc_id=DOCID, edits=[{
        "old_string": "每个 agent 都有独立身份。",
        "new_string": "每个 agent 都有自己的身份与工作区。",
    }])
    assert out["ok"], out

    # carrying the markers through a rewrite of the whole line is fine too
    out = await _kit(prov).batch_edit(doc_id=DOCID, edits=[{
        "old_string": "1. **Agent Identity**: 每个 agent 都有独立身份。",
        "new_string": "1. **Agent Identity**: 每个 agent 拥有独立身份。",
    }])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_batch_edit_writes_into_an_empty_document():
    """A first draft has nothing to anchor to; without this the whole chain stops at the
    step where the document is still empty."""
    prov = _BatchProvider(body="")
    out = await _kit(prov).batch_edit(doc_id=DOCID, edits=[{"new_string": "初稿正文。"}])
    assert out["ok"], out
    assert prov.submitted[0][0] == [("", "初稿正文。")]


@pytest.mark.asyncio
async def test_anchorless_write_is_refused_when_the_document_has_content():
    """Otherwise a replace-what-you-can-locate tool quietly becomes write-anywhere."""
    prov = _BatchProvider(body="已有正文。")
    out = await _kit(prov).batch_edit(doc_id=DOCID, edits=[{"new_string": "追加"}])
    assert not out["ok"]
    assert "空文档" in out["detail"]
    assert prov.submitted == []


@pytest.mark.asyncio
async def test_batch_edit_stands_down_while_the_document_agent_has_work():
    """Not because anchors would break -- that is defined behaviour -- but because two
    writers on one document produce work that is thrown away. The mutex presupposes a
    collision is possible, i.e. the unattended path is live for this document."""
    me = "co-scribe@x.iam.gserviceaccount.com"
    summoned = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="改这句", quoted_text="q", resolved=False,
        mentioned_addresses=(me,),
    )
    prov = _BatchProvider(comments=[summoned])
    kit = _kit(prov, me)

    async def live(_doc):
        return True

    kit._unattended_live = live
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"]
    assert "尚未处理的任务" in out["detail"]
    assert prov.submitted == []


@pytest.mark.asyncio
async def test_mutex_stands_aside_when_no_watch_can_dispatch():
    """The deadlock this fixes: watch off means the assignment can never run, so
    blocking chat on it left the document uneditable by anyone but hand. No live
    watch, no collision -- the write proceeds (D21 companion fix)."""
    me = "co-scribe@x.iam.gserviceaccount.com"
    assigned = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="改这句", quoted_text="q", resolved=False,
        assignee_address=me,
    )
    prov = _BatchProvider(comments=[assigned])
    kit = _kit(prov, me)

    async def not_live(_doc):
        return False

    kit._unattended_live = not_live
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out
    assert prov.submitted, "无活 watch 时聊天写入应放行"


@pytest.mark.asyncio
async def test_work_assigned_to_another_agent_does_not_stand_us_down():
    """A document may hold several agents. Another one's queue is not our business."""
    theirs = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="给别人的", quoted_text="q", resolved=False,
        assignee_address="other@y.iam.gserviceaccount.com",
    )
    prov = _BatchProvider(comments=[theirs])
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_conflict_says_nothing_landed():
    """Atomicity is only worth having if the person is told they have it -- otherwise
    they go and check whether the document was left half-rewritten."""
    prov = _BatchProvider()
    prov.result = EditResult("conflict", detail="stale revision")
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"]
    assert "本批未做任何修改" in out["detail"]


@pytest.mark.asyncio
async def test_replying_to_a_resolved_comment_is_refused():
    """Measured: Drive stamps such a reply with action=reopen and the thread comes back.
    Someone closed it on purpose."""
    done = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="旧线程", quoted_text="q", resolved=True,
    )
    prov = _BatchProvider(comments=[done])
    out = await _kit(prov).reply_comment(doc_id=DOCID, comment_id="c1", content="补一句")
    assert not out["ok"]
    assert "重新打开" in out["detail"]


def test_no_dangling_workmode_references_remain():
    """Configurable work conventions were moved out of this change, onto agent-core's
    WorkspaceNode mechanism shared with team mode.

    What happened: one string replacement during the removal **missed silently**, leaving
    a dangling reference to ``self._read_clouddoc_workmode``. It is evaluated while the
    tool set is being built, so **every agent session** -- not just clouddoc, the code
    adapter too -- failed on an AttributeError. Another defect happened to mask that
    exception as "cannot access local variable", and the real cause stayed unknowable for
    two full rounds.
    """
    # PR2a inverted this guard's duty: workmode **landed** (D4), so the danger is no
    # longer a dangling reference to a removed feature but an incomplete wiring of a
    # present one. The original incident (a silent missed replacement producing an
    # AttributeError at tool-build time in every session) is still what we guard:
    # every workmode symbol referenced must resolve.
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_ALLOWLIST, UNATTENDED_DENYLIST, CloudDocToolkit as _Kit,
    )

    assert {"clouddoc_workmode_get", "clouddoc_workmode_edit"} <= UNATTENDED_DENYLIST
    assert not any("workmode" in n for n in UNATTENDED_ALLOWLIST)
    assert callable(getattr(_Kit, "workmode_get"))
    assert callable(getattr(_Kit, "workmode_edit"))

    from jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode import (  # noqa: F401
        edit_workmode, load_workmode, resolve_workmode_path,
    )



@pytest.mark.asyncio
async def test_chat_can_find_out_which_documents_it_watches(fake):
    """Without this the chat path is stuck: it has no doc_id and no way to get one.

    Observed on the first live chat run -- asked to write into "the shared doc", the
    agent stopped and asked the user to paste a link, while the Docs panel was already
    listing that document.
    """
    fake.accessible = [DocSummary(doc_id=DOC, title="Onboarding Rollout Plan", can_edit=True)]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC])

    out = await tk.list_documents()
    assert out["ok"]
    assert out["documents"] == [
        {"doc_id": DOC, "title": "Onboarding Rollout Plan", "user_named": False, "platform": "fake"}
    ]


@pytest.mark.asyncio
async def test_listing_survives_a_provider_that_cannot_supply_titles(fake):
    """Titles are a convenience; the ids are the part the agent actually needs. A
    listing that fails degrades to one title lookup per document, not to nothing."""
    async def boom():
        raise ProviderError("forbidden", "Drive API has not been enabled")

    fake.list_accessible_documents = boom
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC])

    out = await tk.list_documents()
    assert out["ok"]
    assert [d["doc_id"] for d in out["documents"]] == [DOC]
    assert out["documents"][0]["title"] == await fake.title(DOC)


@pytest.mark.asyncio
async def test_an_unattended_turn_never_sees_the_document_list(fake):
    """A comment-triggered turn is scoped to the document that triggered it.

    Handing it the whole list would widen what it can reach past the thing it was
    asked about, which is the opposite of what the scope is for.
    """
    assert "clouddoc_list_documents" in UNATTENDED_DENYLIST
    assert "clouddoc_list_documents" not in UNATTENDED_ALLOWLIST


def test_every_clouddoc_tool_has_a_display_name():
    """The chat shows this name on every tool call, so a missing one leaks the raw id.

    It drifted once already: clouddoc_edit and clouddoc_resolve_comment kept their
    names here for a while after both tools were removed, and clouddoc_batch_edit --
    the tool that actually replaced them -- had none.
    """
    from jiuwenswarm.common.tool_display import _VERB_BY_TOOL

    actual = UNATTENDED_ALLOWLIST | UNATTENDED_DENYLIST
    mapped = {k for k in _VERB_BY_TOOL if k.startswith("clouddoc_")}
    assert actual - mapped == set(), f"这些工具在聊天里会显示原始名：{sorted(actual - mapped)}"
    assert mapped - actual == set(), f"映射表里有已删除的工具：{sorted(mapped - actual)}"


@pytest.mark.asyncio
async def test_several_documents_come_back_titled_so_the_ask_can_be_specific(fake):
    """Titles are what make disambiguation possible at all.

    With ids alone the agent can only offer two 44-character strings, and a user who
    cannot tell them apart will guess -- which is the outcome the titles exist to
    prevent. Choosing wrongly writes into a real shared document with no undo.
    """
    fake.accessible = [
        DocSummary(doc_id=DOC, title="Onboarding Rollout Plan", can_edit=True),
        DocSummary(doc_id=OTHER, title="Q3 Launch Note", can_edit=True),
    ]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER])

    out = await tk.list_documents()
    assert [d["title"] for d in out["documents"]] == [
        "Onboarding Rollout Plan",
        "Q3 Launch Note",
    ]


def test_the_tool_tells_the_model_not_to_guess():
    """The rule lives in the description, so the description is what has to carry it.

    "有多篇要问清楚" alone reads as a preference; the model needs to be told that
    picking one is wrong, and why.
    """
    tools = {t.card.name: t for t in CloudDocToolkit(FakeProvider()).get_tools()}
    text = tools["clouddoc_list_documents"].card.description
    assert "必须停下来问" in text
    assert "没有撤销" in text


@pytest.mark.asyncio
async def test_the_list_says_so_when_it_covers_only_one_of_several_connections(fake):
    """One toolkit holds one provider, so the list is per-connection, not per-deployment.

    Left unsaid, a one-document answer sends the model down the "只有一篇就直接用" fast
    path -- and if the document the user meant belongs to the *other* connection, that
    path writes into the wrong shared document without ever asking.
    """
    fake.accessible = [DocSummary(doc_id=DOC, title="Onboarding Rollout Plan", can_edit=True)]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], connection_count=lambda: 2)

    out = await tk.list_documents()
    assert out["ok"]
    assert "只覆盖当前连接" in out["detail"]
    assert "先问清楚" in out["detail"]


@pytest.mark.asyncio
async def test_a_single_connection_gets_no_such_warning(fake):
    """The usual deployment has one connection; a caveat there is noise that trains the
    model to ask when it does not need to."""
    fake.accessible = [DocSummary(doc_id=DOC, title="Onboarding Rollout Plan", can_edit=True)]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], connection_count=lambda: 1)

    assert (await tk.list_documents())["detail"] == ""


def test_every_clouddoc_tool_is_registered_in_the_permission_table():
    """An unlisted tool is not "unconstrained" -- the permission layer falls to
    ``level is None`` and resolves that to **deny**.

    So a tool missing here works fine until a deployment sets permissions.enabled and
    then fails closed, silently. clouddoc_list_documents was missing exactly this way:
    with permissions on, the agent could not even find out which documents it watches
    and went back to asking the user for a link.
    """
    import re
    from pathlib import Path

    import jiuwenswarm

    cfg = (Path(jiuwenswarm.__file__).parent / "resources" / "config.yaml").read_text("utf-8")
    listed = set(re.findall(r"^\s+(clouddoc_\w+): ", cfg, re.M))
    actual = UNATTENDED_ALLOWLIST | UNATTENDED_DENYLIST
    assert actual - listed == set(), f"权限表漏登记，开启权限后会被判 deny：{sorted(actual - listed)}"
    assert listed - actual == set(), f"权限表里有已不存在的工具：{sorted(listed - actual)}"


# ------------------------------------------- multi-document ambiguity rail (writes)

@pytest.mark.asyncio
async def test_a_write_is_refused_when_several_docs_and_the_user_named_none(fake):
    """The failure this rail exists for, reproduced.

    Two documents managed; the user said only "the shared doc". A 26B model picked the
    one whose *title sounded closer to the request* -- exactly the "by relevance" choice
    the tool description forbids in bold -- and then reported success. The prompt-level
    rule did not hold, so the rule is code now, on the write path, and it fails closed.
    """
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "In the shared doc, draft how we ship the new onboarding flow.",
    )

    out = await tk.batch_edit(doc_id=OTHER, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert out["ok"] is False
    assert out["reason"] == "ambiguous_document"
    assert "Rollout Plan" in out["candidates"] and "Q3 Launch Note" in out["candidates"]
    assert "没有撤销" in out["detail"]


@pytest.mark.asyncio
async def test_naming_the_document_lets_the_write_through(fake):
    """Naming it is the whole point -- a rail that also blocks correct input gets
    switched off. Match is normalized, so case and punctuation do not matter."""
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "put the draft in 「q3 launch NOTE」 please",
    )

    out = await tk.batch_edit(doc_id=OTHER, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert out["ok"] is True, out


@pytest.mark.asyncio
async def test_a_pasted_link_counts_as_naming_it(fake):
    """People paste links far more often than they retype titles."""
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: f"write it into https://docs.google.com/document/d/{DOC}/edit",
    )

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert out["ok"] is True, out


@pytest.mark.asyncio
async def test_one_document_imposes_nothing(fake):
    """With a single managed document there is nothing to confuse it with, and "the
    shared doc" has always been allowed to stand for it."""
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "update the shared doc")

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert out["ok"] is True, out


@pytest.mark.asyncio
async def test_an_unattended_turn_is_exempt(fake):
    """**The watcher path must not be caught by this.** Its document is bound by the
    gateway, so the target was never the model's to choose, and there is no user text at
    all -- applying the rail there would refuse every comment-triggered job."""
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "",
        turn_doc_id=lambda: DOC,
    )

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert out["ok"] is True, out


@pytest.mark.asyncio
async def test_the_refusal_tells_the_model_how_to_recover(fake):
    """The model has to fix this inside the same turn. The refusal names the candidates
    and says to ask -- and the user's answer, being user text, satisfies the check on
    the retry."""
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "写点东西")

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "HELLO"}])
    assert "先问用户" in out["detail"]
    assert "不要替用户挑一篇" in out["detail"]


@pytest.mark.asyncio
async def test_reading_is_gated_too_not_only_writing(fake):
    """The incident never reached a write.

    The model picked the wrong document, **read** it, found content that happened to
    match the request, and reported "I have drafted the plan there" -- with batch_edit
    never called and both documents untouched. A belief formed about the wrong document
    becomes a false report, so reading is gated on the same evidence as writing.
    """
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "看看共享文档")

    for out in (await tk.read(doc_id=OTHER), await tk.list_comments(doc_id=OTHER)):
        assert out["ok"] is False
        assert out["reason"] == "ambiguous_document"


@pytest.mark.asyncio
async def test_listing_documents_is_never_gated(fake):
    """It is the way out of the ambiguity, so gating it would deadlock the turn: the
    model could not discover the titles it needs in order to ask."""
    fake.accessible = [
        DocSummary(doc_id=DOC, title="Rollout Plan", can_edit=True),
        DocSummary(doc_id=OTHER, title="Q3 Launch Note", can_edit=True),
    ]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "共享文档")

    out = await tk.list_documents()
    assert out["ok"] is True
    assert len(out["documents"]) == 2


@pytest.mark.asyncio
async def test_the_rail_sits_at_the_chokepoint_so_new_tools_inherit_it(fake):
    """Every document-taking tool resolves through _resolve, so the rail cannot be
    bypassed by adding a tool and forgetting to guard it."""
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "那篇文档")

    canonical, err = await tk._resolve(DOC)
    assert canonical is None and err["reason"] == "ambiguous_document"


# ---------------------------------------------------- the UI receipt for a write

def test_write_tools_display_the_document_they_actually_touch():
    """This line is often all the user has to tell which document was changed.

    It must therefore come from the arguments -- the doc_id actually operated on --
    and not from the model's own call_goal, which the model could write to say anything.
    """
    from jiuwenswarm.common.tool_display import build_tool_display_name

    name = build_tool_display_name("clouddoc_batch_edit", {"doc_id": DOC, "edits": []})
    assert "修改云文档" in name
    assert DOC[-8:] in name, name


def test_the_session_suffix_does_not_erase_the_receipt():
    """The ability manager appends ``_jiuwenswarm_s_<session>`` to tool names. Unstripped,
    the lookup misses and the write shows **no name at all** -- worse than before, since
    the write tools no longer fall back to the model's text."""
    from jiuwenswarm.common.tool_display import build_tool_display_name

    plain = build_tool_display_name("clouddoc_batch_edit", {"doc_id": DOC})
    suffixed = build_tool_display_name("clouddoc_batch_edit_jiuwenswarm_s_web_abc123", {"doc_id": DOC})
    assert suffixed == plain != ""


def test_a_write_ignores_the_models_own_wording_and_names_the_document():
    """The behaviour, not just the constant.

    An earlier version of this test asserted only the membership of the exempt set --
    which stays true however the caller uses it, so replacing the branch with a no-op
    left every test green. This one drives the decision itself with a model call_goal
    that names the *other* document.
    """
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        resolve_tool_display_name,
    )

    lie = "把初稿写进 Q3 Launch Note"
    shown = resolve_tool_display_name("clouddoc_batch_edit", lie, {"doc_id": DOC})
    assert lie not in shown
    assert DOC[-8:] in shown, shown


def test_a_read_still_uses_the_models_wording():
    """Reading tools keep call_goal -- it reads better and nothing is at stake."""
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        resolve_tool_display_name,
    )

    goal = "查找用户提到的那篇共享文档"
    assert resolve_tool_display_name("clouddoc_read", goal, {"doc_id": DOC}) == goal


# ------------------------- user text is the rail's only admissible evidence

def _write_history(tmp_path, session_id, lines):
    d = tmp_path / session_id
    d.mkdir(parents=True)
    import json
    (d / "history.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8"
    )
    return tmp_path


def test_user_text_reads_jsonl_and_counts_the_ask_user_answer(tmp_path, monkeypatch):
    """Both halves of a bug that made the rail unusable.

    The file is ``history.jsonl`` (one object per line), not ``history.json`` -- reading
    the wrong name returns "" for everything, so the rail refused every document
    operation forever; observed as the model retrying a refused read nineteen times and
    then claiming success anyway.

    And the ask_user answer is stored on an **assistant** record as a tool result, not as
    a user record. Counting only role=="user" leaves the recovery loop unclosable: the
    model is told to ask, asks, and the answer still does not satisfy the check.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    sid = "web_test"
    root = _write_history(tmp_path, sid, [
        {"role": "user", "content": "In the shared doc, draft the plan."},
        {"role": "assistant", "content": "", "tool_name": "clouddoc_read", "result": "Q3 Launch Note"},
        {"role": "assistant", "content": "", "tool_name": "ask_user",
         "result": "Please select the document:: My Document 01"},
        {"role": "assistant", "content": "I have drafted it in Q3 Launch Note."},
    ])
    monkeypatch.setattr(interface_deep, "get_agent_sessions_dir", lambda: root, raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: root, raising=False
    )

    text = interface_deep._clouddoc_user_text(sid)
    assert "draft the plan" in text
    assert "My Document 01" in text          # ask_user 答复算数
    assert "I have drafted" not in text      # 模型自己的汇报不算
    assert "Q3 Launch Note" not in text      # 模型读到的内容也不算


def test_user_text_is_empty_when_history_is_missing(tmp_path, monkeypatch):
    """Fail closed: no evidence means an ambiguous write is refused, not allowed."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path, raising=False
    )
    assert interface_deep._clouddoc_user_text("nope") == ""
    assert interface_deep._clouddoc_user_text(None) == ""


# ------------------------------------------- doubly escaped anchors, as observed

def test_a_literally_escaped_newline_anchor_is_repaired():
    r"""The exact payload from the live failure.

    The model sent ``"Phased Release\\nPhase 1 …"`` -- backslash and n, two characters --
    where the body holds a real newline, so the anchor could never match. It then resent
    the identical payload **42 times**, because "未应用（not_found）" told it nothing.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import _repair_escapes

    body = "Phased Release\nPhase 1 involves internal beta testing.\n"
    pairs = [("Phased Release\\nPhase 1 involves internal beta testing.", "Staged Release\\nStage 1 involves internal beta testing.")]

    fixed, changed = _repair_escapes(pairs, body)
    assert changed is True
    assert fixed[0][0] == "Phased Release\nPhase 1 involves internal beta testing."
    assert fixed[0][1] == "Staged Release\nStage 1 involves internal beta testing."


def test_repair_never_redirects_an_anchor_that_already_matches():
    r"""Only anchors that are **absent** get repaired, and only when the repair is
    present. Otherwise a document legitimately containing the characters ``\n`` would
    have its edits silently pointed somewhere else."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import _repair_escapes

    body = "a literal \\n lives here"
    pairs = [("a literal \\n lives here", "replaced")]

    fixed, changed = _repair_escapes(pairs, body)
    assert changed is False
    assert fixed == pairs


@pytest.mark.asyncio
async def test_an_unfixable_anchor_says_so_instead_of_inviting_a_retry(fake):
    """A generic "未应用" invites the model to resend the same thing forever. The refusal
    has to name the offending anchor and say that resending will not work."""
    async def not_found(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        return EditResult("invalid", detail="anchor missing")

    fake.edit_batch = not_found
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "x")

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "nowhere to be found", "new_string": "y"}])
    assert out["ok"] is False
    assert "原样重发不会成功" in out["detail"]
    assert "nowhere to be found" in out["detail"]
    assert "clouddoc_read" in out["detail"]


@pytest.mark.asyncio
async def test_batch_edit_actually_applies_the_repair(fake):
    r"""The wiring, not just the helper.

    An earlier version of this file tested ``_repair_escapes`` on its own, so replacing
    the call inside ``batch_edit`` with a no-op left every test green -- the same mistake
    twice in one session. This drives ``batch_edit`` end to end with the literally
    escaped anchor and asserts the edit lands.
    """
    body = "Phased Release\nPhase 1 involves internal beta testing.\n"

    async def read(doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="rev1", text=body)

    async def edit_batch(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        # Behaves like the Google provider: an anchor absent from the body is ``invalid``.
        for old, _new in pairs:
            if old and old not in body:
                return EditResult("invalid", detail="anchor missing")
        fake.edits.append((doc_ref, list(pairs)))
        return EditResult("applied", new_revision_id="rev2")

    fake.read = read
    fake.edit_batch = edit_batch
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "x")

    out = await tk.batch_edit(doc_id=DOC, edits=[{
        "old_string": "Phased Release\\nPhase 1 involves internal beta testing.",
        "new_string": "Staged Release\\nStage 1 involves internal beta testing.",
    }])

    assert out["ok"] is True, out
    applied = fake.edits[-1][1][0]
    assert applied[0] == "Phased Release\nPhase 1 involves internal beta testing."
    assert applied[1] == "Staged Release\nStage 1 involves internal beta testing."


@pytest.mark.asyncio
async def test_the_refusal_names_the_document_the_user_already_confirmed(fake):
    """Observed live: the user answered "My Document 01", and the model went on targeting
    Q3 Launch Note turn after turn. The rail kept refusing -- correctly -- but a refusal
    that only says "the user did not say" leaves it guessing again. Naming the confirmed
    document turns the refusal into a correction."""
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "In the shared doc, draft a plan.\nPlease select the document:: Rollout Plan",
    )

    out = await tk.read(doc_id=OTHER)          # 请求另一篇
    assert out["ok"] is False
    assert out["user_named"] == "Rollout Plan"
    assert "用户点名的是「Rollout Plan」" in out["detail"]


@pytest.mark.asyncio
async def test_naming_both_falls_back_to_asking(fake):
    """With both titles in the user's text there is nothing to correct toward, so the
    tool asks rather than picking the one that happens to sort first."""
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "compare Rollout Plan against Q3 Launch Note",
    )

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "x"}])
    # Both documents named by the user: no ambiguity left, so the call proceeds.
    assert out["ok"] is True, out


# ------------------------- a confirmed document is marked before the model picks

@pytest.mark.asyncio
async def test_the_listing_marks_the_document_the_user_already_named(fake):
    """Correcting the model after it picks still costs a wrong guess every turn.

    Observed: the user confirmed one document, and on the very next turn the model went
    back to the other one, was refused by the rail, and had to ask again. The fact has to
    be in front of it **before** it chooses, not only in the refusal afterwards.
    """
    fake.accessible = [
        DocSummary(doc_id=DOC, title="Rollout Plan", can_edit=True),
        DocSummary(doc_id=OTHER, title="Q3 Launch Note", can_edit=True),
    ]
    tk = CloudDocToolkit(
        fake,
        watched_docs=lambda: [DOC, OTHER],
        user_text=lambda: "draft it\nPlease select the document:: Rollout Plan",
    )

    out = await tk.list_documents()
    by_id = {d["doc_id"]: d for d in out["documents"]}
    assert by_id[DOC]["user_named"] is True
    assert by_id[OTHER]["user_named"] is False
    assert "已指明「Rollout Plan」" in out["detail"]
    assert "不要重新挑" in out["detail"]


@pytest.mark.asyncio
async def test_no_hint_when_the_user_named_nothing(fake):
    """With nothing confirmed there is nothing to carry forward, and a hint would be an
    invitation to pick one anyway."""
    fake.accessible = [
        DocSummary(doc_id=DOC, title="Rollout Plan", can_edit=True),
        DocSummary(doc_id=OTHER, title="Q3 Launch Note", can_edit=True),
    ]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "在共享文档里写点东西")

    out = await tk.list_documents()
    assert all(d["user_named"] is False for d in out["documents"])
    assert "已指明" not in out["detail"]


@pytest.mark.asyncio
async def test_no_hint_when_a_single_document_is_managed(fake):
    """One document needs no disambiguation, and the hint would be noise that trains the
    model to look for a choice that is not there."""
    fake.accessible = [DocSummary(doc_id=DOC, title="Rollout Plan", can_edit=True)]
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "update Rollout Plan")

    out = await tk.list_documents()
    assert "已指明" not in out["detail"]


def test_the_description_explains_user_named():
    """A field the model does not understand is a field it ignores."""
    tools = {t.card.name: t for t in CloudDocToolkit(FakeProvider()).get_tools()}
    text = tools["clouddoc_list_documents"].card.description
    assert "user_named" in text
    assert "不要重新挑" in text


@pytest.mark.asyncio
async def test_a_missing_title_never_counts_as_named(fake):
    """The empty string is a substring of everything.

    With titles unavailable (a Drive hiccup degrades the listing to bare ids), an
    unguarded ``normalize(title) in said`` marks **every** document as user-named --
    which would switch the whole ambiguity rail off exactly when the tool knows least.
    """
    async def boom():
        raise ProviderError("forbidden", "Drive API has not been enabled")

    fake.list_accessible_documents = boom
    tk = CloudDocToolkit(
        fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "写点东西"
    )

    out = await tk.list_documents()
    assert all(d["user_named"] is False for d in out["documents"]), out
    assert "已指明" not in out["detail"]


@pytest.mark.asyncio
async def test_a_missing_title_still_refuses_an_ambiguous_write(fake):
    """Same trap on the rail side: an empty title must not satisfy the check."""
    async def no_title(doc_ref):
        raise ProviderError("forbidden", "no title")

    fake.title = no_title
    tk = CloudDocToolkit(
        fake, watched_docs=lambda: [DOC, OTHER], user_text=lambda: "改一下那个文档"
    )

    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "x"}])
    assert out["ok"] is False
    assert out["reason"] == "ambiguous_document"


# ------------------------------------------------------------ creating documents


@pytest.mark.asyncio
async def test_create_document_creates_and_shares_as_one_operation():
    prov = FakeProvider()
    tk = CloudDocToolkit(prov)
    out = await tk.create_document(title="Meeting Notes", share_with=["a@example.com"])
    assert out["ok"], out
    assert prov.created == ["Meeting Notes"]
    assert prov.shares == [("doc-CREATED-000000000000", "a@example.com", "writer")]
    # The link comes from the provider, so what matters is that it names this document
    # and was not templated here. Asserting a Google editor's "/edit" suffix is what let
    # the hard-coded template survive: it described one platform's URL as the contract.
    assert out["url"] == prov.doc_url("doc-CREATED-000000000000", "document")
    assert "doc-CREATED-000000000000" in out["url"]


@pytest.mark.asyncio
async def test_create_document_refuses_without_a_share_target():
    """Born in the service account's Drive, an unshared document 404s for the very
    person who asked for it. Refusing is kinder than succeeding uselessly."""
    prov = FakeProvider()
    tk = CloudDocToolkit(prov)
    out = await tk.create_document(title="Meeting Notes", share_with=[])
    assert not out["ok"] and "share_with" in out["detail"]
    assert not getattr(prov, "created", [])


@pytest.mark.asyncio
async def test_create_document_refused_on_unattended_turns():
    prov = FakeProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: DOC)
    out = await tk.create_document(title="X", share_with=["a@example.com"])
    assert not out["ok"]
    assert not getattr(prov, "created", [])


@pytest.mark.asyncio
async def test_create_document_reports_partial_share_failures_loudly():
    prov = FakeProvider()
    prov.share_fails = ("bad@example.com",)
    tk = CloudDocToolkit(prov)
    out = await tk.create_document(
        title="X", share_with=["good@example.com", "bad@example.com"]
    )
    assert out["ok"]
    assert out["shared_with"] == ["good@example.com"]
    assert "bad@example.com" in out["detail"]


def test_create_document_is_denylisted_for_unattended_turns():
    assert "clouddoc_create_document" in UNATTENDED_DENYLIST
    assert "clouddoc_create_document" not in UNATTENDED_ALLOWLIST


# ------------------------------------------------------------ highlighting edits


@pytest.mark.asyncio
async def test_batch_edit_passes_highlight_through_to_the_provider():
    prov = FakeProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: DOC)
    out = await tk.batch_edit(
        doc_id=DOC, edits=[{"old_string": "hello", "new_string": "hi"}], highlight=True
    )
    assert out["ok"], out
    assert prov.highlights == [True]
    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hi", "new_string": "yo"}])
    assert prov.highlights == [True, False], "不传 highlight 必须落为 False"


def test_edit_requests_paint_exactly_the_changed_middle():
    """The replacement and the style request must cover only the text that actually
    differs -- common prefix and suffix are carried by the document, not resent --
    and spans are measured in UTF-16 code units like every other index the API sees."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        _edit_requests,
        _trim_common_affixes,
    )

    # Whole-paragraph rewrite where only the middle word changed: the request must
    # shrink to that word. old = "Hello brave world", new = "Hello bright world".
    reqs = _edit_requests([(10, 27, "Hello brave world", "Hello bright world")],
                          highlight=True)
    kinds = [next(iter(r)) for r in reqs]
    assert kinds == ["deleteContentRange", "insertText", "updateTextStyle"]
    # prefix "Hello br" (8) and suffix " world" shared → middle "ave" → "ight"
    assert reqs[0]["deleteContentRange"]["range"] == {"startIndex": 18, "endIndex": 21}
    assert reqs[1]["insertText"] == {"location": {"index": 18}, "text": "ight"}
    style = reqs[2]["updateTextStyle"]
    assert style["range"] == {"startIndex": 18, "endIndex": 22}
    assert style["fields"] == "backgroundColor"

    # Surrogate pairs in the shared prefix must offset in UTF-16 units. 𝄞 = 2 units.
    reqs = _edit_requests([(10, 15, "𝄞ab", "𝄞xb")], highlight=True)
    assert reqs[0]["deleteContentRange"]["range"] == {"startIndex": 12, "endIndex": 13}
    assert reqs[1]["insertText"]["text"] == "x"

    # Identical text produces no requests at all -- nothing changed, nothing painted.
    assert _edit_requests([(10, 15, "same", "same")], highlight=True) == []

    # A pure deletion leaves nothing to paint.
    reqs = _edit_requests([(10, 15, "abcde", "")], highlight=True)
    assert [next(iter(r)) for r in reqs] == ["deleteContentRange"]

    # A pure insertion (empty old, the first-write path goes elsewhere, but the
    # helper must still be sane) deletes nothing.
    reqs = _edit_requests([(10, 10, "", "new text")], highlight=False)
    assert [next(iter(r)) for r in reqs] == ["insertText"]

    # Overlap safety inside the trimmer: shared runs must not be counted twice.
    assert _trim_common_affixes("aaa", "aa") == (2, "a", "")
    assert _trim_common_affixes("aa", "aaa") == (2, "", "a")


@pytest.mark.asyncio
async def test_create_document_translates_the_quota_lie():
    """Consumer Google gives service accounts zero Drive storage; Drive reports the
    resulting failure as "storage quota exceeded", which reads like a full mailbox.
    The tool must say what is actually wrong and what to do instead."""
    prov = FakeProvider()
    prov.raise_on_create = ProviderError(
        "forbidden", "The user's Drive storage quota has been exceeded."
    )
    tk = CloudDocToolkit(prov)
    out = await tk.create_document(title="X", share_with=["a@example.com"])
    assert not out["ok"]
    assert "共享给本账号" in out["detail"]
    assert "quota" not in out["detail"]


# ------------------------------------------------------------ comment-scoped edits


class _FarProvider(_RangeProvider):
    """A body with a unique sentence far outside the comment's ±200 window."""
    BODY = _RangeProvider.BODY + "中间。" * 200 + "远处有一个独特的句子。"

    async def read(self, doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="r1", text=self.BODY)


@pytest.mark.asyncio
async def test_scoped_edit_inside_the_comment_window_lands():
    prov = _FarProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "需要被改写", "new_string": "已经改好", "for_comment_id": "c1"},
    ])
    assert out["ok"], out
    assert prov.edits, "窗口内的修改应当提交"


@pytest.mark.asyncio
async def test_scoped_edit_outside_the_window_refuses_the_whole_batch():
    """The scoping is the rail from the proposal path, not a new rule: an edit that
    strays outside its comment's window refuses atomically, nothing lands."""
    prov = _FarProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "远处有一个独特的句子。", "new_string": "被改掉了。",
         "for_comment_id": "c1"},
    ])
    assert not out["ok"]
    assert "超出" in out["detail"]
    assert not prov.edits, "整批不得落地"


@pytest.mark.asyncio
async def test_scoped_edit_with_a_fabricated_comment_id_is_refused():
    """An unknown id must refuse, not skip: skipping would let a fabricated id buy an
    unscoped edit while looking scoped — the fabricated-doc_id incident in a new coat."""
    prov = _FarProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "需要被改写", "new_string": "已经改好",
         "for_comment_id": "c-fabricated"},
    ])
    assert not out["ok"]
    assert "不存在" in out["detail"]
    assert not prov.edits


@pytest.mark.asyncio
async def test_unscoped_edits_keep_todays_behaviour():
    prov = _FarProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "远处有一个独特的句子。", "new_string": "随便改。"},
    ])
    assert out["ok"], "未声明 for_comment_id 的编辑不受新约束"


# ------------------------------------------- several comments on one passage (D5, IC-4)


class _TwoCommentProvider(_RangeProvider):
    """Two comments quoting overlapping text, and one quoting a distant sentence."""

    BODY = (
        "填充。" * 40
        + "这一句需要被改写。紧接着的这一句也要改。"
        + "尾巴。" * 200
        + "很远处另有一个独特的句子。"
    )

    async def read(self, doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="r1", text=self.BODY)

    async def list_comments(self, doc_ref, *, include_resolved=False):
        def c(cid, quoted):
            return DocComment(
                comment_id=cid, author_is_self=False, author_display_name="X",
                created_time="2026-01-01T00:00:00.000Z", content="改", quoted_text=quoted,
                resolved=False,
            )
        return [
            c("c1", "这一句需要被改写。"),
            c("c2", "紧接着的这一句也要改。"),
            c("far", "很远处另有一个独特的句子。"),
        ]


@pytest.mark.asyncio
async def test_one_edit_may_answer_two_comments_on_the_same_passage():
    """The merge exists so a single edit can serve comments about one sentence, which
    is what people write when two of them mark up the same line."""
    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。紧接着的这一句也要改。",
         "new_string": "两句都已改好。",
         "for_comment_ids": ["c1", "c2"]},
    ])
    assert out["ok"], out
    assert prov.edits, "重叠范围内的合并修改应当提交"


@pytest.mark.asyncio
async def test_comments_at_opposite_ends_cannot_be_merged():
    """IC-4's attack: declaring a near and a far comment together would make the union
    the whole document, so a "bounded" edit covers everything. The batch is refused."""
    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。",
         "for_comment_ids": ["c1", "far"]},
    ])
    assert not out["ok"]
    assert not prov.edits, "整批不得写入"


@pytest.mark.asyncio
async def test_merged_scope_still_refuses_an_edit_outside_it():
    """Merging widens which text is in range, not what may be done to it."""
    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "很远处另有一个独特的句子。", "new_string": "越界写入。",
         "for_comment_ids": ["c1", "c2"]},
    ])
    assert not out["ok"]
    assert not prov.edits


@pytest.mark.asyncio
async def test_a_fabricated_id_among_real_ones_refuses_the_batch():
    """Unknown ids are refused rather than dropped: a model that invents one alongside
    a real one must not buy a wider window while looking scoped."""
    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。",
         "for_comment_ids": ["c1", "c-invented"]},
    ])
    assert not out["ok"]
    assert not prov.edits


@pytest.mark.asyncio
async def test_the_singular_field_still_works():
    """PR1 spelled this for_comment_id and callers still do; it means a list of one."""
    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。", "for_comment_id": "c1"},
    ])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_the_receipt_records_every_comment_an_edit_answered():
    """A merged edit answers several threads, and a revert has to reach all of them, so
    the mapping the receipt carries is the one the rail verified."""
    prov = _TwoCommentProvider()
    seen: dict = {}

    async def spy(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        seen.update(getattr(prov, "receipt_meta", None) or {})
        return EditResult("applied", new_revision_id="rev2")

    prov.edit_batch = spy
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。紧接着的这一句也要改。",
         "new_string": "两句都已改好。",
         "for_comment_ids": ["c1", "c2"]},
    ])
    assert out["ok"], out
    mapping = seen.get("for_comment_ids_by_old") or {}
    assert mapping.get("这一句需要被改写。紧接着的这一句也要改。") == ["c1", "c2"]


# ------------------------------------------------ capability negotiation (C9, PR3 prep)


class _NonAtomicProvider(_RangeProvider):
    """A platform whose API applies edits one at a time -- the Feishu shape."""

    async def capabilities(self, doc_ref):
        return DocCapabilities(
            can_read=True, can_edit=True, can_comment=True, can_resolve=True,
            has_revision_control=True, max_quote_chars=None, atomic_batch=False,
        )


@pytest.mark.asyncio
async def test_a_platform_without_atomic_batches_refuses_a_multi_edit_write():
    """Refused, not worked around. The document is shared, so a half-applied batch is
    visible to everyone reading it, and undoing it would race whoever else is typing."""
    prov = _NonAtomicProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。"},
        {"old_string": "填充。", "new_string": "换掉。"},
    ])
    assert not out["ok"]
    assert "逐条" in out["detail"], out
    assert not prov.edits, "整批不得写入"


@pytest.mark.asyncio
async def test_a_single_edit_still_lands_without_atomic_batches():
    """One edit is one write on any platform, so the limitation does not reach it."""
    prov = _NonAtomicProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。"},
    ])
    assert out["ok"], out
    assert prov.edits


@pytest.mark.asyncio
async def test_a_provider_that_cannot_answer_is_treated_as_capable():
    """Silence must not take the write tool away for reasons unrelated to the write:
    a provider predating the flag, or one whose capability call fails, keeps working."""
    prov = _RangeProvider()  # implements no capabilities() at all
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。"},
        {"old_string": "填充。", "new_string": "换掉。"},
    ])
    assert out["ok"], out


# ------------------------------------------- inverse operations stay attended (IC-5)


def test_no_tier_can_hand_an_unattended_turn_a_revert():
    """Reverting is authorised from the principal's attended surfaces only. A comment
    that talked an unattended turn into undoing the last batch would erase someone's
    legitimate edit, so no tier may carry one."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_FAMILIES,
        unattended_allowlist_for,
    )

    for mode in list(UNATTENDED_FAMILIES) + ["", None, "bogus"]:
        allowed = unattended_allowlist_for(mode)
        assert not [t for t in allowed if t.startswith("clouddoc_revert")]
        assert not [t for t in allowed if t.startswith("clouddoc_unhighlight")]


def test_an_inverse_tool_added_to_a_family_by_mistake_is_still_stripped():
    """The families are hand-written sets, so the rule is enforced where every caller
    passes rather than trusted to whoever edits them next."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import clouddoc_tools as ct

    original = ct.UNATTENDED_FAMILIES["apply_scoped"]
    ct.UNATTENDED_FAMILIES["apply_scoped"] = original | {"clouddoc_revert_batch"}
    try:
        assert "clouddoc_revert_batch" not in ct.unattended_allowlist_for("apply_scoped")
    finally:
        ct.UNATTENDED_FAMILIES["apply_scoped"] = original


def test_an_unknown_mode_gets_no_tools_at_all():
    """A mode nobody recognises must not widen anything -- and with the tier ladder
    retired, "least authority" is the empty set, not a narrower family.

    It used to compare against the reply_only family. That family is gone: its
    tools (read, list, reply) are ones the agent holds without any mandate, so as
    a fallback it authorised the one thing a mandate is actually for -- running an
    unattended turn on this document.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_FAMILIES,
        unattended_allowlist_for,
    )

    assert set(UNATTENDED_FAMILIES) == {"apply_scoped"}
    for mode in ("", None, "bogus", "APPLY_SCOPED", "reply_only"):
        assert unattended_allowlist_for(mode) == frozenset(), mode


# --------------------------- the rail being unavailable degrades in two directions


@pytest.mark.asyncio
async def test_a_merged_scope_is_refused_when_the_rail_cannot_run(monkeypatch):
    """Losing the rail costs precision on a single-comment scope, which a present
    person can judge. A multi-comment scope is different: the code that decides whether
    those comments overlap is the same import, and letting it through unchecked is the
    widening IC-4 exists to stop -- two comments at opposite ends would become one
    range covering the document."""
    import builtins

    real_import = builtins.__import__

    def no_rail(name, *a, **kw):
        if name.endswith("range_rail") or ".range_rail" in name:
            raise ImportError("gateway package absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_rail)

    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。",
         "for_comment_ids": ["c1", "c2"]},
    ])
    assert not out["ok"]
    assert "分别提交" in out["detail"], out
    assert not prov.edits


@pytest.mark.asyncio
async def test_a_single_scope_still_goes_through_when_the_rail_cannot_run(monkeypatch):
    """An agentserver deployed without the gateway package must not lose its write tool
    entirely; on the chat path a person is present and the write is behind an ask."""
    import builtins

    real_import = builtins.__import__

    def no_rail(name, *a, **kw):
        if name.endswith("range_rail") or ".range_rail" in name:
            raise ImportError("gateway package absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_rail)

    prov = _TwoCommentProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改过了。",
         "for_comment_id": "c1"},
    ])
    assert out["ok"], out


# ------------------------------------ a receipt points back at what commissioned it


@pytest.mark.asyncio
async def test_a_chat_write_records_who_commissioned_it():
    """Ring ⑤ asks that a performance be replayable to its source. `source` names the
    write path, which is the code; this names the turn, which is the person or the
    comment that asked for it."""
    prov = _RangeProvider()
    seen: dict = {}

    async def spy(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        seen.update(getattr(prov, "receipt_meta", None) or {})
        return EditResult("applied", new_revision_id="rev2")

    prov.edit_batch = spy
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改好了。"},
    ])
    assert out["ok"], out
    assert seen.get("executor") == "chat"


def test_an_unattended_write_names_the_comment_that_triggered_it():
    """Read back later, "a write happened" does not answer who asked for it; the
    triggering comment does, and it is known at the time. Checked on the metadata the
    write path sets rather than by driving a full turn, since reaching the write also
    requires a live watch -- that gate has its own tests."""
    import re
    import pathlib as _p

    src = _p.Path(
        "jiuwenswarm/extensions/co_scribe/backend/toolkit/clouddoc_tools.py"
    ).read_text(encoding="utf-8")
    block = src[src.index('"apply_for_comment" if declared_scope'):][:800]
    assert re.search(r'"executor":\s*f"comment:\{comment_id\}"', block), block[:200]


@pytest.mark.asyncio
async def test_the_rail_uses_the_quote_limit_the_provider_measured():
    """The provider measures where its platform truncates a quote; the rail compared
    against a configured default and never asked, so quotes between the two numbers were
    refused without having been truncated.

    Wired once into the watcher, attached to apply machinery that was later retired --
    so deleting the dead code silently took the live reader with it and the measurement
    went back to doing nothing, while a test asserting "this flag has a reader" kept
    passing because it grepped for the name rather than for a reachable caller. It sits
    with the rail now."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import RangeRailConfig

    class _Measured(FakeProvider):
        """FakeProvider declares no capabilities at all, so this one answers for it."""

        limit: int | None = 418

        async def capabilities(self, doc_ref):
            return DocCapabilities(
                can_read=True, can_edit=True, can_comment=True, can_resolve=True,
                has_revision_control=True, max_quote_chars=self.limit, atomic_batch=True,
            )

    prov = _Measured()
    kit = CloudDocToolkit(prov)
    assert (await kit._rail_cfg(DOC)).max_quote_chars == 418

    # A provider that declares no limit leaves the deployment's configured value alone.
    prov.limit = None
    assert (await kit._rail_cfg(DOC)).max_quote_chars == RangeRailConfig().max_quote_chars


@pytest.mark.asyncio
async def test_write_region_records_who_commissioned_the_write():
    """Ring ⑤: a receipt must point back at what commissioned the write, not only at
    the code that carried it out.

    The other two write paths already set ``receipt_meta`` (batch_edit as ``chat``,
    apply_for_comment as ``comment:<id>``); the region path shipped without it, so
    every region receipt read back with an empty executor -- the enumerated fix of
    §16.12 not carried to a path added after the enumeration. This tool refuses
    unattended turns, so its commissioning party is always the person in the chat."""
    seen = {}

    class _Prov(FakeProvider):
        def __init__(self):
            super().__init__()
            self.receipt_meta = None

        async def write_regions(self, doc_ref, pairs, *, required_revision_id=""):
            seen["meta"] = dict(self.receipt_meta or {})
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
                EditResult,
            )
            return EditResult(status="applied")

    prov = _Prov()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: None)
    out = await tk.write_region(DOC, "Sheet1!A1", [["x"]])
    assert out["ok"], out.get("detail")
    assert seen["meta"].get("source") == "write_region"
    assert seen["meta"].get("executor") == "chat"
    assert prov.receipt_meta is None, "工具返回后 meta 必须复位，不得泄给下一次写入"


@pytest.mark.asyncio
async def test_write_region_resets_meta_even_when_the_provider_refuses():
    """The reset lives in ``finally`` for a reason: a refused write that leaves meta
    behind would label the next unrelated write with this turn's commissioning party."""
    class _Prov(FakeProvider):
        def __init__(self):
            super().__init__()
            self.receipt_meta = None

        async def write_regions(self, doc_ref, pairs, *, required_revision_id=""):
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
                ProviderError,
            )
            raise ProviderError("invalid", "拒绝")

    prov = _Prov()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: None)
    out = await tk.write_region(DOC, "Sheet1!A1", [["x"]])
    assert not out["ok"]
    assert prov.receipt_meta is None


def test_every_tool_schema_matches_its_callable_signature():
    """The parameter schema is a hand-written second copy of the signature, and a
    copy drifts. Measured live: the regions form was added to apply_for_comment's
    signature but not its schema, and the model -- correctly reading its tools --
    reported the form 'is not among my available tools' while the code sat there
    ready. The declared surface IS the capability as far as the model is concerned;
    this pins schema properties to signature parameters for every clouddoc tool."""
    import inspect

    kit = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None)
    checked = 0
    for tool in kit.get_tools():
        card = getattr(tool, "card", None) or getattr(tool, "tool_card", None) or tool
        name = getattr(card, "name", "?")
        params = (getattr(card, "input_params", None) or {}).get("properties", {})
        func = getattr(tool, "func", None) or getattr(tool, "_func", None)
        if func is None:
            continue
        sig = {p for p in inspect.signature(func).parameters if p != "self"}
        declared = set(params)
        assert declared <= sig, f"{name}: schema 声明了签名没有的参数 {declared - sig}"
        assert sig <= declared, f"{name}: 签名参数未暴露给模型 {sig - declared}"
        checked += 1
    assert checked >= 10, f"只检查了 {checked} 个工具——遍历本身失效了"


# ---------------------------------------------------------------- D16: effect classes


def test_every_tool_declares_an_effect_class_on_its_card():
    """D16: a mode reasons about classes, so a tool without one is invisible to the
    whole coordinate system -- a registration error, not a default."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        ALL_TOOL_NAMES,
        EFFECT_CLASSES,
    )

    assert set(EFFECT_CLASSES) == set(ALL_TOOL_NAMES)
    assert set(EFFECT_CLASSES.values()) <= {
        "read", "communicate", "revertible_write", "irreversible_write", "grant"
    }
    kit = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None)
    seen = 0
    for tool in kit.get_tools():
        card = getattr(tool, "card", None) or tool
        props = getattr(card, "properties", None) or {}
        name = getattr(card, "name", "?")
        assert props.get("effect_class") == EFFECT_CLASSES[name], name
        seen += 1
    assert seen >= 10


@pytest.mark.asyncio
async def test_the_grant_floor_holds_under_full_access(monkeypatch):
    """D16 floor: creating-and-sharing hands out access, and Full Access has no
    confirmation channel -- so 'always ask' realizes as a refusal naming the way
    back. No mode may lift this."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: False)
    kit = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None)
    out = await kit.create_document(title="t", share_with=["a@b.c"])
    assert not out["ok"]
    assert "授权类" in out["detail"] and "Full Access" in out["detail"]


@pytest.mark.asyncio
async def test_the_grant_floor_stands_aside_when_the_channel_exists(monkeypatch):
    """With a confirmation channel present the ordinary ask machinery owns the
    decision; the floor must not double-refuse."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: True)
    kit = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None)
    out = await kit.create_document(title="t", share_with=["a@b.c"])
    # FakeProvider has no create_document -- reaching provider failure means the
    # floor let it pass; refusing at the floor would carry the floor's wording.
    assert "授权类" not in (out.get("detail") or "")


@pytest.mark.asyncio
async def test_a_write_without_receipts_demotes_and_the_floor_catches_it(monkeypatch):
    """D16 demotion: revertible is a mechanical fact. A missing sink makes the write
    irreversible, and an irreversible write under Full Access is refused -- a broken
    receipt chain tightens the gate rather than loosening the bookkeeping."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: False)

    class _Prov(FakeProvider):
        receipt_sink = None

    kit = CloudDocToolkit(_Prov(), turn_doc_id=lambda: None)
    out = await kit.batch_edit(DOC, [{"old_string": "a", "new_string": "b"}])
    assert not out["ok"] and "没有回执记录" in out["detail"]
    out2 = await kit.write_region(DOC, "Sheet1!A1", [["x"]])
    assert not out2["ok"] and "没有回执记录" in out2["detail"]


@pytest.mark.asyncio
async def test_a_receipted_write_stays_auto_under_full_access(monkeypatch):
    """The other half of D16-A: with the receipt chain whole, a revertible write is
    exactly what Full Access is allowed to wave through -- acceptance plus
    revertibility over step-by-step approval, applied to ourselves."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: False)

    class _Prov(FakeProvider):
        def __init__(self):
            super().__init__()
            self.receipt_sink = object()

        async def write_regions(self, doc_ref, pairs, *, required_revision_id=""):
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
                EditResult,
            )
            return EditResult(status="applied")

    kit = CloudDocToolkit(_Prov(), turn_doc_id=lambda: None)
    out = await kit.write_region(DOC, "Sheet1!A1", [["x"]])
    assert out["ok"], out.get("detail")


# ------------------------------------------------------------- exported MCP surface


@pytest.mark.asyncio
async def test_a_full_doc_id_in_the_users_words_exempts_the_ambiguity_rail():
    """The id branch compares like with like. It did not, once: the haystack was
    normalized (hyphens stripped) while the id stayed raw, so a user pasting the
    full document id was refused anyway -- dead since the rail was written, unnoticed
    because people paste titles and links. The MCP surface, whose "user text" is the
    call arguments, hit it on its first write."""
    class _Prov(FakeProvider):
        async def write_regions(self, doc_ref, pairs, *, required_revision_id=""):
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
                EditResult,
            )
            return EditResult(status="applied")

    prov = _Prov()
    prov.receipt_sink = object()
    # A hyphenated id, because that is what broke: normalization strips the hyphens
    # from the haystack, and a needle that keeps them can never match.
    hid = "1Ryx-ATR4_h1dV-DBeHS0000"
    kit = CloudDocToolkit(
        prov,
        turn_doc_id=lambda: None,
        watched_docs=lambda: [hid, OTHER],   # two managed docs: the rail is armed
        user_text=lambda: f'{{"doc_id": "{hid}"}}',
    )
    out = await kit.write_region(hid, "Sheet1!A1", [["x"]])
    assert out["ok"], out.get("detail")


@pytest.mark.asyncio
async def test_the_ask_channel_override_beats_the_deployment_config(monkeypatch):
    """An exported surface has no dialog to raise whatever the config says; the
    override pins the floor by declaration rather than by luck."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: True)  # config says ask exists
    kit = CloudDocToolkit(FakeProvider(), turn_doc_id=lambda: None, ask_channel=False)
    out = await kit.create_document(title="t", share_with=["a@b.c"])
    assert not out["ok"] and "授权类" in out["detail"]


@pytest.mark.asyncio
async def test_the_executor_label_reaches_the_receipt_meta():
    """Ring ⑤ on the exported surface: a CLI's work must read as the CLI's."""
    seen = {}

    class _Prov(FakeProvider):
        def __init__(self):
            super().__init__()
            self.receipt_meta = None
            self.receipt_sink = object()

        async def write_regions(self, doc_ref, pairs, *, required_revision_id=""):
            seen["meta"] = dict(self.receipt_meta or {})
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
                EditResult,
            )
            return EditResult(status="applied")

    kit = CloudDocToolkit(_Prov(), turn_doc_id=lambda: None,
                          executor_label="mcp:claude-code")
    out = await kit.write_region(DOC, "Sheet1!A1", [["x"]])
    assert out["ok"]
    assert seen["meta"].get("executor") == "mcp:claude-code"


# ------------------------------------------------------------------ D21 modes


def _kit_mode(provider, mode, **kw):
    kit = CloudDocToolkit(
        provider, turn_address=lambda: "co-scribe@x.iam.gserviceaccount.com",
        harness_mode=mode, **kw,
    )
    kit._read_docs.add(DOCID)
    return kit


@pytest.mark.asyncio
async def test_direct_mode_writes_without_sink_or_floor():
    """D21's baseline: adoption plus direct editing. No receipt sink and no
    confirmation channel would hit the demotion floor under mandate; in direct
    mode the write simply lands -- errors are normal here, deliberately."""
    prov = _BatchProvider()
    prov.receipt_sink = None
    kit = _kit_mode(prov, "direct", ask_channel=False)
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out
    assert prov.submitted


@pytest.mark.asyncio
async def test_mandate_mode_keeps_the_demotion_floor():
    """The same construction under mandate refuses: no sink means irreversible,
    and irreversible without a channel sits under the floor (D16)."""
    prov = _BatchProvider()
    prov.receipt_sink = None
    kit = _kit_mode(prov, "mandate", ask_channel=False)
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"]
    assert "底线拒绝" in out["detail"]


@pytest.mark.asyncio
async def test_direct_mode_skips_the_assigned_mutex():
    """No unattended path in direct mode, so no collision to guard against --
    the assignment stays in the document and chat editing proceeds."""
    me = "co-scribe@x.iam.gserviceaccount.com"
    assigned = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="改这句", quoted_text="q", resolved=False,
        assignee_address=me,
    )
    prov = _BatchProvider(comments=[assigned])
    kit = _kit_mode(prov, "direct")

    async def live(_doc):  # even a live watch is irrelevant off mandate
        return True

    kit._unattended_live = live
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out


def test_unknown_mode_falls_back_to_mandate_never_bare():
    prov = _BatchProvider()
    kit = _kit_mode(prov, "yolo")
    assert kit._harness_mode == "mandate"


@pytest.mark.asyncio
async def test_grant_floor_survives_every_mode():
    """Creating and sharing hands out access -- a change to authorization, not to
    content. Direct mode is about content friction; the grant floor stays."""
    prov = _BatchProvider()
    kit = _kit_mode(prov, "direct", ask_channel=False)
    out = await kit.create_document(title="t", share_with=["a@b.c"])
    assert not out["ok"]
    assert "授权类动作" in out["detail"]


# ----------------------------------------------------- read-before-write (sunk)


@pytest.mark.asyncio
async def test_a_write_into_an_unread_document_is_refused_under_mandate():
    """Formerly a per-persona rail; now the toolkit's own law. Writing from
    memory of an unread shared document is how other people's edits die."""
    prov = _BatchProvider()
    kit = _kit(prov, read=False)
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"] and "尚未读过" in out["detail"]
    assert prov.submitted == []


@pytest.mark.asyncio
async def test_reading_first_opens_the_write_path():
    prov = _BatchProvider()
    kit = _kit(prov, read=False)
    assert (await kit.read(doc_id=DOCID))["ok"]
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_direct_mode_is_exempt_from_read_before_write():
    """The gate is harness, and direct mode is the bare baseline (D21)."""
    prov = _BatchProvider()
    kit = _UnreadCloudDocToolkit(
        prov, turn_address=lambda: "co-scribe@x.iam.gserviceaccount.com",
        harness_mode="direct",
    )
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert out["ok"], out


@pytest.mark.asyncio
async def test_a_document_this_surface_created_counts_as_read():
    """Demanding a read of a document the same call just made would be ritual:
    the creator knows the content -- it is empty."""
    prov = _BatchProvider()
    prov.created = []

    async def create_document(title):
        prov.created.append(title)
        return DOCID

    async def share_document(doc, email):
        return None

    prov.create_document = create_document
    prov.share_document = share_document
    kit = _kit(prov, read=False)
    out = await kit.create_document(title="t", share_with=["a@b.c"])
    assert out["ok"], out
    assert DOCID in kit._read_docs


@pytest.mark.asyncio
async def test_batch_edit_hands_back_the_receipt_id():
    """The receipt id is what a person quotes to find and revert the write. A model
    that is not told it invents one (measured: a revision id reported as "the
    receipt number"), so the tool returns the ledger's own id verbatim."""
    prov = _BatchProvider()
    prov.result = EditResult("applied", new_revision_id="r2", receipt_id="ab12cd34ef56")
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后一"}],
    )
    assert out["ok"], out
    assert out["receipt_id"] == "ab12cd34ef56"


@pytest.mark.asyncio
async def test_list_documents_fills_titles_and_formats_the_listing_could_not_give():
    """A Feishu app cannot enumerate what is shared with it, so its documents came
    back as bare tokens; the model then read each to learn what it was and was
    refused by the ambiguity rail. One metadata call per unlisted document instead."""
    class _P(_BatchProvider):
        async def list_accessible_documents(self):
            raise ProviderError("invalid", "cannot enumerate")
        async def title(self, ref):
            return {"tokAAAAAAA1": "我的文档", "tokBBBBBBB2": "预算表"}.get(ref, "")
        async def doc_kind(self, ref):
            return {"tokAAAAAAA1": "document", "tokBBBBBBB2": "spreadsheet"}.get(ref, "")
    prov = _P()
    kit = CloudDocToolkit(prov, turn_address=lambda: "x@x", watched_docs=lambda: ["tokAAAAAAA1", "tokBBBBBBB2"])
    out = await kit.list_documents()
    by = {d["doc_id"]: d for d in out["documents"]}
    assert by["tokAAAAAAA1"]["title"] == "我的文档" and by["tokAAAAAAA1"]["kind"] == "document"
    assert by["tokBBBBBBB2"]["title"] == "预算表" and by["tokBBBBBBB2"]["kind"] == "spreadsheet"


@pytest.mark.asyncio
async def test_create_document_honours_the_platform_the_person_named():
    """With several connections the document is born where the person said, and a
    platform the deployment lacks is refused rather than silently redirected."""
    class _Child(_BatchProvider):
        def __init__(self, kind):
            super().__init__()
            self._kind = kind
            self.created = []
            self.shared = []
        @property
        def kind(self): return self._kind
        async def create_document(self, title):
            self.created.append(title)
            return f"{self._kind}-new-{len(self.created)}"
        async def share_document(self, ref, addr, *, role="writer"):
            self.shared.append((ref, addr))
        def doc_url(self, ref, kind=""): return f"https://{self._kind}/{ref}"
    class _Routing(_Child):
        def __init__(self, g, f):
            super().__init__("google")
            self.g, self.f = g, f
            self.learned = {}
        def for_platform(self, kind): return {"google": self.g, "feishu": self.f}.get(kind)
        def learn(self, ref, prov): self.learned[ref] = prov
    g, f = _Child("google"), _Child("feishu")
    kit = CloudDocToolkit(_Routing(g, f), turn_address=lambda: "x@x", ask_channel=True)
    out = await kit.create_document(title="T", share_with=["ou_1"], platform="feishu")
    assert out["ok"], out
    assert f.created == ["T"] and f.shared == [("feishu-new-1", "ou_1")] and g.created == []
    assert out["url"].startswith("https://feishu/")
    out = await kit.create_document(title="T", share_with=["a@b"], platform="wps")
    assert not out["ok"] and "wps" in out["detail"]


class _ShortSelectionProvider(_RangeProvider):
    """A selection that stops one character before the sentence ends.

    Exactly what a live document produced: the platform recorded the sentence without
    its final full stop, so ``exact`` cannot reach the stop and any rewrite of the whole
    sentence is refused by one character.
    """

    async def list_comments(self, doc_ref, *, include_resolved=False):
        return [DocComment(
            comment_id="c1", author_is_self=False, author_display_name="X",
            created_time="2026-01-01T00:00:00.000Z", content="这句要解释清楚",
            quoted_text="这一句需要被改写", resolved=False,   # 注意：不含句号
        )]


@pytest.mark.asyncio
async def test_the_chat_path_carries_the_declared_scope_into_the_rail():
    """Without this wiring the tier is a parameter nobody reads.

    The rail has taken ``scope`` since D23 and the unattended tool passes it; the chat
    tool did not, so its window was always the bare selection. Measured live: a person
    marked a sentence, said 「这句」, and every rewrite was refused by the one character
    the selection stopped short of -- the model shrank to two characters and appended a
    parenthetical. Declaring the tier is what the commenter's own word buys.
    """
    prov = _ShortSelectionProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")

    tight = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改好了。", "for_comment_id": "c1"},
    ])
    assert not tight["ok"], "exact 就是选区，句号在它之外"
    assert not prov.edits

    widened = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改好了。",
         "for_comment_id": "c1", "scope": "sentence"},
    ])
    assert widened["ok"], widened
    assert prov.edits, "声明了句档位，整句改写应当落地"


@pytest.mark.asyncio
async def test_an_unknown_scope_never_widens():
    """The fail direction is inward. A typo must not buy a bigger window."""
    prov = _ShortSelectionProvider()
    tk = CloudDocToolkit(prov, turn_doc_id=lambda: "doc-AAAAAAAAAA")
    out = await tk.batch_edit(doc_id="doc-AAAAAAAAAA", edits=[
        {"old_string": "这一句需要被改写。", "new_string": "改好了。",
         "for_comment_id": "c1", "scope": "SENTENCE_TYPO"},
    ])
    assert not out["ok"], "认不出的档位必须落回 exact，而不是放宽"
    assert not prov.edits


@pytest.mark.asyncio
async def test_add_page_is_chat_only_and_refuses_unattended():
    """A comment cannot warrant a page that does not exist yet.

    Structural, not cautious: a comment authorizes the passage it marks, and a new
    worksheet is outside every passage. It is in the unattended denylist by name so no
    future set operation can drag it back in, and it refuses at the call as well.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_ALLOWLIST,
        UNATTENDED_DENYLIST,
    )

    assert "clouddoc_add_page" in UNATTENDED_DENYLIST
    assert "clouddoc_add_page" not in UNATTENDED_ALLOWLIST


@pytest.mark.asyncio
async def test_add_page_returns_the_address_the_platform_chose():
    """The created title is read back, never echoed.

    A platform that renames a duplicate ("台账" -> "台账1") would otherwise hand back an
    address pointing at somebody else's worksheet, and the next write would land there.
    """

    class _Prov:
        receipt_sink = None
        receipt_meta = None

        def parse_doc_ref(self, ref):
            return ref

        async def doc_kind(self, doc_ref):
            return "spreadsheet"

        async def add_page(self, doc_ref, title=""):
            return f"{title}1"          # the platform de-duplicated

    kit = _kit(_Prov())
    out = await kit.add_page(doc_id=DOC, title="台账")
    assert out["ok"] and out["at"] == "台账1"
    assert "台账1" in out["note"]


@pytest.mark.asyncio
async def test_read_says_how_many_pages_a_deck_has():
    """A one-page deck must not read as three pages because it has three text boxes.

    Observed 2026-09-10: asked for a three-page deck, the model read a deck whose
    segments were ``p/i0``, ``p/i1``, ``p/i3``, took the three writable addresses for
    three pages, and wrote all three pages onto one slide. The structure was in the
    addresses -- one page prefix, three objects -- and nothing said so out loud.
    ``clouddoc_add_page`` was registered and available; it was simply never a thing
    the model had reason to reach for.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        DocSnapshot,
        Segment,
    )

    segs = [
        Segment(char_start=0, char_end=0, address="p/i0"),
        Segment(char_start=0, char_end=0, address="p/i1"),
        Segment(char_start=0, char_end=0, address="p/i3"),
    ]

    class _P:
        receipt_sink = None
        receipt_meta = None

        def parse_doc_ref(self, ref):
            return ref

        async def doc_kind(self, doc_ref):
            return "presentation"

        async def read(self, doc_ref):
            return DocSnapshot(doc_id=doc_ref, kind="presentation",
                               revision_id="r1", text="", segments=segs)

    out = await _kit(_P()).read(doc_id=DOC)
    assert out["pages"] == ["p"], out.get("pages")
    assert "有 1 页" in out["structure_note"]
    assert "clouddoc_add_page" in out["structure_note"]


def test_speaker_notes_flatten_with_a_notes_role():
    """A notes run must be distinguishable from a text box; it never was.

    Both flatten as ``pageId/objectId`` runs, so a page holding no text box read as a
    page with one writable region, and a model asked for "page one" wrote the page
    into the notes while the canvas stayed blank (observed 2026-09-10).
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails import textmap

    text, segs = textmap.flatten_slides([
        ("p1", [textmap.Shape("p1", "box", "标题"), textmap.Shape("p1", "n1", "讲稿", is_notes=True)]),
    ])
    roles = {s.address: getattr(s, "role", "") for s in segs}
    assert roles["p1/box"] == "" and roles["p1/n1"] == "notes"


@pytest.mark.asyncio
async def test_read_marks_notes_and_says_they_are_not_slide_content():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot, Segment

    segs = [Segment(char_start=0, char_end=0, address="p/note1", role="notes")]

    class _P:
        receipt_sink = None
        receipt_meta = None
        def parse_doc_ref(self, r): return r
        async def doc_kind(self, d): return "presentation"
        async def read(self, d): return DocSnapshot(doc_id=d, kind="presentation", revision_id="r", text="", segments=segs)

    out = await _kit(_P()).read(doc_id=DOC)
    assert out["cells"][0]["role"] == "notes"
    assert "演讲者备注" in out["structure_note"] and "clouddoc_add_page" in out["structure_note"]


@pytest.mark.asyncio
async def test_write_region_refuses_notes_unless_the_person_asked_for_notes():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSnapshot, EditResult, Segment

    segs = [Segment(char_start=0, char_end=0, address="p/note1", role="notes")]

    class _P:
        receipt_sink = None
        receipt_meta = None
        def __init__(self): self.written = []
        def parse_doc_ref(self, r): return r
        async def doc_kind(self, d): return "presentation"
        async def read(self, d): return DocSnapshot(doc_id=d, kind="presentation", revision_id="r", text="", segments=segs)
        async def write_regions(self, d, pairs, **kw):
            self.written.append(pairs)
            return EditResult("applied", new_revision_id="r2")

    def kit_with(text, prov):
        k = _kit(prov)
        k._user_text = lambda: text
        return k

    p = _P()
    out = await kit_with("把第一页做成协同编辑", p).write_region(doc_id=DOC, at="p/note1", values=[["协同编辑"]])
    assert not out["ok"] and "演讲者备注" in out["detail"] and "clouddoc_add_page" in out["detail"]
    assert p.written == []

    p2 = _P()
    out = await kit_with("把讲稿写进第一页的备注", p2).write_region(doc_id=DOC, at="p/note1", values=[["讲稿"]])
    assert out["ok"] and p2.written


# ------------------------------------ review: the anchor diagnosis keys on the body


@pytest.mark.parametrize("status", ["invalid", "locate_failed", "not_found"])
@pytest.mark.asyncio
async def test_the_anchor_diagnosis_does_not_depend_on_the_providers_status_word(fake, status):
    """No provider answers ``not_found``: Google says ``invalid``, Feishu ``locate_failed``.

    Keyed on that word, the escape repair and the "resending will not work" diagnosis
    were dead on both platforms -- while a fake in this file spoke the phantom status
    and kept them green. The test is now the absence of the anchor from the body,
    which is the fact the diagnosis is about, under every word a provider may use.
    """
    async def refused(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        return EditResult(status, detail="anchor missing")

    fake.edit_batch = refused
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "x")
    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "nowhere to be found", "new_string": "y"}])
    assert out["ok"] is False
    assert "原样重发不会成功" in out["detail"], out["detail"]
    assert "nowhere to be found" in out["detail"]


@pytest.mark.asyncio
async def test_the_escape_repair_runs_whatever_the_provider_calls_a_missing_anchor(fake):
    """The live shape on Google: the provider answers ``invalid`` with its own sentence."""
    body = "Phased Release\nPhase 1 involves internal beta testing.\n"

    async def read(doc_ref):
        return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="rev1", text=body)

    async def edit_batch(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        for old, _new in pairs:
            if old and old not in body:
                return EditResult("invalid", detail=f"{old[:40]!r} not found in the body")
        fake.edits.append((doc_ref, list(pairs)))
        return EditResult("applied", new_revision_id="rev2")

    fake.read = read
    fake.edit_batch = edit_batch
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "x")
    out = await tk.batch_edit(doc_id=DOC, edits=[{
        "old_string": "Phased Release\\nPhase 1 involves internal beta testing.",
        "new_string": "Staged Release\\nStage 1 involves internal beta testing.",
    }])
    assert out["ok"] is True, out
    assert fake.edits[-1][1][0][0] == "Phased Release\nPhase 1 involves internal beta testing."


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_about_an_anchor_keeps_the_providers_reason(fake):
    """An ``invalid`` for another reason -- overlapping edits, a non-text element --
    must not be re-diagnosed as a missing anchor: every old_string is in the body, so
    the provider's own sentence is what the model has to read."""
    async def overlap(doc_ref, pairs, *, required_revision_id=None, window=None, highlight=False):
        return EditResult("invalid", detail="two edits in the proposal overlap")

    fake.edit_batch = overlap
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC], user_text=lambda: "x")
    out = await tk.batch_edit(doc_id=DOC, edits=[{"old_string": "hello", "new_string": "y"}])
    assert out["ok"] is False
    assert "overlap" in out["detail"]
    assert "原样重发不会成功" not in out["detail"]


# ------------------------------------------ review: ids are normalized on both sides


@pytest.mark.asyncio
async def test_a_hyphenated_id_the_user_typed_is_named_in_the_listing_and_the_rail(fake):
    """``user_named`` and the "the user already named X" correction compared a raw id
    against the alnum-only haystack, so a Google id carrying ``-`` or ``_`` -- most do --
    never matched on either. Only the target-id branch had been fixed."""
    hid = "1Ryx-ATR4_h1dV-DBeHS0000"
    fake.accessible = [
        DocSummary(doc_id=hid, title="Hyphen Doc", can_edit=True),
        DocSummary(doc_id=OTHER, title="Q3 Launch Note", can_edit=True),
    ]
    tk = CloudDocToolkit(
        fake, watched_docs=lambda: [hid, OTHER], user_text=lambda: f"write into {hid} please",
    )
    out = await tk.list_documents()
    by_id = {d["doc_id"]: d for d in out["documents"]}
    assert by_id[hid]["user_named"] is True
    assert by_id[OTHER]["user_named"] is False

    refused = await tk.read(doc_id=OTHER)
    assert refused["ok"] is False
    # FakeProvider.title knows neither id, so the correction names the id itself.
    assert refused.get("user_named") == hid, refused


# ------------------------------- review: the listing seeds discovery with the managed ids


@pytest.mark.asyncio
async def test_the_listing_hands_the_managed_ids_to_a_provider_that_takes_them(fake):
    """Feishu derives the wiki space to walk from the nodes it manages. The chat
    listing called discovery bare, so on that platform it depended on a warm cache."""
    seen = {}

    async def listing(known=()):
        seen["known"] = list(known)
        return [DocSummary(doc_id=DOC, title="T", can_edit=True)]

    fake.list_accessible_documents = listing
    tk = CloudDocToolkit(fake, watched_docs=lambda: [DOC, OTHER])
    out = await tk.list_documents()
    assert out["ok"]
    assert seen["known"] == [DOC, OTHER]


def test_every_write_tool_names_the_document_on_its_line():
    """The write tools' line is a receipt, and a receipt that omits which document
    was written is not one. Two of them rendered as a bare verb."""
    from jiuwenswarm.common.tool_display import build_tool_display_name

    for name in ("clouddoc_write_region", "clouddoc_add_page", "clouddoc_apply_for_comment"):
        line = build_tool_display_name(name, {"doc_id": DOC})
        assert DOC[-8:] in line, (name, line)


# ------------------------- review: a reply is refused when resolution cannot be told


@pytest.mark.asyncio
async def test_replying_is_refused_when_resolution_state_cannot_be_read():
    """A provider that fails to list is not "open": answering it as open would reopen
    a closed thread on exactly the transient the guard was meant to survive."""
    prov = _BatchProvider()

    async def failing(doc_ref, *, include_resolved=False):
        raise ProviderError("rate_limited", "quota")

    prov.list_comments = failing
    replies: list = []

    async def reply(doc_ref, comment_id, content):
        replies.append(content)
        return "r1"

    prov.reply_comment = reply
    out = await _kit(prov).reply_comment(doc_id=DOCID, comment_id="c1", content="补一句")
    assert not out["ok"] and "无法确认" in out["detail"]
    assert replies == [], "状态未知时不得回复"


@pytest.mark.asyncio
async def test_replying_to_an_open_comment_proceeds():
    open_c = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="线程", quoted_text="q", resolved=False,
    )
    prov = _BatchProvider(comments=[open_c])
    replies: list = []

    async def reply(doc_ref, comment_id, content):
        replies.append(content)
        return "r1"

    prov.reply_comment = reply
    out = await _kit(prov).reply_comment(doc_id=DOCID, comment_id="c1", content="补一句")
    assert out["ok"] and replies == ["补一句"]


# ------------------------- review: platform failures on the chat write are refusals, not exceptions


class _RaisingProvider(_BatchProvider):
    def __init__(self, exc, *, first=None, body="正文第一句。正文第二句。"):
        super().__init__(body=body)
        self.exc = exc
        self.first = first          # an EditResult to answer the first call with

    async def edit_batch(self, doc_ref, edits, *, required_revision_id, window=None, highlight=False):
        self.submitted.append((list(edits), required_revision_id))
        if self.first is not None and len(self.submitted) == 1:
            return self.first
        raise self.exc


@pytest.mark.asyncio
async def test_a_platform_failure_on_the_chat_write_is_a_structured_refusal():
    prov = _RaisingProvider(ProviderError("rate_limited", "quota exhausted"))
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"]
    assert "rate_limited" in out["detail"] and "本批未落地" in out["detail"]
    assert prov.receipt_meta is None, "回执元数据必须在离开时清掉"


@pytest.mark.asyncio
async def test_an_unconfirmed_chat_write_forbids_a_blind_resend():
    """A write the platform never confirmed may have landed; the one thing the caller
    must not do with that is send the same batch again."""
    prov = _RaisingProvider(ProviderError("unknown", "socket closed mid-request"))
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"]
    assert out.get("status") == "unknown"
    assert "不要原样重发" in out["detail"]


@pytest.mark.asyncio
async def test_the_repaired_resend_reports_platform_failures_the_same_way():
    """The escape-repair retry is a second platform write and gets the same boundary."""
    prov = _RaisingProvider(
        ProviderError("forbidden", "permission revoked"),
        first=EditResult("invalid", detail="anchor not located"),
        body="正文第一句。\n正文第二句。",
    )
    out = await _kit(prov).batch_edit(
        doc_id=DOCID, edits=[{"old_string": "正文第一句。\\n正文第二句。", "new_string": "改后"}])
    assert not out["ok"] and "forbidden" in out["detail"]
    assert len(prov.submitted) == 2, "第一次定位失败后应带修复过的锚点重发一次"
    assert prov.receipt_meta is None


# ------------------------- review: add_page is a write and is gated and recorded like one


class _PageProvider(FakeProvider):
    receipt_sink = object()      # a sink is wired; what it records is the provider's business
    receipt_meta = None

    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome

    async def doc_kind(self, doc_ref):
        return "spreadsheet"

    async def add_page(self, doc_ref, title=""):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.asyncio
async def test_add_page_without_a_receipt_sink_sits_under_the_floor():
    """A page creation is a write; with no sink it is an irreversible one, and under
    mandate with no confirmation channel that is refused (D16), exactly as batch_edit
    and write_region are."""
    prov = _PageProvider("台账")
    prov.receipt_sink = None
    kit = _kit_mode(prov, "mandate", ask_channel=False)
    out = await kit.add_page(doc_id=DOCID, title="台账")
    assert not out["ok"] and "底线拒绝" in out["detail"]


@pytest.mark.asyncio
async def test_add_page_hands_back_the_receipt_id():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import PageAdded

    prov = _PageProvider(PageAdded(address="台账1", receipt_id="rcpt-9"))
    out = await _kit(prov).add_page(doc_id=DOCID, title="台账")
    assert out["ok"] and out["at"] == "台账1" and out["receipt_id"] == "rcpt-9"


@pytest.mark.asyncio
async def test_add_page_reports_a_platform_refusal_as_a_refusal():
    prov = _PageProvider(ProviderError("forbidden", "no edit right"))
    out = await _kit(prov).add_page(doc_id=DOCID, title="台账")
    assert not out["ok"] and "forbidden" in out["detail"]


@pytest.mark.asyncio
async def test_add_page_with_an_unknown_outcome_forbids_a_blind_retry():
    prov = _PageProvider(ProviderError("unknown", "timed out after the request was sent"))
    out = await _kit(prov).add_page(doc_id=DOCID, title="台账")
    assert not out["ok"] and out.get("status") == "unknown"
    assert "不要原样重发" in out["detail"]


# ------------------------- review: the address is the owning connection's, per document


class _RoutedProvider(_BatchProvider):
    """The shape the chat path sees with several connections: the provider knows
    which connection owns a document and can name that connection's identity."""

    def address_for(self, doc_id):
        return "ou_bot" if doc_id == DOCID else ""


@pytest.mark.asyncio
async def test_a_mention_of_the_owning_connection_reads_as_addressed():
    """On a Feishu connection the address is a bot open id, and it is the owning
    connection's address that decides -- not the first connection's email."""
    summoned = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="改这句", quoted_text="q", resolved=False,
        mentioned_addresses=("ou_bot",),
    )
    prov = _RoutedProvider(comments=[summoned])
    kit = _kit(prov, "")                       # no turn address at all: the routed case
    out = await kit.list_comments(doc_id=DOCID)
    assert out["ok"] and out["comments"][0]["addressed"] is True


@pytest.mark.asyncio
async def test_the_writer_mutex_engages_on_the_owning_connections_address():
    summoned = DocComment(
        comment_id="c1", author_is_self=False, author_display_name="X",
        created_time="t", content="改这句", quoted_text="q", resolved=False,
        mentioned_addresses=("ou_bot",),
    )
    prov = _RoutedProvider(comments=[summoned])
    kit = _kit(prov, "")

    async def live(_doc):
        return True

    kit._unattended_live = live
    out = await kit.batch_edit(
        doc_id=DOCID, edits=[{"old_string": "第一句", "new_string": "改后"}])
    assert not out["ok"] and "尚未处理的任务" in out["detail"]
    assert prov.submitted == []
