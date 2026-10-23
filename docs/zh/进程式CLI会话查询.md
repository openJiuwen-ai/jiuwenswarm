# 进程式 CLI 会话查询

> **目标**：在 CMD 中查询本地 Process CLI 的完整会话 ID，并用新进程继续任务。
>
> **英文版：** [English Version](../en/ProcessCLISessionQueries.md)

---

## 1. 查询会话

安装包含此功能的 JiuwenSwarm 主程序后，可以直接执行：

```cmd
jiuwenswarm-process --list-sessions
```

命令读取当前数据目录中的 Process CLI 会话，展示完整 ID、最近消息时间、模式、标题和项目目录，随后退出。它同时包含 Agent 和 Team 会话，按最近消息时间倒序排列；相同时间按完整 ID 排序。查询不启动 Agent/Runtime，不调用模型，也不写回会话元数据。

列表按本页内容的终端显示宽度对齐，列间至少保留两个空格，首列表头为“会话ID”。完整 ID、标题和项目目录不截断；内容超过终端宽度时可能自然折行。

查询范围是当前数据目录中的全部 Process CLI 会话，不按 CMD 的当前目录自动筛选。Web、TUI 等其他 channel 的会话不在列表中。已归档或标记为临时的会话沿用本地目录查询的过滤规则。

默认数据目录是 `%USERPROFILE%\.jiuwenswarm`；已经设置 `JIUWENSWARM_DATA_DIR` 时使用该目录。查询与续接必须使用同一个数据目录。

## 2. 搜索和翻页

| 参数 | 填写内容 | 默认值与作用 |
| --- | --- | --- |
| `--list-sessions` | 开关，无参数值 | 列出一页会话；没有匹配会话时展示说明，退出码 0。 |
| `--latest-session` | 开关，无参数值 | 只在 stdout 输出最近消息会话的完整 ID；没有匹配会话时 stderr 提示，退出码 1。 |
| `--search` | 最多 200 字符的关键词 | 默认不搜索；匹配完整 ID、标题、项目 ID、项目目录和模式。多个空格分隔关键词需要全部匹配，忽略大小写。适用于两种查询。 |
| `--limit` | 1–200 的整数 | 列表每页条数，默认 20；仅适用于 `--list-sessions`。 |
| `--offset` | 非负整数 | 跳过的匹配条数，默认 0；仅适用于 `--list-sessions`。 |

```cmd
jiuwenswarm-process --list-sessions --search "项目名" --limit 10 --offset 0
jiuwenswarm-process --list-sessions --search "项目名" --limit 10 --offset 10
jiuwenswarm-process --latest-session --search "项目名"
```

查询命令独立运行，不填写任务文本，也不混用 `--session`、`--cwd`、`--mode`、`--output`、`--timeout` 等任务参数。模型、工作目录等运行配置在续接任务时填写。无查询入口的 `--search`、`--limit`、`--offset` 以及非法或冲突参数返回退出码 2。

## 3. 使用查询到的 ID 续接

先执行一轮真实任务：

```cmd
jiuwenswarm-process "请记住口令 BLUE_2026。只回复该口令。" --mode agent.work.normal --timeout 120
```

等待任务结束、CMD 提示符恢复后，查询刚才的会话：

```cmd
jiuwenswarm-process --list-sessions
jiuwenswarm-process --latest-session
```

把得到的完整 ID 填入新的任务命令：

```cmd
jiuwenswarm-process "只回复上一轮让你记住的口令。" --session "替换为查询到的完整会话ID" --mode agent.work.normal --timeout 120
```

预期新进程恢复会话，回复 `BLUE_2026`。不要填写终端中带省略号的缩略 ID；Team 会话续接时填写原会话的对应 `--mode`。

CMD 也可以自动捕获一个搜索范围内的最近 ID。以下写法适用于直接在 CMD 输入；写入 `.cmd` 文件时把 `%S` 改为 `%%S`：

```cmd
set "CLI_SESSION_ID="
for /f "delims=" %S in ('jiuwenswarm-process --latest-session --search "项目名"') do @set "CLI_SESSION_ID=%S"
if defined CLI_SESSION_ID jiuwenswarm-process "继续上一轮任务" --session "%CLI_SESSION_ID%" --mode agent.work.normal --timeout 120
```

无匹配会话时不执行续接。并发执行多个任务时，需用列表核对目标会话；“最近”不保证就是某个特定进程刚完成的会话。

## 4. 查看帮助和检查退出码

```cmd
jiuwenswarm-process -h
jiuwenswarm-process --help
jiuwenswarm-process -help
```

三种写法均显示新查询参数及查询、续接示例，随后退出。

每条命令结束后立即单独执行：

```cmd
echo %ERRORLEVEL%
```

查询成功返回 0；`--latest-session` 无匹配会话或存储读取失败返回 1；参数错误返回 2；中断返回 130。查询到 ID 表示会话可被定位；续接任务的实际完成情况仍需核对回复、工具结果和退出状态。

---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)
