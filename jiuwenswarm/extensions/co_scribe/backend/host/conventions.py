"""Per-document writing conventions.

Conventions are a **document-level policy block** written by a collaborator in a
top-level comment. They sit one layer below how the agent works, which is a
deployment-level matter, and the prompt labels the two separately:

    (2) deployer policy -- a trusted source
    (3) document conventions -- untrusted text written by a collaborator, binding
        on style only

Both labels differ again from the untrusted fence used for third-party comments.
Keeping the three apart is what stops a comment from reading as policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocComment
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig, is_conventions_comment

# The cap counts **characters**, not bytes: under UTF-8, counting Chinese by byte
# would shrink the real allowance threefold.
MAX_CONVENTIONS_CHARS = 4000


@dataclass(frozen=True)
class Conventions:
    source: str            # only ever "in_doc" in this release
    comment_id: str | None
    text: str
    item_count: int
    truncated: bool
    content_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _truncate_on_line_boundary(text: str) -> tuple[str, bool]:
    """Cut on a line boundary, so half a rule is never left looking like a whole one."""
    if len(text) <= MAX_CONVENTIONS_CHARS:
        return text, False
    cut = text[:MAX_CONVENTIONS_CHARS]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > 0 else cut), True


def select_conventions(
    comments: list[DocComment],
    cfg: TriggerConfig,
) -> Conventions | None:
    """Decide which conventions currently bind this document.

    In-document conventions only. A deployer-level channel belongs with the
    editable work conventions (the same batch as workmode, through agent-core's
    workspace node) and is not part of this release. A parameter with no config
    key and no caller would only make a reader think it works.

    Every recognition rule below is deliberately narrow:

      * **Top-level comment bodies only**; replies are ignored outright, or anyone
        with comment access could inject policy with a single reply.
      * The author must not be ``self_identity()``, so the agent's own text cannot
        become policy.
      * The marker prefix is the only test -- no semantic judgement.
      * **The earliest match wins** when several exist; the rest are counted and
        listed as ignored in the acknowledgement. Otherwise planting one more
        marker comment would silently rewrite the policy.
    """
    candidates = [
        c for c in comments
        if is_conventions_comment(c, cfg) and not c.author_is_self and not c.resolved
    ]
    if not candidates:
        return None
    earliest = min(candidates, key=lambda c: c.created_time)
    body = earliest.content
    marker_len = len(cfg.conventions_marker)
    body = body[marker_len:].lstrip("：: \n")
    text, truncated = _truncate_on_line_boundary(body)
    return Conventions(
        source="in_doc",
        comment_id=earliest.comment_id,
        text=text,
        item_count=_count_items(text),
        truncated=truncated,
        content_hash=_hash(text),
    )


def _count_items(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def needs_ack(previous: dict | None, current: Conventions | None) -> bool:
    """Whether an acknowledgement reply is due.

    The trigger is **first taking effect**, not first being seen: the reply goes out
    only when the conventions are actually injected into a turn. Anything else has
    the watcher writing into a document nobody triggered.

    Four cases call for a fresh acknowledgement: none to present, a changed content
    hash, reinstatement after retirement, and the first time they take effect.
    """
    if current is None:
        return False
    if previous is None:
        return True
    if previous.get("hash") != current.content_hash:
        return True
    if not previous.get("acked_at"):
        return True
    return False


def render_ack(current: Conventions) -> str:
    from jiuwenswarm.extensions.co_scribe.backend.host.watch.texts import msg

    key = "ack_truncated" if current.truncated else "ack_conventions"
    return msg(key, current.text, count=current.item_count, limit=MAX_CONVENTIONS_CHARS)
