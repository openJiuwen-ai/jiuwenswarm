## Scheduled tasks (Cron)

How to create and manage a simple scheduled job in JiuwenClaw and push results to a channel (e.g. web, Feishu).

---

### 1. What can cron jobs do?

- **Run a one-line instruction on a schedule**, e.g. “every morning at 9, summarize yesterday’s todos.”
- **Let the agent execute work** on a timer (search, daily report, etc.).
- **Multi-channel delivery** — results can go to web or Feishu (Feishu may need `chat_id`; see the Feishu section in [Channels](Channels.md)).

---

### 2. Create a job in the web UI

1. Open **Cron / Scheduled tasks**.
2. Click **New job** and fill in:

   - **name**: e.g. `daily_todo_summary`
   - **cron_expr**:  
     - Every day 09:00: `0 9 * * *`  
     - Minute 15 every hour: `15 * * * *`
   - **timezone**: often `Asia/Shanghai`
   - **targets**: `web` and/or `feishu`
   - **enabled**: checked (`true`)
   - **description**: natural language for what the agent should do at fire time, e.g. a short health reminder in Chinese or English.
   - **wake_offset_seconds** (optional): default `60` to wake the agent one minute early.

![](../assets/images/定时任务1.png)

3. Save. Jobs are stored in `~/.jiuwenclaw/workspace/cron_jobs.json` and picked up by the scheduler.

---

### 3. Common cron expressions

- **Daily 09:00**: `0 9 * * *`
- **Daily 18:30**: `30 18 * * *`
- **Monday 09:00**: `0 9 * * 1`
- **Every hour on the hour**: `0 * * * *`

Format: `minute hour day month weekday` (5 fields, space-separated),
or  `minute hour day month weekday second year` (7 fields, space-separated).
---

### 4. Create via chat (optional)

If the agent has `cron_create_job`, you can say things like:

> “Create a scheduled task to remind me to drink water every day at 9 on the web.”

The agent fills `cron_expr`, `description`, `targets`, etc., equivalent to using the form.

![](../assets/images/定时任务2.png)
