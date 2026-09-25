"""Trigger detection and entry filtering.

Pure logic, no IO. This half of the watcher is the feature's safety floor, so it has
to be exhaustively testable offline.

**One gate, one anchor.** A comment is work for this agent when a person has *named*
this agent -- in the comment or in a reply under it -- and it is not yet done; nothing
else triggers. Within such a thread the anchor is the agent's own last post, and any
newer reply that names the agent continues the conversation.

That replaces three tiers, two addressing mechanisms and a configurable trigger word.
The old text trigger word carried **no identity at all**: one comment beginning with it
fired every deployment watching that document, measured. A mention carries one.

Google's assignment field was the second addressing mechanism, and it is retired. It
had one property a mention lacks -- an agent cannot set it through the API, where a
mention it writes simply gets none computed (both measured). That mattered while the
question was "can an agent recruit an agent", and it stopped mattering once the mention
set was filtered to what a person wrote, which is where that guarantee actually lives
now. What remained was a second way to ask for the same thing, on one of the two
platforms: Feishu has no assignment at all. Two ways is one too many -- people settle on
the simpler one and the other survives only in documentation, where it misleads.

The safety of a summons was never in the addressing field. A mention is a pointer edge
that carries no authority: the dispatched turn is confined to the one document by the
unattended allowlist and by the watch grant, so a forged mention grants nothing its
forger, who already holds write access, did not already have (§16.14).

Entry filtering keeps its order:

    (0) exclude conventions comments -- before every other check
    (1) the mention gate
    (2) exclude anything a service account wrote, this agent or another
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocComment,
    DocReply,
    summons,
)


class TriggerClass(Enum):
    # SUMMONED is the thread's first turn: someone named this agent and it has not
    # spoken yet. FOLLOW_UP is every later turn in the same thread. The class is not
    # persisted -- the dedup key is (doc, comment, reply) -- so the name says what
    # happened rather than which platform field carried it.
    SUMMONED = "summoned"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True)
class TriggerConfig:
    sa_address: str
    conventions_marker: str = "co-scribe 约定"
    # There is one way to summon this agent: name it. Assignment used to be a second
    # way, on the one platform that has the field, and it is retired -- not because it
    # was unsafe but because two ways to ask for the same thing is one way too many.
    # People settle on the simpler one, and the other becomes a trap for whoever reads
    # the docs instead: Feishu has no assignment at all, so half the product's own
    # description named something its user could not do.
    #
    # Nothing is lost by dropping it. A mention is a pointer edge that carries no
    # authority, so the assignment field -- unforgeable through the API as it is --
    # never added safety over it. What makes the summons safe is that the dispatched
    # turn is confined to the one document by the unattended allowlist and by the watch
    # grant: a forged mention grants nothing its forger, who already holds write
    # access, did not already have (§16.14).

    def prefixes(self) -> dict[str, str]:
        return {"conventions_marker": self.conventions_marker}

    def is_for_me(self, c: DocComment) -> bool:
        """Whether this comment summons this agent.

        The rule itself lives beside ``DocComment`` in the toolkit, because the chat
        write mutex has to ask the same question and the two must not be able to
        answer it differently.
        """
        return summons(c, self.sa_address)


def dedup_key(doc_id: str, comment_id: str, reply_id: str | None = None) -> str:
    """The canonical dedup key: ``clouddoc:{doc_id}:{comment_id}:{reply_id|-}``.

    **Built in exactly one place.** The verdict path and the proposal read-back path
    each used to inline the same format, so changing it would leave half of them
    mismatched -- and a mismatch shows up as already-handled work being redone as if
    it were new.
    """
    return f"clouddoc:{doc_id}:{comment_id}:{reply_id or '-'}"


@dataclass(frozen=True)
class Trigger:
    kind: TriggerClass
    comment: DocComment
    reply: DocReply | None = None

    def key_for(self, doc_id: str) -> str:
        """The canonical dedup key ``clouddoc:{doc_id}:{comment_id}:{reply_id|-}``.

        An empty reply slot holds a ``-`` so that ``a:`` and ``a`` cannot collide.
        The doc_id prefix makes a key self-describing in logs and events that
        aggregate across documents; storage is already partitioned per document, so
        the prefix costs nothing at lookup, but without it a key taken out of context
        says nothing about which document it belongs to.
        """
        return dedup_key(doc_id, self.comment.comment_id,
                         self.reply.reply_id if self.reply else None)


class ConfigError(ValueError):
    """A configuration check failed at startup."""



def validate_prefixes(cfg: TriggerConfig) -> None:
    """No configurable prefix may be blank, and no two may be equal or a prefix of
    one another.

    The set shrank twice: first when the text trigger word went away, then when the
    approval and keep words retired with the propose-then-approve loop. What remains
    is the conventions marker -- checked because a marker that normalises to nothing
    would make every comment read as policy. The pairwise check stays for the next
    configurable string, so a collision is a startup error rather than a comment
    meaning two things.
    """
    items: list[tuple[str, str]] = list(cfg.prefixes().items())

    for name, value in items:
        if not normalize(value):
            raise ConfigError(f"{name} 归一化后为空串——空白回复会被判成该关键词")

    for i, (a, va) in enumerate(items):
        for b, vb in items[i + 1 :]:
            na, nb = normalize(va), normalize(vb)
            if na == nb:
                raise ConfigError(f"{a} 与 {b} 相等：{va!r}")
            if na.startswith(nb) or nb.startswith(na):
                raise ConfigError(f"{a} 与 {b} 互为前缀：{va!r} / {vb!r}")


# ``normalize`` moved to the toolkit's wording module (§25.5): the range rail
# compares through it too, and the single-implementation rule survives the move --
# this re-export keeps the trigger layer's name working.
from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import normalize  # noqa: F401,E402



def is_conventions_comment(comment: DocComment, cfg: TriggerConfig) -> bool:
    """Check (0): a conventions comment is **only** a top-level body starting with the
    marker prefix.

    A reply never counts, or anyone with comment access could inject policy with a
    single reply.
    """
    return normalize(comment.content).startswith(normalize(cfg.conventions_marker))


def _agent_first_spoke_at(comment: DocComment) -> str | None:
    """When the agent **first** spoke in this thread, or None if it never has.

    The anchor has to be the first post rather than the latest one. With the latest
    post as the anchor, a reply that arrives *while a turn is running* is lost for
    good: the turn ends by posting, that post is newer than the reply, and every
    later pass filters the reply out as "older than the anchor". The person waits for
    an answer that will never come and the agent waits for a summons it already
    discarded. Anchoring on the first post lets every later reply be considered, and
    the dedup key -- one per reply id -- is what stops a reply being answered twice.

    No new state is needed: the replies returned by ``list_comments`` already carry
    ``author.me`` and ``createdTime``.
    """
    times = [r.created_time for r in comment.replies if r.author_is_self and r.created_time]
    return min(times) if times else None




def find_triggers(
    comments: list[DocComment],
    cfg: TriggerConfig,
    *,
    doc_id: str,
    already_triggered: set[str],
) -> list[Trigger]:
    """Work out this tick's trigger set from one polled snapshot of the comments.

    ``already_triggered`` is the set of dedup keys; a hit is skipped.

    The shape is one gate and one anchor:

        addressed to me and not done?         no  -> nothing happens here
        has this comment had its turn yet?    no  -> the comment itself is the turn
                                              yes -> a reply that mentions me, newer than
                                                     my first post, is a turn

    Answering the thread as a whole on the first turn is what keeps a thread that was
    discussed before being addressed from dispatching one turn per historical reply --
    the turn sees all of them at once. After that, a mention is what continues it.
    """
    out: list[Trigger] = []
    me = normalize(cfg.sa_address)
    for c in comments:
        # (0) A conventions comment is excluded unconditionally, both as a trigger
        #     candidate and from the injection timeline, and this runs **before** every
        #     other check.
        if is_conventions_comment(c, cfg):
            continue
        # (2) Nothing a service account wrote is a trigger -- neither this agent's own
        #     posts nor another agent's. Two agents in one thread would otherwise answer
        #     each other without end, each turn producing the reply that triggers the
        #     next; measured, and the dedup key does not stop it because every turn
        #     writes a new reply.
        if c.author_is_self or c.author_is_service_account:
            continue

        if not me or c.resolved:
            continue

        # "Is this the first turn?" is the comment's own dedup key, **not** whether the
        # agent has spoken. It can speak for reasons that are not turns -- the mention
        # hint is one -- and using speech as the proxy broke the exact sequence that
        # hint asks for: the hint counted as having spoken, so only replies newer than
        # it were looked at, and the summons it invited was never seen. The key already
        # exists and already prevents repeats.
        first = Trigger(TriggerClass.SUMMONED, c)
        if first.key_for(doc_id) not in already_triggered:
            # (1) The gate, **at first touch only**. A mention -- in the body or in a
            #     person's reply -- names one account, so a document may hold several
            #     agents and each one sees only its own work. The mention set is
            #     pre-filtered to what a person wrote, so an agent cannot summon an
            #     agent through it (§16.14). The first turn answers the thread as a
            #     whole, so a summons that arrived in a reply still yields one
            #     SUMMONED turn, not a FOLLOW_UP with nothing before it.
            if not cfg.is_for_me(c):
                continue
            out.append(first)
            continue

        # The first turn is behind us. From here on the thread continues **only on a
        # reply that mentions this agent**: no @, no participation. That is the base
        # rule for a thread several people and several agents share -- a discussion
        # between two other collaborators must not wake the agent to answer each of
        # their sentences, and anyone who does want it back says so by naming it.
        #
        # The cost is that an answer to the agent's own question does not reach it
        # unless it carries an @, so every text that asks for something back says
        # "@ me" rather than "reply here".
        #
        # What the earlier replies still do: they are thread context. The next turn's
        # prompt carries the whole thread, so a request written without an @ is read
        # (and reconciled against the @-ed one) as soon as some reply does summon the
        # agent. The mention decides *when* it participates; the thread decides *what*
        # it was asked.
        #
        # The anchor is the agent's first post. A thread whose first turn left no
        # post behind (the dedup key was written, the turn never got to speak) falls
        # back to the comment's own time, so a mention newer than the comment itself
        # still continues the thread instead of leaving it dead for good.
        spoke_at = _agent_first_spoke_at(c) or c.created_time

        for r in c.replies:
            # A reply older than the anchor belongs to the first turn, which saw the
            # thread whole.
            if not r.created_time or r.created_time <= spoke_at:
                continue
            if r.author_is_self or r.author_is_service_account:
                continue
            # Replies carry server-computed mentions, so this is the platform's own
            # answer to "was this addressed to me", not a substring search.
            if not any(normalize(a) == me for a in r.mentioned_addresses):
                continue
            t = Trigger(TriggerClass.FOLLOW_UP, c, r)
            if t.key_for(doc_id) not in already_triggered:
                out.append(t)
    return out


# ---------------------------------------------------------------- startup sweep


class OrphanClass(Enum):
    PLACEHOLDER_AFTER_DISPATCH = "placeholder_after_dispatch"  # crashed after dispatch
    PLACEHOLDER_BEFORE_DISPATCH = "placeholder_before_dispatch"  # crashed before dispatch


@dataclass(frozen=True)
class Orphan:
    kind: OrphanClass
    doc_id: str
    turn_id: str | None = None
    comment_id: str | None = None
    payload: dict | None = None


def classify_orphans(
    doc_id: str,
    inflight: dict[str, dict],
    triggered_ids: set[str],
) -> list[Orphan]:
    """Sort leftover inflight state into two classes at startup.

    An inflight record whose keys are all on disk was dispatched before the crash, so
    its placeholder needs a visible closing line; one whose keys never landed was not,
    and its placeholder can simply be released.
    """
    out: list[Orphan] = []
    for turn_id, rec in inflight.items():
        keys = set(rec.get("keys") or [])
        kind = (
            OrphanClass.PLACEHOLDER_AFTER_DISPATCH
            if keys and keys <= triggered_ids
            else OrphanClass.PLACEHOLDER_BEFORE_DISPATCH
        )
        out.append(Orphan(kind, doc_id, turn_id=turn_id, payload=rec))
    return out
