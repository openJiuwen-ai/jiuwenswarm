# Scheduled Tasks (Cron)

Scheduled tasks (Cron) are an essential mechanism for automation in JiuwenSwarm.

---

## Concepts

### What are Scheduled Tasks

A **Scheduled Task (Cron Job)** is a mechanism that automatically executes tasks according to a predefined schedule. In JiuwenSwarm, scheduled tasks allow the Agent to automatically perform specific operations at specified times and push results to designated channels.

**Core Capabilities:**

| Capability | Description |
|------------|-------------|
| **Scheduled Execution** | Automatically triggered based on Cron expression |
| **Natural Language Description** | Describe tasks in natural language, Agent understands and executes |
| **Channel Delivery** | Results can be pushed to a designated channel (Web, Feishu, WeChat, etc.) |
| **Auto Wake-up** | Wake up Agent in advance to ensure timely execution |
| **Team/SwarmFlow** | Support multi-agent collaboration for complex tasks (see [Team mode and SwarmFlow](#team-mode-and-swarmflow-multi-agent-scheduled-jobs)) |

**Typical Use Cases:**

- 📅 **Daily Reminders**: Send work reminders, health reminders at fixed times
- 📊 **Periodic Summaries**: Regularly summarize data, generate daily/weekly reports
- 🔄 **Scheduled Checks**: Periodically check system status, monitor task progress
- 📢 **Message Push**: Send messages or notifications to specific channels on schedule
- 🤝 **Collaborative Tasks**: Team mode multi-agent collaboration for complex analysis

### Cron Expression Syntax

Cron expressions are the standard format for defining when scheduled tasks execute.

**Basic Format:**

```
minute hour day month weekday
```

| Field | Range | Description |
|-------|-------|-------------|
| minute | 0-59 | Minute |
| hour | 0-23 | Hour |
| day | 1-31 | Day of month |
| month | 1-12 | Month |
| weekday | 0-6 | Day of week (0=Sunday) |

**Special Characters:**

| Character | Description | Example |
|-----------|-------------|---------|
| `*` | Any value | `* * * * *` = every minute |
| `,` | Multiple values | `0,30 * * * *` = 0th and 30th minute of every hour |
| `-` | Range | `0 9-17 * * *` = every hour from 9am to 5pm |
| `/` | Interval | `*/15 * * * *` = every 15 minutes |

**Common Expression Examples:**

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Every day at 9:00 AM |
| `30 18 * * *` | Every day at 6:30 PM |
| `0 9 * * 1` | Every Monday at 9:00 AM |
| `0 9,18 * * *` | Every day at 9:00 AM and 6:00 PM |
| `*/30 * * * *` | Every 30 minutes |
| `0 0 * * *` | Every day at midnight |

## Quick Start

### Create via Web Interface

**Steps:**

1. Open JiuwenSwarm Web interface, click **Work** in the left navigation
2. In the sub-panel on the left side of the Work page, click **Scheduled Tasks**
3. Enter the scheduled tasks page, click **Create** button
4. Fill in the task configuration form:

![Scheduled Tasks Page](../assets/images/current-ui-en/10-Scheduled-Tasks.png)

| Field | Description | Example |
|-------|-------------|---------|
| **Task Name** | Task name (user-readable identifier; system assigns a separate `job_id`) | `daily_reminder` |
| **Cron Expression** | Cron expression | `0 9 * * *` (every day at 9am) |
| **Timezone** | Task execution timezone | `Asia/Shanghai` (default) |
| **Status** | Task status | Enable/Disable |
| **Description** | Task content description | Generate today's work reminder |
| **Wake Offset Seconds** | Wake-up advance seconds, default 0 | `0` (default, no advance wake-up) |
| **Timeout Seconds** | Execution timeout (60-259200), default 3600 (1 hour) for both normal and team modes | `3600` (default) |
| **Delete After Run** | Auto-delete after one execution, default false | `false` (default) |
| **Delivery Channel** | Result delivery channel (single channel ID) | `tui`, `web`, `feishu`, `wechat`, `wecom`, `whatsapp`, `xiaoyi`, `dingtalk` |
| **Execution Mode** | Agent execution mode | `agent.fast` (default) |
| **Project Directory** | Project working directory (absolute path) | `/home/user/my-project`; defaults to current session's project |

> **Timeout Note**: Both normal modes like `agent.fast` and Team modes like `team`/`team.plan`/`code.team` default to 3600 seconds (1 hour). If you need a longer execution time, you can set it when creating.

5. Click **Create**, the task will take effect automatically

**Project归属:**

The task is automatically assigned to the project matching `project_dir` (falls back to the default project if no visible project matches). You can then manage cron jobs and their execution sessions by project in the project view.

**Storage Location:**

Scheduled task configurations are saved at:
```
~/.jiuwenswarm/agent/home/cron_jobs.json
```

### Create via Chat

When the Agent has the `cron_create_job` tool capability, you can create scheduled tasks directly through natural language conversation.

> **Permission Configuration**: You need to set the related cron tools to `allow` in `config.yaml`'s `permissions.tools` to operate scheduled tasks via chat.

**Example Conversation:**

```
User: Create a scheduled task to remind me to drink water every morning at 9.
```

The Agent will automatically:
1. Parse time intent → `cron_expr: "0 9 * * *"`
2. Understand task content → `description: "remind to drink water"`
3. Determine delivery channel → `targets: "web"`
4. Call the tool to create the task

> **Tip**: When creating tasks via chat, the Agent automatically infers reasonable defaults based on context, such as timezone and channel.

---

## Scheduled Task Execution

### Execution Process

When a scheduled task triggers at the fixed time, a running conversation will appear on the chat page.

**Execution Flow:**

1. **Trigger Time**: Reaches the time point defined by Cron expression
2. **Agent Wake-up**: Agent is woken up based on `wake_offset_seconds` advance
3. **Task Execution**: Agent starts executing the content described in the task
4. **Result Delivery**: After execution completes, results are pushed to the designated channel

**Execution Status:**

| Status | Description |
|--------|-------------|
| **Pending** | Task created, waiting for trigger time |
| **Running** | Agent is executing the task |
| **Completed** | Task execution finished, results pushed |
| **Failed** | Error occurred during task execution |

### View Execution Results

The execution results of scheduled tasks can be viewed on the chat page.

---

## Scheduled Task Management

After creation, tasks can be managed on the frontend page with the following operations:

| Operation | Description |
|-----------|-------------|
| **Run Now** | Manually trigger the task; returns `{accepted, run_id, session_id}` so you can jump to the execution session |
| **Preview** | View task details (includes `project_id` and `last_session_id`) |
| **Disable** | Pause the task, no longer auto-trigger |
| **Update** | Modify task config (including `project_dir` to reassign project) |
| **Delete** | Delete the task |

### Bidirectional Task–Session Linking

Each cron execution creates a session. The following fields link tasks and sessions in both directions:

| Field | Location | Description |
|-------|----------|-------------|
| `project_id` | CronJob | Project ID the task belongs to |
| `last_session_id` | CronJob | Session ID of the most recent execution (`null` if never run) |
| `cron_id` | SessionInfo | Source cron job ID (empty for regular sessions) |

- Task → session: use `last_session_id` from `cron.job.get`
- Session → task: filter by `cron_id` via `project.get_cron_sessions`
- Project view: `project.get_sessions` (regular) + `project.get_cron_sessions` (cron) are mutually exclusive

---

## Practical Examples

### Daily Work Reminder

**Scenario:** Automatically generate a work reminder every morning at 9 AM and push to the Web interface.

**Configuration Steps:**

1. Create a scheduled task, enter in the chat:
```
Every morning at 9 AM, automatically generate today's work reminder, including: 1) Todo list 2) Important schedule 3) Weather info
```

![Work Reminder Scheduled Task Execution 1](../assets/images/cron/schedule_task_demo_daily_work_notice_1.png)

2. After successful creation, the Agent will automatically execute and push results at 9:00 AM every day

**Data Source Explanation:** Todos, schedules, etc. are read by the Agent from `agent/workspace/memory/` memory files (written during daily conversations); weather info is obtained via search tools. Content not in memory typically won't appear in the reminder.

**Execution Result:**

![Work Reminder Scheduled Task Execution 2](../assets/images/cron/schedule_task_demo_daily_work_notice_2.png)

```
📋 Today's Work Reminder (May 20, 2026, Wednesday)

1️⃣ Todo List
Currently no pending todos.

2️⃣ Important Schedule
No important schedule for today.

3️⃣ Weather Info
Today's weather:

- 🌫️ Condition: Cloudy
- 🌡️ Temperature: 16°C ~ 25°C
- 💨 Wind: East wind, light breeze
- 📍 Location: Beijing

Three-day Forecast:

- May 21 (Thu): Cloudy to light rain, 16°C ~ 24°C, south wind light breeze
- May 22 (Fri): Moderate rain to cloudy, 17°C ~ 23°C, south wind light breeze
- May 23 (Sat): Cloudy, 19°C ~ 28°C, southwest wind light breeze

Tips:

- Today is cloudy with moderate temperature, recommend wearing a light jacket
- Rain expected tomorrow and the day after, please prepare rain gear in advance
- Weekend weather will improve, suitable for outdoor activities
- Wish you a productive day! If you have new todos or schedule, feel free to tell me.
```

---

## FAQ

### Q1: What if the scheduled task doesn't execute on time?

**Troubleshooting Steps:**

1. Check if the task is enabled (`enabled: true`)
2. Verify the Cron expression is correct
3. Check if the timezone setting is correct
4. Ensure JiuwenSwarm service is running
5. Check log files for any errors

### Q2: How to modify an existing scheduled task?

In the scheduled task list on the Web interface, click the **"Edit"** button on the right side of the task, modify the configuration and save. The scheduler will automatically update after modification.

### Q3: Are scheduled task results saved?

Scheduled task execution results are:
- Pushed to the designated channel (Web/Feishu, etc.)
- Saved as session history in corresponding session files
- Viewable through session management

### Q4: What does wake_offset_seconds do?

`wake_offset_seconds` defines how early to wake up the Agent (default `0`, i.e. no advance wake-up). For example, when set to `300` (5 minutes):
- Task scheduled for 9:00 AM
- `wake_offset_seconds: 300` (5 minutes)
- Agent starts preparing at 8:55 AM to ensure execution at 9:00 AM sharp

### Q5: Which delivery channels are supported?

Currently supported channels:
- `tui` - TUI terminal (broadcast to all connected windows)
- `web` - Web interface
- `feishu` - Feishu
- `wechat` - WeChat
- `wecom` - WeCom (Enterprise WeChat)
- `whatsapp` - WhatsApp
- `xiaoyi` - Xiaoyi
- `dingtalk` - DingTalk

---

## Pushing to TUI Channel

When `targets=tui`, scheduled task results are pushed to all connected TUI windows.

**Notes:**

- When using `targets=tui`, please keep the TUI online, otherwise you may not receive the execution results
- It is recommended to set additional push channels (like `web` or IM) as a backup
- You can view task configuration via `/cron show <job_id>`, or check historical results through the Web interface

---

## Team mode and SwarmFlow (multi-agent scheduled jobs)

Besides the default single-agent path, cron jobs now support **Team mode**: at wake time the gateway starts multi-agent collaboration and may run a **SwarmFlow** workflow (see [TUI SwarmFlow Guide](TUISwarmFlowGuide.md); for Team mode basics, see [Agent Team Guide](AgentTeam.md)).

#### Supported execution modes (`mode`)

| `mode` | Description |
|---|---|
| `agent.fast` | **Default**. Single agent, fast path; good for reminders and simple queries |
| `agent` / `agent.plan` / `plan` | Single agent with planning or deeper reasoning |
| `team` | Multi-agent team; may use SwarmFlow |
| `team.plan` | Team with planning-oriented collaboration |
| `code.team` | Code-oriented team collaboration |

When creating jobs from TUI/Web, pass `mode=`. The UI loads supported modes and default timeouts via `cron.job.meta`.

#### Examples

```text
# Weekly team report pushed to TUI
/cron add name=model-weekly cron_expr="0 9 * * 1" description="Compare GLM vs DeepSeek and output a Markdown report" mode=team targets=tui

# Simple reminder with default agent.fast
# 5-field: minute hour day month dow
/cron add name=water cron_expr="30 8 * * *" description="Remind me to drink water" targets=tui
```

### Timeout Settings

| Mode | Default Timeout |
|---|---|
| Normal modes (e.g. `agent.fast`) | 3600 s (1 hour) |
| `team` / `team.plan` / `code.team` | 3600 s (1 hour) |

If you need a longer execution time, you can specify it when creating:
```text
/cron add name=long-report cron_expr="0 9 * * 1" description="..." mode=team timeout_seconds=3600 targets=tui
```

### Execution and delivery

**Execution mode**

- **Normal mode**: Suitable for simple tasks, returns results quickly
- **Team mode**: Suitable for complex tasks, supports multi-agent collaboration

**Why use independent sessions**

- The execution process will not be displayed in real-time on the interface that created the task
- Both modes will push the final results to the specified channel

**Result push**

| Scenario | Push content |
|----------|--------------|
| Normal completion | Agent's response |
| Execution failure | Error message starting with `[cron]` |

**More information**

- Learn about Team collaboration: [Agent Team Guide](AgentTeam.md)
- Learn about SwarmFlow: [SwarmFlow Guide](TUISwarmFlowGuide.md)

---

## Related Links

- [Channels](Channels.md) - Configure message delivery channels
- [Task Planning](TaskPlanning.md) - Learn about Agent dynamic task decomposition
- [Agent Tutorial](Agent.md) - Learn about conversation features

---

*Document Version: v1.0*  
*Target Audience: JiuwenSwarm Users*  
*Last Updated: 2026-05-05*
