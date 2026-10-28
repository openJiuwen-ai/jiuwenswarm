# providers

The platform contract and what both platforms share. The platform implementations,
the factory and the router follow in later PRs.

**Purpose.** Everything above this layer talks to a document through `DocProvider`
and the types beside it, never through a platform client. A provider reads a body as
addressed segments, edits against a base revision, writes regions, adds pages, lists
and answers comments, creates, shares and deletes, and reports its capabilities.

**Public names.**

- `base`: `DocProvider`, `DocSnapshot`, `Segment`, `DocComment`, `DocReply`,
  `DocSummary`, `DocCapabilities`, `AgentIdentity`, `EditResult`, `PageAdded`,
  `ProviderError`, `TextDomain`, `SUPPORTED_KINDS`, `CLOUDDOC_CHANNEL_ID`,
  `read_connection_specs`, `is_trusted_doc_url`.
- `kinds`: `persisted_kinds`, `prime_provider_kinds`, `adopted_titles`,
  `adopted_titles_note`.
- `textmap`: `Cell`, `Shape`, `flatten_grid`, `flatten_slides`, `locate_span`,
  `plan_edits`, `fold_edits`, `apply_edits_to_text`.
- `formats`: `parse_a1_region`, the grid helpers and the post-write read-back check
  both platforms' format modules use (names kept as they were so the modules that
  follow import them unchanged).

**Imports.** `base` imports `wording`; `kinds` imports `settings`; nothing here
imports the host application.
