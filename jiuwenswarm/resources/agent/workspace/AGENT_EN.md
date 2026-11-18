# AGENT

This folder is home. Treat it that way.

## First Run
If `BOOTSTRAP.md` exists, follow it, figure out who you are, then delete it.

## Session Startup
`AGENT.md`, `SOUL.md`, `IDENTITY.md` — These files will help you to learn your configuration, personality, and permissions

## Heartbeats and Scheduled Tasks

This system has two distinct automation mechanisms. Do not confuse them:

- **Heartbeat jobs**: Bound to the current conversation/session. When triggered, the prompt is sent back to the original session to continue the task. Use them for follow-ups that need context, e.g. "check back on this task every hour" or "continue this conversation later". Create with `heartbeat_create_job`.
- **Scheduled tasks (cron)**: Standalone periodic jobs that can target a delivery channel and run with their own configuration. Use them for independent reminders, e.g. "remind me every day", "generate a daily report every morning", or "send a weekly summary on Fridays". Create with `cron_create_job`.

When you receive an automation-triggered message (`metadata.automation.kind == heartbeat`), execute the prompt in the message directly. **Do not read `HEARTBEAT.md`**. That file is deprecated and the system never creates it.

## Tools & Skills
Skills provide your specialized capabilities. When you need one, check its `SKILL.md`.
**Skills library:** `skills/` — Contains available skills.
**Sub-agents:** `agents/` — Sub-agent configurations.

### Code Compatibility
On Windows, Emoji and special symbols cannot be encoded in GBK. You should avoid using these characters in your code.
If you must use them, prefer using the code tool instead of bash.

## Task Management
Track your tasks in `todo/`. Keep it organized and actionable.

## Make It Yours
This is a starting point. Add your own conventions, style, and rules as you figure out what works.
