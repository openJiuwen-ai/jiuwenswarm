# Beyond Manual Code Review? Building an Automated Review Pipeline with JiuwenClaw

## Introduction — When “code quality” becomes part of daily development

In team development, code review is a key safeguard for quality. In practice, though:

> After every push, reviewers spend a long time on syntax, style, and security, while the conversations that actually matter get buried.

Code review tends to hit three pain points:

1. **Too much repetition** — Syntax, formatting, and basic security checks are repeated by hand every time.
2. **Fragmented tooling** — Linters, security scanners, and complexity tools each run in isolation; results are hard to combine.
3. **No clear metrics** — It is unclear how good the code is; there is no shared score or actionable guidance.

If code review is to improve quality, it needs three capabilities:

1. **Multi-dimensional static analysis** (lint + security + complexity)
2. **Intelligent scoring and suggestions** (quantified assessment + direction for improvement)
3. **Multi-channel reporting** (e.g. Feishu notifications + local reports)

This article walks through:

- How to design a layered analysis stack (Ruff + Radon + Bandit)
- How to implement a composite scoring engine (quality + security + complexity + style)
- How to build flexible report generators (Markdown reports + Feishu cards)
- How to support multiple code sources (local scan + Git clone)
- End-to-end implementation and testing

If you are asking:

- How to move code review from “manual checking” to “intelligent analysis”
- How to give code quality a quantifiable score
- How to build an extensible analysis pipeline

the following sections may offer a useful angle.

---

## Project environment

> **This document reflects a real project.** Configuration and code match what was actually used, not placeholder examples.

### Runtime environment

| Item | Value |
| --- | --- |
| **Project path** | `D:\Download\jiuwenclaw` |
| **OS** | Windows 10 |
| **Python** | 3.10+ |
| **Model service** | Zhipu AI (GLM-4.7) |

### Analysis tools

| Tool | Version | Purpose | Languages |
| --- | --- | --- | --- |
| **Ruff** | 0.4.0+ | Python linting (replaces Pylint/Flake8) | Python |
| **Radon** | 6.0.0+ | Cyclomatic complexity | Python |
| **Bandit** | 1.7.0+ | Security scanning | Python |
| **ESLint** | 9.0+ | Code quality | JavaScript/TypeScript |
| **Checkstyle** | 10.12+ | Style checks | Java |
| **golangci-lint** | latest | General Go checks | Go |
| **Clippy** | latest | Linting | Rust |

### Key file layout

```plain
D:\Download\jiuwenclaw\
├── .env                              # Environment variables
├── workspace/
│   └── agent/
│       ├── reports/code-review/      # Review report output
│       └── skills/code-review/       # Skill module
│           ├── SKILL.md              # Skill definition v2.0.1
│           ├── config.py             # Configuration
│           ├── run_review.py         # Entry script
│           ├── models/               # Data models
│           ├── collectors/           # Code collection
│           ├── analyzers/            # Analysis engine
│           └── generators/           # Report generation
```

## 1. Problem background

### 1.1 Limits of traditional code review

When people say “code review,” they often mean:

> “Isn’t that just PR review — checking correctness and formatting?”

For simple cases, manual review is enough.

In real projects you quickly run into three issues:

1. **Low efficiency**  
   Every review re-checks basics: unused imports, hard-coded secrets, oversized functions — work tools should automate.

2. **Inconsistent standards**  
   Different reviewers care about different things; there is no shared, quantitative bar.

3. **Easy to miss issues**  
   Vulnerabilities such as SQL injection or command injection usually need tooling; manual review misses them often.

Example: fifteen functions, three with complexity over 20 (grade D), two with possible SQL injection — hard to catch everything in one pass.

The **code review assistant** runs three complementary analyzers, feeds a scoring engine, and produces a report with issues and suggestions.

### 1.2 Pain points for developers

Typical quality workflows force you to **run many tools before each commit**: Flake8, Pylint, Bandit — each with its own output format to merge by hand.

Another gap: **you do not know how good the code really is**. Fifty warnings appear — how serious are they? What is the overall quality?

The worst misses are **security issues**: e.g. `os.system(user_input)` overlooked in review but exploitable in production.

With the assistant, Ruff (lint), Radon (complexity), and Bandit (security) run automatically; the engine computes quality, security, complexity, and style scores and returns an overall grade with suggestions.

### 1.3 Why JiuwenClaw’s skill system fits

JiuwenClaw is an open Agent framework; its skill model fits code review tooling:

| Capability | Description |
| --- | --- |
| **Modular skills** | Each skill can bundle multiple Python modules for clean layering |
| **Tool integration** | `allowed_tools` grants access to system tools and external commands |
| **File I/O** | Read/write files for scanning and report generation |
| **Multi-channel delivery** | e.g. Feishu for review summaries |

The main idea is **composability**: collection, static analysis, and reporting are separated with clear boundaries.

## 2. Technical approach

### 2.1 Layered architecture

The code review skill lives in JiuwenClaw’s application layer and uses a classic three-tier layout:

```
┌─────────────────────────────────────────────────────────┐
│                    Application Layer                     │
│                   (code-review skill)                    │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Collectors  │→ │  Analyzers  │→ │ Generators  │     │
│  │  Collection │  │   Analysis  │  │   Reports   │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│        │                │                │              │
│   LocalCollector   RuffAnalyzer    ReportGenerator      │
│   AtomGitClient    RadonAnalyzer   FeishuPublisher      │
│                    BanditAnalyzer                       │
│                    ScoreCalculator                      │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Data flow

End-to-end review flow:

```
User / conversation triggers: review code
         │
         ▼
┌──────────────────┐
│   COLLECTORS     │  Code collection
│  LocalCollector  │  Local directory scan
│  AtomGitClient   │  Git clone
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   ANALYZERS      │  Multi-dimensional analysis
│  RuffAnalyzer    │  Lint → lint_issues
│  RadonAnalyzer   │  Complexity → complexity_issues
│  BanditAnalyzer  │  Security → security_issues
│  ScoreCalculator │  Scoring → ReviewScore
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   GENERATORS     │  Reporting
│  ReportGenerator │  Markdown report
│  FeishuPublisher │  Feishu card
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│    OUTPUT        │
│  Local .md file  │
│  Feishu card     │
└──────────────────┘
```

### 2.3 Core components

| Component | Type | Role | Module |
| --- | --- | --- | --- |
| **LocalCollector** | Collector | Local file collection | `collectors/local_collector.py` |
| **AtomGitClient** | Collector | Git clone and file access | `collectors/atomgit_client.py` |
| **RuffAnalyzer** | Analyzer | Lint | `analyzers/ruff_analyzer.py` |
| **RadonAnalyzer** | Analyzer | Complexity | `analyzers/radon_analyzer.py` |
| **BanditAnalyzer** | Analyzer | Security | `analyzers/bandit_analyzer.py` |
| **ScoreCalculator** | Analyzer | Composite score | `analyzers/score_calculator.py` |
| **ReportGenerator** | Generator | Markdown reports | `generators/report_generator.py` |
| **FeishuPublisher** | Generator | Feishu delivery | `generators/feishu_publisher.py` |

### 2.4 Scoring model

Weighted multi-dimensional scoring:

| Dimension | Weight | Basis |
| --- | --- | --- |
| **Code quality** | 35% | Lint issue count and severity |
| **Security** | 30% | Security findings and severity |
| **Complexity** | 20% | Cyclomatic complexity and high-complexity functions |
| **Style** | 15% | Style issues |

Overall score maps to grades:

| Grade | Range | Meaning |
| --- | --- | --- |
| A | 90–100 | Excellent |
| B | 80–89 | Good |
| C | 70–79 | Acceptable |
| D | 60–69 | Needs improvement |
| F | Below 60 | Fail |

## Chapter 3 — Engineering the Skills layout

### 3.1 Directory structure

```plain
workspace/agent/skills/code-review/
├── SKILL.md                    # Skill definition (required)
├── config.py                   # Configuration
├── run_review.py               # Entry script
│
├── models/                     # Data models
│   ├── __init__.py
│   ├── code_issue.py
│   ├── code_metrics.py
│   ├── review_result.py
│   └── review_score.py
│
├── collectors/                 # Collection layer
│   ├── __init__.py
│   ├── local_collector.py
│   └── atomgit_client.py
│
├── analyzers/                  # Analysis layer
│   ├── __init__.py
│   ├── ruff_analyzer.py
│   ├── radon_analyzer.py
│   ├── bandit_analyzer.py
│   ├── ast_analyzer.py
│   └── score_calculator.py
│
└── generators/                 # Report layer
    ├── __init__.py
    ├── report_generator.py
    └── feishu_publisher.py
```

### 3.2 SKILL.md definition (v2.0.1)

```markdown
---
name: code-review
version: 2.0.1
description: Multi-language code review assistant for Python/JavaScript/Java/Go/Rust; quality, security, and complexity
tags: [code, review, python, javascript, typescript, java, go, rust, quality, security]
allowed_tools: [mcp_exec_command, read_file, write_file]
---

# Multi-language code review assistant

Review code in arbitrary Git repositories across multiple languages; detect security, quality, and complexity issues.

## Supported languages

| Language | Lint | Security | Complexity |
|----------|------|----------|------------|
| **Python** | Ruff | Bandit | Radon |
| **JavaScript/TypeScript** | ESLint | eslint-plugin-security | — |
| **Java** | Checkstyle | — | — |
| **Go** | golangci-lint | gosec | gocyclo |
| **Rust** | Clippy | cargo-audit | — |

## Usage

### Review a remote Git repository

```bash
cd D:/Download/jiuwenclaw && python workspace/agent/skills/code-review/run_review.py clone --url <repo-url>
```

### Review local code

```bash
cd D:/Download/jiuwenclaw && python workspace/agent/skills/code-review/run_review.py local --path <path>
```

## Important limits

**Do not read and return the full report file.**

Large repositories can produce tens of thousands of lines, which can:

- Exceed WebSocket message size (1MB limit)
- Fail to send, so the user sees nothing

**Correct approach:**

1. Extract a summary from command output
2. Return a short Feishu-friendly summary
3. Tell the user the detailed report was saved

## Feishu-friendly summary format

```
📊 Code review report

━━━━━━━━━━━━━━━━━━━━━━
📦 Repo: <name>
📁 Files scanned: <count>
⏰ Review time: <time>
━━━━━━━━━━━━━━━━━━━━━━

📋 Issue summary

🔴 Errors: <count>
🟡 Warnings: <count>
🔵 Info: <count>

💡 Suggestions

1. <suggestion 1>
2. <suggestion 2>

━━━━━━━━━━━━━━━━━━━━━━
📄 Full report saved
```

## Score grades

| Grade | Score | Status | Icon |
|-------|-------|--------|------|
| A | 90-100 | Excellent | 🟢 |
| B | 80-89 | Good | 🔵 |
| C | 70-79 | Acceptable | 🟡 |
| D | 60-69 | Needs work | 🟠 |
| F | Below 60 | Fail | 🔴 |
```

### 3.3 Report template example

```markdown
# :white_check_mark: Code review report

| Field | Value |
|-------|-------|
| Review type | clone |
| Target | `owner/repo` |
| Review time | 2026-03-08 10:30 |
| Files scanned | 25 |

## Scores

| Dimension | Score |
|-----------|-------|
| **Overall** | **72.5** (C) |
| Code quality | 65.0 |
| Security | 80.0 |
| Complexity | 85.0 |
| Style | 70.0 |

**Status**: Pass

## Issue summary

- Total: **45** issues
- Errors: 3
- Warnings: 28
- Info: 14

## Issue details

### Security (5)

- `app.py:42` [B608] Possible SQL injection vector
  > SQL injection risk

### Code quality (32)

- `utils.py:15` [F401] `os` imported but unused
  > Remove unused import

### Complexity (8)

- `service.py:120` [CD] Function 'process_data' complexity too high (23, grade D)
  > Refactor strongly recommended: split into smaller functions

## Suggestions

1. Three high-severity security issues — prioritize fixes
2. Five high-complexity functions — consider refactoring
3. Fix three error-level issues

---
*Report generated at: 2026-03-08 10:30:15*
```

## Chapter 4 — Data models

### 4.1 `CodeIssue` (`models/code_issue.py`)

```python
# models/code_issue.py
# -*- coding: utf-8 -*-
"""
Code issue model

Represents a single issue with location, severity, category, etc.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CodeIssue:
    """A single code issue."""

    file: str                    # File path
    line: int                    # Line number
    severity: str                # error / warning / info
    category: str                # lint / complexity / security / style
    message: str                 # Description
    rule: str                    # Rule id (e.g. E501, B101)
    source: str                  # Tool (ruff / bandit / radon)
    column: int = 0
    suggestion: str = ""         # Fix suggestion

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "rule": self.rule,
            "source": self.source,
            "suggestion": self.suggestion,
        }

    def __str__(self) -> str:
        location = f"{self.file}:{self.line}"
        return f"[{self.rule}] {location} - {self.message}"
```

### 4.2 `CodeMetrics` (`models/code_metrics.py`)

```python
# models/code_metrics.py
# -*- coding: utf-8 -*-
"""
Code metrics model

Line counts, function counts, complexity, etc.
"""

from dataclasses import dataclass


@dataclass
class CodeMetrics:
    """Aggregated code metrics."""

    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0

    avg_complexity: float = 0.0
    max_complexity: int = 0
    complexity_rank: str = "A"
    high_complexity_count: int = 0

    maintainability_index: float = 0.0
    maintainability_rank: str = "A"

    function_count: int = 0
    class_count: int = 0
    file_count: int = 0

    def to_dict(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "code_lines": self.code_lines,
            "comment_lines": self.comment_lines,
            "avg_complexity": round(self.avg_complexity, 2),
            "max_complexity": self.max_complexity,
            "complexity_rank": self.complexity_rank,
            "high_complexity_count": self.high_complexity_count,
            "function_count": self.function_count,
            "class_count": self.class_count,
            "file_count": self.file_count,
        }
```

### 4.3 `ReviewScore` (`models/review_score.py`)

```python
# models/review_score.py
# -*- coding: utf-8 -*-
"""
Review score model

Multi-dimensional scores and grade mapping.
"""

from dataclasses import dataclass


@dataclass
class ReviewScore:
    """Scores for a review run."""

    overall: float = 0.0          # Overall 0–100
    quality_score: float = 0.0
    security_score: float = 0.0
    complexity_score: float = 0.0
    style_score: float = 0.0

    grade: str = "C"              # A/B/C/D/F
    passed: bool = False

    @staticmethod
    def score_to_grade(score: float) -> str:
        if score >= 90:
            return "A"
        elif score >= 80:
            return "B"
        elif score >= 70:
            return "C"
        elif score >= 60:
            return "D"
        else:
            return "F"

    def to_dict(self) -> dict:
        return {
            "overall": round(self.overall, 1),
            "quality_score": round(self.quality_score, 1),
            "security_score": round(self.security_score, 1),
            "complexity_score": round(self.complexity_score, 1),
            "style_score": round(self.style_score, 1),
            "grade": self.grade,
            "passed": self.passed,
        }
```

## Chapter 5 — Analysis engine

### 5.1 Ruff analyzer

```python
# analyzers/ruff_analyzer.py
# -*- coding: utf-8 -*-
"""
Ruff lint analyzer

Uses Ruff for quality checks — typically 10–100× faster than Pylint/Flake8.
"""

import json
import subprocess
from pathlib import Path
from typing import List, Optional

try:
    from ..models import CodeIssue
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from models import CodeIssue


class RuffAnalyzer:
    """Ruff-based lint analyzer."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    def analyze_code(self, code: str, filename: str = "temp.py") -> List[CodeIssue]:
        """
        Analyze a code string.

        Uses stdin to avoid temp files. On Windows, pass bytes to avoid GBK issues.
        """
        try:
            result = subprocess.run(
                ["ruff", "check", "--output-format", "json",
                 "--stdin-filename", filename, "-"],
                input=code.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            return self._parse_output(
                result.stdout.decode("utf-8", errors="replace"),
                filename
            )
        except subprocess.TimeoutExpired:
            return [CodeIssue(
                file=filename, line=0, severity="error",
                category="lint", message="Ruff analysis timed out",
                rule="TIMEOUT", source="ruff"
            )]
        except FileNotFoundError:
            return [CodeIssue(
                file=filename, line=0, severity="warning",
                category="lint", message="Ruff not installed; run: pip install ruff",
                rule="MISSING", source="ruff"
            )]

    def analyze_directory(self, dirpath: str,
                          exclude_patterns: Optional[List[str]] = None) -> List[CodeIssue]:
        """Analyze all Python files under a directory."""
        path = Path(dirpath)
        if not path.exists():
            return []

        exclude_args = []
        if exclude_patterns:
            for pattern in exclude_patterns:
                exclude_args.extend(["--exclude", pattern])

        cmd = ["ruff", "check", "--output-format", "json", str(path)]
        cmd.extend(exclude_args)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return self._parse_output(result.stdout, str(path))

    def _parse_output(self, output: str, default_file: str = "") -> List[CodeIssue]:
        issues = []
        if not output.strip():
            return issues

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return issues

        for item in data:
            location = item.get("location", {})
            issues.append(CodeIssue(
                file=item.get("filename", default_file),
                line=location.get("row", 0),
                column=location.get("column", 0),
                severity=self._map_severity(item.get("severity", "warning")),
                category="lint",
                message=item.get("message", ""),
                rule=item.get("code", ""),
                source="ruff",
                suggestion=self._get_suggestion(item.get("code", ""), item.get("message", "")),
            ))
        return issues

    def _map_severity(self, ruff_severity: str) -> str:
        severity_map = {
            "error": "error", "warning": "warning", "info": "info",
            "fatal": "error", "convention": "info", "refactor": "info",
        }
        return severity_map.get(ruff_severity.lower(), "warning")

    def _get_suggestion(self, rule: str, message: str) -> str:
        suggestions = {
            "E501": "Break long lines into multiple lines using parentheses or backslash",
            "F401": "Remove unused imports",
            "F841": "Remove unused variables",
            "E711": "Use 'is None' instead of '== None'",
            "E722": "Catch specific exceptions instead of bare except",
        }
        return suggestions.get(rule, "")
```

### 5.2 Radon complexity analyzer

```python
# analyzers/radon_analyzer.py
# -*- coding: utf-8 -*-
"""
Radon complexity analyzer

CC, raw metrics, maintainability index (MI).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:
    from radon.complexity import cc_rank, cc_visit
    from radon.metrics import mi_visit
    from radon.raw import analyze as raw_analyze
    RADON_AVAILABLE = True
except ImportError:
    RADON_AVAILABLE = False
    cc_rank = cc_visit = mi_visit = raw_analyze = None

try:
    from ..models import CodeIssue, CodeMetrics
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from models import CodeIssue, CodeMetrics


@dataclass
class ComplexityResult:
    """Single complexity result."""
    name: str
    type: str           # function / method / class
    complexity: int
    rank: str           # A–F
    line: int
    file: str = ""
    classname: str = ""


class RadonAnalyzer:
    """Radon-based complexity analyzer."""

    COMPLEXITY_THRESHOLDS = {
        "A": (1, 5),
        "B": (6, 10),
        "C": (11, 20),
        "D": (21, 30),
        "E": (31, 40),
        "F": (41, 100),
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.max_complexity = self.config.get("max_complexity", 10)

    def analyze_complexity(self, code: str, filename: str = "") -> List[ComplexityResult]:
        if not RADON_AVAILABLE:
            return []

        results = []
        try:
            cc_results = cc_visit(code)
            for item in cc_results:
                complexity = item.complexity
                rank = cc_rank(complexity)
                results.append(ComplexityResult(
                    name=item.name,
                    type="method" if item.is_method else "function",
                    complexity=complexity,
                    rank=rank,
                    line=item.lineno,
                    file=filename,
                    classname=item.classname or "",
                ))
        except Exception:
            pass
        return results

    def analyze_metrics(self, code: str) -> CodeMetrics:
        metrics = CodeMetrics()
        if not RADON_AVAILABLE:
            return metrics

        try:
            raw = raw_analyze(code)
            metrics.total_lines = raw.loc
            metrics.code_lines = raw.sloc
            metrics.comment_lines = raw.comments
            metrics.blank_lines = raw.blank

            mi = mi_visit(code, True)
            if isinstance(mi, (int, float)):
                metrics.maintainability_index = mi
                metrics.maintainability_rank = self._mi_to_rank(mi)
        except Exception:
            pass
        return metrics

    def get_high_complexity_issues(self, complexity_results: List[ComplexityResult]) -> List[CodeIssue]:
        issues = []
        for result in complexity_results:
            if result.complexity > self.max_complexity:
                severity = "error" if result.rank in ["D", "E", "F"] else "warning"
                issues.append(CodeIssue(
                    file=result.file,
                    line=result.line,
                    severity=severity,
                    category="complexity",
                    message=f"Function '{result.name}' complexity too high ({result.complexity}, rank {result.rank})",
                    rule=f"C{result.rank}",
                    source="radon",
                    suggestion=self._get_complexity_suggestion(result),
                ))
        return issues

    def _get_complexity_suggestion(self, result: ComplexityResult) -> str:
        suggestions = {
            "C": "Consider splitting the function to reduce branches",
            "D": "Refactor strongly: split into smaller functions",
            "E": "Must refactor — hard to maintain and test",
            "F": "Must refactor immediately — nearly unmaintainable",
        }
        return suggestions.get(result.rank, "Consider reducing function complexity")
```

### 5.3 Bandit security analyzer

```python
# analyzers/bandit_analyzer.py
# -*- coding: utf-8 -*-
"""
Bandit security analyzer

Detects SQL injection, command injection, hard-coded secrets, weak crypto, etc.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

try:
    from ..models import CodeIssue
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from models import CodeIssue


class BanditAnalyzer:
    """Bandit-based security analyzer."""

    SECURITY_SUGGESTIONS = {
        "B101": "assert may be stripped in optimized mode",
        "B105": "Possible hard-coded password string",
        "B110": "try/except catches all exceptions",
        "B303": "Insecure hash (MD5/SHA1)",
        "B311": "random not suitable for crypto",
        "B324": "Certificate not verified",
        "B404": "subprocess usage may be unsafe",
        "B501": "requests without verifying certificate",
        "B506": "Unsafe YAML load",
        "B601": "subprocess command injection risk",
        "B603": "subprocess command injection risk",
        "B605": "os.system command injection risk",
        "B606": "os.popen command injection risk",
        "B608": "Possible SQL injection",
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    def analyze_code(self, code: str, filename: str = "temp.py") -> List[CodeIssue]:
        """Bandit has no stdin support — use a temp file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            temp_path = f.name

        try:
            return self.analyze_file(temp_path, display_filename=filename)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def analyze_file(self, filepath: str, display_filename: Optional[str] = None) -> List[CodeIssue]:
        path = Path(filepath)
        if not path.exists():
            return [CodeIssue(
                file=filepath, line=0, severity="error",
                category="security", message=f"File not found: {filepath}",
                rule="NOT_FOUND", source="bandit"
            )]

        try:
            result = subprocess.run(
                ["bandit", "-f", "json", "-r", str(path)],
                capture_output=True, text=True, timeout=60
            )
            return self._parse_output(result.stdout, display_filename or str(path))
        except FileNotFoundError:
            return [CodeIssue(
                file=filepath, line=0, severity="warning",
                category="security", message="Bandit not installed; run: pip install bandit",
                rule="MISSING", source="bandit"
            )]

    def _parse_output(self, output: str, default_file: str = "") -> List[CodeIssue]:
        issues = []
        if not output.strip():
            return issues

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return issues

        for item in data.get("results", []):
            test_id = item.get("test_id", "")
            issues.append(CodeIssue(
                file=item.get("filename", default_file),
                line=item.get("line_number", 0),
                severity=self._map_severity(item.get("issue_severity", "MEDIUM")),
                category="security",
                message=item.get("issue_text", ""),
                rule=test_id,
                source="bandit",
                suggestion=self.SECURITY_SUGGESTIONS.get(test_id, ""),
            ))
        return issues

    def _map_severity(self, bandit_severity: str) -> str:
        severity_map = {"HIGH": "error", "MEDIUM": "warning", "LOW": "info"}
        return severity_map.get(bandit_severity.upper(), "warning")
```

### 5.4 Score calculator

```python
# analyzers/score_calculator.py
# -*- coding: utf-8 -*-
"""
Composite score calculator

Weighted multi-dimensional scoring from collected issues.
"""

from dataclasses import dataclass
from typing import List, Optional

try:
    from ..models import CodeIssue, CodeMetrics, ReviewResult, ReviewScore
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from models import CodeIssue, CodeMetrics, ReviewResult, ReviewScore


@dataclass
class ScoringWeights:
    quality: float = 0.35
    security: float = 0.30
    complexity: float = 0.20
    style: float = 0.15


class ScoreCalculator:
    """Computes ReviewScore from a ReviewResult."""

    DEFAULT_WEIGHTS = ScoringWeights()

    SEVERITY_PENALTY = {
        "error": 5.0,
        "warning": 2.0,
        "info": 0.5,
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.pass_threshold = self.config.get("pass_threshold", 60.0)
        self.max_complexity = self.config.get("max_complexity", 10)
        self.weights = self.config.get("weights", self.DEFAULT_WEIGHTS)

    def calculate(self, result: ReviewResult) -> ReviewScore:
        score = ReviewScore()

        score.quality_score = self._calc_quality_score(result.lint_issues)
        score.security_score = self._calc_security_score(result.security_issues)
        score.complexity_score = self._calc_complexity_score(result.metrics)
        score.style_score = self._calc_style_score(result.style_issues)

        score.overall = (
            score.quality_score * self.weights.quality +
            score.security_score * self.weights.security +
            score.complexity_score * self.weights.complexity +
            score.style_score * self.weights.style
        )

        score.grade = ReviewScore.score_to_grade(score.overall)
        score.passed = score.overall >= self.pass_threshold

        return score

    def _calc_quality_score(self, issues: List[CodeIssue]) -> float:
        score = 100.0
        for issue in issues:
            penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            score -= penalty
        return max(0.0, min(100.0, score))

    def _calc_security_score(self, issues: List[CodeIssue]) -> float:
        score = 100.0
        severity_multiplier = {"error": 15.0, "warning": 8.0, "info": 2.0}

        for issue in issues:
            base_penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            multiplier = severity_multiplier.get(issue.severity, 1.0)
            score -= base_penalty * (multiplier / 5)

        has_critical = any(i.severity == "error" for i in issues)
        min_score = 20.0 if has_critical else 0.0
        return max(min_score, min(100.0, score))

    def _calc_complexity_score(self, metrics: CodeMetrics) -> float:
        score = 100.0

        if metrics.avg_complexity > 5:
            score -= (metrics.avg_complexity - 5) * 5
        if metrics.high_complexity_count > 0:
            score -= metrics.high_complexity_count * 3

        return max(0.0, min(100.0, score))

    def _calc_style_score(self, issues: List[CodeIssue]) -> float:
        score = 100.0
        for issue in issues:
            penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            score -= penalty * 0.5
        return max(0.0, min(100.0, score))

    def get_improvement_suggestions(self, result: ReviewResult) -> List[str]:
        suggestions = []

        if result.score.quality_score < 70:
            suggestions.append("Code quality is low; fix lint errors and warnings first")

        if result.score.security_score < 70:
            critical = [i for i in result.security_issues if i.severity == "error"]
            if critical:
                suggestions.append(f"{len(critical)} high-severity security issues — prioritize fixes")

        if result.score.complexity_score < 70:
            if result.metrics.high_complexity_count > 0:
                suggestions.append(
                    f"{result.metrics.high_complexity_count} high-complexity functions — consider splitting"
                )

        if result.errors > 0:
            suggestions.append(f"Fix {result.errors} error-level issues")

        if result.score.overall >= 90:
            suggestions.append("Excellent code quality — keep it up")

        return suggestions[:5]
```

## Chapter 6 — Collection and report generation

### 6.1 Local collector

```python
# collectors/local_collector.py
# -*- coding: utf-8 -*-
"""
Local code collector

Scans a directory and collects Python files.
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class FileInfo:
    path: str
    relative_path: str
    size: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "size": self.size,
        }


@dataclass
class ScanResult:
    files: List[FileInfo] = field(default_factory=list)
    total_files: int = 0
    total_size: int = 0
    errors: List[str] = field(default_factory=list)


class LocalCollector:
    """Collects Python files from a local tree."""

    DEFAULT_EXCLUDES = [
        "**/test_*.py",
        "**/*_test.py",
        "**/tests/**",
        "**/venv/**",
        "**/.venv/**",
        "**/__pycache__/**",
        "**/node_modules/**",
        "**/.git/**",
    ]

    def __init__(
        self,
        path: str,
        exclude_patterns: Optional[List[str]] = None,
        max_file_size: int = 1024 * 1024,
    ):
        self.path = Path(path).resolve()
        self.exclude_patterns = exclude_patterns or self.DEFAULT_EXCLUDES
        self.max_file_size = max_file_size

    def collect(self) -> ScanResult:
        result = ScanResult()

        if not self.path.exists():
            result.errors.append(f"Path does not exist: {self.path}")
            return result

        for py_file in self.path.rglob("*.py"):
            if self._should_exclude(py_file):
                continue

            size = py_file.stat().st_size
            if size > self.max_file_size:
                continue

            result.files.append(FileInfo(
                path=str(py_file),
                relative_path=str(py_file.relative_to(self.path)),
                size=size,
            ))
            result.total_size += size

        result.total_files = len(result.files)
        return result

    def get_file_content(self, filepath: str) -> Optional[str]:
        try:
            return Path(filepath).read_text(encoding="utf-8")
        except Exception:
            return None

    def _should_exclude(self, filepath: Path) -> bool:
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(str(filepath), pattern):
                return True
            if fnmatch.fnmatch(filepath.name, pattern):
                return True
        return False
```

### 6.2 Git clone flow (`run_review.py`)

```python
def review_clone(repo_url: str, config: CodeReviewConfig, branch: str = "main") -> ReviewResult:
    """
    Clone a Git repository and run the review.

    Works with any remote (AtomGit, GitHub, Gitee, etc.).
    """
    import shutil
    import subprocess
    import tempfile

    parsed = parse_repo_url(repo_url)
    repo_name = f"{parsed[0]}/{parsed[1]}" if parsed[0] and parsed[1] else repo_url

    result = ReviewResult(review_type="clone", target=repo_name)

    temp_dir = tempfile.mkdtemp(prefix="code_review_")

    try:
        clone_cmd = [
            "git", "clone",
            "--depth", "1",
            "--branch", branch,
            "--single-branch",
            repo_url,
            temp_dir
        ]

        proc = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=300)

        if proc.returncode != 0:
            clone_cmd = ["git", "clone", "--depth", "1", repo_url, temp_dir]
            proc = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=300)

            if proc.returncode != 0:
                result.suggestions = [f"Clone failed: {proc.stderr}"]
                return result

        scan_result = review_local(temp_dir, config)
        scan_result.review_type = "clone"
        scan_result.target = repo_name
        return scan_result

    except subprocess.TimeoutExpired:
        result.suggestions = ["Clone timed out — repository may be too large"]
        return result
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
```

### 6.3 Report generator

```python
# generators/report_generator.py
# -*- coding: utf-8 -*-
"""
Markdown report generator for code review results.
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import ReviewResult


class ReportGenerator:
    def __init__(self, output_dir: str = "workspace/agent/reports/code-review"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, result: "ReviewResult") -> str:
        lines = []

        icon = ":white_check_mark:" if result.score.passed else ":x:"
        lines.append(f"# {icon} Code review report")
        lines.append("")

        lines.append("| Field | Value |")
        lines.append("|------|-----|")
        lines.append(f"| Review type | {result.review_type} |")
        lines.append(f"| Target | `{result.target}` |")
        lines.append(f"| Review time | {result.reviewed_at.strftime('%Y-%m-%d %H:%M')} |")
        lines.append(f"| Files scanned | {result.files_scanned} |")
        lines.append("")

        lines.append("## Scores")
        lines.append("")
        lines.append("| Dimension | Score |")
        lines.append("|------|------|")
        lines.append(f"| **Overall** | **{result.score.overall:.1f}** ({result.score.grade}) |")
        lines.append(f"| Code quality | {result.score.quality_score:.1f} |")
        lines.append(f"| Security | {result.score.security_score:.1f} |")
        lines.append(f"| Complexity | {result.score.complexity_score:.1f} |")
        lines.append(f"| Style | {result.score.style_score:.1f} |")
        lines.append("")
        lines.append(f"**Status**: {'Pass' if result.score.passed else 'Fail'}")
        lines.append("")

        lines.append("## Issue summary")
        lines.append("")
        lines.append(f"- Total: **{result.total_issues}** issues")
        lines.append(f"- Errors: {result.errors}")
        lines.append(f"- Warnings: {result.warnings}")
        lines.append(f"- Info: {result.infos}")
        lines.append("")

        lines.append("## Issue details")
        lines.append("")

        if result.total_issues == 0:
            lines.append("No issues found.")
        else:
            if result.security_issues:
                lines.append(f"### Security ({len(result.security_issues)})")
                lines.append("")
                for issue in result.security_issues[:10]:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                if len(result.security_issues) > 10:
                    lines.append(f"- ... and {len(result.security_issues) - 10} more")
                lines.append("")

            if result.lint_issues:
                lines.append(f"### Code quality ({len(result.lint_issues)})")
                lines.append("")
                for issue in result.lint_issues[:10]:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                if len(result.lint_issues) > 10:
                    lines.append(f"- ... and {len(result.lint_issues) - 10} more")
                lines.append("")

            if result.complexity_issues:
                lines.append(f"### Complexity ({len(result.complexity_issues)})")
                lines.append("")
                for issue in result.complexity_issues:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                lines.append("")

        lines.append("## Code metrics")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|------|-----|")
        lines.append(f"| Total lines | {result.metrics.total_lines} |")
        lines.append(f"| Code lines | {result.metrics.code_lines} |")
        lines.append(f"| Comment lines | {result.metrics.comment_lines} |")
        lines.append(f"| Functions | {result.metrics.function_count} |")
        lines.append(f"| Classes | {result.metrics.class_count} |")
        lines.append(f"| Avg complexity | {result.metrics.avg_complexity:.1f} |")
        lines.append(f"| Max complexity | {result.metrics.max_complexity} |")
        lines.append("")

        lines.append("## Suggestions")
        lines.append("")
        for i, suggestion in enumerate(result.suggestions, 1):
            lines.append(f"{i}. {suggestion}")
        lines.append("")

        lines.append("---")
        lines.append(f"*Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        return "\n".join(lines)

    def save_report(self, content: str, result: "ReviewResult") -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = result.target.replace("/", "_").replace("\\", "_").replace(":", "_")
        filename = f"review_{result.review_type}_{target}_{timestamp}.md"
        filepath = self.output_dir / filename

        filepath.write_text(content, encoding="utf-8")
        return str(filepath)
```

## Chapter 7 — Configuration and deployment

### 7.1 Environment variables

> **This project uses the project-directory layout**: config lives under the repo:
> - Env file: `D:\Download\jiuwenclaw\.env`
> - Skills: `D:\Download\jiuwenclaw\workspace\agent\skills\`
>
> That keeps everything version-controlled for PRs.

Example `.env` at the project root:

```env
# Model (Zhipu GLM-4.7 via OpenAI-compatible client)
MODEL_PROVIDER="OpenAI"
MODEL_NAME="glm-4.7"
API_BASE="https://open.bigmodel.cn/api/paas/v4"
API_KEY="your-api-key"

# Feishu (optional)
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_REVIEW_CHAT_ID=oc_xxx

# AtomGit (no public API in this setup — use git clone)
ATOMGIT_TOKEN=xxx
```

### 7.2 JiuwenClaw `config.yaml`

> Config path: `D:\Download\jiuwenclaw\config\config.yaml`

```yaml
react:
  agent_name: main_agent
  max_iterations: 50
  model_name: ${MODEL_NAME:-glm-4.7}
  answer_chunk_size: 500
  stream_chunk_threshold: 50
  stream_character_threshold: 2000
  model_client_config:
    client_provider: OpenAI
    api_base: https://open.bigmodel.cn/api/paas/v4
    api_key: ${API_KEY}
    verify_ssl: false
  model_config_obj:
  context_engine_config:
    enable_reload: true
  evolution:
    enabled: true
    auto_scan: false
    skill_base_dir: "workspace/agent/skills"

tools:
  - todo
  - skill

channels:
  feishu:
    app_id: cli_xxx
    app_secret: xxx
    enabled: true
```

| Key | Meaning |
|-----|---------|
| `client_provider: OpenAI` | OpenAI-compatible client |
| `api_base` | Zhipu API base URL |
| `model_name` | Model id |
| `channels.feishu.enabled` | Enable Feishu channel |

### 7.3 Installing dependencies

```bash
pip install ruff radon bandit

npm install -g eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin

go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
go install github.com/securego/gosec/v2/cmd/gosec@latest

rustup component add clippy
cargo install cargo-audit

mkdir -p tools
curl -L "https://github.com/checkstyle/checkstyle/releases/download/checkstyle-10.12.5/checkstyle-10.12.5-all.jar" -o tools/checkstyle.jar

pip install lark-oapi
```

### 7.4 Heartbeat

In `HEARTBEAT.md`, schedule periodic scans:

```markdown
## Active tasks
- Run code quality scan  # daily
```

## Chapter 8 — Testing

| Method | Notes |
|--------|------|
| Feishu chat | Send review requests to the bot in private chat |
| Web UI | Send requests through the JiuwenClaw web app |

### 8.1 Feishu

Prerequisites: JiuwenClaw running, Feishu bot + WebSocket, `code-review` skill deployed.

Example message:

```
https://atomgit.com/openJiuwen/deepsearch please review this repository
```

The agent loads the skill, clones, scans (Ruff + Radon + Bandit), generates a report, and replies on Feishu.

### 8.2 Web UI

Prerequisites: `python start_services.py web`, open the app (default `http://localhost:8000`), skill deployed.

You get streaming thought traces and intermediate steps — useful for debugging.

### 8.3 Sample report output

```markdown
# :x: Code review report

| Field | Value |
|------|-------|
| Review type | local |
| Target | `./jiuwenclaw` |
| Review time | 2026-03-07 21:53 |
| Files scanned | 110 |

## Scores

| Dimension | Score |
|------|------|
| **Overall** | **41.0** (F) |
| Code quality | 0.0 |
| Security | 20.0 |
| Complexity | 100.0 |
| Style | 100.0 |

**Status**: Fail

## Issue summary

- Total: **245** issues
- Errors: 5
- Warnings: 184
- Info: 56

## Issue details

### Security (72)

- `app.py:130` [B105] Possible hardcoded password: 'EMAIL_TOKEN'
  > Hard-coded password string
- `database.py:654` [B608] Possible SQL injection vector
  > SQL injection risk

### Code quality (152)

- `interface.py:10` [F401] `re` imported but unused
  > Remove unused import

### Complexity (21)

- `app.py:720` [CD] Function '_run' complexity too high (23, grade D)
  > Refactor strongly: split into smaller functions

## Suggestions

1. Code quality is low — fix lint errors and warnings first
2. Two high-severity security issues — prioritize
3. Fix five error-level issues
```

## Chapter 9 — Takeaways

### 9.1 Key decisions

| Topic | Choice | Reason |
| --- | --- | --- |
| Lint | Ruff | Much faster than Pylint/Flake8 |
| Complexity | Radon | Native Python API |
| Security | Bandit | Python-focused security rules |
| Code fetch | `git clone` | No AtomGit REST API |
| Reports | Markdown | Easy to share; Feishu renders it |

### 9.2 Pitfalls we hit

1. **Windows encoding** — Use `input=code.encode("utf-8")` instead of `text=True` for Ruff stdin.
2. **Bandit and stdin** — Write a temp file, analyze, delete.
3. **No AtomGit API** — Use `git clone`.
4. **Project-directory mode** — Default user workspace `C:\Users\<user>\.jiuwenclaw\` is awkward for VCS; point config at the repo instead.
5. **Skill paths** — With project mode, skills live under `...\workspace\agent\skills\code-review\`.
6. **Max iterations** — Complex repos can hit `max_iterations` if the agent opens files one-by-one; follow the skill and use the right tools.
7. **WebSocket size** — Huge reports exceed ~1MB; forbid returning the full file in `SKILL.md` and return summaries only.
8. **Multi-language stacks** — Install ESLint, Checkstyle JAR, etc., for non-Python parts of the repo.

### 9.3 Possible extensions

1. More languages — ESLint, Checkstyle, golangci-lint, Clippy are already sketched in the skill.
2. **Incremental scans** — Only `git diff` files.
3. **PR integration** — When AtomGit exposes APIs, automate PR comments.
4. **Trends** — Store scores over time and chart quality.

---

## Closing

Building this review assistant clarified how to ship a practical analysis pipeline:

1. **Combine tools** — Ruff, Bandit, Radon each cover different angles.
2. **Make scoring meaningful** — Weight by severity, not raw counts.
3. **Make reports actionable** — Say what to fix and how.
4. **Keep architecture extensible** — New analyzers and formats plug in cleanly.

**Paths**: `D:\Download\jiuwenclaw\workspace\agent\skills\code-review\` — `SKILL.md`, entry `run_review.py`.
