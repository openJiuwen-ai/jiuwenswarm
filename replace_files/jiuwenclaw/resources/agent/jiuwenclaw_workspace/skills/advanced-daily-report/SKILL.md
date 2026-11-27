---
name: advanced-daily-report
version: 2.0.0
description: 进阶版日报生成器，支持多数据源采集、工作分析、趋势对比、周报月报聚合
tags: [report, automation, productivity, daily, weekly, monthly, advanced]
allowed_tools: [read_memory, write_memory, bash, read_file, write_file]
---

# 进阶版日报生成器

自动采集多源数据，智能分析工作效率，生成日报/周报/月报并推送到飞书。

## 核心能力

### 1. 多数据源采集

| 数据源 | 采集内容 | 频率 |
|--------|----------|------|
| **Git 仓库** | 提交记录、代码变更统计 | 实时 |
| **网易邮箱** | 收发邮件统计、未读提醒 | 实时 |
| **记忆系统** | 今日工作记录、长期记忆 | 实时 |
| **待办事项** | 任务状态、完成率 | 实时 |

### 2. 智能工作分析

- **效率指标计算**
  - 任务完成率 = 已完成 / 总任务
  - 生产力得分（0-100）
  - 专注度得分（0-100）

- **趋势对比**
  - 与昨日对比
  - 与上周同期对比
  - 周趋势图

- **关键词提取**
  - 自动提取今日工作关键词
  - 工作主题聚类

### 3. 多报告类型

| 类型 | 触发方式 | 推送时间 |
|------|----------|----------|
| **日报** | 手动/定时 | 每天 18:00 |
| **周报** | 定时 | 每周五 18:00 |
| **月报** | 定时 | 每月最后一天 18:00 |

## 目录结构

```
daily-report/
├── SKILL.md              # 技能定义（本文件）
├── collectors/           # 数据采集模块
│   ├── __init__.py
│   ├── git_collector.py  # Git 提交采集
│   ├── email_collector.py # 邮件统计采集
│   ├── memory_collector.py # 记忆数据采集
│   ├── todo_collector.py  # 待办事项采集
│   └── aggregator.py      # 数据聚合器
├── analyzers/            # 分析模块
│   ├── __init__.py
│   └── work_analyzer.py  # 工作分析引擎
├── generators/           # 报告生成模块
│   ├── __init__.py
│   └── report_generator.py # 报告生成器
└── report_helper.py      # 兼容旧版脚本
```

## 使用方式

### ⚠️ 重要：执行方式

本技能通过执行 Python 脚本来采集数据（Git提交、邮箱邮件、记忆、待办）。
**必须使用 `bash` 工具执行脚本**，而不是直接回复用户。

**脚本会自动采集以下数据**：
- **Git 提交记录**：通过 `git log` 命令读取 `D:/Download/jiuwenclaw` 仓库的提交历史
- **邮箱邮件统计**：通过 IMAP 协议连接 `.env` 中配置的邮箱账户读取邮件统计（需要邮箱授权码）
- **记忆系统**：读取 `~/.jiuwenclaw/agent/memory/` 目录下的每日记忆文件
- **待办事项**：读取 `~/.jiuwenclaw/agent/sessions/` 下各会话的 `todo.md` 文件

### 手动触发

当用户请求生成日报/周报/月报时，**执行以下命令**：

```bash
# 生成今日日报（记忆/待办/Git 等；Git 在仓库根目录统计）
python ~/.jiuwenclaw/agent/skills/daily-report/run_report.py daily --save

# 生成指定日期日报
python ~/.jiuwenclaw/agent/skills/daily-report/run_report.py daily --date 2026-03-06 --save

# 生成周报（聚合一周数据）
python ~/.jiuwenclaw/agent/skills/daily-report/run_report.py weekly --save

# 生成月报（聚合一月数据，包含每日Git提交统计）
python ~/.jiuwenclaw/agent/skills/daily-report/run_report.py monthly --save

# 生成月报（指定月份）
python ~/.jiuwenclaw/agent/skills/daily-report/run_report.py monthly --year 2026 --month 3 --save
```

### 执行步骤

1. 用户发送 "生成日报" / "生成周报" / "生成月报" 等指令
2. **使用 bash 执行上述命令**
3. 脚本自动采集数据：
   - Git: 执行 `git log` 获取提交记录、代码变更统计
   - 邮箱: 通过 IMAP 连接获取邮件统计（如果配置了邮箱）
   - 记忆: 读取记忆文件获取工作记录
   - 待办: 解析 todo.md 获取任务状态
4. 脚本执行完成后，输出格式为 `REPORT_FILE:/path/to/report.md`
5. **⚠️ 重要：使用 read_file 工具读取报告文件，然后将完整内容发送给用户**
   - 脚本输出包含 `REPORT_FILE:` 前缀，后面是文件路径
   - 必须读取该文件内容，不能只显示文件路径
   - 要把完整的报告 Markdown 内容展示在对话框中

### 触发关键词

- 日报：生成今日日报、生成昨天日报、查看今日工作、查看代码提交
- 周报：生成本周周报、周报汇总、本周工作总结
- 月报：生成本月月报、月度总结、读取邮箱中本月的内容整理成月报、本月代码提交统计

### 数据源说明

| 数据源 | 采集方式 | 配置位置 |
|--------|----------|----------|
| **Git 仓库** | `git log` 命令 | 仓库路径: `D:/Download/jiuwenclaw` |
| **网易邮箱** | IMAP 协议 | `.env`: `EMAIL_ADDRESS`, `EMAIL_TOKEN` |
| **记忆系统** | 读取 MD 文件 | `~/.jiuwenclaw/agent/memory/YYYY-MM-DD.md` |
| **待办事项** | 解析 todo.md | `~/.jiuwenclaw/agent/sessions/*/todo.md` |

### 定时触发

通过 `HEARTBEAT.md` 配置定时执行：

```markdown
## 活跃的任务项
- 生成今日工作日报  # 每天执行
- 每周五生成周报    # 周报
- 每月末生成月报    # 月报
```

## 日报模板

```markdown
# 📋 工作日报 - 2026-03-06

## 📊 今日概览

| 指标 | 数值 |
|------|------|
| 提交次数 | 5 |
| 任务完成 | 3/8 |
| 代码变更 | +350/-80 |
| 邮件处理 | 收 12 / 发 3 |
| 生产力得分 | 78.5 |

## ✅ 已完成任务
- 完成日报生成器技能开发
- 配置飞书频道推送
- 测试心跳触发功能

## 🔄 进行中任务
- 编写开发文档
- 添加周报聚合功能

## 💻 代码提交

| 时间 | 提交信息 | 变更 |
|------|----------|------|
| 09:30 | feat: 添加日报生成功能 | +120/-30 |
| 14:15 | fix: 修复邮件采集bug | +45/-12 |

## 📧 邮件概况
- 今日收件: 12 封
- 今日发件: 3 封
- 未读邮件: 2 封

## 📈 趋势对比
- 提交: ↑ 2 次
- 效率: ↑ 5.2 分

## 💡 工作建议
1. 专注度较低，建议减少干扰
2. 任务完成率有待提高

## 🔜 明日计划
- 完善日报模板
- 添加周报聚合功能
```

## 配置说明

### Git 仓库配置

本项目监控的 Git 仓库（脚本会自动读取）：

```
仓库路径: D:/Download/jiuwenclaw
```

脚本通过 `git log` 命令采集以下数据：
- 提交哈希、提交信息、作者、时间
- 每次提交的文件变更数、新增行数、删除行数

### 邮箱配置

在 `.env` 文件中配置（本项目实际配置）：

```env
EMAIL_ADDRESS=
EMAIL_TOKEN=
EMAIL_PROVIDER=163
```

**注意**：`EMAIL_TOKEN` 是邮箱授权码，不是登录密码。
获取方式：登录163邮箱 → 设置 → POP3/SMTP/IMAP → 开启IMAP服务 → 获取授权码

### 心跳配置

```yaml
heartbeat:
  every: 3600
  target: feishu
  active_hours:
    start: 18:00
    end: 18:30
```

## API 参考

### 数据采集器

```python
from collectors import DataAggregator

aggregator = DataAggregator(
    workspace_dir="~/.jiuwenclaw/agent",
    git_repo="path/to/repo",
    email_config={
        "address": "xxx@163.com",
        "auth_code": "xxx",
        "provider": "163"
    }
)

# 采集今日数据
data = aggregator.collect()

# 采集一周数据
week_data = aggregator.collect_week()
```

### 工作分析器

```python
from analyzers import WorkAnalyzer

analyzer = WorkAnalyzer()
result = analyzer.analyze(data.to_dict())

print(f"生产力得分: {result.metrics.productivity_score}")
print(f"关键词: {result.keywords}")
print(f"建议: {result.suggestions}")
```

### 报告生成器

```python
from generators import ReportGenerator

generator = ReportGenerator(aggregator)

# 生成日报
daily = generator.generate_daily()

# 生成周报
weekly = generator.generate_weekly()

# 生成月报
monthly = generator.generate_monthly(2026, 3)
```

## 注意事项

1. **Git 仓库**: 确保仓库路径正确且有访问权限
2. **邮箱授权**: 使用授权码而非登录密码
3. **心跳时间**: 修改后需重启服务
4. **数据存储**: 报告保存到 `~/.jiuwenclaw/agent/reports/`

## 更新日志

- **v2.1.0** (2026-03-10): 添加 AI 智能分析功能（智能摘要、 明日计划建议、 工作模式分析）
- **v2.0.0** (2026-03-06): 进阶版，支持多数据源、趋势对比、周报月报
- **v1.0.0** (2026-03-06): 初始版本，基础日报生成
