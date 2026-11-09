# WeLink message reliability

Upgrade Gateway and AgentServer together on `jiuwenmate_0.2.8`. Vision model configuration is unchanged.

Invalid messages and mismatched bindings are permanently rejected and may advance the cursor. Queue saturation, a closed scheduler, and transient service or storage failures return `retryable`. The account's `pending-*.json` preserves the unaccepted batch across restarts; the original source reference still provides server-side deduplication.

Shared groups are rechecked as well as unshared groups. Successful membership observations are persisted with their time, rechecked after 60 seconds, and valid for 300 seconds by default. A single failed lookup or 429 preserves access during that validity period. An explicit removal, or an expired observation without successful refresh, stops history reads, including delivery reconciliation.

Mention and history IDs use an alias without replacing the original Request key. Text-based alignment requires the same group and sender and exactly one unmatched candidate. Replies use only the original ID or an established alias; unmatched replies are independent group messages. A mention is not treated as accepted until its inbound handler succeeds.

Before any socket write, `deliveries-*.sqlite3` records the idempotency key, group, text, assistant identities, history baseline, and wire correlation IDs. The file contains message text and is restricted to owner access. A socket write leaves delivery `unknown`. Every ten seconds the server rotates through one unresolved delivery and asks Gateway only to read history:

- Confirm delivery only from a unique, unused history message with matching group, assistant and text, and the wire ID or a numeric ID after the saved baseline. Text matching requires a single assistant identity.
- A correlated `tool_error` without positive history evidence can confirm failure.
- Missing history, unavailable services, ambiguous matches and missing legacy receipts remain unknown. Absence from history is not proof of failure.

Unknown deliveries are never automatically resent, including after restart. Identical text in the same group waits while an earlier matching delivery remains unresolved. Confirmed deliveries use the existing archival flow.

Reconciliation currently reads the latest 20-message history page. Older results in busy groups may require manual verification and remain unknown; do not reset them to pending for blind retries. Existing delivered rows from older versions are not rewritten.

WeLink history reconciliation runs in a separate background task. Slow requests and scan failures do not block the shared delivery/archive worker. Service shutdown cancels and awaits the reconciliation task.
