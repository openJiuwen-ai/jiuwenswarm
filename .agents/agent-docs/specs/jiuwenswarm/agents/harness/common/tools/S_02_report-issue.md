---
id: S_02_report-issue
title: Report a JiuwenSwarm bug from Web chat
type: S
author: hienchuphan@gmail.com
author_email: hienchuphan@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/auto_harness/issue_fix
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/agents/harness/common/tools/report_issue.py
  - jiuwenswarm/agents/harness/common/auto_harness/issue_fix/gitcode_issue_client.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
---

# Report a JiuwenSwarm bug from Web chat

`report_issue` is a server tool for interactive single-agent Web chat in the personal edition. It accepts a one-line title and a Markdown body. The agent supplies the environment, reproduction steps, expected behavior, actual behavior, and relevant redacted diagnostics. The server selects the fixed public GitCode repository `openJiuwen/jiuwenswarm` and reads the workspace's GitCode integration token.

The tool shows the redacted title, body, and destination in a confirmation card. It accepts a one-time approval addressed to that confirmation for the same draft. Rejection, invalid input, missing credentials, and a mismatched draft prevent submission. The submitted payload matches the reviewed draft.

A receipt keyed by the confirmation ID is claimed in the runtime state directory before the POST. Replaying that approval returns the recorded outcome or an unknown outcome while the first attempt is in flight. A successful result includes the issue number and link. An ambiguous result directs the user to check the tracker before another submission.
