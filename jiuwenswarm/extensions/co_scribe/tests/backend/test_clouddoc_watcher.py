"""End-to-end watcher tests with a fake provider and a controllable clock."""

from __future__ import annotations

import logging

from dataclasses import replace

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocCapabilities, DocComment, DocReply, DocSnapshot, EditResult, ProviderError,
)
from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import (
    TRANSPORT_TIMEOUT_CEILING, CloudDocCommentWatcher, WatcherConfig,
)
from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import CloudDocStore
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig, dedup_key
from jiuwenswarm.extensions.co_scribe.backend.host.watch.texts import msg as _msg
from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

SA = "co-scribe@x.iam.gserviceaccount.com"
DOC = "doc-1"
BODY = "填充。" * 40 + "这一句需要被改写。" + "尾巴。" * 40


class Clock:
    def __init__(self): self.t = 1_000.0
    def __call__(self): return self.t


class FakeProvider:
    def __init__(self):
        self.text = BODY
        self.revision = "r1"
        self.comments: list[DocComment] = []
        self.replies: list[tuple[str, str]] = []
        self.updates: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self.edit_result = EditResult("applied", new_revision_id="r2")
        self.edit_calls: list[list] = []
        self.edit_windows: list = []
        self.caps = DocCapabilities(
            can_read=True, can_edit=True, can_comment=True, can_resolve=True,
            has_revision_control=True, max_quote_chars=418,
        )
        self.posture: list[tuple[str, str]] = [("user", "writer")]
        # The document's *format*. ``kind`` on a provider already means the
        # platform (google/feishu), so this one cannot borrow that name.
        self.doc_format = "document"

    async def doc_kind(self, doc_ref):
        return self.doc_format

    async def capabilities(self, doc_ref):
        if isinstance(self.caps, Exception):
            raise self.caps
        return self.caps

    async def sharing_posture(self, doc_ref):
        if isinstance(self.posture, Exception):
            raise self.posture
        return self.posture

    @property
    def kind(self): return "fake"
    def parse_doc_ref(self, s): return s
    async def read(self, d):
        return DocSnapshot(doc_id=d, kind="document", revision_id=self.revision, text=self.text)
    async def list_comments(self, d, *, include_resolved=False):
        return [c for c in self.comments if include_resolved or not c.resolved]
    async def edit_batch(self, d, edits, *, required_revision_id, window=None):
        self.edit_windows.append(window)
        self.edit_calls.append(list(edits))
        return self.edit_result
    async def reply_comment(self, d, cid, content):
        self.replies.append((cid, content))
        return f"r{len(self.replies)}"
    async def update_reply(self, d, cid, rid, content):
        self.updates.append((cid, rid, content))
    async def delete_reply(self, d, cid, rid):
        self.deletes.append((cid, rid))


def C(cid, content, *, replies=(), me=False, mentioned=(SA,), quoted="这一句需要被改写。",
      assignee=SA, resolved=False, sa_author=False):
    """A comment that **names us by default**: that is what hands out work, and it is
    the premise of nearly every case here. Pass ``mentioned=()`` for a comment that
    does not summon us.

    It also carries the assignment field, because a real Google comment that mentions
    an account usually does. The field is inert -- the gate does not read it."""
    return DocComment(comment_id=cid, author_is_self=me, author_display_name="X",
                      created_time="2026-01-01T00:00:00.000Z", content=content,
                      quoted_text=quoted, resolved=resolved,
                      mentioned_addresses=tuple(mentioned), replies=tuple(replies),
                      assignee_address=assignee, author_is_service_account=sa_author)


def R(rid, content, t, *, me=False, sa_author=False, mentioned=()):
    return DocReply(reply_id=rid, author_is_self=me, author_display_name="X",
                    created_time=t, content=content,
                    mentioned_addresses=tuple(mentioned),
                    author_is_service_account=sa_author)


def _mandated_registry(tmp_path, *doc_ids):
    """A registry holding one live ``apply_scoped`` mandate per document.

    Rigs used to omit the registry entirely and the watcher dispatched anyway, at
    a "strictest tier". That tier is retired -- no mandate now means no dispatch --
    so any rig that means to exercise dispatch has to carry the mandate. The file
    name is deliberately not ``w.json``: tests that swap in an *empty* registry to
    exercise refusal build one at that path.
    """
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    reg = WatchRegistry(path=tmp_path / "kit-watches.json", rate_max=0)
    for doc_id in (doc_ids or (DOC,)):
        reg.issue(doc_id, "apply_scoped", expires_at=None)
    return reg


@pytest.fixture
async def kit(tmp_path):
    """A steady-state fixture, with the document already watched.

    Cold start -- first watch registers without dispatching -- is a separate matter
    covered by the test_cold_start_* cases. Mixing the two would have seeding eat the
    first tick of every case.
    """
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    reg = _mandated_registry(tmp_path, DOC, "slow-doc", "fast-doc")

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=reg,
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])      # 标记为已纳管，进入稳态
    return w, prov, store, dispatched


@pytest.fixture
async def kit_cold(tmp_path):
    """The same rig **without** seeding, so the first tick is a genuine cold start."""
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(),
    )
    w._docs = [DOC]
    return w, prov, store, dispatched


async def _arm(store, prov):
    """Stand in for a previous tick having run the comment's first turn: the agent has
    spoken in the thread, so its first-turn key is already consumed."""
    await store.mark_triggered(DOC, [dedup_key(DOC, "c1")])


# ------------------------------------------------------------ triggering and dispatch


@pytest.mark.asyncio
async def test_mention_dispatches_once_and_dedups(kit):
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()
    # The rig carries one ``apply_scoped`` mandate -- the only level there is.
    assert len(sent) == 1 and sent[0][2]["clouddoc"] == {"doc_id": DOC, "comment_id": "c1", "mode": "apply_scoped"}
    await w.tick()
    assert len(sent) == 1, "第二轮不得重复派发"


@pytest.mark.asyncio
async def test_metadata_payload_carries_doc_id_every_dispatch(kit):
    """The channel stored on a session is lazy, so the authorization scope must travel with
    every dispatch."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} x", mentioned=[SA]), C("c2", f"@{SA} y", mentioned=[SA])]
    await w.tick()
    assert all(m["clouddoc"]["doc_id"] == DOC for _, _, m in sent)
    # Each turn binds **its own** comment; binding the wrong one would have the agent
    # writing into someone else's thread
    assert [m["clouddoc"]["comment_id"] for _, _, m in sent] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_authorization_payload_never_carries_untrusted_text(kit):
    """The ``clouddoc`` sub-dictionary is the cross-process authorization scope and admits
    **only server-generated opaque ids**.

    Drive generates both doc_id and comment_id, and nobody can write into them, while the
    prompt and the comment body must stay in ``prompt`` and travel via params.content.
    Merging the two would put text written by anyone with comment access into the same
    dictionary the agentserver reads to decide which document and which comment this turn
    is authorized for.
    """
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} 注入尝试", mentioned=[SA], quoted="被引用的原文")]
    await w.tick()
    _, _, payload = sent[0]
    assert set(payload["clouddoc"]) == {"doc_id", "comment_id", "mode"}, (
        "授权域只含服务端生成值:两个 id + watch 档位(IC-1)"
    )
    assert "注入尝试" not in repr(payload["clouddoc"])
    # The comment body does reach the model, just under a different key
    assert "注入尝试" in payload["prompt"]


@pytest.mark.asyncio
async def test_untrusted_fence_nonce_differs_per_turn(kit):
    """The fence nonce is regenerated each turn: a fixed marker can be copied into a comment
    to close the fence early."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} x", mentioned=[SA]), C("c2", f"@{SA} y", mentioned=[SA])]
    await w.tick()
    import re

    fences = [re.findall(r"\[UNTRUSTED-([0-9a-f]+)\]", m["prompt"])[0] for _, _, m in sent]
    assert len(set(fences)) == len(fences), "两轮用了同一个 nonce"


# ------------------------------------------------------------ follow-ups under an answered comment


@pytest.mark.asyncio
async def test_a_follow_up_mention_consumes_its_own_key(kit):
    """The reply's key is stored when its turn is dispatched, or a crash between the
    two dispatches the same reply again."""
    w, prov, store, sent = kit
    await _arm(store, prov)
    prov.comments = [C("c1", "x", replies=[R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True), R("r1", "同意", "2026-01-01T00:00:09.000Z", mentioned=(SA,))])]
    await w.tick()
    assert await store.is_triggered(DOC, f"clouddoc:{DOC}:c1:r1") is True



@pytest.mark.asyncio
async def test_the_agents_own_reply_never_summons_it(kit):
    """Whatever the agent writes under a comment, it is not a summons: an agent
    answering itself is the loop the prohibition exists to stop."""
    w, prov, store, sent = kit
    await _arm(store, prov)
    prov.comments = [C("c1", "x", replies=[R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True), R("r1", "同意", "2026-01-01T00:00:09.000Z", me=True)])]
    await w.tick()
    assert sent == [] and prov.edit_calls == []



@pytest.mark.asyncio
async def test_a_follow_up_mention_dispatches_a_turn_and_the_watcher_writes_nothing(kit):
    w, prov, store, sent = kit
    await _arm(store, prov)
    prov.comments = [C("c1", "x", replies=[
        R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True),
        R("r1", "同意，但第二处再改改", "2026-01-01T00:00:09.000Z", mentioned=(SA,)),
    ])]
    await w.tick()
    assert prov.edit_calls == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_the_watcher_never_writes_the_document_itself(kit):
    """Every write happens inside a dispatched turn, through the toolkit and its rails.
    The watcher's own loop holds no write path, whatever a reply under the comment
    says -- an approval word included.
    """
    w, prov, store, sent = kit
    await _arm(store, prov)
    prov.comments = [C("c1", "x", replies=[R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True), R("r1", "同意", "2026-01-01T00:00:09.000Z", mentioned=(SA,))])]
    await w.tick()
    assert len(sent) == 1, "the mention dispatches a turn"
    assert prov.edit_calls == [], "and the watcher itself writes nothing"


# ------------------------------------------------------------ startup sweep


@pytest.mark.asyncio
async def test_sweep_recovers_two_orphan_classes(kit):
    w, prov, store, sent = kit
    await store.mark_triggered(DOC, ["c1:-"])
    await store.begin_inflight(DOC, "t1", ["c1:-"], {"c1": "rp1"})   # 派发后崩
    await store.begin_inflight(DOC, "t2", ["c9:-"], {"c9": "rp9"})   # 派发前崩

    actions = await w.sweep()
    assert len(actions) == 2
    assert any("处理中断" in t and "@ 我一句" in t for _, _, t in prov.updates), \
        "派发后崩的占位须改写成请人 @ 我"
    assert await store.list_inflight(DOC) == {}


# ------------------------------------------------------------ resilience


@pytest.mark.asyncio
async def test_rate_limit_does_not_mark_document_failed(kit):
    """Quota is shared globally: feed throttling into a per-document state machine and one
    person hammering one document freezes them all."""
    w, prov, store, sent = kit

    async def boom(*a, **k):
        raise ProviderError("rate_limited", "quota")

    prov.list_comments = boom
    for _ in range(5):
        await w.tick()
    assert (await store.snapshot())[DOC]["backoff"]["failed"] is False


@pytest.mark.asyncio
async def test_permission_failure_marks_failed(kit):
    """Three consecutive permission errors set failed, and **each must wait out the backoff
    window**.

    Without advancing the clock the second and third ticks are skipped by backoff and the
    count never reaches three -- which is exactly what working backoff looks like. Only a
    test that drives the clock observes the real sequence.
    """
    w, prov, store, sent = kit

    async def boom(*a, **k):
        raise ProviderError("forbidden", "no access")

    prov.list_comments = boom
    clock = w._now_fn
    for _ in range(3):
        await w.tick()
        clock.t += 3600          # 越过退避窗口（上限 15min）
    assert (await store.snapshot())[DOC]["backoff"]["failed"] is True


@pytest.mark.asyncio
async def test_backoff_window_skips_polling_until_it_expires(kit):
    """No API calls inside the backoff window. Without reading it, a document whose access was
    revoked retries every cycle forever."""
    w, prov, store, sent = kit
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        raise ProviderError("forbidden", "no access")

    prov.list_comments = boom
    await w.tick()
    assert calls["n"] == 1
    await w.tick()                      # 同一时刻：应被跳过
    await w.tick()
    assert calls["n"] == 1, "退避窗口内仍在打 API"

    w._now_fn.t += 3600                 # 窗口过期
    await w.tick()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_success_clears_the_failure_counter(kit):
    """One success resets the count. Otherwise it only grows, and network flapping lifts the
    permission test past usefulness -- observed in production at 10, all of them Errno 49."""
    w, prov, store, sent = kit
    ok = prov.list_comments

    async def boom(*a, **k):
        raise ProviderError("transport", "网络抖动")

    prov.list_comments = boom
    await w.tick()
    assert (await store.snapshot())[DOC]["backoff"]["failures"] == 1

    prov.list_comments = ok
    w._now_fn.t += 3600
    await w.tick()
    b = (await store.snapshot())[DOC]["backoff"]
    assert b["failures"] == 0 and b["until"] is None


@pytest.mark.asyncio
async def test_rate_limiting_backs_off_without_freezing(kit):
    """Rate limiting is a **global** signal wearing per-document clothing: slow down, but do
    not advance failed.

    Let it add to failures and, after someone else exhausts the quota, the count climbs
    and the first genuine 403 after that freezes the document immediately.
    """
    w, prov, store, sent = kit

    async def boom(*a, **k):
        raise ProviderError("rate_limited", "quota")

    prov.list_comments = boom
    for _ in range(5):
        await w.tick()
        w._now_fn.t += 3600
    b = (await store.snapshot())[DOC]["backoff"]
    assert b["failures"] == 0, "限流不得累加每文档失败计数"
    assert b["failed"] is False
    assert b["until"] is not None, "限流仍必须推进退避窗口"


def test_turn_timeout_is_clamped_below_transport_ceiling():
    """Matching the transport's hard cap races it with an unpredictable exception type; going
    higher makes the wait_for here dead code."""
    assert WatcherConfig(turn_timeout_seconds=900).clamped_turn_timeout() < TRANSPORT_TIMEOUT_CEILING
    assert WatcherConfig().clamped_turn_timeout() == 540.0


# ------------------------------------------------------------ read-back and settling
#
# Every case in this group came from a gap found in live use: the model answering in
# plain text, a proposal posted but never stored, and someone mentioning the agent and
# seeing no response at all. None of the three is a wrong computation; each is a right one
# that was never connected back.





@pytest.mark.asyncio
async def test_placeholder_is_posted_before_dispatch(kit):
    """Someone who mentions the agent must see an acknowledgement at once, and the placeholder
    id is also crash recovery's only handle."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()
    assert prov.replies and prov.replies[0][1].startswith("⏳")


@pytest.mark.asyncio
async def test_turn_text_lands_in_the_thread_even_without_a_tool_call(kit):
    """Measured: the model often answers in plain text without calling reply_comment. Not
    writing that back is the same as not answering."""
    w, prov, store, sent = kit

    async def dispatch(doc_id, comment_id, payload):
        sent.append((doc_id, comment_id, payload))
        return "这句可以改成「输出缺乏事实依据」。"

    w._dispatch = dispatch
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()

    assert any("输出缺乏事实依据" in text for _, _, text in prov.updates), \
        "轮次文本没有落回线程"


@pytest.mark.asyncio
async def test_empty_turn_result_is_reported_not_left_pending(kit):
    """A timeout or transport failure returns an empty string, and leaving the working
    placeholder in place would stall there forever."""
    w, prov, store, sent = kit

    async def dispatch(doc_id, comment_id, payload):
        return ""

    w._dispatch = dispatch
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()
    assert any("@ 我一句" in text for _, _, text in prov.updates), prov.updates


@pytest.mark.asyncio
async def test_inflight_records_the_placeholder_for_crash_recovery(kit):
    """sweep() uses the placeholder id in the inflight record to rewrite an interrupted turn
    into something visible."""
    w, prov, store, sent = kit
    seen = {}

    async def dispatch(doc_id, comment_id, payload):
        seen.update(await store.list_inflight(DOC))
        return "好"

    w._dispatch = dispatch
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()

    assert seen, "派发期间必须有 inflight 记录"
    rec = next(iter(seen.values()))
    assert rec["placeholders"] == {"c1": "r1"}
    assert await store.list_inflight(DOC) == {}, "轮次结束必须清空"




@pytest.mark.asyncio
async def test_follow_up_turns_get_no_placeholder(kit):
    """Placeholders go out only on first touch.

    The person has just replied and the agent answers within a minute, so a placeholder
    carries no information, while one per iteration would flood the thread.
    """
    w, prov, store, sent = kit
    await _arm(store, prov)
    prov.comments = [C("c1", "x", replies=[
        R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True),
        R("r1", "再改改这里", "2026-01-01T00:00:09.000Z", mentioned=(SA,)),
    ])]
    await w.tick()
    assert sent, "follow-up 应当派发"
    assert not any(t.startswith("⏳") for _, t in prov.replies), "follow-up 不该发占位"


@pytest.mark.asyncio
async def test_placeholder_failure_aborts_the_dispatch_and_returns_the_key(kit):
    """A failed placeholder abandons the dispatch -- dispatching anyway yields a turn
    with no placeholder to write its final state into, and crash recovery loses its
    only handle -- and gives the key back, so a transient failure costs one poll
    rather than the summons.
    """
    w, prov, store, sent = kit
    real_reply = prov.reply_comment
    calls = {"n": 0}

    async def boom_once(d, cid, content):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("transport", "503")
        return await real_reply(d, cid, content)

    prov.reply_comment = boom_once
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()
    assert sent == [], "占位失败仍然派发了"
    assert await store.is_triggered(DOC, f"clouddoc:{DOC}:c1:-") is False, "键必须交还"
    await w.tick()
    assert len(sent) == 1, "下一轮必须重试并派发"
    assert await store.is_triggered(DOC, f"clouddoc:{DOC}:c1:-") is True


@pytest.mark.asyncio
async def test_one_slow_document_does_not_stall_the_others(kit):
    """Trouble with one document must not stall polling for the rest.

    Dispatch awaits inside the tick, so walking documents serially lets one slow turn --
    up to 540s -- block every other document.
    """
    import asyncio as _asyncio

    w, prov, store, sent = kit
    w._docs = ["slow-doc", "fast-doc"]
    for doc in w._docs:
        await store.seed_if_new(doc, [])   # 两篇都进入稳态
    started: list[str] = []

    async def dispatch(doc_id, comment_id, payload):
        started.append(doc_id)
        if doc_id == "slow-doc":
            await _asyncio.sleep(0.25)
        return "ok"

    w._dispatch = dispatch
    prov.comments = [C("c1", f"@{SA} x", mentioned=[SA])]
    await _asyncio.wait_for(w.tick(), timeout=2)
    assert set(started) == {"slow-doc", "fast-doc"}
    # Were it serial, fast-doc could not start until slow-doc's sleep had finished
    assert started.index("fast-doc") <= 1, f"fast-doc 被慢文档卡住了: {started}"


# ------------------------------------------------------------ cold start and error sanitisation
#
# Both groups came from code review rather than an incident -- but the first group's
# consequence is exactly the class we already hit with a missing migration replaying
# history, on a trigger that is more common still: switching the feature on.


@pytest.mark.asyncio
async def test_cold_start_seeds_history_without_dispatching(tmp_path):
    """First watch of a document with history: register every trigger point and dispatch
    nothing.

    Without this, switching the feature on replays the document's history -- every past
    mention dispatches a turn and posts.
    """
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    sent: list = []

    async def dispatch(d, c, p):
        sent.append(c)
        return "ok"

    w = CloudDocCommentWatcher(prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
                               dispatch=dispatch, now_fn=Clock(),
                               registry=_mandated_registry(tmp_path))
    w._docs = [DOC]
    prov.comments = [C(f"old{i}", f"@{SA} 半年前的评论", mentioned=[SA]) for i in range(5)]

    await w.tick()
    assert sent == [], f"冷启动派发了 {len(sent)} 轮"
    assert prov.replies == [], "冷启动往文档里发帖了"

    # The second tick reaches steady state: history still does not trigger, a new comment
    # does
    await w.tick()
    assert sent == []
    prov.comments.append(C("new1", f"@{SA} 新评论", mentioned=[SA]))
    await w.tick()
    assert sent == ["new1"]


@pytest.mark.asyncio
async def test_seeding_happens_once_even_after_dedup_window_expires(tmp_path):
    """The test is the seeded flag, not whether triggered_ids is empty.

    With the latter, the history is replayed a second time once the 30-day window has
    evicted every key.
    """
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    assert await store.seed_if_new(DOC, ["k1"]) is True
    assert await store.seed_if_new(DOC, ["k2"]) is False, "第二次不该再 seed"


@pytest.mark.asyncio
async def test_internal_errors_are_not_written_into_the_document(kit):
    """Errors are sanitised: a failed turn writes generic wording plus a reference that
    reconciles with the log, and detail stays in the log.

    What happened: an UnboundLocalError from the agent runtime was pasted verbatim into a
    user's document.
    """
    w, prov, store, sent = kit

    async def dispatch(d, c, p):
        return ""      # dispatcher 对失败一律返回空串

    w._dispatch = dispatch
    prov.comments = [C("c1", f"@{SA} 改这句", mentioned=[SA])]
    await w.tick()

    written = [t for _, _, t in prov.updates]
    assert written, "占位没有被收尾"
    assert "@ 我一句" in written[0]
    assert "编号" in written[0], "缺少与日志对账的编号"
    assert "Traceback" not in written[0] and "local variable" not in written[0]


@pytest.mark.asyncio
async def test_two_replies_in_one_tick_get_distinct_inflight_records(kit):
    """Two new replies on one comment within a tick must not collide on turn_id.

    A second-resolution timestamp has them share one inflight record: the later overwrites
    the earlier, whichever finishes first deletes the record, and the other's placeholder
    stays "working on it" forever.
    """
    w, prov, store, sent = kit
    seen: list[dict] = []

    async def dispatch(d, c, p):
        sent.append((d, c, p))
        seen.append(await store.list_inflight(DOC))
        return "ok"

    w._dispatch = dispatch
    await _arm(store, prov)
    # The agent must already have spoken: the first turn answers a thread as a whole, so
    # two replies only become two turns once there is an anchor to be newer than.
    prov.comments = [C("c1", "x", replies=[
        R("rp1", "我先回一句。", "2026-01-01T00:00:05.000Z", me=True),
        R("r1", "先说一句", "2026-01-01T00:00:09.000Z", mentioned=(SA,)),
        R("r2", "再说一句", "2026-01-01T00:00:10.000Z", mentioned=(SA,)),
    ])]
    await w.tick()
    assert len(sent) == 2, f"应派发两轮，实际 {len(sent)}"
    # By the second dispatch the first has finished; what matters is that the two used
    # different keys
    keys = {k for snap in seen for k in snap}
    assert len(keys) == 2, f"两轮共用了同一个 turn_id: {keys}"



def _conv(text="句子要短。"):
    return C("k1", f"co-scribe 约定：{text}")


@pytest.mark.asyncio
async def test_conventions_are_acknowledged_on_first_effect(kit):
    """The trigger is first **taking effect**, not first being seen: nothing should be written
    into a document nobody triggered."""
    w, prov, store, sent = kit
    prov.comments = [_conv()]
    await w.tick()
    assert prov.replies == [], "没有触发时就回帖了"

    prov.comments.append(C("c1", f"@{SA} 改这句", mentioned=[SA]))
    await w.tick()
    acks = [t for _, t in prov.replies if "已读取本文档约定" in t]
    assert len(acks) == 1, prov.replies


@pytest.mark.asyncio
async def test_acknowledgement_is_not_repeated_for_unchanged_conventions(kit):
    w, prov, store, sent = kit
    prov.comments = [_conv(), C("c1", f"@{SA} x", mentioned=[SA])]
    await w.tick()
    prov.comments.append(C("c2", f"@{SA} y", mentioned=[SA]))
    await w.tick()
    acks = [t for _, t in prov.replies if "已读取本文档约定" in t]
    assert len(acks) == 1, f"重复确认了 {len(acks)} 次"


@pytest.mark.asyncio
async def test_rewritten_conventions_are_acknowledged_again(kit):
    """The test is a content hash: recording only "acknowledged once" lets every later edit
    take effect in silence."""
    w, prov, store, sent = kit
    prov.comments = [_conv(), C("c1", f"@{SA} x", mentioned=[SA])]
    await w.tick()

    prov.comments[0] = _conv("句子要长。用全称。")
    prov.comments.append(C("c2", f"@{SA} y", mentioned=[SA]))
    await w.tick()
    acks = [t for _, t in prov.replies if "已读取本文档约定" in t]
    assert len(acks) == 2, f"约定改写后没有重新确认: {acks}"


# ------------------------------------------------------------------ admission


async def _admitted(tmp_path, prov):
    """Run one tick and return (watcher, store, dispatched). Structurally the same as kit, but
    the caller configures the provider's capabilities and sharing posture first, so that
    fixture cannot be reused."""
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=_mandated_registry(tmp_path),
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    prov.comments = [C("c1", f"@{SA} 改一下", mentioned=(SA,))]
    await w.tick()
    return w, store, dispatched


@pytest.mark.asyncio
async def test_comment_only_access_is_refused_and_never_polled_again(tmp_path):
    """Comment-only access fails silently, so it has to fail fast.

    This is the most dangerous configuration mistake: comments read, proposals post,
    people see them, but Docs stops returning a revisionId and **no edit can ever land**.
    The user concludes the feature is broken or the model is poor, and does not think of
    permissions. The temptation is natural too -- "I don't want it changing my document."
    """
    prov = FakeProvider()
    prov.caps = replace(prov.caps, can_edit=False, has_revision_control=False)
    w, store, dispatched = await _admitted(tmp_path, prov)

    assert dispatched == [], "拒绝纳管的文档不应派发任何轮次"
    assert await store.is_frozen(DOC, 0.0), "必须立刻冻结，而不是每 15 秒重试到永远"

    # Once frozen, not even the admission check should go out, or a refusal turns into two
    # API calls per tick.
    prov.caps = ProviderError("forbidden", "should not be asked again")
    await w.tick()


@pytest.mark.asyncio
async def test_admission_is_checked_once_per_process_not_every_tick(tmp_path):
    """Admission is a startup check, not a per-tick heartbeat.

    Losing permission mid-run is not detected here; that surfaces as a 403 and the
    per-document backoff advances failed. Checking every tick would add two API calls to
    every poll of every document, purely burning quota.
    """
    prov = FakeProvider()
    calls = []
    orig = prov.capabilities

    async def counted(doc_ref):
        calls.append(doc_ref)
        return await orig(doc_ref)

    prov.capabilities = counted
    w, _, _ = await _admitted(tmp_path, prov)
    await w.tick()
    await w.tick()
    assert len(calls) == 1, f"准入检查跑了 {len(calls)} 次，应只跑一次"


@pytest.mark.asyncio
async def test_transient_capability_error_does_not_freeze_the_document(tmp_path):
    """The admission check failing is not the same as insufficient permission.

    Network flapping and throttling both make capabilities raise. Reading that as "no edit
    rights" would permanently freeze a perfectly configured document over one timeout, and
    recovery would need a person.
    """
    prov = FakeProvider()
    prov.caps = ProviderError("rate_limited", "429")
    w, store, dispatched = await _admitted(tmp_path, prov)
    assert dispatched == []
    assert not await store.is_frozen(DOC, 0.0), "瞬态错误不应冻结文档"

    prov.caps = DocCapabilities(
        can_read=True, can_edit=True, can_comment=True, can_resolve=True,
        has_revision_control=True, max_quote_chars=418,
    )
    await w.tick()
    assert dispatched, "恢复后应正常工作"


@pytest.mark.asyncio
async def test_unknown_sharing_posture_does_not_block_a_writable_document(tmp_path):
    """An unreadable sharing posture is reported as **unknown**, treated neither as safe nor
    as fatal.

    permissions.list needs more permission than editing does, and a non-owner often cannot
    read it. Letting it decide admission would require every deployer to own the document.
    """
    prov = FakeProvider()
    prov.posture = ProviderError("forbidden", "not the owner")
    _, store, dispatched = await _admitted(tmp_path, prov)
    assert dispatched, "共享态未知不应阻断一篇可写文档"
    assert not await store.is_frozen(DOC, 0.0)


@pytest.mark.asyncio
async def test_link_shared_document_is_warned_about_but_still_watched(tmp_path, caplog):
    """Anyone-with-the-link is a warning, not a refusal.

    The sharing list is the authorization list: whoever can comment can trigger the agent
    and approve its proposals. That is the document owner's decision to make, not
    Co-scribe's to make for them -- but they need to know they have made it.
    """
    prov = FakeProvider()
    prov.posture = [("anyone", "writer")]
    with caplog.at_level(logging.WARNING):
        _, _, dispatched = await _admitted(tmp_path, prov)
    assert dispatched, "链接共享不应阻断纳管"
    assert any("任何人" in r.getMessage() for r in caplog.records), "必须告警"


# ------------------------------------------------------------------ a restart re-checks


async def _fresh_process(tmp_path, prov, store=None):
    """Simulate a process restart: the same state file, a fresh watcher with a fresh
    _thaw_tried."""
    store = store or CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[str] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append(comment_id)
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=_mandated_registry(tmp_path),
    )
    w._docs = [DOC]
    return w, store, dispatched


@pytest.mark.asyncio
async def test_fixed_permission_thaws_on_next_process_start(tmp_path):
    """A freeze is a memory, not a sentence: once the user fixes the permission, restarting the
    process recovers.

    Without this, nothing in the code can clear failed once it is persisted -- the freeze
    check short-circuits admission permanently, "clear on success" never sees that
    success, and the only way out becomes removing the document from the config and adding
    it back, which nobody would find.
    """
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "comment_only_access")

    prov = FakeProvider()  # 默认能力可编辑 = 用户已修好权限
    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    prov.comments = [C("c1", f"@{SA} 改一下", mentioned=(SA,))]
    await w.tick()

    assert dispatched, "修好权限后，新进程的首个 tick 应恢复派发"
    assert not await store.is_frozen(DOC, 0.0), "解冻必须落盘，否则下一 tick 又被短路"


@pytest.mark.asyncio
async def test_still_broken_doc_is_rechecked_once_per_process(tmp_path):
    """A document still broken costs one extra capabilities call per process and does not decay
    into retrying every tick."""
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "comment_only_access")

    prov = FakeProvider()
    prov.caps = replace(prov.caps, can_edit=False, has_revision_control=False)
    calls: list[str] = []
    orig = prov.capabilities

    async def counted(doc_ref):
        calls.append(doc_ref)
        return await orig(doc_ref)

    prov.capabilities = counted
    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    await w.tick()
    await w.tick()
    await w.tick()

    assert len(calls) == 1, f"重检跑了 {len(calls)} 次，应每进程一次"
    assert await store.is_frozen(DOC, 0.0), "没修好就不得解冻"
    assert dispatched == []


@pytest.mark.asyncio
async def test_rate_limit_backoff_is_not_bypassed_by_restart(tmp_path):
    """The backoff window is a rate-limiting rhythm, and a restart is not permission to step
    around it.

    Otherwise a storm of restarts -- a crash loop, a rolling deploy -- clears every backoff
    and drives requests at full rate exactly when quota is tightest. The verdict bit,
    failed, and the rhythm bit, until, have to be treated separately.
    """
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_failure(DOC, "rate_limited", now=9e12)  # 窗口远在未来

    prov = FakeProvider()
    calls: list[str] = []
    orig = prov.capabilities

    async def counted(doc_ref):
        calls.append(doc_ref)
        return await orig(doc_ref)

    prov.capabilities = counted
    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    prov.comments = [C("c1", f"@{SA} 改一下", mentioned=(SA,))]
    await w.tick()

    assert calls == [], "退避窗口内不得因为是新进程就发起重检"
    assert dispatched == []


@pytest.mark.asyncio
async def test_thaw_lands_even_if_first_tick_fails_midway(tmp_path):
    """Thawing depends on the fact the re-check established, not on whether the first tick
    completed.

    The moment admission says healthy, the fact the freeze rested on no longer holds. Were
    thawing to depend on the clear-on-success at the end of a tick, one unrelated transient
    -- network flapping, a failed comment fetch -- would refreeze a document that is
    demonstrably fixed for another whole process lifetime. From the user's side that reads
    as "restarting doesn't help either", which is the very thing this mechanism exists to
    remove.
    """
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "comment_only_access")

    prov = FakeProvider()  # 能力健康

    async def boom(*a, **k):
        raise RuntimeError("transient outage after admission")

    prov.list_comments = boom
    w, store, _ = await _fresh_process(tmp_path, prov, store)
    await w.tick()

    assert not await store.is_frozen(DOC, 0.0), "准入已通过，解冻不得被后续故障连坐"


# ------------------------------------------------------------------ replies follow the comment's language


@pytest.mark.asyncio
async def test_replies_follow_the_comment_language(kit):
    """System text follows the language of the triggering comment; the reader is the person in
    the document.

    """
    w, prov, store, sent = kit

    async def empty_dispatch(doc_id, comment_id, metadata):
        return ""   # 模型空手而归

    w._dispatch = empty_dispatch

    prov.comments = [C("c1", f"@{SA} Please improve this sentence", mentioned=[SA])]
    await w.tick()
    texts = [t for _, t in prov.replies] + [t for _, _, t in prov.updates]
    assert any("Working on it" in t for t in texts), texts
    assert any("didn't complete" in t for t in texts), texts

    prov.replies.clear()
    prov.updates.clear()
    prov.comments = [C("c2", f"@{SA} 改一下这句", mentioned=[SA])]
    await w.tick()
    texts = [t for _, t in prov.replies] + [t for _, _, t in prov.updates]
    assert any("正在处理" in t for t in texts), texts
    assert any("没有完成" in t for t in texts), texts


# ------------------------------------------------------------ verdicts with nothing to judge




@pytest.mark.asyncio
async def test_placeholder_is_retracted_when_the_model_posted_its_own_reply(kit):
    """Two posts under one comment is what this prevents: the model's own reply, and
    the placeholder overwritten with its closing narration about having written it."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", "改一下这句")]

    async def dispatch(doc_id, comment_id, metadata):
        prov.comments = [C("c1", "改一下这句", replies=[
            R("m1", "我先回一句。", "2026-01-01T00:00:07.000Z", me=True),
        ])]
        return "I have replied in the thread."
    w._dispatch = dispatch

    await w.tick()
    assert prov.deletes == [("c1", "r1")], prov.deletes
    posted = [t for _, t in prov.replies] + [t for _, _, t in prov.updates]
    assert not any("I have replied" in t for t in posted), posted


@pytest.mark.asyncio
async def test_another_agents_reply_mid_turn_is_not_the_model_having_spoken(kit):
    """A second bot replying under the comment while the turn runs must not read as
    this model's own post: the fallback write-back would be dropped and the
    placeholder deleted over an answer nobody posted."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", "改一下这句")]

    async def dispatch(doc_id, comment_id, metadata):
        prov.comments = [C("c1", "改一下这句", replies=[
            R("r1", "⏳ 正在处理…", "2026-01-01T00:00:06.000Z", me=True),
            R("b1", "另一个机器人插话。", "2026-01-01T00:00:07.000Z", sa_author=True),
        ])]
        return "The sentence looks fine to me."
    w._dispatch = dispatch

    await w.tick()
    assert prov.deletes == [], "占位不得因他人的帖子被删"
    assert prov.updates and prov.updates[-1][2] == "The sentence looks fine to me."


@pytest.mark.asyncio
async def test_the_answer_is_still_written_when_the_model_posted_nothing(kit):
    """The fallback this write-back exists for: the model answered in plain text and
    called no tool, so without it the person who mentioned the agent sees nothing."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", "改一下这句")]

    async def dispatch(doc_id, comment_id, metadata):
        # By now the placeholder is a real reply in the thread -- the watcher posted it
        # before the turn started, and it is authored by the service account like every
        # other post of ours. Counting it as the model having spoken would suppress the
        # write-back on **every** first-touch turn, which is the whole fallback.
        prov.comments = [C("c1", "改一下这句", replies=[
            R("r1", "⏳ 正在处理…", "2026-01-01T00:00:06.000Z", sa_author=True),
        ])]
        return "The sentence looks fine to me."
    w._dispatch = dispatch

    await w.tick()
    assert prov.deletes == []
    assert prov.updates and prov.updates[-1][2] == "The sentence looks fine to me."


@pytest.mark.asyncio
async def test_failed_thaw_refreezes_at_once_instead_of_recounting(tmp_path):
    """The thaw re-check failing with the same permanent kind is confirmation, not a
    fresh streak. Counted from one again, the document needed two more failing ticks
    to re-freeze, and for those two polling cycles the panel showed the row healthy."""
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "forbidden")

    prov = FakeProvider()

    async def still_forbidden(d, *, include_resolved=False):
        raise ProviderError("forbidden", "Forbidden")
    prov.list_comments = still_forbidden

    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    await w.tick()

    assert await store.is_permanently_failed(DOC), "解冻复查再撞永久错误，必须立刻恢复冻结"
    assert dispatched == []

    # And it stays frozen on the next tick -- no second thaw this process.
    await w.tick()
    assert await store.is_permanently_failed(DOC)


@pytest.mark.asyncio
async def test_transient_error_during_thaw_does_not_refreeze(tmp_path):
    """Only the same permanent kinds confirm the freeze. A network blip during the one
    thaw attempt burns the attempt (documented cost) but must not mark the document
    permanently failed -- the next restart still gets its chance."""
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "forbidden")

    prov = FakeProvider()

    async def flaky(d, *, include_resolved=False):
        raise ProviderError("transport", "Errno 49")
    prov.list_comments = flaky

    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    await w.tick()

    assert not await store.is_permanently_failed(DOC), "瞬时错误不得当作永久失败的确认"


# ------------------------------------------------------------ conflict retry




def test_narration_filter_catches_the_observed_shapes():
    from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import reads_as_dispatcher_narration
    assert reads_as_dispatcher_narration(
        'Since the user has acknowledged my previous response ("ok, I got it"), '
        "no further action is required for this specific comment."
    )
    assert reads_as_dispatcher_narration("我已回复该评论，说明了估算的来源。")
    # Real answers must pass.
    assert not reads_as_dispatcher_narration("Yes, we need budget approval before Stage 2.")
    assert not reads_as_dispatcher_narration("The Product Team will own the fallback design.")


@pytest.mark.asyncio
async def test_follow_up_narration_is_dropped_not_posted(kit):
    """A follow-up turn that closes with dispatcher narration writes nothing into the
    thread. Observed on camera: the user replied "it's ok", and the fallback posted
    third-person prose about the user into the user's own document."""
    w, prov, store, sent = kit
    # First turn: the comment itself. Model answers via tool (recorded in replies).
    prov.comments = [C("c1", f"@{SA} which vendor is this?", mentioned=[SA])]

    async def first_turn(doc_id, comment_id, metadata):
        prov.comments = [C("c1", f"@{SA} which vendor is this?", mentioned=[SA], replies=[
            R("m1", "I don't have that information.", "2026-01-01T00:00:06.000Z", me=True),
        ])]
        return "I don't have that information."
    w._dispatch = first_turn
    await w.tick()
    posted_before = len(prov.replies) + len(prov.updates)

    # Follow-up: the person acknowledges; the model calls no tool and closes with
    # narration.
    prov.comments = [C("c1", f"@{SA} which vendor is this?", mentioned=[SA], replies=[
        R("m1", "I don't have that information.", "2026-01-01T00:00:06.000Z", me=True),
        R("h1", "ok, I got it", "2026-01-01T00:00:09.000Z", mentioned=(SA,)),
    ])]

    async def follow_up(doc_id, comment_id, metadata):
        return ("Since the user has acknowledged my previous response, "
                "no further action is required for this specific comment.")
    dispatched2: list[str] = []
    async def follow_up_counted(doc_id, comment_id, metadata):
        dispatched2.append(comment_id)
        return await follow_up(doc_id, comment_id, metadata)
    w._dispatch = follow_up_counted
    await w.tick()

    assert dispatched2, "后续回合必须真的派发——否则本测试在空转"
    texts = [t for _, t in prov.replies] + [t for _, _, t in prov.updates]
    assert not any("no further action" in t for t in texts), texts
    assert len(prov.replies) + len(prov.updates) == posted_before, "旁白不得发进线程"


@pytest.mark.asyncio
async def test_first_touch_narration_still_resolves_the_placeholder(kit):
    """On a first touch the placeholder must resolve to something: dropping there would
    leave a permanent "Working on it...". The filter applies to follow-ups only."""
    w, prov, store, sent = kit
    prov.comments = [C("c1", f"@{SA} 帮忙看看", mentioned=[SA])]

    async def dispatch(doc_id, comment_id, metadata):
        return "The user is asking for help; I will reply to the comment."
    w._dispatch = dispatch
    await w.tick()
    assert prov.updates, "首触的 placeholder 必须被写成最终内容"
    assert "I will reply" in prov.updates[-1][2], prov.updates


# ---------------------------------------------------------- no proposal path (PR2c)
# The proposal machinery is gone: a proposal-shaped reply, from anyone, creates no
# state and has no later application path.


async def test_hand_posted_proposal_format_reply_arms_nothing(kit):
    # With ingest gone, even a perfectly formatted proposal-shaped reply (from anyone)
    # creates no state and no later application path.
    w, prov, store, dispatched = kit
    prov.comments = [C("c1", "x", replies=[
        R("rp1", "【拟议修改 1/1】\n原文：这一句需要被改写。\n拟改为：坏。", "2026-01-01T00:00:05.000Z", me=True),
        R("r1", "同意", "2026-01-01T00:00:09.000Z", mentioned=(SA,)),
    ])]
    await w.tick()
    assert prov.edit_calls == [], "提议格式的回复不得让 watcher 自己落地任何修改"
    entry = (await store.snapshot()).get(DOC) or {}
    assert "threads" not in entry and "applying" not in entry, "提议格式不再在状态里留下任何痕迹"


async def test_the_turn_sees_the_discussion_under_the_comment(kit):
    """find_triggers answers a thread as a whole on the first turn, rather than once per
    historical reply, on the stated grounds that "the turn sees all of them at once".

    It did not. Nothing passed the replies to the prompt and nothing in the prompt read
    them, so a thread where people had settled a constraint before summoning the agent
    handed the agent the opening comment alone -- and it broke a constraint nobody had
    told it about."""
    w, prov, store, dispatched = kit
    prov.comments = [C("c1", "把这段改简洁", replies=[
        R("r1", "注意术语「甲」不要动", "2026-01-01T00:00:01.000Z"),
    ])]
    await w.tick()
    assert dispatched, "应当派发一个回合"
    prompt = dispatched[-1][2]["prompt"] if isinstance(dispatched[-1][2], dict) else str(dispatched[-1])
    assert "不要动" in str(prompt), "线程里的约束必须进入这一回合"


# ------------------------------------------------- gaps the mutation probe found
#
# Each of these exists because a mutation survived: the code could have been wrong that
# way with the suite still green. The module was at 87% line coverage throughout.


async def test_a_link_shared_document_is_warned_about(kit, caplog):
    """§6.1: the share list is the authorisation list, so "anyone with the link" means
    anyone with the link can trigger the agent and approve its work. That is the document
    owner's decision and the code only warns -- but the warning was unchecked, and a
    warning nobody verifies is one that can quietly stop being emitted."""
    import logging

    w, prov, store, dispatched = kit
    prov.posture = [("anyone", "writer")]
    w._admitted.discard(DOC)
    with caplog.at_level(logging.WARNING):
        await w.tick()
    assert any("任何人" in r.message or "anyone" in r.message.lower() for r in caplog.records), (
        "以链接公开共享的文档必须告警"
    )


async def test_a_document_that_fails_admission_is_not_polled(kit):
    """Admission is the gate that keeps comment-only access from looking like it works.
    Bypassing it entirely left every test green, which means nothing was checking that a
    refused document is actually skipped rather than merely logged about."""
    w, prov, store, dispatched = kit
    prov.caps = replace(prov.caps, can_edit=False)
    w._admitted.discard(DOC)
    prov.comments = [C("c1", "改一下")]
    await w.tick()
    assert not dispatched, "准入拒绝的文档不得派发回合"


async def test_a_provider_without_per_document_domain_falls_back(kit):
    """A Feishu document (markdown domain) and a Google spreadsheet (plain) disagree
    about whether an asterisk is content, so the domain is asked per document -- and a
    provider that cannot answer must fall back rather than raise. The fallback branch
    was unchecked."""
    w, prov, store, dispatched = kit

    # Both directions. The first version of this test only checked the fallback, so the
    # mutation that skips the provider entirely -- never asking, always defaulting --
    # survived it. A test for a fallback has to say what it is falling back *from*.
    async def per_doc(_doc_id):
        return "markdown"

    type(prov).text_domain_for = staticmethod(per_doc)
    try:
        w._domain_cache.clear()
        assert await w._text_domain(DOC) == "markdown", (
            "provider 能按文档回答时必须采用它的答案"
        )
    finally:
        delattr(type(prov), "text_domain_for")

    w._domain_cache.clear()
    assert await w._text_domain(DOC) == w._domain_default


@pytest.mark.asyncio
async def test_a_thaw_still_has_to_pass_admission(tmp_path):
    """A frozen document gets one thaw attempt per process, and a service account that
    still cannot edit must find it frozen afterwards.

    The thaw path's own admission call turns out to be an optimisation rather than a
    gate: the unconditional check below refuses the same document, and ``_admit``
    re-freezes on refusal, so removing it costs a clear-then-re-freeze and changes
    nothing observable. What this pins is the outcome -- still frozen, not polled --
    which is what a person reading the panel depends on."""
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    await store.seed_if_new(DOC, [])
    await store.note_permanent_failure(DOC, "comment_only_access")

    prov = FakeProvider()
    prov.caps = replace(prov.caps, can_edit=False)  # still comment-only
    w, store, dispatched = await _fresh_process(tmp_path, prov, store)
    await w.tick()

    assert await store.is_permanently_failed(DOC), (
        "准入仍拒绝时，解冻不得生效——否则面板会显示健康而写入依然做不了"
    )
    assert not dispatched


# ---------------------------------------------------------- D19 tier 2: ledger check


class _LedgerSink:
    def __init__(self, entries=None):
        self.entries = entries or []

    def list_for(self, doc_id):
        return self.entries


def _ledger_rig(tmp_path, *, sink, replies=()):
    prov = FakeProvider()
    prov.receipt_sink = sink
    prov.comments = [DocComment(
        comment_id="c1", author_is_self=False, author_display_name="张三",
        created_time="2026-08-31T00:00:00.000Z", content="改一下",
        quoted_text="旧句", resolved=False, mentioned_addresses=(SA,),
        replies=tuple(replies), assignee_address=SA,
        author_is_service_account=False,
    )]
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())

    async def dispatch(doc_id, comment_id, metadata):
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(),
    )
    return w, prov


@pytest.mark.asyncio
async def test_a_claim_with_no_receipt_draws_a_mechanical_correction(tmp_path):
    """D19 tier 2, the founding shape: "I have drafted the plan" over zero writes.
    The audit needs no understanding -- a modification claim with no receipt in the
    turn's window is false whatever it says, and the correction lands where the
    claim landed."""
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(entries=[]))
    await w._ledger_check(DOC, "c1", "我已完成修改，文档已更新。", dispatched_at=100.0)
    assert prov.replies, "零回执的完成声明必须收到纠正帖"
    assert "没有产生任何写入回执" in prov.replies[-1][1]


@pytest.mark.asyncio
async def test_a_claim_backed_by_a_receipt_passes_silently(tmp_path):
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(
        entries=[{"ts": 150.0, "status": "applied", "executor": "comment:c1"}]
    ))
    await w._ledger_check(DOC, "c1", "已直接修改。", dispatched_at=100.0)
    assert prov.replies == []


@pytest.mark.asyncio
async def test_someone_elses_receipt_does_not_cover_the_claim(tmp_path):
    """The ledger is per document; only receipts this turn commissioned reconcile
    its claim. An owner's chat-path write during the turn, or another thread's
    turn, must not vouch for a modification this turn never made."""
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(entries=[
        {"ts": 150.0, "status": "applied", "executor": "chat"},
        {"ts": 160.0, "status": "applied", "executor": "comment:c2"},
    ]))
    await w._ledger_check(DOC, "c1", "已直接修改。", dispatched_at=100.0)
    assert prov.replies and "没有产生任何写入回执" in prov.replies[-1][1]


@pytest.mark.asyncio
async def test_another_agents_claim_in_the_thread_is_not_ours_to_reconcile(tmp_path):
    reply = DocReply(
        reply_id="b1", author_is_self=False, author_display_name="other-bot",
        created_time="2026-08-31T00:01:00.000Z", content="已按要求修改完毕。",
        mentioned_addresses=(), author_is_service_account=True,
    )
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(entries=[]), replies=[reply])
    await w._ledger_check(DOC, "c1", "本轮处理结束。", dispatched_at=100.0)
    assert prov.replies == [], "别的机器人的声明不由本轮对账"


@pytest.mark.asyncio
async def test_a_receipt_from_before_the_turn_does_not_cover_the_claim(tmp_path):
    """Yesterday's write cannot vouch for today's claim: only receipts inside the
    turn's window reconcile it."""
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(
        entries=[{"ts": 50.0, "status": "applied"}]
    ))
    await w._ledger_check(DOC, "c1", "已直接修改。", dispatched_at=100.0)
    assert prov.replies and "没有产生任何写入回执" in prov.replies[-1][1]


@pytest.mark.asyncio
async def test_a_turn_that_claims_nothing_is_left_alone(tmp_path):
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(entries=[]))
    await w._ledger_check(DOC, "c1", "原文没有问题，无需修改。", dispatched_at=100.0)
    assert prov.replies == []


@pytest.mark.asyncio
async def test_a_lying_thread_reply_is_caught_even_with_a_clean_answer(tmp_path):
    """The other lying surface: the model posts "已修改" through reply_comment and
    closes with something bland. The audit scans the turn's own thread posts too."""
    reply = DocReply(
        reply_id="r9", author_is_self=True, author_display_name="agent",
        created_time="2026-08-31T00:01:00.000Z", content="已按要求修改完毕。",
        mentioned_addresses=(), author_is_service_account=False,
    )
    w, prov = _ledger_rig(tmp_path, sink=_LedgerSink(entries=[]), replies=[reply])
    await w._ledger_check(DOC, "c1", "本轮处理结束。", dispatched_at=100.0)
    assert prov.replies and "没有产生任何写入回执" in prov.replies[-1][1]


# ---------------------------------------------------------------- duplicated notices
#
# Measured 2026-09-02 on a Google document: one mention, one dedup mark, **two**
# identical hints 25 s apart. The provider issued one POST; the HTTP layer re-sent it
# after losing the response, and the platform had already applied the first. The
# watcher cannot stop the socket from doing that, so it (a) never posts a notice a
# thread already carries and (b) deletes the later copy of an echoed notice.



@pytest.mark.asyncio
async def test_a_notice_already_in_the_thread_is_not_posted_again(kit, tmp_path):
    w, prov, store, dispatched = kit
    # No watch for DOC: every trigger is refused with the fixed notice.
    w._registry = WatchRegistry(path=tmp_path / "w.json")
    notice = _msg("watch_unauthorized", "请帮忙")
    prov.comments = [C("c1", "请帮忙", mentioned=(SA,),
                       replies=(R("r1", notice, "2026-01-01T00:00:10.000Z", me=True),))]
    await w.tick()
    assert prov.replies == [], "the thread already says it"
    assert dispatched == []


@pytest.mark.asyncio
async def test_an_echoed_notice_is_deleted_and_a_genuine_repeat_is_kept(kit):
    w, prov, store, dispatched = kit
    notice = _msg("watch_unauthorized", "请帮忙")
    prov.comments = [
        C("c1", "请帮忙", mentioned=(SA,), assignee=None, replies=(
            R("r1", notice, "2026-01-01T00:00:10.000Z", me=True),
            R("r2", notice, "2026-01-01T00:00:35.000Z", me=True),     # the echo
        )),
        C("c2", "请帮忙", mentioned=(SA,), assignee=None, replies=(
            R("r3", notice, "2026-01-01T00:00:10.000Z", me=True),
            R("r4", notice, "2026-01-01T00:10:00.000Z", me=True),     # 10 min later: two decisions
        )),
        C("c3", "请帮忙", mentioned=(SA,), assignee=None, replies=(
            R("r5", "处理中…", "2026-01-01T00:00:10.000Z", me=True),
            R("r6", "处理中…", "2026-01-01T00:00:12.000Z", me=True), # not a notice: untouched
        )),
    ]
    await store.mark_triggered(DOC, [f"clouddoc:{DOC}:c{i}:-" for i in (1, 2, 3)])
    await w.tick()
    assert prov.deletes == [("c1", "r2")]
    await w.tick()
    assert prov.deletes == [("c1", "r2")], "a deletion is attempted once per process"


@pytest.mark.asyncio
async def test_an_echo_whose_deletion_fails_is_not_retried_every_tick(kit):
    w, prov, store, dispatched = kit
    notice = _msg("watch_unauthorized", "请帮忙")
    prov.comments = [C("c1", "请帮忙", mentioned=(SA,), assignee=None, replies=(
        R("r1", notice, "2026-01-01T00:00:10.000Z", me=True),
        R("r2", notice, "2026-01-01T00:00:35.000Z", me=True),
    ))]
    await store.mark_triggered(DOC, [f"clouddoc:{DOC}:c1:-"])
    calls = []

    async def failing(d, cid, rid):
        calls.append(rid)
        raise ProviderError("forbidden", "no")
    prov.delete_reply = failing
    await w.tick()
    await w.tick()
    assert calls == ["r2"]


@pytest.mark.asyncio
async def test_a_person_asking_again_after_the_notice_is_answered_again(kit, tmp_path):
    """The notice guard looks only past the last human word: a standing refusal is
    repeated when the person asks again, never held against them as "already said"."""
    w, prov, store, dispatched = kit
    w._registry = WatchRegistry(path=tmp_path / "w.json")
    notice = _msg("watch_unauthorized", "请帮忙")
    prov.comments = [C("c1", "请帮忙", mentioned=(SA,), replies=(
        R("r1", notice, "2026-01-01T00:00:10.000Z", me=True),
        R("r2", "可以处理了", "2026-01-01T00:05:00.000Z", mentioned=(SA,)),
    ))]
    await store.mark_triggered(DOC, [dedup_key(DOC, "c1")])     # the first turn is behind us
    await w.tick()
    assert prov.replies == [("c1", notice)]


@pytest.mark.asyncio
async def test_without_a_registry_nothing_is_dispatched(tmp_path):
    """No registry means no mandate can be shown, so the trigger is refused.

    This used to dispatch "at the strictest tier". The tier is retired: it was
    never a weaker mandate, only an unattended turn holding tools the agent has
    without any mandate at all. The strictest thing available is not dispatching.
    """
    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(),
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    prov.comments = [C("c1", f"@{SA} 改一下", mentioned=[SA])]
    await w.tick()
    assert dispatched == [], "没有注册表就不派发"
    assert any("值守没有开启" in r[1] for r in prov.replies), "但要告诉协作者为什么"


@pytest.mark.asyncio
async def test_a_withdrawn_format_is_refused_before_anything_is_dispatched(tmp_path):
    """A row adopted while markdown was served stays in the panel, and stays inert.

    The person keeps the row -- removing it is theirs to decide -- but a comment on
    that document must not produce a turn that runs and then fails on the write. The
    refusal is permanent rather than a three-strikes streak: the format will not
    become supported between two polls, and counting it would leave the row reading
    healthy for two more ticks before turning red.
    """
    prov = FakeProvider()
    prov.doc_format = "markdown"
    w, store, dispatched = await _admitted(tmp_path, prov)

    assert dispatched == [], "不支持的格式不得派发任何轮次"
    assert await store.is_frozen(DOC, 0.0), "必须立刻冻结，而不是每 15 秒重试到永远"


@pytest.mark.asyncio
async def test_a_provider_that_cannot_name_the_format_still_admits(tmp_path):
    """The empty answer means "cannot say", not "unsupported".

    Reading it as unsupported would freeze every document on any provider that does
    not track formats, and nothing in the panel could unfreeze them.
    """
    prov = FakeProvider()
    prov.doc_format = ""
    _, store, dispatched = await _admitted(tmp_path, prov)

    assert not await store.is_frozen(DOC, 0.0)
    assert dispatched, "格式未知不等于不支持，正常文档必须照常派发"


@pytest.mark.asyncio
async def test_a_settled_admission_failure_is_recorded_not_retried(tmp_path):
    """A document that is gone must stop costing a platform call every poll.

    Admission answered every failure the same way -- log, skip this tick, come back
    in thirty seconds. That is right for a network blip and wrong for a deleted
    document: measured on the live tenant, one of them failed admission on 1827
    consecutive polls, burning a round trip each time and burying every other line in
    the log.
    """
    prov = FakeProvider()
    prov.caps = ProviderError("not_found", "该文档已在回收站")
    w, store, dispatched = await _admitted(tmp_path, prov)

    assert dispatched == []
    assert await store.is_frozen(DOC, 0.0), "已定的失败必须记账,而不是每轮重来"

    # Frozen, so the next tick does not ask the platform again.
    prov.caps = ProviderError("forbidden", "should not be asked again")
    await w.tick()


@pytest.mark.asyncio
async def test_a_transient_admission_failure_still_retries(tmp_path):
    """The other half, and the reason the two are told apart: a timeout during the
    admission call must not freeze a perfectly good document."""
    prov = FakeProvider()
    prov.caps = ProviderError("transport", "Errno 49")
    w, store, dispatched = await _admitted(tmp_path, prov)

    assert dispatched == []
    assert not await store.is_frozen(DOC, 0.0), "瞬时失败不得冻结文档"


# ------------------------------------------------ the poll interval is a live setting


def test_the_poll_interval_is_clamped_wherever_it_comes_from():
    """One place corrects an out-of-range value, so the config file, the panel and a
    test cannot disagree about what 1 second or 900 means."""
    from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import (
        MAX_POLL_INTERVAL_SECONDS,
        MIN_POLL_INTERVAL_SECONDS,
        clamp_poll_interval,
    )

    assert clamp_poll_interval(12.5) == 12.5
    assert clamp_poll_interval(1) == MIN_POLL_INTERVAL_SECONDS, "太快会烧配额"
    assert clamp_poll_interval(9999) == MAX_POLL_INTERVAL_SECONDS
    for junk in (0, -3, "x", None, float("nan")):
        assert clamp_poll_interval(junk, 30.0) == 30.0, f"{junk!r} 应回落到默认值"


@pytest.mark.asyncio
async def test_changing_the_interval_takes_effect_without_a_restart(tmp_path, monkeypatch):
    """The panel writes this value, so the loop has to read it every time round.

    It used to be read once at construction. A control whose effect waits for a
    restart is a control that lies, and this is the guard on that: the same watcher
    object answers differently after the config changes, with nothing rebuilt.
    """
    from jiuwenswarm.extensions.co_scribe.backend.host.watch import comment_watcher as cw

    w = CloudDocCommentWatcher(
        FakeProvider(), CloudDocStore(tmp_path / "s.json"),
        TriggerConfig(sa_address=SA), WatcherConfig(poll_interval_seconds=30.0),
        dispatch=None, now_fn=Clock(),
    )
    assert w._live_poll_interval() == 30.0, "没有配置项时用构造时的值"

    # Patched on the module the watcher imports it from, inside the method, so the
    # test exercises the same lookup the running loop does.
    import jiuwenswarm.common.config as common_config

    section: dict = {}
    monkeypatch.setattr(common_config, "get_config", lambda: {"clouddoc": section})
    section["poll_interval_seconds"] = 10
    assert w._live_poll_interval() == 10.0

    section["poll_interval_seconds"] = 1          # 越界
    assert w._live_poll_interval() == cw.MIN_POLL_INTERVAL_SECONDS

    section["poll_interval_seconds"] = "nonsense"  # 坏值退回构造值，不是崩溃
    assert w._live_poll_interval() == 30.0


# ------------------------- review: the format lookup fails inside admission, not around it


@pytest.mark.asyncio
async def test_a_document_whose_format_lookup_fails_settled_is_frozen_not_raised(tmp_path):
    """The format lookup is the first platform call a document meets. A deleted Google
    document raised ``not_found`` out of ``_admit`` -- which ``one()`` calls outside its
    own try -- so every tick logged "tick raised" and nothing ever froze or retired it:
    the 1827-poll shape on a different line."""
    prov = FakeProvider()

    async def gone(doc_ref):
        raise ProviderError("not_found", "File not found")

    prov.doc_kind = gone
    w, store, dispatched = await _admitted(tmp_path, prov)
    assert dispatched == []
    assert await store.is_permanently_failed(DOC), "settled 的准入失败必须冻结，而不是每轮抛异常"

    calls = []
    orig = prov.capabilities

    async def counted(d):
        calls.append(d)
        return await orig(d)

    prov.capabilities = counted
    await w.tick()
    assert calls == [], "格式都查不到的文档不应再走到能力检查"


@pytest.mark.asyncio
async def test_a_transient_format_lookup_failure_skips_the_tick_and_freezes_nothing(tmp_path):
    prov = FakeProvider()

    async def flaky(doc_ref):
        raise ProviderError("transport", "timeout")

    prov.doc_kind = flaky
    w, store, dispatched = await _admitted(tmp_path, prov)
    assert dispatched == []
    assert not await store.is_permanently_failed(DOC)


# ------------------------- review: an unreadable live config dispatches nothing


@pytest.mark.asyncio
async def test_an_unreadable_live_config_dispatches_nothing(tmp_path, monkeypatch):
    """The live flag is the deployment's standing consent to unattended writes. A
    config that cannot be read has not given it, so the tick fails closed: the loop
    survives, nothing is dispatched and no trigger is consumed, and a config that
    reads again takes effect on the next tick."""
    import jiuwenswarm.common.config as common_config

    prov = FakeProvider()
    store = CloudDocStore(tmp_path / "s.json", now_fn=Clock())
    dispatched: list[tuple] = []

    async def dispatch(doc_id, comment_id, metadata):
        dispatched.append((doc_id, comment_id, metadata))
        return "ok"

    w = CloudDocCommentWatcher(
        prov, store, TriggerConfig(sa_address=SA), WatcherConfig(),
        dispatch=dispatch, now_fn=Clock(), registry=_mandated_registry(tmp_path),
    )
    w._docs = [DOC]
    await store.seed_if_new(DOC, [])
    prov.comments = [C("c1", f"@{SA} 改一下", mentioned=(SA,))]

    def unreadable():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(common_config, "get_config", unreadable)
    await w.tick()
    assert dispatched == [], "读不到实时配置时不得派发"
    await w.tick()
    assert dispatched == [], "配置仍不可读，仍不派发"

    monkeypatch.setattr(common_config, "get_config", lambda: {"clouddoc": {"enabled": True}})
    await w.tick()
    assert len(dispatched) == 1, "配置恢复可读后，下一轮即派发被搁置的召唤"
