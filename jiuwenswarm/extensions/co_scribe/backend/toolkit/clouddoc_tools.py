"""CloudDocToolkit: the clouddoc tools.

The tool contract:

* The parameter is always ``doc_id``, accepts a URL or a bare id, and is
  **normalized at the entrance**. Authorization compares the normalized value;
  comparing the raw argument would fail every time a user pastes a link in chat.
* The framework passes every declared parameter, so an optional one arrives as
  ``None`` rather than absent: declare them all, default them all, and treat
  ``None`` as absent.
* Every return is a structured ``{"ok": bool, "detail": str, ...}``.
* **A refusal returns, it never raises.** An exception becomes a ``Tool execution
  error`` and trips ``ToolCallResilienceRail``, dressing up a deterministic safety
  refusal as a transient fault.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from jiuwenswarm.extensions.co_scribe.backend.toolkit.cards import (
    LocalFunction,
    Tool,
    ToolCard,
)

from .providers.provider import DocProvider, PageAdded, ProviderError, list_accessible, summons

logger = logging.getLogger(__name__)

# Timeout for one tool call. The framework defaults to 300s; every clouddoc tool is a
# single API call, so 60s leaves ample room for retries while staying far below
# turn_timeout_seconds (540), and one stuck call cannot eat the whole turn.
_TOOL_TIMEOUT_S = 60

# Tools permitted in an unattended session. **A literal list, never set subtraction**:
# written as a subtraction, a newly added tool would quietly join the allowlist.
#
# IC-1 (PR2b): the closed set is a **family keyed by watch mode**, not one set.
# There is now exactly one mode -- ``apply_scoped``, the only level a watch can be
# granted at -- so the family has one member. The keying stays: an unknown or
# missing mode must resolve to the **strictest** answer, and the strictest answer
# is an **empty set**, not some other family's tools.
#
# The retired ``reply_only`` family lived here. Its tools were read + reply, which
# every turn already has without any mandate, so as a "safety tier" it delegated
# nothing but the right to run a turn at all -- and refusing to run the turn is
# strictly safer than running a narrowed one.
UNATTENDED_FAMILIES: dict[str, frozenset[str]] = {
    "apply_scoped": frozenset(
        {
            "clouddoc_read",
            "clouddoc_list_comments",
            "clouddoc_apply_for_comment",  # the bounded direct-apply primitive (PR2c)
            "clouddoc_reply_comment",
        }
    ),
}


def unattended_allowlist_for(mode: str | None) -> frozenset[str]:
    """The closed set for one unattended turn. Missing/unknown mode -> **empty**.

    The empty set is the last line of defence, not the working path: with the
    watch registry now binary, the watcher does not dispatch at all without an
    ``apply_scoped`` mandate, so a turn carrying any other mode should not exist.
    If one ever does -- a stale in-flight payload, a ledger from another release,
    a bug -- it gets no tools rather than a narrower toolset, because there is no
    such thing as a safer toolset here: every tool below apply is one the agent
    can already use outside a mandate.

    Inverse operations are stripped here too (IC-5). A family is a hand-written set and
    a future edit could add one to it by mistake; subtracting them at the one place
    every caller goes through means that mistake cannot reach a turn.
    """
    family = UNATTENDED_FAMILIES.get(mode or "", frozenset())
    return frozenset(
        t for t in family if not t.startswith(UNATTENDED_FORBIDDEN_PREFIXES)
    )


# Every tool this toolkit registers, and the one place the names are written down.
#
# Four things need this list and each used to keep its own copy: the toolkit that
# builds them, the team runtime's whitelist, the scene hook's branch, and the
# permission config. A list copied four times drifts, and the drift is silent -- a tool
# missing from the team whitelist simply does not appear, with no error anywhere.
#
# The order is the order a person meets them: read, then discuss, then change, then
# the working-style pair.
#
# **Thirteen.** The as-built list has drifted twice already (seven in the first draft,
# then nine when the workmode pair landed without updating the count) -- which is the
# same drift this constant exists to stop, caught by writing the list down in one
# place and comparing it with what the toolkit actually builds.
ALL_TOOL_NAMES: tuple[str, ...] = (
    "clouddoc_read",
    "clouddoc_list_comments",
    "clouddoc_reply_comment",
    "clouddoc_list_documents",
    "clouddoc_create_document",
    "clouddoc_share_document",
    "clouddoc_trash_document",
    "clouddoc_batch_edit",
    "clouddoc_write_region",
    "clouddoc_add_page",
    "clouddoc_apply_for_comment",
    "clouddoc_workmode_get",
    "clouddoc_workmode_edit",
)


# The widest family, kept under the historic name: registration-time stripping used
# it as "everything an unattended turn could ever hold", and that reading stays
# true -- the per-mode narrowing below still applies at tool selection.
UNATTENDED_ALLOWLIST = UNATTENDED_FAMILIES["apply_scoped"]

# Named explicitly and refused unconditionally, so no future set operation can drag
# them back in.
UNATTENDED_DENYLIST = frozenset(
    {
        "clouddoc_batch_edit",
        "clouddoc_write_region",
        # A comment authorizes the passage it marks. A new worksheet or slide is by
        # definition outside every passage, so no comment can ever be the warrant
        # for one -- this is a structural refusal, not a caution.
        "clouddoc_add_page",
        "clouddoc_list_documents",
        "clouddoc_create_document",
        # The other two lifecycle acts: a comment must not be able to hand out access
        # or make a document disappear.
        "clouddoc_share_document",
        "clouddoc_trash_document",
        # workmode is deployer configuration: an unattended turn editing its own
        # working style is the conventions-cannot-change-conventions rule (§4.8.6).
        "clouddoc_workmode_get",
        "clouddoc_workmode_edit",
    }
)

# IC-5: reverting is authorised only from the principal's attended surfaces -- chat and
# the history view -- and never handed to an unattended turn, or a crafted comment could
# talk the agent into undoing the last batch and erase someone's legitimate edit.
#
# Today this holds because reverting is not an agent tool at all: it lives on the panel's
# websocket methods, which a turn cannot reach. That is an accident of where the feature
# landed rather than a rule, so the rule is written down: anything matching this belongs
# in the denylist above the moment it becomes a tool.
UNATTENDED_FORBIDDEN_PREFIXES = ("clouddoc_revert", "clouddoc_unhighlight")

# How many addressed runs a read reports. A grid can be thousands of cells, and a
# whole sheet in one tool result crowds out the document itself; the count of what was
# dropped is reported, because a silently shortened list reads as the whole sheet.
_MAX_CELLS_REPORTED = 200

# A string that is mostly an A1 address, with or without a sheet prefix and with or
# without something appended after a colon. Deliberately narrow: a cell may legitimately
# contain the text "A1", and refusing that would be worse than the failure this catches.
# What it looks for is a coordinate being used *as* the payload -- "Sheet1!A1:大家好".
_ADDRESS_LIKE = re.compile(
    r"^(?:'[^']+'|[A-Za-z][\w ]*)![A-Z]+\d+(?::.*)?$|^[A-Z]+\d+:[A-Z]+\d+$"
)


def _unescape_literals(s: str) -> str:
    r"""Turn the two-character sequences ``\n`` / ``\t`` / ``\"`` into what they denote.

    Only these three, and only as a repair attempt whose result must still be found in
    the body -- a blanket ``unicode_escape`` would also mangle real backslashes in the
    document's own text.
    """
    return s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


def _repair_escapes(
    pairs: list[tuple[str, str]], body: str
) -> tuple[list[tuple[str, str]], bool]:
    r"""Fix anchors that carry a literal ``\n`` where the body has a newline.

    Returns the pairs and whether anything was repaired. A pair is only rewritten when
    the original anchor is absent from the body **and** the unescaped one is present, so
    this can never redirect an edit that was already going to land.
    """
    out: list[tuple[str, str]] = []
    changed = False
    for old, new in pairs:
        if old and old not in body:
            fixed = _unescape_literals(old)
            if fixed != old and fixed in body:
                out.append((fixed, _unescape_literals(new)))
                changed = True
                continue
        out.append((old, new))
    return out, changed


def _first_missing_anchor(
    pairs: list[tuple[str, str]], body: str
) -> tuple[int, str] | None:
    """The first (index, old_string) that is not in the body, or None if all are."""
    for i, (old, _new) in enumerate(pairs):
        if old and old not in body:
            return i, old
    return None


def _normalize_for_match(text: str) -> str:
    """Casefold and strip everything that is not a letter or digit.

    Titles and the way people type them differ in spacing, punctuation and case --
    "My Document 01", "my document 01", "「My Document 01」" are one name. Comparing the
    raw strings would refuse a user who did name the document, and a rail that fires on
    correct input gets switched off.
    """
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _ok(**payload: Any) -> dict:
    return {"ok": True, "detail": "", **payload}


def _lifecycle_receipt(
    sink: Any, doc_id: str, op: str, subject: dict, *, commit: bool = True,
    executor: str = "chat",
) -> str:
    """Record one lifecycle act. Fail-soft on the ledger itself: the act the person
    approved must not die on audit plumbing, but the caller gets "" back and the
    panel shows nothing for it -- the same posture the edit path takes.

    ``executor`` is ``chat`` by default: these tools run only on the attended chat
    path (they are denied unattended), so the person in the conversation is who
    commissioned the act -- an empty executor read as "nobody" in the audit view
    (measured 2026-09-03)."""
    if sink is None:
        return ""
    try:
        rid = sink.begin(doc_id, [], highlight=False, executor=executor, source="",
                         op=op, subject=subject)
        if commit:
            sink.commit(rid, revision_after=None)
        return str(rid)
    except Exception:  # noqa: BLE001
        logger.exception("[clouddoc] lifecycle receipt failed op=%s doc=%s", op, doc_id)
        return ""


def _write_refusal(exc: ProviderError) -> dict:
    """A platform failure on a write, in the structured form every other refusal takes.

    Two outcomes hide behind one exception type. A failure the platform reported
    (rate limit, lost permission, conflict) means nothing landed. An ``unknown`` means
    the platform did not say -- the request may have been applied -- and the one thing
    the caller must not do with that is resend the same batch, because a blind retry
    of a write that already landed is how one edit becomes two. The refusal says which
    it was, and forbids the retry in words the model acts on.
    """
    if exc.kind == "unknown":
        return _fail(
            f"写入结果未知：平台没有确认这批改动是否落地（{exc}）。"
            "**不要原样重发**——先用 clouddoc_read 回读当前正文核对，"
            "确认没有落地后再决定是否重写；回执账本里这笔写入以实际记录为准。",
            status="unknown",
        )
    return _fail(f"写入失败（{exc.kind}）：{exc}。本批未落地。", status=exc.kind)


def _fail(detail: str, **payload: Any) -> dict:
    """A refusal, in both vocabularies.

    Every guardrail in this toolkit refuses by explaining -- which markdown marker is in
    the way, which document was not read first, why a range cannot be widened -- and those
    sentences are the feature, not decoration: they are how "the rule is code, not prompt"
    reaches the person and the model that has to act on it.

    They were reaching neither. The refusal travelled in ``detail``, the runtime's tool
    envelope reports ``error``, and nothing bridged the two: a batch edit refused for
    containing ``##`` arrived at the model as ``success=False error=''`` -- a failure with
    no reason, after which it narrated that it had written the document and stopped.
    Measured on a live turn, 2026-09-10.

    Both keys carry the same sentence. ``detail`` is what this toolkit's own callers and
    the panel read; ``error`` is what the envelope surfaces. An explicit ``error`` in
    ``payload`` still wins, for the few places that distinguish them.
    """
    return {"ok": False, "detail": detail, "error": detail, **payload}


def _region_preview(content: list[list[str]]) -> str:
    """One line of a region's content, for a reply a person reads in a thread."""
    return " / ".join(" ".join(cell for cell in row if cell) for row in content).strip()


# ---------------------------------------------------------------- D16: effect classes
#
# What kind of thing each tool does, on the two axes that matter for a permission
# mode: blast radius and reversibility. A mode decides where the ask line sits; it
# never decides what a tool *is*. Two classes sit below a floor no mode can lift:
# ``grant`` changes authorization itself (what Full Access hands out is the power to
# act, never the power to grant further -- M3's counterpart on the chat path), and
# ``irreversible_write`` is a write whose misjudgment cannot be absorbed afterwards.
#
# ``revertible_write`` is a mechanical fact, not a label: it holds only while the
# write path produces content-complete receipts. The writers re-check that at call
# time and demote themselves -- a broken receipt chain must tighten the gate, not
# loosen the bookkeeping (C9's logic: capability lost, tier lowered).
EFFECT_CLASSES: dict[str, str] = {
    "clouddoc_read": "read",
    "clouddoc_list_comments": "read",
    "clouddoc_list_documents": "read",
    "clouddoc_workmode_get": "read",
    "clouddoc_reply_comment": "communicate",
    "clouddoc_batch_edit": "revertible_write",
    "clouddoc_write_region": "revertible_write",
    "clouddoc_add_page": "revertible_write",
    "clouddoc_apply_for_comment": "revertible_write",
    # A workmode edit's inverse is the same edit reversed, and the file is local
    # deployment style, not a shared document.
    "clouddoc_workmode_edit": "revertible_write",
    # Creation *and sharing* in one act: the addresses gain access they did not have.
    # That is a change to authorization, not to content.
    "clouddoc_create_document": "grant",
    "clouddoc_share_document": "grant",
    # Trashing keeps the document (Google restores it; Feishu keeps it in the recycle
    # bin for a person to restore), but it takes the document away from everyone who
    # had it, and on Feishu no call brings it back: floor-class, always asked.
    "clouddoc_trash_document": "irreversible_write",
}


def _ask_channel_available() -> bool:
    """Whether this session has a confirmation channel at all.

    The UI's Full Access maps to ``permissions.enabled: false``, and a disabled
    permission system does not soften the rail -- **the rail is never built**. So
    "always ask" cannot be implemented as asking there; its honest realization is a
    refusal that names the way back. Unreadable config reads as no channel: a floor
    that cannot verify its ground must hold, not yield.
    """
    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.deployment import (
            deployment_config,
        )

        perms = deployment_config().get("permissions") or {}
        return bool(perms.get("enabled", False))
    except Exception:  # noqa: BLE001 - fail-closed: unverifiable = floor active
        return False


def _ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _edit_line(lang: str, old_s: str, new_s: str) -> str:
    """One line of the "what changed" list, showing the change rather than the pair.

    Printed whole, an edit that only **adds** text renders as ``X → X…``: the new
    string starts with the old one, so the reader sees the same sentence twice and has
    to read to the end to find the difference. Observed on a live Feishu document --
    a 15-character sentence replaced by itself plus 1135 characters of translation,
    rendered as two identical-looking quotes.

    It matters most exactly where it went wrong. On a platform whose write channel has
    no highlight, this list is the **only** place the reader learns what changed, so a
    line that reads as "nothing happened" defeats the mechanism that stands in for the
    colour.

    The common prefix and suffix come off first -- the same trimming the Google write
    path already does to decide what to paint, and the panel already does to render a
    whole-document receipt as the span it changed. This is the third place that needed
    it and the one that did not have it.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        _trim_common_affixes,
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import tool_msg as msg

    if not new_s:
        return msg("edit_line_deleted", lang, old=old_s)
    pre, old_mid, new_mid = _trim_common_affixes(old_s, new_s)
    if not old_mid and new_mid:
        # A pure addition. Name where it landed, using the tail of what it followed,
        # so the reader can find the place without re-reading the whole passage.
        where = _ellipsize(old_s[max(0, pre - 24):pre].strip() or old_s.strip(), 24)
        return msg("edit_line_added", lang,
                   where=where, n=len(new_mid), added=_ellipsize(new_mid.strip(), 80))
    # A replacement prints the pair whole, the way it always has. Trimming it to the
    # difference was tried here and withdrawn: the reader's next move is to find the
    # old text in the document, and a quote silently shortened at either end -- even
    # by no more than the full stop the two halves share -- is no longer the string
    # they can search for. Trimming earns its place only in the branch above, where
    # the pair is otherwise unreadable; a replacement was never unreadable.
    return msg("edit_line", lang, old=old_s, new=new_s)


def _declared_scope(edit: Any) -> str:
    """The structural tier one edit declares, from the closed menu (D23).

    Anything unrecognised resolves to ``exact`` -- the selection itself. The fail
    direction must never widen: a typo, a translated word, or a tier this build does not
    know must not buy a bigger window than the person marked.

    The menu is read from the rails package beside this module, and the import is
    guarded so a deployment stripped of the rails keeps its write tool. With no rail
    there is nothing to enforce a widened window, so every tier resolves to ``exact``
    -- the one direction that is safe to fail in.
    """
    if not isinstance(edit, dict):
        return "exact"
    raw = str(edit.get("scope") or "").strip().lower()
    if not raw:
        return "exact"
    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import SCOPES
    except Exception:  # noqa: BLE001 - no rail, no widening
        return "exact"
    return raw if raw in SCOPES else "exact"


def _declared_comment_ids(edit: Any) -> list[str]:
    """The comment ids one edit declares, from either spelling.

    ``for_comment_ids`` serves several comments on the same passage (D5); the older
    singular name stays valid and means a list of one, so a caller that scopes an edit
    the way PR1 did keeps working.
    """
    if not isinstance(edit, dict):
        return []
    raw = edit.get("for_comment_ids")
    if raw is None:
        raw = edit.get("for_comment_id")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        cid = str(item or "").strip()
        # De-duplicated in order: the same id twice is one comment, and letting it
        # through twice would put a duplicate in the receipt's mapping.
        if cid and cid not in out:
            out.append(cid)
    return out


def _no_quote_detail(kind: str) -> str:
    """Why a comment carries no quoted text, diagnosed per format.

    Measured on live Google comments (2026-09-10). A comment left on a **cell range**
    stores no ``quotedFileContent`` at all -- the field is absent, not empty -- and its
    anchor is ``{"type":"workbook-range","uid":0,"range":"<opaque id>"}`` whose id
    resolves through no public Sheets surface: ``namedRanges``, ``developerMetadata``
    and ``protectedRanges`` were all empty on the very spreadsheet carrying that
    comment. A comment on a **single cell** does carry that cell's value, which is how
    the quote pathway works there at all.

    So on a spreadsheet an absent quote is the platform's ordinary answer to a
    multi-cell selection, not a stale anchor. Saying "the anchor expired" sends the
    reader hunting a fault that is not there and hides the one move that works --
    select one cell, or say which cells in chat, where the write is behind an ask and
    ``clouddoc_write_region`` addresses cells directly.
    """
    if kind == "spreadsheet":
        return (
            "这条批注没有可用的修改边界：Google 对表格的**多格选区**不保存引用文本，"
            "区域锚点也无法解析成格子地址。请选中**单个格子**重新评论，"
            "或到 Jiuwen chat 里说明要改哪些格子（chat 路径可逐格直改）。"
        )
    return "没有引用任何正文（锚点已失效）"


def _turn_instruction(bound: Any, me: str) -> str:
    """The text this turn is answering: the newest person-authored reply that
    mentions this agent and is newer than the agent's first post in the thread,
    or the comment itself when there is none.

    Mirrors the watcher's own anchor (the agent's first post, else the comment's
    time) so the rail judges the same request the trigger fired on. Only a person's
    reply counts, and only one that names this agent: the same two conditions that
    make a reply a follow-up summons.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import normalize

    content = str(getattr(bound, "content", "") or "")
    me_n = normalize(me or "")
    if not me_n:
        return content
    replies = list(getattr(bound, "replies", ()) or ())
    spoke = [r.created_time for r in replies if r.author_is_self and r.created_time]
    anchor = min(spoke) if spoke else (getattr(bound, "created_time", "") or "")
    newest: Any = None
    for r in replies:
        if not r.created_time or r.created_time <= anchor:
            continue
        if r.author_is_self or r.author_is_service_account:
            continue
        if not any(normalize(a) == me_n for a in (r.mentioned_addresses or ())):
            continue
        if newest is None or r.created_time > newest.created_time:
            newest = r
    return str(newest.content or "") if newest is not None else content


class CloudDocToolkit:
    """One instance per session, caching the provider's revision so a read can be
    chained into an edit.

    ``turn_doc_id`` is where this turn's authorization scope is read: it returns the
    ``doc_id`` from the turn's metadata, or ``None`` on the chat path, which imposes
    no constraint. The real reader lives on the agentserver side; this stays an
    injectable seam so it can be tested offline.
    """

    def __init__(
        self,
        provider: DocProvider,
        *,
        turn_doc_id: Callable[[], str | None] | None = None,
        turn_comment_id: Callable[[], str | None] | None = None,
        turn_progress: Callable[[], dict] | None = None,
        turn_mode: Callable[[], str | None] | None = None,
        rail_overrides: dict | None = None,
        turn_address: Callable[[], str] | None = None,
        watched_docs: Callable[[], list] | None = None,
        connection_count: Callable[[], int] | None = None,
        user_text: Callable[[], str] | None = None,
        workmode_file: str | None = None,
        workmode_prefer_zh: bool = True,
        executor_label: str | None = None,
        ask_channel: bool | None = None,
        grant_checker: Callable[[str], bool] | None = None,
        harness_mode: str = "mandate",
    ) -> None:
        # IC-3's pre-write query point, as a protocol rather than a registry class:
        # the mechanism needs "is this document's write authority still live", not a
        # particular storage. None keeps today's default (the gateway's cross-process
        # registry, imported lazily below) so every existing constructor is unchanged;
        # the library extraction swaps the default for an injected checker.
        self._grant_checker = grant_checker
        # D21: the deployment's chosen relationship with the harness. "mandate"
        # (default) is the full machinery; "direct" is the deliberate baseline --
        # adoption plus direct editing, errors normal, nothing to undo them with;
        # "recorded" is the config-only ablation tier (receipts without
        # interception) and appears in no UI. The subtractions below key off this
        # one field; an unknown value falls back to mandate, never to bare.
        self._harness_mode = harness_mode if harness_mode in ("mandate", "recorded", "direct") else "mandate"
        # Read-before-write (mandate mode): the documents this surface has read
        # in its lifetime. Shared documents change under other people's hands, so
        # writing from memory of an unread document is how their edits get
        # clobbered. Formerly a per-persona rail; sunk here so every agent and
        # every surface carries it, not just the ones wearing the right card.
        self._read_docs: set[str] = set()
        # Who commissions this surface's writes, for the receipt ledger. The chat
        # path's default is the person in the chat; an exported surface (the MCP
        # server) names its caller instead -- ``mcp:<client>`` -- so the history
        # separates a CLI's work from a conversation's at a glance (ring ⑤).
        self._executor_label = str(executor_label or "chat")
        # Whether this surface has a confirmation channel at all. None means "ask
        # the deployment config" (the in-process default); an exported surface
        # passes False outright -- there is no dialog to raise, whatever the
        # config says -- which pins the D16 floor open-eyed rather than by luck.
        self._ask_channel_override = ask_channel
        self._provider = provider
        # One document's text domain, cached. A connection reaches several formats now,
        # so the markup rule is a property of the document rather than of the provider;
        # the rail and the prompt must still read one answer, which is why it is looked
        # up here rather than guessed at each call site.
        self._domain_cache: dict[str, str] = {}
        self._turn_doc_id = turn_doc_id or (lambda: None)
        self._turn_comment_id = turn_comment_id or (lambda: None)
        self._turn_progress = turn_progress or (lambda: {})
        # The watch level the turn was dispatched under (IC-1), read from the same
        # snapshot as the ids. The pre-write checkpoint hands it to the registry so
        # a tier changed mid-turn intercepts the write rather than draining; None
        # (the chat path, or a surface without the snapshot) checks liveness only.
        self._turn_mode = turn_mode or (lambda: None)
        # Deployment overrides for the range rail's caps (config ``clouddoc.rail``).
        # The gateway used to build these into the watcher's config, whose reader
        # retired with the proposal machinery -- leaving config keys that silently
        # did nothing. The rail runs here now, so the overrides land here.
        self._rail_overrides = dict(rail_overrides or {})
        self._turn_address = turn_address or (lambda: "")
        # Per-document identity, read through ``_address_for``: a routed provider
        # answers with the owning connection's address, a single provider with the
        # turn's. Both paths must recognise the same mentions.
        # Which documents this connection watches. **The chat path has no doc_id
        # injected**, so without this the agent cannot name a single document it is
        # allowed to touch -- it asks the user to paste a link even though the panel
        # is already listing the document. Observed on the first live chat run.
        self._watched_docs = watched_docs or (lambda: [])
        self._workmode_file = workmode_file
        self._workmode_prefer_zh = workmode_prefer_zh
        # A toolkit holds exactly one provider, hence one account, so the document list
        # covers **one connection**. With several configured, "there is only one document"
        # would be a statement about this connection that the model reads as a statement
        # about the deployment -- and the single-document fast path would then write into
        # a document the user never meant. The count is what lets the tool say so.
        self._connection_count = connection_count or (lambda: 1)
        # **Everything the user typed in this session, and nothing the model wrote.**
        # This is the only signal in the toolkit that the model cannot manufacture, and
        # the ambiguity rail below is built entirely on it. Reading the model's own
        # arguments would prove nothing: the model picks a doc_id and passes it, which
        # looks identical whether the user named the document or the model guessed.
        self._user_text = user_text or (lambda: "")

    # ------------------------------------------------------------ authorization

    @property
    def _mandate(self) -> bool:
        """True when the full harness governs this surface (D21)."""
        return self._harness_mode == "mandate"

    def _ask_channel(self) -> bool:
        """This surface's confirmation-channel fact, override first (see __init__)."""
        if self._ask_channel_override is not None:
            return bool(self._ask_channel_override)
        return _ask_channel_available()

    async def _rail_cfg(self, doc_id: str, **over) -> Any:
        """The range-rail config for one document.

        Two things are the document's rather than the deployment's. The text domain,
        because a Feishu docx and a spreadsheet in one tenant disagree about whether an
        asterisk is content. And the quote limit, because the provider **measured** where
        its platform truncates a quote -- Google at about 418 code points -- while the
        rail compared against a configured default of 400 and never asked, refusing
        quotes in between that had not been truncated at all.

        That wiring existed once, in the watcher, attached to the apply machinery that
        was later retired -- so deleting the dead code took the live reader with it and
        the measurement went back to doing nothing. It sits here now because here is
        where the rail actually runs.
        """
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import RangeRailConfig

        allowed = {"adjacent_budget", "max_quote_chars", "max_insert_chars", "max_edits"}
        conf = {k: int(v) for k, v in self._rail_overrides.items()
                if k in allowed and v is not None}
        cfg = RangeRailConfig(text_domain=await self._text_domain(doc_id),
                              **{**conf, **over})
        try:
            caps = await self._provider.capabilities(doc_id)
        except Exception:  # noqa: BLE001 - a rail that cannot ask still has a default
            return cfg
        measured = getattr(caps, "max_quote_chars", None)
        if measured is None:
            return cfg
        from dataclasses import replace as _replace

        return _replace(
            cfg, max_quote_chars=int(measured),
            # Markless truncation makes length-at-limit mean "cut off": the rail must
            # refuse those as truncated, not anchor the fragment (D22: a silently
            # shortened quote is a silently shortened grant).
            quote_hard_limit=(
                int(measured)
                if getattr(caps, "quote_truncates_unmarked", False)
                else None
            ),
        )

    async def _text_domain(self, doc_id: str) -> str:
        """This document's text domain, for the range rail's markup check.

        Falls back to the provider-wide value when a provider cannot answer for one
        document: refusing a legitimate proposal is a worse day than a
        slightly wrong markup rule, but far better than a tool call that raises.
        """
        cached = self._domain_cache.get(doc_id)
        if cached is not None:
            return cached
        domain = getattr(self._provider, "text_domain", "plain")
        fn = getattr(self._provider, "text_domain_for", None)
        if fn is not None:
            try:
                domain = await fn(doc_id)
            except Exception:  # noqa: BLE001
                pass
        self._domain_cache[doc_id] = domain
        return domain

    async def _resolve(self, doc_id: str | None) -> tuple[str | None, dict | None]:
        """Normalize doc_id and compare it against this turn's authorization scope.

        Returns ``(canonical_doc_id, error_payload)``; when error_payload is not None
        the caller should hand it straight back to the model.

        **On an unattended turn a missing argument falls back to the bound value.**
        Observed without it: the model replies asking for the document ID and stops --
        it has no way to know that 44-character string. This loosens nothing, because
        the fallback is precisely the value the gateway sent down, and an explicitly
        passed one must still match exactly.
        """
        bound = self._turn_doc_id()
        if bound:
            # **Unattended: the bound document wins outright, whatever was passed.**
            # Comparing and refusing on a mismatch was the earlier rule, and it deadlocks
            # a turn it should have rescued. The id is 44 opaque characters the model has
            # no way to know, yet the generated schema marks the parameter required, so a
            # model with nothing to put there invents one -- measured: two turns in a row
            # sent fabricated ids (``9bb0542d2b3b2deb``) while the gateway had bound the
            # real ``1vIKnZmo...``. Each was refused, each refusal told the model only
            # that something was unauthorized, and both turns died having written nothing.
            #
            # Overriding is **stricter** than refusing, not looser: the write lands on
            # the gateway's document by construction, so a wrong id cannot reach a
            # document either way. What changes is that being wrong is no longer fatal.
            # The discrepancy still has to be visible, hence the log.
            if doc_id:
                try:
                    passed = self._provider.parse_doc_ref(doc_id)
                except ProviderError:
                    passed = doc_id
                if passed != bound:
                    logger.warning(
                        "[clouddoc] 模型传入的 doc_id 与本轮绑定不符，按绑定值执行: "
                        "passed=%s bound=%s", passed, bound,
                    )
            return bound, None
        if not doc_id:
            return None, _fail("缺少 doc_id")  # missing doc_id
        try:
            canonical = self._provider.parse_doc_ref(doc_id)
        except ProviderError as exc:
            return None, _fail(str(exc))

        if bound is None:
            # Chat path. **The ambiguity rail lives here, at the one chokepoint every
            # document-taking tool passes through**, rather than on each tool: read,
            # list_comments, propose_edit, reply_comment and batch_edit all resolve
            # through this method, and a tool added later inherits the rail instead of
            # needing someone to remember it.
            #
            # Reading is gated too, not only writing. The failure this comes from never
            # reached a write: the model picked the wrong document, **read** it, found
            # content that happened to match the request, and told the user it had
            # drafted the plan there. A belief formed about the wrong document becomes a
            # false report, which is its own kind of damage.
            ambiguous = await self._ambiguous_target(canonical)
            if ambiguous is not None:
                return None, ambiguous
            return canonical, None
        if canonical != bound:
            # Pointing at another document inside an unattended session: a deterministic
            # refusal, not a fault.
            return None, _fail(
                "本轮只授权操作当前文档，请求的文档不在授权范围内"
            )
        return canonical, None

    def _resolve_comment(self, comment_id: str | None) -> tuple[str | None, dict | None]:
        """Narrow where a post can go to the one comment that triggered this turn.

        The same two reasons as doc_id: the model does not know the id, and binding it
        also tightens authorization -- unbound, an unattended agent could post a
        proposal under **any** comment in the document, including a thread other people
        are using.

        ``comment_id`` is an opaque id generated by Drive, not text anyone can write,
        so putting it in the authorization payload does not violate the rule that the
        scope carries no untrusted text.
        """
        bound = self._turn_comment_id()
        if bound:
            # Same rule as doc_id, and the same measured failure: the model sent
            # ``9bb0542d2b3b2deb`` for a thread Drive calls ``AAACB_z5wdc``. The bound
            # comment is the only one this turn may post under either way -- overriding
            # says so by construction instead of refusing and leaving the model to guess
            # again.
            if comment_id and comment_id != bound:
                logger.warning(
                    "[clouddoc] 模型传入的 comment_id 与本轮绑定不符，按绑定值执行: "
                    "passed=%s bound=%s", comment_id, bound,
                )
            return bound, None
        if not comment_id:
            return None, _fail("缺少 comment_id")
        if bound is not None and comment_id != bound:
            return None, _fail("本轮只授权回复触发这次作业的那条评论")
        return comment_id, None


    async def _check_comment_scopes(
        self,
        doc_id: str,
        snapshot: Any,
        pairs: list[tuple[str, str]],
        scoped: list[tuple[int, tuple[str, ...], str]],
    ) -> dict | None:
        """Validate edits that declare ``for_comment_id`` against that comment's range.

        The rail is imported lazily, so a deployment stripped of the rails package
        does not lose its write tool entirely.

        A failed import degrades in two different directions, and telling them apart
        matters more since D5. Narrowing an edit to one comment's window is a usability
        precheck on this path -- a person is present and the write is behind an ask --
        so losing it costs precision, and the call goes through with a warning. But a
        declaration naming **several** comments asks for a merged window, and the code
        that decides whether those comments actually overlap is the same import. Letting
        that through unchecked is exactly the widening IC-4 exists to stop: two comments
        at opposite ends of a document would be treated as one range covering
        everything. Multi-comment scopes are therefore refused when the rail cannot run,
        while single-comment ones stay a precheck.

        Unknown comment ids are refused rather than skipped. A model that fabricates an
        id would otherwise buy itself an unscoped edit while looking scoped -- the same
        shape as the fabricated doc_id incident, closed the same way.
        """
        self._scoped_window = None
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import (
                check_range,
                union_window,
            )
        except Exception:  # noqa: BLE001
            merged = [cids for _, cids, _sc in scoped if len(cids) > 1]
            if merged:
                return _fail(
                    "范围轨不可用，无法校验多条批注是否引用同一处正文，"
                    "因此拒绝合并修改。请为每条批注分别提交一处修改。"
                )
            logger.warning("[clouddoc] range rail unavailable, comment scopes unchecked")
            return None
        try:
            comments = await self._provider.list_comments(doc_id, include_resolved=True)
        except ProviderError as exc:
            return _fail(f"无法读取评论以校验修改范围（{exc.kind}）：{exc}")
        quoted_by_id = {c.comment_id: (c.quoted_text or "") for c in comments}
        cfg = await self._rail_cfg(doc_id)
        # Keyed by the comments **and** the declared tier: two edits serving one comment
        # at different tiers are two different authorizations, not one group.
        by_scope: dict[tuple[tuple[str, ...], str], list[tuple[str, str]]] = {}
        for idx, cids, tier in scoped:
            for cid in cids:
                if cid not in quoted_by_id:
                    return _fail(
                        f"edits[{idx}] 声明的评论不存在：{cid!r}。"
                        "请用 clouddoc_list_comments 取回真实的 comment_id。"
                    )
                if not quoted_by_id[cid]:
                    kind = getattr(snapshot, "kind", "") or ""
                    if kind == "spreadsheet":
                        return _fail(
                            f"评论 {cid} 无法作为修改范围。" + _no_quote_detail(kind)
                        )
                    return _fail(
                        f"评论 {cid} 没有引用任何正文（锚点已失效），无法作为修改范围。"
                        "请让用户确认要改哪一句，改用普通编辑。"
                    )
            by_scope.setdefault((tuple(cids), tier), []).append(pairs[idx])

        approved: list[tuple[int, int]] = []
        for (cids, tier), group in by_scope.items():
            quotes = [quoted_by_id[c] for c in cids]
            # One declared comment keeps the single-quote path, so its diagnostics stay
            # the ones a model has been answering to. Several go through the merge,
            # which refuses ids whose text does not actually overlap (IC-4).
            if len(quotes) == 1:
                check = check_range(snapshot, quotes[0], group, cfg, scope=tier)
            else:
                # A merged window is already a claim that several selections describe one
                # place. Widening it structurally on top would compound two widenings, and
                # the unattended path does not offer that either -- merged stays exact.
                if tier != "exact":
                    return _fail(
                        f"为多条评论（{', '.join(cids)}）声明的合并修改不能同时声明 "
                        f"scope={tier!r}。合并窗只支持 exact；要按结构放宽，请对每条评论"
                        "分别提交一处修改。"
                    )
                merged, failure = union_window(snapshot, quotes, cfg)
                if failure is not None:
                    return _fail(
                        f"为评论 {', '.join(cids)} 声明的合并修改无法成立：{failure.detail}。"
                        "**整批未做任何修改。**",
                        verdict=failure.verdict.value,
                    )
                check = check_range(snapshot, quotes[0], group, cfg, window=merged)
            if not check.ok:
                return _fail(
                    f"为评论 {', '.join(cids)} 声明的修改超出了引用范围：{check.detail}。"
                    "**整批未做任何修改。**请把这一处收窄到评论锚定的句子附近再试。",
                    verdict=check.verdict.value,
                )
            approved.append((check.window_lo, check.window_hi))
        # One scope group means one approved window, and the provider must judge
        # uniqueness by the same rule the rail did -- D23 shrank the rail window to
        # the exact quote, so re-judging across the whole body would refuse edits
        # the rail passed (a quote of "42" is unique in its cell and everywhere in
        # a sheet). Several groups have no single window; those keep whole-body
        # locating, which only ever errs stricter.
        self._scoped_window = approved[0] if len(approved) == 1 else None
        return None


    async def _refuse_non_atomic_batch(self, doc_id: str, count: int) -> dict | None:
        """Refuse a multi-edit batch on a provider that cannot make it one write.

        A provider that does not answer -- because the call failed, or because it does
        not implement the question at all -- is treated as capable. The flag exists to
        let a platform decline, not to make every write depend on one more call
        succeeding; failing closed here would take the write tool away from a provider
        for reasons unrelated to the write.
        """
        try:
            caps = await self._provider.capabilities(doc_id)
        except Exception:  # noqa: BLE001 - see the docstring: silence means capable
            return None
        if getattr(caps, "atomic_batch", True):
            return None
        return _fail(
            f"本平台不支持一次提交多处修改（本次 {count} 处）。"
            "请逐条处理——分多次调用，每次只改一处。"
        )

    async def read(self, doc_id: str | None = None) -> dict:
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        await self._progress("progress_reading")
        try:
            snap = await self._provider.read(canonical)
        except ProviderError as exc:
            return _fail(f"读取失败（{exc.kind}）：{exc}")

        self._read_docs.add(canonical)
        out = _ok(text=snap.text, revision_id=snap.revision_id, doc_id=canonical)
        # **What the body is made of, said where it is about to matter.** The write tool's
        # description already warns that a plain-text body takes markdown markers
        # literally, but a description competes with the person's own phrasing: asked for
        # "four sections", the model reached for ``##`` headings and the write was refused
        # after the whole document had been composed (measured, 2026-09-10). Reading comes
        # first by rule, so the constraint is attached to what was read -- it arrives with
        # the evidence rather than as advice given earlier and further away.
        domain = await self._text_domain(canonical)
        out["body_format"] = domain
        if domain == "plain":
            out["body_format_note"] = (
                "这篇文档的正文是纯文本：markdown 记号（行首 #、列表的 - 或 *、**加粗**）"
                "会原样出现在文档里，写入时会被拒绝。章节标题请写成单独一行的普通文字。"
            )
        # **A spreadsheet needs its coordinates.** The flat text is what the range rail
        # anchors on, and for a document it is the whole story; for a grid it is not.
        # Measured live: a comment asked to "move this to A1", the read returned
        # ``'\n\n\n\n\n\n\t大家好'``, and the agent reasoned in circles for thirteen
        # minutes trying to work out which cell that was -- the addresses were sitting
        # in ``Segment.address`` the whole time and were never passed on.
        #
        # Only for the formats that are addressed by something other than position: a
        # document would gain a long list of runs that say nothing a person or a model
        # can use.
        if snap.kind in ("spreadsheet", "presentation"):
            cells = [
                ({"at": seg.address, "text": snap.text[seg.char_start : seg.char_end], "role": seg.role}
                 if getattr(seg, "role", "") else
                 {"at": seg.address, "text": snap.text[seg.char_start : seg.char_end]})
                for seg in snap.segments
                if seg.address
            ]
            # The format is reported whether or not anything was found. It used to be
            # set only alongside a non-empty cell list, so an empty deck read back
            # indistinguishable from an empty document -- and an agent asked to write a
            # title could not tell what it was looking at.
            out["kind"] = snap.kind
            # **How many pages or worksheets there are, said rather than left to be
            # inferred from the addresses.** A deck's addresses are ``<page>/<object>``
            # and a grid's are ``<sheet>!<cell>``, so the structure is in there -- but
            # only to a reader who parses it. Asked for a three-page deck, the model
            # read a one-page deck holding three empty text boxes, saw three writable
            # addresses, and wrote the three pages into them (observed 2026-09-10). It
            # had ``clouddoc_add_page`` available and no reason to reach for it, because
            # nothing it read said "this deck has one page".
            container = "page" if snap.kind == "presentation" else "sheet"
            sep = "/" if container == "page" else "!"
            names = list(dict.fromkeys(
                seg.address.split(sep, 1)[0]
                for seg in snap.segments if seg.address and sep in seg.address
            ))
            if names:
                out["pages" if container == "page" else "sheets"] = names
                out["structure_note"] = (
                    f"这份文档有 {len(names)} "
                    + ("页幻灯片" if container == "page" else "个工作表")
                    + f"：{'、'.join(names[:12])}"
                    + ("…" if len(names) > 12 else "")
                    + "。**一个地址前缀就是一"
                    + ("页" if container == "page" else "个工作表")
                    + "**，同一前缀下的多个地址是同一"
                    + ("页上的多个文本框" if container == "page" else "个工作表里的多个格子")
                    + "。要新增请用 clouddoc_add_page，不要把它们当成多"
                    + ("页" if container == "page" else "个工作表")
                    + "用。"
                    + ("带 role=notes 的地址是**演讲者备注**，不是页面内容；一页若没有文本框，"
                       "用 clouddoc_add_page 新建一页（自带标题框和正文框），不要把内容写进备注。"
                       if container == "page" else "")
                )
            if cells:
                out["cells"] = cells[:_MAX_CELLS_REPORTED]
                if len(cells) > _MAX_CELLS_REPORTED:
                    out["cells_truncated"] = len(cells) - _MAX_CELLS_REPORTED
                # The formula cells, named rather than merely refused later: an agent
                # that knows which cells it must not touch can say so up front instead
                # of proposing an edit the rail then rejects.
                readonly = [
                    seg.address for seg in snap.segments if seg.readonly_reason
                ][:_MAX_CELLS_REPORTED]
                if readonly:
                    out["formula_cells"] = readonly
        return out

    async def list_comments(
        self, doc_id: str | None = None, include_resolved: bool | None = None
    ) -> dict:
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        try:
            comments = await self._provider.list_comments(
                canonical, include_resolved=bool(include_resolved)
            )
        except ProviderError as exc:
            return _fail(f"读取评论失败（{exc.kind}）：{exc}")
        return _ok(
            comments=[
                {
                    "comment_id": c.comment_id,
                    "author": c.author_display_name,
                    "is_self": c.author_is_self,
                    "created_time": c.created_time,
                    "content": c.content,
                    "quoted_text": c.quoted_text,
                    # A comment left on a shape as a whole quotes nothing but names the
                    # shape; those addresses are the regions apply_for_comment's
                    # ``regions`` form may write into. Empty means the quote is the
                    # only bound.
                    "anchor_regions": list(c.anchor_regions),
                    "resolved": c.resolved,
                    # Which of these is a task and which is only a marker. Without it the
                    # model has to guess, and guessing is how a marker becomes work.
                    "assignee": c.assignee_address,
                    "addressed": bool(self._address_for(canonical)) and any(
                        a.strip().lower() == self._address_for(canonical).lower()
                        for a in c.mentioned_addresses
                    ),
                    "replies": [
                        {
                            "reply_id": r.reply_id,
                            "author": r.author_display_name,
                            "is_self": r.author_is_self,
                            "content": r.content,
                        }
                        for r in c.replies
                    ],
                }
                for c in comments
            ]
        )


    async def reply_comment(
        self,
        doc_id: str | None = None,
        comment_id: str | None = None,
        content: str | None = None,
    ) -> dict:
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        comment_id, cerr = self._resolve_comment(comment_id)
        if cerr:
            return cerr
        if not content:
            return _fail("缺少 content")
        await self._progress("progress_replying")
        # Replying to a resolved comment **reopens it** (measured: Drive stamps the reply
        # with action=reopen). Someone closed that thread on purpose, so resurrecting it
        # as a side effect of answering is not ours to do.
        resolved = await self._resolution_state(canonical, comment_id)
        if resolved is None:
            # Unknown is refused, not waved through: the guard exists to keep a
            # closed thread closed, and "could not tell" answered as "open" would
            # reopen one on exactly the transient it was supposed to survive.
            return _fail(
                "暂时无法确认这条评论是否已解决（平台读取评论列表失败）。"
                "回复一条已解决的评论会把它重新打开，所以本轮不回复；请稍后再试。"
            )
        if resolved:
            return _fail(
                "这条评论已解决；回复它会把它重新打开。如果确实要继续这条线程，"
                "请让文档里的人先重新打开它。"
            )
        try:
            reply_id = await self._provider.reply_comment(canonical, comment_id, content)
        except ProviderError as exc:
            return _fail(f"回复失败（{exc.kind}）：{exc}")
        return _ok(reply_id=reply_id)

    async def _retry_with_repaired_anchors(
        self, canonical: str, pairs: list, snap: Any, result: Any, *, highlight: bool,
    ) -> Any:
        """One repaired resend when the anchors could not be located, or a refusal.

        **A missing anchor is almost always double-escaped newlines.** Observed: the
        model emitted "Phased Release\\nPhase 1 …" -- the two characters backslash and
        n -- where the body holds a real newline, so the anchor could never match. It
        then resent the identical payload 42 times, because "未应用（not_found）" says
        nothing about what is wrong with it.

        Keyed on the anchor being absent from the body, never on a status. The
        providers answer a failed location as ``invalid`` (Google) or ``locate_failed``
        (Feishu); an earlier version keyed on a ``not_found`` no provider produces, so
        the repair and the diagnosis below were dead on both platforms while a fake in
        the test suite kept them green.

        Returns the (possibly new) ``EditResult`` to continue with, or a refusal dict.
        The platform's own failure on the resend is the same structured refusal as on
        the first write.
        """
        if result.ok or result.status == "conflict":
            return result
        repaired, changed = _repair_escapes(pairs, snap.text)
        if changed:
            logger.info("[clouddoc] 锚点含字面 \\n，已修复后重试")
            try:
                result = await self._provider.edit_batch(
                    canonical, repaired, required_revision_id=snap.revision_id or "",
                    window=getattr(self, "_scoped_window", None),
                    highlight=highlight,
                )
            except ProviderError as exc:
                return _write_refusal(exc)
        if not result.ok:
            missing = _first_missing_anchor(repaired if changed else pairs, snap.text)
            if missing is not None:
                return _fail(
                    f"第 {missing[0] + 1} 处的 old_string 在正文里找不到，未做任何修改。"
                    f"给的是 {missing[1][:60]!r}。"
                    "**原样重发不会成功**：请先 clouddoc_read 取回当前正文，"
                    "把 old_string 逐字复制过去——换行要用真正的换行符，不要写成 \\n。",
                    status=result.status,
                )
        return result

    async def _resolution_state(self, doc_id: str, comment_id: str) -> bool | None:
        """Whether the thread is resolved: True, False, or None when it cannot be told.

        Three answers, because the caller acts differently on each. A provider that
        fails to list is ``None``; it used to collapse into ``False`` and the reply
        went ahead on exactly the transient the guard was meant to survive. A comment
        the full listing does not contain is not a resolved one: the platform refuses
        a reply to a comment that does not exist, with its own message.
        """
        try:
            for c in await self._provider.list_comments(doc_id, include_resolved=True):
                if c.comment_id == comment_id:
                    return bool(c.resolved)
        except ProviderError:
            return None
        return False

    async def batch_edit(
        self,
        doc_id: str | None = None,
        edits: list[dict] | None = None,
        highlight: bool | None = None,
    ) -> dict:
        """Write the body directly: several edits, one atomic submission.

        This is the chat path's only write primitive. It replaced a single-edit tool
        because that one was neither atomic nor cheap to confirm -- ten changes meant ten
        confirmations and, on the fifth failure, four changes already in the document.

        Three things it refuses, each for its own reason:

        * **while the document agent has assigned work.** Not because anchors would break
          -- an edit invalidating a proposal is defined behaviour and the apply path
          re-checks before writing -- but because two writers on one document produce
          work that is thrown away. The check is inherently racy: an assignment can
          appear right after it passes, and it changes no document revision, so the
          revision pin does not cover it.
        * **structural markdown.** The body is plain text, so ``## Heading`` lands as
          those characters. Only structural markers are refused, not every asterisk: a
          report that mentions ``*args`` is ordinary, a report full of ``##`` is broken.
        * **writing into a document that is not empty without an anchor.** An empty
          old_string is how a first draft is written; allowing it anywhere would turn a
          replace-what-you-can-uniquely-locate tool into write-anywhere.
        """
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        # D16 demotion: "revertible" is a mechanical fact, and it just stopped being
        # one -- with no receipt sink this write cannot be undone by receipt, which
        # makes it an irreversible write, and irreversible writes sit under the floor:
        # they need a confirmation, and Full Access has no channel to ask through.
        if self._mandate and getattr(self._provider, "receipt_sink", None) is None and not self._ask_channel():
            return _fail(
                "回执通道不可用，这次写入将没有回执记录（降为不可逆写），而当前会话是 "
                "Full Access——没有确认通道，按底线拒绝。请修复工作区 config 目录的"
                "回执存储，或把会话权限切回逐项确认（default）。"
            )
        pairs, perr = _normalize_edits(edits, allow_empty_old=True)
        if perr:
            return _fail(perr)

        # C9: a platform that cannot commit several edits as one write says so, and the
        # batch is refused rather than half-applied. Editing and undoing is not an
        # option here -- the document is shared, so a partial write is visible to
        # everyone reading it, and the undo would race whoever else is typing.
        if len(pairs) > 1:
            deny = await self._refuse_non_atomic_batch(canonical, len(pairs))
            if deny:
                return deny

        marker = _introduces_structural_markup(pairs)
        if marker:
            return _fail(
                f"这段内容含 markdown 记号 {marker!r}，而本文档正文是纯文本——"
                "记号会原样出现在文档里。请写成不带记号的散文，章节标题用单独一行普通文字。"
            )

        try:
            snap = await self._provider.read(canonical)
        except ProviderError as exc:
            return _fail(f"读取失败（{exc.kind}）：{exc}")
        # **On a grid, an address is not content.** Text replacement can say anything a
        # cell could contain, including a string that looks like a coordinate -- and a
        # model with no way to express "move" will reach for exactly that.
        #
        # Observed, on a real spreadsheet: asked to move a value to A1 and having no
        # region primitive available, the agent wrote ``Sheet1!A1:大家好`` **into the
        # cell**, and the range rail passed it because as flat text it is an ordinary
        # replacement. The document was left holding a coordinate as its content.
        #
        # So the refusal names the tool that can do it. A rail that only says no leaves
        # the model to invent another way around.
        if snap.kind in ("spreadsheet", "presentation"):
            # **Emptying a cell here is one-way, so it is refused.**
            #
            # A replacement anchors on the text it is replacing. Once a cell is empty
            # there is no text to anchor on, and nothing can put anything back into it
            # -- the run collapses to zero length and no ``old_string`` can locate it.
            #
            # Observed, on a real spreadsheet: asked to move a value, the agent deleted
            # it in one call and tried to write it back in the next. The delete
            # committed, the write could not anchor, and the sheet was left empty with
            # its content gone. Two calls, no transaction between them.
            #
            # Clearing is a legitimate thing to want; it just has to be said as a region
            # whose new content is empty, which is one write and reversible by writing
            # the old content back.
            emptied = next((old for old, new in pairs if old and not new.strip()), "")
            if emptied:
                return _fail(
                    f"把 {emptied[:20]!r} 删空之后，这个格子就再也写不回去了——"
                    "文本替换要靠被替换的文字定位，空格子没有可定位的东西。"
                    "要清空或移动，请用 clouddoc_write_region 一次写完整个区域，"
                    "例如 at='Sheet1!A7:B7'、values=[['大家好','']]。"
                )

            looks_addressed = next(
                (new for _old, new in pairs if _ADDRESS_LIKE.match(new.strip())), ""
            )
            if looks_addressed:
                return _fail(
                    f"{looks_addressed!r} 看起来是一个单元格地址，而这里写入的是格子的**内容**——"
                    "把地址写进格子只会让它显示成那串文字。"
                    "要移动或重排，请用 clouddoc_write_region 声明整个区域应当变成什么，"
                    "例如 at='Sheet1!A7:B7'、values=[['大家好','']]。"
                )

        # Read-before-write (mandate): a write into a document this surface has
        # never read is a write from memory, and shared documents change under
        # other hands. The unattended apply path is exempt -- its anchor window
        # re-reads by construction.
        if self._mandate and canonical not in self._read_docs:
            return _fail(
                "本会话尚未读过这篇文档。共享文档随时可能被他人修改，"
                "请先 clouddoc_read 拿到当前内容再写入。"
            )
        # The mutex exists to prevent two writers colliding -- which requires the
        # unattended path to be capable of dispatching at all. With no live watch
        # (off, suspended, expired) the assignment can never run, so blocking chat
        # on it manufactured a deadlock: nothing would ever clear the comment. In
        # direct mode there is no unattended path, so no collision either (D21).
        if self._mandate and await self._unattended_live(canonical):
            blocked = await self._summoned_open(canonical)
            if blocked:
                return _fail(
                    f"文档里还有 {len(blocked)} 条 @ 了你、值守回合尚未处理的任务"
                    f"（{', '.join(blocked[:3])}）。现在改正文会和文档侧的处理撞车。"
                    "等值守回合处理完再改，或请用户先解决这些批注；"
                    "不要自行到批注里回复来「标记完成」。"
                )

        creating = not snap.text.strip()
        # Edits that declare which comment they serve are validated against that
        # comment's anchor window -- the same rule, the same code, as the proposal
        # path. Without this the scoping instruction is a prompt-level hope, and
        # measured on a live document four comments produced four whole-paragraph
        # rewrites. Edits without the field keep today's behaviour.
        # D23's structural scope reaches this path too. The menu, the geometry and the
        # disclosure were built for the unattended tool and the rail has taken a
        # ``scope`` argument since; the chat path simply never passed one, so its window
        # was always the bare selection. That was an accidental difference, not a
        # decision -- and it bit: a person selected a sentence, wrote 「这句要解释清楚」,
        # and every rewrite the model tried was refused by one character, because the
        # selection stopped just short of the full stop and ``exact`` cannot reach it.
        # It ended up appending a parenthetical to two characters.
        scoped = [
            (i, _declared_comment_ids(edits[i]), _declared_scope(edits[i]))
            for i in range(len(pairs))
            if isinstance(edits, list) and i < len(edits) and isinstance(edits[i], dict)
        ]
        scoped = [(i, cids, sc) for i, cids, sc in scoped if cids]
        if scoped:
            if creating:
                return _fail("空文档没有评论可作为修改范围，for_comment_id 无效。")
            err = await self._check_comment_scopes(canonical, snap, pairs, scoped)
            if err:
                return err
        # The per-write caps (§5.6) hold on the chat path too, not only under the
        # unattended rail: a person's approval is of the request as described, and
        # eleven edits or a page of new text is not what "change a few things" says.
        try:
            cfg = await self._rail_cfg(canonical)
        except ImportError:
            # No rail package on this deployment: the caps are the rail's, and the
            # chat write still stands behind a person's approval (see the test of the
            # same name). Nothing to compare against, so nothing is refused here.
            cfg = None
        if cfg is not None and len(pairs) > cfg.max_edits:
            return _fail(
                f"一次最多 {cfg.max_edits} 处改动（本次 {len(pairs)} 处，max_edits）。"
                "请分成几批，或先和用户确认范围。"
            )
        grown = sum(max(0, len(new) - len(old)) for old, new in pairs)
        if cfg is not None and grown > cfg.max_insert_chars:
            return _fail(
                f"一次新增正文最多 {cfg.max_insert_chars} 字符（本次净增 {grown}，"
                "max_insert_chars）。请缩短，或分几次写入并逐次确认。"
            )
        anchorless = [i for i, (old, _) in enumerate(pairs) if not old]
        if anchorless and not creating:
            return _fail("只有空文档才能不给 old_string 直接写入；请给出要替换的原文。")
        if creating and len(anchorless) != len(pairs):
            # Silently dropping the anchors and writing the new strings anyway would be
            # the worst of both: the caller asked to replace something that is not there.
            return _fail("文档是空的，没有可替换的原文；请把内容放进一条 new_string。")

        # Which comments each edit answered, so a revert can notify every thread the
        # batch served -- including the several a merged edit answered at once (D5).
        by_old: dict[str, list[str]] = {}
        tiers: list[str] = []
        for i, cids, tier in scoped:
            if cids:
                by_old.setdefault(pairs[i][0], []).extend(
                    c for c in cids if c not in by_old.get(pairs[i][0], [])
                )
                if tier != "exact" and tier not in tiers:
                    tiers.append(tier)
        self._provider.receipt_meta = {
            # A widened window is a thing the owner audits, so the tier rides in the
            # source the same way it does on the unattended path: "batch_edit" alone
            # would hide that this edit reached past the selection someone marked.
            "source": "batch_edit" + (f"({'+'.join(tiers)})" if tiers else ""),
            # A chat write is commissioned by the principal in person; naming the turn
            # that way separates it from a comment-triggered one when the history is
            # read back.
            "executor": self._executor_label,
            "for_comment_ids_by_old": by_old,
        }
        # The receipt metadata stays set across the repaired retry below: that retry
        # is the same commissioned write, and a receipt written without its source
        # and executor is a receipt the history view cannot attribute.
        try:
            try:
                if creating:
                    body = "\n".join(new for _, new in pairs)
                    result = await self._provider.edit_batch(
                        canonical, [("", body)], required_revision_id=snap.revision_id or "",
                        highlight=bool(highlight),
                    )
                else:
                    result = await self._provider.edit_batch(
                        canonical, pairs, required_revision_id=snap.revision_id or "",
                        # The window the rail approved for a single scope group (None
                        # otherwise): both layers must judge uniqueness by one rule.
                        window=getattr(self, "_scoped_window", None),
                        highlight=bool(highlight),
                    )
            except ProviderError as exc:
                return _write_refusal(exc)
            repaired_result = await self._retry_with_repaired_anchors(
                canonical, pairs, snap, result, highlight=bool(highlight),
            )
        finally:
            self._provider.receipt_meta = None
        if isinstance(repaired_result, dict):
            return repaired_result
        result = repaired_result

        if result.status == "conflict":
            return _fail(
                "文档在这次改动准备期间被人编辑过，**本批未做任何修改**。"
                "请重新读取当前正文后再试。",
                status=result.status,
            )
        if not result.ok:
            return _fail(f"未应用（{result.status}）：{result.detail}", status=result.status)
        # Whether the platform pinned the write to the revision that was read. The
        # model was answering "yes, doubly so" for a spreadsheet, whose format has no
        # lock (measured); the fact comes from the capability declaration, and where
        # it is false the result says what protected the write instead.
        try:
            locked = bool((await self._provider.capabilities(canonical)).has_revision_control)
        except Exception:  # noqa: BLE001
            locked = True
        out = _ok(status=result.status, revision_id=result.new_revision_id,
                  edit_count=len(pairs), receipt_id=result.receipt_id,
                  optimistic_lock=locked)
        if not locked:
            out["detail"] = (
                "该格式没有乐观锁：写入前重取比对，但不能保证不覆盖并发编辑。"
                "用户问起时照实说。"
            )
        return out

    async def _ambiguous_target(self, doc_id: str) -> dict | None:
        """Refuse a write when several documents are managed and **the user never said
        which one**. None means the target is settled; otherwise a refusal to hand back.

        This exists because the prompt-level version of the same rule does not hold.
        The tool description already says, in bold, to stop and ask rather than pick by
        order, relevance or recency. Observed anyway on a 26B model with two documents
        managed: it picked the one whose *title sounded more like the request*, reported
        "I have drafted the plan in Q3 Launch Note", and had in fact written nothing at
        all. A rule that a model can decline to follow is not a boundary; this one is
        code, on the write path, and it fails closed.

        **What counts as the user having said which one**: the document's title, id, or
        link appearing in text the user typed. Nothing the model produced is consulted --
        it chose the doc_id being checked here, so treating that as evidence would make
        the check circular.

        A single managed document imposes nothing: there is no other document to confuse
        it with, and the chat path has always let "the shared doc" stand for it.

        The refusal names both the problem and the way out, because the model has to
        recover inside the same turn: ask the user, and the answer -- being user text --
        satisfies this check on the retry.
        """
        # **The unattended path is exempt, and must be.** There the gateway binds the
        # document that triggered the turn, so the target was never the model's to
        # choose; there is also no user text at all, so this check would refuse every
        # comment-triggered job and take the whole watcher path down with it.
        if self._turn_doc_id() is not None:
            return None

        watched = self._watched_docs() or []
        if len(watched) < 2:
            return None

        said = _normalize_for_match(self._user_text())
        # The id goes through the same normalization as the haystack. It did not,
        # once: ``said`` had its hyphens stripped while the raw id kept them, so the
        # id branch could never match -- a user pasting the full document id was
        # still refused. Unnoticed for weeks because people paste titles and links,
        # not bare ids; the MCP surface, whose "user text" is the call arguments,
        # hit it on the first write.
        if _normalize_for_match(doc_id) in said:
            return None
        try:
            title = await self._provider.title(doc_id)
        except ProviderError:
            # Cannot read the title, so cannot prove the user named it. Refusing here is
            # the fail-closed direction: a transient Drive error must not become a licence
            # to write into whichever document the model picked.
            title = ""
        if title and _normalize_for_match(title) in said:
            return None

        names: list[str] = []
        named_by_user: list[str] = []
        for raw in watched:
            try:
                other = str(self._provider.parse_doc_ref(str(raw)))
            except ProviderError:
                continue
            try:
                title = await self._provider.title(other) or other
            except ProviderError:
                title = other
            names.append(title)
            # The id goes through the same normalization as the haystack, for the
            # reason given above: raw, a hyphenated id can never match.
            if _normalize_for_match(title) in said or _normalize_for_match(other) in said:
                named_by_user.append(title)

        # **When the user has already named one, say which.** The rail keeps the whole
        # session's user text, so an earlier answer still counts -- but the model does
        # not carry that forward: observed picking the same wrong document turn after
        # turn even though the user had confirmed the other one minutes earlier. A
        # refusal that only says "the user did not say" leaves it guessing again; naming
        # the confirmed document turns the refusal into a correction it can act on.
        if len(named_by_user) == 1:
            return _fail(
                f"用户点名的是「{named_by_user[0]}」，而这次请求的是另一篇。"
                f"当前纳管 {len(names)} 篇（{'、'.join(names[:5])}）。"
                f"要操作「{named_by_user[0]}」就用它的 doc_id 重试；"
                "若确实是别的那篇，请先问用户确认——写错文档没有撤销。",
                reason="ambiguous_document",
                candidates=names,
                user_named=named_by_user[0],
            )

        return _fail(
            f"当前纳管了 {len(watched)} 篇文档（{'、'.join(names[:5])}），"
            "而用户没有说要改哪一篇——标题、链接、文档 id 都没出现在用户的话里。"
            "**不要替用户挑一篇**：写错文档没有撤销。"
            "请先问用户是哪一篇，拿到明确答复后再改。",
            reason="ambiguous_document",
            candidates=names,
        )

    async def _unattended_live(self, doc_id: str) -> bool:
        """Whether the unattended path could dispatch for this document now.

        The embedding host's registry answers when present; a standalone
        deployment has no watcher at all, so the collision the mutex guards
        against cannot happen and the answer is False."""
        try:
            from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

            return bool(WatchRegistry().check(doc_id).dispatchable)
        except ImportError:
            return False
        except Exception:  # noqa: BLE001 - an unreadable registry must not block chat
            return False

    def _address_for(self, doc_id: str | None) -> str:
        """This connection's own address for ``doc_id``.

        A routed provider knows which connection adopted the document and answers
        with that connection's identity; a single provider, or a document nobody
        adopted, falls back to the turn's address. Normalised the same way the
        watcher normalises its own: stripped, compared case-insensitively by callers.
        """
        fn = getattr(self._provider, "address_for", None)
        if doc_id and callable(fn):
            try:
                found = str(fn(doc_id) or "").strip()
            except Exception:  # noqa: BLE001 - an identity lookup must not fail a read
                found = ""
            if found:
                return found
        return str(self._turn_address() or "").strip()

    async def _summoned_open(self, doc_id: str) -> list[str]:
        """Comments that will produce an unattended turn and have not had one yet.

        The mutex below refuses a chat write while such a comment is outstanding, so
        this list has to mean exactly "work the watcher is about to do". It therefore
        asks the trigger gate itself rather than re-deriving the rule: the two answered
        different questions once -- the gate summoned on a mention, this counted
        assignments -- and a mutex keyed on a signal the gate no longer acts on blocks
        chat forever on a turn that will never come. That deadlock has been built here
        once already, when the watch was off; it is the same shape.

        Handled means this agent has posted in the thread, not that a person has
        resolved the comment: resolving is theirs, and a handled comment can stay open
        for weeks. Counting those blocked every chat write on the document for as long
        as the comment lived (measured 2026-09-03: two chat batches refused, the agent
        then "completing" the task by replying into the thread unasked).
        """
        me = self._address_for(doc_id)
        if not me:
            return []
        try:
            cs = await self._provider.list_comments(doc_id, include_resolved=False)
        except ProviderError:
            return []
        return [c.comment_id for c in cs
                if summons(c, me) and not c.resolved
                and not any(getattr(r, "author_is_self", False) for r in (c.replies or ()))]

    # ------------------------------------------------------------ registration

    async def list_documents(self) -> dict:
        """The documents this agent watches, so chat can name one without being told.

        Deliberately **not** in ``UNATTENDED_ALLOWLIST``: a comment-triggered turn is
        scoped to the document that triggered it, and handing it the full list would
        widen what it can see past the thing it was asked about.

        Titles are a convenience -- they cost one Drive call and the list is still
        useful without them, so a provider failure degrades to ids rather than an error.
        """
        ids: list[str] = []
        for raw in self._watched_docs() or []:
            try:
                ids.append(str(self._provider.parse_doc_ref(str(raw))))
            except ProviderError:
                continue
        if not ids:
            return _ok(documents=[], detail="当前连接没有纳管任何文档。")

        titles: dict[str, str] = {}
        # **The format, alongside the title.** ``list_accessible_documents`` has carried
        # it since PR4 and this dropped it, so every entry looked alike -- and a request
        # like "look at the slide deck I just added" had nothing to match on but a
        # guess at the title.
        #
        # Observed: asked to handle a comment on a newly added deck, the agent listed
        # the documents, found six indistinguishable rows, and stopped there.
        kinds: dict[str, str] = {}
        try:
            # The managed ids ride along: Feishu derives the wiki space to walk from
            # them, and without the seed a cold process listed nothing on that platform.
            for summary in await list_accessible(self._provider, ids):
                titles[summary.doc_id] = summary.title
                if summary.kind:
                    kinds[summary.doc_id] = summary.kind
        except ProviderError as exc:
            logger.warning("[clouddoc] 取文档标题失败（%s），只返回 id", exc.kind)

        # A listing does not reach every document: a Feishu app cannot enumerate what
        # is shared with it, so its adopted documents came back as bare tokens with no
        # title and no format. The model then read each one to learn what it was --
        # and the ambiguity rail, rightly, refused reads of documents nobody had named.
        # One metadata call per unlisted document fills the gap.
        for i in ids:
            if not titles.get(i):
                try:
                    titles[i] = await self._provider.title(i)
                except Exception:  # noqa: BLE001 - a title is decoration
                    pass
            if not kinds.get(i):
                try:
                    kinds[i] = str(await self._provider.doc_kind(i) or "")
                except Exception:  # noqa: BLE001 - unknown stays unknown
                    pass

        # **Mark the one the user has already named.** The ambiguity rail keeps the whole
        # session's user text, so an answer given several turns ago still authorizes that
        # document -- but the model does not carry the choice forward. Observed: the user
        # confirmed one document, and on the next turn the model went back to the other
        # one, was refused, and had to ask again. Correcting it after the fact still costs
        # a wrong guess every turn; this puts the fact in front of it *before* it picks.
        said = _normalize_for_match(self._user_text())
        docs = []
        confirmed: list[str] = []
        for i in ids:
            title = titles.get(i, "")
            # Same normalization on both sides -- ``said`` has its hyphens stripped, so
            # a raw Google id (most carry ``-`` or ``_``) could never be found in it.
            named = _normalize_for_match(i) in said or (
                bool(title) and _normalize_for_match(title) in said
            )
            entry = {"doc_id": i, "title": title, "user_named": named}
            if kinds.get(i):
                entry["kind"] = kinds[i]
            # Which platform holds it: the model was guessing from the id's shape
            # (measured), and the shape is not a promise.
            owner = getattr(self._provider, "owner", None)
            try:
                host = owner(i) if owner else self._provider
                if getattr(host, "kind", ""):
                    entry["platform"] = str(host.kind)
            except Exception:  # noqa: BLE001 - a listing must not die on routing
                pass
            docs.append(entry)
            if named:
                confirmed.append(title or i)

        notes = []
        if len(confirmed) == 1 and len(ids) > 1:
            notes.append(
                f"用户在本次会话里已指明「{confirmed[0]}」。"
                "除非这次的话里明确换了另一篇，就用它，不要重新挑。"
            )
        # Say it plainly when the list is partial, so "only one" cannot be mistaken for
        # "only one anywhere" -- see _connection_count.
        if self._connection_count() > 1:
            notes.append(
                "注意：本列表只覆盖当前连接的文档，其他连接下的文档不在其中。"
                "若用户指的可能是别的文档，先问清楚。"
            )
        return _ok(documents=docs, detail="".join(notes))

    async def create_document(
        self,
        title: str | None = None,
        share_with: list[str] | None = None,
        platform: str | None = None,
    ) -> dict:
        """Create a document and share it, as one operation.

        ``platform`` names where the document is born ("google" or "feishu") when the
        deployment has connections on more than one; the person's words decide, and
        without a choice the first connection creates it, as before.

        **Sharing is part of creation, not an optional extra.** The document is born in
        the service account's Drive, where no person can see it; created without a
        share, the model would report a URL that 404s for the very person who asked.
        That is why an empty ``share_with`` is refused rather than defaulted.

        The addresses come from the user's message. That keeps the rule the
        authorization scope lives by -- the model cannot know an address the user never
        typed, so it cannot exfiltrate a document to one.

        Chat path only. An unattended turn is bound to one existing document; creating
        new ones from a comment trigger would let a runaway loop mint documents nobody
        asked for.
        """
        if self._turn_doc_id() is not None:
            return _fail("无人值守回合不允许创建文档。")
        # D16 floor: this tool is ``grant`` class -- it hands out access. Full Access
        # has no confirmation channel (the permission rail is never built there), so
        # "always ask" realizes as a refusal that names the way back. No mode, no
        # configuration, no future set operation may lift this.
        if not self._ask_channel():
            return _fail(
                "创建并共享文档会授予新的访问权限（授权类动作），而当前会话是 Full Access"
                "——没有确认通道，按底线拒绝。请把会话权限切回逐项确认（default）后重试。"
            )
        if not title or not title.strip():
            return _fail("缺少 title。")
        emails = [e.strip() for e in (share_with or []) if e and e.strip()]
        if not emails:
            return _fail(
                "缺少 share_with：新文档存放在服务账号名下，不共享就没有人能打开。"
                "请让用户给出要共享的地址（Google 为邮箱；飞书为 open_id 或邮箱）。"
            )
        # Where the document is born. A routed provider spans several platforms and
        # creates on the first unless told otherwise; the person's words decide, and a
        # platform this deployment lacks is refused rather than silently redirected.
        target = self._provider
        want = (platform or "").strip().lower()
        if want:
            pick = getattr(self._provider, "for_platform", None)
            if pick is not None:
                target = pick(want)
            elif str(getattr(self._provider, "kind", "")).lower() != want:
                target = None
            if target is None:
                return _fail(f"当前部署没有「{want}」平台的连接，无法在该平台建档。")
        # Receipts are recorded where the ledger is wired (the hosts wire it; the
        # attended chat path tolerates its absence as batch_edit does, since a person
        # confirms each act). Without one there is nothing for the panel to revert.
        sink = getattr(target, "receipt_sink", None)
        try:
            doc_id = await target.create_document(title.strip())
            # The creator knows the new document's content -- it is empty -- so the
            # read-before-write ledger admits it at birth; demanding a read of a
            # document this call just made would be ritual, not information.
            self._read_docs.add(str(doc_id))
            learn = getattr(self._provider, "learn", None)
            if learn is not None:
                learn(doc_id, target)
        except ProviderError as exc:
            if "quota" in str(exc).lower():
                # Not a quota problem at all: consumer Google gives service accounts
                # **zero** Drive storage, so an SA cannot own files, and Drive reports
                # that as "storage quota exceeded". Relaying that message would send
                # the user off to empty a trash folder that was never full.
                return _fail(
                    "创建失败：此部署的服务账号没有 Drive 存储空间"
                    "（个人 Google 账号下服务账号不能名下持有文件）。"
                    "请让用户自己新建文档并共享给本账号。"
                )
            return _fail(f"创建失败（{exc.kind}）：{exc}")
        # The creation's receipt is written after the platform returns the id, since
        # the id is what the ledger is keyed by; it is committed at once because the
        # act is already done. (A crash between the create and this line leaves a
        # document with no receipt in the account's Drive, which discovery lists.)
        create_rid = _lifecycle_receipt(
            sink, str(doc_id), "create", {"title": title.strip()},
        )
        shared: list[str] = []
        failed: list[str] = []
        share_rids: list[str] = []
        for email in emails:
            rid = _lifecycle_receipt(
                sink, str(doc_id), "share", {"email": email, "role": "writer"},
                commit=False,
            )
            try:
                await target.share_document(doc_id, email)
                shared.append(email)
                if rid:
                    sink.commit(rid, revision_after=None)
                    share_rids.append(rid)
            except ProviderError as exc:
                failed.append(f"{email}（{exc.kind}）")
                if rid:
                    sink.abort(rid, reason=f"{exc.kind}: {exc}")
        # From the provider, not a template: this link is handed to a person, and the
        # template was Google-document-shaped whatever the platform or the format.
        url = target.doc_url(doc_id, "document")
        # Adoption is the panel's, and it only happens by itself where discovery can
        # see the new document. Google lists the account's whole Drive. Feishu
        # discovery walks the wiki spaces the managed documents live in, and a
        # document the app just created is born in the app's own space, outside any
        # of them (measured: the panel refreshed to the same rows after a creation),
        # so the honest instruction there is to paste the link.
        auto = getattr(target, "kind", "") == "google"
        notes = [
            "文档已创建。把链接给用户；"
            + ("在 Docs 面板刷新后它会被自动纳管。" if auto
               else "请用户把链接粘贴到 Docs 面板完成纳管——新建文档在应用自己的空间里，"
                    "面板的自动发现只覆盖应用所在的知识库空间。"),
        ]
        if failed:
            # A partial share must be loud: the document exists, but someone the user
            # named cannot open it, and only the user can decide what to do about that.
            notes.append("以下地址共享失败，需要告知用户：" + "、".join(failed))
        return _ok(
            doc_id=doc_id, url=url, shared_with=shared, detail="".join(notes),
            receipt_id=create_rid, share_receipt_ids=share_rids,
        )

    def _lifecycle_gate(self, what: str) -> dict | None:
        """The two refusals every lifecycle act shares: no unattended turn may perform
        one, and one with no confirmation channel is refused rather than performed
        silently (the always-ask floor realized where asking is impossible)."""
        if self._turn_doc_id() is not None:
            return _fail(f"无人值守回合不允许{what}。")
        if not self._ask_channel():
            return _fail(
                f"{what}会改变他人对文档的访问（底线类动作），而当前会话是 Full Access"
                "——没有确认通道，按底线拒绝。请把会话权限切回逐项确认（default）后重试。"
            )
        return None

    async def share_document(
        self, doc_id: str | None = None, share_with: list[str] | None = None,
    ) -> dict:
        """Grant addresses access to a registered document, one receipt per address.

        The addresses come from the user's message, as for creation: the model cannot
        know an address the user never typed. Each share is its own receipt so the
        panel can undo one address without touching the others.
        """
        refused = self._lifecycle_gate("共享文档")
        if refused:
            return refused
        doc_id, err = await self._resolve(doc_id)
        if err:
            return err
        emails = [e.strip() for e in (share_with or []) if e and e.strip()]
        if not emails:
            return _fail("缺少 share_with：请让用户给出要共享的地址（Google 为邮箱；飞书为 open_id 或邮箱）。")
        sink = getattr(self._provider, "receipt_sink", None)
        shared: list[str] = []
        failed: list[str] = []
        rids: list[str] = []
        for email in emails:
            rid = _lifecycle_receipt(
                sink, str(doc_id), "share", {"email": email, "role": "writer"}, commit=False,
            )
            try:
                await self._provider.share_document(doc_id, email)
                shared.append(email)
                if rid:
                    sink.commit(rid, revision_after=None)
                    rids.append(rid)
            except ProviderError as exc:
                failed.append(f"{email}（{exc.kind}）")
                if rid:
                    sink.abort(rid, reason=f"{exc.kind}: {exc}")
        notes = []
        if shared:
            notes.append("已共享给：" + "、".join(shared) + "。")
        if failed:
            notes.append("以下地址共享失败，需要告知用户：" + "、".join(failed))
        if not shared:
            return _fail("".join(notes) or "没有地址共享成功。")
        return _ok(doc_id=doc_id, shared_with=shared, receipt_ids=rids, detail="".join(notes))

    async def trash_document(self, doc_id: str | None = None) -> dict:
        """Move a registered document to the platform's trash, with a receipt.

        Bringing it back is the platform's recycle bin; the receipt records the act.
        """
        refused = self._lifecycle_gate("移入回收站")
        if refused:
            return refused
        doc_id, err = await self._resolve(doc_id)
        if err:
            return err
        sink = getattr(self._provider, "receipt_sink", None)
        rid = _lifecycle_receipt(sink, str(doc_id), "trash", {}, commit=False)
        try:
            await self._provider.trash_document(doc_id)
        except ProviderError as exc:
            if rid:
                sink.abort(rid, reason=f"{exc.kind}: {exc}")
            return _fail(f"移入回收站失败（{exc.kind}）：{exc}")
        if rid:
            sink.commit(rid, revision_after=None)
        return _ok(
            doc_id=doc_id, receipt_id=rid,
            detail="已移入回收站。如需恢复，请让用户在平台的回收站里操作（Google 云端硬盘 / 飞书回收站）。",
        )

    async def _progress(self, key: str) -> None:
        """Say what this turn is doing, over the watcher's placeholder.

        Narration, and it fails silently on purpose. An unattended turn is three to
        four sequential model calls -- 45 to 52 seconds measured against a live
        document -- and the thread used to show one unchanging "Working on it…" for
        the whole of it, which a reader cannot tell from a hang.

        Three properties keep it from becoming a liability:

        * **No model round trip.** The steps already happen; this reports them from
          inside the tools that perform them. A tool the model had to call to report
          progress would cost a whole extra call -- 6 to 16 seconds each, measured --
          and make the turn it describes slower than the silence it replaced.
        * **Never a write to the document.** It rewrites one reply the watcher
          already posted in the thread it was summoned into, so it adds nothing to
          the document and nothing new to the thread.
        * **Never fatal.** Any failure is swallowed: a note about the work must not
          be able to cost the work. Unattended only -- outside a watch turn there is
          no placeholder and nothing is said.
        """
        got = self._turn_progress() or {}
        rid = got.get("reply_id")
        doc_id = self._turn_doc_id()
        cid = self._turn_comment_id()
        if not (rid and doc_id and cid):
            return
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import tool_msg

            # The watcher chose the language from the comment that summoned the turn,
            # which is the only place that text is in hand; "一" is a sample that
            # reads as Chinese, "" as English.
            sample = "一" if got.get("lang") == "zh" else ""
            await self._provider.update_reply(doc_id, cid, rid, tool_msg(key, sample))
        except Exception:  # noqa: BLE001 - narration must never break the turn
            logger.debug("[clouddoc] 进度提示写入失败，忽略", exc_info=True)

    async def apply_for_comment(self, edits: list[dict] | None = None,
                                doc_id: str | None = None,
                                comment_id: str | None = None,
                                regions: list[dict] | None = None,
                                scope: str = "exact") -> dict:
        """The apply_scoped watch's direct-apply primitive (PR2c, D1).

        Unattended-only: on the chat path batch_edit already covers bounded writes
        behind an ask, so this tool refuses there -- one write path per surface.
        Both ids are **overridden** by the turn's bindings (C4); the model's values
        never matter. Every rail here is **fail-closed** (IC-2): a rail that cannot
        run is an authorization that cannot be verified, and the write is refused --
        this is the opposite of the chat path's usability prechecks.

        Two forms, one authorization principle -- the person's selection is the bound:

        * ``edits`` -- text replacements, bounded by the comment's quoted text through
          the range rail. The form for a comment left on selected text.
        * ``regions`` -- ``[{"at": ..., "values": [[...]]}]``, bounded by the shapes
          the comment is anchored to. The form for a comment left on a shape as a
          whole: the platform records which box was clicked, no quote exists, and the
          clicked shape **is** the selection. Every ``at`` must be one of the bound
          comment's anchored regions; anything else refuses the whole batch.
        """
        bound_doc = self._turn_doc_id()
        bound_cid = self._turn_comment_id()
        if bound_doc is None:
            return _fail("本工具是无人值守直改原语；聊天路径请用 clouddoc_batch_edit。")
        if not bound_doc or not bound_cid:
            return _fail("授权缺失：本回合没有绑定的文档或评论。")
        doc_id, comment_id = bound_doc, bound_cid
        await self._progress("progress_applying")

        if regions and edits:
            return _fail("edits 与 regions 只能用其一：一条批注授权一种边界。")

        region_pairs: list[tuple[str, list[list[str]]]] = []
        for i, item in enumerate(regions or []):
            if not isinstance(item, dict):
                return _fail(f"regions[{i}] 必须是对象。")
            a, v = item.get("at"), item.get("values")
            if not a or not str(a).strip():
                return _fail(f"regions[{i}].at 不能为空。")
            if not isinstance(v, list) or not all(isinstance(r, list) for r in v):
                return _fail(f"regions[{i}].values 必须是二维数组。")
            region_pairs.append(
                (str(a).strip(), [[("" if x is None else str(x)) for x in r] for r in v])
            )

        pairs: list[tuple[str, str]] = []
        for i, e in enumerate(edits or []):
            old_s = str((e or {}).get("old_string") or "")
            new_s = str((e or {}).get("new_string") or "")
            if not old_s:
                return _fail(f"edits[{i}].old_string 不能为空。")
            pairs.append((old_s, new_s))
        if not pairs and not region_pairs:
            return _fail("edits 不能为空。")

        # IC-2: no receipt sink, no write.
        #
        # Two comments elsewhere in the tree state that this path "re-checks and refuses
        # without one", and the check did not exist. A sink is built with a try/except
        # at wiring time, so a failure there left ``receipt_sink`` as None and the write
        # went ahead **untracked** -- which is precisely what IC-3's write-ahead
        # discipline exists to prevent, on the one path that runs with nobody watching.
        #
        # Fail-closed here, unlike the chat path: an attended write is asked about
        # first, so a lost audit record is recoverable by asking the person. Nobody is
        # there to ask on this one.
        if getattr(self._provider, "receipt_sink", None) is None:
            return _fail(
                "回执记录不可用，无人值守直改被拒绝：无法留下这次修改的凭据。"
                "请检查工作区 config 目录是否可写。"
            )

        # IC-3: the pre-write checkpoint reads the live grant state -- revocation,
        # suspension, expiry and a tier change intercept an in-flight write here. The
        # check goes through the injected protocol when one was supplied; the default
        # reaches the gateway's cross-process registry with the mode this turn was
        # dispatched under, and either way an unverifiable state refuses.
        try:
            checker = self._grant_checker
            if checker is None:
                try:
                    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

                    turn_mode = self._turn_mode()
                    registry = WatchRegistry()
                    checker = lambda d: registry.is_write_live(d, mode=turn_mode)  # noqa: E731
                except ImportError:
                    # Standalone deployment: the grant surface is the connection
                    # config itself -- a document is write-live iff it is listed.
                    watched = {str(d) for d in (self._watched_docs() or [])}
                    checker = lambda d: d in watched  # noqa: E731
            if not checker(doc_id):
                return _fail("授权已撤销、已到期或档位不符，本轮写入被拦截。")
        except Exception as exc:  # noqa: BLE001 - fail-closed: unverifiable = refused
            return _fail(f"无法核验授权状态（{exc}），写入被拒绝。")

        # IC-2: the range rail, fail-closed.
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import check_range
        except Exception as exc:  # noqa: BLE001
            return _fail(f"范围轨不可用（{exc}），写入被拒绝。")
        try:
            snap = await self._provider.read(doc_id)
            comments = await self._provider.list_comments(doc_id, include_resolved=True)
        except ProviderError as exc:
            return _fail(f"无法读取文档以校验范围（{exc.kind}）：{exc}")
        bound = next((c for c in comments if c.comment_id == comment_id), None)
        quoted = (bound.quoted_text or "") if bound else ""
        anchored = tuple(getattr(bound, "anchor_regions", ()) or ()) if bound else ()
        if not anchored and quoted and snap.kind == "presentation":
            # Feishu's comment payload carries the quoted text but no anchor field.
            # The shape the person pointed at is still recoverable: the quote anchors
            # uniquely in the flattened deck, and the segment containing it IS that
            # shape -- the same authorization criterion as D18's decoded anchor, on a
            # different carrier. Ambiguity fails closed (anchor_quote returns None on
            # zero or many matches), exactly like the text path's own anchoring.
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.range_rail import (
                anchor_quote,
            )

            hit = anchor_quote(snap, quoted)
            if hit is not None:
                seg = next(
                    (g for g in snap.segments
                     if g.char_start <= hit[0] < g.char_end),
                    None,
                )
                if seg is not None and seg.address:
                    anchored = (seg.address,)

        if region_pairs:
            # The region form's bound is the anchor: the shapes the person clicked
            # when leaving the comment. Same principle as the quote, different
            # carrier -- and fail-closed the same way: no anchor, no region write.
            if not anchored:
                return _fail(
                    "绑定评论没有锚定任何形状，区域形式不可用。"
                    "请用 edits（old_string/new_string），或请协作者点中目标文本框重新评论。"
                )
            outside = [a for a, _ in region_pairs if a not in anchored]
            if outside:
                return _fail(
                    f"这些区域不在批注锚定的形状内：{', '.join(outside[:5])}；"
                    f"本条批注授权的区域是 {', '.join(anchored)}。**整批未做任何修改。**"
                )
        else:
            if not quoted:
                if anchored:
                    return _fail(
                        "绑定评论没有引用正文，但锚定了形状区域："
                        f"{', '.join(anchored)}。请改用 regions 形式"
                        "（[{\"at\": 区域地址, \"values\": [[\"内容\"]]}]）在锚定形状内直改。"
                    )
                if snap.kind == "spreadsheet":
                    return _fail(_no_quote_detail(snap.kind))
                return _fail("绑定评论没有引用任何正文（锚点已失效），无法有界直改。")
            cfg = await self._rail_cfg(doc_id)
            # D23: the model declares a structural scope from the closed menu; the
            # rail computes the geometry. An unknown value resolves to exact there.
            check = check_range(snap, quoted, pairs, cfg, scope=str(scope or "exact"))
            if not check.ok:
                return _fail(f"修改超出评论引用范围：{check.detail}。**整批未做任何修改。**")
            approved_window = (check.window_lo, check.window_hi)

        # Receipt metadata. Every edit here serves the one bound comment; the map is
        # many-to-many because the chat path can merge several comments on one passage
        # into a single edit, and a revert has to reach every thread it answered.
        # D19 tier 3a: the predicate rail. An explicit instruction implies a
        # direction, and the data to check it is already in hand -- both halves of
        # every text pair, the new half of every region. Same posture as the range
        # rail: the range rail bounds where, this bounds which direction; a definite
        # violation refuses the whole batch before anything is sent.
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.result_predicates import (
            check_result_predicates,
        )

        # On a follow-up turn the request is the mentioning reply, not the comment
        # the thread began with: "delete this" answered, then "actually make it
        # twice as long" -- checking the second against the first refuses the very
        # edit that was asked for.
        instruction = _turn_instruction(bound, self._turn_address()) if bound else ""
        rp_pairs = (
            [(o, n) for o, n in pairs]
            if pairs
            else [(None, "\n".join("\t".join(row) for row in content))
                  for _, content in region_pairs]
        )
        violated = check_result_predicates(instruction, rp_pairs)
        if violated:
            return _fail(f"{violated}。**整批未做任何修改。**")

        declared_scope = str(scope or "exact")
        if declared_scope not in ("exact", "sentence", "line", "paragraph"):
            declared_scope = "exact"
        self._provider.receipt_meta = {
            # The declared scope rides in the source so the audit view can see a
            # structurally widened grant, not only the thread reader (review note).
            "source": (
                "apply_for_comment" if declared_scope == "exact"
                else f"apply_for_comment:scope={declared_scope}"
            ),
            # Ring ⑤ asks that a performance be replayable to what commissioned it.
            # `source` names the write path; this names the turn, so a receipt points
            # back at the comment that triggered it rather than only at the code that
            # carried it out.
            "executor": f"comment:{comment_id}",
            "for_comment_ids_by_old": {old_s: [comment_id] for old_s, _ in pairs},
            # The region form's old is computed by the provider, so keying by it is
            # impossible here; the blanket attributes every edit to this comment.
            "for_comment_ids": [comment_id] if region_pairs else [],
        }
        try:
            if region_pairs:
                result = await self._provider.write_regions(
                    doc_id, region_pairs,
                    required_revision_id=snap.revision_id or "",
                )
            else:
                result = await self._provider.edit_batch(
                    doc_id, pairs,
                    required_revision_id=snap.revision_id,
                    # The window the rail approved: the applying layer must judge
                    # uniqueness by the same rule, or a rail-passed edit is refused
                    # at apply time as "not unique in the body".
                    window=approved_window,
                    highlight=True,  # mandatory: visibility is what acceptance reads
                )
        except ProviderError as exc:
            return _fail(f"写入失败（{exc.kind}）：{exc}")
        finally:
            self._provider.receipt_meta = None
        if result.status == "conflict":
            return _fail("正文在校验后被他人修改（revision 冲突），本轮未写入；重新触发即可。")
        if result.status != "applied":
            return _fail(f"未能应用：{result.detail}")

        # D1d: the receipt reply names revertability -- acceptance's rejection branch
        # must be discoverable where the change is seen.
        #
        # And it names **what the reader will actually see**. The text used to say the
        # change was highlighted in yellow whatever the platform did, so on any surface
        # without a highlighting primitive -- Feishu, and every format past a plain
        # document -- the person was sent to look for a marker that was not there.
        #
        # Where the platform cannot paint, the reply carries the changes themselves.
        # That is what ring ⑥ was asking for: a colour was one way to let the reader
        # see what moved, not the requirement.
        #
        # Worded in the commenter's language, like every other line the system writes
        # into a thread. The comment body is the sample the watcher uses too.
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import (
            looks_chinese,
            tool_msg as msg,
        )

        lang = bound.content or ""
        if result.highlighted:
            body = msg("applied_highlighted", lang)
        elif region_pairs:
            listed = "\n".join(
                f"- 批注所锚定的文本框已写为：「{_ellipsize(_region_preview(content), 80)}」"
                for _, content in region_pairs
            )
            body = (
                "已直接修改。本平台的写入通道不支持高亮，新内容逐条列在下面：\n"
                f"{listed}"
            )
            if snap.kind == "presentation":
                # The deck swap keeps shape styling but flattens run-level styling
                # inside paragraphs; saying so here is the D14 posture -- the reader
                # of the change learns it where the change is seen.
                body += "\n（段内加粗/颜色等文字级样式不保留，形状样式不变。）"
        else:
            listed = "\n".join(_edit_line(lang, old_s, new_s) for old_s, new_s in pairs)
            body = msg("applied_listed", lang, listed=listed)
        # D23 disclosure: a widened window is stated where the change is seen, so the
        # scope the model declared is never a silent fact.
        declared = declared_scope
        if declared in ("sentence", "line", "paragraph") and not region_pairs:
            zh = {"sentence": "整句", "line": "整行", "paragraph": "整段"}[declared]
            body += (
                f"\n授权窗：选区所在{zh}（scope={declared}）。"
                if looks_chinese(lang) else
                f"\nAuthorized window: the enclosing {declared} of the selection (scope={declared})."
            )
        try:
            await self._provider.reply_comment(
                doc_id,
                comment_id,
                body + "\n" + msg("revert_hint", lang),
            )
        except ProviderError:
            logger.warning("[clouddoc] apply_for_comment 回执回帖失败 doc=%s", doc_id)
        return {
            "ok": True,
            "detail": "已应用并高亮。" if result.highlighted else "已应用；本平台不支持高亮，改动已在回帖中逐条列出。",
            "edits": len(pairs) + len(region_pairs),
            "highlighted": bool(result.highlighted),
            "receipt_id": result.receipt_id,
        }

    async def add_page(self, doc_id: str | None = None, title: str | None = None) -> dict:
        """Add a worksheet to a spreadsheet, or a slide to a deck, and answer its address.

        **Chat only, and for the same reason write_region is.** A comment authorizes
        the passage it marks; a page that does not exist yet is outside every passage,
        so no comment can warrant one. It is in the unattended denylist by name.

        This exists because its absence was reaching the platform anyway. Asked for
        "a ledger called 云文档台账", the model wrote to a worksheet of that name, was
        refused because it did not exist, concluded correctly that the toolkit could
        not create one -- and then made it with the platform's own CLI through a shell,
        filling 28 cells with no receipt anywhere (2026-09-10). The guard that now
        refuses that route makes the dead end honest; this makes it not a dead end.
        A capability the machinery lacks and the platform has is a standing invitation
        to leave the machinery.

        The returned ``at`` is the address to write through next -- a worksheet's title
        or a slide's page id -- read from the platform's reply, never echoed from the
        request, because a platform that renames a duplicate would otherwise hand back
        an address pointing at someone else's page.
        """
        if self._turn_doc_id() is not None:
            return _fail(
                "无人值守回合不提供新增页：批注授权的是它圈中的那段内容，"
                "而尚不存在的页不在任何批注范围内。"
            )
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        # D16 demotion, same as batch_edit and write_region: a page creation is a
        # write, and a write with no receipt sink is an irreversible one, which
        # sits under the floor in a session with no confirmation channel.
        if self._mandate and getattr(self._provider, "receipt_sink", None) is None and not self._ask_channel():
            return _fail(
                "回执通道不可用，这次新增页将没有回执记录（降为不可逆写），而当前会话是 "
                "Full Access——没有确认通道，按底线拒绝。请修复工作区 config 目录的"
                "回执存储，或把会话权限切回逐项确认（default）。"
            )
        deny = await self._ambiguous_target(canonical)
        if deny:
            return deny
        want = str(title or "").strip()
        try:
            kind = await self._provider.doc_kind(canonical)
        except ProviderError:
            kind = ""
        if kind == "spreadsheet" and not want:
            return _fail("新增工作表需要 title：工作表名就是之后写入用的区域地址。")
        try:
            added = await self._provider.add_page(canonical, want)
        except ProviderError as exc:
            return _write_refusal(exc)
        address = added.address if isinstance(added, PageAdded) else str(added)
        receipt_id = added.receipt_id if isinstance(added, PageAdded) else None
        if kind == "spreadsheet":
            note = (f"已新增，区域地址前缀是 {address!r}；"
                    f"写入内容请用 clouddoc_write_region，例如 at=\"{address}!A1\"。")
        else:
            # A deck page is addressed by an opaque id and Google's create call takes
            # no title; the title box on the new page is written like any other
            # region, after a read shows its address. Said here so the caller does
            # not report a title it never wrote.
            note = (f"已新增，页面 id 是 {address!r}；"
                    "这一页的标题框和正文框还没有内容（Google 不接受新页标题参数），"
                    "写入前请先 clouddoc_read 取回它的区域地址。")
        return _ok(doc_id=canonical, at=address, kind=kind, receipt_id=receipt_id, note=note)

    async def write_region(
        self,
        doc_id: str | None = None,
        at: str | None = None,
        values: list[list[str]] | None = None,
        regions: list[dict] | None = None,
    ) -> dict:
        """State what one or more addressed regions should read like, rather than what to replace.

        The chat path's structural write (D15). ``batch_edit`` replaces text it can
        locate, and some changes are not replacements: moving a cell's value one column
        left is two changes, one of them where nothing can be located because the
        destination is empty. Saying "A7:B7 should now read 大家好, empty" expresses it
        as one atomic write, and so do swapping, clearing and reordering, without a verb
        for each.

        **Chat only.** On this path the person is here and their instruction is the
        authorisation; the unattended path's authorisation is the region a comment
        anchors to. Measured 2026-09-10 (§18.5, settled): a Google spreadsheet comment
        yields no region at all -- a single cell carries only its value as the quote,
        a range selection carries no quote, and the ``workbook-range`` anchor id
        resolves through no public surface. So a comment-triggered turn keeps the
        replacement primitive and its range rail (one comment, one unique-valued
        cell), and this wider write stays where a person approves each call.
        """
        if self._turn_doc_id() is not None:
            return _fail(
                "无人值守回合不提供区域写入：该路径的授权边界是评论锚定的范围，"
                "而表格评论能否给出区域尚未确定。请用 clouddoc_apply_for_comment。"
            )
        canonical, err = await self._resolve(doc_id)
        if err:
            return err
        if self._mandate and canonical not in self._read_docs:
            return _fail(
                "本会话尚未读过这篇文档。共享文档随时可能被他人修改，"
                "请先 clouddoc_read 拿到当前内容与区域坐标再写入。"
            )
        # D16 demotion, same as batch_edit: no receipt sink means no revert path, and
        # an unrevertible write under a mode with no confirmation channel is refused.
        if self._mandate and getattr(self._provider, "receipt_sink", None) is None and not self._ask_channel():
            return _fail(
                "回执通道不可用，这次区域写入将没有回执记录（降为不可逆写），而当前会话是 "
                "Full Access——没有确认通道，按底线拒绝。请修复工作区 config 目录的"
                "回执存储，或把会话权限切回逐项确认（default）。"
            )
        # Two shapes, one meaning. ``regions`` is the general form; ``at``/``values``
        # is the one-region case spelled out, because a single edit should not have to
        # be wrapped in a list to be said.
        pairs: list[tuple[str, list[list[str]]]] = []
        if regions:
            if not isinstance(regions, list):
                return _fail("regions 必须是数组，每项形如 {\"at\": ..., \"values\": [[...]]}。")
            for i, item in enumerate(regions):
                if not isinstance(item, dict):
                    return _fail(f"regions[{i}] 必须是对象。")
                a, v = item.get("at"), item.get("values")
                if not a or not str(a).strip():
                    return _fail(f"regions[{i}].at 不能为空。")
                if not isinstance(v, list) or not all(isinstance(r, list) for r in v):
                    return _fail(f"regions[{i}].values 必须是二维数组。")
                pairs.append((str(a).strip(), [[("" if x is None else str(x)) for x in r] for r in v]))
        elif at:
            if not isinstance(values, list) or not all(isinstance(r, list) for r in (values or [])):
                return _fail("values 必须是二维数组，如 [[\"大家好\", \"\"]]。")
            pairs.append((str(at).strip(), [[("" if x is None else str(x)) for x in r] for r in values]))
        else:
            return _fail("需要 regions，或者 at 与 values；应为区域地址，如 Sheet1!A7:B7。")

        deny = await self._ambiguous_target(canonical)
        if deny:
            return deny
        # Receipt metadata, same convention as the other two write paths: ``source``
        # names the write path, ``executor`` names the turn. This tool refuses
        # unattended turns above, so the commissioning party is always the person in
        # the chat -- the same ``chat`` that batch_edit records.
        self._provider.receipt_meta = {
            "source": "write_region",
            "executor": self._executor_label,
            "for_comment_ids_by_old": {},
        }
        # **Speaker notes are not slide content.** A deck's notes flatten as a run with
        # the same address shape as a text box, so a page holding no box read as a page
        # with one writable region, and "page one" was written into the notes while the
        # canvas stayed blank (observed 2026-09-10). A write to a notes run goes ahead
        # only when the person's own words asked for notes; otherwise the refusal names
        # the way to a real page. Decks only -- the other formats have no such run.
        # Fail-open on purpose: the guard is a decoration on top of the write, and a
        # provider that cannot answer ``doc_kind`` or ``read`` (a stand-in, a wrapper
        # without them) must not turn a valid write into an error. Losing the guard
        # costs one misplaced note; losing the write costs the person's work.
        kind_for_guard = ""
        try:
            fn = getattr(self._provider, "doc_kind", None)
            kind_for_guard = (await fn(canonical)) if fn is not None else ""
        except Exception:  # noqa: BLE001
            kind_for_guard = ""
        if kind_for_guard == "presentation":
            snap_for_guard = None
            try:
                snap_for_guard = await self._provider.read(canonical)
            except Exception:  # noqa: BLE001
                snap_for_guard = None
            if snap_for_guard is not None:
                notes_at = {g.address for g in (snap_for_guard.segments or ())
                            if g.address and getattr(g, "role", "") == "notes"}
                hit = [a for a, _ in pairs if a in notes_at]
                said = _normalize_for_match(self._user_text())
                asked_for_notes = any(k in said for k in ("备注", "演讲者备注", "speakernotes", "notes"))
                if hit and not asked_for_notes:
                    return _fail(
                        "这些地址是**演讲者备注**，不是页面上的文本框：" + "、".join(hit[:5]) +
                        "。用户没有要求写备注，所以没有写入。页面内容要写进文本框；"
                        "这一页若没有文本框，用 clouddoc_add_page 新建一页（自带标题框和正文框）再写。"
                    )
        try:
            result = await self._provider.write_regions(canonical, pairs)
        except ProviderError as exc:
            return _fail(f"区域写入失败（{exc.kind}）：{exc}")
        finally:
            self._provider.receipt_meta = None
        if result.status != "applied":
            return _fail(f"未能写入：{result.detail or result.status}")
        return _ok(
            detail="已把 " + "、".join(a for a, _ in pairs) + " 写成指定内容。",
            regions=[a for a, _ in pairs],
            receipt_id=result.receipt_id,
        )

    async def workmode_get(self) -> dict:
        """Read the deployment working-style file (style only, §4.8 / D4)."""
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode import (
                load_workmode,
                resolve_workmode_path,
            )
        except Exception as exc:  # noqa: BLE001 - style tool: report, never raise
            return {"ok": False, "detail": f"workmode 模块不可用：{exc}"}

        wm = load_workmode(self._workmode_file, prefer_zh=self._workmode_prefer_zh)
        return {
            "ok": True,
            "text": wm.text,
            "source": wm.source,
            "path": str(resolve_workmode_path(self._workmode_file)),
            "detail": "内容来自内置模板（尚未有自定义文件）" if wm.source == "builtin" else "内容来自工作方式文件",
        }

    async def workmode_edit(self, old_string: str = "", new_string: str = "") -> dict:
        """Unique-match edit of the working-style file. Chat path only -- the tool sits
        in UNATTENDED_DENYLIST, so conventions cannot rewrite conventions."""
        try:
            from jiuwenswarm.extensions.co_scribe.backend.toolkit.workmode import edit_workmode
        except Exception as exc:  # noqa: BLE001 - style tool: report, never raise
            return {"ok": False, "detail": f"workmode 模块不可用：{exc}"}

        out = edit_workmode(
            self._workmode_file,
            old_string,
            new_string or "",
            prefer_zh=self._workmode_prefer_zh,
        )
        if out.get("ok"):
            # The working-style file is deployment configuration, not a shared
            # document: it has no ledger entry and no panel row. Said here so the
            # model does not invent a receipt number (measured: "E2E-20260904-001").
            out["detail"] = str(out.get("detail") or "") + (
                " 工作方式文件没有回执编号，也不在修改履历里；"
                "要撤回就再改一次把原文换回来。"
            )
        return out

    def _adopted_titles_line(self) -> str:
        """The registered titles, embedded in the read card at registration time.

        Mechanical routing evidence: the model keeps hunting the local disk for a
        file-looking title unless the title itself is visible as a cloud document.
        Capped so a large deployment does not bloat the card."""
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.kinds import adopted_titles

        titles = adopted_titles(self._watched_docs() or [])
        if not titles:
            return ""
        shown = "、".join(f"《{x}》" for x in titles[:12])
        more = f" 等 {len(titles)} 篇" if len(titles) > 12 else ""
        return f"当前已纳管：{shown}{more}——这些名字都是云文档，直接用本工具族。"

    def get_tools(self) -> list[Tool]:
        def make(
            name: str,
            description: str,
            input_params: dict,
            func: Callable[..., Any],
            *,
            parallel_safe: bool = True,
        ) -> Tool:
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
                parallel_safe=parallel_safe,
                properties={
                    "resilience": {"timeout_s": _TOOL_TIMEOUT_S},
                    # D16: what kind of thing this tool does, on the axes a
                    # permission mode reasons about. Looked up, never defaulted -- a
                    # tool without a class is a registration error, not a read.
                    "effect_class": EFFECT_CLASSES[name],
                },
            )
            return LocalFunction(card=card, func=func)

        doc_id_param = {
            "type": "string",
            "description": "文档分享链接或文档 id。",
        }

        return [
            make(
                "clouddoc_write_region",
                "按区域写入（表格等有结构的格式）：声明区域应当变成什么内容，而不是替换某段文字。"
                "移动、交换、清空用它。"
                "**跨区域的移动要在一次调用里写完**：源和目的地不在同一个矩形时，"
                "用 regions 一次给出两块，例如把 B7 挪到 A1——"
                "regions=[{'at':'Sheet1!A1','values':[['大家好']]},"
                "{'at':'Sheet1!B7','values':[['']]}]。"
                "分两次调用会让文档中间态出现两份内容。"
                "同一矩形内的重排用 at/values 即可，如 at='Sheet1!A7:B7'、values=[['大家好','']]。"
                "values 形状须与区域一致；仅聊天路径可用；任一区域含公式格会整批拒绝。",
                {"type": "object", "properties": {
                    "doc_id": doc_id_param,
                    "regions": {"type": "array", "description": "多个区域，一次原子写入；跨区域移动用这个",
                                "items": {"type": "object", "properties": {
                                    "at": {"type": "string", "description": "区域地址，A1 记法"},
                                    "values": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                                }, "required": ["at", "values"]}},
                    "at": {"type": "string", "description": "单区域地址（regions 的简写形式）"},
                    "values": {"type": "array", "description": "二维数组，行优先，形状须与区域一致",
                               "items": {"type": "array", "items": {"type": "string"}}},
                }, "required": ["doc_id"]},
                self.write_region,
                parallel_safe=False,
            ),
            make(
                "clouddoc_add_page",
                "给已有表格新增工作表，或给已有幻灯片新增一页。"
                "**表格的 title 就是之后写入用的区域地址前缀**（返回的 at 是平台实际采用的名字，"
                "重名时平台会改名，所以用返回值而不是你传进去的值）。"
                "写入内容仍然用 clouddoc_write_region。"
                "只建「页」，不建文件——新建整个文档用 clouddoc_create_document。"
                "仅聊天路径可用：批注授权的是它圈中的内容，尚不存在的页不在任何批注范围内。"
                "平台不支持时会明确拒绝——**这时如实说明并停下，不要改用 shell 去调平台命令行**，"
                "那条路不留回执，等于这次修改在系统里没有发生过。",
                {"type": "object", "properties": {
                    "doc_id": doc_id_param,
                    "title": {"type": "string",
                              "description": "工作表名（表格必填，且就是区域地址前缀）。"
                                             "幻灯片：飞书必填，作为新页标题（平台会把内容完全相同的"
                                             "新增页请求合并成一页）；Google 的新页没有标题参数，"
                                             "传了也不会写进页面，标题请在新页建好后用 "
                                             "clouddoc_write_region 写入标题框"},
                }, "required": ["doc_id"]},
                self.add_page,
                parallel_safe=False,
            ),
            make(
                "clouddoc_read",
                "读取云文档正文，返回纯文本与 revision。"
                "**用户提到的标题哪怕像文件名（带 .md、.pdf、「幻灯片」「表格」字样）也是云文档**："
                "先用 clouddoc_list_documents 找到它，不要去本地文件系统里搜。"
                + self._adopted_titles_line() +
                "表格与幻灯片另返回 cells：每项 {at, text}，at 是单元格地址（如 "
                "'Sheet1'!B7）或幻灯片元素地址；formula_cells 列出不可写入的公式格。"
                "改表格前先看 cells 确定坐标——纯文本里的制表符与换行不表示行列。",
                {"type": "object", "properties": {"doc_id": doc_id_param},
                 "required": ["doc_id"]},
                self.read,
            ),
            make(
                "clouddoc_list_comments",
                "列出文档评论（默认只列未解决的），含 comment_id、作者、引用文本、回复，"
                "以及 addressed（是否 @ 了你）。"
                "**在批注或其回复里 @ 你，是让你处理的唯一方式**；平台的「指派」字段不读——"
                "飞书没有这个字段，Google 的也不构成授权。没被 @ 的批注不是你的任务。",
                {
                    "type": "object",
                    "properties": {
                        "doc_id": doc_id_param,
                        "include_resolved": {
                            "type": "boolean",
                            "description": "是否包含已解决的评论。",
                            "default": False,
                        },
                    },
                    "required": ["doc_id"],
                },
                self.list_comments,
            ),
            make(
                "clouddoc_reply_comment",
                "在指定评论下回复。用于非提议性的说明与答复。已解决的评论不要回复——"
                "回复会把它重新打开。",
                {
                    "type": "object",
                    "properties": {
                        "doc_id": doc_id_param,
                        "comment_id": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["doc_id", "comment_id", "content"],
                },
                self.reply_comment,
                parallel_safe=False,
            ),
            make(
                "clouddoc_create_document",
                "创建一篇新的云文档并共享给指定地址，返回链接。"
                "**share_with 必填**：新文档存放在服务账号名下，不共享就没有人能打开——"
                "地址必须来自用户亲口给出的（Google 为邮箱；飞书为 open_id 或邮箱），不要猜。"
                "用户说明了平台（飞书/Google）时传 platform。"
                "创建后把链接原样告诉用户，并转达返回的纳管说明。",
                {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "文档标题。"},
                        "share_with": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要共享的地址（editor 权限），来自用户的话。",
                        },
                        "platform": {
                            "type": "string",
                            "enum": ["google", "feishu"],
                            "description": "在哪个平台建档；用户没说则省略。",
                        },
                    },
                    "required": ["title", "share_with"],
                },
                self.create_document,
                parallel_safe=False,
            ),
            make(
                "clouddoc_share_document",
                "把一篇已纳管的云文档共享给指定地址（editor 权限）。"
                "地址必须来自用户亲口给出的（Google 为邮箱；飞书为 open_id 或邮箱），不要猜。"
                "每个地址各留一条回执；收回共享由用户在平台上操作。",
                {
                    "type": "object",
                    "properties": {
                        "doc_id": doc_id_param,
                        "share_with": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要共享的地址，来自用户的话。",
                        },
                    },
                    "required": ["doc_id", "share_with"],
                },
                self.share_document,
                parallel_safe=False,
            ),
            make(
                "clouddoc_trash_document",
                "把一篇已纳管的云文档移入平台回收站。只在用户明确要求关闭/删除这篇文档时用；"
                "恢复由用户在平台的回收站里操作（飞书没有恢复接口）——"
                "结果里会说明，原样转达。",
                {
                    "type": "object",
                    "properties": {"doc_id": doc_id_param},
                    "required": ["doc_id"],
                },
                self.trash_document,
                parallel_safe=False,
            ),
            make(
                "clouddoc_list_documents",
                "列出当前纳管的云文档（doc_id、标题、kind，以及 user_named）。"
                "kind 是格式：document 文档 / spreadsheet 表格 / presentation 幻灯片。"
                "用户说「那个幻灯片」「我加的表格」时，按 kind 认，别猜标题。"
                "用户说「共享的文档」「那篇文档」而没有给链接时，先用它确定是哪一篇。"
                "**user_named=true 表示用户在本次会话里已经指明过这一篇**——"
                "除非用户这次明确换了另一篇，就直接用它，不要重新挑。"
                "只有一篇就直接用。有多篇且都没被指明时，按标题匹配用户的说法；"
                "**若不能唯一确定是哪一篇，必须停下来问**——"
                "不要按顺序、相关性或最近修改时间挑一篇。写错文档没有撤销。"
                "返回的 platform 说明文档在哪个平台。"
                "**撤销某次改动要用平台自己的版本历史**（Google 的「版本记录」/飞书的「历史版本」），"
                "你没有回退工具；用户要撤销时告诉他去那里，并给出文档链接。"
                "**「只允许某人 @ 你」这类权限规则由用户在 Docs 面板设置**，"
                "不要写进记忆或工作方式文件，那里写了也不生效。",
                {"type": "object", "properties": {}},
                self.list_documents,
            ),
            make(
                "clouddoc_batch_edit",
                "直接修改正文：多处改动一次原子提交，全成或全不成。"
                "空文档可用它写入首版内容（old_string 留空）。"
                "**要在某处后面追加内容**，就把该处原文作为 old_string、"
                "new_string 写成「原文 + 新内容」——本工具只有替换，没有插入原语。"
                "**正文是纯文本**：不要写 markdown 标题、列表记号或 **加粗**，"
                "那些符号会原样出现在文档里；章节标题写成单独一行普通文字即可。",
                {
                    "type": "object",
                    "properties": {
                        "doc_id": doc_id_param,
                        "edits": {
                            "type": "array",
                            "description": "多处改动，一次提交。写入空文档时给一条、"
                            "old_string 留空、new_string 为全文。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_string": {"type": "string"},
                                    "new_string": {"type": "string"},
                                    "for_comment_id": {
                                        "type": "string",
                                        "description": "这处修改所服务的评论 id。"
                                        "**应用批注时必须填**：修改会被限制在该评论"
                                        "引用的句子附近，超出即整批拒绝。",
                                    },
                                    "for_comment_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "同一处正文上有多条批注时，"
                                        "用这个字段一次列出它们，一处修改同时回应各条。"
                                        "**这些批注必须引用彼此重叠的正文**：不重叠时"
                                        "整批拒绝，请拆成各自独立的修改。"
                                        "只服务一条批注时用 for_comment_id 即可。",
                                    },
                                    "scope": {
                                        "type": "string",
                                        "enum": ["exact", "sentence", "line", "paragraph"],
                                        "description": "这处修改的授权档位，只在填了 "
                                        "for_comment_id 时有意义，默认 exact。"
                                        "**你不画区间，只从这个菜单里选一档**，"
                                        "范围由代码从批注选区算出。"
                                        "选哪一档看批注者自己的话："
                                        "说「这句」选 sentence，说「这行」选 line，"
                                        "说「这段」「这条」选 paragraph，没这么说就用 exact。"
                                        "**正文自己写了什么不作数**——只看批注者的话。"
                                        "exact 是选区本身，常常不含句末的句号；"
                                        "改写整句时用 sentence，否则会差那一个字符。"
                                        "档位会写进回执，主人事后看得见。"
                                        "多条批注合并的修改只支持 exact。",
                                    },
                                },
                                "required": ["new_string"],
                            },
                        },
                        "highlight": {
                            "type": "boolean",
                            "description": "为 true 时给本次写入的文字加黄色背景，"
                            "让读者一眼看出改了哪里。仅当用户要求标出改动时使用。",
                            "default": False,
                        },
                    },
                    "required": ["doc_id", "edits"],
                },
                self.batch_edit,
                parallel_safe=False,
            ),
            make(
                "clouddoc_apply_for_comment",
                "按绑定评论的授权范围**有界直改**正文：多处修改一次原子提交、"
                "自动留回执。仅无人值守回合可用；doc_id/comment_id 由系统绑定，"
                "留空即可。两种形式二选一：评论引用了文字时用 edits——授权窗＝评论选区"
                "按 scope 档位扩到的结构边界，越界整批拒绝；评论**锚定了形状**"
                "（clouddoc_list_comments 的 anchor_regions 非空、quoted_text 为空）时用 "
                "regions——把锚定形状写成指定内容，地址必须取自 anchor_regions，越界整批拒绝。",
                {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "系统绑定，留空。"},
                        "comment_id": {"type": "string", "description": "系统绑定，留空。"},
                        "edits": {
                            "type": "array",
                            "description": "文本形式：一到多处替换，构成一个原子单元。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_string": {"type": "string"},
                                    "new_string": {"type": "string"},
                                },
                                "required": ["old_string", "new_string"],
                            },
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["exact", "sentence", "line", "paragraph"],
                            "default": "exact",
                            "description": "授权窗档位（D23）。exact＝仅评论选区本身（默认）；"
                            "sentence/line/paragraph＝从选区扩到所在句/行/段的结构边界。"
                            "按**批注者的要求**选择——批注要求改整段就声明 paragraph；"
                            "边界由系统按几何计算，越界整批拒绝，所选档位会在回帖中披露。",
                        },
                        "regions": {
                            "type": "array",
                            "description": "区域形式：每项 {at, values}，at 取自该评论的 "
                            "anchor_regions，values 为二维数组（形状区域即 [[\"整框新内容\"]]）。"
                            "与 edits 互斥。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "at": {"type": "string"},
                                    "values": {
                                        "type": "array",
                                        "items": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                                "required": ["at", "values"],
                            },
                        },
                    },
                },
                self.apply_for_comment,
                parallel_safe=False,
            ),
            make(
                "clouddoc_workmode_get",
                "查看当前的工作方式（部署级风格约定：语气、批注处理习惯、攒批节奏）。"
                "修改前先用它取原文。",
                {"type": "object", "properties": {}},
                self.workmode_get,
            ),
            make(
                "clouddoc_workmode_edit",
                "修改工作方式：old_string 在文件中唯一匹配后替换为 new_string。"
                "只承载风格——权限类语句（能否修改、是否确认、范围）写了也不生效，"
                "那些由代码强制。",
                {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["old_string", "new_string"],
                },
                self.workmode_edit,
                parallel_safe=False,
            ),
        ]


# ---------------------------------------------------------------- edit normalisation

_STRUCTURAL_MD = (
    ("行首的 #", lambda ln: ln.lstrip().startswith("#")),
    ("行首的列表记号", lambda ln: ln.lstrip()[:2] in ("- ", "* ", "+ ")),
)


def _count_structural(text: str) -> dict[str, int]:
    counts = {name: 0 for name, _ in _STRUCTURAL_MD}
    for line in text.split("\n"):
        for name, test in _STRUCTURAL_MD:
            if test(line):
                counts[name] += 1
    counts["成对的 **"] = text.count("**") // 2
    return counts


def _introduces_structural_markup(pairs: list[tuple[str, str]]) -> str | None:
    """Structural markdown only, and only what the edit **adds**.

    Two narrowings, each for its own reason. Structural because a stray asterisk in prose
    is not the problem -- a report mentioning ``*args`` is ordinary -- while ``## Heading``
    and ``- item`` at the start of a line, and paired ``**bold**``, land in the body as
    those characters. And by increase because a document may already contain them: the
    live test document holds ``**Agent Identity and Workspace**``, and an absolute test
    refused any rewrite that merely preserved it.
    """
    for old, new in pairs:
        before, after = _count_structural(old), _count_structural(new)
        for name in after:
            if after[name] > before[name]:
                return name
    return None


def _normalize_edits(
    edits: Any, *, allow_empty_old: bool = False
) -> tuple[list[tuple[str, str]], str | None]:
    if not edits:
        return [], "缺少 edits"
    if not isinstance(edits, list):
        return [], "edits 必须是数组"
    pairs: list[tuple[str, str]] = []
    for i, item in enumerate(edits):
        if not isinstance(item, dict):
            return [], f"edits[{i}] 必须是对象"
        old = item.get("old_string")
        new = item.get("new_string")
        if old is None and allow_empty_old:
            old = ""
        if not isinstance(old, str) or (not old and not allow_empty_old):
            return [], f"edits[{i}].old_string 缺失或为空"
        if not isinstance(new, str):
            return [], f"edits[{i}].new_string 缺失"
        pairs.append((old, new))
    return pairs, None
