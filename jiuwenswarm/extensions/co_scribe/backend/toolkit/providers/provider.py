"""DocProvider: the minimal contract for cloud-document co-editing.

The types here are Co-scribe's conceptual model, the layer where providers agree on
meaning. Each provider is one mapping from these concepts onto a platform's API.

Coordinate system:
    ``DocSnapshot.text`` is **flat plain text**, not markdown. A comment's
    quoted_text and every ``old_string`` live in that coordinate system, measured in
    **Python code points**. The document's own indices are **UTF-16 code units** and
    are discontinuous with respect to character space, because non-text elements
    occupy an index without contributing a character. ``DocSnapshot.segments``
    carries the conversion between the two; callers must not use a character offset
    as a document index.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Literal

from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import normalize

# A normalized platform document id (Drive fileId for Google).
# Deliberately not a dataclass: authorization compares doc_id by value, and a plain
# string keeps that comparison a simple equality test.
DocRef = str

# A reply id. comment_id is always known from context -- state stores
# {comment_id: reply_id} -- so this is not a pair.
ReplyRef = str

# ``refused`` is a declared platform limit hit before anything was sent (a
# cross-slide batch, a multi-region write on a platform whose batch is not
# atomic); ``locate_failed`` is an anchor the writer could not find inside the
# window the rail passed. Both answer through ``.ok`` like the rest, and are listed
# here because this Literal is the vocabulary readers trust -- the two were
# being produced for a while without appearing in it.
EditStatus = Literal[
    "applied", "conflict", "invalid", "unknown", "refused", "locate_failed"
]

# The body's text domain. Markdown markers are noise in the first and content in the
# second; both the range rail and the prompt branch on it.
TextDomain = Literal["plain", "markdown"]

# The channel id for an unattended cloud-document session. This is a
# **cross-process contract**: the watcher on the gateway side stamps it on every
# envelope, and the agentserver side reads it as "nobody is present this turn".
# It is defined in this dependency-free module because both processes need it --
# the gateway cannot import interface_deep, which would pull the whole deep-agent
# stack into the gateway process, and a copied literal would drift silently the
# first time someone changed one side.
CLOUDDOC_CHANNEL_ID = "__clouddoc__"

# The formats co-scribe serves. Markdown files were served too and are not any
# more: the format has no addressed form on either platform, so an unattended turn
# on one had no selection to bound it and acted on the whole file, and a comment on
# a .md carries neither an anchor nor the quoted text that every other format's edit
# window is computed from (measured). Withdrawn rather than guarded.
#
# This tuple is the one place that answers "can co-scribe open this". Discovery
# filters by it, adoption refuses by it, the panel marks a row unsupported by it,
# and the read/write dispatch refuses by it -- so a format cannot be half-served,
# listed by one path and rejected by the next.
SUPPORTED_KINDS = ("document", "spreadsheet", "presentation")

# What the person and the model are both told. It names the format as unsupported
# rather than the document as broken: the file is fine, this tool does not open it.
UNSUPPORTED_KIND_DETAIL = "云文档不支持这个格式，只支持文档、表格、幻灯片。"


def is_supported_kind(kind: str) -> bool:
    """Whether co-scribe serves this format.

    An empty kind is *not* unsupported -- it is unknown, which happens before the
    first read teaches the format, and refusing it here would refuse every document
    adopted from a bare token. The dispatch resolves the kind first and asks after.
    """
    return not kind or kind in SUPPORTED_KINDS


def read_connection_specs(cfg: dict) -> list[dict]:
    """Read the list of connection specs out of the clouddoc config section.

    **It lives in this dependency-free module for the same reason as
    CLOUDDOC_CHANNEL_ID**: the gateway and the agentserver both read the config
    under one schema. Two separate readers already went wrong once -- after the panel
    upgraded a config to the ``connections:`` list, the agentserver was still reading
    the old top-level ``credentials_file``, tool registration was skipped in silence,
    and an unattended turn met the model with zero tools.

    The current shape is the ``connections:`` list. The older single-connection form
    (top-level credentials_file and documents) is still accepted in full and folded
    into a one-element list.
    """
    conns = cfg.get("connections")
    if isinstance(conns, list) and conns:
        return [
            {
                "credentials_file": str(c.get("credentials_file") or ""),
                "documents": list(c.get("documents") or []),
            }
            for c in conns
            if isinstance(c, dict)
        ]
    if cfg.get("credentials_file"):
        return [{
            "credentials_file": str(cfg["credentials_file"]),
            "documents": list(cfg.get("documents") or []),
        }]
    return []


def doc_ref_token(ref: object) -> str:
    """The bare token a configured document entry names, for exact comparison.

    Adopted lists hold the canonical id the panel persisted, but an older config may
    still hold the pasted link. Both platforms put the token in the path segment after
    a known marker (``/d/<id>`` on Google; ``/docx/<token>`` and its siblings on
    Feishu) or in an ``id=`` query. Stripping that here lets a host compare document
    identities without building a provider. A string that is not a link is returned
    as it is; the comparison that follows is equality, never containment.
    """
    s = str(ref or "").strip()
    if not s.startswith(("http://", "https://")):
        return s
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(s)
    segs = [p for p in parts.path.split("/") if p]
    for marker in ("d", "docx", "docs", "wiki", "sheets", "slides", "file"):
        if marker in segs:
            i = segs.index(marker)
            if i + 1 < len(segs):
                return segs[i + 1]
    ids = parse_qs(parts.query).get("id")
    return ids[0] if ids else s


# Hosts a pasted link may come from and still be shown back as the document's own
# address. The link is text the person typed, and the origin it names is trusted only
# when it is the platform's. Anything else keeps the token and takes its address from
# the platform instead -- built from the id on Google, returned by the metadata call
# on Feishu -- never from the paste. Feishu private deployments live on their own
# domains; for those the metadata-returned link is the only trusted one.
TRUSTED_DOC_HOSTS: dict[str, tuple[str, ...]] = {
    "google": ("docs.google.com", "drive.google.com"),
    "feishu": (".feishu.cn", ".larksuite.com", ".larkoffice.com"),
}


def is_trusted_doc_url(url: object, vendor: str) -> bool:
    """Whether ``url`` is an https link on one of ``vendor``'s own hosts."""
    s = str(url or "").strip()
    if not s.lower().startswith("https://"):
        return False
    from urllib.parse import urlsplit

    host = (urlsplit(s).hostname or "").lower()
    if not host:
        return False
    for allowed in TRUSTED_DOC_HOSTS.get(str(vendor or "").lower(), ()):
        if allowed.startswith("."):
            if host == allowed[1:] or host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


@dataclass(frozen=True)
class DocCapabilities:
    """What a provider can do with one document.

    The claim here used to be "every flag has a named reader; there are no reserved
    fields", and it had stopped being true: of seven flags, two were read. The rest
    were measured, declared, asserted in tests, and consumed by nothing -- so a
    provider that got one wrong was corrected by nobody, and reasoning that leaned on
    one ("declare it false and admission tightens") leaned on air.

    Which are load-bearing is now stated per flag, and pinned by a test, so the next
    flag to go inert says so instead of looking like protection.
    """

    # Load-bearing: admission refuses a document that cannot be read.
    can_read: bool
    # Load-bearing: admission refuses comment-only access, which is the configuration
    # mistake that otherwise fails silently.
    can_edit: bool
    # Informational. On both platforms comment rights follow edit rights, so this
    # cannot be false where can_edit is true, and nothing branches on it.
    can_comment: bool
    # Informational **by decision**: invariant ③ forbids the agent resolving a thread a
    # person opened, so whether the platform would permit it does not enter into
    # anything. Kept because a person reading a capability report wants to see it.
    can_resolve: bool
    # Whether the platform can refuse a write that would land on changed content.
    #
    # Load-bearing: admission warns the person when it is false, because on the newer
    # formats it is false **with full edit rights** and permanently -- a spreadsheet has
    # no revision id anywhere in its API. The older inference, that losing edit rights
    # is what takes revisionId away, was true only while every document was a Doc.
    #
    # Never simulated: a provider that compensates with a content re-read still says
    # False (C9), because the compensation has a window and a lock does not.
    has_revision_control: bool
    # Where the platform truncates quoted text (measured at about 418 code points for
    # Google). None means unknown, or no truncation.
    #
    # Load-bearing: the range rail uses it in place of its configured default, because
    # the limit exists to detect a quote the platform cut off -- a fact about the
    # platform rather than a policy. It was measured and then ignored for a while, with
    # the rail comparing against a configured 400 and refusing quotes in the 400-418
    # band that had not been truncated at all.
    max_quote_chars: int | None
    # Whether the platform truncates a quote **without leaving a mark**. Google ends a
    # cut-off quote with an ellipsis, which is detectable; Feishu cuts hard at exactly
    # 128 code points with nothing appended (TENANT-VERIFY 2026-09-02: measured
    # identically for an API block anchor and for a live user selection). Where this
    # is True, a quote whose length reaches max_quote_chars is treated as truncated:
    # a selection of exactly the limit is indistinguishable from a longer one the
    # platform cut, and refusing it is the fail-closed direction -- under D22 the
    # selection *is* the authorization, so a silently shortened quote is a silently
    # shortened grant.
    #
    # Load-bearing via CloudDocToolkit._rail_cfg, which translates it into
    # RangeRailConfig.quote_hard_limit -- a RangeRailConfig built anywhere else does
    # not inherit the guard from this flag alone.
    quote_truncates_unmarked: bool = False
    # Whether several edits can be committed as one indivisible write. Google's
    # batchUpdate is; a platform whose API applies edits one at a time is not, and a
    # multi-edit batch there could half-succeed.
    #
    # A provider that cannot do this says so and the batch is refused with a reason
    # (C9: never pretend a capability exists). It is not worked around by editing and
    # then undoing -- a document is shared, so a partial write is visible to everyone
    # looking at it, and the undo would fight whoever else is typing.
    #
    # Defaults to True so a provider written before this flag existed keeps behaving
    # as it did; opting out is the deliberate act.
    atomic_batch: bool = True


@dataclass(frozen=True)
class AgentIdentity:
    """The agent's own identity on the platform."""

    # A display name. **Never a test of identity**: users can change their own, and it
    # is all Drive returns for a comment's author.
    display_name: str
    # The account address (the service-account email for Google). Used to match an @
    # mention, never to identify a comment's author.
    address: str | None = None


@dataclass(frozen=True)
class Segment:
    """How one run of flat text maps back onto a place the provider can write to.

    ``text[char_start:char_end]`` is the run. Where it lives is said twice, because
    the platforms disagree about what an address is:

    * ``index_start``/``index_end`` -- a document index range, for the platforms that
      number a body linearly. Non-text elements (inline images, footnote references)
      produce no characters but do occupy indices, so indices jump between segments;
      that discontinuity is why a run needs its own entry at all.
    * ``address`` -- an opaque string, for the platforms that do not number anything.
      A cell is ``Sheet1!B7``; a slide run is ``pageId/objectId``. The format is the
      producing provider's own business.

    The rails and the watcher work in character offsets into ``DocSnapshot.text``,
    which is what lets a new document format reuse them whole: a provider only has
    to flatten its document so that a platform-quoted string is a literal
    substring, and be able to turn a character span back into a write. Three
    readers do look at the fields, and each only at the addressed formats: the
    read tool reports ``address`` and ``role`` so a grid or a deck can be written
    to by coordinate, the range rail clamps a spreadsheet or presentation window
    to the anchored run, and the region write refuses a ``notes`` run unless the
    person asked for notes.
    """

    char_start: int
    char_end: int
    index_start: int = 0
    index_end: int = 0
    # Where this run lives, for a platform whose write is not addressed by an index.
    address: str = ""
    # What kind of place the run is, where the format has more than one kind. A
    # deck's speaker notes flatten as a run just like a text box, with the same
    # ``pageId/objectId`` address; without this a page holding no text box read as a
    # page with one writable region, and a model asked for "page one" wrote the whole
    # page into the notes (observed 2026-09-10). "" for everything else.
    role: str = ""
    # Whether writing here would destroy something the flat text does not show. A
    # spreadsheet cell holding ``=SUM(A1:A9)`` reads as ``42``, and a comment quotes
    # the 42; writing the edited 42 back replaces the formula with a literal, and the
    # document looks unchanged afterwards. IC-7: such a run is marked at flattening
    # time and every edit landing in one is refused.
    readonly_reason: str = ""


@dataclass(frozen=True)
class DocSummary:
    """One document the agent's account can reach, as seen from the outside.

    Enough to decide whether to watch it and to show it to a person -- not a snapshot;
    reading the body is a separate, far more expensive call.
    """

    doc_id: DocRef
    title: str
    can_edit: bool
    # Which format, so a person listing what the agent manages can see that the deck
    # and the spreadsheet are in there alongside the documents. Defaults to empty for
    # a provider that only ever produced one kind.
    kind: str = ""


@dataclass(frozen=True)
class DocSnapshot:
    doc_id: DocRef
    kind: str
    revision_id: str | None
    text: str
    segments: tuple[Segment, ...] = ()


@dataclass(frozen=True)
class DocComment:
    comment_id: str
    author_is_self: bool
    author_display_name: str
    created_time: str
    content: str
    quoted_text: str
    resolved: bool
    mentioned_addresses: tuple[str, ...] = ()
    replies: tuple["DocReply", ...] = ()
    # Who the comment is assigned to, if anyone -- Google carries the field, Feishu
    # has none. It is **displayed, never read for authority**: a mention is the one
    # summons on both platforms (see ``summons``), and the field is kept on the
    # record only so a reader can see what the platform shows.
    assignee_address: str | None = None
    # Whether the author is itself a service account -- another agent, not a person.
    # Agents must not trigger agents, and this is how the trigger layer tells them apart
    # without knowing anything about a particular platform's account naming.
    author_is_service_account: bool = False
    # The regions a shape-anchored comment names, in the provider's own address form
    # (``pageId/objectId`` for a deck shape). A person who comments on a text box as a
    # whole -- rather than on selected text -- produces no quote, but the platform
    # records which shape was clicked, and that shape is a natural write boundary:
    # the person's click is the selection. Empty for text-quoted comments and for
    # platforms whose anchor decodes to nothing (a spreadsheet's workbook-range id).
    anchor_regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocReply:
    reply_id: ReplyRef
    author_is_self: bool
    author_display_name: str
    created_time: str
    content: str
    # Replies **do** carry server-computed mentions. An earlier comment here asserted
    # the opposite as a measured fact and was wrong; that error was the whole reason
    # reply addressing fell back to substring matching, with the false positives that
    # brings ("I already asked @co-scribe about this").
    mentioned_addresses: tuple[str, ...] = ()
    author_is_service_account: bool = False


async def list_accessible(provider, known=()) -> list:
    """Discovery, handing the provider the managed documents when it takes them.

    Feishu enumerates a wiki space and finds the space from the nodes already
    managed, so it wants ``known``; Google enumerates outright and ignores it. Passed
    only when the signature accepts it, so a provider -- or a test stand-in -- written
    to the older one-argument contract keeps working unchanged. One helper for every
    host, because the chat listing had dropped the argument that the panel passed.
    """
    import inspect

    fn = provider.list_accessible_documents
    try:
        params = inspect.signature(fn).parameters
        takes = "known" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        takes = False
    if takes:
        return await fn(known=list(known))
    return await fn()


def summons(c: DocComment, sa_address: str) -> bool:
    """Whether a person named this agent in this comment or in a reply under it.

    **The** summons rule, and it lives here rather than in the watcher because two
    layers need the same answer. The watcher asks it to decide whether a turn runs;
    the toolkit asks it to decide whether a chat write must stand down while the
    unattended path still owes that comment a turn. Those two once derived the rule
    separately -- the watcher on the mention, the toolkit on the platform's assignment
    field -- and a mutex keyed on a signal the gate no longer acts on blocks chat
    forever on a turn that will never come.

    Only a person's mention counts. An agent's mention of an agent is the recruitment
    the loop prohibition forbids, at any level of the thread; the caller's comment set
    is already filtered for the top level, and replies are filtered here.

    The platform's assignment field is deliberately not read. It says who a comment is
    addressed to, which is worth showing a reader, and it decides nothing: it was a
    second way to ask for the same thing on the one platform that has it, and Feishu
    has none.
    """
    me = normalize(sa_address)
    if not me:
        return False
    if any(normalize(a) == me for a in c.mentioned_addresses):
        return True
    for r in c.replies:
        if r.author_is_self or r.author_is_service_account:
            continue
        if any(normalize(a) == me for a in r.mentioned_addresses):
            return True
    return False


@dataclass(frozen=True)
class EditResult:
    status: EditStatus
    new_revision_id: str | None = None
    detail: str = ""
    # Whether the platform actually painted the changed text, when highlighting was
    # asked for. **Answered, not assumed**: the receipt used to record what the caller
    # requested, so a platform that could not highlight still produced a receipt saying
    # it had -- and the panel then offered an "unhighlight" button that did nothing.
    #
    # Ring ⑥ needs the reader to see what changed; a background colour is one way to
    # show them, not the only one. A provider that cannot paint says so here and the
    # caller substitutes an honest alternative (spelling the change out in the reply)
    # rather than either lying or refusing.
    highlighted: bool = False
    # The ledger entry this write produced, when a receipt sink recorded one. It is
    # what a person quotes to find the write in the panel's history and revert it, so
    # the tools hand it back verbatim: a model that is not told the id invents one
    # (measured 2026-09-03: a revision id reported as "the receipt number").
    receipt_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True)
class PageAdded:
    """What ``add_page`` answers: the new page's address and the receipt that records
    it. The receipt id travels with the address so the tool can hand it back the way
    every other write does; None means the provider ran without a sink."""

    address: str
    receipt_id: str | None = None


# Failure kinds the platform reports before it mutates anything: the request was
# received, judged and refused. A receipt for such a write is closed as aborted. Every
# other failure -- a timeout, a dropped connection, an answer that could not be read
# -- may have landed, and its receipt stays pending for the sweep to settle as unknown.
PRE_MUTATION_KINDS = frozenset({
    "invalid", "unsupported", "forbidden", "not_found", "rate_limited", "conflict",
    "auth", "ledger_unavailable",
})


class ProviderError(Exception):
    """A classified provider-layer error.

    Backoff and admission both branch on ``kind``:

        not_found       the document or comment is gone -- a per-document signal that
                        may advance the failure count
        forbidden       insufficient permission -- likewise per-document
        rate_limited    throttled, including Drive's 403 userRateLimitExceeded -- a
                        **global** signal that goes to the process-level token bucket
                        and must **never** advance the failure count
        invalid         the request itself was malformed -- a bug in this code, not a
                        conflict
        conflict        a revision conflict
        unknown         submitted, outcome unknown (timeout, connection reset)
        transport       any other transport-layer error
        ledger_unavailable  the receipt ledger is wired but cannot be read or written; the
                        write was refused before the platform call (IC-2)
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class DocProvider(ABC):
    """The minimal co-editing contract for one cloud-document platform.

    Every method is ``async``, but the SDKs underneath are usually synchronous and
    blocking. An implementation must put the blocking call inside
    ``asyncio.to_thread``, or it will stall the gateway's event loop.
    """

    @property
    @abstractmethod
    def kind(self) -> str:
        """The provider's identifier, for example ``"google"``."""

    @property
    def text_domain(self) -> TextDomain:
        """This provider's body text domain.

        It decides whether markdown markers (``**``, backticks and so on) are
        **content** or **noise**:

        * ``"plain"`` -- markers render as nothing and land in the body as literal
          characters. Google Docs rich text falls here once flattened: observed, a
          comment asking for bold produced a proposal of ``**this sentence**``, and
          approving it added four asterisks to the document.
        * ``"markdown"`` -- markers are the correct output and refusing them is the
          error.

        The default is ``"plain"``, so a provider that forgets to declare one fails
        toward refusing an extra proposal rather than writing asterisks into someone's
        document.
        """
        return "plain"

    async def doc_kind(self, doc_ref: DocRef) -> str:
        """This document's format, or "" when the provider cannot say.

        Both real providers answer this, and the watcher's admission check asks it to
        refuse a format co-scribe no longer serves before it dispatches anything. It
        is on the interface rather than reached for on the concrete class so that
        "cannot say" is a declared answer instead of an AttributeError.

        The empty default admits: a provider that does not track formats is not
        thereby claiming its documents are unsupported, and the read path refuses on
        its own. Under-refusing here is recoverable; over-refusing would freeze
        documents nobody could unfreeze.
        """
        return ""

    async def text_domain_for(self, doc_ref: DocRef) -> TextDomain:
        """The body text domain of **one** document.

        A connection reaches documents of several kinds, and they disagree: a Feishu
        docx round-trips markdown and wants its asterisks kept, while the spreadsheet
        beside it wants them refused. Deciding once per provider was right while a
        provider meant one format, and became wrong the moment it meant several
        (§16.12: a new capability makes an old premise false without anything appearing
        to be missing).

        Defaults to the provider-wide answer, so a single-format provider is unaffected
        and needs no override.
        """
        return self.text_domain

    def doc_url(self, doc_ref: DocRef, kind: str = "") -> str:
        """A link a person can open, for this document on this platform.

        Built by the provider because only the provider knows the shape. It was built
        by the caller instead -- ``https://docs.google.com/document/d/{id}/edit``,
        hard-coded in the panel and in the tool that hands a new document's link to the
        user. That is wrong twice over: wrong for every Feishu document, and wrong for
        every Google spreadsheet and deck, both of which live under a different
        path. The tool's own text says "give this link to the user", so a wrong one
        is handed to a person rather than merely logged.

        ``kind`` is the format name when the caller knows it; a provider that needs it
        and is not given it may look it up or fall back.
        """
        return str(doc_ref)

    @abstractmethod
    def parse_doc_ref(self, url_or_id: str) -> DocRef:
        """A share link or bare id to a normalized DocRef. Raises
        ProviderError("invalid", ...) when it cannot be parsed.

        The authorization check compares this method's **output**, never the tool's
        raw argument: pasting a link in chat is ordinary usage, and comparing raw
        arguments would fail to match every time.
        """

    @abstractmethod
    async def self_identity(self) -> AgentIdentity: ...

    @abstractmethod
    async def capabilities(self, doc_ref: DocRef) -> DocCapabilities: ...

    async def title(self, doc_ref: DocRef) -> str:
        """The document title, for display only. Empty by default -- a title is
        decoration, never an input to any decision."""
        return ""

    @abstractmethod
    async def read(self, doc_ref: DocRef) -> DocSnapshot: ...

    @abstractmethod
    async def edit(
        self,
        doc_ref: DocRef,
        old_string: str,
        new_string: str,
        *,
        revision_id: str | None = None,
    ) -> EditResult:
        """A single edit. **Chat path only**; the watcher path uses edit_batch."""

    @abstractmethod
    async def edit_batch(
        self,
        doc_ref: DocRef,
        edits: list[tuple[str, str]],
        *,
        required_revision_id: str,
        window: tuple[int, int] | None = None,
        highlight: bool = False,
    ) -> EditResult:
        """Several edits, **submitted once**.

        ``highlight`` paints the inserted text's background so a reader can see at a
        glance what changed. It rides in the same atomic batch -- the insert indices
        are already in hand at that moment, and a separate styling pass could land on
        a body that moved in between.

        ``window`` is the character range the range rail approved. Given it, this
        locates old_string only inside that range, judging by exactly the rail's
        rule; without it, the fallback is uniqueness across the whole body. The two
        layers must share one rule, or an edit the rail passed as unique within the
        window gets refused at apply time, after the rail already said yes.

        What the platform side must guarantee: every range resolved against the same
        snapshot, applied in **descending** index order -- otherwise an earlier edit
        shifts the offsets of a later one -- and the batch atomic.

        **Never re-read and re-locate on conflict.** That would bypass revision
        protection and range protection together. A conflict always comes back as
        ``EditResult(status="conflict")``.
        """

    async def add_page(self, doc_ref: DocRef, title: str = "") -> "str | PageAdded":
        """Add a worksheet to a spreadsheet, or a slide to a deck; answer its address.

        A provider that records the write answers ``PageAdded`` so the receipt id
        reaches the tool; a bare address is accepted from one that does not.

        The gap this closes was found the hard way. The toolkit could write into an
        address but not bring one into being, so asked for "a ledger called 云文档台账"
        the model wrote to a worksheet that did not exist, was refused, correctly
        concluded the toolkit could not create one -- and reached for the platform's
        own CLI through a shell, which made the worksheet and filled 28 cells with no
        receipt anywhere (observed 2026-09-10). A capability the machinery lacks and
        the platform has is a standing invitation to leave the machinery; the guard
        that now refuses that route only makes the refusal honest, it does not give
        anyone what they were trying to do.

        Returns the created page's **address component**: a worksheet's title, which is
        what precedes ``!A1``; a slide's page id, which is what precedes ``/objectId``.
        Read from the platform's reply rather than echoed, because a platform that
        renames a duplicate would otherwise hand back an address pointing elsewhere.

        Refuses with ``unsupported`` where the format or the platform has no such
        operation. Documents have no pages to add.
        """
        raise ProviderError("unsupported", "该平台不支持新增页/工作表。")

    async def write_regions(
        self,
        doc_ref: DocRef,
        regions: list[tuple[str, list[list[str]]]],
        *,
        required_revision_id: str = "",
    ) -> EditResult:
        """Make one addressed region read exactly like ``content`` (D15).

        The general form of the write. ``edit_batch`` is its special case: a region
        matched by locating ``old_string``, whose content is a single string.

        The reason for it is that some changes are not replacements. A comment asking to
        move a cell's value one column left is two changes, one of them outside the range
        the comment anchored, with a destination too empty to locate anything in -- and
        it cannot be said at all in ``(old_string, new_string)``, whatever the platform
        can do. ``values.batchUpdate`` writes several ranges atomically; the limit was
        the shape of our own primitive.

        Stating a region's whole intended content, rather than adding a verb per
        operation, is what keeps the machinery from multiplying: move, swap, clear,
        reorder and bulk fill are all "this region should now read like this". Reverting
        is writing the previous content back, and the receipt's two ends stay old and
        new.

        **Several regions, committed as one write.** A move whose source and
        destination are not in the same rectangle -- B7 to A1 is seven rows apart -- is
        two regions, and doing them as two calls leaves the document holding the value
        twice in between. Measured live: the agent wrote the destination, read back and
        found the text in both places, and spent six minutes reasoning its way to the
        second call. Nothing but the model's own diligence closed that window; a crash
        or a timeout in it leaves a duplicated value behind.

        ``values.batchUpdate`` writes several ranges atomically, so the window need not
        exist. One region is the ordinary case and simply a list of one.

        Each region is the provider's own address form -- A1 notation for a spreadsheet.
        Content is row-major, and its shape must match the region's, because a mismatch
        is the difference between filling a row and overwriting a column.

        Absent by default and declared rather than simulated (C9): a provider whose
        platform has no addressed write says so, and the caller reports that instead of
        approximating it with replacements.
        """
        raise ProviderError(
            "unsupported",
            "该 provider 不支持按区域写入；结构性改动（移动、交换、清空整块）无法表达。",
        )

    async def read_regions(self, doc_ref: DocRef, regions: list[str]) -> list[str]:
        """Report what each addressed region reads like right now, flattened.

        The read half of ``write_regions``, and it exists for the revert path: a
        region receipt's inverse is "write the old content back", and the anchor
        criterion behind it is "the region still reads exactly like what this receipt
        wrote". Comparing that needs the current content in the same flattened form the
        receipt stores -- rows joined by the grid's row separator, cells by its cell
        separator, a single string for a one-shape region.

        Declared rather than simulated (C9), like its write half: a provider without
        addressed regions has nothing to read by address.
        """
        raise ProviderError(
            "unsupported",
            "该 provider 不支持按区域读取；区域回执在此无法核对锚点。",
        )

    @abstractmethod
    async def list_comments(
        self, doc_ref: DocRef, *, include_resolved: bool = False
    ) -> list[DocComment]:
        """**Must page to the end.** Raise when enumeration cannot complete; never
        truncate in silence."""

    @abstractmethod
    async def reply_comment(
        self, doc_ref: DocRef, comment_id: str, content: str
    ) -> ReplyRef: ...

    @abstractmethod
    async def update_reply(
        self, doc_ref: DocRef, comment_id: str, reply_id: ReplyRef, content: str
    ) -> None: ...

    @abstractmethod
    async def delete_reply(
        self, doc_ref: DocRef, comment_id: str, reply_id: ReplyRef
    ) -> None:
        """Remove a reply the agent itself posted.

        Only ever used to retract the watcher's own placeholder. A turn where the
        model posted its own reply would otherwise leave two replies in the thread:
        the model's answer, and the placeholder overwritten with the model's closing
        narration -- third-person prose about what it was about to do, written into
        somebody's shared document."""

    @abstractmethod
    async def resolve_comment(
        self, doc_ref: DocRef, comment_id: str, content: str | None = None
    ) -> None: ...

    @abstractmethod
    async def create_document(self, title: str) -> DocRef:
        """Create an empty document owned by the service account, returning its id.

        The document lives in the service account's Drive: nobody can see it until
        ``share_document`` grants access, and it dies with the GCP project. Callers
        must treat sharing as part of creation, not an optional extra."""

    @abstractmethod
    async def share_document(
        self, doc_ref: DocRef, email: str, *, role: str = "writer"
    ) -> None:
        """Grant ``email`` access. No notification mail is sent -- the person asking
        for the document is in the conversation already; mail would only add noise
        (and demo 01 explicitly teaches unchecking Notify for the same reason)."""

    async def trash_document(self, doc_ref: DocRef) -> None:
        """Move the document to the platform's trash. The document keeps its id;
        bringing it back is the platform's recycle bin, not this feature's."""
        raise ProviderError("unsupported", "该平台不支持移入回收站。")

    @abstractmethod
    async def list_shared_unsupported(self) -> list[dict]:
        """Files shared with this account that co-editing cannot take: spreadsheets,
        presentations, uploaded Office files. Each row: {"title": ..., "kind": ...}.

        Discovery filters to ``SUPPORTED_KINDS`` on purpose -- a format without an
        addressed form has nothing to bound an edit. But the *silence* was not on
        purpose: the file genuinely was shared, the permission exists, and the panel
        pretended it did not, so nobody could tell "unsupported" apart from "the
        share failed"."""

    @abstractmethod
    async def list_accessible_documents(
        self, known: Iterable[DocRef] = ()
    ) -> list[DocSummary]:
        """Every document this account can reach.

        ``known`` is the documents already managed on this connection. A provider
        that can only enumerate *around* what it already holds -- a wiki space is
        found from the nodes in it -- starts from these rather than from any cache,
        so a cold process answers the same as a warm one. Providers that enumerate
        outright ignore it.

        Sharing a document with the agent is the act that grants it; this reports the
        result, so a person who has just shared one can find it by name instead of
        copying a link. It is **not** a watch list: watching costs polling and turns, so
        what to watch stays a decision, not a consequence of being shared.
        """

    def forget_kind(self, doc_ref: DocRef) -> None:
        """Drop any remembered format for this document, so the next ask is a fresh one.

        Refresh is repair, and a remembered format is one of the things that can need
        repairing: the panel persists what a provider reported and feeds it back on the
        next start, so a format recorded wrongly is fed back for ever and no amount of
        refreshing dislodges it. A provider that cannot be wrong about formats does
        nothing here.
        """
        return None

    @property
    def discovery_available(self) -> bool:
        """Whether ``list_accessible_documents`` can actually enumerate.

        A platform may refuse to tell an application what was shared with it -- that is
        the tenant's policy, not a bug, and the provider learns it by asking once. The
        panel needs the answer because the alternative is a screen that promises "shared
        documents arrive on their own" over a list that will never fill, and an owner who
        concludes the sharing failed. ``True`` until proven otherwise, so a provider that
        never had the problem says nothing.
        """
        return True

    @abstractmethod
    async def sharing_posture(self, doc_ref: DocRef) -> list[tuple[str, str]]:
        """Return ``[(type, role), ...]``.

        Used to detect a sharing posture with **no owner-curated audience**, such as
        anyone-with-the-link. Raises ProviderError("forbidden", ...) when it cannot be
        read, and the caller must then report the sharing state as unknown rather than
        letting it pass.
        """
