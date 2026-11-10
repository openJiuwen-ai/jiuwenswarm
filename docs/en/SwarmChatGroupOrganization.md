# SwarmChat Group @ Expert Teams and Team Organization

> **Goal:** Describe how a SwarmChat group message that mentions two or more expert teams becomes one Team Organization, and how only the Owner posts the final result. The reader is a developer changing this path or matching it to runtime logs.
>
> **中文版：** [Chinese Version](../zh/SwarmChat群聊专家团组织.md)

---

## 1. What this path does

The SwarmChat group panel is `/groups` and `/groups/{groupId}`, rendered by `GroupChatWorkspace`. When the user mentions two or more expert teams, those teams share one request. They do not each open a private run and each post a group message.

The group message keeps its mentions, so the outer group team wakes the mentioned proxies through the existing path. The first mentioned expert team is the Owner and decides between ordinary chat, clarification, and substantive work in its existing model turn. Only substantive work makes each proxy call `run_team` to launch its resident expert-team instance. The outer group team never joins the organization.

The panel shows group messages and execution cards; organization work has an aggregate card and ordinary chat has none. It does not call `org.snapshot` and it does not mount an organization panel. The organization result appears after the Owner writes it into group history.

## 2. Four roles

| Role | What it is | What it does not do |
| --- | --- | --- |
| Group proxy | A member of the outer group team. `member_name` looks like `member-…`. After it is mentioned, its model calls `run_team`. | It does not do the child task and it does not post the organization result. |
| Expert-team instance | The resident Team launched for that proxy, identified as `org-expert-{digest}`. It uses `org_*` to do the work. | It does not speak in the group as the proxy. |
| Owner | The first expert team in the plan. Its instance creates the root, reviews submissions, and a background task writes the final report into the group. | The outer group team is not the Owner. |
| Single-team execution | The `GroupExecutions` path used when no live organization plan matches. `finish` posts the group message. | This path stays unused while a multi-team organization is live. |

A group roster row stores `kind`, `id`, `member_name`, and `display_name`. A single expert is `agent_template`. An expert team is `agent_group`, and the package name is `id`. On read, `expert` means `agent_template`, `expert_team` means `agent_group`, and `definition_id` means `id`. Organization selection accepts expert teams only.

## 3. End-to-end flow

```mermaid
sequenceDiagram
    participant User as SwarmChat
    participant Gate as Message ingress
    participant Owner as Owner proxy
    participant Proxy as Other proxies
    participant Org as Expert organization
    User->>Gate: Pure multi-team mention
    Gate->>Gate: Persist candidate with original mentions and client_message_id
    Gate->>Owner: Original message notice
    Gate->>Proxy: Original notice; end turn before model call until decision
    alt Ordinary chat
        Owner->>User: group_send_message; close pending state after successful post
    else Clarification needed
        Owner->>User: ask_user; no execution card
        User->>Owner: Answer; continue deciding this request
    else Substantive work
        Owner->>Gate: run_team commits task decision
        Gate->>Gate: Create organization plan and card
        Gate->>Proxy: Private framework notice bound to original message
        Owner->>Org: run_team launches its instance
        Proxy->>Org: Each proxy calls run_team to launch its instance
        Org->>Org: Create, execute and accept children in stages, then summarize
        Org->>User: Owner publishes final result
    end
```


### 3.1 How the frontend builds mentions

`selectedMentions` requires both a picker selection and `@display_name` in the text, followed by the end of the text, whitespace, or one of `，。！？、,:：;；!?`. Typing `@name` without the picker stores `mentions: []`.

A message with empty mentions is still stored. `_begin_team_round` returns immediately, so no proxy is woken. That is not a running task.

### 3.2 When a plan is recorded

Only a saved group's pure multi-team message (at least two distinct `agent_group` members and no other member kinds) enters candidate routing. Ingress persists `organization_routes.json` in the session directory without creating a plan, execution card, or instance.

The first mentioned team is the Owner. Its existing model turn chooses `group_send_message` with `organization_reply=final_chat` for a final ordinary-chat answer, `ask_user` for clarification, or `run_team` for substantive work. No extra classification call is made. Single experts, single teams, and mixed mentions retain their existing routes, including direct-reply marks and queuing.

Only the task decision calls `record_organization_plan`, keyed by this message's `client_message_id`. The archived mentions and body remain unchanged; answers to clarification questions bound to this request are appended to the organization brief. Retries and follow-ups remain isolated by message identity, even if the model supplies an older execution ID. Overlapping active organizations retain their existing queue behavior.

Ordinary chat reserves its decision before posting with a stable server-generated reply ID. Only a successful post completes the request; retries reuse its content and ID. Non-Owners cannot decide, clarify, or post. An Owner turn ending without a reply fails the candidate. Pending decisions or chat posts time out after 15 minutes; a bound Owner process exiting also ends waiting. Clarification does not use that 15-minute timeout. Historical organization tasks are not migrated.

New organization candidates store the Owner PID, creation time, and stable process identity. Liveness checks prefer psutil's process identity so macOS clock adjustments cannot turn a pending clarification into a false failure. Confirmed process loss, zombie state, or identity replacement ends waiting; permission errors or temporary inspection failures preserve the current state for a later recheck. Legacy records retain creation-time checks. Already-ended proxy turns are not automatically resumed. These checks apply only to pure multi-team candidates and do not replace proxy-driven `run_team` calls.

Clarification resumes create a fresh proxy invocation. The outer `GroupAskUserRail.before_invoke` mounts a round-local binding rail on the actual inner ReActAgent. Its inner `before_invoke` verifies the native question call ID, group, target, and member against the persisted question and restores the original `client_message_id` before interrupt replay and tool-context copying. It does not rely on ContextVar inheritance across the scheduler queue or select the latest plan. The outer context retains the same verified identity for round completion. Question targets remain on NativeHarness; the inner binding rail is unmounted on completion, exception, or cancellation, without duplicate registration. Only organization answers carry a server-generated message marker; it detects missing records and identifies failures, but never substitutes for persisted ownership checks. Missing records/plans, unanswered questions, or ambiguous ownership terminate invalid resumes and block task launches, broadcasts, and result publication instead of falling back to a private team. Answers are saved before waking the proxy and restored to pending if delivery fails. Single-expert, single-team, mixed mentions, and private-team questions retain their existing paths.

### 3.3 How proxies wake

The message is delivered with its original mentions through agent-core `post_message`. Targets are mentioned members other than `user` whose role is not `passive_human`. The outer group Leader is not pulled into the organization by this message.

Before admission, other proxies end their turns before calling the model. Once admitted, private mailbox notices with framework-only metadata wake them. Recipients verify the sender, recipient, and group before reconstructing the original user notice. The program sends notices; proxies still call `run_team` to launch instances. After a proxy calls `run_team`, the organization path returns as soon as the instance is ready. The model-facing text says the organization is already running, do not call `run_team` again, do not post a group message, and the Owner posts the final report.

## 4. The run_team fork

`RunTeam.invoke` first resolves the consumed message binding. Only the Owner can admit a pending multi-team candidate. Premature non-Owner calls return that they are waiting for the Owner and no task is running. Chat or finished candidates cannot fall back to private-team execution. For a bound organization task, `start`, `continue`, and `update` use this message's plan instead of an old execution target. One proxy cannot cancel an organization.

Other messages retain existing single-expert, single-team, and mixed-mention routing, including updates, cancellation, and follow-ups into completed organization instances.

Private-team execution still accepts successful plain-text leader replies. Terminal Core `task_failed` frames, including typed payloads, fail the execution instead of being ignored. Retry notices discard unfinished text from the failed attempt; a successful retry can still deliver normally. A leader reply ending in the explicit `[Request interrupted by an unexpected error]` sentinel is not published as a successful result. General business-report keywords are not used to infer failure.

A matching plan calls `run_organization_member` and does not create a `GroupExecutions` record. With no match, a managed group uses the existing single-team background run. `GroupExecutions.finish` then posts `execution-result-{execution_id}`. A single expert and a single expert team stay on that path. A direct reply does not resume the organization instance and is no longer deferred until the organization posts. While an organization runs, each proxy turn lists the organizations it has joined (card `execution_id`, task, Owner or member). The proxy's organization instance is busy then, so a new task does not take the after-organization follow-up path into that instance.

Overlapping `run_team` calls from one proxy share one instance-setup task. Each plan has one Owner background wait.

### 4.1 Why the organization call returns immediately

AbilityManager cuts a tool call off at 300 seconds by default, with a hard ceiling of 3600 seconds. An organization can take longer, so `run_team` cannot wait for completion inside the tool call.

After the instance is ready:

- A member proxy returns the in-progress result only.
- The Owner proxy also returns immediately. The wait and the group post run in `_owner_runs`.

While the Owner background is running, or this plan is still queued behind an older round, the plan is not marked `expired` just because it is older than 62 minutes. A later `run_team` stays on the organization path and returns immediately. If no Owner task is alive, no queue task is waiting, and the plan is older than 62 minutes, the plan becomes `expired`. A later `run_team` for that same multi-@ message does not open a single-team execution and does not post to the group. A different message that is not this plan still uses the single-team path or a follow-up. Those 62 minutes apply only to a plan with neither an Owner background nor a queue task. Once the Owner background is running, the result wait has no time limit. A queued plan stays until its turn.

## 5. Instances and organization tasks

One group can run organizations with disjoint members concurrently. Each `organization_teams` row stores `organization_id` and is updated by organization plus member. Joining, waiting, and pausing instances at completion query only that organization. Success, failure, or cancellation clears only its registrations. Legacy rows inherit their recorded top-level `organization_id` on read and preserve that ownership on the next write. Group-wide publication checks may inspect all registrations; deleting a group dissolves every organization individually. Proxies still launch instances through `run_team`, and overlapping member sets retain the existing queue rules.

The instance id is stable for a group session and `member_name`: `org-expert-` plus a 12-character digest. The same proxy reuses that Team, and the id does not change with the organization. Every multi-mention request admitted as substantive work creates a new organization id: `gorg-`, the sanitized session id, and an 8-character plan digest, at most 96 characters. Earlier rounds are never reused. That round's task pool stays isolated; expert-team instances and their conversation context are still reused. A plan saved before organization ids were recorded is treated as the older `gorg-` plus sanitized session id. Instances share the outer team's database with the group session, so group deletion can stop them the same way it stops a private run.

The Owner instance receives `complete_team_run` and a prompt that requires `org_*` coordination instead of an internal swarmflow for the cross-team request. Other instances receive the capability `group-member:{member_name}`. Reusing a live instance still applies this round's role: an Owner gains the submit tool, Owner prompt, and `group-coordination`; a member loses them. An existing instance is not the same as a round that is already configured.

The Owner background performs the mechanical setup itself:

1. Wait until every planned proxy binds its stable instance through `run_team`. The background does not launch a missing instance. Wait while any missing proxy is active or has a pending question, queued admission, or running admission bound to this exact plan. Each missing member gets its own idle timer; activity or an explicit wait resets that timer. Fail only after every missing member has been inactive (or unreadable), without an explicit wait, for 120 continuous seconds. The root is not created, and the Owner does not launch a replacement team from a template.
2. Create root `groot-{plan digest}` and claim it. That id comes from the plan, so recovering the same plan still recognizes the original root. The background does not choose the aggregation mode and does not start the root. If the user named `SUMMARY_TEAM` or `HIERARCHICAL`, the Owner model uses that mode. If the user named neither, the Owner model chooses from the task and does not default to Summary Team. The model then starts the root. Only a summary execution of this same root can keep the round waiting.
3. Register capability `group-member:{member_name}` for each mentioned expert team. The Owner instance publishes the child tasks. The background does not pre-create placeholder children.
4. If the root still has no child after 90 seconds, queue one reminder to the Owner instance. The reminder tells the model to select the mode first, choosing itself when the user named neither, then create one still-claimable child per mentioned expert in that same turn. Each child's `required_capabilities` is only `group-member:{member_name}`, so the matching team is woken and claims it with `org_claim_task`. Do not call `org_delegate_task` unless the user asked to delegate, and do not wait for one child to finish before creating the next. `SUMMARY_TEAM` finishes through the summary execution, and `HIERARCHICAL` has the Owner complete the root.

Expert work, review, summary, and final submission stay inside the instances. The Owner instance calls `complete_team_run`, which fills the same result slot the Owner background is waiting on. For `SUMMARY_TEAM` it also waits until the summary execution completes. For `HIERARCHICAL` the Owner completes the root and there is no separate summary execution. This wait has no time limit. The background posts only after those are done. The group message contains only the requested deliverable. It does not describe whether an instance was paused, rebuilt from a template, or reused an earlier draft. If the root completes and no submission arrives, it waits another 180 seconds and can use the root output.

For short text or a short report saved as one file (such as Markdown), when bound sources suffice and have no conclusion-changing disagreement, the Summary Team prompt directs its leader to draft, check, write the file, and call `org_summary_complete` directly. A file request alone does not trigger delegation to the integrator and drafter. Code, multiple files, long research, complex formatting, or specialist work can still use internal collaboration. Final files still belong in the organization's `summary/` directory; submission validation and Owner publication remain in place.

The organization Owner preserves the user's original length requirements when creating child and summary tasks. The Summary leader, integrator, and drafter pass those requirements through outlines and assignments. Approximate requests such as “about 500 words” do not acquire invented percentage tolerances, strict ranges, per-section quotas, or count-report acceptance criteria. Explicit user limits and per-section counts remain binding: draft first, count with a tool, then make a focused correction and recheck if needed, avoiding repeated manual counting in model reasoning. These are organization-specific prompt constraints, not a new hard length validator or a latency guarantee. Shared prompts for ordinary single-expert, single-team, and mixed-mention requests are unchanged.

### 5.1 Connecting organization tasks to private-team work

The Owner must explicitly claim its own child with `org_claim_task` and start it with `org_update_task(action='start')`. Claiming the root does not claim that child, and Core does not wake the creating team on its own task-created event. Private `create_task`, `claim_task`, and `swarmflow` work belongs inside that started child. Other expert tasks may be created in dependency order, such as creating a PPT task after its source content is ready; they need not all exist before the Owner starts private work.

Organization instance leader specs declare the `swarm.group_org_lifecycle` Rail so cold recovery rebuilds it through the same provider. Reused instances also receive the declaration and live Rail. The policy applies only when the instance's bound organization matches a live multi-mention plan containing that proxy. Single experts, single expert teams, mixed mentions, and direct follow-ups after a finished organization keep their existing behavior. The separate Summary Team spec does not install this Rail.

- Before each model call, the policy reads current child IDs, assignment/execution states, pending reviews, and repair relations from the organization task pool. It adds no model calls. Private work still needs an organization completion submission; reviews use organization child IDs, never private task IDs. Results are not automatically accepted.
- Private task dispatch without this team's claimed and started child returns corrective instructions without dispatching work. Work authorization includes descendants at any depth under the current root, but requires an IN_PROGRESS task assigned to this team; tasks belonging to other teams or roots do not qualify. Without an IN_PROGRESS child, the Owner may use the final-integration dispatch exception only when every mentioned expert has a direct child and all sources are accepted or superseded by accepted repairs. Prompts require checking the full request and downstream tasks not yet created before final aggregation; completion of existing tasks alone does not establish completion of the request.
- Expert children require exactly one `group-member:{member_name}` capability from this plan. A team merely present in the public group is not a participant in this organization. Another root cannot be created.
- When the same expert has an unresolved failed, rejected, or revision-requested child, a new sibling must identify the original through `repairs_task_id`. Core continues to enforce repair eligibility, active-repair deduplication, and atomic transitions. A v2/v3 name alone is not a repair. Accepted work with no unresolved failures does not prevent explicitly requested new work.
- `complete_team_run` requires the root to be complete. A transient state-read failure returns retry instructions for dispatch while leaving inspection, claim, start, and completion tools available.
- `swarmflow(action='pause'/'stop')` with an existing `resume_id` does not require an active organization child or a successful organization state read, so cleanup can be requested after task failure or during a database outage. The workflow tool determines the actual control result. Launching and resuming workflows still require work authorization.

These checks preserve proxy-driven `run_team` instance launch, leave card protocols unchanged, and do not rewrite historical failed records.

## 6. The two group messages

Pure multi-team messages show one Owner bubble below their request: “Received, processing…” while undecided and “Waiting for clarification…” after asking the user. A successful ordinary reply removes it. Task admission changes it to “Organization received your request, processing…”, followed by child-task progress and summarization. Failed candidates display a retry message without the waiting animation.

`group.get` exposes `organization_routes`; `group.route.updated` triggers a frontend refresh. Reloads restore state by the exact `client_message_id`. Organization executions still bind through `organization_plan_id`. Other teams have no pending bubbles. A terminal organization or matching Owner result removes its bubble. Single experts, single teams, and mixed mentions retain per-member indicators.

| Source | Message id prefix | Sender | When |
| --- | --- | --- | --- |
| Organization completion | `expert-result-{plan_id}` | Owner proxy | After the Owner background confirms completion |
| Single-team completion | `execution-result-{execution_id}` | The proxy that ran it | When single-team `finish` succeeds |

During the organization the panel can stay empty. The organization path creates no execution card. A mentioned proxy that is not the Owner cannot reply to that organization message: `group_send_message` and `send_file` are rejected. It replies to other messages normally. That proxy's expert-team instance also loses those tools, so it cannot reply to the group. Only the Owner background posts `expert-result-` after the organization finishes. A message whose id starts with `execution-result-` came from the single-team auto-post, not from the Owner organization report.

On completion—success or failure—the background pauses the expert-team instances, then dissolves the organization on the SwarmChat side: release membership, erase this organization's task-pool rows (including task and output records), and stop only that organization's summary team. It does not call the runtime dissolve that would stop expert-team instances. Those instances and their conversation context stay. On success it posts `expert-result-` and marks the plan `done`. That post attaches only the Owner's final files: the `complete_team_run` attachments plus the file named by the root task `result_uri` (for example the summary team's report under `summary/`). Child-task outputs and drafts are not attached. Identical file content is attached once, and the first path is kept. Without a final file the post is text only. On failure it marks the plan `failed`, propagates the error, and does not post `expert-result-`. The Owner proxy posts one "执行失败：<cause>" message (`expert-error-{plan_id}`). The plan is also marked `failed` immediately when the Owner background is cancelled before it posts, when the Owner instance is not bound to the organization, or when the queue task exits with an error or is cancelled. A killed process cannot write that status; a plan with neither an Owner background nor a queue task still becomes `expired` after 62 minutes. The next multi-@ round always gets a new organization id; the Owner is the first @-mentioned expert for that round. Group deletion still uses the full dissolve and stops these instances.

A later message that mentions only one of those experts is a follow-up, not a new organization. The proxy decides: a simple request that one agent can answer uses `group_send_message`; work that needs the expert team calls `run_team`. That `run_team` returns to the proxy's existing instance, on the same team and the same session, and loads the context from its earlier child task. It does not open a blank private team and it does not rejoin the organization.

## 7. Plan status

Plans live in `organization_plans` on the group's `group.json`. Instance bindings live in `organization_teams`.

| Status | Meaning |
| --- | --- |
| `pending` | Recorded, and the Owner background has not marked it running yet. |
| `running` | The Owner background is waiting for the result. |
| `done` | The Owner wrote the final report into the group. |
| `failed` | Setup failed, or the organization root failed. |
| `expired` | No Owner background is alive, no queue task is waiting, and the plan is older than 62 minutes. |

A repeated `run_team` for the same user message joins that message's plan. If this proxy has not joined an older live plan yet, this `run_team` joins that older plan first and does not skip it just because a newer mention is already in the history. Recording a later multi-@ plan that shares a member with an unfinished plan schedules that plan's Owner immediately. A proxy that is not in the older plan is queued as well and does not bind instances that the older plan still holds. A failed early attempt leaves the plan queued. Once the older plan has been accepted, another multi-@ call also returns queued. The Owner background starts the new plan after the older one finishes. A single @ while an organization is still live no longer stays on that organization path; the proxy chooses an organization update or a new private run. When that same multi-@ message's plan is already `failed`, `expired`, or `done`, this `run_team` stops: it does not join the organization, does not create a single-team execution, and does not post to the group. A different message that is not this plan still follows the single-team path, a follow-up, or a new multi-@ plan.

## 8. Code map

| Step | Location |
| --- | --- |
| Mention selection | `channels/web/frontend/src/features/groupChat/state.ts` |
| Group page | `channels/web/frontend/src/features/groupChat/GroupChatWorkspace.tsx` |
| Plan recording | `_record_group_organization_plan` in `server/runtime/agent_adapter/team_helpers.py` |
| Plan, roster, and organization bindings | `server/runtime/group_service.py` |
| `run_team` fork and single-team post | `agents/swarm/group_chat.py`, `server/runtime/group_execution.py` |
| Instances, task split, and Owner background | `agents/swarm/group_org_bootstrap.py` |
| Mention notification | agent-core `openjiuwen/agent_teams/group_chat/handler.py` |

Tests are in `tests/agents/swarm/test_group_org_bootstrap.py`. The single-team test checks that `run_team` keeps the private path when no plan is live.

## 9. Current boundaries

- A repeated `run_team` for the same multi-@ message stays on the organization path while that run is live. After that plan has ended, another `run_team` does not open a single-team execution and does not post to the group. A different message is routed by the proxy: an organization update, a new private run, or a follow-up.
- Whether a message belongs to the organization is the model's judgement, with no rule behind it. A wrong call turns an organization addition into a new task, or sends an unrelated task into the organization instance.
- The Owner background waits until the round finishes or fails, with no time limit. On failure, cancellation, an unbound Owner instance, or a queue task that exits with an error or is cancelled, the plan becomes `failed` and no `expert-result-` message is written. On failure the Owner proxy posts `expert-error-{plan_id}`. A killed process cannot write that status; the plan still becomes `expired` after 62 minutes when neither an Owner background nor a queue task is alive.
- Member join waits while any missing proxy is active or explicitly waiting on this plan. It fails only after all missing members have each been inactive (or unreadable) without such a wait for 120 continuous seconds. Questions or queues belonging to another organization do not suspend this timer; execution never starts short-handed.
- The panel shows the organization execution card after the organization is created, with one row per expert team. The behavior is in the next section.

## 10. Organization execution card

Reuse the single-team `ExecutionCard`. Do not add a card component. One multi-expert request writes one execution record with `execution_kind: organization`. The card title is that record's `title`, and each row comes from `tasks` and `task_details`. The single-team create, `start_group_task`, `finish`, and internal `team.task` projection stay unchanged.

The organization record does not call `GroupExecutions.finish`, because that posts another `execution-result-` group message. The organization group message is still only the Owner background's `expert-result-{plan_id}`.

The card appears after the organization has been created, not when the plan is first recorded. The Owner proxy's `run_team` launches the Owner instance and completes `create_organization`; that step writes this one record with `presentation: card`, so the list and `group.execution.created` show it. The id is derived from the plan id. Entering the same organization again updates that card.

The header progressively switches from the original request to root task content as described below. After root `groot-…` is created, store `root_task_id` on the card and do not add a row for it. Each row is one organization child the Owner published under that root. The row title is the child's own `title`, and the small text is the display name of the expert team that claimed it. A row does not appear before that child exists. Status is `pending` before claim, `in_progress` after claim, and `completed` after acceptance. Tasks inside an expert-team instance are not written into top-level `tasks`; they appear in the disclosure described below. `SUMMARY_TEAM` adds a final summary row labeled Summary Team. Its title is that summary task's own `title`. It is `in_progress` while it runs and `completed` when it finishes, and the row appears only when the summary task exists and every source chapter is completed and accepted (or covered by an accepted repair). A summary task pre-created while sources are unfinished or awaiting review stays off the card, its progress total, and its summarizing stage; this presentation rule does not alter runtime scheduling. `HIERARCHICAL` has no summary task, so the card does not add a summary row while any chapter is still unfinished. A child sent back for revision stays failed on its own row; an accepted repair task closes that chapter. After every chapter is accepted it adds the final row. The small text is the Owner expert team that completes the root, and the status follows the root. The title is the report heading the Owner stored on the root output. Until that heading exists, the row title is “汇总”. The report text stays off the card; after the Owner posts, View result still jumps to the group message. While every child is accepted and summary is still running, the card stays `running`.

Leave `member_name` empty and store `owner_member`. Single-team lookup and `team.task` attachment use `member_name`. An organization card that used the Owner's name would collide with the Owner's single-team execution, and internal tasks would be written into its rows.

Exceptions use the card's existing failed status. There is no new error-text area. A failed child row is `failed`. If Owner setup fails, the organization root fails, or the Owner wait is cancelled, the card is `failed` and every unfinished row is `failed`, so the existing failure icon and status label show it. When setup or the root fails, the Owner proxy also posts one "执行失败：<cause>" message (`expert-error-{plan_id}`), so the group never goes silent.

After the Owner posts, the card becomes `completed` and stores that message's `message_id`. View result stays the current in-page link. Besides matching the sender to `member_name`, also match a sender equal to `owner_member` when the message id equals the card's `message_id`.

The Owner background's existing poll reads these children from the organization task pool, writes the same execution record, and pushes `group.execution.updated`. Proxy `run_team` results do not update the card. `project_organization_progress` is not the entry point: it requires the root team to be the outer group team, while this root is the Owner instance.

---

## Early organization progress

Requests mentioning multiple expert teams exclusively create the organization card when the plan is recorded, before proxies launch instances through `run_team`. The same card displays Starting expert teams, Experts working, and Summarizing based on real task state; the interval before execution uses Processing. Queued requests retain their queued state. The stage is persisted for refresh without replacing child rows or completion status. Duplicate acceptance cannot reset an existing card. Failed or expired startup plans close their bound early cards. A round that errors or ends before any proxy calls `run_team` fails the plan, and the next execution-list query closes that card too. A plan that is already executing, or waiting behind an earlier round, stays as it is. A proxy that called `run_team` without acceptance may retry within its live turn. Waiting for an `ask_user` answer keeps that turn and its binding open. When the turn actually ends, historical attempts are cleared; once all turns end without acceptance, running work, or a queue, the unstarted plan and its card fail. Cancellation during initial card publication also closes an unstarted plan, even before the caller receives its plan ID; already accepted, active, or queued plans are preserved. Publishing errors do not stop execution. Single experts, single teams, mixed mentions, and historical cards without a stage keep their existing presentation.

Organization cards link `organization_plan_id` to the prompt's `client_message_id` and never sort before that prompt. This applies to early creation, message acceptance, and history refresh without changing the stored creation time.

Organization cards show an overall stage only while no child rows exist. Once rows are present, all overall stage footers are hidden, including startup, execution, summary, and processing; progress is shown by the task rows, including the summary task. Other card types keep their existing presentation.

## Routing follow-up organization requests

Each message mentioning only two or more expert teams owns an independent organization plan, including related follow-ups such as adding suggestions. When the proxy consumes input, the exact trigger ID in the system notice is verified against the archived message and bound to its tool calls; neither the oldest pending turn nor the latest history entry replaces that identity. Input batches containing organization requests are delivered to the model individually in arrival order.

`run_team` routes by this binding before interpreting the model-selected action. For a new organization message, `start`, `continue`, and `update` all accept that plan. An older organization sharing members blocks the new plan rather than receiving its requirements. Retries keep the same plan, failed acceptance retains the binding, and completed plans do not restart. `cancel` is never converted into startup.

Proxies still launch instances through `run_team`, using the existing queued recovery mechanism. Single experts, single teams, and mixed mentions retain their direct, private-team, and adjustment paths even while another organization is active. Result publication restrictions and shared-stream logging remain unchanged.

## Back

- [Documentation home](../README_EN.md)
- [Project home](../../README.md)

The organization card header progressively adopts its root task: before the root exists it uses the original request; once available it prefers a meaningful root title, falling back to the description for an empty title or the `Swarm group request` placeholder. Missing root content preserves the existing heading, or uses the original request on first publication. Final report text never replaces the heading; `task` retains the original request. This affects organization cards only and preserves existing first-sentence extraction, title length limits and overflow scrolling. Single-expert, single-team and mixed mentions are unchanged.

Public user broadcasts delivered as `<team-inbound from="user" type="broadcast">` also retain their exact request identity. Only the outer message ID is resolved against the current team message table; group metadata, sender, broadcast flag, client request ID and mentioned recipient are verified before Core renders the normal group notice. IDs embedded in bodies or historical quotes never select a route. Failed verification invalidates the old binding and rejects run_team instead of guessing a recent request. Verified multi-team requests activate their candidate first and report a missing organization plan rather than resuming an old private team. Single-expert, single-team and mixed mentions retain their existing routes, and proxies still launch instances through run_team.

Organization cards add a collapsed-by-default “Team tasks for this request” disclosure. It shows private task titles, assigned experts, actual statuses and completed/total counts without estimating percentages. Each team board appears once, under its first visible organization row; multiple chapters or Owner aggregation do not duplicate it. Organization totals, acceptance checks and summary-row gating are unchanged. Private task completion does not imply organization acceptance. Existing polling and persisted snapshots refresh progress without resetting an open disclosure; page reload starts collapsed.

Before root creation, capture and persist the historical task IDs for each participating instance and the organization-specific Summary Team. Only tasks outside that baseline are shown, with reads scoped by session and team. A failed baseline is unavailable, never assumed empty. Refresh errors retain the previous snapshot with a warning, and observed tasks remain after board cleanup. Reads are time-bounded and do not change proxy run_team launch or scheduling. Historical cards without a baseline are not reconstructed by guessing task ownership. Single-expert, single-team and mixed mentions have no new disclosure.

After the Owner accepts an organization request, both the verified member notification and the current-turn system context include that exact plan’s original request and confirmed clarification answers. Later requests cannot replace this data. Before accepting the plan through `run_team`, a proxy may call only `run_team` or `read_file`. The code rejects repeated pre-start clarification before ask_user persists an interruption and instructs the proxy to join first. Owner clarification before task admission and business questions inside admitted instances retain their existing behavior. This gate applies only to organization turns, leaving single-expert, single-team, and mixed mentions unchanged.

For pure multi-team candidates, group_send_message requires an explicit organization_reply purpose. acknowledgement neither posts a message nor changes routing state; it returns guidance to continue with ask_user or run_team while the existing Owner bubble acknowledges receipt. Missing or invalid purposes are rejected without posting or ending the request. Only successful final_chat delivery completes ordinary conversation, and pending clarification questions still block it. Promises to ask or start work must not be declared final_chat. This field is interpreted only by organization-candidate admission; single experts, single teams, mixed mentions and execution results retain their existing calls. The model declares the purpose explicitly; this is not a semantic classifier and cannot guarantee that a model never mislabels an acknowledgement as final_chat.

The Worker checks abandoned organizations on startup and retries periodically. Unfinished organization rows and expert-task snapshots are closed only after verifying that the former execution is gone and no active call or live binding has taken over the same organization. Completed work is retained; unfinished rows on failed or expired cards are also closed. Interrupted cards explain the cause and ask the user to submit a new request. Cleanup failures remain retryable. Unknown ownership, unverified database configuration, or a live binding defer cleanup.

This recovery deletes only the specified organization’s public task-pool records and group registrations. It does not invoke runtime membership release, stop reused teams, or delete expert-team sessions, private task history, or artifact files. Memory already lost when the process exited is not automatically restored. Proxy-driven run_team startup and single-expert, single-team, and mixed-mention behavior remain unchanged.
