# 告别人工 Code Review？用 JiuwenClaw 构建自动化审查流水线

## 引言｜当"代码质量"真正走进日常开发流程

在团队协作开发中，代码审查（Code Review）是保障代码质量的重要环节。但现实情况往往是：

> 每次提交代码后，Reviewer 要花大量时间检查语法错误、风格问题、安全隐患，而真正有价值的逻辑讨论反而被淹没。
>

代码审查面临三个核心痛点：

1. **重复性工作太多**：语法错误、格式问题、基础安全问题，每次都要人工检查
2. **工具分散不统一**：Lint 工具、安全扫描、复杂度分析各跑各的，结果难以整合
3. **缺乏量化评估**：代码到底好不好？没有统一的评分标准和改进建议

如果我们希望代码审查真正成为提升代码质量的工具，它必须具备三种能力：
1. **多维度静态分析**（Lint + 安全 + 复杂度）
2. **智能评分与建议**（量化评估 + 改进方向）
3. **多渠道报告输出**（飞书推送 + 本地报告）

在这篇文章里，我会完整拆解：

+ 如何设计多层次代码分析架构（Ruff + Radon + Bandit）
+ 如何实现综合评分引擎（质量分 + 安全分 + 复杂度分 + 风格分）
+ 如何构建灵活的报告生成器（Markdown 报告 + 飞书卡片）
+ 如何支持多种代码来源（本地扫描 + Git 仓库克隆）
+ 完整的代码实现与测试验证流程

如果你也在思考：

+ 如何把代码审查从"人工检查"变成"智能分析"？
+ 如何给代码质量一个可量化的评分？
+ 如何构建可扩展的代码分析流水线？

那接下来的内容，或许会给你一些新的视角。

---

## 项目环境说明

> **本文档基于真实开发项目编写**，所有配置和代码均为实际使用的版本，非示例占位符。
>

### 实际运行环境

| 项目 | 配置值 |
| --- | --- |
| **项目路径** | `D:\Download\jiuwenclaw` |
| **操作系统** | Windows 10 |
| **Python** | 3.10+ |
| **模型服务** | 智谱AI (GLM-4.7) |

### 代码分析工具

| 工具 | 版本 | 功能 | 语言 |
| --- | --- | --- | --- |
| **Ruff** | 0.4.0+ | Python Linting（替代 Pylint/Flake8） | Python |
| **Radon** | 6.0.0+ | 圈复杂度分析 | Python |
| **Bandit** | 1.7.0+ | 安全漏洞扫描 | Python |
| **ESLint** | 9.0+ | 代码质量检查 | JavaScript/TypeScript |
| **Checkstyle** | 10.12+ | 代码风格检查 | Java |
| **golangci-lint** | latest | 综合代码检查 | Go |
| **Clippy** | latest | Lint 检查 | Rust |

### 核心文件位置

```plain
D:\Download\jiuwenclaw\
├── .env                              # 环境变量配置
├── workspace/
│   └── agent/
│       ├── reports/code-review/      # 审查报告输出目录
│       └── skills/code-review/       # 技能模块
│           ├── SKILL.md              # 技能定义 v2.0.1
│           ├── config.py             # 配置管理
│           ├── run_review.py         # 入口脚本
│           ├── models/               # 数据模型层
│           ├── collectors/           # 代码采集层
│           ├── analyzers/            # 分析引擎层
│           └── generators/           # 报告生成层
```

## 一、问题背景

### 1.1 传统代码审查的局限性

说起代码审查，大家第一反应往往是：

> "那不就是 PR Review 吗？看看代码写得对不对、格式规不规范？"
>

在简单场景里，人工审查确实够用。

可一到真实开发现场，你就会碰到三类大坑：

1. **效率低下**
   每次审查都要从头检查基础问题：未使用的导入、硬编码密码、过长函数...这些本该由工具自动发现。

2. **标准不统一**
   不同的 Reviewer 关注点不同，有的看重格式，有的看重逻辑，没有统一的量化标准。

3. **容易遗漏**
   安全漏洞（如 SQL 注入、命令注入）往往需要专业工具才能发现，人工审查很容易漏掉。

举个例子，一段代码有 15 个函数，其中 3 个复杂度超过 20（D 级），还有 2 处潜在的 SQL 注入风险。人工审查很难一次性发现所有问题。

**代码审查助手** 不一样了——它用三大多维分析工具扫描代码，通过评分引擎计算综合分数，最终生成包含问题详情、改进建议的完整报告。

### 1.2 开发者的实际痛点

做代码质量相关项目时，踩过不少坑。

最典型的就是**每次提交代码前要跑多个工具**。先跑 Flake8 检查语法，再跑 Pylint 检查风格，还要跑 Bandit 检查安全...每个工具输出格式不同，结果需要人工汇总。

还有一个问题是**不知道代码到底好不好**。跑了工具，发现 50 个警告，但这些警告有多严重？代码整体质量如何？没有统一的评估标准。

最麻烦的是**安全漏洞容易漏掉**。明明写了 `os.system(user_input)`，人工审查时没注意，结果上线后被发现存在命令注入风险。

用了代码审查助手之后，这些问题确实解决了很多。系统会自动运行 Ruff（Lint）、Radon（复杂度）、Bandit（安全）三个工具，然后通过评分引擎计算质量分、安全分、复杂度分、风格分，最终给出综合评分和改进建议。

### 1.3 JiuwenClaw 技能系统的优势

JiuwenClaw 是一个开源的 Agent 开发框架，其技能系统非常适合构建代码审查工具：

| 能力 | 说明 |
| --- | --- |
| **模块化技能** | 每个 Skill 可以包含多个 Python 模块，便于分层设计 |
| **工具集成** | 可声明 `allowed_tools` 获取系统工具权限，执行外部命令 |
| **文件操作** | 支持读写文件，便于扫描代码和生成报告 |
| **多渠道推送** | 支持飞书等渠道推送审查报告 |

这个框架的价值在哪？简单说就是**能力可组合**。通过分层设计，我们可以把代码采集、静态分析、报告生成分别封装，形成清晰的责任边界。

## 二、技术方案

### 2.1 分层架构设计

代码审查助手采用经典的三层架构，在 JiuwenClaw 的 Application Layer（应用层）：

```
┌─────────────────────────────────────────────────────────┐
│                    Application Layer                     │
│                   (code-review skill)                    │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Collectors  │→ │  Analyzers  │→ │ Generators  │     │
│  │  代码采集层  │  │  分析引擎层  │  │  报告生成层  │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│        │                │                │              │
│   LocalCollector   RuffAnalyzer    ReportGenerator      │
│   AtomGitClient    RadonAnalyzer   FeishuPublisher      │
│                     BanditAnalyzer                       │
│                     ScoreCalculator                      │
└─────────────────────────────────────────────────────────┘
```

### 2.2 数据处理流程

完整的代码审查处理流程：

```
用户请求/对话触发: 审查代码
         │
         ▼
┌──────────────────┐
│   COLLECTORS     │  代码采集
│  LocalCollector  │  本地目录扫描
│  AtomGitClient   │  Git仓库克隆
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   ANALYZERS      │  多维分析
│  RuffAnalyzer    │  Lint检查 → lint_issues
│  RadonAnalyzer   │  复杂度 → complexity_issues
│  BanditAnalyzer  │  安全 → security_issues
│  ScoreCalculator │  评分 → ReviewScore
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   GENERATORS     │  报告生成
│  ReportGenerator │  Markdown报告
│  FeishuPublisher │  飞书卡片推送
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│    OUTPUT        │  结果输出
│  本地报告文件     │  .md 文件
│  飞书消息卡片     │  交互式卡片
└──────────────────┘
```

### 2.3 核心组件概览

本项目的核心组件及其职责：

| 组件 | 类型 | 职责 | 所在模块 |
| --- | --- | --- | --- |
| **LocalCollector** | Collector | 本地代码文件采集 | `collectors/local_collector.py` |
| **AtomGitClient** | Collector | Git 仓库克隆与文件获取 | `collectors/atomgit_client.py` |
| **RuffAnalyzer** | Analyzer | Lint 语法检查 | `analyzers/ruff_analyzer.py` |
| **RadonAnalyzer** | Analyzer | 复杂度分析 | `analyzers/radon_analyzer.py` |
| **BanditAnalyzer** | Analyzer | 安全漏洞扫描 | `analyzers/bandit_analyzer.py` |
| **ScoreCalculator** | Analyzer | 综合评分计算 | `analyzers/score_calculator.py` |
| **ReportGenerator** | Generator | Markdown 报告生成 | `generators/report_generator.py` |
| **FeishuPublisher** | Generator | 飞书消息推送 | `generators/feishu_publisher.py` |

### 2.4 评分体系设计

代码审查助手采用多维度加权评分体系：

| 维度 | 权重 | 计算依据 |
| --- | --- | --- |
| **代码质量** | 35% | Lint 问题数量与严重程度 |
| **安全性** | 30% | 安全漏洞数量与严重程度 |
| **复杂度** | 20% | 圈复杂度与高复杂度函数数量 |
| **风格** | 15% | 代码风格问题 |

最终综合评分转换为等级：

| 等级 | 分数范围 | 状态 |
| --- | --- | --- |
| A | 90-100 | 优秀 |
| B | 80-89 | 良好 |
| C | 70-79 | 合格 |
| D | 60-69 | 需改进 |
| F | <60 | 不通过 |

## 第三章｜Skills 技能系统工程化设计

### 3.1 Skills 目录结构

```plain
workspace/agent/skills/code-review/
├── SKILL.md                    # 技能定义（必须）
├── config.py                   # 配置管理
├── run_review.py               # 入口脚本
│
├── models/                     # 数据模型层
│   ├── __init__.py
│   ├── code_issue.py           # 代码问题模型
│   ├── code_metrics.py         # 代码指标模型
│   ├── review_result.py        # 审查结果模型
│   └── review_score.py         # 评分模型
│
├── collectors/                 # 代码采集层
│   ├── __init__.py
│   ├── local_collector.py      # 本地文件采集
│   └── atomgit_client.py       # Git 仓库客户端
│
├── analyzers/                  # 分析引擎层
│   ├── __init__.py
│   ├── ruff_analyzer.py        # Ruff Lint 分析
│   ├── radon_analyzer.py       # Radon 复杂度分析
│   ├── bandit_analyzer.py      # Bandit 安全分析
│   ├── ast_analyzer.py         # AST 结构分析
│   └── score_calculator.py     # 综合评分计算
│
└── generators/                 # 报告生成层
    ├── __init__.py
    ├── report_generator.py     # Markdown 报告生成
    └── feishu_publisher.py     # 飞书推送
```

### 3.2 SKILL.md 技能定义（v2.0.1）

```markdown
---
name: code-review
version: 2.0.1
description: 多语言代码审查助手，支持 Python/JavaScript/Java/Go/Rust，检测质量/安全/复杂度问题
tags: [code, review, python, javascript, typescript, java, go, rust, quality, security]
allowed_tools: [bash, read_file, write_file]
---

# 多语言代码审查助手

审查任意 Git 仓库的多种编程语言代码，检测安全漏洞、代码质量问题、复杂度问题。

## 支持的语言

| 语言 | Lint 工具 | 安全扫描 | 复杂度分析 |
|------|-----------|----------|------------|
| **Python** | Ruff | Bandit | Radon |
| **JavaScript/TypeScript** | ESLint | eslint-plugin-security | - |
| **Java** | Checkstyle | - | - |
| **Go** | golangci-lint | gosec | gocyclo |
| **Rust** | Clippy | cargo-audit | - |

## 使用方式

### 审查远程 Git 仓库

```bash
cd D:/Download/jiuwenclaw && python workspace/agent/skills/code-review/run_review.py clone --url <仓库地址>
```

### 审查本地代码

```bash
cd D:/Download/jiuwenclaw && python workspace/agent/skills/code-review/run_review.py local --path <路径>
```

## ⚠️ 重要限制

**绝对不要读取并返回完整报告文件！**

大型仓库的报告可能有数万行，会导致：
- WebSocket 消息过大（超过 1MB 限制）
- 发送失败，用户收不到任何回复

**正确做法：**
1. 从命令输出中提取摘要信息
2. 用飞书友好格式返回简短摘要
3. 告知用户详细报告已保存

## 飞书友好输出格式

```
📊 代码审查报告

━━━━━━━━━━━━━━━━━━━━━━
📦 仓库：<仓库名称>
📁 扫描文件：<数量> 个
⏰ 审查时间：<时间>
━━━━━━━━━━━━━━━━━━━━━━

📋 问题统计

🔴 错误：<数量> 个
🟡 警告：<数量> 个
🔵 提示：<数量> 个

💡 改进建议

1. <建议1>
2. <建议2>

━━━━━━━━━━━━━━━━━━━━━━
📄 详细报告已保存
```

## 评分等级

| 等级 | 分数 | 状态 | 图标 |
|------|------|------|------|
| A | 90-100 | 优秀 | 🟢 |
| B | 80-89 | 良好 | 🔵 |
| C | 70-79 | 合格 | 🟡 |
| D | 60-69 | 需改进 | 🟠 |
| F | <60 | 不通过 | 🔴 |
```

### 3.3 报告模板示例

```markdown
# :white_check_mark: 代码审查报告

| 属性 | 值 |
|------|-----|
| 审查类型 | clone |
| 目标 | `owner/repo` |
| 审查时间 | 2026-03-08 10:30 |
| 扫描文件 | 25 |

## 评分

| 维度 | 分数 |
|------|------|
| **综合评分** | **72.5** (C) |
| 代码质量 | 65.0 |
| 安全性 | 80.0 |
| 复杂度 | 85.0 |
| 风格 | 70.0 |

**状态**: 通过

## 问题统计

- 总计: **45** 个问题
- 错误: 3
- 警告: 28
- 提示: 14

## 问题详情

### 安全 (5)

- `app.py:42` [B608] Possible SQL injection vector
  > SQL 注入风险

### 代码质量 (32)

- `utils.py:15` [F401] `os` imported but unused
  > 删除未使用的导入

### 复杂度 (8)

- `service.py:120` [CD] 函数 'process_data' 复杂度过高 (23, D级)
  > 强烈建议重构，将函数拆分为多个小函数

## 改进建议

1. 发现 3 个高危安全问题，请优先处理
2. 有 5 个高复杂度函数，建议拆分重构
3. 修复 3 个错误级别问题

---
*报告生成时间: 2026-03-08 10:30:15*
```

## 第四章｜数据模型层完整实现

### 4.1 代码问题模型 (CodeIssue)

```python
# models/code_issue.py
# -*- coding: utf-8 -*-
"""
代码问题模型

表示单个代码问题，包含位置、严重程度、分类等信息
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CodeIssue:
    """代码问题"""

    file: str                    # 文件路径
    line: int                    # 行号
    severity: str                # 严重级别: error/warning/info
    category: str                # 类别: lint/complexity/security/style
    message: str                 # 问题描述
    rule: str                    # 规则ID (如 E501, B101)
    source: str                  # 来源工具 (ruff/bandit/radon)
    column: int = 0              # 列号
    suggestion: str = ""         # 修复建议

    def to_dict(self) -> dict:
        """转换为字典"""
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
        """字符串表示"""
        location = f"{self.file}:{self.line}"
        return f"[{self.rule}] {location} - {self.message}"
```

### 4.2 代码指标模型 (CodeMetrics)

```python
# models/code_metrics.py
# -*- coding: utf-8 -*-
"""
代码指标模型

统计代码行数、函数数量、复杂度等指标
"""

from dataclasses import dataclass


@dataclass
class CodeMetrics:
    """代码指标"""

    # 行数统计
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0

    # 复杂度
    avg_complexity: float = 0.0
    max_complexity: int = 0
    complexity_rank: str = "A"
    high_complexity_count: int = 0

    # 可维护性
    maintainability_index: float = 0.0
    maintainability_rank: str = "A"

    # 结构
    function_count: int = 0
    class_count: int = 0
    file_count: int = 0

    def to_dict(self) -> dict:
        """转换为字典"""
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

### 4.3 评分模型 (ReviewScore)

```python
# models/review_score.py
# -*- coding: utf-8 -*-
"""
审查评分模型

多维度评分与等级计算
"""

from dataclasses import dataclass


@dataclass
class ReviewScore:
    """审查评分"""

    overall: float = 0.0          # 综合评分 (0-100)
    quality_score: float = 0.0    # 代码质量分
    security_score: float = 0.0   # 安全评分
    complexity_score: float = 0.0 # 复杂度评分
    style_score: float = 0.0      # 风格评分

    grade: str = "C"              # 等级: A/B/C/D/F
    passed: bool = False          # 是否通过审查

    @staticmethod
    def score_to_grade(score: float) -> str:
        """分数转等级"""
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
        """转换为字典"""
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

## 第五章｜分析引擎层完整实现

### 5.1 Ruff Lint 分析器

```python
# analyzers/ruff_analyzer.py
# -*- coding: utf-8 -*-
"""
Ruff Lint 分析器

使用 Ruff 进行代码质量检查
特点：比 Pylint/Flake8 快 10-100 倍
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
    """Ruff Lint 分析器"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    def analyze_code(self, code: str, filename: str = "temp.py") -> List[CodeIssue]:
        """
        分析代码字符串

        关键点：使用 stdin 传递代码，避免创建临时文件
        Windows 兼容：使用 bytes 编码避免 GBK 编码问题
        """
        try:
            result = subprocess.run(
                ["ruff", "check", "--output-format", "json",
                 "--stdin-filename", filename, "-"],
                input=code.encode("utf-8"),  # 重要：使用 bytes 而非 text
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
                category="lint", message="Ruff 分析超时",
                rule="TIMEOUT", source="ruff"
            )]
        except FileNotFoundError:
            return [CodeIssue(
                file=filename, line=0, severity="warning",
                category="lint", message="Ruff 未安装，请运行: pip install ruff",
                rule="MISSING", source="ruff"
            )]

    def analyze_directory(self, dirpath: str,
                          exclude_patterns: Optional[List[str]] = None) -> List[CodeIssue]:
        """分析目录下所有 Python 文件"""
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
        """解析 Ruff JSON 输出"""
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
            "E501": "将长行拆分为多行，使用括号或反斜杠",
            "F401": "删除未使用的导入",
            "F841": "删除未使用的变量",
            "E711": "使用 'is None' 而不是 '== None'",
            "E722": "捕获具体异常而不是裸 except",
        }
        return suggestions.get(rule, "")
```

### 5.2 Radon 复杂度分析器

```python
# analyzers/radon_analyzer.py
# -*- coding: utf-8 -*-
"""
Radon 复杂度分析器

使用 Radon 进行代码复杂度分析
支持：圈复杂度(CC)、原始指标(Raw)、可维护性指数(MI)
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
    """复杂度分析结果"""
    name: str           # 函数/方法名
    type: str           # 类型: function/method/class
    complexity: int     # 复杂度值
    rank: str           # 等级: A-F
    line: int           # 行号
    file: str = ""      # 文件路径
    classname: str = "" # 所属类名


class RadonAnalyzer:
    """Radon 复杂度分析器"""

    # 复杂度等级阈值
    COMPLEXITY_THRESHOLDS = {
        "A": (1, 5),    # 简单
        "B": (6, 10),   # 中等
        "C": (11, 20),  # 复杂
        "D": (21, 30),  # 很复杂
        "E": (31, 40),  # 极其复杂
        "F": (41, 100), # 难以维护
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.max_complexity = self.config.get("max_complexity", 10)

    def analyze_complexity(self, code: str, filename: str = "") -> List[ComplexityResult]:
        """分析代码复杂度"""
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
        """分析代码指标"""
        metrics = CodeMetrics()
        if not RADON_AVAILABLE:
            return metrics

        try:
            # 原始指标
            raw = raw_analyze(code)
            metrics.total_lines = raw.loc
            metrics.code_lines = raw.sloc
            metrics.comment_lines = raw.comments
            metrics.blank_lines = raw.blank

            # 可维护性指数
            mi = mi_visit(code, True)
            if isinstance(mi, (int, float)):
                metrics.maintainability_index = mi
                metrics.maintainability_rank = self._mi_to_rank(mi)
        except Exception:
            pass
        return metrics

    def get_high_complexity_issues(self, complexity_results: List[ComplexityResult]) -> List[CodeIssue]:
        """将高复杂度结果转换为问题列表"""
        issues = []
        for result in complexity_results:
            if result.complexity > self.max_complexity:
                severity = "error" if result.rank in ["D", "E", "F"] else "warning"
                issues.append(CodeIssue(
                    file=result.file,
                    line=result.line,
                    severity=severity,
                    category="complexity",
                    message=f"函数 '{result.name}' 复杂度过高 ({result.complexity}, {result.rank}级)",
                    rule=f"C{result.rank}",
                    source="radon",
                    suggestion=self._get_complexity_suggestion(result),
                ))
        return issues

    def _get_complexity_suggestion(self, result: ComplexityResult) -> str:
        suggestions = {
            "C": "建议拆分函数，减少条件分支",
            "D": "强烈建议重构，将函数拆分为多个小函数",
            "E": "必须重构，代码难以维护和测试",
            "F": "必须立即重构，代码几乎不可维护",
        }
        return suggestions.get(result.rank, "考虑降低函数复杂度")
```

### 5.3 Bandit 安全分析器

```python
# analyzers/bandit_analyzer.py
# -*- coding: utf-8 -*-
"""
Bandit 安全分析器

使用 Bandit 进行代码安全扫描
检测：SQL注入、命令注入、硬编码密码、弱加密等
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
    """Bandit 安全分析器"""

    # 常见安全问题的建议
    SECURITY_SUGGESTIONS = {
        "B101": "使用 assert 进行断言可能在优化模式下被禁用",
        "B105": "硬编码密码字符串",
        "B110": "try/except 块捕获了所有异常",
        "B303": "使用了不安全的加密算法 (MD5/SHA1)",
        "B311": "random 模块不适用于加密场景",
        "B324": "证书未验证",
        "B404": "使用 subprocess 可能不安全",
        "B501": "使用 requests 时不验证证书",
        "B506": "不安全的 YAML 加载",
        "B601": "subprocess 命令注入风险",
        "B603": "subprocess 命令注入风险",
        "B605": "os.system 命令注入风险",
        "B606": "os.popen 命令注入风险",
        "B608": "SQL 注入风险",
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    def analyze_code(self, code: str, filename: str = "temp.py") -> List[CodeIssue]:
        """
        分析代码字符串

        注意：Bandit 不支持 stdin，需要写入临时文件
        """
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
        """分析单个文件"""
        path = Path(filepath)
        if not path.exists():
            return [CodeIssue(
                file=filepath, line=0, severity="error",
                category="security", message=f"文件不存在: {filepath}",
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
                category="security", message="Bandit 未安装，请运行: pip install bandit",
                rule="MISSING", source="bandit"
            )]

    def _parse_output(self, output: str, default_file: str = "") -> List[CodeIssue]:
        """解析 Bandit JSON 输出"""
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

### 5.4 综合评分计算器

```python
# analyzers/score_calculator.py
# -*- coding: utf-8 -*-
"""
综合评分计算器

根据各类问题计算综合评分
采用多维度加权评分体系
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
    """评分权重"""
    quality: float = 0.35      # 代码质量 (lint 问题)
    security: float = 0.30     # 安全性 (bandit 问题)
    complexity: float = 0.20   # 复杂度
    style: float = 0.15        # 风格


class ScoreCalculator:
    """综合评分计算器"""

    DEFAULT_WEIGHTS = ScoringWeights()

    # 严重级别扣分
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
        """计算综合评分"""
        score = ReviewScore()

        # 计算各维度分数
        score.quality_score = self._calc_quality_score(result.lint_issues)
        score.security_score = self._calc_security_score(result.security_issues)
        score.complexity_score = self._calc_complexity_score(result.metrics)
        score.style_score = self._calc_style_score(result.style_issues)

        # 加权计算总分
        score.overall = (
            score.quality_score * self.weights.quality +
            score.security_score * self.weights.security +
            score.complexity_score * self.weights.complexity +
            score.style_score * self.weights.style
        )

        # 确定等级和是否通过
        score.grade = ReviewScore.score_to_grade(score.overall)
        score.passed = score.overall >= self.pass_threshold

        return score

    def _calc_quality_score(self, issues: List[CodeIssue]) -> float:
        """计算代码质量分"""
        score = 100.0
        for issue in issues:
            penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            score -= penalty
        return max(0.0, min(100.0, score))

    def _calc_security_score(self, issues: List[CodeIssue]) -> float:
        """计算安全评分（安全问题权重更高）"""
        score = 100.0
        severity_multiplier = {"error": 15.0, "warning": 8.0, "info": 2.0}

        for issue in issues:
            base_penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            multiplier = severity_multiplier.get(issue.severity, 1.0)
            score -= base_penalty * (multiplier / 5)

        # 有高危问题时，最低分 20
        has_critical = any(i.severity == "error" for i in issues)
        min_score = 20.0 if has_critical else 0.0
        return max(min_score, min(100.0, score))

    def _calc_complexity_score(self, metrics: CodeMetrics) -> float:
        """计算复杂度评分"""
        score = 100.0

        if metrics.avg_complexity > 5:
            score -= (metrics.avg_complexity - 5) * 5
        if metrics.high_complexity_count > 0:
            score -= metrics.high_complexity_count * 3

        return max(0.0, min(100.0, score))

    def _calc_style_score(self, issues: List[CodeIssue]) -> float:
        """计算风格评分"""
        score = 100.0
        for issue in issues:
            penalty = self.SEVERITY_PENALTY.get(issue.severity, 1.0)
            score -= penalty * 0.5
        return max(0.0, min(100.0, score))

    def get_improvement_suggestions(self, result: ReviewResult) -> List[str]:
        """生成改进建议"""
        suggestions = []

        if result.score.quality_score < 70:
            suggestions.append("代码质量较低，建议先修复 lint 错误和警告")

        if result.score.security_score < 70:
            critical = [i for i in result.security_issues if i.severity == "error"]
            if critical:
                suggestions.append(f"发现 {len(critical)} 个高危安全问题，请优先处理")

        if result.score.complexity_score < 70:
            if result.metrics.high_complexity_count > 0:
                suggestions.append(
                    f"有 {result.metrics.high_complexity_count} 个高复杂度函数，建议拆分重构"
                )

        if result.errors > 0:
            suggestions.append(f"修复 {result.errors} 个错误级别问题")

        if result.score.overall >= 90:
            suggestions.append("代码质量优秀，继续保持！")

        return suggestions[:5]
```

## 第六章｜代码采集与报告生成

### 6.1 本地代码采集器

```python
# collectors/local_collector.py
# -*- coding: utf-8 -*-
"""
本地代码采集器

扫描本地目录，收集 Python 文件
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class FileInfo:
    """文件信息"""
    path: str              # 绝对路径
    relative_path: str     # 相对路径
    size: int              # 文件大小（字节）

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "size": self.size,
        }


@dataclass
class ScanResult:
    """扫描结果"""
    files: List[FileInfo] = field(default_factory=list)
    total_files: int = 0
    total_size: int = 0
    errors: List[str] = field(default_factory=list)


class LocalCollector:
    """本地代码采集器"""

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
        max_file_size: int = 1024 * 1024,  # 1MB
    ):
        self.path = Path(path).resolve()
        self.exclude_patterns = exclude_patterns or self.DEFAULT_EXCLUDES
        self.max_file_size = max_file_size

    def collect(self) -> ScanResult:
        """采集所有 Python 文件"""
        result = ScanResult()

        if not self.path.exists():
            result.errors.append(f"路径不存在: {self.path}")
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
        """获取文件内容"""
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

### 6.2 Git 仓库克隆功能

```python
# run_review.py 中的克隆函数
def review_clone(repo_url: str, config: CodeReviewConfig, branch: str = "main") -> ReviewResult:
    """
    克隆 Git 仓库并扫描

    支持任何 Git 仓库（包括 AtomGit、GitHub、Gitee 等）
    """
    import shutil
    import subprocess
    import tempfile

    # 解析仓库名称
    parsed = parse_repo_url(repo_url)
    repo_name = f"{parsed[0]}/{parsed[1]}" if parsed[0] and parsed[1] else repo_url

    result = ReviewResult(review_type="clone", target=repo_name)

    # 创建临时目录
    temp_dir = tempfile.mkdtemp(prefix="code_review_")

    try:
        # 克隆仓库（浅克隆，只获取最新提交）
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
            # 尝试不指定分支
            clone_cmd = ["git", "clone", "--depth", "1", repo_url, temp_dir]
            proc = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=300)

            if proc.returncode != 0:
                result.suggestions = [f"克隆仓库失败: {proc.stderr}"]
                return result

        # 扫描克隆的代码
        scan_result = review_local(temp_dir, config)
        scan_result.review_type = "clone"
        scan_result.target = repo_name
        return scan_result

    except subprocess.TimeoutExpired:
        result.suggestions = ["克隆仓库超时，仓库可能太大"]
        return result
    finally:
        # 清理临时目录
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
```

### 6.3 报告生成器

```python
# generators/report_generator.py
# -*- coding: utf-8 -*-
"""
报告生成器

生成 Markdown 格式的代码审查报告
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import ReviewResult


class ReportGenerator:
    """报告生成器"""

    def __init__(self, output_dir: str = "workspace/agent/reports/code-review"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, result: "ReviewResult") -> str:
        """生成 Markdown 报告"""
        lines = []

        # 标题
        icon = ":white_check_mark:" if result.score.passed else ":x:"
        lines.append(f"# {icon} 代码审查报告")
        lines.append("")

        # 基本信息
        lines.append("| 属性 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 审查类型 | {result.review_type} |")
        lines.append(f"| 目标 | `{result.target}` |")
        lines.append(f"| 审查时间 | {result.reviewed_at.strftime('%Y-%m-%d %H:%M')} |")
        lines.append(f"| 扫描文件 | {result.files_scanned} |")
        lines.append("")

        # 评分
        lines.append("## 评分")
        lines.append("")
        lines.append("| 维度 | 分数 |")
        lines.append("|------|------|")
        lines.append(f"| **综合评分** | **{result.score.overall:.1f}** ({result.score.grade}) |")
        lines.append(f"| 代码质量 | {result.score.quality_score:.1f} |")
        lines.append(f"| 安全性 | {result.score.security_score:.1f} |")
        lines.append(f"| 复杂度 | {result.score.complexity_score:.1f} |")
        lines.append(f"| 风格 | {result.score.style_score:.1f} |")
        lines.append("")
        lines.append(f"**状态**: {'通过' if result.score.passed else '未通过'}")
        lines.append("")

        # 问题统计
        lines.append("## 问题统计")
        lines.append("")
        lines.append(f"- 总计: **{result.total_issues}** 个问题")
        lines.append(f"- 错误: {result.errors}")
        lines.append(f"- 警告: {result.warnings}")
        lines.append(f"- 提示: {result.infos}")
        lines.append("")

        # 问题详情
        lines.append("## 问题详情")
        lines.append("")

        if result.total_issues == 0:
            lines.append("未发现问题。")
        else:
            # 按类别分组
            if result.security_issues:
                lines.append(f"### 安全 ({len(result.security_issues)})")
                lines.append("")
                for issue in result.security_issues[:10]:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                if len(result.security_issues) > 10:
                    lines.append(f"- ... 还有 {len(result.security_issues) - 10} 个问题")
                lines.append("")

            if result.lint_issues:
                lines.append(f"### 代码质量 ({len(result.lint_issues)})")
                lines.append("")
                for issue in result.lint_issues[:10]:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                if len(result.lint_issues) > 10:
                    lines.append(f"- ... 还有 {len(result.lint_issues) - 10} 个问题")
                lines.append("")

            if result.complexity_issues:
                lines.append(f"### 复杂度 ({len(result.complexity_issues)})")
                lines.append("")
                for issue in result.complexity_issues:
                    lines.append(f"- `{issue.file}:{issue.line}` [{issue.rule}] {issue.message}")
                    if issue.suggestion:
                        lines.append(f"  > {issue.suggestion}")
                lines.append("")

        # 代码指标
        lines.append("## 代码指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 总行数 | {result.metrics.total_lines} |")
        lines.append(f"| 代码行 | {result.metrics.code_lines} |")
        lines.append(f"| 注释行 | {result.metrics.comment_lines} |")
        lines.append(f"| 函数数量 | {result.metrics.function_count} |")
        lines.append(f"| 类数量 | {result.metrics.class_count} |")
        lines.append(f"| 平均复杂度 | {result.metrics.avg_complexity:.1f} |")
        lines.append(f"| 最大复杂度 | {result.metrics.max_complexity} |")
        lines.append("")

        # 改进建议
        lines.append("## 改进建议")
        lines.append("")
        for i, suggestion in enumerate(result.suggestions, 1):
            lines.append(f"{i}. {suggestion}")
        lines.append("")

        # 页脚
        lines.append("---")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        return "\n".join(lines)

    def save_report(self, content: str, result: "ReviewResult") -> str:
        """保存报告到文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = result.target.replace("/", "_").replace("\\", "_").replace(":", "_")
        filename = f"review_{result.review_type}_{target}_{timestamp}.md"
        filepath = self.output_dir / filename

        filepath.write_text(content, encoding="utf-8")
        return str(filepath)
```

## 第七章｜配置与部署

### 7.1 环境变量配置

> **本项目采用项目目录模式**，所有配置文件位于项目目录：
> - 配置文件：`D:\Download\jiuwenclaw\.env`
> - 技能目录：`D:\Download\jiuwenclaw\workspace\agent\skills\`
>
> 这样可以随项目版本控制，方便提交 PR。

在项目根目录的 `.env` 文件中配置：

```env
# 模型配置（智谱AI GLM-4.7）
# 使用 OpenAI 兼容客户端调用智谱AI API
MODEL_PROVIDER="OpenAI"
MODEL_NAME="glm-4.7"
API_BASE="https://open.bigmodel.cn/api/paas/v4"
API_KEY="your-api-key"

# 调用示例（Python requests）
# import requests
#
# url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# payload = {
#     "model": "glm-4.7",
#     "messages": [{"role": "user", "content": "你好"}],
#     "stream": False,
#     "temperature": 1
# }
# headers = {
#     "Authorization": "Bearer <your-api-key>",
#     "Content-Type": "application/json"
# }
# response = requests.post(url, json=payload, headers=headers)
# print(response.text)

# 飞书推送（可选）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_REVIEW_CHAT_ID=oc_xxx

# AtomGit（目前不支持 API，使用 git clone 代替）
ATOMGIT_TOKEN=xxx
```

### 7.2 JiuwenClaw 配置文件

> **本项目采用项目目录模式**，配置文件位于：
> `D:\Download\jiuwenclaw\config\config.yaml`

JiuwenClaw 配置文件内容：

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
  # Skills 在线自演进配置
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

**配置说明：**

| 配置项 | 说明 |
|--------|------|
| `client_provider: OpenAI` | 使用 OpenAI 兼容客户端 |
| `api_base` | 智谱AI API 地址 |
| `model_name: glm-4.7` | 使用的模型名称 |
| `channels.feishu.enabled` | 启用飞书通道 |

### 7.3 依赖安装

```bash
# Python 分析工具
pip install ruff radon bandit

# JavaScript/TypeScript 分析工具
npm install -g eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin

# Go 分析工具
go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
go install github.com/securego/gosec/v2/cmd/gosec@latest

# Rust 分析工具
rustup component add clippy
cargo install cargo-audit

# Java 分析工具 (下载到项目 tools 目录)
mkdir -p tools
curl -L "https://github.com/checkstyle/checkstyle/releases/download/checkstyle-10.12.5/checkstyle-10.12.5-all.jar" -o tools/checkstyle.jar

# 飞书 SDK（可选）
pip install lark-oapi
```

**工具检测状态：**

| 语言 | 工具 | 安装命令 |
|------|------|----------|
| Python | Ruff, Radon, Bandit | `pip install ruff radon bandit` |
| JavaScript/TypeScript | ESLint | `npm install -g eslint` |
| Java | Checkstyle | 下载 JAR 到 `tools/checkstyle.jar` |
| Go | golangci-lint, gosec | `go install ...` |
| Rust | Clippy, cargo-audit | `rustup component add clippy` |

### 7.4 心跳配置

在 `HEARTBEAT.md` 中配置定时审查：

```markdown
## 活跃的任务项
- 执行代码质量扫描  # 每天扫描代码质量
```

## 第八章｜测试验证

| 测试方式 | 说明 |
|--------|------|
| 飞书对话 | 通过飞书私聊机器人发送审查请求 |
| Web 对话 | 通过 JiuwenClaw Web 界面发送审查请求 |

### 8.1 飞书对话测试

JiuwenClaw 作为远程智能管家，支持通过飞书与机器人对话进行代码审查。

**前提条件：**
1. 启动 JiuwenClaw 服务
2. 配置飞书机器人并建立 WebSocket 长连接
3. code-review 技能已部署到项目目录

在飞书中私聊机器人发送消息：

**测试消息：**
```
https://atomgit.com/openJiuwen/deepsearch 对这个仓库进行审查
```

**测试流程：**

机器人收到消息后，会自动：
1. 读取 code-review 技能文档
2. 克隆远程仓库
3. 扫描代码文件（Ruff + Radon + Bandit）
4. 生成审查报告
5. 返回审查结果到飞书

**审查结果示例：**

| 项目 | openJiuwen-DeepSearch |
|------|---------------------|
| 版本 | 0.2.0 |
| 综合评分 | 4.2/5 |
| 代码规模 | 195 个 Python 文件，33,760 行代码 |
| 问题率 | 5.1% (10 个文件存在问题) |
| 推荐指数 | [推荐] |

### 8.2 Web 对话测试

JiuwenClaw 提供 Web 界面，可以直接在浏览器中与 Agent 对话进行代码审查。

**前提条件：**
1. 启动 JiuwenClaw Web 服务：`python start_services.py web`
2. 访问 Web 界面（默认 http://localhost:8000）
3. code-review 技能已部署到项目目录

**测试步骤：**

1. 打开浏览器访问 Web 界面
2. 在对话框输入审查请求：

```
https://atomgit.com/openJiuwen/deepsearch 对这个仓库进行审查
```

3. 等待 Agent 处理（通常需要 5-10 分钟，取决于仓库大小）
4. 查看返回的审查结果

**测试结果：**

Agent 会按照技能指引执行审查流程，返回包含评分、问题列表、改进建议的完整报告。

**Web 测试优势：**
- 实时显示 Agent 思考过程
- 支持查看中间步骤
- 方便调试和问题排查

### 8.3 报告示例



```markdown
# :x: 代码审查报告

| 属性 | 值 |
|------|-----|
| 审查类型 | local |
| 目标 | `./jiuwenclaw` |
| 审查时间 | 2026-03-07 21:53 |
| 扫描文件 | 110 |

## 评分

| 维度 | 分数 |
|------|------|
| **综合评分** | **41.0** (F) |
| 代码质量 | 0.0 |
| 安全性 | 20.0 |
| 复杂度 | 100.0 |
| 风格 | 100.0 |

**状态**: 未通过

## 问题统计

- 总计: **245** 个问题
- 错误: 5
- 警告: 184
- 提示: 56

## 问题详情

### 安全 (72)

- `app.py:130` [B105] Possible hardcoded password: 'EMAIL_TOKEN'
  > 硬编码密码字符串
- `database.py:654` [B608] Possible SQL injection vector
  > SQL 注入风险

### 代码质量 (152)

- `interface.py:10` [F401] `re` imported but unused
  > 删除未使用的导入

### 复杂度 (21)

- `app.py:720` [CD] 函数 '_run' 复杂度过高 (23, D级)
  > 强烈建议重构，将函数拆分为多个小函数

## 改进建议

1. 代码质量较低，建议先修复 lint 错误和警告
2. 发现 2 个高危安全问题，请优先处理
3. 修复 5 个错误级别问题
```

## 第九章｜技术要点总结

### 9.1 关键技术决策

| 决策点 | 选择 | 原因 |
| --- | --- | --- |
| Lint 工具 | Ruff | 比 Pylint/Flake8 快 10-100 倍 |
| 复杂度分析 | Radon | Python 原生 API，无需 subprocess |
| 安全扫描 | Bandit | 专门针对 Python 安全问题 |
| 代码获取 | git clone | AtomGit 无公开 API，克隆更通用 |
| 报告格式 | Markdown | 兼容性好，飞书可渲染 |

### 9.2 踩过的坑

1. **Windows 编码问题**
   问题：subprocess 在 Windows 上默认使用 GBK 编码
   解决：使用 `input=code.encode("utf-8")` 而非 `text=True`

2. **Bandit 不支持 stdin**
   问题：Bandit 无法从 stdin 读取代码
   解决：写入临时文件，分析后删除

3. **AtomGit 无公开 API**
   问题：AtomGit 目前不提供 REST API
   解决：改用 git clone 方式获取代码

4. **切换到项目目录模式**
   背景：JiuwenClaw 默认优先使用用户工作区 `C:\Users\<用户名>\.jiuwenclaw\`，但这不利于项目版本控制
   解决：删除用户工作区的 config 目录，让系统回退到项目目录模式

   ```bash
   # 备份并删除用户工作区 config 目录
   mv "C:/Users/Lenovo/.jiuwenclaw/config" "C:/Users/Lenovo/.jiuwenclaw/config.bak"

   # 复制 config 到项目目录
   mkdir -p "D:/Download/jiuwenclaw/config"
   cp "C:/Users/Lenovo/.jiuwenclaw/config.bak/config.py" "D:/Download/jiuwenclaw/config/"
   cp "C:/Users/Lenovo/.jiuwenclaw/config.bak/config.yaml" "D:/Download/jiuwenclaw/config/"
   ```

   **切换后的路径：**
   | 项目 | 路径 |
   |------|------|
   | 配置文件 | `D:\Download\jiuwenclaw\config\` |
   | 环境变量 | `D:\Download\jiuwenclaw\.env` |
   | 技能目录 | `D:\Download\jiuwenclaw\workspace\agent\skills\` |
   | 报告目录 | `D:\Download\jiuwenclaw\workspace\agent\reports\` |

5. **技能文件路径**
   本项目采用项目目录模式，技能直接放在项目目录即可：
   `D:\Download\jiuwenclaw\workspace\agent\skills\code-review\`

6. **Agent 迭代次数超限**
   问题：复杂任务达到 max_iterations (50) 后未完成
   表现：返回 "Max iterations reached without completion"
   原因：Agent 用浏览器逐个访问文件，效率太低
   解决：确保 Agent 读取技能文档并按技能指引使用正确工具

7. **WebSocket 消息过大导致发送失败**
   问题：大型仓库（如 RuoYi-Vue3）的审查报告超过 1MB
   表现：`websockets.exceptions.PayloadTooBig: frame exceeds limit of 1048576 bytes`
   原因：Agent 读取并返回完整报告文件，报告内容过大
   解决：在 SKILL.md 中明确禁止返回完整报告，只返回简短摘要

   **更新 SKILL.md 添加限制说明：**
   ```markdown
   ## ⚠️ 重要限制

   **绝对不要读取并返回完整报告文件！**

   大型仓库的报告可能有数万行，会导致：
   - WebSocket 消息过大（超过 1MB 限制）
   - 发送失败，用户收不到任何回复

   **正确做法：**
   1. 从命令输出中提取摘要信息
   2. 用飞书友好格式返回简短摘要
   3. 告知用户详细报告已保存
   ```

8. **多语言代码审查工具安装**
   问题：审查 Vue3 + Java 项目时提示缺少工具
   解决：安装对应语言的审查工具

   ```bash
   # JavaScript/TypeScript
   npm install -g eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin

   # Java - 下载 Checkstyle JAR 到 tools 目录
   mkdir -p D:/Download/jiuwenclaw/tools
   curl -L "https://github.com/checkstyle/checkstyle/releases/download/checkstyle-10.12.5/checkstyle-10.12.5-all.jar" -o D:/Download/jiuwenclaw/tools/checkstyle.jar

   # 配置环境变量 (.env)
   CHECKSTYLE_HOME=D:/Download/jiuwenclaw/tools
   ```

### 9.3 扩展方向

1. ~~**支持更多语言**~~ ✅ 已完成
   已添加 ESLint（JavaScript/TypeScript）、Checkstyle（Java）、golangci-lint（Go）、Clippy（Rust）

2. **增量扫描**
   只扫描 git diff 的变更文件，提升效率

3. **PR 集成**
   当 AtomGit 提供 API 后，支持 PR 自动审查

4. **历史趋势**
   记录历史评分，展示代码质量趋势图

---

## 写在最后

代码审查助手的开发，让我对"如何构建一个实用的代码分析工具"有了更深的理解：

1. **工具组合比单一工具更有效**
   Ruff 负责语法、Bandit 负责安全、Radon 负责复杂度，各司其职又相互补充。

2. **评分体系要有意义**
   不是简单的问题计数，而是根据严重程度加权，给开发者明确的改进方向。

3. **报告要可操作**
   不仅告诉用户"有什么问题"，还要给出"怎么修复"的建议。

4. **架构要可扩展**
   分层设计让添加新分析器、新报告格式变得简单。

如果你也想构建类似的代码审查工具，这个项目的架构和实现可以作为参考。

---

**项目地址**：`D:\Download\jiuwenclaw\workspace\agent\skills\code-review\`

**技能文档**：`SKILL.md`

**入口脚本**：`run_review.py`
