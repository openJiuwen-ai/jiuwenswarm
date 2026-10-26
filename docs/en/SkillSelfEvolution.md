# Skill Self-Evolution

## 1. Concept Explanation

### 1.1 Introduction to Skill Self-Evolution

Skill self-evolution is a core feature of JiuwenSwarm based on the openJiuwen evolution framework. It breaks the limitation of fixed capabilities in traditional Agent systems. In traditional systems, once capability definitions are written, they rarely change—tool call errors only generate log entries, and user feedback about misunderstandings doesn't alter future behavior. The capability ceiling is fixed from the day of deployment.

JiuwenSwarm's skill self-evolution mechanism uses a built-in evolution signal detection system to continuously monitor execution processes and dialogue content, automatically converting real-world usage issues into improvement inputs for skills. This transforms skills from one-time static documents into living documents that continuously iterate with real usage.

### 1.2 Core Value

The core value of the skill self-evolution mechanism lies in:

- **No manual intervention required**: The agent automatically improves itself during daily operation
- **Continuous capability improvement**: Skills become more accurate and reliable with increased usage time
- **Adaptive to scene changes**: Automatically adjusts and optimizes based on actual usage scenarios
- **Reduced maintenance costs**: Decreases manual effort for updating and maintaining skills

## 2. Usage Methods

### 2.1 Self-Evolution Configuration Switch

The skill auto-evolution feature is enabled by turning on the master switch `react.evolution.enabled` in the configuration. After the evolution rail is mounted, passive signal scanning runs automatically after chat and tool execution; optionally enable `review_trigger` (soft review follow-ups) and `auto_save` (auto-approve).

![Enable auto-evolution detection](../assets/images/skill演进_自动检测开关.png)

### 2.2 Automatic Evolution (No Intervention Required)

The system automatically detects evolution signals after each tool execution and dialogue completion. When execution exceptions or user corrections are detected, it automatically generates evolution records and stores them in `evolutions.json`.

No user action is required—evolution proceeds silently in the background. When the skill is called next time, it automatically loads content including evolution experience.

![Auto-trigger](../assets/images/skill演进_自动触发.png)

### 2.3 Manual Evolution Triggering

If you want to immediately trigger evolution for a specific skill, you can enter:

```bash
/evolve <skill_name>
```

For example:

```bash
/evolve xlsx
```

The system scans recent dialogue and execution records, generates evolution experience for the skill, and displays the generated results.

![Manual trigger](../assets/images/skill演进_手动触发.png)

### 2.4 View Evolution Status

To see which skills have pending evolution experience, you can enter:

```bash
/evolve list
```

The system lists all skills containing pending evolution records and their specific content summaries.

![Evolution overview](../assets/images/skill演进_查看和整理经验.png)

### 2.5 Manage Evolution Experience

Evolution experience is stored in the `evolutions.json` file under the skill directory. You can directly edit this file to manage evolution experience:

**Directory location:**
```
~/.jiuwenswarm/workspace/agent/skills/<skill_name>/
├── SKILL.md           # Skill source document
├── evolutions.json    # Evolution experience records ← Edit here
└── ...
```

**Common operations:**
- Add new records: Append new objects to the `entries` array
- Modify records: Edit the `change.content` field to update evolution content
- Delete records: Remove from the array
- Mark as solidified: Set `applied` to `true` (system will also toggle this during solidify)

⚠️ **Important**: Do not modify system-managed fields such as `id`, `source`, `timestamp`, `context`, `section`, `action`, `target`, or `relevant`. These fields are generated and maintained by the system.

Changes take effect automatically in the next dialogue.

## 3. Case Practice: Triggering Code Modifications Through Text Dialogue Fields

### 3.1 Scenario Background

Suppose we have a simple `file-operations` skill for handling file operations. This skill includes a `read_file` function for reading file content.

**Initial SKILL.md definition:**
```markdown
# file-operations Skill

## Functions

### read_file
- Function: Read the content of the specified file
- Parameters:
  - `file_path`: File path
- Return: File content

## Examples
- Read file: read_file({"file_path": "/path/to/file.txt"})
```

### 3.2 Evolution Triggered by Dialogue

#### Round 1: Problem Discovery

```
User: Help me read the test.txt file in the current directory
Assistant: Sure, I'll read the test.txt file in the current directory for you.
Tool: file-operations.read_file({
  "file_path": "test.txt"
})
Tool Response: Error: File not found: test.txt
Assistant: Sorry, I can't find the test.txt file. Could you provide the full path?
```

The system detects tool execution failure (`File not found`) and automatically triggers an evolution signal.

#### Round 2: User Provides Full Path

```
User: It should be ./test.txt
Assistant: Sure, I'll read the ./test.txt file for you.
Tool: file-operations.read_file({
  "file_path": "./test.txt"
})
Tool Response: This is the content of the test.txt file.
Assistant: Successfully read the ./test.txt file. Here's the content:
This is the content of the test.txt file.
```

#### Round 3: User Feedback

```
User: No, when I just provide the file name, you should automatically look in the current directory without me manually adding ./
Assistant: Thank you for your feedback. I'll optimize this feature so it automatically looks in the current directory when only a file name is provided.
```

The system detects user correction feedback ("No") and triggers another evolution signal.

### 3.3 Automatically Generated Evolution Records

The system automatically generates evolution records in the `evolutions.json` file under the `file-operations` skill directory.

### 3.4 Effect After Evolution

When the skill is called again, it automatically loads the evolution experience, and the improved functionality works as follows:

```
User: Help me read the test.txt file in the current directory
Assistant: Sure, I'll read the test.txt file in the current directory for you.
Tool: file-operations.read_file({
  "file_path": "test.txt"
})
Tool Response: This is the content of the test.txt file.
Assistant: Successfully read the test.txt file. Here's the content:
This is the content of the test.txt file.
```

Now, when the user only provides a file name, the system automatically looks in the current directory without requiring the user to manually add the `./` prefix.

## 4. Feature Interpretation: Principles and Mechanisms

### 4.1 Core Components

#### 4.1.1 SkillCallOperator

SkillCallOperator is the core entry point for interaction between JiuwenSwarm and skills, responsible for unified management of skills:

- Reads skill definitions (SKILL.md)
- Executes skill commands
- Automatically loads accumulated evolution experiences of skills

When the system detects areas for improvement, these improvements are first stored in `evolutions.json`, and SkillCallOperator merges them before returning them to the Agent.

#### 4.1.2 SkillOptimizer

SkillOptimizer is the optimizer that drives the entire skill evolution process:

1. **Receive signals**: Receives exception signals from SignalDetector to understand what problems the current skill is encountering
2. **Analyze and judge**: Combines dialogue context to determine if the problem is worth recording
3. **Generate improvements**: Calls LLM to generate specific improvement suggestions
4. **Execute recording**: Writes generated improvement plans to evolution records

When you use the `/evolve` command, it's SkillOptimizer working behind the scenes.

#### 4.1.3 SkillEvolutionManager

SkillEvolutionManager is the core manager of the evolution lifecycle, responsible for coordinating various stages of evolution work:

- **Signal scanning**: Calls SignalDetector to extract events that require evolution
- **Record generation**: Calls LLM to convert signals into executable improvement plans
- **Storage management**: Maintains reading and writing of `evolutions.json` files
- **Content solidification**: Merges pending evolution records into the original SKILL.md

It connects SignalDetector, SkillOptimizer, and SkillCallOperator to form a complete evolution loop.

#### 4.1.4 SignalDetector

SignalDetector is the detector for evolution signals, continuously monitoring for anomalies in dialogue and execution results:

- Listens to every tool execution result, capturing error keywords
- Captures user correction feedback (such as "no", "should be", etc.)
- Determines which skill the signal should be attributed to and associates context

SignalDetector works based on rules, does not require calling LLM, and therefore has fast response speed.

### 4.2 Signal Detection Mechanism

#### 4.2.1 Execution Exception Detection

The system automatically detects exceptions in tool execution, including:
- Tool call timeouts
- Interface return errors
- Exception interruptions during code execution

Detection keywords include but are not limited to:
- General errors: `error`, `exception`, `failed`, `failure`, `timeout`
- Network-related: `connection error`, `econnrefused`, `enoent`
- Permission-related: `permission denied`, `command not found`

#### 4.2.2 User Correction Detection

The system identifies user correction feedback, which is often more valuable than error logs:

- Chinese patterns: `不对`, `不是这`, `错 了`, `应该 是`, `你搞错了`, `纠正一下`
- English patterns: `that's wrong`, `you're wrong`, `should be`, `actually`

### 4.3 Evolution Process

```text
User chat / tool run
        │
        ▼
┌───────────────────┐
│  SignalDetector   │  Listens and identifies signals
│   Detects execution exceptions │
│   Detects user corrections │
└────────┬──────────┘
         │
         ▼
┌─────────────────────────────┐
│    SkillEvolutionManager    │
│         .scan()             │  Extracts evolution signals
└────────────┬───────────────┘
             │
             ▼
┌─────────────────────────────┐
│    SkillEvolutionManager    │
│       .generate()           │  LLM generates evolution records
└────────────┬───────────────┘
             │
             ▼
┌─────────────────────────────┐
│      evolutions.json        │  Writes pending solidification records
│    (Under Skill directory) │
└────────────┬───────────────┘
             │
             ▼ (optional)
┌─────────────────────────────┐
│         .solidify()         │  Merges into SKILL.md
└─────────────────────────────┘
```

### 4.4 Evolution Record Storage

Evolution records are stored in the `evolutions.json` file under each skill directory:

```json
{
  "skill_id": "<skill_name>",
  "version": "1.0.0",
  "updated_at": "2024-01-15T10:30:00Z",
  "entries": [
    {
      "id": "ev_1234abcd",
      "source": "execution_failure",
      "timestamp": "2024-01-15T10:30:00Z",
      "context": "API timeout after 30s",
      "change": {
        "section": "Troubleshooting",
        "action": "append",
        "content": "## FAQ\n- On API timeout..."
      },
      "applied": false
    }
  ]
}
```

Where:
- `applied: false` indicates pending solidification status
- `applied: true` indicates already solidified into SKILL.md