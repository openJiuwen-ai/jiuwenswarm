# Team message routing patch

This branch packages the implementation as an agent-core dependency patch.
JiuwenSwarm pins agent-core to commit
`2f4bedb9c5d85ab70d7c79d36183e6a98799e94e`; the application dependency
declaration is unchanged. Checking out this branch alone does not activate it.

## Apply

Clone agent-core, check out the pinned commit, then run from that checkout:

```sh
git apply --check /path/to/jiuwenswarm/patches/agent-core-private-message.patch
git apply /path/to/jiuwenswarm/patches/agent-core-private-message.patch
```

From a JiuwenSwarm checkout with dependencies already installed:

```sh
uv pip install --no-deps -e /path/to/agent-core
uv run --no-sync jiuwenswarm-start
```

Do not resync the original pinned dependency over the editable installation.
Keep API credentials outside the repository.

## Behavior

- Private messages accept steer, follow_up and leader-only abort.
- Abort directly stops execution and discards queued follow-ups and pre-abort
  mailbox follow-ups. Other old inputs remain held in history and do not replay. Its body is not run. Only a new explicit direct leader
  message resumes work; peers, broadcasts and scheduler templates cannot.
  Task status is not changed to completed. File side effects are not undone.
- Ordinary team broadcasts use receiver-side Jev: steer, follow_up or drop.
- Public group mentions bypass Jev when idle. Busy recipients use only steer
  or follow_up; errors or missing configuration fall back to original steer delivery. Unmentioned members are not woken.
- Group follow-ups use the existing runtime queue. Native busy steer folds its earlier inputs into context, removing them after successful admission.
- Broadcast submission is not acknowledgment. Leader summaries distinguish
  confirmed receipt from unconfirmed receipt.
- Broadcasts return a message_id. Use send_message(action="status",
  message_id="...") without content/to to query receiver feedback without sending.
  The sender or the same team's leader can query it.
  pending means no decision yet; deferred means waiting for the bound task;
  submitted means runtime acceptance (a follow_up may still be queued);
  dropped means no delivery. None of these means a model has read or acknowledged it.
  Public group mentions also store feedback for their explicitly mentioned recipients.
- JEV_ENDPOINT_PATH supports providers using /v1/decisions; the default remains
  /v1/systemone.

## Validation

Targeted group conversation, group chat integration, message handler and Jev
tests: 79 passed. Earlier real service team rehearsals using Mindshub Jev
observed steer, follow_up and drop for the same broadcast. Public group mention
routing has automated coverage; it has not had a live group chat rehearsal.


### Delivery repair (2026-11-16)

Private messages now return IDs and queryable feedback. The same-team leader may query direct messages too. `withdraw` recalls only the sender's not-yet-claimed inputs, per recipient; dispatching/submitted inputs need explicit correction. Ordinary broadcasts have per-message handled state instead of a shared read watermark. Missing or failed Jev decisions fall back to original delivery. Group chat retains its context cursor. Leader abort no longer requires exactly one active task.
Contract ownership/version checks, snapshot-based acceptance, and substantive replies are prompt-level coordination rules. They are not enforced by a new contract database. See the AgentTeam documentation for current semantics; earlier validation counts above describe earlier patch revisions.


### Steer context folding

Earlier follow-up input is folded into native team steer context and marked `merged` after admission. Failed admission preserves input; later arrivals and different invocation contexts are retained. No second task queue, completion API, or automatic reminder is added. Existing task boards remain the work authority. External CLI runtimes keep their original send interface.

Ordinary-team prompts require an explicit private-message mode. Current-work information uses `steer`; independent later requests use `follow_up`; only the leader may choose `abort`. Specific multiple recipients are contacted separately with a mode for each; broadcasts remain recipient-routed. Omitted modes remain accepted for old callers. This is a prompt rule, not new runtime validation; reload the service and create a new team to load the updated policies.

Mode selection asks whether receipt after the current task ends would be too late. Prerequisite guidance, corrections and dependency readiness use steer; follow_up must remain useful afterwards without affecting the current task. Mixed requests are split, or use steer with explicit timing. Abort is for execution that must stop immediately, not ordinary requirement changes or silence. Tool descriptions respect scheduled recipient restrictions and no longer recommend targets as a way around private-message modes.

## Verification for the develop submission (2026-11-17)

The patch applies to a clean tree of the pinned agent-core revision. All 38
implementation and test files in the applied patch match the locally exercised
implementation. Existing generated-index authors are preserved.

Run from that patched agent-core checkout with the JiuwenSwarm environment:

```sh
python -X utf8 -m pytest -o addopts= -q \
  tests/unit_tests/agent_teams/harness/test_abort.py \
  tests/unit_tests/agent_teams/rails/test_team_tool_rail.py \
  tests/unit_tests/agent_teams/test_group_conversation.py \
  tests/unit_tests/agent_teams/test_message_handler_gate.py \
  tests/unit_tests/agent_teams/test_team_agent_coordination.py \
  tests/unit_tests/agent_teams/tools/test_tool_message.py \
  tests/unit_tests/agent_teams/test_follow_up_merge.py \
  tests/unit_tests/agent_evolving/attribution/test_jev.py
```

Result: **200 passed, 19 skipped** on Python 3.13 / Windows. The skips are not
counted as coverage. UTF-8 mode avoids a pre-existing test's reliance on the
platform default text encoding. The minimal agent-core environment lacks the
SQLite extra; use an environment containing the application's dependencies.

A normal-cluster release-review rehearsal also completed all eight tasks:
12 member-to-member messages, 44 private `steer` sends, one private `follow_up`
send and nine broadcasts. This verifies real interaction, not a performance
advantage of the modes. The scenario fixtures and service credentials are not
part of this patch.

This remains an opt-in dependency patch. No dependency pin, lockfile or automatic
patch application changes are included. Upstream integration into agent-core and
an eventual dependency-pin update require separate maintainer adoption.
