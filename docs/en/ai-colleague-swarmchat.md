# AI colleagues in SwarmChat

Choose an enabled colleague from the AI colleagues tab when creating a group or adding members.
Joining registers an identity. A public mention from a user, expert or colleague submits work to the colleague's existing scheduler.
The colleague posts its final reply under its group identity.
SwarmChat roster registration is independent of ordinary team size quotas. Internal expert team execution retains its quotas; colleague execution concurrency is managed by the colleague scheduler.

Each colleague keeps a separate continuous Session for each group. Recent context is provided automatically;
search_group_messages retrieves additional history, including messages from before joining and other authorized IM or SwarmChat groups.
Message searches require no sharing approval, while current membership and access boundaries remain enforced.
SwarmChat history is available without IM history configuration. IM searches prefer the current execution identity's history boundary.
Public group files can be searched and imported into the current Session. Editing a copy does not change the original.
File sharing and tool-operation permissions still apply.

Operations requiring confirmation appear as cards in the group conversation. Only final replies and explicitly published files enter group history.
Automatic colleague handoffs are limited to five per group since the latest user message. A new user message starts a new allowance.
Stopping a group also stops its colleague requests.

Membership, Sessions and pending replies survive service restart. Replies wait until the group runtime is ready again.
Development requires an agent-core containing HOST_MANAGED; updating the frontend alone does not enable this capability.

Colleagues can list authorized Feishu group members through a read-only tool without shell approvals. Approval cards show a redacted operation summary and apply only to the selected call.

## Auto reply

Auto reply is on by default in a group. When a message mentions nobody, a judge model decides which members should reply, and each selected expert, expert-team proxy or AI colleague replies under its own identity. A message with a mention skips the judgement and is always answered by the mentioned members.

The group settings panel on the right lets you change this:
- With auto reply off, an unmentioned message is only archived.
- The judge model follows the group session model by default. A small, fast model lowers cost and waiting time.

The outcome is shown under your message: deciding, which members were asked, no member needs to reply, or the decision failed. When it fails nobody replies; mention a member instead.
The judge model may ask several members at once, and they reply independently. When a request needs one joint result from several expert teams, the judge model can instead name one of them as the lead: only the lead is woken first, and it decides whether to answer directly, ask you a question, or start the joint work. Once it starts, the other teams join and a single summary is posted. The message then shows that the lead was asked to coordinate. To choose the lead yourself, mention the teams in order; the first one mentioned leads. Messages from members never trigger auto reply. Stopping the whole group also cancels auto replies that have not started.
