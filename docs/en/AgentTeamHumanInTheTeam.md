# Agent Team Human Collaboration (HITT)

## Introduction

Agent Team supports not only fully autonomous AI members — you can also **let real humans join as team members** and collaborate alongside AI.

Typical flow: someone creates a team on Web (e.g. a Gomoku game, a code review group) with human seats. Others join via Feishu or Xiaoyi using `/join`, then participate in team conversations under that seat's identity.

> Each human role in the team (e.g. `player-1`) has a corresponding Agent. Your messages drive this Agent; by contrast, AI members claim tasks and speak on their own.
>
> HITT is **enabled by default** — no extra configuration needed. If your team config has `enable_hitt: false`, human members will be unavailable.

---

## 1. Overall Flow

### 1.1 Create a Team on Web

Switch to team mode on Web and describe the team you want in natural language, including human seats. For example:

```
Create a Gomoku game team. The leader hosts the game. Include 1 human player seat.
After the human player joins, the leader asks if they're ready, then starts the game.
Use a Markdown table for the 5×5 board. Use ⚫ for black, ⚪ for white, ➕ for empty.
```

Once created, an invite box appears showing something like:

```
Join instructions:

  /join sess_abc123 as player-1

Leave:

  /exit
```

After the team is created, that Web page has a god view — it can see all member conversations and the Leader's output.

### 1.2 Join via Feishu / Xiaoyi

Copy the `/join` command from the invite box and send it:

**Feishu group**: @ the bot first
```
@bot /join sess_abc123 as player-1
```

**Feishu DM / Xiaoyi**: send directly
```
/join sess_abc123 as player-1
```

After the system confirms, you've joined as "player-1".

### 1.3 Leave

```
/exit
```

Your seat is released.

---

## 2. Messaging

### 2.1 Messaging Rules

**Web**: Sending a message directly chats with the Leader. To drive your Agent, type `$member_name body`.

**Feishu / Xiaoyi**: After `/join`, the system automatically drives your Agent as your seat — just talk normally.

For example, after `/join`-ing as `player-1` in Feishu, typing "I'm ready" is automatically translated to `$player-1 I'm ready` which drives your Agent.

### 2.2 `$` Syntax

The `$` prefix drives the human member Agent. There are three patterns:

| Syntax | Meaning | Example |
|--------|---------|---------|
| `$member_name body` | Drive Agent; content goes to Leader | `$player-1 I place at row 3, column 3` |
| `$member_name @target body` | Send a private message as the Agent | `$player-1 @player-2 Your turn` |
| `$member_name @all body` | Broadcast as the Agent | `$player-1 @all I resign` |

On Feishu/Xiaoyi, `$member_name` is auto-prepended — just talk normally or use `@` mentions.

> **Spacing rule**: there must be a space after `$member_name`. `$player-1@player-2` (no space) will be parsed incorrectly.

### 2.3 Receiving Messages

When others @ you or the Leader sends you a message:
- **Feishu / Xiaoyi**: delivered to your DM or group chat
- **Web**: shown in the team conversation view



### 2.4 Completing Tasks

When the Leader assigns you a task, instruct your Agent to do the work. Tasks assigned to human members do not auto-complete — you need to drive your Agent to complete them.

---

## 3. FAQ

### Q1: How do I drive a human member Agent on Web?

Speaking directly on Web chats with the Leader. To drive a human member Agent, prefix your input with `$member_name`, e.g. `$player-1 I place at row 3, column 3`. Feishu and Xiaoyi auto-prepend `$` after `/join` — no manual prefix needed.

### Q2: What can my Agent do?

Full file operations, shell commands. The Leader can assign tasks to it, but tool calls and task confirmations are all driven by you.

### Q3: What's the difference between the Web creator page and a human member?

The Web page that created the team has a god view — it sees all member conversations and Leader output, and chatting directly reaches the Leader. Human members join via `/join` as seat participants — they need `$member_name` to identify themselves when speaking (auto-prepended on Feishu/Xiaoyi) and only see messages sent to them.

---

## 4. Related Docs

- [Agent Team User Guide](AgentTeam.md)
