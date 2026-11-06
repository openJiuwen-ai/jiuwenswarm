# Multiple WeLink assistants in one group

One employee reader login serves multiple colleague bindings. Each colleague owns its assistant AK/SK, socket and delivery journal. Adding or revoking a different colleague preserves existing sockets and the shared reader cursor. Revoking the last binding stops the reader but retains its login configuration.

## Setup

Sign in once on the first colleague's channel page, then create or enter distinct assistant credentials for each colleague. Reuse the reader login for subsequent colleagues. Set each assistant's full group member account in `assistant_account`, either during manual credential entry or after QR creation. Invite assistants to groups yourself. Platform mentions go to the corresponding socket; ordinary history follows each colleague's existing passive/adaptive setting shared with Feishu.

No verified AK-to-member-account conversion is available. Routing matches explicit `assistant_account` (or a member account exactly equal to the AK), never a guessed name or suffix. Without a mapping, multi-assistant history routing, delivery confirmation and handoff may remain pending. Legacy single-slot behavior is retained only with one active binding and one owned assistant in the group; a second binding disables that fallback.

The channel section now stores shared `account`, `cli_authorized`, `reader_credential_dir`, CLI/environment/interval settings and a `bindings` list. Each entry stores `persistent_agent_id`, `assistant_credential_id`, `assistant_secret`, `assistant_account`, `credential_dir`, and `enabled`. Duplicate colleague IDs or AKs, another reader account, and invalid intervals are rejected before saving. Public status never contains SK. Legacy single-slot configuration is read compatibly, new writes use bindings, and deleting the former owner preserves a still-used shared reader directory.

## Catch-up and handoff

A single reader polls history. Each colleague gets its own first-observation latest-page catch-up and readable boundary based on source or existing archive timestamps. Existing active boundaries are unchanged. Out-of-window triggers end before scheduling, without a failure announcement.

The archive is shared per group; Requests are deduplicated per colleague. Pending fanout recipients, colleague identity and mention/history aliases survive restart. Transient failures retain the queue; credential reassignment cannot redirect old pending work to a different colleague.

`[[agent:id]]` becomes a visible name. Socket writes remain unknown until history proves delivery. Only then does a fresh membership check permit local handoff using the confirmed history message ID. Pending confirmed handoffs survive restart; acceptance is idempotent. Native platform mention fields remain deferred pending protocol confirmation.

Upgrade Gateway and AgentServer together. The employee CLI remains read-only, with no send fallback, group creation or invitations. Reconciliation remains an independent background worker and reads only the latest 20 history entries. Missing evidence remains unknown and is never automatically resent. Old account-key routes can only inspect existing receipts, not initiate a new send. Reader queue/alias files and per-assistant delivery journals contain conversation data and should be retained and managed accordingly.
