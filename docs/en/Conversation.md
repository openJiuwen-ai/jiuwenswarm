# Conversation

Conversation is the most commonly used entry point in JiuwenSwarm. It is not just chat, but the primary interface for receiving and executing tasks. Here, you can ask questions, assign tasks, add requirements, and review both progress and outcomes.

---

## 1. Getting Started

### 1.1 Conversation Overview

**Positioning of the Conversation Page**

The conversation page is JiuwenSwarm's core interaction interface. It is not only a chat window, but a **workbench for task intake and execution**. Through conversation, you can:

- **Ask questions**: Obtain information and clarifications
- **Assign tasks**: Let the agent execute concrete operations
- **Add requirements**: Adjust scope dynamically during execution
- **Track progress**: See task status and outcomes in real time

![Conversation page overview](../assets/images/conversation/chat_page_overview.png)

**Core Capabilities**

| Capability | Description |
|:---|:---|
| **Natural Language Understanding** | Describe needs in plain language without memorizing complex commands |
| **Task Planning** | Automatically break complex requests into executable subtask sequences |
| **Dynamic Adjustment** | Insert new requirements or modify plans at any point during execution |
| **Tool Orchestration** | Automatically choose and combine suitable tools to complete tasks |
| **Result Feedback** | Clearly present the execution process and final results |

---

### 1.2 How to Start a Conversation

**Basic Workflow**

1. **Describe your request**: Enter your requirement or task in the input box
2. **Send the message**: Click send or press Enter
3. **Review the response**: The agent interprets your request and starts execution
4. **Continue refining**: Add follow-up requirements or adjust direction based on progress

![Conversation operation flow](../assets/images/conversation/chat_operation_flow.png)

**Tips for Better Prompts**

For better execution quality, include the following elements when describing requirements:

| Element | Description | Example |
|:---|:---|:---|
| **Goal** | What you want to achieve | "Generate API documentation" |
| **Scope** | Where the request applies | "For all interfaces in the `src/api` directory" |
| **Constraints** | Special requirements or limits | "Use Chinese and output in Markdown format" |
| **Output format** | Expected output form | "Write to `docs/api.md`" |

**Prompt Quality Comparison**

| Recommended ✅ | Too vague ❌ |
|:---|:---|
| "Help me organize all Markdown files in `docs/zh`, verify heading hierarchy, fix formatting issues, and produce a change checklist." | "Organize docs" |
| "Analyze the code structure in `src/core`, generate an architecture diagram as PNG, and save it to `docs/architecture.png`." | "Analyze code" |
| "Translate `README.md` into English while preserving the original format, and write to `README_EN.md`." | "Translate README" |

**First-time Usage Suggestions**

- Start with **small tasks with clear goals** to quickly understand agent capabilities
- If the first result is not ideal, **continue adding requirements** instead of starting over
- Use **follow-up questions and refinements** to improve execution quality

---

### 1.3 Common Operations During Conversation

During task execution, you can perform the following operations at any time:

**Dynamic Adjustment Actions**

| Action | Description | Typical scenario |
|:---|:---|:---|
| **Add requirements** | Append details or change direction on the current running task | Missing details, need to adjust output format or constraints |
| **Insert new task** | Add a brand-new independent task into the queue | A higher-priority independent need appears |
| **Interrupt task** | Stop a task currently in progress | Need to halt or change direction |
| **Adjust priority** | Reorder pending tasks | Re-prioritize execution sequence |
| **End task** | Mark a task completed or cancelled | Task is no longer needed |
| **View status** | Check current task list and progress | Understand overall progress |

> **Add requirements vs Insert new task**: "Add requirements" refines or extends an **existing task** (e.g., "also add charts to the report"), while "Insert new task" adds a **brand-new independent task** (e.g., "also translate another document for me").

**Core Feature: Adjust While Chatting**

JiuwenSwarm supports **real-time dynamic adjustment**. When you add new requirements while tasks are running, the system will:

1. **Understand the new requirement**: Parse your newly added input
2. **Assess impact**: Evaluate effects on current tasks
3. **Re-plan**: Integrate new requirements into the task queue
4. **Continue execution**: Proceed with the updated plan

**Example: Full Dynamic Adjustment Walkthrough**

The following example chains common dynamic adjustment operations, showing the complete process from task planning to execution, adjustment, and review:

**Step 1: Assign a task**

```
User: Process December invoice data and generate a summary report. Start by creating a todo list.

Agent: Task list:
  [1]	Get December invoice data file	🔄 In progress
  [2]	Read and parse invoice data	⏳ Pending
  [3]	Data cleaning and validation	⏳ Pending
  [4]	Generate summary report	⏳ Pending
  [5]	Output report file	⏳ Pending

▶ Start task [1]...
```

![Conversation page after assigning task](../assets/images/conversation/dynamic_insert_task.png)

**Step 2: Insert a new task**

```
User: Also process January invoices, merge both months, and email finance.

Agent: Task list updated:
  [1]	Get December and January invoice data files	🔄 In progress
  [2]	Read and parse December and January invoice data	⏳ Pending
  [3]	Data cleaning and validation (Dec + Jan)	⏳ Pending
  [4]	Merge data and generate summary report	⏳ Pending
  [5]	Export report file and email finance	⏳ Pending

▶ Continue execution...
```

![Conversation page after inserting new task](../assets/images/conversation/dynamic_insert_new_task.png)



---

## 2. Execution Modes

### 2.1 Mode Overview

JiuwenSwarm supports multiple execution modes. Different modes fit different scenarios, and you can choose based on task characteristics.

![Conversation page execution modes](../assets/images/conversation/chat_execution_modes.png)

**Mode Comparison**

| Mode | How it runs | Best for | Characteristics |
|:---|:---|:---|:---|
| **Task Planning** | Decomposes requirements into concrete steps and executes by plan | Complex and multi-step tasks requiring progress tracking | Clear and controllable process; supports dynamic adjustment |
| **Performance Mode** | Handles requests flexibly and supports parallel tasks | Clear goals where fast results are preferred | Fast response; efficiency-first |
| **Cluster Mode** | Multi-agent collaborative execution | Large-scale tasks requiring specialized division of labor | Complementary capabilities; collaborative processing |

**Mode Switching**

You can switch execution modes in the following ways:

- **UI switch**: Select the execution mode at the bottom of the conversation page
- **Command switch**: Use `/mode` or `/switch` (see [CLI Commands](#3-cli-commands))

---

### 2.2 Task Planning Mode

#### 2.2.1 Concept Overview

**What is task planning?**

Task planning is one of JiuwenSwarm's core capabilities. It gives the agent **structured task decomposition and dynamic management**. For complex or multi-step requests, the agent automatically parses them into executable subtasks and systematically records/tracks them through built-in todo tools.

**Core Value**

| Capability | Description |
|:---|:---|
| **Dynamic decomposition** | Automatically splits complex requests into executable subtask sequences |
| **Real-time tracking** | Updates subtask status as each step completes, making progress visible and controllable |
| **Flexible intervention** | Supports adding requirements or urgent insertions mid-execution without breaking the overall flow |
| **Goal preservation** | Reduces goal drift and execution gaps in long-running tasks |

**Applicable Scenarios**

- Tasks with many steps that need phased completion
- Tasks likely to change during execution
- Tasks where process transparency is important
- Tasks requiring confirmation at each stage

![Conversation page after assigning task](../assets/images/conversation/dynamic_insert_task.png)

---

### 2.3 Performance Mode

#### 2.3.1 Concept Overview

**What is performance mode?**

Performance Mode (Fast Mode) is optimized for simple and clear tasks. In this mode, the agent will:

- **Respond quickly**: Reduce planning overhead and execute directly
- **Process in parallel**: Run multiple independent tasks at the same time
- **Prioritize efficiency**: Complete work as fast as possible

**Applicable scenarios**

- Tasks with clear goals and straightforward steps
- Tasks where fast results are preferred
- Tasks that do not require detailed process decomposition
- Multiple simple tasks that need batch processing

#### 2.3.2 Practical Example

**Case: Batch document rewriting**

```
User: Rewrite the following two technical docs so they are easier for beginners:
1. docs/api.md
2. docs/architecture.md

Agent: 🔍 Processing docs in parallel...

  Rewriting completed! Both beginner-friendly versions are saved.

  Original file                 Beginner version
  ContextCompression.md         ContextCompression_Beginner.md

  🎉 Both beginner-friendly docs are saved in the same directories as the originals. Want me to refine anything else?
```

![Performance mode diagram](../assets/images/conversation/performance_mode_demo.png)

---

### 2.4 Cluster Mode

#### 2.4.1 Concept Overview

**What is cluster mode?**

Cluster Mode (Team Mode) is JiuwenSwarm's multi-agent collaboration mode. Multiple specialized agents work together, each owning a focused domain, to complete complex tasks.

**Core characteristics**

- **Specialized division of labor**: Different agents handle different domains
- **Collaborative execution**: Agents communicate and coordinate
- **Complementary capabilities**: Combine multiple specialized strengths
- **Result integration**: Aggregate outputs from all agents

**Applicable scenarios**

- Large-scale, cross-domain complex tasks
- Tasks requiring multiple professional skill sets
- Tasks difficult for a single agent to complete alone

#### 2.4.2 Practical Example

**Case: Full-stack project development**

```
User: Build a user management system including frontend UI, backend APIs, and database design.

Agent: 🤖 Starting cluster mode and assigning work:
[team_leader] I can help build a user management system. Before we start, I need to confirm several key details so the solution matches your requirements...

User: Use the default approach.

Agent:
[team_leader] ✅ Team formed and started!
[frontend-dev] Received! I have reviewed the task board.
[backend-dev] Project kickoff received!
[qa-engineer] Project kickoff received!
```

The screenshots below are ordered roughly as the conversation unfolds.

1. **Requirements alignment**  
   Conversation UI where the Team Leader confirms scope and key details with the user before cluster startup.

![Cluster mode diagram 1](../assets/images/conversation/chat_cluster_mode_1.png)

2. **Pickup and parallel start**  
   Conversation and status UI when specialized agents pick up subtasks and start work in parallel.

![Cluster mode diagram 2](../assets/images/conversation/chat_cluster_mode_2.png)

3. **Collaboration in progress**  
   Conversation UI as multiple agents continue coordinated execution (exact layout depends on the product).

![Cluster mode diagram 3](../assets/images/conversation/chat_cluster_mode_3.png)

4. **Workspace output paths**  
   Example directory layout and important file paths under `workspace` after the task produces outputs.

![Cluster mode result example: workspace file paths](../assets/images/conversation/cluster_mode_sample_workspace_paths.png)

5. **Frontend showcase**  
   Example frontend pages of the output user management system.

![Cluster mode result example: frontend UI](../assets/images/conversation/cluster_mode_sample_frontend.png)

> **Note**: The cluster mode example above demonstrates the basic multi-agent collaboration flow. In practice, please verify whether cluster mode can fully deliver your engineering goals based on your project needs.

---

## 3. CLI Commands

You can control sessions and switch modes through CLI commands during conversation:

- For mode introduction and switching commands, see: [Mode System](Modes.md)
- For common CLI commands and usage, see: [Slash Commands](SlashCommands.md)

---

## 4. Conversation Logic

### 4.1 Execution Flow

Basic logic behind JiuwenSwarm conversations:

```
User Input → Intent Understanding → Mode Selection → Task Handling → Result Feedback
```

**Detailed flow**

1. **Intent understanding**: Parse natural language and identify real requirements
2. **Mode selection**: Choose an execution strategy based on task traits and current mode
3. **Task handling**:
   - **Task planning mode**: Decompose into subtasks and execute by plan
   - **Performance mode**: Execute quickly, with parallel support
   - **Cluster mode**: Delegate to multiple agents for collaboration
4. **Result feedback**: Present results in a clear and understandable way

### 4.2 Dynamic Adjustment Mechanism

JiuwenSwarm can re-plan task lists during execution based on new user input:

- **Real-time listening**: Continuously receive new user messages
- **Impact evaluation**: Evaluate how new requirements affect current tasks
- **Intelligent merge**: Integrate new requirements into the queue appropriately
- **Seamless continuation**: Keep execution flow coherent and uninterrupted

### 4.3 Task Planning Mechanism Details

**Core mechanism**

Core logic of task planning mode:

1. **Requirement parsing**: Parse user requests into executable subtask sequences
2. **Task recording**: Systematically record all subtasks via todo tools
3. **Status tracking**: Update each subtask status in real time
4. **Dynamic adjustment**: Let users insert, modify, or cancel tasks anytime

**Tool support**

JiuwenSwarm provides a complete todo toolkit (`TodoToolkit`). Tasks are persisted as Markdown in `workspace/session/{session_id}/todo.md`, isolated by session with concurrency-safe read/write.

| Tool | Description |
| :--- | :--- |
| `todo_create` | Create the initial todo list |
| `todo_insert` | Insert a new task at a specified position |
| `todo_complete` | Mark a task as completed |
| `todo_remove` | Remove a specified task |
| `todo_list` | List all current todo items |

**Task states**

| State | Description |
| :--- | :--- |
| `waiting` | Pending execution |
| `running` | In progress |
| `completed` | Completed |
| `cancelled` | Cancelled |
