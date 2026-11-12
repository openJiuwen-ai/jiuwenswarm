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
  - jiuwenswarm/agents/harness/common/tools/issue_report_template.py
  - jiuwenswarm/agents/harness/common/auto_harness/issue_fix/gitcode_issue_client.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
---

# Report a JiuwenSwarm bug from Web chat

Interactive single-agent Web chat in the personal edition exposes `get_issue_template` and `report_issue`. The template lookup returns the current Bug Report form and its identifier. The report tool validates structured fields against that form, renders the title, labels, and body, and reads the workspace's GitCode integration token. The destination is the public `openJiuwen/jiuwenswarm` repository on GitCode.

The existing Web confirmation card displays the redacted draft and its destination. Posting requires one-time approval for the same rendered draft and template. Rejection, missing credentials, invalid fields, and a changed template or draft prevent submission.

The tool claims each approval ID in a SQLite receipt before posting. Replay returns the recorded outcome or an unknown outcome if the attempt has no receipt yet. Success returns the issue number and link. An ambiguous outcome directs the user to check the tracker before retrying.
