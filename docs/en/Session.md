# Session

Sessions are the core mechanism in JiuwenSwarm for managing conversation history and context.

---

## Concepts

### What is a Session

A **Session** is a data unit in JiuwenSwarm used to record and manage single or multiple continuous conversation interactions. Each session contains:

- **Conversation History**: Complete message exchanges between user and AI
- **Context Information**: Current task status, execution progress, intermediate results, etc.
- **Metadata**: Session ID, creation time, last update time, and other identifiers

> **Tip**: Session and Memory are two different concepts:
> - **Session**: Temporary conversation history, disappears when closed or cleared
> - **Memory**: Persistently stored important information, retained across sessions

### Session File Structure

JiuwenSwarm session data is stored in the local workspace with the following structure:

```
.jiuwenswarm/
└── agent/
    └── sessions/
        ├── sess_19ddd41cbc0_fd1e4d/    # Regular session directory
        │   ├── metadata.json          # Session metadata
        │   └── history.json           # Conversation history
        ├── sess_19ddd5cc729_09bb02/   # Another session
        │   ├── metadata.json
        │   └── history.json
        ├── heartbeat_19de6f526fb_224694/  # Heartbeat session directory
        │   ├── metadata.json
        │   └── history.json
        └── ...
```

**Session Types:**

| Directory Prefix | Type | Description |
|------------------|------|-------------|
| `sess_` | Regular Session | Conversation sessions initiated via Web, Feishu, etc. |
| `heartbeat_` | Heartbeat Session | System heartbeat task sessions |
| `cron_` | Cron Session | Scheduled task sessions (`cron_id` is non-empty in `metadata.json`) |

**Session type distinction:**

- **Regular sessions**: `cron_id` is empty; retrieved via `project.get_sessions`
- **Cron sessions**: `cron_id` is non-empty (set to the `CronJob.id`); retrieved via `project.get_cron_sessions`, supports filtering by `cron_id` for all historical runs of a task
- The two types are mutually exclusive in project view; `project.list` `session_count` only counts regular sessions

**metadata.json File Content:**

![metadata.json Example](../assets/images/session/jiuwenclaw_session_history_metadata.png)

| Field | Description | Example |
|-------|-------------|---------|
| `session_id` | Unique session identifier | `sess_19ddd41cbc0_fd1e4d` |
| `channel_id` | Session source channel | `web`, `feishu`, `__heartbeat__` |
| `user_id` | User identifier | Empty or user ID |
| `created_at` | Session creation time (Unix timestamp) | `1716249600.732591` |
| `last_message_at` | Last message time | `1716253200.865117` |
| `title` | Session title (usually first message summary) | `Help me write a technical document` |
| `message_count` | Total message count | `15` |
| `mode` | Execution mode | `agent.plan` |
| `project_id` | Project ID the session belongs to | `proj_abc123` (empty string = default project) |
| `cron_id` | Source cron job ID | Empty for regular sessions; non-empty for cron-triggered sessions |

**history.json File Content:**

history.json is a JSON array that records complete conversation history. Each message contains the following fields:

![history.json Example](../assets/images/session/jiuwenclaw_session_history_json.png)

| Field | Description | Example |
|-------|-------------|---------|
| `id` | Unique message identifier, suffix `user` indicates user query, suffix `assistant` indicates agent response | `req_mol5noj7_22:user` |
| `role` | Message role | `user` or `assistant` |
| `request_id` | Request identifier | `req_mol5noj7_22` |
| `channel_id` | Message source channel | `web` |
| `timestamp` | Message timestamp | `1777533730.7309785` |
| `content` | Message content | User input or AI response text |
| `event_type` | Event type | `chat.delta`, `chat.final`, `chat.reasoning` |
| `tool_calls` | Tool call information (only in assistant messages) | Contains tool name, parameters, etc. |

> **Tip**: Agent response messages also contain `tool_calls` and other fields for recording tool call information.

### Why Sessions are Needed

The session mechanism plays an important role in JiuwenSwarm:

1. **Context Continuity**
   - Maintains conversation coherence, AI can understand previous exchanges
   - Supports information reference and supplementation in multi-turn conversations

2. **Task State Tracking**
   - Records task planning, execution progress, and intermediate results
   - Supports task interruption, resumption, and adjustment

3. **History Review**
   - Users can view previous conversation records
   - Facilitates review and problem-solving process tracing

4. **Resource Management**
   - Reasonably manages conversation context, avoids information overload
   - Supports context compression mechanism, optimizes token usage

### Session Use Cases

| Scenario | Description |
|----------|-------------|
| **Daily Conversation** | Q&A, consultation, content creation with AI |
| **Task Execution** | Execute complex tasks, track progress, adjust plans |
| **History Review** | View previous conversations, resume interrupted work |
| **Problem Investigation** | Trace issues through session records, analyze execution process |
| **Multi-task Switching** | Switch between different sessions, handle multiple independent tasks |
| **Cron Task Execution** | Scheduled tasks automatically trigger sessions, execute periodic work |
| **Multi-channel Conversation** | Conversations from different channels (Web, Feishu, WeChat, etc.) |

---

## Feature Demo

### View Session Chat History

You can view complete chat history of all sessions to understand past conversation content.

![Session List Example](../assets/images/session/jiuwenclaw_session_history_preview.png)

**Steps:**

1. **Via Web Interface**
   - In JiuwenSwarm Web interface, click "Sessions" in the left navigation
   - Enter session management page to see all session list (as displayed in frontend session management)
   - Click any session to view its complete chat history

2. **Via Local Files**
   - Navigate to session storage directory: `.jiuwenswarm/agent/sessions/`
   - Enter the corresponding session directory (e.g., `sess_19ddd41cbc0_fd1e4d/`)
   - View conversation content in `history.json` file

> **Tip**: The frontend page has a "Conversation Preview" toggle to switch between JSON text format or conversation page format, convenient for viewing raw data or conversation content.

### Restore Session

Restoring a session syncs historical session content to the frontend to continue previous work.

![Restore Session Example](../assets/images/session/jiuwenclaw_session_recovery_history_1.png)

![Restore Session Example 2](../assets/images/session/jiuwenclaw_session_recovery_history_2.png)

**Steps:**

1. Find the session to restore in the session management page (recommend selecting an actual chat history session, not a heartbeat session)
2. Click the "Restore" button or double-click the session entry
3. System will load all historical messages for that session
4. After restoration, you can see complete conversation history in the chat page
5. Continue entering new content, AI will respond based on historical context

**Use Cases:**

| Scenario | Description |
|----------|-------------|
| **Interruption Recovery** | Previous work was interrupted, need to continue |
| **Task Continuation** | Complex task completed in multiple sessions, continue from last progress |
| **Content Supplement** | Not satisfied with previous results, need to add requirements or adjust |

**Notes:**

- After restoring a session, new messages will be appended to the original history
- If the session contains incomplete tasks, the system will attempt to restore task state
- Long inactive sessions may have been compressed and need decompression when restored

### Delete Historical Session

If a session is no longer needed, you can delete it directly in session management to free storage and keep the list tidy.

![Delete Session Example](../assets/images/session/jiuwenclaw_session_history_delete.png)

**Steps:**

1. In JiuwenSwarm Web interface, click "Sessions" in the left navigation
2. Select the session to delete in the session management page
3. Click the delete icon in the right-side session detail area
4. In the popup confirmation dialog, click "Confirm"
5. After successful deletion, the session will be removed from the list

> **Tip**: Please make sure the selected session is no longer needed. Deleted sessions may not be recoverable.

---

## FAQ

### Q1: What's the difference between session and memory?

**Session** is temporary conversation history, storing all messages during the current conversation, disappears when cleared or closed. **Memory** is persistently stored important information like user preferences, key knowledge points, retained across sessions.

### Q2: How much storage space do sessions use?

Session data is usually small, each message is about a few KB. But if containing many file uploads or long conversations, a single session can reach several MB. It's recommended to periodically clean up unneeded historical sessions.

### Q3: Will previous task state be preserved after restoring a session?

Yes, when restoring a session, the system will attempt to restore previous task state including task list, execution progress, etc. However, some tasks that were being executed may need to be re-triggered.

### Q4: Where is session data stored?

Session data is stored in the `.jiuwenswarm/agent/sessions/` directory of the local workspace. Each session is an independent directory containing `metadata.json` and `history.json` files.

### Q5: How to backup important sessions?

You can:
1. Directly copy session JSON files to another location
2. Export session content as Markdown or text format
3. Write important information to memory for cross-session retention

---

## Related Links

- [Quick Start](Quickstart.md) - Learn JiuwenSwarm basics
- [Memory System](Memory.md) - Learn about persistent memory mechanism
- [Task Planning](TaskPlanning.md) - Learn about task management mechanism
- [Page Overview](Page-Overview.md) - Learn about interface layout
- [Agent Tutorial](Agent.md) - Learn about conversation features

---

*Document Version: v1.0*  
*Target Audience: JiuwenSwarm Users*  
*Last Updated: 2026-05-05*