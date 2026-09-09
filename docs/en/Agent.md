# Agent

This guide explains what an **Agent** is in JiuwenSwarm, how it is structured, where files live on disk, and how to view or adjust configuration safely.

---

## Concepts

### What is an agent?

In JiuwenSwarm, an **Agent** is a digital assistant that can act on its own. It is not just a large language model—it is an execution entity built from several cooperating parts.

**Core definition:**

**Agent = identity + tools + skills + memory + workspace + config**

> **Note:** The above 6 items are the core components that make up a single agent. The workspace contains runtime data such as todos, sessions, and file storage. When multiple agents work together, they can form a cross-agent **collaboration capability** for task decomposition and parallel execution.

**How it differs from plain LLM chat:**

| Aspect | Plain LLM chat | JiuwenSwarm agent |
|--------|----------------|-------------------|
| Execution | Text replies only | Can call tools (files, shell, web search, etc.) |
| Memory | Short-term, within a session | Long-term across sessions; preferences and history |
| Skills | Fixed capability | Loadable skill modules for specialized work |
| Workspace | None | Dedicated workspace for tasks, todos, and sessions |
| Configuration | None | Independent config system for models, channels, permissions |
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
│  │  ┌─────────┐  ┌─────────────────────────────┐ │ │
│  │  │  Todo   │  │          Sessions            │ │ │
│  │  └─────────┘  └─────────────────────────────┘ │ │
│  └───────────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────────┐ │
│  │                Config                         │ │
│  └───────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Takeaways:**

1. **Identity** — who the agent is and how it communicates  
2. **Tools** — "hands" for files, search, code, shell, and media  
3. **Skills** — loadable modules (e.g. Git, document workflows)  
4. **Memory** — user profile, history, and decisions across sessions  
5. **Workspace** — the agent's "desk" for tasks, todos, and sessions  
6. **Config** — the agent's "settings panel" for models, channels, and permissions  

> This section is conceptual only. Later sections go into each part in more detail. See also: [Configuration](Configuration.md), [Memory](Memory.md), [Skill Self-Evolution](SkillSelfEvolution.md).

---

## Web frontend Agent page

In the web frontend, the **Agent** page is a **workspace file browser** for viewing the agent's workspace files and memory content.

![Agent Management page](../assets/images/current-ui-en/03-Agent-Management.png)

### Page features

| Feature | Description |
|---------|-------------|
| **Workspace browsing** | Browse the agent workspace directory structure, view files and directories |
| **File preview** | Preview the content of previewable files in the workspace |
| **Refresh** | Refresh the workspace file list |

### How to use

1. Click **Agent** in the left navigation bar
2. The left side of the page shows the workspace directory structure (e.g. `workspace/`)
3. Click a directory to expand and view the file list
4. Click a previewable file to display its content preview on the right

> **Tip:** The Agent page is mainly for viewing workspace files. To modify configuration, go to **More** → **Configuration**.

---

## Structure

### What an agent is made of

An agent consists of **6 core components** and **1 collaboration capability** (multi-agent collaboration). You can focus on the ones you care about.

**Overview:**

| Part | Type | Role | User focus | Main effect |
|------|------|------|------------|-------------|
| **Identity** | Core component | Who the agent is, tone, style | Customizable | Conversation style and behavior |
| **Workspace** | Core component | Tasks, todos, sessions, runtime data | Good to understand | Task tracking and persistence |
| **Tools** | Core component | Files, web, code, shell, media | Usually no edits | What operations are possible |
| **Skills** | Core component | Professional modules (Git, PPT, etc.) | Load as needed | Extra capabilities |
| **Memory** | Core component | Preferences, history, decisions | Mostly automatic | Continuity and personalization |
| **Config** | Core component | Models, channels, permissions | Advanced users | Model, security, channel behavior |
| **Multi-Agent Collaboration** | Collaboration capability | Supports team-based workflows for parallel operation | Enable as needed | Task decomposition and efficiency |

**Details:**

#### 1. Identity

Defines who the agent is and how it talks to you:

- Role (e.g. personal assistant, technical advisor)  
- Personality (concise vs. thorough)  
- Principles (e.g. try first, then ask; respect trust)  

**Files:** `agent/workspace/IDENTITY_ZH.md`, `agent/workspace/SOUL_ZH.md` (Chinese); `agent/workspace/IDENTITY_EN.md`, `agent/workspace/SOUL_EN.md` (English)

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

- `skill-creator` — skill creation assistant, helps generate new skills  
- `swarmskill-creator` — Swarm skill creator, supports multi-agent collaboration skills  
- `gitcode-api` — GitCode platform API operations  
- `project-maintainer` — project maintenance assistant, supports code review and version management  

**Location:** `skills/` directory

#### 5. Memory

Memory is organized into three files:

- **USER.md (user profile)** — Located at the workspace root; stores user identity, preferences, and habits
- **MEMORY.md (long-term memory)** — Under `memory/`; stores durable, reusable content like decisions and persistent facts
- **YYYY-MM-DD.md (daily log)** — Under `memory/daily_memory/`; archives daily runtime context and conversation snippets by date

**Note:** Memory is mostly automatic; you can search history when needed. See [Memory](Memory.md) for the full structure.

#### 6. Config

Controls runtime behavior:

- Model choice and parameters (temperature, timeout, etc.)  
- Channels (Feishu, WeChat, Telegram, etc.)  
- Permissions (what needs your approval)  
- Memory and logging  

**File:** `config/config.yaml`

> You do not need to edit everything by hand. In practice, focus on **identity** and **skills**; the rest is largely managed by the system.

#### 7. Multi-Agent Collaboration

JiuwenSwarm supports multi-agent collaboration through team-based workflows to handle complex tasks.

**Collaboration modes:**
- **Team mode**: A leader agent breaks down tasks, while multiple teammate agents execute subtasks  
- **Swarm mode**: Multiple agents work in parallel, with task distribution and result aggregation through skill orchestration  

**Features:**
- Automatic task decomposition: Complex tasks are split into executable subtasks by the leader  
- Parallel execution: Multiple agents work simultaneously for efficiency  
- Result aggregation: The leader collects and integrates results from teammates  
- Dynamic adjustment: Task assignments adapt based on execution progress  

**Configuration:** Team settings are configured under `modes.team.<team_name>` in `config/config.yaml`

> See team collaboration documentation for more details on multi-agent workflows.

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
│   ├── builtin_rules.yaml           # Built-in rules
│   ├── .env                         # Environment variables
│   └── runtime_state/               # Runtime state directory (per-session / per-task files)
│
├── agent/                           # Agent-related data
│   ├── sessions/                    # Session history storage
│   ├── home/                        # Agent home data
│   └── workspace/                   # Agent workspace
│       ├── AGENT_ZH.md              # Agent bootstrap config (Chinese)
│       ├── AGENT_EN.md              # Agent bootstrap config (English)
│       ├── IDENTITY_ZH.md           # Identity (Chinese)
│       ├── IDENTITY_EN.md           # Identity (English)
│       ├── SOUL_ZH.md               # Values and persona (Chinese)
│       ├── SOUL_EN.md               # Values and persona (English)
│       ├── USER.md                  # User profile and preferences
│       ├── memory/                  # Agent memory store
│       ├── todo/                    # Agent todo items storage
│       └── skills/                  # Skills
│
├── .agent_teams/                    # Team-related data (bindings.json, team.db, trio-count/, etc.)
├── .updates/                        # Version update cache
├── gateway/                         # Gateway data
├── logs/                            # Log files
├── memory/                          # Global memory store
├── received_files/                  # Incoming external files
├── web/                             # Web channel assets
└── .trace/                          # Trace data (created on demand)
```

**Key files:**

| Path | Purpose | Edit? | If you change it |
|------|---------|-------|------------------|
| `config/config.yaml` | Models, channels, permissions, memory | Advanced users, carefully | Affects models, channels, security; restart required |
| `config/builtin_rules.yaml` | Built-in rules | Not recommended | Changes default system behavior |
| `agent/sessions/` | Session history storage | Auto-managed by system | Affects session history; manage via Web UI |
| `agent/workspace/AGENT_ZH.md` | Bootstrap config (Chinese) | Yes, when needed | Affects startup behavior |
| `agent/workspace/IDENTITY_ZH.md` | Identity (Chinese) | Customizable | Affects how the agent sees its role |
| `agent/workspace/SOUL_ZH.md` | Values and persona (Chinese) | Customizable | Affects tone and style |
| `agent/workspace/USER.md` | User profile and preferences | Auto-managed by system | Affects personalization; update via agent conversation |
| `agent/workspace/skills/` | Skills | Add skills | Extends capabilities |
| `agent/workspace/memory/` | Memory store (MEMORY.md, daily_memory/) | Do not edit by hand | Risk of corrupting memory data |
| `agent/workspace/todo/` | Agent todo items storage | Auto-managed by system | Affects task tracking; manage via agent conversation |
| `todo/` | Global todo items storage | Auto-managed by system | Affects task tracking; manage via agent conversation |
| `logs/` | Logs | View only | Used for troubleshooting |

**Example (Windows):**

```text
C:\Users\Administrator\.jiuwenswarm\
├── config\config.yaml
├── config\builtin_rules.yaml
├── config\runtime_state\           # Runtime state directory (per-session / per-task files)
├── .agent_teams\                    # Team-related data
├── todo\                            # Global todo items
├── agent\
│   ├── sessions\                    # Session history
│   ├── home\                       # Agent home data
│   └── workspace\
│       ├── AGENT_ZH.md
│       ├── AGENT_EN.md
│       ├── IDENTITY_ZH.md
│       ├── IDENTITY_EN.md
│       ├── SOUL_ZH.md
│       ├── SOUL_EN.md
│       ├── USER.md
│       ├── memory\
│       ├── todo\                    # Agent todo items
│       └── skills\
├── .updates\                        # Version update cache
├── logs\                            # Log files
└── .trace\                          # Trace data (created on demand)
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
| `channels.*.enabled` | Channel on/off | Read-only OK |

**Category 2 — change with care**

| Key | Meaning | Effect | Suggestion |
|-----|---------|--------|------------|
| `models.defaults[0].model_client_config.model_name` | Default model | Quality and speed | Confirm the model works first |
| `models.defaults[0].model_config_obj.temperature` | Temperature | Creativity vs. stability | Often 0.7–1.0 |
| `permissions.tools.*` | Tool permissions | Safety | Understand risk before changing |

**Category 3 — avoid unless you know why**

| Key | Meaning | Risk | Suggestion |
|-----|---------|------|------------|
| `models.defaults[0].model_client_config.api_key` | API key | Leakage | Prefer environment variables |
| `memory.external.*` | External memory engine | Memory may break | Keep defaults |
| `gateway.*` | Gateway settings | Connectivity | Change only when deploying |
| `permissions.rules.*` | Security rules | Security holes | Keep defaults |

#### After you change config

**Restart is required for changes to take effect.**

Restart commands vary by installation method:

```bash
# Method 1: Windows service (installed via installer)
net stop jiuwenswarm
net start jiuwenswarm

# Method 2: Command line (manual startup)
# Stop the current terminal process, then:
jiuwenswarm-start

# Method 3: Python module (development)
# Stop the current process, then:
python -m jiuwenswarm.app

# Method 4: Container deployment (Docker/Kubernetes)
# Restart according to your container orchestration config:
docker restart jiuwenswarm-container
```

#### Common scenarios

**Scenario 1: Switch model**

```yaml
# In config.yaml
models:
  defaults:
    - model_client_config:
        api_base: https://api.example.com/v1
        api_key: your-api-key
        model_name: "your-model-name"  # e.g. deepseek-chat, gpt-4o
        client_provider: OpenAI
      model_config_obj:
        temperature: 0.95
      is_default: true
```

Restart the service.

**Scenario 2: Adjust reply style**

```yaml
models:
  defaults:
    - model_client_config:
        api_base: https://api.example.com/v1
        api_key: your-api-key
        model_name: your-model-name
        client_provider: OpenAI
      model_config_obj:
        temperature: 0.8   # more creative
        # temperature: 0.3  # more stable
      is_default: true
```

**Scenario 3: Enable or disable a channel**

```yaml
channels:
  feishu:
    apps:
      - name: Feishu default app
        enabled: true  # Enable the Feishu channel (enabled is inside the apps list element)
  telegram:
    enabled: false  # Disable the Telegram channel (telegram has enabled at the top level)
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
