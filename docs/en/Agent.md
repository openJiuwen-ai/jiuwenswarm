# Agent

This guide explains what an **Agent** is in JiuwenSwarm, how it is structured, where files live on disk, and how to view or adjust configuration safely.

---

## Concepts

### What is an agent?

In JiuwenSwarm, an **Agent** is a digital assistant that can act on its own. It is not just a large language model—it is an execution entity built from several cooperating parts.

**Core definition:**

**Agent = identity + tools + skills + memory + workspace**

**How it differs from plain LLM chat:**

| Aspect | Plain LLM chat | JiuwenSwarm agent |
|--------|----------------|-------------------|
| Execution | Text replies only | Can call tools (files, shell, web search, etc.) |
| Memory | Short-term, within a session | Long-term across sessions; preferences and history |
| Skills | Fixed capability | Loadable skill modules for specialized work |
| Workspace | None | Dedicated workspace for tasks, todos, and state |
| Personalization | None | Identity and config shape tone and behavior |

**How the pieces fit together:**

```text
┌─────────────────────────────────────────────────────┐
│                    Agent                             │
├─────────────────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ Identity│  │  Tools  │  │ Skills  │  │ Memory  │ │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘ │
│  ┌───────────────────────────────────────────────┐ │
│  │              Workspace                       │ │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────────────┐│ │
│  │  │  Todo   │  │ Config  │  │    Sessions     ││ │
│  │  └─────────┘  └─────────┘  └─────────────────┘│ │
│  └───────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Takeaways:**

1. **Identity** — who the agent is and how it communicates  
2. **Tools** — “hands” for files, search, code, shell, and media  
3. **Skills** — loadable modules (e.g. Git, document workflows)  
4. **Memory** — user profile, history, and decisions across sessions  
5. **Workspace** — the agent’s “desk” for tasks, config, and sessions  

> This section is conceptual only. Later sections go into each part in more detail.

---

## Structure

### What an agent is made of

An agent has six main areas. You can focus on the ones you care about.

**Overview:**

| Part | Role | User focus | Main effect |
|------|------|------------|-------------|
| **Identity** | Who the agent is, tone, style | Customizable | Conversation style and behavior |
| **Workspace** | Tasks, todos, sessions, runtime data | Good to understand | Task tracking and persistence |
| **Tools** | Files, web, code, shell, media | Usually no edits | What operations are possible |
| **Skills** | Professional modules (Git, PPT, etc.) | Load as needed | Extra capabilities |
| **Memory** | Preferences, history, decisions | Mostly automatic | Continuity and personalization |
| **Todo** | Task tracking | Day to day | Execution efficiency |
| **Config** | Models, channels, permissions | Advanced users | Model, security, channel behavior |

**Details:**

#### 1. Identity

Defines who the agent is and how it talks to you:

- Role (e.g. personal assistant, technical advisor)  
- Personality (concise vs. thorough)  
- Principles (e.g. try first, then ask; respect trust)  

**Files:** `IDENTITY.md`, `SOUL.md`

#### 2. Workspace

Runtime environment for:

- Current tasks and todos  
- Session history and state  
- Skills and local overrides  
- Temporary outputs  

**Location:** under the `.jiuwenswarm/` directory

#### 3. Tools

Built-in capabilities, including:

- Files: read, write, edit, search  
- Web: search, fetch pages  
- Code: Python, JavaScript  
- System: shell commands  
- Media: image OCR, audio transcription, video analysis  

**Note:** Tools are provided by the system; you normally do not change them manually.

#### 4. Skills

Loadable modules. Each skill typically defines goals, steps, tool usage, and output rules.

**Examples:**

- `gitcode-pr` — open a Pull Request on GitCode  
- `gitcode-pr-review-fix` — address PR review comments and update code  

**Location:** `skills/` directory

#### 5. Memory

Three kinds:

- **User profile** — who you are, preferences, habits  
- **Episodic** — events, decisions, conversation snippets  
- **Semantic** — background knowledge and concepts  

**Note:** Memory is mostly automatic; you can search history when needed.

#### 6. Config

Controls runtime behavior:

- Model choice and parameters (temperature, timeout, etc.)  
- Channels (Feishu, WeChat, Telegram, etc.)  
- Permissions (what needs your approval)  
- Memory and logging  

**File:** `config/config.yaml`

> You do not need to edit everything by hand. In practice, focus on **identity** and **skills**; the rest is largely managed by the system.

---

## Directory layout

### Local paths and key files

High-level layout under your user data directory:

**Overview:**

```text
C:\Users\<username>\.jiuwenswarm\
│
├── config/                          # Configuration
│   ├── config.yaml                  # Main config (models, channels, permissions)
│   └── builtin_rules.yaml           # Built-in rules
│
├── agent/                           # Agent-related data
│   └── <service_id>/                # Service instance
│       └── <agent_id>/              # Agent instance
│           ├── agent/               # Agent workspace
│           │   ├── AGENT.md         # Agent bootstrap config
│           │   ├── IDENTITY.md      # Identity
│           │   ├── SOUL.md          # Values and persona
│           │   ├── HEARTBEAT.md     # Heartbeat tasks
│           │   └── sessions/        # Session data
│           ├── config/              # Per-agent config overrides (optional)
│           ├── memory/              # Agent memory store
│           ├── skills/              # Skills
│           └── todo/                # Todos
│
├── gateway/                         # Gateway data
├── logs/                            # Log files
├── memory/                          # Global memory store
├── received_files/                  # Incoming external files
└── web/                             # Web channel assets
```

**Key files:**

| Path | Purpose | Edit? | If you change it |
|------|---------|-------|------------------|
| `config/config.yaml` | Models, channels, permissions, memory | Advanced users, carefully | Affects models, channels, security; restart required |
| `config/builtin_rules.yaml` | Built-in rules | Not recommended | Changes default system behavior |
| `agent/<id>/agent/AGENT.md` | Bootstrap config | Yes, when needed | Affects startup behavior |
| `agent/<id>/agent/IDENTITY.md` | Identity | Customizable | Affects how the agent sees its role |
| `agent/<id>/agent/SOUL.md` | Values and persona | Customizable | Affects tone and style |
| `agent/<id>/agent/HEARTBEAT.md` | Heartbeat tasks | Adjustable | Affects scheduled / proactive behavior |
| `agent/<id>/skills/` | Skills | Add skills | Extends capabilities |
| `agent/<id>/memory/` | Memory store | Do not edit by hand | Risk of corrupting memory data |
| `agent/<id>/todo/` | Todos | System-managed | Affects task tracking |
| `logs/` | Logs | View only | Used for troubleshooting |

**Example (Windows):**

```text
C:\Users\Administrator\.jiuwenswarm\
├── config\config.yaml
├── service_default_service_id\
│   └── agent_default_agent_id\
│       └── agent\
│           ├── AGENT.md
│           ├── IDENTITY.md
│           ├── SOUL.md
│           ├── skills\
│           └── sessions\
```

> **Notes:**  
> 1. Restart the service after changing config files.  
> 2. Do not hand-edit memory or session stores unless you know what you are doing.  
> 3. New skills must follow the skill format (see [Skills](Skills.md)).

---

## Operations

### Viewing and understanding agent configuration

How to inspect settings and what is safe to change.

#### View configuration

**Option 1: Ask the agent**

Examples:

- “Show me the current configuration.”  
- “Where is my agent config file?”  
- “Read config.yaml for me.”  

The agent can read and summarize the files.

**Option 2: Open the file directly**

Use an editor (VS Code, Notepad++, etc.):

```text
C:\Users\<username>\.jiuwenswarm\config\config.yaml
```

#### Risk levels

**Category 1 — safe to read**

| Key | Meaning | Suggestion |
|-----|---------|------------|
| `preferred_language` | Preferred language | Read-only OK |
| `logging.level` | Log level | Read-only OK |
| `heartbeat.every` | Heartbeat interval | Read-only OK |
| `channels.*.enabled` | Channel on/off | Read-only OK |

**Category 2 — change with care**

| Key | Meaning | Effect | Suggestion |
|-----|---------|--------|------------|
| `models.default.model_name` | Default model | Quality and speed | Confirm the model works first |
| `models.default.temperature` | Temperature | Creativity vs. stability | Often 0.7–1.0 |
| `heartbeat.active_hours` | Active window | When proactive runs fire | Match your schedule |
| `permissions.tools.*` | Tool permissions | Safety | Understand risk before changing |

**Category 3 — avoid unless you know why**

| Key | Meaning | Risk | Suggestion |
|-----|---------|------|------------|
| `models.default.api_key` | API key | Leakage | Prefer environment variables |
| `memory.external.*` | External memory engine | Memory may break | Keep defaults |
| `gateway.*` | Gateway settings | Connectivity | Change only when deploying |
| `permissions.rules.*` | Security rules | Security holes | Keep defaults |

#### After you change config

**Restart is required for changes to take effect.**

```bash
# Windows (depends on how you installed)
# If running as a service:
net stop jiuwenswarm
net start jiuwenswarm

# If running from the command line:
# Stop the current process, then:
jiuwenswarm-start
# or: python -m jiuwenswarm.app
```

#### Common scenarios

**Scenario 1: Switch model**

```yaml
# In config.yaml
models:
  default:
    model_client_config:
      model_name: "your-model-name"  # e.g. deepseek-chat, gpt-4o
```

Restart the service.

**Scenario 2: Adjust reply style**

```yaml
models:
  default:
    model_config_obj:
      temperature: 0.8   # more creative
      # temperature: 0.3  # more stable
```

**Scenario 3: Enable or disable a channel**

```yaml
channels:
  feishu:
    enabled: true
  telegram:
    enabled: false
```

#### Troubleshooting

If something breaks after a config change:

1. **Check logs** under `logs/`  
2. **Revert** the changed values  
3. **Restart** the service  
4. **Ask the agent** to help interpret errors  

> **Safety:**  
> - Back up `config.yaml` before editing.  
> - When unsure, ask the agent first.  
> - Put API keys in environment variables (`.env`), not plain text in YAML when possible.

---

*Simplified Chinese: [智能体](../zh/智能体.md)*
