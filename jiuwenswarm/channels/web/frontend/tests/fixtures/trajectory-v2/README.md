# Trajectory v2 replay vectors

Each file is one case the web trajectory viewer's reducer
(`jiuwenswarm/channels/web/frontend/src/features/trajectory/projector/trajectory-v2-reducer.ts`)
and `openjiuwen.agent_evolving.trajectory.windows` must replay identically.

- `records`: OTLP JSON spans, one v2 event each, in arbitrary order.
- `expected.windows`: per execution subject, every rebuilt window as its message ids.
- `expected.by_inference`: the window each inference span (a commit's `parentSpanId`) read.
- `expected.issue_codes`: the chain issues, in replay order. The viewer may add
  view-only diagnostics (`v2.checkpoint_recovery`, `v2.partial_window`) on top.
- `payloads_conform_to_schema: false` marks a vector whose payloads are
  deliberately malformed against `trajectory_v2_payloads.schema.json`.

The repositories share no root, so these files are copied into the viewer's
tests. A change to a v2 payload or to replay semantics updates both copies.
