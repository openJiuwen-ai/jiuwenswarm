"""CloudDocCommentWatcher: poll, dispatch, apply.

The lifecycle follows ``GatewayHeartbeatService`` -- idempotent start,
cancel-and-await stop, a loop that swallows everything but cancellation. The clock
and sleep seams follow cron's ``now_fn``, except that the cron precedent has no test
using it, whereas the time windows, backoff and session growth here all depend on
time, so these seams are ones the tests **actually drive**.
"""

from __future__ import annotations

import asyncio
import re
import time
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from dataclasses import dataclass

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    UNSUPPORTED_KIND_DETAIL,
    DocProvider,
    ProviderError,
    is_supported_kind,
)
from jiuwenswarm.extensions.co_scribe.backend.host.conventions import (
    needs_ack,
    render_ack,
    select_conventions,
)
from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import CloudDocStore
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import (
    OrphanClass,
    TriggerClass,
    TriggerConfig,
    classify_orphans,
    find_triggers,
)
from jiuwenswarm.extensions.co_scribe.backend.host.watch.texts import msg
from jiuwenswarm.extensions.co_scribe.backend.host.watch.turn_prompt import build_turn_prompt
from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import looks_chinese


def _since_last_person(thread: Any) -> list:
    """The replies posted after the last one a person wrote (all of them when no
    person has replied)."""
    replies = list(thread.replies or ())
    for i in range(len(replies) - 1, -1, -1):
        if not replies[i].author_is_self:
            return replies[i + 1:]
    return replies


def _within(earlier: str, later: str, window_s: float) -> bool:
    """Whether two provider timestamps lie within ``window_s`` of each other.

    Google gives RFC 3339, Feishu gives epoch seconds or milliseconds as a string.
    Anything unparseable compares as *not* within: the cleanup then leaves both
    replies alone, which is the safe way to be wrong.
    """
    a, b = _epoch(earlier), _epoch(later)
    if a is None or b is None:
        return False
    return 0 <= b - a <= window_s


def _epoch(stamp: str) -> float | None:
    s = str(stamp or "").strip()
    if not s:
        return None
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


logger = logging.getLogger(__name__)

# The transport caps a request at 600s. Matching that value races it, with an
# unpredictable exception type; going higher makes the wait_for on this side dead
# code. Clamped to 90% at startup.
TRANSPORT_TIMEOUT_CEILING = 600.0

# Bounds on the poll interval, applied wherever the value comes from.
#
# The floor is a quota decision, not a technical one: every watched document costs
# at least one list_comments per tick, so halving the interval doubles the spend on
# an idle deployment. Five seconds is the shortest that still leaves the measured
# ~2s round trip room to finish before the next tick starts on a document.
#
# The ceiling is the point past which a person stops believing the feature works.
MIN_POLL_INTERVAL_SECONDS = 5.0
MAX_POLL_INTERVAL_SECONDS = 600.0


def clamp_poll_interval(value: Any, default: float = 30.0) -> float:
    """The poll interval as a number inside the bounds, whatever was configured.

    Everything that can set this -- the config file, the panel, a test -- goes
    through here, so an out-of-range value is corrected in one place rather than
    trusted by one caller and rejected by the next.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds <= 0:  # NaN or nonsense
        return default
    return min(max(seconds, MIN_POLL_INTERVAL_SECONDS), MAX_POLL_INTERVAL_SECONDS)


@dataclass
class WatcherConfig:
    poll_interval_seconds: float = 30.0
    turn_timeout_seconds: float = 540.0
    # Lists rather than single words, so a Chinese and an English collaborator can
    # express the same commitment in their own language. The test is still exact
    # equality per entry; only the language is loosened. What goes in the lists is a
    # safety parameter.
    # The deployment-level working-style file (§4.8 / D4). Empty means the default
    # location under the workspace config dir; the file is re-read on every dispatch.
    workmode_file: str = ""

    def clamped_turn_timeout(self) -> float:
        return min(self.turn_timeout_seconds, TRANSPORT_TIMEOUT_CEILING * 0.9)


_NARRATION_MARKS = (
    # Third-person prose about the user, or statements that no action is needed --
    # dispatcher-facing closings, never something a person says to a colleague in a
    # thread. Both languages, because the model closes in the language of the turn.
    "the user has",
    "the user is",
    "no further action",
    "i will reply",
    "i have replied",
    "acknowledged my previous response",
    "用户已",
    "无需进一步",
    "我将回复",
    "我已回复",
)


def reads_as_dispatcher_narration(text: str) -> bool:
    """Whether a final answer is addressed to the dispatcher rather than the thread.

    The write-back is a fallback for a model that answered in plain text and called no
    tool; its final answer is normally that missing reply. But on a follow-up turn the
    model often closes with narration about the conversation itself -- observed twice
    on camera: "Since the user has acknowledged my previous response (\"ok, I got
    it\"), no further action is required for this specific comment." Posting that puts
    third-person prose about the user into the user's own document.

    A deterministic marker list, deliberately conservative: it must catch the observed
    shapes, and a false positive silently drops a real answer, which is the worse
    error. This is a filter on one write path, not a guarantee about model prose.
    """
    lowered = text.casefold()
    return any(mark in lowered for mark in _NARRATION_MARKS)


class CloudDocCommentWatcher:
    def __init__(
        self,
        provider: DocProvider,
        store: CloudDocStore,
        trigger_cfg: TriggerConfig,
        cfg: WatcherConfig,
        *,
        dispatch: Callable[[str, str, dict], Awaitable[str]],
        now_fn: Callable[[], float],
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        registry: "object | None" = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._tcfg = trigger_cfg
        self._cfg = cfg
        self._dispatch = dispatch
        # The standing-mandate registry (PR2b). None gates nothing -- the PR1 shape,
        # kept for tests that exercise the machinery below the gate; production wiring
        # always passes a registry (asserted by its own test).
        self._registry = registry
        self._now_fn = now_fn
        # The provider's declared text domain carries through to both the range rail
        # and the prompt. They must share one source, or the prompt says markup is
        # allowed while the rail refuses it.
        #
        # Asked per document, not once: one watcher watches many documents and a
        # connection now reaches several formats, so a Feishu docx and a spreadsheet
        # in the same tenant answer differently. The provider-wide value stays as the
        # fallback for a provider that serves one format.
        self._domain_default = getattr(provider, "text_domain", "plain")
        self._domain_cache: dict[str, str] = {}
        # Capabilities as answered at admission, kept so the rail can use what the
        # provider measured instead of a configured guess. One entry per admitted
        # document; admission already pays for the call.
        self._caps_cache: dict[str, Any] = {}
        self._sleep = sleep_fn or asyncio.sleep
        self._task: asyncio.Task | None = None
        self._docs: list[str] = []
        # Whether the last tick found the live config unreadable. Logged on the
        # transition only: a config that stays broken must not fill the log once
        # per poll, and the recovery is worth one line of its own.
        self._config_unreadable = False
        # Documents that passed admission. Checked once per document per process,
        # rather than burning quota every tick.
        self._admitted: set[str] = set()
        # Failed documents this process has already given a re-check. Deliberately not
        # persisted: a freeze is a memory rather than a sentence, and "a fresh process
        # looks once more" is exactly what a per-process set means.
        self._thaw_tried: set[str] = set()
        # Duplicate own notices this process already tried to delete, so a deletion
        # the platform refuses is not retried every tick.
        self._dedup_tried: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ live panel actions

    async def _text_domain(self, doc_id: str) -> str:
        """This document's text domain, cached for the life of the process.

        A document does not change format, so one lookup per document is enough; the
        cache exists because the answer costs a metadata call on some providers and
        this runs on every dispatch.

        A provider that cannot answer for a document falls back to its own declared
        domain rather than propagating the failure: getting the markup rule slightly
        wrong is a worse outcome than a refused proposal, but it is a far better one
        than a watcher tick that dies.
        """
        cached = self._domain_cache.get(doc_id)
        if cached is not None:
            return cached
        fn = getattr(self._provider, "text_domain_for", None)
        domain = self._domain_default
        if fn is not None:
            try:
                domain = await fn(doc_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[clouddoc] %s 的文本域取不到，按 provider 默认值 %s 处理：%s",
                    doc_id,
                    self._domain_default,
                    exc,
                )
        self._domain_cache[doc_id] = domain
        return domain

    def watch(self, doc_id: str) -> None:
        """Add a document to this process's polling set. Persisting it is the caller's
        job -- the panel's."""
        if doc_id not in self._docs:
            self._docs.append(doc_id)

    def unwatch(self, doc_id: str) -> None:
        """Remove it from the polling set and forget everything this process knew about
        it, so re-adding decides afresh."""
        if doc_id in self._docs:
            self._docs.remove(doc_id)
        self._admitted.discard(doc_id)
        self._thaw_tried.discard(doc_id)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Begin polling whatever this watcher is already watching.

        It used to take the document list and **overwrite** ``_docs`` with it, which made
        the list have two sources: what the registry held and what the panel had added
        through ``watch``. A document added before the first start was silently dropped,
        and ``all_docs`` reported one source or the other depending on a flag. The
        watcher owns its list from the moment its connection exists.
        """
        if self._task and not self._task.done():
            return  # idempotent
        self._task = asyncio.create_task(self._loop(), name="clouddoc-comment-watcher")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        await self.sweep()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one failed tick must not end the loop
                logger.exception("[clouddoc] tick failed")
            await self._sleep(self._live_poll_interval())

    def _live_poll_interval(self) -> float:
        """The interval to wait before the next tick, read from the config each time.

        Read live for the same reason ``tick`` reads ``enabled`` and ``mode`` live: the
        panel writes this value, and a control whose effect waits for a restart is a
        control that lies. The constructed value stays as the fallback for a
        deployment with nothing configured and for tests.
        """
        try:
            from jiuwenswarm.common.config import get_config

            section = get_config().get("clouddoc") or {}
            raw = section.get("poll_interval_seconds")
        except Exception:  # noqa: BLE001 - an unreadable config must not stop the loop
            return self._cfg.poll_interval_seconds
        if raw is None:
            return self._cfg.poll_interval_seconds
        return clamp_poll_interval(raw, self._cfg.poll_interval_seconds)

    # ------------------------------------------------------------ startup sweep

    async def sweep(self) -> list[str]:
        """Two classes of orphan, both placeholders left by a turn that never finished.

        Collecting state is **not** done here. A watcher knows its own documents and
        nothing else, while ``gc`` is absolute -- it deletes every document not in the
        list it is given. With one watcher those were the same thing and this method used
        to call it; with several sharing one state file they are not, and each watcher
        was deleting the others' documents on every start. The registry owns that call
        now, because it is the only layer that knows the full set.
        """
        actions: list[str] = []

        # Receipts first (IC-3). A pending receipt is a write that was begun and never
        # closed, and until now nothing ever adjudicated one: ``pending()`` had a
        # docstring naming this sweep and no caller anywhere but the tests, so crash
        # windows stayed open for good and, because the retention cap deliberately skips
        # pending, the file grew without bound while being rewritten on every write.
        #
        # Adjudicating means recording that the outcome is unknown, never guessing it.
        # Only receipts old enough to belong to a dead process are touched -- the file is
        # shared between processes, and settling a live one would destroy the evidence.
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import ReceiptStore

            settled = ReceiptStore().sweep_stale()
            if settled:
                logger.warning(
                    "[clouddoc] %d 条回执停留在写入中且已超龄，标记为结果未知，"
                    "需对照文档确认：%s",
                    len(settled), ", ".join(settled[:5]),
                )
                actions.append(f"receipts_unknown:{len(settled)}")
        except Exception:  # noqa: BLE001 - bookkeeping must not stop the watcher starting
            logger.exception("[clouddoc] receipt sweep failed")

        for doc_id in self._docs:
            snap = await self._store.snapshot()
            entry = snap.get(doc_id) or {}
            orphans = classify_orphans(
                doc_id,
                entry.get("inflight") or {},
                set(entry.get("triggered_ids") or {}),
            )
            for o in orphans:
                if o.kind is OrphanClass.PLACEHOLDER_AFTER_DISPATCH:
                    for cid, rid in (o.payload or {}).get("placeholders", {}).items():
                        # Both languages, spelled out rather than routed through
                        # texts.py: recovery runs at startup, after the process that
                        # held the triggering comment is gone, so there is no language
                        # sample left to choose from.
                        await self._safe_update(
                            doc_id, cid, rid,
                            "处理中断。在本线程 @ 我一句即可让我重试 / "
                            "Interrupted — @-mention me in this thread and I'll retry.",
                        )
                    await self._store.end_inflight(doc_id, o.turn_id)
                    actions.append(f"{o.kind.value}:{o.turn_id}")
                elif o.kind is OrphanClass.PLACEHOLDER_BEFORE_DISPATCH:
                    await self._store.end_inflight(doc_id, o.turn_id)
                    actions.append(f"{o.kind.value}:{o.turn_id}")
        return actions

    # ------------------------------------------------------------ one poll

    async def tick(self) -> dict:
        """One poll: **documents in parallel, work within a document serial**.

        Walking the documents serially would be wrong. Dispatch awaits inside the
        tick, so a single slow turn -- up to the 540s timeout -- would stall polling
        for every other document with it.

        Within a document the work stays serial: two triggers on the same document
        dispatched concurrently would write the same session at once.

        **There is no cap on how many run at once**, and that is a known boundary rather
        than an oversight: with the indicative limit of about fifty watched documents
        (§9.8) a tick can dispatch fifty agent turns together. Capping it would change
        the timing of every poll for a scenario nobody has reached; the ceiling belongs
        with the quota-and-scale question, which is where §9.8 already keeps it.
        """
        summary = {"dispatched": 0, "applied": 0, "kept": 0, "rejected": 0}
        if not self._docs:
            return summary
        # D21: only the mandate mode has an unattended path at all. In direct mode
        # assignments stay in the document untouched -- no dispatch, no consumed
        # triggers, no mechanical replies -- so switching back to mandate later
        # finds them exactly where their authors left them.
        try:
            from jiuwenswarm.common.config import get_config

            section = get_config().get("clouddoc") or {}
            mode = str(section.get("mode") or "mandate").strip().lower()
            live_enabled = bool(section.get("enabled", True))
        except Exception:  # noqa: BLE001 - the loop survives; this tick's dispatch does not
            # Fail closed. The live flag is the deployment's standing consent to
            # unattended writes, and a config that cannot be read has not given it.
            # The loop keeps polling so a repaired config takes effect on the next
            # tick, but nothing is dispatched -- and no trigger is consumed -- until
            # one can be read.
            if not self._config_unreadable:
                logger.exception("[clouddoc] 实时配置不可读，本轮不派发；配置恢复后自动继续")
            self._config_unreadable = True
            return summary
        if self._config_unreadable:
            logger.info("[clouddoc] 实时配置已恢复可读，恢复派发")
            self._config_unreadable = False
        if not live_enabled or mode in ("direct", "recorded"):
            # Uninstalling the co-scribe plugin flips enabled off mid-session;
            # the loop keeps running until restart, so it must read the flag
            # live rather than trust its construction-time value.
            return summary

        async def one(doc_id: str) -> None:
            # The provider's format routing is process memory and the pasted link
            # that taught it does not come back after a restart; the panel's store
            # does. Fed once per document per process, so a known spreadsheet's
            # comment verbs are not sent down the docx paths into backoff.
            note = getattr(self._provider, "note_kind", None)
            primed: set = getattr(self, "_kind_primed", None) or set()
            self._kind_primed = primed
            if note is not None and doc_id not in primed:
                primed.add(doc_id)
                try:
                    health = await self._store.doc_health(doc_id)
                    kind = (health.get("panel_meta") or {}).get("kind") or ""
                    if kind:
                        note(doc_id, kind)
                except Exception:  # noqa: BLE001 - priming must not stop the loop
                    pass
            # The **reading end** of backoff. Without it, the failed and until fields
            # the store records are just a note nobody reads: a document whose access
            # was revoked retries every cycle forever, and a throttled one keeps
            # hammering at full rate.
            if await self._store.is_frozen(doc_id, self._now_fn()):
                # A failed document gets one re-check per process. Without it, fixing
                # the permission would not thaw the document even across a restart:
                # failed is persisted, the freeze check short-circuits admission
                # permanently, "clear on success" never sees that success, and the only
                # way out becomes removing the document from the config and adding it
                # back.
                # Only the failed verdict opens this door; the backoff window is a
                # rate-limiting rhythm and a restart must not step around it.
                # A transient error burns this process's one chance, since _admit's
                # False does not distinguish transient from settled. That costs the user
                # one more restart and saves _admit from a three-valued return.
                retriable = (
                    doc_id not in self._thaw_tried
                    and await self._store.is_permanently_failed(doc_id)
                )
                if not retriable:
                    summary["skipped"] = summary.get("skipped", 0) + 1
                    return
                self._thaw_tried.add(doc_id)
                # Redundant for safety and kept for cost: the unconditional check below
                # would refuse this document anyway, and ``_admit`` re-freezes on refusal,
                # so removing this one changes no outcome -- only a pointless
                # clear-then-re-freeze pair of writes. A mutation run will report it as a
                # surviving mutant for exactly that reason; it is equivalent, not
                # unchecked.
                if not await self._admit(doc_id):
                    summary["skipped"] = summary.get("skipped", 0) + 1
                    return
                await self._store.clear_failure(doc_id)
                thawing = True
            else:
                thawing = False
            # Admission has to sit outside the try. The clear_failure below reads "_tick_doc
            # returned normally" as success, and a refusal returns normally too, so a
            # check placed inside would wipe the very freeze it just set.
            if not await self._admit(doc_id):
                summary["skipped"] = summary.get("skipped", 0) + 1
                return
            try:
                await self._tick_doc(doc_id, summary)
            except ProviderError as exc:
                if thawing and exc.kind in ("not_found", "forbidden"):
                    # The thaw re-check failing with the same permanent kind is
                    # confirmation, not a fresh streak. Routing it through the
                    # three-strikes count re-froze the document only after two more
                    # ticks, and in between the panel showed the row healthy --
                    # measured: failures=1/2 with failed=False for two polling cycles
                    # before turning red again.
                    await self._store.note_permanent_failure(doc_id, exc.kind)
                    logger.warning(
                        "[clouddoc] %s thaw re-check failed: %s (%s) — refrozen at once",
                        doc_id, exc, exc.kind,
                    )
                    return
                b = await self._store.note_failure(doc_id, exc.kind, self._now_fn())
                logger.warning(
                    "[clouddoc] %s failed: %s (%s) failures=%s failed=%s",
                    doc_id, exc, exc.kind, b.get("failures"), b.get("failed"),
                )
                return
            # One success resets the count. Otherwise it only ever grows, and network
            # flapping lifts it until "three consecutive permission errors" means
            # nothing -- measured at 10, all of them Errno 49.
            await self._store.clear_failure(doc_id)

        # return_exceptions: a non-ProviderError from one document must not swallow the
        # results of the others
        results = await asyncio.gather(*(one(d) for d in self._docs), return_exceptions=True)
        for doc_id, res in zip(self._docs, results):
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                logger.exception("[clouddoc] %s tick raised", doc_id, exc_info=res)
        return summary

    async def _admit(self, doc_id: str) -> bool:
        """The admission check, run once per document per process.

        **Comment-only access is the configuration mistake that most needs to fail
        fast**, because it fails silently: comments still read, proposals still post,
        but Docs stops returning a revisionId so concurrency protection quietly
        evaporates, and permissions.list returns 403 so the link-sharing warning cannot
        run either. The user believes it works until they notice no edit has ever
        landed. The temptation is natural too -- "I don't want it changing my document."

        Losing permission mid-run is not detected here. That shows up as a 403 and the
        per-document backoff advances failed. So this runs once after the process
        starts rather than burning quota every tick.
        """
        if doc_id in self._admitted:
            return True

        # The backstop for a format co-scribe no longer serves. The registry drops
        # such a document at adoption, so this normally never fires -- but that drop
        # reads the panel's recorded formats and is deliberately fail-soft, so a
        # state file that cannot be read admits everything rather than evicting a
        # whole deployment's documents on a bad startup. This is the boundary that
        # holds in that case: nothing is dispatched, so a comment on such a document
        # cannot produce a turn that runs and then fails on the write.
        #
        # Permanent, not a three-strikes streak: the format will not become supported
        # between two polls, and counting it would leave the row reading healthy for
        # two more ticks.
        try:
            kind = await self._provider.doc_kind(doc_id)
        except ProviderError as exc:
            # The format lookup is the first platform call a document meets, so a
            # document that is gone or unshared fails **here**, before the capability
            # check below that records settled failures -- and ``one()`` calls this
            # outside its own try, so the error escaped as "tick raised" on every
            # poll: never frozen, never retired, the 1827-poll shape on another line.
            # Same two verdicts as below: settled kinds are recorded once, the rest
            # skip this tick.
            if exc.kind in ("not_found", "forbidden"):
                logger.error(
                    "[clouddoc] %s 准入检查失败（%s），不再重试：%s", doc_id, exc.kind, exc
                )
                await self._store.note_permanent_failure(doc_id, exc.kind)
                return False
            logger.warning("[clouddoc] %s 准入检查失败（%s），本轮跳过", doc_id, exc.kind)
            return False
        if not is_supported_kind(kind):
            logger.error(
                "[clouddoc] %s 拒绝纳管：%s（格式 %s）",
                doc_id, UNSUPPORTED_KIND_DETAIL, kind or "未知",
            )
            await self._store.note_permanent_failure(doc_id, "unsupported_kind")
            return False

        try:
            caps = await self._provider.capabilities(doc_id)
        except ProviderError as exc:
            # A settled failure is recorded, not retried. Admission used to answer
            # every failure the same way -- log, skip this tick, come back in thirty
            # seconds -- which is right for a network blip and wrong for a document
            # that is gone: measured on the live tenant, one deleted document failed
            # admission on 1827 consecutive polls, burning a platform round trip each
            # time and burying every other line in the log.
            #
            # The same two kinds the thaw path already treats as settled are settled
            # here: the document is not coming back on its own, and the ways out are
            # the ones a person takes anyway -- the panel's refresh, which clears the
            # verdict, or a restart, which grants one re-check.
            if exc.kind in ("not_found", "forbidden"):
                logger.error(
                    "[clouddoc] %s 准入检查失败（%s），不再重试：%s", doc_id, exc.kind, exc
                )
                await self._store.note_permanent_failure(doc_id, exc.kind)
                return False
            logger.warning("[clouddoc] %s 准入检查失败（%s），本轮跳过", doc_id, exc.kind)
            return False

        self._caps_cache[doc_id] = caps

        if not caps.can_edit:
            logger.error(
                "[clouddoc] %s 拒绝纳管：服务账号只有评论权。"
                "没有编辑权时 revisionId 不再返回，并发保护会静默消失，"
                "提议可以发出但永远无法应用。请把该文档共享给服务账号并给「编辑者」权限。",
                doc_id,
            )
            await self._store.note_permanent_failure(doc_id, "comment_only_access")
            return False

        try:
            posture = await self._provider.sharing_posture(doc_id)
        except ProviderError as exc:
            # If the sharing posture cannot be read, **say it is unknown**; never treat
            # that as safe.
            logger.warning("[clouddoc] %s 共享状态未知（%s）", doc_id, exc.kind)
            posture = []
        if not caps.has_revision_control:
            # Warned, not refused. The inference that no edit rights means no revision
            # id held while every document was a Google Doc; it does not hold for the
            # formats added since -- a spreadsheet has no revision id anywhere in its
            # API, with full edit rights.
            #
            # Refusing would remove the formats the person asked for. Saying nothing
            # would be the failure this flag exists to prevent: a concurrent edit is
            # overwritten and the turn reports success. So it is said, once, to the
            # person who can decide -- the same treatment "anyone with the link" gets
            # just below, and for the same reason.
            logger.warning(
                "[clouddoc] %s 所在的文档类型没有乐观锁：写入前会重取比对，"
                "但无法保证不覆盖并发编辑。这是平台能力的边界，不是配置问题。",
                doc_id,
            )
        if any(t == "anyone" for t, _ in posture):
            logger.warning(
                "[clouddoc] %s 以「知道链接的任何人」共享：任何持链接的人都能触发 agent "
                "并批准它的提议。共享名单就是授权名单——这是文档所有者的决定，此处只告警。",
                doc_id,
            )

        self._admitted.add(doc_id)
        return True

    async def _tick_doc(self, doc_id: str, summary: dict) -> None:
        comments = await self._provider.list_comments(doc_id)

        # First time watching: register the existing trigger points as handled and do
        # nothing else this tick. It runs before read-back, because read-back also posts
        # into the document (a refusal explanation), and historical proposals should not
        # be re-judged just because the feature was switched on.
        if await self._seed_if_new(doc_id, comments):
            summary["seeded"] = summary.get("seeded", 0) + 1
            return

        await self._dedupe_own_notices(doc_id, comments)

        # Work out the trigger set and dispatch
        state = (await self._store.snapshot()).get(doc_id) or {}
        triggered = set(state.get("triggered_ids") or {})

        await self._unhighlight_resolved(doc_id, comments)
        conventions = select_conventions(comments, self._tcfg)
        # The conventions acknowledgement fires on **first taking effect**, not first
        # being seen: the watcher should not write into a document nobody triggered. It
        # is also the only feedback on whether the marker was recognised at all --
        # conventions_marker matches literally, and one wrong space turns the comment
        # silently into an ordinary one.
        acked_this_tick = False
        for t in find_triggers(
            comments, self._tcfg, doc_id=doc_id, already_triggered=triggered,
        ):
            # The dispatch gate (D2/D3/D9): one live conjunction per dispatch, never
            # a cached boolean. A refused trigger is **consumed** -- pre-grant work
            # never auto-dispatches later (the signed cutline: observation-time,
            # fail-closed; a later grant covers new events only), suspended-period
            # work waits for a human (the unified backlog law), and the collaborator
            # hears why exactly once (the consumed key doubles as the reply dedup).
            if self._registry is not None:
                verdict = self._registry.check(doc_id)
                if not verdict.dispatchable and verdict.reason == "rate_limited":
                    # The brake is a pause, not a verdict on the work: leave the
                    # trigger unconsumed so it dispatches when the window slides
                    # free, post nothing (a per-denial reply was fuel for
                    # bot-versus-bot threads -- each denial notice re-triggered the
                    # counterpart), and skip the denial note (the eventual dispatch
                    # is the audit event; a storm of throttle lines is not). The
                    # log line is the only trace the pause leaves.
                    rate_max = getattr(self._registry, "_rate_max", None)
                    window = getattr(self._registry, "_rate_window", None)
                    if isinstance(rate_max, int) and isinstance(window, (int, float)):
                        logger.info(
                            "[clouddoc] gate %s/%s paused: rate_limited (%d/%ds)",
                            doc_id, t.comment.comment_id, rate_max, int(window),
                        )
                    else:
                        logger.info(
                            "[clouddoc] gate %s/%s paused: rate_limited",
                            doc_id, t.comment.comment_id,
                        )
                    continue
                if not verdict.dispatchable:
                    logger.info(
                        "[clouddoc] gate %s/%s denied: %s",
                        doc_id, t.comment.comment_id, verdict.reason,
                    )
                    self._registry.note_denied(doc_id, verdict.reason)
                    await self._store.mark_triggered(doc_id, [t.key_for(doc_id)])
                    # ``watch_suspended`` used to be the third answer here. With
                    # suspension retired there is no verdict that maps to it: a
                    # watch is off (unauthorized) or over budget (queued). The
                    # text survives in the table for dedup only -- see
                    # ``_NOTICE_KEYS`` -- so an old notice already sitting in a
                    # thread is still recognised and not echoed.
                    reply_key = {
                        "no_watch": "watch_unauthorized",
                        "expired": "watch_unauthorized",
                        "over_budget": "watch_over_budget",
                    }.get(verdict.reason, "watch_unauthorized")
                    await self._safe_reply(
                        doc_id, t.comment.comment_id,
                        msg(reply_key, t.comment.content), thread=t.comment,
                    )
                    summary["gated"] = summary.get("gated", 0) + 1
                    continue
                turn_mode = verdict.mode
            else:
                # No registry to consult, so no mandate can be shown: treated
                # exactly as ``no_watch``. This branch used to dispatch at the
                # "strictest tier" instead -- but a tier that only replies is not a
                # weaker mandate, it is an unattended turn nobody authorised, run
                # with tools the agent has anyway. With the ladder retired, the
                # strictest thing available is not dispatching, and that is what a
                # missing registry gets. The collaborator still hears why: the
                # unauthorised reply below needs no mandate to be posted.
                logger.warning(
                    "[clouddoc] 无授权注册表，doc=%s 按无授权处理，不派发", doc_id
                )
                await self._store.mark_triggered(doc_id, [t.key_for(doc_id)])
                await self._safe_reply(
                    doc_id, t.comment.comment_id,
                    msg("watch_unauthorized", t.comment.content), thread=t.comment,
                )
                summary["gated"] = summary.get("gated", 0) + 1
                continue

            # Style is re-read on every dispatch: editing the file fires no config
            # event, so caching it would delay changes to the next restart (§4.8.6).
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode import (
                load_workmode,
                prefer_zh_from_words,
            )
            wm = load_workmode(
                self._cfg.workmode_file,
                prefer_zh=prefer_zh_from_words(self._tcfg.conventions_marker),
            )
            if wm.error:
                logger.warning("[clouddoc] workmode 文件不可读，回退内置模板：%s", wm.error)
            prompt = build_turn_prompt(
                t.comment,
                text_domain=await self._text_domain(doc_id),
                mode=turn_mode,
                workmode_text=wm.text,
                conventions=conventions,
                reply_content=t.reply.content if t.reply else None,
                # The discussion under the comment. find_triggers answers a thread as a
                # whole on the first turn precisely so the agent can read all of it at
                # once -- and until now nothing passed it, so it read none of it.
                thread=t.comment.replies,
            )
            if conventions is not None and not acked_this_tick:
                previous = await self._store.get_conventions(doc_id)
                if needs_ack(previous, conventions):
                    await self._safe_reply(
                        doc_id, conventions.comment_id or t.comment.comment_id,
                        render_ack(conventions),
                    )
                    await self._store.note_conventions_acked(doc_id, conventions.content_hash)
                acked_this_tick = True

            # The dispatch order is fixed: write triggered_ids, post the placeholder,
            # then dispatch.
            await self._store.mark_triggered(doc_id, [t.key_for(doc_id)])

            # A placeholder goes out **only on first touch**. Follow-ups get none: the
            # person just replied and the agent answers within a minute, so a placeholder
            # carries no information while iteration would flood the thread.
            first_touch = t.kind is not TriggerClass.FOLLOW_UP
            placeholder_id = None
            # The dedup key doubles as turn_id: one comment can carry two new replies
            # within a tick, and a second-resolution timestamp would collide on one id,
            # with the inflight records overwriting each other.
            turn_id = t.key_for(doc_id)
            if first_touch:
                placeholder_id = await self._safe_reply_id(
                    doc_id, t.comment.comment_id, msg("placeholder", t.comment.content)
                )
                if placeholder_id is None:
                    # **A failed placeholder abandons this dispatch, and gives the key
                    # back.** Dispatching anyway yields a turn with no placeholder to
                    # write its final state into, so it could only start another
                    # reply, and crash recovery would lose its only handle. Keeping
                    # the key would lose the summons for good on a transient error;
                    # returned, the next tick finds the same trigger and tries again.
                    await self._store.unmark_triggered(doc_id, [t.key_for(doc_id)])
                    logger.warning(
                        "[clouddoc] placeholder failed, trigger returned for the next "
                        "poll doc=%s comment=%s",
                        doc_id, t.comment.comment_id,
                    )
                    continue
                await self._store.begin_inflight(
                    doc_id, turn_id, [t.key_for(doc_id)], {t.comment.comment_id: placeholder_id}
                )
            else:
                await self._store.begin_inflight(doc_id, turn_id, [t.key_for(doc_id)], {})

            # The payload has two layers and **they must not be merged**. ``clouddoc``
            # is the cross-process authorization scope and holds nothing but
            # server-generated opaque ids; putting the prompt in it would place
            # untrusted comment text in the same dictionary the authorization check
            # reads. ``prompt`` travels in params.content -- content, not permission.
            if self._registry is not None:
                # Counted at dispatch, persisted: a restart never refills the day.
                self._registry.note_dispatch(doc_id)
            dispatched_at = time.time()
            answer = await self._dispatch(
                doc_id,
                t.comment.comment_id,
                {"clouddoc": {"doc_id": doc_id, "comment_id": t.comment.comment_id,
                              # IC-1: the watch level rides in the authorization
                              # payload -- a server value, snapshotted here so a
                              # later modification drains instead of reaching back.
                              "mode": turn_mode},
                 # Display only: the placeholder this turn may narrate into, so the
                 # thread says what is happening instead of going quiet for the
                 # length of the turn. Outside the authorization dict on purpose.
                 "clouddoc_progress": {
                     "reply_id": placeholder_id or "",
                     "lang": "zh" if looks_chinese(t.comment.content) else "en",
                 },
                 "prompt": prompt.text},
            )
            summary["dispatched"] += 1

            await self._settle_turn(
                doc_id, t.comment.comment_id, placeholder_id, answer,
                first_touch=first_touch, correlation=turn_id,
                lang_sample=t.comment.content,
                # Counted **before** the turn, from the listing this trigger was found
                # in, so the comparison afterwards attributes only the turn's own posts.
                agent_replies_before=sum(
                    1 for r in t.comment.replies if r.author_is_self
                ),
            )
            await self._ledger_check(
                doc_id, t.comment.comment_id, answer, dispatched_at
            )
            await self._store.end_inflight(doc_id, turn_id)

    async def _unhighlight_resolved(self, doc_id: str, comments: list) -> None:
        """D8.3's automatic half: a human resolving the thread is the acceptance, and
        acceptance removes that batch's highlights by receipt. Fail-soft everywhere --
        style cleanup must never break the tick."""
        sink = getattr(self._provider, "receipt_sink", None)
        clear = getattr(self._provider, "clear_highlight", None)
        if sink is None or clear is None:
            return
        # Receipts first (a local file read): only when a highlighted, un-cleared
        # batch exists is the resolved view worth an API call -- the tick's own fetch
        # excludes resolved comments, so they are invisible in ``comments``.
        try:
            receipts = [
                r for r in sink.list_for(doc_id)
                if r.get("status") == "applied" and r.get("highlight")
                and not r.get("unhighlighted")
            ]
        except Exception:  # noqa: BLE001
            return
        if not receipts:
            return
        try:
            full = await self._provider.list_comments(doc_id, include_resolved=True)
        except Exception:  # noqa: BLE001
            return
        resolved = {c.comment_id for c in full if c.resolved}
        if not resolved:
            return
        for r in receipts:
            cids = {c for e in r.get("edits", []) for c in (e.get("for_comment_ids") or [])}
            if cids and cids <= resolved:
                try:
                    await clear(doc_id, [e["new"] for e in r["edits"]])
                    sink.mark_unhighlighted(r["receipt_id"])
                except Exception:  # noqa: BLE001
                    logger.exception("[clouddoc] resolve 撤高亮失败 doc=%s", doc_id)

    async def _seed_if_new(self, doc_id: str, comments: list) -> bool:
        """First watch: register the existing trigger points and do nothing this tick.

        What gets registered is **every trigger key computable at this moment**.
        Otherwise switching the feature on re-judges historical comments against a body
        that no longer looks the way it did.
        """
        keys = [
            t.key_for(doc_id)
            for t in find_triggers(
                comments, self._tcfg, doc_id=doc_id, already_triggered=set(),
            )
        ]
        seeded = await self._store.seed_if_new(doc_id, keys)
        if seeded:
            logger.info(
                "[clouddoc] %s first watch: seeded %d existing trigger point(s), dispatching none",
                doc_id, len(keys),
            )
        return seeded

    # ------------------------------------------------------------ posting helpers

    async def _safe_reply_id(self, doc_id: str, comment_id: str, text: str) -> str | None:
        try:
            return await self._provider.reply_comment(doc_id, comment_id, text)
        except ProviderError as exc:
            logger.warning("[clouddoc] placeholder post failed: %s", exc)
            return None

    # ------------------------------------------------- D19 tier 2: report vs ledger

    _CLAIMS_MODIFIED = re.compile(
        r"已(?:直接)?(?:修改|更新|写入|应用|改好|完成修改)|已按要求修改"
        r"|have (?:updated|modified|edited|applied)"
        r"|has been (?:updated|modified|edited)",
    )

    async def _ledger_check(
        self, doc_id: str, comment_id: str, answer: str, dispatched_at: float
    ) -> None:
        """A completion claim must reconcile with the receipt ledger (D19 tier 2).

        The measured failure shape (2026-08-13): the model reported "I have drafted
        the plan in the shared document" with zero writes behind it. On this path
        the success replies that tools post always ride on a real receipt, so the
        lying surface is the model's own words -- the closing answer, and anything
        it posted through reply_comment. Neither needs understanding to audit:
        a claim of modification with **no receipt in the turn's window** is false,
        whatever it says, and the thread gets a mechanical correction so the person
        reads the truth where they read the claim.

        Fail-soft throughout: this is an annotation duty, and no failure in it may
        turn a completed turn into a reported failure. A missing sink cannot
        adjudicate and says so in the log rather than guessing either way.
        """
        try:
            texts = [answer or ""]
            try:
                comments = await self._provider.list_comments(doc_id)
                for c in comments:
                    if c.comment_id != comment_id:
                        continue
                    # Only this agent's own posts are its claims; another agent's
                    # reply in the same thread is that agent's to reconcile.
                    for r in c.replies:
                        if r.author_is_self:
                            texts.append(r.content or "")
            except ProviderError:
                pass  # the answer alone is still worth auditing
            if not any(self._CLAIMS_MODIFIED.search(t) for t in texts):
                return
            sink = getattr(self._provider, "receipt_sink", None)
            if sink is None:
                logger.warning(
                    "[clouddoc] 对账无法进行：声称已修改但回执通道不可用 doc=%s", doc_id
                )
                return
            # Only receipts this turn commissioned reconcile its claim. The ledger
            # is per document, and a chat-path write by the owner during the turn
            # would otherwise vouch for a modification the turn never made.
            executor = f"comment:{comment_id}"
            written = [
                r for r in sink.list_for(doc_id)
                if float(r.get("ts") or 0) >= dispatched_at
                and r.get("executor") == executor
            ]
            if written:
                return
            logger.warning(
                "[clouddoc] 对账不符：本轮声称已修改但零回执 doc=%s comment=%s",
                doc_id, comment_id,
            )
            await self._safe_reply_id(
                doc_id, comment_id,
                "⚠️ 对账提示：本轮回复声称已修改，但没有产生任何写入回执——"
                "修改并未落地。请在本线程再 @ 我一次，或由文档接入方查看日志。",
            )
        except Exception:  # noqa: BLE001 - annotation duty must not fail the turn
            logger.exception("[clouddoc] 对账检查未完成 doc=%s", doc_id)

    async def _settle_turn(
        self,
        doc_id: str,
        comment_id: str,
        placeholder_id: str | None,
        answer: str,
        *,
        first_touch: bool,
        correlation: str = "",
        lang_sample: str = "",
        agent_replies_before: int | None = None,
    ) -> None:
        """Write a turn's outcome back into the thread.

        Both cases have to be written:

        * An empty ``answer`` means a timeout or a transport failure. Leaving the
          "Working on it..." placeholder would stall there forever, so it is replaced
          with readable failure wording.
        * A non-empty ``answer`` **must be written back**, rather than relying on the
          model to call ``reply_comment``. In practice the model often answers in plain
          text, and without writing that back the person who mentioned the agent sees
          nothing at all.

        The write-back is a **fallback, not a transcript**, and only the first of those
        two cases is unconditional. ``answer`` is the model's closing message, which is
        addressed to whatever dispatched the turn rather than to the thread: when the
        model has already posted its own reply, writing it too puts a second post under
        the comment narrating the first. Measured on a six-comment document, every
        thread carried one -- benign as a cover note above a proposal block ("I have
        submitted a proposal to..."), and on a turn that only replied, third-person
        planning prose in somebody's shared document: "The user is suggesting a better
        name... I will reply to the comment to explain that I cannot suggest one."

        So the answer is written only when the model said nothing itself, and the
        placeholder is retracted rather than filled. A failed turn is always written --
        silence there is the stall this method exists to prevent.

        First touch has a placeholder to overwrite; a follow-up has none, so it can only
        start a new reply.
        """
        text = (answer or "").strip()
        if text and agent_replies_before is not None:
            if await self._model_posted(doc_id, comment_id, placeholder_id, agent_replies_before):
                if placeholder_id and first_touch:
                    await self._safe_delete(doc_id, comment_id, placeholder_id)
                return
        if text and not first_touch and reads_as_dispatcher_narration(text):
            # Follow-up turns only. A first touch has a placeholder that must resolve to
            # *something* -- a narration there is bad, a permanent "Working on it..." is
            # worse. A follow-up that closes with narration simply needed no reply.
            logger.info("[clouddoc] dropped dispatcher narration for %s: %.60s", comment_id, text)
            return
        if not text:
            # Sanitise the error: a readable sentence plus a reference that reconciles
            # with the log, and internal exception detail only in the log. Written back
            # verbatim, stack information from the agent runtime lands straight in a
            # user's document -- which has happened.
            text = msg("turn_incomplete", lang_sample, ref=correlation[-12:])
        if placeholder_id and first_touch:
            await self._safe_update(doc_id, comment_id, placeholder_id, text)
        else:
            await self._safe_reply(doc_id, comment_id, text)

    async def _model_posted(
        self, doc_id: str, comment_id: str, placeholder_id: str | None, before: int
    ) -> bool:
        """Did this turn put a reply of the agent's own into the thread?

        The placeholder is excluded by id: the watcher posted it, not the model, and
        counting it would read every first-touch turn as having spoken. Only the
        agent's own posts count: a second agent replying mid-turn is not this
        model having spoken, and reading it so would drop the fallback write-back
        and delete the placeholder over an answer nobody posted.

        A listing failure answers **False** -- the fallback write-back is what keeps a
        mention from going unanswered, and losing it to a transient error is the worse
        of the two mistakes.
        """
        try:
            comments = await self._provider.list_comments(doc_id)
        except ProviderError as exc:
            logger.warning("[clouddoc] post-turn listing failed: %s (%s)", exc, exc.kind)
            return False
        for c in comments:
            if c.comment_id != comment_id:
                continue
            now = sum(
                1 for r in c.replies
                if r.author_is_self and r.reply_id != placeholder_id
            )
            return now > before
        return False

    async def _safe_delete(self, doc_id: str, comment_id: str, reply_id: str) -> None:
        try:
            await self._provider.delete_reply(doc_id, comment_id, reply_id)
        except ProviderError as exc:
            # Already gone, or the thread was deleted mid-turn. The placeholder is
            # cosmetic at this point -- the model's own reply carries the answer.
            logger.warning("[clouddoc] placeholder delete failed: %s (%s)", exc, exc.kind)

    async def _safe_reply(
        self, doc_id: str, comment_id: str, text: str, *, thread: Any = None
    ) -> None:
        """Post a fixed notice, unless the thread already carries it from this account.

        The dedup key stops a notice from being *decided* twice; this stops it from
        being *posted* twice when the decision was recorded and the post itself is
        what the previous pass did not get credit for (a crash between the two, or a
        state file older than the document).

        Only the replies **after the last thing a person wrote** count. A person who
        speaks again after the notice is asking again, and the same answer has to be
        given again: measured 2026-09-02, "you may proceed now" under a standing
        refusal drew silence, which reads as being ignored rather than as refused.
        """
        if thread is not None and any(
            r.author_is_self and r.content == text for r in _since_last_person(thread)
        ):
            logger.info("[clouddoc] %s/%s already holds this notice, not re-posting",
                        doc_id, comment_id)
            return
        try:
            await self._provider.reply_comment(doc_id, comment_id, text)
        except ProviderError as exc:
            logger.warning("[clouddoc] reply failed: %s", exc)

    # The fixed notices the watcher posts from its own hand. Only these are ever
    # deduplicated: a placeholder the turn later edits, or a model's reply, may
    # legitimately repeat and must not be touched.
    # ``watch_suspended`` is here although nothing posts it any more: a thread
    # that already holds one from an earlier release must still be recognised as
    # this watcher's own notice, or the dedup sweep stops seeing it.
    _NOTICE_KEYS = (
        "watch_unauthorized", "watch_suspended", "watch_over_budget",
        "watch_over_budget_legacy",
    )
    # Two copies of one notice further apart than this are two decisions, not one
    # post echoed by the transport.
    _NOTICE_DUP_WINDOW_S = 120.0

    async def _dedupe_own_notices(self, doc_id: str, comments: list) -> None:
        """Delete the later of two identical own notices posted back to back.

        Measured 2026-09-02: one mention hint, one dedup mark, **two** replies 25 s
        apart. The provider issues a single POST, but the HTTP layer underneath
        re-sends a request whose response was lost on a dropped connection, and
        the platform had already applied the first. Nothing above the socket can
        prevent that, so the watcher cleans up after it instead: the person should
        read one notice, and the state that decides whether it was said is the
        dedup key, not the reply count, so removing the echo changes nothing else.
        """
        for c in comments:
            notices = {msg(k, c.content) for k in self._NOTICE_KEYS}
            prev = None
            for r in c.replies:
                echo = (
                    prev is not None
                    and prev.author_is_self and r.author_is_self
                    and prev.content == r.content and r.content in notices
                    and _within(prev.created_time, r.created_time,
                                self._NOTICE_DUP_WINDOW_S)
                )
                if not echo:
                    prev = r
                    continue
                key = (c.comment_id, str(r.reply_id))
                if key in self._dedup_tried:
                    continue
                self._dedup_tried.add(key)
                try:
                    await self._provider.delete_reply(doc_id, c.comment_id, r.reply_id)
                    logger.info("[clouddoc] %s/%s removed a duplicated notice %s",
                                doc_id, c.comment_id, r.reply_id)
                except ProviderError as exc:
                    logger.warning("[clouddoc] duplicate notice not removed: %s", exc)

    async def _safe_update(self, doc_id: str, comment_id: str, reply_id: str, text: str) -> None:
        try:
            await self._provider.update_reply(doc_id, comment_id, reply_id, text)
        except ProviderError as exc:
            # A 404 means the comment or placeholder was deleted: stop retrying
            logger.warning("[clouddoc] placeholder update failed: %s (%s)", exc, exc.kind)
