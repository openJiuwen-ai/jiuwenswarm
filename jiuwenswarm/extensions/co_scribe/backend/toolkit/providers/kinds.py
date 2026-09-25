"""Document-format priming and the guardrail state path, host-free.

Both were born in the gateway's cursor store and are needed by every host that
builds a provider for bare tokens -- the agentserver's chat and unattended turns
among them. A runtime adapter must not import the gateway (the architecture test
pins that), so the two live here, beside the receipts ledger, and the gateway
re-exports them.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def prime_provider_kinds(provider, doc_ids) -> None:
    """Feed persisted document formats into a freshly built provider.

    The provider's kind cache is process memory; the panel's store is what survives
    a restart. Every host that builds a provider for bare tokens must call this, or
    a spreadsheet or deck reads as a docx and its tools fail with the
    platform's document-flavored error. Enumerated hosts: the panel listing, the
    watcher tick, the agentserver turn/chat path, the team runtime, the MCP export
    -- the pattern review found the last three missing exactly because the first
    two were fixed by enumeration (§16.12: an enumerated fix does not follow later
    paths; this helper is the single place new hosts inherit).

    Synchronous and fail-soft: a read-only peek at the shared state file.
    """
    note = getattr(provider, "note_kind", None)
    note_url = getattr(provider, "note_url", None)
    if note is None and note_url is None:
        return
    try:
        import json as _json

        data = _json.loads(get_clouddoc_state_path().read_text(encoding="utf-8"))
        docs = data.get("docs") or data or {}
        # Every document's link is read, not only those in ``doc_ids``: a Feishu
        # create needs the tenant origin, which any one persisted link teaches, and
        # the document being created is not in the list. Kinds stay per requested doc.
        if note_url is not None and isinstance(docs, dict):
            for doc, entry in docs.items():
                url = ((entry or {}).get("panel_meta") or {}).get("url") or ""
                if url:
                    note_url(str(doc), str(url))
        if note is not None:
            for doc in doc_ids or []:
                kind = ((docs.get(str(doc)) or {}).get("panel_meta") or {}).get("kind") or ""
                if kind:
                    note(str(doc), kind)
    except Exception:  # noqa: BLE001 - priming must never take a host down
        logger.debug("[clouddoc] kind priming skipped", exc_info=True)


def get_clouddoc_state_path() -> Path:
    """The dedup/threads/panel-meta state file, beside the other guardrail ledgers.

    It lived under ``agent/home`` -- the very directory handed to agent turns as
    their workspace, so a prompt-injected turn with file tools could clear
    ``triggered_ids`` (re-arming consumed triggers) or poison ``panel_meta.kind``
    (adversarial review F6). Guardrail state must sit outside the model's reach,
    with the receipts and the watch registry under ``config/``. One-time migration:
    an existing file at the old location is moved on first touch.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.deployment import (
        workspace_dir as get_user_workspace_dir,
    )

    new_path = get_user_workspace_dir() / "config" / "clouddoc-state.json"
    if not new_path.exists():
        legacy = get_user_workspace_dir() / "agent" / "home" / "clouddoc_state.json"
        if legacy.is_file():
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                legacy.replace(new_path)
            except OSError:
                # A failed move keeps reading the legacy file rather than starting
                # empty -- forgetting consumed triggers would replay old comments.
                return legacy
    return new_path


def persisted_kinds(doc_ids) -> dict[str, str]:
    """The formats the panel recorded, straight from its store -- synchronous and
    fail-soft like the priming above.

    Read at startup to decide whether a document in the config is still a format
    co-scribe serves. It is deliberately the *persisted* answer rather than the
    provider's: asking the provider would be a network call per document on the
    startup path, for a question the panel already answered at adoption.

    A document with no recorded format is absent from the result rather than
    present as "". The caller reads that as unknown, which admits -- the format is
    learned on the first read, and a startup guess must not evict a document the
    panel simply never got round to probing.
    """
    try:
        import json as _json

        data = _json.loads(get_clouddoc_state_path().read_text(encoding="utf-8"))
        # Same dual shape as the priming above.
        docs = data.get("docs") or data or {}
        out: dict[str, str] = {}
        for doc in doc_ids or []:
            kind = ((docs.get(str(doc)) or {}).get("panel_meta") or {}).get("kind") or ""
            if kind:
                out[str(doc)] = str(kind)
        return out
    except Exception:  # noqa: BLE001
        return {}


def adopted_titles(doc_ids) -> list[str]:
    """The registered documents' titles, straight from the panel's persisted
    metadata -- synchronous and fail-soft like the priming above.

    The tool cards put these in front of the model at routing time: a title
    that looks like a filename ("README.md", a deck name) sent the model to
    the local filesystem three campaigns in a row, because nothing in its
    context said the name belongs to a cloud document. The titles themselves
    are the signal; prose alone did not hold.
    """
    try:
        import json as _json

        data = _json.loads(get_clouddoc_state_path().read_text(encoding="utf-8"))
        # Same dual shape as the priming above: the live file's top level is the
        # doc map itself; a "docs" wrapper appears in older snapshots and tests.
        docs = data.get("docs") or data or {}
        out: list[str] = []
        for doc in doc_ids or []:
            title = ((docs.get(str(doc)) or {}).get("panel_meta") or {}).get("title") or ""
            if title:
                out.append(str(title))
        return out
    except Exception:  # noqa: BLE001
        return []
