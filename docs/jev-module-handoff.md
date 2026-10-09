# Independent Jev module — handoff

Updated: 2026-10-09. The independent classifier is `packages/jev-classifier`. Jiuwen integration stays outside that package.

## Current branch and scope checkpoint, 2026-10-09

The user authorized starting implementation, then clarified the public boundary: **context + new messages -> delivery behavior (INTERRUPT or APPEND)**. The module must be independent of the native harness. Recipient lifecycle fields, runtime snapshot types and identity/version fields must not be mandatory merely because the old integration uses them. Jiuwen assembles context and applies the returned behavior. This clarification supersedes the more prescriptive recipient-input proposals below.

The user subsequently selected `codex/duplex-a2a-snapshot-20260918`. Recheck after fetching both configured JiuwenSwarm remotes:

- Current HEAD: `a72218565`; zero commits ahead/behind `origin/codex/duplex-a2a-snapshot-20260918`. The previous working branch was `codex/duplex-a2a-0.2.7` at `696154b65`.
- The branches differ substantially (571 files); this is a different development line, not a simple newer/older version of the same change. Current SDK pin: `9e3390195a9ea15235b2b5f7412cb2aa440622cc`, versus `4fd009e...` on the previous branch.
- Shared `duplex_choice.py` and `duplex_decision.py` are identical between those heads. Current `duplex_jev.py` drops the compatibility `_decision` helper. Current routing removes freshness-check machinery and trims shadow/benchmark-specific integration; native interruption includes failure acknowledgement handling from `2fccb162f`. These remain legacy application integration, not the new module's API.
- Upstream develop advanced from `a27e3d13e` to `899fd636e` with four commits covering trajectory timing/UI and voice/video duplex defaults/error reporting. Its SDK pin advanced from `56a1963a...` to `61aff2d61...`. No changes to the JEV adapter, shared choice transport or team harness paths were present in this fetched delta. No merges, cherry-picks or dependency updates were performed.
- The classifier package is `packages/jev-classifier`. Its public call is context plus new messages, and a successful result is exactly INTERRUPT or APPEND. It is a separate distribution, not a dependency of the root application. `jiuwenswarm/common/duplex_jev.py` remains the existing in-app adapter. Keep the module in `packages/`.
- The benchmark-location uncertainty below is now partially resolved: branch `codex/duplex-a2a-0.2.7-message-revision`, commit `1447b4858`, contains `jiuwenswarm/benchmarks/duplex_outcome.py`, its unit test and outcome integration tests inside `test_duplex_e2e.py`. It does not use the separately named `test_duplex_outcome.py` integration file claimed by the older document. This benchmark work is not in the current branch and is outside the independent JEV task.
- Tracked working files were clean at this checkpoint. Earlier untracked documents and launcher files remain. Root `AGENTS.md` is absent on this branch; continue honoring the instructions the user supplied in this chat.

## Confirmed direction

The user has closed the previous message-race investigation and wants to focus only on the new delivery design. Build an independent Jev decision module; the Jiuwen team owns integration.

**Jev returns exactly `INTERRUPT` or `APPEND`.** It does not approve/reject delivery, choose steer/followup/abort, rewrite messages, or arbitrate through a master agent. Do not revive the old master-review workflow, recipient holds, review database, or requirement-history proposal.

Boundary:

```text
Jiuwen: select routing policy and supply message + recipient context
    -> Independent Jev classifier
    <- INTERRUPT / APPEND, or a separate explicit failure
Jiuwen: apply delivery, lifecycle actions and follow-up clearing
```

Working semantic definitions to finalize with examples:

- `INTERRUPT`: the incoming message requires changing the current task/plan or next action.
- `APPEND`: incorporate the message without interrupting the current work.

These labels are semantic judgments. Do not assume INTERRUPT means native abort, or APPEND means a particular runtime queue. Jiuwen owns that mapping. Timeout, invalid output and missing required context must not masquerade as successful APPEND judgments; fallback behavior belongs to the integration contract.

## Leader's requested invocation rules

The original request was translated from Chinese. The user confirmed the literal English tokens: from agent, steer, followup, abort, send_message, to agent, idle. Full original sentence structure was not provided.

| Source | Requested behavior |
| --- | --- |
| Private team message | Sender can specify steer/followup/abort; exact parameter wording remains ambiguous. |
| Idle recipient | Direct delivery; whether this exception is private-message-only needs confirmation. |
| Private steer/abort | Invoke Jev. |
| Private followup | Bypass Jev. |
| Broadcast | Invoke Jev. |
| Chat @mention | Invoke Jev in any state; exact meaning of “Only @” needs confirmation. |
| Chat generating a new steer | Clear pending followup if nonempty; scope and timing need confirmation. |
| Direct typed-user message | User manually controls delivery; bypass automatic Jev judgment. |
| Voice interaction and voice-agent/STT | Invoke Jev. |

Keep source types explicit: a voice transcript is text but belongs to the voice policy. Recommend recipient-specific judgments for broadcasts. Source routing and bypass rules belong to Jiuwen, not the classifier's queue/lifecycle logic.

## Original translated request reviewed, 2026-10-09

The user supplied the full translated request again. It supports the existing independent-module boundary; it does not require a larger agent or a service. The literal translation alone does not establish INTERRUPT/APPEND semantics: that output contract comes from the later agreement recorded above. In particular, “determines whether to deliver” must not silently introduce a reject/drop label.

Likely reading of the tool change: the **sending agent chooses a delivery mode** on private `send_message` calls. `stitch` and `abord` are translation artifacts for the previously confirmed `steer` and `abort`. This does not establish a new editable sender-identity parameter. Jiuwen owns the actual parameter name, allowed values and omitted-mode default.

Recommended invocation precedence, pending confirmation with the requester/Jiuwen:

| Delivery surface | Proposed rule | Remaining uncertainty |
| --- | --- | --- |
| Private team message, idle recipient | Deliver directly, bypass Jev regardless of requested mode | Whether idle bypass is private-only; what an idle abort request means |
| Private team message, active recipient, followup | Bypass Jev | Omitted-mode default and other lifecycle states |
| Private team message, active recipient, steer/abort | Invoke Jev | How each label maps to the requested runtime action |
| Broadcast | Invoke Jev for each recipient, including idle recipients | Idle precedence is proposed; per-recipient evaluation is recommended |
| Chat with a recipient @mention | Invoke Jev for that recipient in any state | Meaning of “Only @”; non-mentions, bare mentions and @all remain unspecified |
| Direct typed-user input using manual delivery controls | Bypass Jev | Distinguish this surface from typed chat @mentions |
| Voice interaction and voice-agent/STT delivery | Invoke Jev at each intended delivery boundary | Partial/final transcripts and whether two calls for one utterance are intended |

Route by the delivery surface and origin, not just by text format. A voice transcript remains voice input. A typed chat @mention follows the chat rule under this proposal, while direct typed-user input follows manual controls. If Jiuwen presents both in one surface, its precedence must be agreed explicitly.

The followup-clearing requirement appears under chat. Keep its proposed initial scope there: when Jiuwen successfully accepts a newly generated steer, clear the agreed pending followups for that recipient/chat. An INTERRUPT result alone must not clear anything. Jiuwen must define whether followup means a runtime queue or a UI draft, which entries are affected, and when the operation takes effect. Do not extend clearing to every team or voice steer without confirmation.

### Contract refinements to settle before implementation

- Refine the working semantic question to **whether the message needs to affect ongoing work before it continues**. Merely adding future work or changing a later task should not automatically count as INTERRUPT. Example: “Use the corrected input for the calculation you are doing now” suggests INTERRUPT; “After this calculation, also export a CSV” suggests APPEND. These are proposed labeled examples, not evaluated behavior.
- Supply one incoming message and one recipient's context per judgment. Required context should describe lifecycle state and, when active, the current task/current activity and relevant recent conversation. Define an explicit idle context rather than treating idle as missing context. Whether idle always implies APPEND remains a semantic decision because some source policies still require a Jev call while idle.
- Keep optional source/requested-action metadata separate from the semantic result. A sender selecting abort does not itself force INTERRUPT or authorize Jev to execute abort.
- Keep the successful public result exactly INTERRUPT or APPEND; represent validation, timeout and provider failures separately. Select a latency budget and context limit explicitly. Jiuwen owns operational fallback.
- Correlate results to the supplied message/recipient context so Jiuwen can detect a stale result. An opaque caller-supplied context token is sufficient; this does not require new persistence or a queue schema inside the module.
- Before integration, Jiuwen must fill in a mapping table for requested steer/abort crossed with INTERRUPT/APPEND, plus label handling for broadcast/chat/voice and the idle case. None of these cells can be inferred solely from the translated request.

Extend validation with two distinct sets: module parsing/failure tests and labeled semantic examples; and documented Jiuwen acceptance scenarios covering every invocation/bypass row, action mappings, accepted-steer clearing, typed mentions, and voice revisions. Delivery-policy assertions belong to integration tests, not to classifier logic. The classifier package is the module implementation. Jiuwen delivery-policy wiring, action mapping, and acceptance scenarios remain Jiuwen's work.

## Implementation plan

1. **Agree a small contract.** Define supplied recipient context, message identity/content, optional source/requested-action metadata, decision semantics and explicit error handling. Start with existing task/conversation context; no new persistent memory subsystem.
2. **Choose packaging and transport.** Create a standalone importable component with injected configuration/client and no JiuwenSwarm imports. Evaluate the existing SDK Jev client below; keep any SDK dependency behind an adapter. A hosted service is not required by “independent.”
3. **Implement the classifier.** One Jev decision stage, strict INTERRUPT/APPEND output validation, bounded context, explicit timeout/cancellation/error behavior. It must not read databases, send messages, pause agents or clear queues.
4. **Test the contract and decisions.** Deterministic tests for parsing and failures; labeled examples for corrections versus compatible additions, insufficient evidence, and different recipient contexts. Any live evaluation is separate from deterministic tests.
5. **Deliver integration examples.** Document required inputs, invocation precedence, output/failure handling, the requested-action/label mapping table and acceptance scenarios from the original-request review above. Jiuwen implements send_message changes, context assembly, runtime behavior and queue clearing.

Decisions still needed: mapping sender-requested steer/abort against INTERRUPT/APPEND; idle-rule precedence; @mention meaning; scope of follow-up clearing; STT partial versus final transcripts. Package location is settled at `packages/jev-classifier`. Do not expand this into another message-race project.

## Latest source audit and reuse candidates

Both repositories' GitHub origin/upstream refs were fetched. agent-core initially fetched only develop; all upstream branch heads were subsequently fetched explicitly. No branches were merged or production code changed for this audit. Findings are source inspection, not runtime verification, and do not cover unpublished/GitCode-only work.

- JiuwenSwarm upstream/develop: `a27e3d13e`; SDK pin `56a1963aa24529e16c7a5930e0e8cc43af347e92`.
- agent-core upstream/develop: `61aff2d61`.
- Team send_message still exposes to/content/summary/optional targets, without sender-selected delivery mode. Internal TeamMessageManager has a framework-level sender override, distinct from the public tool.
- Native idle/start, steer, follow-up and abort primitives exist. Ordinary steering does not clear follow-ups.
- Group-chat plumbing exists: agent-core `8345a2730`, JiuwenSwarm `4d6b38b1f`. Mention selection is deterministic; the requested Jev policy was not found there.
- Cross-session session_send_message has steer/follow_up, but is separate from team send_message.
- Existing fork code: `jiuwenswarm/common/duplex_jev.py`, `duplex_choice.py`, `duplex_router.py`. It already classifies APPEND/INTERRUPT, but is coupled to JiuwenSwarm helpers/configuration.
- **Reuse candidate:** agent-core `upstream/release/v0.1.19.post1`, commit `8f9f11269`, contains `openjiuwen/core/foundation/llm/system_one/` with JevSystemOneClient and typed schemas. Absent from the inspected develop. It depends on SDK errors/logging/message types and defaults to a 360-second timeout with retries; explicitly configure a suitable delivery latency budget rather than inheriting these defaults.

## Workspace and next-agent instructions

### Supporting-document review, 2026-10-09

- `jev-routing-requirements-analysis.md`, section 10, is the relevant prior remote-branch audit for this request. It records existing native primitives, upstream group-chat additions and the release-branch generic Jev client, while reporting that the requested delivery-policy wiring was not found. This is source-audit evidence, not end-to-end verification.
- `current-multi-agent-communication.md` describes the existing application/SDK baseline at SDK `9e339019...`; `queued-message-revision-api-audit.md` audits that same older pin for the now-closed review proposal. Their hooks and limitations are background evidence, not proof of current delivery-policy implementation. Current `pyproject.toml` pins `4fd009e...`.
- `proposed-multi-agent-communication.md` explicitly says not implemented and proposes additional deduplication/recipient selection. Those features are outside this module's scope.
- `queued-message-revision-plan.md` and `queued-message-revision-implementation-stages.md` still describe master review as current. Their status wording is historical relative to this handoff; the stages explicitly report no production implementation started. The architecture and reviewer-context documents belong to that older proposal as well.
- `queued-message-outcome-benchmark.md` claims an implemented deterministic runner, but its named `duplex_outcome.py` and `test_duplex_outcome.py` files were not found in the current checkout or the nested `.benchmark-worktree-v4pro` during this review. Treat implementation location/status as unverified; the document also says live-model arms were not run. `duplex-benchmark-runs-20261005.md` reports separate earlier experiments, not validation of the new invocation matrix.
- Limited source checks in this review confirmed the installed team `send_message` schema still exposes `to/content/summary/optional targets`, and the installed native `_push_steer` delegates to the steering queue. The audited upstream group-chat commit `4d6b38b1f` exists in local Git objects but is not an ancestor of current HEAD `696154b65`; do not equate an upstream addition with a locally integrated feature. The separate `agent-core` clone was inaccessible under the current filesystem permissions, so its release-client findings were not independently reverified in this review. No fresh fetch or runtime tests were performed.

- JiuwenSwarm: `C:\Users\hiennoob\jiuwenswarm`.
- Separate SDK clone: `C:\Users\hiennoob\agent-core`; origin is zuiho-kai/agent-core, upstream is openJiuwen-ai/agent-core. It may require additional write permission. The prior audit could inspect/fetch Git objects, but git status reported “must be run in a work tree”; inspect its layout before editing.
- At handoff, JiuwenSwarm branch was `codex/duplex-a2a-0.2.7`, HEAD `696154b65`. The workspace has changed during the conversation; recheck status/branch and preserve unrelated changes.
- Read applicable AGENTS.md. Respond in English unless explicitly asked otherwise.
- This file is authoritative for the latest scope. `docs/jev-routing-requirements-analysis.md` has detailed background, but its earlier approve/reject-versus-selection question is superseded: the user settled **INTERRUPT/APPEND**. Older `queued-message-revision-*` documents are historical and out of scope.

Suggested opening prompt for the new chat:

> Read docs/jev-module-handoff.md and applicable AGENTS.md. Focus only on the independent Jev INTERRUPT/APPEND classifier. Inspect existing Jev implementations, propose the minimal module contract and package location, and resolve the remaining contract decisions with me before implementing. Jiuwen owns integration; do not restart the old message-race or master-arbitration work.
