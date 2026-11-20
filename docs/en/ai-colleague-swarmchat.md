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
