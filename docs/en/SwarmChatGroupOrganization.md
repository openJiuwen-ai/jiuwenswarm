# SwarmChat Group @ Expert Teams and Team Organization

> **Goal:** Describe how a SwarmChat group message that mentions two or more expert teams becomes one Team Organization, and how only the Owner posts the final result. The reader is a developer changing this path or matching it to runtime logs.
>
> **中文版：** [Chinese Version](../zh/SwarmChat群聊专家团组织.md)

---

## 1. What this path does

The SwarmChat group panel is `/groups` and `/groups/{groupId}`, rendered by `GroupChatWorkspace`. When the user mentions two or more expert teams, those teams share one request. They do not each open a private run and each post a group message.

The group message keeps its mentions, so the outer group team wakes the mentioned proxies through the existing path. Each proxy calls `run_team` once and launches the resident expert-team instance bound to that proxy. The first mentioned expert team is the Owner. The outer group team never joins the organization.

The panel shows group messages and single-team execution cards. It does not call `org.snapshot` and it does not mount an organization panel. The organization result appears after the Owner writes it into group history.

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
    participant Gate as Plan record
    participant Group as Outer group team
    participant Proxy as Group proxy
    participant Org as Expert-team instance
    participant Owner as Owner background

    User->>Gate: Group message with mentions
    alt At least two expert teams and no single expert
        Gate->>Gate: Record the plan; a new set uses the first team as Owner
    else A single expert is also mentioned
        Gate->>Gate: No plan; record a direct reply
    else Otherwise
        Gate->>Group: No plan; later calls use the single-team path
    end
    Group->>Proxy: Wake only the mentioned proxies
    Proxy->>Proxy: run_team
    alt This message is a direct reply and this proxy is still in a live organization
        Proxy-->>Proxy: Wait until that organization posts its result
        Owner->>User: Owner writes the expert-result group message
        Proxy->>User: Then the single-team path writes execution-result, without a new organization
    else This message is a direct reply
        Proxy->>User: Single-team finish writes execution-result, or a single expert replies itself
    else This proxy belongs to a live plan
        Proxy->>Org: Launch or reuse the instance and join the organization
        Proxy-->>Proxy: Return immediately that the organization is running
        Owner->>Org: Create the root and one child per member, then claim each child
        Org->>Org: The instance completes its organization task
        Owner->>User: Owner writes the expert-result group message
    else No live plan
        Proxy->>User: Single-team finish writes the execution-result group message
    end
```

### 3.1 How the frontend builds mentions

`selectedMentions` requires both a picker selection and `@display_name` in the text, followed by the end of the text, whitespace, or one of `，。！？、,:：;；!?`. Typing `@name` without the picker stores `mentions: []`.

A message with empty mentions is still stored. `_begin_team_round` returns immediately, so no proxy is woken. That is not a running task.

### 3.2 When a plan is recorded

A team round calls `record_organization_plan` only when the message has more than one mention. The plan is stored only when all of the following hold:

1. The session is a saved group.
2. The mentions contain at least two expert teams (`agent_group`) and no single expert (`agent_template`). A message that mentions an expert team and a single expert does not record a plan. It records a direct-reply mark instead. An expert team that is still in an organization waits until that organization posts its result, then uses the original single-team path. That direct reply does not open an organization and does not resume the organization instance. A single expert who is not in that organization may reply immediately.
3. Whether the previous Owner is mentioned does not matter. A follow-up or a queued round still records a plan.

Every multi-mention round creates a new organization. The first expert team mentioned in this message is the Owner; the previous round's Owner is not reused. If one of those instances is still in an earlier `pending` or `running` plan, the new plan waits until those plans finish. The plan id is the message `client_message_id`. Mentions and body are left unchanged.

### 3.3 How proxies wake

The message is delivered with its original mentions through agent-core `post_message`. Targets are mentioned members other than `user` whose role is not `passive_human`. The outer group Leader is not pulled into the organization by this message.

The woken model is the group proxy. After it calls `run_team`, the organization path returns as soon as the instance is ready. The model-facing text says the organization is already running, do not call `run_team` again, do not post a group message, and the Owner posts the final report.

## 4. The run_team fork

`RunTeam.invoke` handles `update` and `cancel` first. For `start` and `continue`, it takes the oldest message this proxy still owes. When that message is an organization that has not posted, this call joins it. When it is a direct reply and this proxy is still in a live organization, the call only returns a wait and does not start a single team. After that organization posts, the direct reply uses the ordinary single-team run. Any other message uses its own plan, or, when it has none, the oldest `pending` or `running` plan that includes this proxy.

A matching plan calls `run_organization_member` and does not create a `GroupExecutions` record. With no match, a managed group uses the existing single-team background run. `GroupExecutions.finish` then posts `execution-result-{execution_id}`. A single expert and a single expert team stay on that path. A direct reply does not resume the organization instance. While the organization is still running, the expert-team proxies it names still cannot post to the group. They may post for the direct reply only after that result is in the group. A single expert who is not in the organization is not held by that wait.

Overlapping `run_team` calls from one proxy share one instance-setup task. Each plan has one Owner background wait.

### 4.1 Why the organization call returns immediately

AbilityManager cuts a tool call off at 300 seconds by default, with a hard ceiling of 3600 seconds. An organization can take longer, so `run_team` cannot wait for completion inside the tool call.

After the instance is ready:

- A member proxy returns the in-progress result only.
- The Owner proxy also returns immediately. The wait and the group post run in `_owner_runs`.

While the Owner background is running, or this plan is still queued behind an older round, the plan is not marked `expired` just because it is older than 62 minutes. A later `run_team` stays on the organization path and returns immediately. If no Owner task is alive, no queue task is waiting, and the plan is older than 62 minutes, the plan becomes `expired`. A later `run_team` for that same multi-@ message does not open a single-team execution and does not post to the group. A different message that is not this plan still uses the single-team path or a follow-up. Those 62 minutes apply only to a plan with neither an Owner background nor a queue task. Once the Owner background is running, the result wait has no time limit. A queued plan stays until its turn.

## 5. Instances and organization tasks

The instance id is stable for a group session and `member_name`: `org-expert-` plus a 12-character digest. The same proxy reuses that Team, and the id does not change with the organization. Every multi-mention round creates a new organization id: `gorg-`, the sanitized session id, and an 8-character plan digest, at most 96 characters. Earlier rounds are never reused. That round's task pool stays isolated; expert-team instances and their conversation context are still reused. A plan saved before organization ids were recorded is treated as the older `gorg-` plus sanitized session id. Instances share the outer team's database with the group session, so group deletion can stop them the same way it stops a private run.

The Owner instance receives `complete_team_run` and a prompt that requires `org_*` coordination instead of an internal swarmflow for the cross-team request. Other instances receive the capability `group-member:{member_name}`. Reusing a live instance still applies this round's role: an Owner gains the submit tool, Owner prompt, and `group-coordination`; a member loses them. An existing instance is not the same as a round that is already configured.

The Owner background performs the mechanical setup itself:

1. Wait until every planned proxy binds its stable instance through `run_team`. The background does not launch a missing instance. While a proxy is still handling this multi-@ message, the wait continues past 120 seconds, with no further time limit. If that proxy's turn has already ended without `run_team`, or its status cannot be read, the round is `failed` at 120 seconds. The root is not created, and the Owner does not launch a replacement team from a template.
2. Create root `groot-{plan digest}` and claim it. That id comes from the plan, so recovering the same plan still recognizes the original root. The background does not choose the aggregation mode and does not start the root. If the user named `SUMMARY_TEAM` or `HIERARCHICAL`, the Owner model uses that mode. If the user named neither, the Owner model chooses from the task and does not default to Summary Team. The model then starts the root. Only a summary execution of this same root can keep the round waiting.
3. Register capability `group-member:{member_name}` for each mentioned expert team. The Owner instance publishes the child tasks. The background does not pre-create placeholder children.
4. If the root still has no child after 90 seconds, queue one reminder to the Owner instance. The reminder tells the model to select the mode first, choosing itself when the user named neither, then create one still-claimable child per mentioned expert in that same turn. Each child's `required_capabilities` is only `group-member:{member_name}`, so the matching team is woken and claims it with `org_claim_task`. Do not call `org_delegate_task` unless the user asked to delegate, and do not wait for one child to finish before creating the next. `SUMMARY_TEAM` finishes through the summary execution, and `HIERARCHICAL` has the Owner complete the root.

Expert work, review, summary, and final submission stay inside the instances. The Owner instance calls `complete_team_run`, which fills the same result slot the Owner background is waiting on. For `SUMMARY_TEAM` it also waits until the summary execution completes. For `HIERARCHICAL` the Owner completes the root and there is no separate summary execution. This wait has no time limit. The background posts only after those are done. The group message contains only the requested deliverable. It does not describe whether an instance was paused, rebuilt from a template, or reused an earlier draft. If the root completes and no submission arrives, it waits another 180 seconds and can use the root output.

## 6. The two group messages

| Source | Message id prefix | Sender | When |
| --- | --- | --- | --- |
| Organization completion | `expert-result-{plan_id}` | Owner proxy | After the Owner background confirms completion |
| Single-team completion | `execution-result-{execution_id}` | The proxy that ran it | When single-team `finish` succeeds |

During the organization the panel can stay empty. The organization path creates no execution card. A mentioned proxy that is not the Owner cannot post: `group_send_message` and `send_file` are rejected. That proxy's expert-team instance also loses those tools, so it cannot reply to the group. Only the Owner background posts `expert-result-` after the organization finishes. A message whose id starts with `execution-result-` came from the single-team auto-post, not from the Owner organization report.

On completion—success or failure—the background pauses the expert-team instances, then dissolves the organization on the SwarmChat side: release membership, erase this organization's task-pool rows (including task and output records), and stop only that organization's summary team. It does not call the runtime dissolve that would stop expert-team instances. Those instances and their conversation context stay. On success it posts `expert-result-` and marks the plan `done`. That post attaches only the Owner's final files: the `complete_team_run` attachments plus the file named by the root task `result_uri` (for example the summary team's report under `summary/`). Child-task outputs and drafts are not attached. Without a final file the post is text only. On failure it marks the plan `failed`, propagates the error, and does not post. The plan is also marked `failed` immediately when the Owner background is cancelled before it posts, when the Owner instance is not bound to the organization, or when the queue task exits with an error or is cancelled. A killed process cannot write that status; a plan with neither an Owner background nor a queue task still becomes `expired` after 62 minutes. The next multi-@ round always gets a new organization id; the Owner is the first @-mentioned expert for that round. Group deletion still uses the full dissolve and stops these instances.

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

A repeated `run_team` for the same user message joins that message's plan. If this proxy has not joined an older live plan yet, this `run_team` joins that older plan first and does not skip it just because a newer mention is already in the history. Recording a later multi-@ plan that shares a member with an unfinished plan schedules that plan's Owner immediately. A proxy that is not in the older plan is queued as well and does not bind instances that the older plan still holds. A failed early attempt leaves the plan queued. Once the older plan has been accepted, another multi-@ call also returns queued. The Owner background starts the new plan after the older one finishes. A single @ while an organization is still live stays on that organization path and does not open a private team. When that same multi-@ message's plan is already `failed`, `expired`, or `done`, this `run_team` stops: it does not join the organization, does not create a single-team execution, and does not post to the group. A different message that is not this plan still follows the single-team path, a follow-up, or a new multi-@ plan.

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

- A repeated `run_team` stays on the organization path while that run is live. After that same multi-@ message's plan has ended, another `run_team` does not open a single-team execution and does not post to the group. A different message still uses the single-team path or a follow-up.
- The Owner background waits until the round finishes or fails, with no time limit. On failure, cancellation, an unbound Owner instance, or a queue task that exits with an error or is cancelled, the plan becomes `failed` and no `expert-result-` message is written. A killed process cannot write that status; the plan still becomes `expired` after 62 minutes when neither an Owner background nor a queue task is alive.
- Member join keeps waiting while a proxy is still handling the message, with no further time limit. If that turn has ended without `run_team`, or the proxy status cannot be read, the round fails at 120 seconds instead of starting short-handed.
- The panel shows the organization execution card after the organization is created, with one row per expert team. The behavior is in the next section.

## 10. Organization execution card

Reuse the single-team `ExecutionCard`. Do not add a card component. One multi-expert request writes one execution record with `execution_kind: organization`. The card title is that record's `title`, and each row comes from `tasks` and `task_details`. The single-team create, `start_group_task`, `finish`, and internal `team.task` projection stay unchanged.

The organization record does not call `GroupExecutions.finish`, because that posts another `execution-result-` group message. The organization group message is still only the Owner background's `expert-result-{plan_id}`.

The card appears after the organization has been created, not when the plan is first recorded. The Owner proxy's `run_team` launches the Owner instance and completes `create_organization`; that step writes this one record with `presentation: card`, so the list and `group.execution.created` show it. The id is derived from the plan id. Entering the same organization again updates that card.

The title is the parent task: the user text stored on the plan. After root `groot-…` is created, store `root_task_id` on the card and do not add a row for it. Each row is one organization child the Owner published under that root. The row title is the child's own `title`, and the small text is the display name of the expert team that claimed it. A row does not appear before that child exists. Status is `pending` before claim, `in_progress` after claim, and `completed` after acceptance. Tasks inside an expert-team instance are not written into `tasks`. `SUMMARY_TEAM` adds a final summary row labeled Summary Team. Its title is that summary task's own `title`. It is `in_progress` while it runs and `completed` when it finishes, and the row appears only after the summary task exists, which is after every source child is accepted. `HIERARCHICAL` has no summary task, so the card does not add a summary row while any chapter is still unfinished. A child sent back for revision stays failed on its own row; an accepted repair task closes that chapter. After every chapter is accepted it adds the final row. The small text is the Owner expert team that completes the root, and the status follows the root. The title is the report heading the Owner stored on the root output. Until that heading exists, the row title is “汇总”. The report text stays off the card; after the Owner posts, View result still jumps to the group message. While every child is accepted and summary is still running, the card stays `running`.

Leave `member_name` empty and store `owner_member`. Single-team lookup and `team.task` attachment use `member_name`. An organization card that used the Owner's name would collide with the Owner's single-team execution, and internal tasks would be written into its rows.

Exceptions use the card's existing failed status. There is no new error-text area. A failed child row is `failed`. If Owner setup fails, the organization root fails, or the Owner wait is cancelled, the card is `failed` and every unfinished row is `failed`, so the existing failure icon and status label show it. An exception does not add a group message.

After the Owner posts, the card becomes `completed` and stores that message's `message_id`. View result stays the current in-page link. Besides matching the sender to `member_name`, also match a sender equal to `owner_member` when the message id equals the card's `message_id`.

The Owner background's existing poll reads these children from the organization task pool, writes the same execution record, and pushes `group.execution.updated`. Proxy `run_team` results do not update the card. `project_organization_progress` is not the entry point: it requires the root team to be the outer group team, while this root is the Owner instance.

---

## Back

- [Documentation home](../README_EN.md)
- [Project home](../../README.md)
