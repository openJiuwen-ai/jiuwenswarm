---
id: S_02_report-issue
title: Report a JiuwenSwarm bug from Web chat
type: S
author: hienchuphan@gmail.com
author_email: hienchuphan@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/auto_harness/issue_fix
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/common/rails/interrupt
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot
files:
  - jiuwenswarm/agents/harness/common/tools/report_issue.py
  - jiuwenswarm/agents/harness/common/tools/issue_report_template.py
  - jiuwenswarm/agents/harness/common/rails/interrupt/interrupt_helpers.py
  - jiuwenswarm/channels/web/frontend/src/types/websocket.ts
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot/IssueReviewPanel.tsx
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot/promptRouting.ts
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/InteractionSlot/InteractionSlot.css
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/agents/harness/common/auto_harness/issue_fix/gitcode_issue_client.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/agents/swarm/config_specs.py
  - jiuwenswarm/agents/swarm/providers/runtime_tools.py
  - jiuwenswarm/agents/swarm/registry.py
---

# Report a JiuwenSwarm bug from Web chat

`report_issue` is a server tool for interactive Web chat in the personal edition. Single agents, Team Work or Team Code leaders, and native JiuwenSwarm members of Web group chats can call it. The shared group policy mounts both reporting tools for leaders and teammates. It accepts a template ID, a one-line title, and structured form answers. The agent supplies the description, environment, reproduction steps, and expected and actual behavior. Additional required fields come from the current form. The server selects the fixed public GitCode repository `openJiuwen/jiuwenswarm` and reads the workspace's GitCode integration token.

The read-only `get_issue_template` tool fetches the project's localized Bug Report YAML from GitCode's default branch. For a transport failure, HTTP 429, or a server error, it reads the same localized path from the project's GitHub mirror on `develop`. It returns the source host, file path, content-derived template ID, title prefix, labels, required fields, and valid form choices. Identical contents at the same path produce the same template ID across hosts. `report_issue` independently reloads the same file and rejects a stale ID or invalid answers. The backend renders the form, sets its labels, and sends the redacted title, body, labels, and destination as `questions[].issue_report` on the addressed Web confirmation. The read-only Review issue panel displays the Markdown preview and offers the exact Markdown source. Publish and Cancel answer that same confirmation. It accepts a one-time approval addressed to that confirmation for the same draft. Rejection, invalid input, missing credentials, and a mismatched draft prevent submission. The submitted payload matches the reviewed draft. In group chat, the review is stored with the owning member and shown above the group composer. The group owner's decision resumes that member's exact interrupt.

A receipt keyed by the confirmation ID is claimed in the runtime state directory before the POST. Replaying that approval returns the recorded outcome or an unknown outcome while the first attempt is in flight. A successful result includes the issue number and link. An ambiguous result directs the user to check the tracker before another submission.
