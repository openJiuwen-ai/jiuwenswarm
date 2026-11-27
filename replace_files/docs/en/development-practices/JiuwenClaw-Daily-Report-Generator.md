# From Configuration to Architecture: Building the Advanced JiuwenClaw Daily Report Generator

## Introduction — When “daily report automation” meets real office work

Over the past year, AI agents have been discussed everywhere — from simple Q&A to complex workflows. Putting an agent into everyday office work surfaces a practical gap:

> A conversational agent is not the same as an assistant that automates work.

Office work is rarely “just chat.” It includes repetitive chores: end-of-day reports, weekly rollups, monthly summaries. They are not intellectually hard, but they consume time and attention.

If an agent only chats, it stays a novelty.

To be a **deliverable automation assistant**, it needs three things: (1) collect data from multiple sources, (2) analyze productivity intelligently, and (3) push results through the right channels proactively.

This article covers:

- Multi-source collection (Git commits + email stats + memory + todos)
- A work-analysis engine (metrics, trends, keyword extraction)
- Report generation (daily, weekly, monthly)
- Scheduled tasks and push configuration
- Implementation and validation

If you are asking:

- How to turn a demo agent into a productivity tool
- How to make an agent push information instead of only reacting
- How to build a reusable modular skill set

the sections below may help.

---

## Project environment

> **This document reflects a real project.** Configuration and code match what was actually used.

### Runtime

| Item | Value |
| --- | --- |
| **Project path** | `D:\Download\jiuwenclaw` |
| **OS** | Windows 10 |
| **Python** | 3.10+ |
| **Model** | ModelScope (`Qwen/Qwen3-235B-A22B-Instruct-2507`) |

### Data sources

| Source | Value |
| --- | --- |
| **Git repo** | `D:\Download\jiuwenclaw` (this project) |
| **Email** | `zxworkem@163.com` (NetEase 163 Email) |
| **Delivery** | Feishu (`cli_a92035b1823a9cd2`) |
| **Heartbeat window** | Daily 18:00–18:30 |

### Key paths

```plain
D:\Download\jiuwenclaw\
├── .env
├── config/config.yaml                # App config (heartbeat, Feishu)
├── workspace/
│   ├── HEARTBEAT.md                  # Heartbeat tasks
│   └── agent/skills/daily-report/    # Skill module
│       ├── SKILL.md                  # Skill definition v2.0.0
│       ├── collectors/
│       ├── analyzers/
│       └── generators/
```

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772822490912-87defae8-95ee-4c68-890b-d5082761d783.png)

## 1. Problem background

### 1.1 Limits of a “basic” daily report skill

People often assume:

> “Isn’t it just memory + todos + a template?”

That works for toy demos.

In production you hit three problems:

1. **Single source** — Only memory and todos; no visibility into commits, email, or real output.
2. **No analysis** — Lists tasks but does not compute metrics, trends, or advice.
3. **Fixed report type** — Daily only; no weekly or monthly rollups.

Example: “commit trend this week” or “productivity vs last week” needs Git history, time windows, and comparison logic — a basic skill cannot do that.

The **advanced daily report** skill aggregates multiple sources, runs analysis, and produces metrics, trends, and suggestions.

### 1.2 Office pain points

**Writing the daily report takes too long** — 15–20 minutes at the end of the day to remember what shipped.

**Formats drift** — Sometimes bullet lists, sometimes narrative, sometimes skipped; managers struggle to compare.

**Important work gets forgotten** — A critical bug fix or feature may be omitted from the report. Auto-collecting Git commits reduces that gap.

**Email work is invisible** — Volume, unread counts, and follow-ups rarely make it into a manual report.

With the advanced skill, Git, email stats, memory, and todos feed an engine that computes metrics and trends.

### 1.3 JiuwenClaw skills

| Capability | Description |
| --- | --- |
| **Modular skills** | Multiple Python modules per skill |
| **Tools** | `allowed_tools` for system integration |
| **Heartbeat** | Periodic skill execution |
| **Channels** | Feishu, Web, etc. |

The payoff is **extensibility**: collection, analysis, and reporting stay separated.

## 2. Technical approach

### 2.1 Layering in JiuwenClaw

The advanced daily report skill sits in the application layer (see diagram in the original article).

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772822901009-32c4955a-17b4-47f8-81f3-22283748f998.png)

### 2.2 Three-layer data flow

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772861778248-67064c9c-4de8-41c7-850a-699ca4205cd1.png)

### 2.3 Core components

| Component | Type | Role | Module |
| --- | --- | --- | --- |
| **GitCollector** | Collector | Git commits | `collectors/git_collector.py` |
| **EmailCollector** | Collector | NetEase mailbox stats | `collectors/email_collector.py` |
| **MemoryCollector** | Collector | Memory files | `collectors/memory_collector.py` |
| **TodoCollector** | Collector | Todo lists | `collectors/todo_collector.py` |
| **DataAggregator** | Aggregator | Merge sources | `collectors/aggregator.py` |
| **WorkAnalyzer** | Analyzer | Work analysis | `analyzers/work_analyzer.py` |
| **ReportGenerator** | Generator | Reports | `generators/report_generator.py` |

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772861858079-e087425b-3bfd-40de-8567-92c874d0b396.png)

### 2.4 Flow and design choices

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772861961611-4d5aae74-099e-4cc4-aec5-622b14fc515e.png)

| Decision | Choice | Reason |
| --- | --- | --- |
| Git | `git log` | No extra deps |
| Email | IMAP | Supported by NetEase |
| Tokenization | jieba (optional) | Good for Chinese; can degrade gracefully |
| Reports | Markdown | Portable; Feishu renders |
| Trigger | Heartbeat + manual | Scheduled + on demand |

## Chapter 3 — Skills system engineering

### 3.1 Skills directory structure

```plain
workspace/agent/skills/daily-report/
├── SKILL.md
├── collectors/
│   ├── __init__.py
│   ├── git_collector.py
│   ├── email_collector.py
│   ├── memory_collector.py
│   ├── todo_collector.py
│   └── aggregator.py
├── analyzers/
│   ├── __init__.py
│   └── work_analyzer.py
├── generators/
│   ├── __init__.py
│   └── report_generator.py
└── report_helper.py            # Legacy compatibility
```

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862008911-7b9d807a-9175-4c62-a208-c3b6381169be.png)

### 3.2 `advanced-daily-report` SKILL.md


```markdown
---
name: advanced-daily-report
version: 2.0.0
description: Advanced version daily report generator, supports multi-data source collection, work analysis, trend comparison, and weekly and monthly report aggregation
tags: [report, automation, productivity, daily, weekly, monthly, advanced]
allowed_tools: [read_memory, write_memory, mcp_exec_command, read_file, write_file]
---

# Advanced version daily report generator

Automatically collect multi-source data, intelligently analyze work efficiency, generate daily report/weekly report/monthly report and push them to Feishu.

## Core capabilities

### 1. Multi-data source collection

| Data source | Collected content | Frequency |
|--------|----------|------|
| **Git repositories** | submission records, code change statistics | real time |
| **Netease email** | sent/received email statistics, unread reminders | real time |
| **memory system** | today’s work records, long-term memory | real time |
| **to-do items** | task status, completion rate | real time |

### 2. Intelligent work analysis

- **Efficiency metric calculation**
  - task completion rate = completed / total tasks
  - productivity score (0-100)
  - focus score (0-100)

- **Trend comparison**
  - compared with yesterday
  - compared with the same period last week
  - weekly trend chart

- **Keyword extraction**
  - automatically extract today’s work keywords
  - work topic clustering

### 3. Multiple report types

| Type | Trigger method | Push time |
|------|----------|----------|
| **Daily report** | manual/timed | every day 18:00 |
| **Weekly report** | timed | every Friday 18:00 |
| **Monthly report** | timed | on the last day of every month 18:00 |

## Directory structure
```

daily-report/
├── SKILL.md              # Skill definition (this file)
├── collectors/
│   ├── __init__.py
│   ├── git_collector.py
│   ├── email_collector.py
│   ├── memory_collector.py
│   ├── todo_collector.py
│   └── aggregator.py
├── analyzers/
│   ├── __init__.py
│   └── work_analyzer.py
├── generators/
│   ├── __init__.py
│   └── report_generator.py
└── report_helper.py      # Legacy compatibility
```

## Usage

### ⚠️ Important: how to run

This skill collects data by running Python scripts (Git, mailbox, memory, todos).

**You must use the `mcp_exec_command` tool to run the scripts** — do not reply to the user without executing them.

**What the scripts collect automatically:**

- **Git commits**: `git log` on repo `D:/Download/jiuwenclaw`
- **Mailbox stats**: IMAP to `zxworkem@163.com` (requires mailbox authorization code)
- **Memory**: daily files under `workspace/agent/memory/`
- **Todos**: `todo.md` under `workspace/session/`

### Manual trigger

When the user asks for a daily / weekly / monthly report, **run:**

```bash
# Today’s daily report (Git + todos + memory)
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py daily --save

# Daily report for a specific date
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py daily --date 2026-03-06 --save

# Weekly report (aggregate one week)
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py weekly --save

# Monthly report (aggregate one month, incl. per-day Git stats)
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py monthly --save

# Monthly report for a given month
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py monthly --year 2026 --month 3 --save
```

### Execution steps

1. User sends something like “generate daily / weekly / monthly report”.
2. **Run the commands above with `mcp_exec_command`.**
3. The script collects:
   - **Git**: `git log` for commits and churn
   - **Email**: IMAP stats (if mail is configured)
   - **Memory**: work notes from memory files
   - **Todos**: task state from `todo.md`
4. Wait until the script finishes and read its output.
5. Send the report content to the user.

### Trigger phrases (examples)

- **Daily**: generate today’s report, yesterday’s report, today’s work, today’s commits
- **Weekly**: this week’s summary, weekly rollup, week in review
- **Monthly**: this month’s summary, monthly review, summarize this month’s mail into a report, this month’s commit stats

### Data sources

| Source | How | Where to configure |
|--------|-----|--------------------|
| **Git** | `git log` | Repo path: `D:/Download/jiuwenclaw` |
| **NetEase** | IMAP | `.env`: `EMAIL_ADDRESS`, `EMAIL_TOKEN` |
| **Memory** | Read Markdown | `workspace/agent/memory/YYYY-MM-DD.md` |
| **Todos** | Parse `todo.md` | `workspace/session/*/todo.md` |

### Scheduled trigger

Configure periodic runs in `HEARTBEAT.md`:

```markdown
## Active tasks
- Generate today’s work report    # daily
- Generate weekly report          # every Friday
- Generate monthly report         # month end
```

## Daily report template

```markdown
# 📋 Work daily — 2026-03-06

## 📊 Overview

| Metric | Value |
|--------|-------|
| Commits | 5 |
| Tasks done | 3/8 |
| Code churn | +350/-80 |
| Email | in 12 / out 3 |
| Productivity | 78.5 |

## ✅ Done
- Finished daily-report skill
- Configured Feishu channel
- Tested heartbeat

## 🔄 In progress
- Write documentation
- Weekly aggregation

## 💻 Commits

| Time | Message | Churn |
|------|---------|-------|
| 09:30 | feat: daily report | +120/-30 |
| 14:15 | fix: email collector | +45/-12 |

## 📧 Email
- Inbox today: 12
- Sent today: 3
- Unread: 2

## 📈 Trends
- Commits: ↑ 2 vs yesterday
- Efficiency: ↑ 5.2 pts

## 💡 Suggestions
1. Focus seems low — reduce interruptions
2. Task completion rate can improve

## 🔜 Tomorrow
- Polish daily template
- Add weekly rollup
```

## Configuration

### Git repo

Monitored repository (read automatically by scripts):

```
Repo path: D:/Download/jiuwenclaw
```

The script uses `git log` to collect:

- Commit hash, message, author, time
- Per commit: files changed, insertions, deletions

### Email

Configure in `.env` (shape used in the project):

```env
EMAIL_ADDRESS=zxworkem@163.com
EMAIL_TOKEN=<your-imap-auth-code>
EMAIL_PROVIDER=163
```

**Note:** `EMAIL_TOKEN` is the mailbox **authorization code**, not the login password. In 163: Settings → POP3/SMTP/IMAP → enable IMAP → generate auth code.

### Heartbeat

```yaml
heartbeat:
  every: 3600
  target: feishu
  active_hours:
    start: 18:00
    end: 18:30
```

## API reference

### Data aggregator

```python
from collectors import DataAggregator

aggregator = DataAggregator(
    workspace_dir="workspace",
    git_repo="path/to/repo",
    email_config={
        "address": "xxx@163.com",
        "auth_code": "xxx",
        "provider": "163"
    }
)

# Collect today
data = aggregator.collect()

# Collect one week
week_data = aggregator.collect_week()
```

### Work analyzer

```python
from analyzers import WorkAnalyzer

analyzer = WorkAnalyzer()
result = analyzer.analyze(data.to_dict())

print(f"Productivity: {result.metrics.productivity_score}")
print(f"Keywords: {result.keywords}")
print(f"Suggestions: {result.suggestions}")
```

### Report generator

```python
from generators import ReportGenerator

generator = ReportGenerator(aggregator)

daily = generator.generate_daily()
weekly = generator.generate_weekly()
monthly = generator.generate_monthly(2026, 3)
```

## Notes

1. **Git**: ensure the repo path is correct and readable.
2. **Mail**: use the authorization code, not the login password.
3. **Heartbeat**: restart services after changes.
4. **Storage**: reports are saved under `workspace/agent/reports/`.

## Changelog

- **v2.0.0** (2026-03-06): Advanced — multi-source, trends, weekly/monthly.
- **v1.0.0** (2026-03-06): Initial basic daily report.
```

![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862026601-dd7ed3ff-26da-4182-8f6f-c45c437feffc.png)

### 3.3 Daily report template (minimal example)
```markdown
# 📋 Work daily — 2026-03-06

## 📊 Overview

| Metric | Value |
|--------|-------|
| Commits | 5 |
| Tasks done | 3/8 |
| Code churn | +350/-80 |
| Email | in 12 / out 3 |
| Productivity | 78.5 |

## ✅ Done
- Finished daily-report skill
- Configured Feishu channel

## 💻 Commits

| Time | Message | Churn |
|------|---------|-------|
| 09:30 | feat: add daily report | +120/-30 |

## 📧 Email
- Inbox today: 12
- Sent today: 3
- Unread: 2

## 📈 Trends
- Commits: ↑ 2
- Efficiency: ↑ 5.2 pts

## 💡 Suggestions
1. Focus seems low — reduce interruptions

## 🔜 Tomorrow
- Polish daily template
```

## Chapter 4 — Data collection layer (full implementation)

### 4.1 Git collector

```python
# collectors/git_collector.py
# -*- coding: utf-8 -*-
"""
Git 提交记录采集器

功能：
- 获取指定日期的 Git 提交记录
- 统计提交次数、修改文件数、代码行数变化
- 支持多个仓库
"""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class GitCommit:
    """Git 提交记录"""

    hash: str  # 提交哈希
    message: str  # 提交信息
    author: str  # 作者
    date: datetime  # 提交时间
    files_changed: int = 0  # 修改文件数
    insertions: int = 0  # 新增行数
    deletions: int = 0  # 删除行数

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "message": self.message,
            "author": self.author,
            "date": self.date.isoformat(),
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


@dataclass
class GitStats:
    """Git 统计数据"""

    commits: list[GitCommit] = field(default_factory=list)
    total_commits: int = 0
    total_files_changed: int = 0
    total_insertions: int = 0
    total_deletions: int = 0

    @property
    def net_lines(self) -> int:
        """净增行数"""
        return self.total_insertions - self.total_deletions

    def to_dict(self) -> dict:
        return {
            "total_commits": self.total_commits,
            "total_files_changed": self.total_files_changed,
            "total_insertions": self.total_insertions,
            "total_deletions": self.total_deletions,
            "net_lines": self.net_lines,
            "commits": [c.to_dict() for c in self.commits],
        }


class GitCollector:
    """Git 提交记录采集器"""

    def __init__(self, repo_path: str | Path):
        """
        初始化 Git 采集器

        Args:
            repo_path: Git 仓库路径
        """
        self.repo_path = Path(repo_path).resolve()

    def _run_git_command(self, args: list[str], timeout: int = 30) -> str:
        """执行 Git 命令"""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo_path)] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            return ""

    def get_commits(self, date: Optional[str] = None, author: Optional[str] = None) -> GitStats:
        """
        获取指定日期的提交记录

        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认今天
            author: 作者名称过滤，默认不过滤

        Returns:
            GitStats: Git 统计数据
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        stats = GitStats()

        # 获取提交列表
        log_format = "%H|%s|%an|%ai"
        since = f"{date} 00:00:00"
        until = f"{date} 23:59:59"

        cmd_args = [
            "log",
            f"--since={since}",
            f"--until={until}",
            f"--format={log_format}",
        ]

        if author:
            cmd_args.append(f"--author={author}")

        log_output = self._run_git_command(cmd_args)

        if not log_output:
            return stats

        # 解析提交记录
        for line in log_output.split("\n"):
            if not line.strip():
                continue

            parts = line.split("|", 3)
            if len(parts) < 4:
                continue

            commit_hash, message, author_name, date_str = parts

            try:
                commit_date = datetime.fromisoformat(
                    date_str.replace(" ", "T").split("+")[0]
                )
            except ValueError:
                commit_date = datetime.now()

            # 获取每个提交的文件变更统计
            numstat = self._run_git_command(
                ["show", "--numstat", "--format=", commit_hash]
            )

            files_changed = 0
            insertions = 0
            deletions = 0

            for stat_line in numstat.split("\n"):
                if not stat_line.strip():
                    continue
                stat_parts = stat_line.split("\t")
                if len(stat_parts) >= 2:
                    try:
                        ins = int(stat_parts[0]) if stat_parts[0] != "-" else 0
                        dels = int(stat_parts[1]) if stat_parts[1] != "-" else 0
                        insertions += ins
                        deletions += dels
                        files_changed += 1
                    except ValueError:
                        continue

            commit = GitCommit(
                hash=commit_hash[:8],
                message=message.strip(),
                author=author_name,
                date=commit_date,
                files_changed=files_changed,
                insertions=insertions,
                deletions=deletions,
            )

            stats.commits.append(commit)
            stats.total_commits += 1
            stats.total_files_changed += files_changed
            stats.total_insertions += insertions
            stats.total_deletions += deletions

        return stats
```

### 4.2 Email Statistics Collector

> **Important Note**: The 163 email service’s IMAP has special restrictions. Using the `SELECT` command directly will return an `"Unsafe Login"` error.  
> Solution:
> 1. Register the ID command in `imaplib`: `imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')`
> 2. After logging in, send the ID command to declare the client identity
> 3. Use the `STATUS` command to obtain email statistics (bypassing the `SELECT` restriction)
> 4. If you need to read email content, after the ID command is successfully sent, you can use `SELECT` normally

```python
# collectors/email_collector.py
# -*- coding: utf-8 -*-
"""
Email Statistics Collector

Supports:
- NetEase Mail (163/126/yeah)
- Reading emails via the IMAP protocol

Features:
- Count the number of emails in the inbox
- Get the number of unread emails
- Read email content previews

Special handling for 163:
- Must register the ID command and send it after logging in
- Use the STATUS command to get statistics (bypass the Unsafe Login restriction)
"""

import email
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header
from typing import Optional

try:
    import imaplib
    IMAP_AVAILABLE = True
    # 163 mailbox must: register ID command in imaplib
    imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')
except ImportError:
    IMAP_AVAILABLE = False
    imaplib = None


# NetEase IMAP server configuration
NETEASE_IMAP_SERVERS = {
    "163": "imap.163.com",
    "126": "imap.126.com",
    "yeah": "imap.yeah.net",
}


@dataclass
class EmailInfo:
    """Email information"""
    subject: str = ""  # Subject
    sender: str = ""  # Sender
    date: str = ""  # Date
    body_preview: str = ""  # Body preview

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date,
            "body_preview": self.body_preview[:200] if self.body_preview else "",
        }


@dataclass
class EmailStats:
    """Email statistics data"""
    total_emails: int = 0  # Total number of emails in the mailbox
    unread: int = 0  # Number of unread emails
    recent_emails: list[EmailInfo] = field(default_factory=list)  # Recent emails

    def to_dict(self) -> dict:
        return {
            "total_emails": self.total_emails,
            "unread": self.unread,
            "recent_emails": [e.to_dict() for e in self.recent_emails],
        }


class EmailCollector:
    """Email statistics collector"""

    def __init__(
        self,
        email_address: str,
        auth_code: str,
        provider: str = "163",
    ):
        """
        Initialize the email collector

        Args:
            email_address: Email address
            auth_code: Authorization code (not the login password)
            provider: Email provider (163/126/yeah)
        """
        if not IMAP_AVAILABLE:
            raise ImportError("imaplib module is not available")

        self.email_address = email_address
        self.auth_code = auth_code
        self.provider = provider.lower()

        if self.provider not in NETEASE_IMAP_SERVERS:
            raise ValueError(f"Unsupported email provider: {provider}")

        self.imap_server = NETEASE_IMAP_SERVERS[self.provider]
        self._connection = None

    def _decode_str(self, s: str) -> str:
        """Decode an email string"""
        if s is None:
            return ""
        decoded_parts = decode_header(s)
        result = []
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(encoding or "utf-8", errors="ignore"))
                except Exception:
                    result.append(part.decode("utf-8", errors="ignore"))
            else:
                result.append(part)
        return "".join(result)

    def connect(self) -> bool:
        """Connect to the IMAP server and send the ID command"""
        try:
            self._connection = imaplib.IMAP4_SSL(self.imap_server, 993)
            self._connection.login(self.email_address, self.auth_code)

            # 163 mailbox must: send ID immediately after login
            args = '("name" "python" "version" "1.0" "vendor" "python-imap")'
            self._connection._simple_command("ID", args)

            return True
        except Exception as e:
            print(f"Failed to connect to mailbox: {e}")
            return False

    def disconnect(self):
        """Disconnect"""
        if self._connection:
            try:
                self._connection.logout()
            except Exception:
                pass
            self._connection = None

    def get_stats(self) -> EmailStats:
        """
        Get email statistics (use the STATUS command, bypass SELECT restrictions)

        Returns:
            EmailStats: Email statistics data
        """
        stats = EmailStats()

        if not self._connection:
            if not self.connect():
                return stats

        try:
            # Use STATUS to get statistics (SELECT on 163 will return Unsafe Login)
            status, data = self._connection.status("INBOX", "(MESSAGES UNSEEN)")
            if status == "OK" and data:
                # Parse response: b'"INBOX" (MESSAGES 39 UNSEEN 32)'
                response = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
                messages_match = re.search(r'MESSAGES\s+(\d+)', response)
                unseen_match = re.search(r'UNSEEN\s+(\d+)', response)
                if messages_match:
                    stats.total_emails = int(messages_match.group(1))
                if unseen_match:
                    stats.unread = int(unseen_match.group(1))
        except Exception as e:
            print(f"Failed to get email statistics: {e}")

        return stats

    def get_recent_emails(self, limit: int = 10, days: int = 30) -> list[EmailInfo]:
        """
        Read recent email content (after sending the ID command, SELECT works normally)

        Args:
            limit: Maximum number of emails to read
            days: Only read emails within the last N days

        Returns:
            List of emails
        """
        if not self._connection:
            if not self.connect():
                return []

        emails = []

        try:
            # ID command has been sent; now SELECT works normally
            typ, dat = self._connection.select("INBOX")
            if typ != "OK":
                return []

            # Search for emails within the last N days
            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            typ, msg_ids = self._connection.search(None, f'(SINCE {since_date})')

            if typ != "OK" or not msg_ids[0]:
                return []

            ids = msg_ids[0].split()[-limit:]  # Get the latest N emails

            for msg_id in reversed(ids):
                try:
                    typ, msg_data = self._connection.fetch(msg_id, "(RFC822)")
                    if typ != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    # Decode subject
                    subject = self._decode_str(msg["Subject"]) or "(No subject)"
                    from_addr = self._decode_str(msg.get("From", ""))
                    date_str = msg.get("Date", "")

                    # Extract body
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                payload = part.get_payload(decode=True)
                                charset = part.get_content_charset() or "utf-8"
                                body = payload.decode(charset, errors="ignore")
                                break
                            elif content_type == "text/html" and not body:
                                payload = part.get_payload(decode=True)
                                charset = part.get_content_charset() or "utf-8"
                                html_body = payload.decode(charset, errors="ignore")
                                body = re.sub(r'<[^>]+>', ' ', html_body)
                                body = re.sub(r'\s+', ' ', body).strip()
                    else:
                        payload = msg.get_payload(decode=True)
                        charset = msg.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="ignore") if payload else ""

                    emails.append(EmailInfo(
                        subject=subject[:100],
                        sender=from_addr[:80],
                        date=date_str,
                        body_preview=body[:500] if body else ""
                    ))
                except Exception:
                    continue

        except Exception as e:
            print(f"Failed to read email content: {e}")

        return emails

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
```

### 4.3 Memory Data Collector
```python
# collectors/memory_collector.py
# -*- coding: utf-8 -*-
"""
Memory Data Collector

Function:
- Read today's memory file
- Read long-term memory
- Extract work summary
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class MemoryData:
    """Memory data"""

    today_content: str = ""  # Today's memory content
    long_term_content: str = ""  # Long-term memory content
    work_summaries: list[str] = field(default_factory=list)  # List of work summaries
    key_decisions: list[str] = field(default_factory=list)  # Key decisions

    def to_dict(self) -> dict:
        return {
            "today_content": self.today_content[:500] if self.today_content else "",
            "work_summaries": self.work_summaries,
            "key_decisions": self.key_decisions,
        }


class MemoryCollector:
    """Memory Data Collector"""

    def __init__(self, workspace_dir: str | Path):
        """
        Initialize memory collector

        Args:
            workspace_dir: workspace directory path
        """
        self.workspace_dir = Path(workspace_dir)
        self.memory_dir = self.workspace_dir / "agent" / "memory"

    def _read_file_safe(self, file_path: Path) -> str:
        """Safely read file"""
        if not file_path.exists():
            return ""
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _extract_list_items(self, content: str) -> list[str]:
        """Extract list items (lines starting with - or *)"""
        items = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                item = stripped.lstrip("-* ").strip()
                # Skip comments and empty items
                if item and not item.startswith("<!--"):
                    items.append(item)
        return items

    def _extract_sections(self, content: str, section_title: str) -> list[str]:
        """Extract content under a specified title"""
        items = []
        in_section = False

        for line in content.split("\n"):
            stripped = line.strip()

            # Detect title
            if stripped.startswith("##"):
                if section_title.lower() in stripped.lower():
                    in_section = True
                else:
                    in_section = False
                continue

            if in_section:
                if stripped.startswith("-") or stripped.startswith("*"):
                    item = stripped.lstrip("-* ").strip()
                    if item and not item.startswith("<!--"):
                        items.append(item)

        return items

    def collect(self, date: Optional[str] = None) -> MemoryData:
        """
        Collect memory data

        Args:
            date: Date string (YYYY-MM-DD), default is today

        Returns:
            MemoryData: Memory data
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        data = MemoryData()

        # Read today's memory
        today_file = self.memory_dir / f"{date}.md"
        data.today_content = self._read_file_safe(today_file)

        # Read long-term memory
        memory_file = self.memory_dir / "MEMORY.md"
        data.long_term_content = self._read_file_safe(memory_file)

        # Extract work summaries
        data.work_summaries = self._extract_list_items(data.today_content)

        # Extract key decisions (from long-term memory)
        data.key_decisions = self._extract_sections(
            data.long_term_content, "decisions"
        ) or self._extract_sections(data.long_term_content, "preferences")

        return data

    def get_week_memories(self, end_date: Optional[str] = None) -> dict[str, MemoryData]:
        """Get memory data for a week"""
        if end_date is None:
            end_date = datetime.now()
        else:
            end_date = datetime.strptime(end_date, "%Y-%m-%d")

        result = {}
        for i in range(7):
            date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            result[date] = self.collect(date)

        return result

    def get_month_memories(self, year: int, month: int) -> dict[str, MemoryData]:
        """Get memory data for a month"""
        import calendar

        _, days_in_month = calendar.monthrange(year, month)
        result = {}

        for day in range(1, days_in_month + 1):
            date = f"{year:04d}-{month:02d}-{day:02d}"
            result[date] = self.collect(date)

        return result
```

### 4.4 To-Do Item Collector
```python
# collectors/todo_collector.py
# -*- coding: utf-8 -*-
"""
To-Do Item Collector

Function:
- Read the todo.md file
- Parse task status
- Count completion status
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class TodoTask:
    """To-do task"""

    id: str
    content: str
    status: str  # completed, running, waiting, cancelled
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class TodoStats:
    """To-do statistics"""

    total: int = 0
    completed: int = 0
    running: int = 0
    waiting: int = 0
    cancelled: int = 0
    tasks: list[TodoTask] = field(default_factory=list)

    @property
    def completion_rate(self) -> float:
        """Completion rate"""
        if self.total == 0:
            return 0.0
        return self.completed / self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "completed": self.completed,
            "running": self.running,
            "waiting": self.waiting,
            "cancelled": self.cancelled,
            "completion_rate": round(self.completion_rate, 2),
            "tasks": [t.to_dict() for t in self.tasks],
        }


class TodoCollector:
    """To-Do Item Collector"""

    def __init__(self, workspace_dir: str | Path):
        """
        Initialize the to-do collector

        Args:
            workspace_dir: workspace directory path
        """
        self.workspace_dir = Path(workspace_dir)
        self.session_dir = self.workspace_dir / "session"

    def _read_file_safe(self, file_path: Path) -> str:
        """Safely read file"""
        if not file_path.exists():
            return ""
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _find_latest_todo_file(self) -> Optional[Path]:
        """Find the latest todo.md file"""
        if not self.session_dir.exists():
            return None

        todo_files = list(self.session_dir.rglob("todo.md"))

        if not todo_files:
            return None

        # Sort by modification time and return the latest
        todo_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return todo_files[0]

    def _parse_status(self, line: str) -> tuple[str, str]:
        """
        Parse the task line and extract ID and status

        Supports format:
        - [x] 1. Task content
        - [ ] 1. Task content
        - 1. [status:completed] Task content
        - 1. ✅ Task content
        """
        # Markdown checkbox format
        checkbox_match = re.match(r"\s*-\s*\[([xX ])\]\s*(.+)", line)
        if checkbox_match:
            checked = checkbox_match.group(1).lower() == "x"
            content = checkbox_match.group(2).strip()
            status = "completed" if checked else "waiting"
            return "", status

        # Format with status marker
        status_match = re.match(r"\s*(\d+)\.\s*\[status:(\w+)\]\s*(.+)", line, re.IGNORECASE)
        if status_match:
            task_id = status_match.group(1)
            status = status_match.group(2).lower()
            content = status_match.group(3).strip()
            return task_id, status

        # Format with status marker (before square brackets)
        bracket_match = re.match(r"\s*(\d+)\.\s*\[([xX✅🔄⏳❌])\]\s*(.+)", line)
        if bracket_match:
            task_id = bracket_match.group(1)
            status_char = bracket_match.group(2)
            content = bracket_match.group(3).strip()

            status_map = {
                "x": "completed", "X": "completed", "✅": "completed",
                "🔄": "running", "⏳": "waiting", "❌": "cancelled",
            }
            status = status_map.get(status_char, "waiting")
            return task_id, status

        # Plain numbered format
        number_match = re.match(r"\s*(\d+)\.\s+(.+)", line)
        if number_match:
            task_id = number_match.group(1)
            content = number_match.group(2).strip()

            # Detect status from content
            if "✅" in content or "[completed]" in content:
                status = "completed"
            elif "🔄" in content or "[in progress]" in content:
                status = "running"
            elif "❌" in content or "[cancel]" in content:
                status = "cancelled"
            else:
                status = "waiting"

            return task_id, status

        return "", ""

    def collect(self) -> TodoStats:
        """
        Collect to-do data

        Returns:
            TodoStats: to-do statistics data
        """
        stats = TodoStats()

        todo_file = self._find_latest_todo_file()
        if not todo_file:
            return stats

        content = self._read_file_safe(todo_file)
        if not content:
            return stats

        task_counter = 0

        for line in content.split("\n"):
            if not line.strip():
                continue

            task_id, status = self._parse_status(line)

            if status:
                task_counter += 1

                # Extract task content (remove status marker)
                content_clean = re.sub(r"\[status:\w+\]\s*", "", line)
                content_clean = re.sub(r"\[[xX✅🔄⏳❌]\]\s*", "", content_clean)
                content_clean = re.sub(r"^\s*-\s*\[[xX ]\]\s*", "", content_clean)
                content_clean = re.sub(r"^\s*\d+\.\s*", "", content_clean)
                content_clean = content_clean.strip()

                if not task_id:
                    task_id = str(task_counter)

                task = TodoTask(id=task_id, content=content_clean, status=status)

                stats.tasks.append(task)
                stats.total += 1

                if status == "completed":
                    stats.completed += 1
                elif status == "running":
                    stats.running += 1
                elif status == "waiting":
                    stats.waiting += 1
                elif status == "cancelled":
                    stats.cancelled += 1

        return stats
```

### 4.5 Collection Module Initialization File

```python
# collectors/__init__.py
# -*- coding: utf-8 -*-
"""
Advanced Daily Report Generator - Data Collection Module

Includes:
- GitCollector: Collects Git commit records
- EmailCollector: Collects email statistics
- MemoryCollector: Collects memory data
- TodoCollector: Collects todo items
- DataAggregator: Aggregates collected data
"""

from .git_collector import GitCollector, GitCommit
from .email_collector import EmailCollector, EmailStats
from .memory_collector import MemoryCollector
from .todo_collector import TodoCollector
from .aggregator import DataAggregator, CollectedData

__all__ = [
    "GitCollector", "GitCommit",
    "EmailCollector", "EmailStats",
    "MemoryCollector",
    "TodoCollector",
    "DataAggregator", "CollectedData",
]
```

### 4.6 Data Aggregator
```python
# collectors/aggregator.py
# -*- coding: utf-8 -*-
"""
Data Aggregator

Function:
- Integrate data from all collectors
- Apply unified time window filtering
- Provide a unified data access interface
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .email_collector import EmailCollector, EmailStats
from .git_collector import GitCollector, GitStats
from .memory_collector import MemoryCollector, MemoryData
from .todo_collector import TodoCollector, TodoStats


@dataclass
class CollectedData:
    """Aggregated data"""

    date: str  # Date
    collected_at: datetime  # Collection time

    # Git data
    git: GitStats = field(default_factory=GitStats)

    # Email data
    email: EmailStats = field(default_factory=EmailStats)

    # Memory data
    memory: MemoryData = field(default_factory=MemoryData)

    # Todo data
    todo: TodoStats = field(default_factory=TodoStats)

    # Historical comparison data
    comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "collected_at": self.collected_at.isoformat(),
            "git": self.git.to_dict(),
            "email": self.email.to_dict(),
            "memory": self.memory.to_dict(),
            "todo": self.todo.to_dict(),
            "comparison": self.comparison,
        }


class DataAggregator:
    """Data Aggregator"""

    def __init__(
        self,
        workspace_dir: str | Path,
        git_repo: Optional[str | Path] = None,
        email_config: Optional[dict] = None,
    ):
        """
        Initialize the data aggregator

        Args:
            workspace_dir: workspace directory
            git_repo: Git repository path
            email_config: Email configuration
        """
        self.workspace_dir = Path(workspace_dir)

        # Initialize each collector
        self.memory_collector = MemoryCollector(self.workspace_dir)
        self.todo_collector = TodoCollector(self.workspace_dir)

        # Git collector (optional)
        self.git_collector = None
        if git_repo:
            self.git_collector = GitCollector(git_repo)

        # Email collector (optional)
        self.email_collector = None
        self.email_config = email_config

    def collect(self, date: Optional[str] = None, include_comparison: bool = True) -> CollectedData:
        """
        Aggregate and collect data

        Args:
            date: Date string, default is today
            include_comparison: Whether to include historical comparison

        Returns:
            CollectedData: Aggregated data
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        data = CollectedData(
            date=date,
            collected_at=datetime.now(),
        )

        # Collect memory data
        data.memory = self.memory_collector.collect(date)

        # Collect todo data
        data.todo = self.todo_collector.collect()

        # Collect Git data
        if self.git_collector:
            data.git = self.git_collector.get_commits(date)

        # Collect email data
        if self.email_config and self.email_collector is None:
            try:
                self.email_collector = EmailCollector(
                    email_address=self.email_config["address"],
                    auth_code=self.email_config["auth_code"],
                    provider=self.email_config.get("provider", "163"),
                )
            except Exception as e:
                print(f"Email collector initialization failed: {e}")

        if self.email_collector:
            try:
                with self.email_collector:
                    data.email = self.email_collector.get_stats(date)
            except Exception as e:
                print(f"Email data collection failed: {e}")

        # Historical comparison
        if include_comparison:
            data.comparison = self._generate_comparison(data, date)

        return data

    def _generate_comparison(self, current_data: CollectedData, date: str) -> dict:
        """Generate historical comparison data"""
        comparison = {}

        try:
            current_date = datetime.strptime(date, "%Y-%m-%d")

            # Compare with yesterday
            yesterday = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
            yesterday_data = self._collect_light(yesterday)

            comparison["yesterday"] = {
                "git_commits": {
                    "current": current_data.git.total_commits,
                    "previous": yesterday_data.git.total_commits,
                    "change": current_data.git.total_commits - yesterday_data.git.total_commits,
                },
                "todo_completed": {
                    "current": current_data.todo.completed,
                    "previous": yesterday_data.todo.completed,
                    "change": current_data.todo.completed - yesterday_data.todo.completed,
                },
            }

        except Exception:
            pass

        return comparison

    def _collect_light(self, date: str) -> CollectedData:
        """Lightweight collection (only Git and memory)"""
        data = CollectedData(
            date=date,
            collected_at=datetime.now(),
        )

        if self.git_collector:
            data.git = self.git_collector.get_commits(date)

        data.memory = self.memory_collector.collect(date)

        return data
```

---

## Chapter 5 | Work Analysis and Report Generation Module

This chapter integrates the complete implementation of the work analysis engine and the report generator, and is the core processing layer of the daily report generator.

### 5.1 Data Source Collection Instructions
**The script will automatically collect the following data**:

| Data source | Collection method | Configuration location |
| --- | --- | --- |
| **Git repository** | `git log` command | Repository path: `D:/Download/jiuwenclaw` |
| **NetEase email** | IMAP protocol | `.env`: `EMAIL_ADDRESS`, `EMAIL_TOKEN` |
| **Memory system** | Read MD files | `workspace/agent/memory/YYYY-MM-DD.md` |
| **To-do items** | Parse todo.md | `workspace/session/*/todo.md` |

### 5.2 Execution Method
**Important**: This skill collects data by executing a Python script, and must be executed using `mcp_exec_command`:

```bash
# Generate daily report
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py daily --save

# Generate weekly report
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py weekly --save

# Generate monthly report
cd D:/Download/jiuwenclaw && python workspace/agent/skills/daily-report/run_report.py monthly --save
```

### 5.3 Efficiency Metrics Data Structure
```python
# analyzers/work_analyzer.py
# -*- coding: utf-8 -*-
"""
Work analysis engine

Functions:
- Efficiency metrics calculation
- Trend comparison analysis
- Keyword extraction
- Intelligent summary generation
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional


@dataclass
class EfficiencyMetrics:
    """Efficiency metrics"""

    # Task metrics
    task_completion_rate: float = 0.0  # Task completion rate
    tasks_completed: int = 0  # Completed tasks count
    tasks_total: int = 0  # Total tasks count

    # Git metrics
    commit_count: int = 0  # Number of commits
    files_changed: int = 0  # Number of modified files
    lines_added: int = 0  # Number of lines added
    lines_deleted: int = 0  # Number of lines deleted
    net_lines: int = 0  # Number of net added lines

    # Communication metrics
    emails_received: int = 0  # Emails received
    emails_sent: int = 0  # Emails sent

    # Comprehensive metrics
    productivity_score: float = 0.0  # Productivity score (0-100)
    focus_score: float = 0.0  # Focus score (0-100)

    def to_dict(self) -> dict:
        return {
            "task_completion_rate": round(self.task_completion_rate, 2),
            "tasks_completed": self.tasks_completed,
            "tasks_total": self.tasks_total,
            "commit_count": self.commit_count,
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "net_lines": self.net_lines,
            "emails_received": self.emails_received,
            "emails_sent": self.emails_sent,
            "productivity_score": round(self.productivity_score, 2),
            "focus_score": round(self.focus_score, 2),
        }
```

### 5.4 Productivity Score Calculation Logic
```python
class WorkAnalyzer:
    """Work analysis engine"""

    # Stop words list
    STOPWORDS = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
        "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看",
        "自己", "这", "那", "什么", "这个", "那个", "可以", "然后", "还是", "但是",
        "如果", "因为", "所以", "或者", "而且", "已经", "可能", "应该", "需要",
    }

    def _calculate_productivity_score(self, metrics: EfficiencyMetrics) -> float:
        """
        Calculate productivity score

        Scoring rules:
        1. Task completion contribution (highest 40 points)
           - Completion rate * 40

        2. Code contribution (highest 30 points)
           - Number of commits: 5 points each time, up to 15 points
           - Lines of code: 1 point for every 50 lines, up to 15 points

        3. Communication contribution (highest 20 points)
           - Sent emails: 2 points per email, up to 10 points
           - Received emails: 1 point per 5 emails, up to 10 points

        4. Activity contribution (highest 10 points)
           - Any output gets 10 points
        """
        score = 0.0

        # Task completion contribution (highest 40 points)
        score += metrics.task_completion_rate * 40

        # Code contribution (highest 30 points)
        code_score = min(metrics.commit_count * 5, 15)
        code_score += min(metrics.net_lines / 50, 15)
        score += code_score

        # Communication contribution (highest 20 points)
        communication_score = min(metrics.emails_sent * 2, 10)
        communication_score += min(metrics.emails_received / 5, 10)
        score += communication_score

        # Activity contribution (highest 10 points)
        if metrics.commit_count > 0 or metrics.tasks_completed > 0:
            score += 10

        return min(score, 100.0)

    def _calculate_focus_score(self, metrics: EfficiencyMetrics) -> float:
        """
        Calculate focus score

        Scoring rules:
        1. Base score 100 points
        2. Uncompleted task deduction: deduct 5 points for each, maximum deduct 30 points
        3. Commit frequency bonus: reasonable commits (≤5 times) add 10 points
        4. Email interference deduction: more than 20 emails, deduct 0.5 points per email, maximum deduct 20 points
        """
        score = 100.0

        # Deduction for unfinished tasks
        pending_tasks = metrics.tasks_total - metrics.tasks_completed
        score -= min(pending_tasks * 5, 30)

        # Commit frequency (moderately optimal)
        if metrics.commit_count > 0:
            if metrics.commit_count <= 5:
                score += 10
            elif metrics.commit_count > 10:
                score -= 5

        # Email interference deduction
        if metrics.emails_received > 20:
            score -= min((metrics.emails_received - 20) * 0.5, 20)

        return max(score, 0.0)
```

--- 

### 5.5 Report Generator Core Code
```python
# generators/report_generator.py
# -*- coding: utf-8 -*-
"""
Report Generator

Supports:
- Daily report generation
- Weekly report generation (aggregates one week of data)
- Monthly report generation (aggregates one month of data)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..analyzers.work_analyzer import AnalysisResult, WorkAnalyzer
from ..collectors.aggregator import CollectedData, DataAggregator


@dataclass
class ReportConfig:
    """Report configuration"""

    report_type: str = "daily"  # daily, weekly, monthly
    date: str = ""
    include_trends: bool = True
    include_suggestions: bool = True
    output_format: str = "markdown"


class ReportGenerator:
    """Report generator"""

    def __init__(
        self,
        data_aggregator: DataAggregator,
        work_analyzer: Optional[WorkAnalyzer] = None,
    ):
        self.data_aggregator = data_aggregator
        self.work_analyzer = work_analyzer or WorkAnalyzer()

    def generate_daily(self, date: Optional[str] = None) -> str:
        """Generate daily report"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # Collect data
        data = self.data_aggregator.collect(date, include_comparison=True)

        # Analyze data
        analysis = self.work_analyzer.analyze(data.to_dict())

        # Generate report
        return self._render_daily_report(data, analysis)

    def generate_weekly(self, end_date: Optional[str] = None) -> str:
        """Generate weekly report"""
        if end_date is None:
            end_date = datetime.now()
        else:
            end_date = datetime.strptime(end_date, "%Y-%m-%d")

        start_date = end_date - timedelta(days=6)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # Collect one week of data
        week_data = self.data_aggregator.collect_week(end_str)

        # Aggregate week data
        aggregated = self._aggregate_week_data(week_data)

        return self._render_weekly_report(aggregated, start_str, end_str)

    def _render_daily_report(self, data: CollectedData, analysis: AnalysisResult) -> str:
        """Render daily report Markdown"""
        lines = [
            f"# 📋 Work Daily Report - {data.date}",
            "",
            "## 📊 Today’s Overview",
            "",
            "| Metric | Value |",
            "|------|------|",
            f"| Commit Count | {analysis.metrics.commit_count} |",
            f"| Tasks Completed | {analysis.metrics.tasks_completed}/{analysis.metrics.tasks_total} |",
            f"| Code Changes | +{analysis.metrics.lines_added}/-{analysis.metrics.lines_deleted} |",
            f"| Productivity Score | {analysis.metrics.productivity_score:.1f} |",
            "",
        ]

        # Completed tasks
        completed_tasks = [t for t in data.todo.tasks if t.status == "completed"]
        if completed_tasks:
            lines.extend(["## ✅ Completed Tasks", ""])
            for task in completed_tasks[:10]:
                lines.append(f"- {task.content}")
            lines.append("")

        # Git commit records
        if data.git.commits:
            lines.extend([
                "## 💻 Code Commits",
                "",
                "| Time | Commit Message | Change |",
                "|------|----------|------|",
            ])
            for commit in data.git.commits[:10]:
                time_str = commit.date.strftime("%H:%M") if commit.date else "-"
                lines.append(
                    f"| {time_str} | {commit.message[:40]} | "
                    f"+{commit.insertions}/-{commit.deletions} |"
                )
            lines.append("")

        # Trend comparison
        if analysis.trends.vs_yesterday:
            lines.extend(["## 📈 Trend Comparison", ""])
            vs_y = analysis.trends.vs_yesterday
            if "commits" in vs_y:
                change = vs_y["commits"]["change"]
                symbol = "↑" if change > 0 else "↓" if change < 0 else "→"
                lines.append(f"- Commits: {symbol} {abs(change)} times")
            lines.append("")

        # Work suggestions
        if analysis.suggestions:
            lines.extend(["## 💡 Work Suggestions", ""])
            for i, suggestion in enumerate(analysis.suggestions, 1):
                lines.append(f"{i}. {suggestion}")
            lines.append("")

        return "\n".join(lines)
```

### 5.6 Analysis Module Initialization File
```python
# analyzers/__init__.py
# -*- coding: utf-8 -*-
"""
Advanced Version Daily Report Generator - Analysis Module

Contains:
- WorkAnalyzer: work analysis engine
- EfficiencyMetrics: efficiency metrics
- TrendComparison: trend comparison
- AnalysisResult: analysis result
"""

from .work_analyzer import (
    WorkAnalyzer,
    EfficiencyMetrics,
    TrendComparison,
    AnalysisResult,
)

__all__ = [
    "WorkAnalyzer",
    "EfficiencyMetrics",
    "TrendComparison",
    "AnalysisResult",
]
```

### 5.7 Generation Module Initialization File
```python
# generators/__init__.py
# -*- coding: utf-8 -*-
"""
Report generation module

Supports:
- Daily report generation
- Weekly report generation
- Monthly report generation
"""

from .report_generator import ReportGenerator, ReportConfig

__all__ = ["ReportGenerator", "ReportConfig"]
```

### 5.8 Entry Script (run_report.py)
```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily/Weekly/Monthly Report Generation Entry Script (Independent Version)

Usage:
    python run_report.py daily [date]           # Generate daily report
    python run_report.py weekly [end_date]      # Generate weekly report
    python run_report.py monthly [year] [month] # Generate monthly report
"""

import argparse
import io
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

# Fix Windows encoding problems
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Get script directory
SKILL_DIR = Path(__file__).parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent.parent


def collect_git_stats(date: str = None) -> dict:
    """Collect Git submission statistics"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log",
             f"--since={date} 00:00:00",
             f"--until={date} 23:59:59",
             "--format=%H|%s|%an|%ai",
             "--numstat"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30
        )

        commits = []
        total_insertions = 0
        total_deletions = 0

        if result.stdout:
            current_commit = None
            for line in result.stdout.strip().split("\n"):
                if "|" in line and len(line.split("|")) >= 4:
                    parts = line.split("|")
                    if current_commit:
                        commits.append(current_commit)
                    current_commit = {
                        "hash": parts[0][:8],
                        "message": parts[1],
                        "author": parts[2],
                        "insertions": 0,
                        "deletions": 0
                    }
                elif current_commit and "\t" in line:
                    stat_parts = line.split("\t")
                    if len(stat_parts) >= 2:
                        try:
                            ins = int(stat_parts[0]) if stat_parts[0] != "-" else 0
                            dels = int(stat_parts[1]) if stat_parts[1] != "-" else 0
                            current_commit["insertions"] += ins
                            current_commit["deletions"] += dels
                            total_insertions += ins
                            total_deletions += dels
                        except ValueError:
                            pass

            if current_commit:
                commits.append(current_commit)

        return {
            "total_commits": len(commits),
            "total_insertions": total_insertions,
            "total_deletions": total_deletions,
            "commits": commits
        }
    except Exception as e:
        return {"error": str(e)}


def generate_daily_report(date: str = None) -> str:
    """Generate daily report"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # Collect Git data
    git_stats = collect_git_stats(date)

    # Read memory file
    memory_file = PROJECT_ROOT / "workspace" / "agent" / "memory" / f"{date}.md"
    memory_content = ""
    work_items = []

    if memory_file.exists():
        memory_content = memory_file.read_text(encoding="utf-8")
        for line in memory_content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                item = stripped.lstrip("-* ").strip()
                if item and not item.startswith("<!--"):
                    work_items.append(item)

    # Find todo file
    todo_file = None
    session_dir = PROJECT_ROOT / "workspace" / "session"
    if session_dir.exists():
        todo_files = list(session_dir.rglob("todo.md"))
        if todo_files:
            todo_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            todo_file = todo_files[0]

    # Parse todo
    completed_tasks = []
    pending_tasks = []

    if todo_file and todo_file.exists():
        todo_content = todo_file.read_text(encoding="utf-8")
        import re
        for line in todo_content.split("\n"):
            stripped = line.strip()
            # Checkbox format
            match = re.match(r"-\s*\[([xX ])\]\s*(.+)", stripped)
            if match:
                checked = match.group(1).lower() == "x"
                task = match.group(2).strip()
                if checked:
                    completed_tasks.append(task)
                else:
                    pending_tasks.append(task)

    # Generate report
    lines = [
        f"# 📋 Work Daily Report - {date}",
        "",
        "## 📊 Today’s Overview",
        "",
        "| Metric | Value |",
        "|------|------|",
        f"| Code Commits | {git_stats.get('total_commits', 0)} times |",
        f"| Code Changes | +{git_stats.get('total_insertions', 0)}/-{git_stats.get('total_deletions', 0)} |",
        f"| Completed Tasks | {len(completed_tasks)} items |",
        f"| In Progress | {len(pending_tasks)} items |",
        "",
    ]

    # Completed tasks
    if completed_tasks:
        lines.extend(["## ✅ Completed Tasks", ""])
        for task in completed_tasks[:10]:
            lines.append(f"- {task}")
        lines.append("")

    # Code submissions
    if git_stats.get("commits"):
        lines.extend([
            "## 💻 Code Submissions",
            "",
            "| Time | Submission Info | Changes |",
            "|------|----------|------|",
        ])
        for commit in git_stats["commits"][:10]:
            lines.append(
                f"| {commit.get('hash', '-')} | {commit.get('message', '-')[:40]} | "
                f"+{commit.get('insertions', 0)}/-{commit.get('deletions', 0)} |"
            )
        lines.append("")

    # Work records
    if work_items:
        lines.extend(["## 📝 Today’s Work Records", ""])
        for item in work_items[:10]:
            lines.append(f"- {item}")
        lines.append("")

    # Tomorrow plan
    lines.extend(["## 🔜 Tomorrow’s Plan", ""])
    if pending_tasks:
        for task in pending_tasks[:5]:
            lines.append(f"- {task}")
    else:
        lines.append("- To be filled in")
    lines.append("")

    return "\n".join(lines)


def generate_monthly_report(year: int = None, month: int = None) -> str:
    """Generate monthly report"""
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    import calendar
    _, days_in_month = calendar.monthrange(year, month)

    # Collect data for the whole month
    total_commits = 0
    total_insertions = 0
    total_deletions = 0
    active_days = 0

    for day in range(1, days_in_month + 1):
        date = f"{year:04d}-{month:02d}-{day:02d}"
        stats = collect_git_stats(date)
        commits = stats.get("total_commits", 0)
        total_commits += commits
        total_insertions += stats.get("total_insertions", 0)
        total_deletions += stats.get("total_deletions", 0)
        if commits > 0:
            active_days += 1

    # Generate report
    lines = [
        f"# 📋 Work Monthly Report - {year}年{month}月",
        "",
        "## 📊 This Month’s Overview",
        "",
        "| Metric | Value |",
        "|------|------|",
        f"| Active Days | {active_days}/{days_in_month} days |",
        f"| Code Submissions | {total_commits} times |",
        f"| Code Changes | +{total_insertions}/-{total_deletions} |",
        "",
        "## 📝 Work Summary",
        "",
        f"This month completed {total_commits} code submissions,",
        f"Net added code {total_insertions - total_deletions} lines.",
        "",
        "## 🔜 Next Month’s Plan",
        "",
        "- Continue improving project features",
        "",
    ]

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Daily/Weekly/Monthly Report Generator")
    parser.add_argument(
        "type",
        choices=["daily", "weekly", "monthly"],
        help="Report type: daily (daily report), weekly (weekly report), monthly (monthly report)"
    )
    parser.add_argument("--date", "-d", help="Date (YYYY-MM-DD)")
    parser.add_argument("--year", "-y", type=int, help="Year")
    parser.add_argument("--month", "-m", type=int, help="Month")
    parser.add_argument("--save", "-s", action="store_true", help="Save to file")

    args = parser.parse_args()

    try:
        if args.type == "daily":
            date = args.date or datetime.now().strftime("%Y-%m-%d")
            print(f"Generating daily report ({date})...", file=sys.stderr)
            content = generate_daily_report(date)
            date_str = date

        elif args.type == "weekly":
            date = args.date or datetime.now().strftime("%Y-%m-%d")
            print(f"Generating weekly report (as of {date})...", file=sys.stderr)
            # Weekly report temporarily use daily report instead
            content = generate_daily_report(date)
            date_str = date

        elif args.type == "monthly":
            now = datetime.now()
            year = args.year or now.year
            month = args.month or now.month
            print(f"Generating monthly report ({year}年{month}月)...", file=sys.stderr)
            content = generate_monthly_report(year, month)
            date_str = f"{year:04d}-{month:02d}"

        # Output result
        print("\n" + "=" * 50)
        print(content)
        print("=" * 50)

        # Save file
        if args.save:
            reports_dir = PROJECT_ROOT / "workspace" / "agent" / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            filepath = reports_dir / f"{args.type}-{date_str}.md"
            filepath.write_text(content, encoding="utf-8")
            print(f"\nReport saved to: {filepath}", file=sys.stderr)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## Chapter 6｜Configuration and Deployment
### 6.1 Environment Variable Configuration (.env)
The `.env` configuration actually used by this project:

```plain
# Model configuration
MODEL_PROVIDER="OpenAI"
MODEL_NAME="Qwen/Qwen3-235B-A22B-Instruct-2507"
API_BASE="https://api-inference.modelscope.cn/v1"
API_KEY="ms***-bd3e-257a7eef7922"

# Email configuration (NetEase 163 email)
# Note: EMAIL_TOKEN uses the mailbox authorization code, not the login password
# Get方式: log in to 163 email → Settings → POP3/SMTP/IMAP → enable IMAP service → get authorization code
EMAIL_ADDRESS=zx***@163.com
EMAIL_TOKEN=***
EMAIL_PROVIDER=163

# Embedding configuration (optional, used for memory system vectorization)
EMBED_API_KEY=
EMBED_API_BASE=
EMBED_MODEL=

# Other API configuration
JINA_API_KEY=
SERPER_API_KEY=
PERPLEXITY_API_KEY=
```

**Email configuration description**：

+ `EMAIL_ADDRESS`：the complete email address
+ `EMAIL_TOKEN`：email authorization code (not login password); it needs to be obtained after enabling IMAP service in the mailbox settings
+ `EMAIL_PROVIDER`：email provider; currently supports three NetEase mailboxes: `163`、`126`、`yeah`

### 6.2 Heartbeat Configuration (HEARTBEAT.md)
```markdown
# Heartbeat tasks

In this file, configure tasks that JiuwenClaw needs to execute periodically.

---

## Active task items

<!-- After this line, add tasks to be executed, one per line, starting with "- " -->

- Generate today's work daily report

<!-- Weekly report tasks (triggered every Friday) -->
<!-- - Generate this week's work weekly report -->

<!-- Monthly report tasks (triggered at the end of every month) -->
<!-- - Generate this month's work monthly report -->

---

## Completed task items

<!-- Move completed tasks to this section or delete -->

---

## Task description

### Daily report task
- **Trigger time**: Every day 18:00 - 18:30 (configured according to config.yaml)
- **Push target**: Feishu
- **Content**: Today's Git commits, task completion status, email statistics, work efficiency analysis

### Weekly report task
- **Trigger time**: Every Friday 18:00 - 18:30
- **Push target**: Feishu
- **Content**: This week's data aggregation, trend analysis, next week's plan

### Monthly report task
- **Trigger time**: The last day of every month 18:00 - 18:30
- **Push target**: Feishu
- **Content**: This month's data aggregation, achievements summary, next month's plan

---

## Configuration method

Modify the `heartbeat` configuration in `config/config.yaml`：

```yaml
heartbeat:
  every: 3600              # Heartbeat interval (seconds)
  target: feishu           # Push target
  active_hours:
    start: 18:00           # Effective start time
    end: 18:30             # Effective end time
```

**After modification, restart the service for it to take effect.**

```

### 6.3 Git Repository Configuration
The Git repositories monitored by this project (the script will automatically read):

```plain
Repository path: D:/Download/jiuwenclaw
```

**Collection method**：The script collects data through the `git log` command; no additional configuration is needed.

**Collection content**：

+ Commit hash
+ Commit message
+ Commit author, commit time
+ Number of changed files, number of added lines, number of deleted lines

**Execution command**：

```bash
# The git command executed internally by the script
git -C D:/Download/jiuwenclaw log --since="2026-03-07 00:00:00" --until="2026-03-07 23:59:59" --format="%H|%s|%an|%ai" --numstat
```

**Multi-repository support**：If you need to monitor multiple repositories, expand `DataAggregator`：

```python
# Example of extending configuration (needs to be implemented by yourself)
git_repos:
  - path: "D:/Download/jiuwenclaw"
    name: "jiuwenclaw"
  - path: "D:/Projects/another-repo"
    name: "another-project"
```

### 6.4 Feishu Channel Configuration (config.yaml)
The Feishu configuration actually used by this project in `config.yaml`：

```yaml
heartbeat:
  # Heartbeat interval (seconds), default 3600 (1 hour)
  every: 3600
  # The channel for returning heartbeat results
  target: feishu
  # Heartbeat effective time window (local time)
  # Daily report generation will be triggered during 18:00-18:30
  active_hours:
    start: 18:00
    end: 18:30

channels:
  feishu:
    # Feishu application configuration
    # Get方式: Feishu Open Platform → create an enterprise self-built application → get App ID and App Secret
    app_id: cli_a92035b1823a9cd2
    app_secret: *****
    encrypt_key:        # Encryption key (optional)
    verification_token: # Verification token (optional)
    allow_from:         # IP whitelist (optional)
    enabled: true
```

**Feishu application configuration steps**：

1. Access [Feishu Open Platform](https://open.feishu.cn/)
2. Create an enterprise self-built application to get `app_id` and `app_secret`
3. Add “Bot” capability
4. Configure event subscription: `im.message.receive_v1`
5. Publish the app to all members

---

## Chapter 7｜Test and Verification
### 7.1 Test Data Collector
```bash
# Test Git collection (collect commit records for the specified date)
cd D:\Download\jiuwenclaw
python workspace/agent/skills/daily-report/run_report.py daily --date 2026-03-07

# Test monthly report generation (collect data for the whole month)
python workspace/agent/skills/daily-report/run_report.py monthly --year 2026 --month 3

# Test saving to a file
python workspace/agent/skills/daily-report/run_report.py daily --save
```

<!-- This is an image. The OCR content is: ASE)(JIUWENCLAW) PS D:\DOWNLOAD\JIUWE JIUWENCLAW> -MONTH 3 THLY--YEAR 2026 LOAD PYTHON WORKSPACE/AGENT/SKILLS/DAILY-REPORT/RUN REPORT.PY MONTHLY --Y 正在生成月报(2026年3月)... #自工作月报 2026年3月 本月概览 ## 指标数值 活跃天数|3/31天I 代码提交|6次| 代码变更 +73181/-616 工作总结 ## 本月共完成6次代码提交, 净增代码 72565行. 下月计划 ## 继续完善项目功能 -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862327714-d3e2c75f-83bc-46fc-8c25-e95e723f9dee.png)

### 7.2 Complete Test Procedure
#### Step 1：Create a To-do List (test to-do data collection)
Send in Feishu or the Web frontend:

```plain
Help me create a to-do list:
1. Complete daily report generator skill development
2. Implement the Git commit data collection module
3. Implement the email statistics data collection module
4. Configure Feishu channel push
5. Test the heartbeat trigger function
6. Write the development documentation
```

<!-- This is an image. The OCR content is: Help me create a to-do list: 1.Complete daily report generator skill development 2.Implement GIT commit data collection module 3.Implement email statistics data collection module 4.Configure Feishu channel push 5.Test heartbeat trigger function 6.Write development documentation JIUWENCLAW daily report sending It has successfully added 6 new tasks requested by the user to the to-do list, and recorded them in today's memory file. The current to-do list has been updated, including: 1.Complete daily report generator skill development 2.Implement GIT commit data collection module 3.Implement email statistics data collection module 4.Configure Feishu channel push 5.Test heartbeat trigger function 6.Write development documentation The previously completed tasks are kept at the bottom of the list as historical records. The new task list is ready and can be started. -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862423989-5bdc4508-5cfe-4465-86c1-ba696f0246f8.png)

#### Step 2：Simulate Work Records (test memory data collection)
```plain
Help me record today's work:
- In the morning, completed the writing of the SKILL.md skill definition file
- Created the Git commit collector git_collector.py
- Created the email statistics collector email_collector.py
- Created the memory data collector memory_collector.py
- Created the to-do items collector todo_collector.py
- In the afternoon, completed the work analysis engine work_analyzer.py
- Implemented the report generator report_generator.py
- Configured heartbeat and Feishu push
- Conducted functional testing and debugging
```

<!-- This is an image. The OCR content is: Help me record today's work: In the morning, completed the writing of the SKILL.MD skill definition file Created the GIT commit collector GIT_COLLECTOR.PY Created the email statistics collector EMAIL_COLLECTOR.PY Created the memory data collector MEMORY_COLLECTOR.PY Created the to-do items collector TODO_COLLECTOR.PY In the afternoon, completed the work analysis engine WORK_ANALYZER.PY -Implemented the report generator REPORT_GENERATOR.PY Configured heartbeat and Feishu push Conducted functional testing and debugging JIUWENCLAW daily report sending It has successfully recorded today's work content to "MEMORY/2026-03-07.MD", including in the morning the completion of writing the SKILL.MD compiler and the creation of multiple collectors (GIT, COLLECTOR.PY, EMAIL_COLLECTOR.PY, MEMORY_COLLECTOR.PY, TODO_COLLECTOR.PY), as well as completing in the afternoon the work analysis engine and report generator development, and the configuration and functional testing. The record is completed. -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862487637-8206f051-3e56-42b9-acc5-fa6a9d2ae819.png)

<!-- This is an image. The OCR content is: 中口口口 终谈() 编码(日) 转到(G) 远泽(S) 文件() 运行() 直谷(M) 12026-03-07MD U X 发行说明:1.110.0 WORKSPACE>AGENL>MEMORY>因26-03-07MD>-07M 当施行办状态 两户请农创连设为适息,包容区项目标任务:1,充效白招生成员以期开发,2.配置飞手原油铁道, Q 上午完成了SKTLLIND滨与 创建了辅助原本 配出了心跳和飞书 .下午进行了功能测试开差执行 DAILY-REPORT 技能生成今日日报,2826-03-07 节#操作记录 用户请求创建新的消办清单,包含6项任务: REPORTS WORKSPACE ,实现GIT提交数据采集模块 实现邮箱统计数据采集模块 MEMORY 配置飞书频道推送 测试心既触发功能 2026-03-07MD 6.缩与开发文档新增任务 用户创建新的待办清单,包含以下6项任务: 完成日报生威器技能开发 MEMORY.OB SHM 实现GIT提交数据采集楼块 MEMORY.DB-WAL 实现由难统计数据采集模块 CA MEMORYMD 配置飞书须道推送 测试心跳触发功能 编写开发文档 当前待办状态 口 KILLS 插入新任务列表,原任务保留历史记录:工作日志 DAILY-REPORT 上午完成了 SKILL,MD 技能定义文件编与 ANALYZERS 创建了 GIT 提交采集器 GIT COLLECTOR.PY 理了邮件统计采货器EMAIL_COLLECTOR.PY 创建了记亿数据采集器 MENORY COLLECTOR.PY 创建了待办,事项采集器TODO COLLECTOR.PY REPERT_HELPER PY 下午完成了工作分析引引草WORK ANALYZER.PY RUN REPORT.PY 实现了报告生成器 REPORT.GENERATOR.PY SKILL.MD 配置了心跳和飞书推送 LINANCAL-DOCUMENI-PARSER 进行了功能测试和调试 INBNOAL PARSEC.OW SOULMD GIBGNORE BUILDPS1 >大男 行2.列10 空标2 UIF8 CRIF () MSTDOWN BFNITH >* GA MAIN' & ( 0 LICK TO UPDATE IS READY,CLICK TO REST 0乡 FINISH SETUP -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862510145-a395e7f8-075a-4f4c-8757-10d4ba4074d9.png)

#### Step 3：Submit Code (test Git data collection)
```bash
# Submit some code in the project to test Git collection
git add .
git commit -m "feat: Add complete daily report generator function

- Implement multi-data source collection (Git/email/memory/todo)
- Add work analysis engine
- Support daily/weekly/monthly report generation
- Configure Feishu push and heartbeat trigger"
```

<!-- This is an image. The OCR content is: JIUWENCLAW daily report sending JIUWENCLAW daily report sending Bot #Submit some code in the project to test GIT collection GIT ADD. GIT COMMIT -M "FEAT:Add complete daily report generator function Implement multi-data source collection (GIT/email/memory/todo) Add work analysis engine Support daily/weekly/monthly report generation Configure Feishu push and heartbeat trigger" JIUWENCLAW daily report sending It has successfully executed the GIT commit operation: 1. Disabled the prompt information for ignored files 2. Used the -F parameter to forcibly add files ignored by `GITIGNORE" 3. Successfully committed the code, the commit message is: 1 FEAT:Add complete daily report generator function 2 3 -Implement multi-data source collection (GIT/email/memory/todo) 4-Add work analysis engine 5 Support daily/weekly/monthly report generation 6-Configure Feishu push and heartbeat trigger The code submission is completed. -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862545512-3cf0d5fd-ab1e-4d4a-bea2-520a843302f4.png)

<!-- This is an image. The OCR content is: Historical records: Current project's GIT history Commit hash Date Commit message FEAT:Add complete daily report generator function 60ED98E 2026-03-07 FEAT:Optimize Feishu CHANNEL 8DBELCF 2026-03-06 FIX HEARTBEAT C1FE22D 2026-03-05 1D7CBDA 2026-03-05 SOME BUG FIX F21649A 2026-03-03 UPDATE:Update file README.MD 6C0F844 UPDATE:Update file README.MD 2026-03-03 INITIAL COMMIT 2026-03-03 C08E67E Total of 7 commits; the project started from March 3. Today's (March 7) latest commit is FEAT:Add complete daily report generator function. -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772862651273-81909a36-6233-46a6-b35f-2e21f783f7cc.png)

#### Step 4：Generate the Daily Report
```plain
Generate today's daily report
```

<!-- This is an image. The OCR content is: JIUWENCLAW daily report sending JIUWENCLAW daily report sending Bot Heart Generate today's daily report JIUWENCLAW daily report sending Work daily report-2026-03-07 #and ### Values Metrics 1 time code submission +15/-0 code changes 7 items completed tasks 9 items in progress ## Completed tasks B.Execute DAILY-REPORT skill to generate today's daily report|COMPLETED Completed today's daily report has been regenerated, saved path: REPORTS/DAILY-2026-03-07.MD C.Execute DAILY-REPORT according to ability to generate today's daily report COMPLETED Completed today's daily report has been regenerated, saved path: REPORTS/DAILY-2026-03-07.MD J.Complete daily report generator skill development is completed K Configure Feishu channel push COMPLETED |Feishu channel push configuration completed 1.Record today's work log|COMPLETED|Has recorded today's work content to MEMORY/2026-03- 07.MD O.Execute DAILY-REPORT skill to generate today's daily report|COMPLETED Completed today's daily report has been regenerated, saved path: REPORTS/DAILY-2026-03-07.MD P.Execute DAILY-REPORT skill to generate today's daily report|COMPLETED|Generated a draft of today's daily report, saved path: REPORTS/DAILY-2026-03-06.MD Code submission change submission info time FEAT:Add daily report generator complete... +15/-0 60ED98EB ### Daily work record User requested to create a to-do list containing 6 tasks User submitted code to test GIT collection function Successfully executed DAILY-REPORT skill to generate today's daily report, saved path: REPORTS/DAILY-2026-07.MD Tomorrow's plan .Generate today's daily report|WAITING D.Complete daily report generator skill development | WAITING Implement GIT commit data collection module|WAITING +Implement email statistics data collection module|WAITING G Configure Feishu channel push | WAITING Send to JIUWENCLAW daily report sending -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772863455503-33da3fbd-859f-4ea0-8578-7a846c5438c4.png)

#### Step 5：Generate the Monthly Report (test email data collection)
```plain
Read this month's content in the mailbox and organize it into a monthly report
```

<!-- This is an image. The OCR content is: JIUWENCLAW daily report sending Mechanic bot JIUWENCLAW daily report sending Read the month's content in the mailbox and organize it into a monthly report JIUWENCLAW daily report sending It has successfully generated the monthly report for March 2026 for you! Here is the full content: #Work monthly report- March 2026 井井 This month's overview Values Metrics Active days 4/31 days Code submissions 7 times Sender: Alibaba Cloud Time: FRI, 6 MAR 2026 14:42:14+0800 (CST) Content preview: A{COLOR:并1366EC;TEXT-DECORATION:NONE;] This email was sent by Alibaba Cloud, automatically sent by the system, please do not reply directly, thanks! Product solution documentation and community free trial pricing Cloud marketplace to learn more LET'S MAKE SURE THIS IS THE RIGHT EMAIL ADDRESS FOR YOU.PLEASE ENTER THIS VERIFICATION ###Spring recruitment good luck has arrived! Participate in activities to win prizes freely Sender: NetEase Mail Time: WED, 4 MAR 2026 12:45:12+0800 (CST) Content preview: @MEDIA SCREEN AND(MIN-WIDTH:750PX)AND(-WEBKIT-MIN-DEVICE-PIXEL-RATIO:1) [DIV.NTES-EDM-DESKTOP{DISPLAY:BLOCK!IMPORTANT]DIV.NTES-EDM MAHILOFDIENFARNANALIMNNNSTANFTLAMANIS ERREON SND (MIN.ININTH.7GANY) You can log in to Lingxia product market to use: HTTPS://WWW.IXWA... Otherwise, please immediately HREF-"HTTPS://HELP.STEAMPOWERED.COM//EN/WIZARD/HELPWITHLOGIN> your Steam password<... ###Your STEAM account recent changes Sender:'STEAM SUPPORT" Time:TUE,03 MAR 2026 05:16:00-0800 Content preview: QFRZG605, Hello! Time:TUE.03 MAR 2026 05:14:11 -0800 Content preview: GFRZG605, Hello! Below is the code required when changing your STEAM login credentials: Your account verification code is: PWJK8 If you did not try to change your STEAM login credentials on a computer located in TOKYO, Japan, please ignore this email. It may be another user entered incorrect login information. Have a nice day, STEAM Team This notice has been sent to the email address associated with your STEAM account. This email was automatically generated by the system, please... Sent to JIUWENCLAW daily report sending -->
![](https://cdn.nlark.com/yuque/0/2026/png/27326384/1772866734099-2059a8b7-632a-4e42-978b-0bdc80f9c2b5.png) 

## Chapter 9｜Expansion Directions
### 9.1 More Data Sources
| Data source | Collection method | Value |
| --- | --- | --- |
| WeCom/ DingTalk | API | Message communication statistics |
| Schedules/Calendar | CalDAV/iCal | Meeting time analysis |
| Jira/Feishu tasks | API | Project progress tracking |
| Browser history | Local database | Tracing work content |

### 9.2 Smarter Analysis
+ **Work mode recognition**：identify efficient time periods, inefficient time periods
+ **Fatigue level early warning**：based on continuous work duration
+ **Time allocation suggestions**：optimize task priority

### 9.3 Richer Interaction
+ **Feishu button interaction**：edit, regenerate, push
+ **Daily report editing functionality**：save after online modification
+ **Approval process**：Leader review and confirmation

---

## To end with

From the original simple idea—“Can AI help me write daily reports”—to this now complete multi-data source daily report generation system, this project has undergone multiple iterations and optimizations.

The biggest challenge encountered during development was **IMAP protocol adaptation for the 163 email**. NetEase mail security restrictions caused the `SELECT` command to return the “Unsafe Login” error. After repeated debugging and consulting materials, the following solution was ultimately used:

1. **Register ID command**：`imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')`
2. **Send identity declaration**：immediately send the `ID` command after logging in
3. **Use STATUS command**：bypass the SELECT restriction to obtain email statistics

This system can now:
- Automatically collect Git commits, email statistics, memory records, and to-do items
- Generate daily, weekly, and monthly reports
- Push on a schedule through Feishu
- Read email content and generate summaries

If you are also trying to build similar AI Agent applications, I hope this article can provide you with some references.

> **Let the AI Agent truly become an intelligent work assistant, starting with the advanced daily report generator.**
>
> — JiuwenClaw Advanced daily report generator development practice

---

**References**：
- [163 email IMAP ID command solution](https://github.com/HKUDS/nanobot/issues/1123)
- [NetEase email help center](https://help.mail.163.com/)
- [Python imaplib documentation](https://docs.python.org/3/library/imaplib.html)



