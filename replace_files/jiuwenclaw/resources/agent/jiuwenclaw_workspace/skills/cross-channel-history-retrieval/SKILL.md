---
name: cross-channel-history-retrieval
description: >-
  跨会话检索聊天原文（记忆不足时再用）。在回答任何关于历史事件、日期、人物、过去对话的问题时，如果记忆中没有相关信息或不足以回答，则需要使用跨会话检索聊天原文。用 mcp_exec_command 执行 scripts/search_history.py，读 ~/.jiuwenclaw/agent/sessions/*/history.json。支持 channel、session_id、关键词、时间窗。如果搜索结果不足，尝试用不同的关键词再次搜索。
allowed_tools: [mcp_exec_command]
---

# 跨频道历史检索

用于从 `~/.jiuwenclaw/agent/sessions/<session_id>/history.json` 中检索历史消息，并把命中结果整理为可直接粘贴进当前上下文的文本块。

## 何时使用

- 用户提到“其他频道/会话”的聊天内容，或在 **A 频道问自己在 B 频道（如网页）说过什么**
- **「今天/刚才我问了什么」「关于某某我提过什么问题」** 且当前会话里看不到原文
- 用户给出关键词，要求回溯某时间段对话
- 用户要求把检索结果“带到当前上下文里”

## 执行方式

必须使用 `mcp_exec_command` 执行脚本，不要只口头总结。

```bash
python ~/.jiuwenclaw/agent/skills/cross-channel-history-retrieval/scripts/search_history.py --channel feishu --query "报销 审批" --start "2026-03-26 09:00" --end "2026-03-26 18:00" --limit 30
```

（Windows：**`--channel` / `--query` / 时间参数等与 Unix 相同**；默认 `mcp_exec_command` 走 **cmd**，**cmd 不会展开 `~`**，不要用 `~/.jiuwenclaw`，应写 `python %USERPROFILE%\.jiuwenclaw\agent\skills\cross-channel-history-retrieval\scripts\search_history.py` 再接同样参数。若整条命令在 PowerShell 里执行，`~` 一般会展开，也可用 `$env:USERPROFILE\...`。）

## 参数说明

- `--channel`：按频道过滤（如 `feishu` / `dingtalk` / `web`）。如果用户在语言里没有指明 channel，则不要传 `--channel`，脚本会扫描所有会话。
- `--session-id`：只检索指定会话（优先级高于 `--channel`）
- `--query`：空格分词关键词（例如 `"合同 审批"`）
- `--keyword`：可重复传入多个精确关键词
- `--start`、`--end`：显式时间范围，格式支持
  - `YYYY-MM-DD`
  - `YYYY-MM-DD HH:MM`
  - `YYYY-MM-DD HH:MM:SS`
  - ISO8601（如 `2026-03-26T10:30:00+08:00`）
- `--at`：某个时间点，配合 `--window-minutes` 形成检索窗
- `--window-minutes`：窗口大小（默认 120 分钟）
- `--timezone`：默认 `Asia/Shanghai`
- `--limit`：返回命中上限（默认 20）
- `--max-sessions`：最多扫描会话数量（默认 200）
- `--auto-expand`：无命中时自动扩大时间窗重试（默认开启）

## 时间策略

1. 若用户明确给了开始/结束时间，优先使用。
2. 若仅给“某时刻”，使用 `--at + --window-minutes`。
3. 若用户没给时间，使用默认窗口（最近 24 小时）。
4. 若初次无结果且 `--auto-expand` 开启，自动扩展为最近 72 小时再试一次。

## 输出与上下文注入

脚本**第一行**固定输出 `SKILL=cross-channel-history-retrieval`，便于在 `mcp_exec_command` 回显或日志里 `grep` 确认本 skill 的脚本已执行。

脚本会输出两个区块：

- `HISTORY_SEARCH_SUMMARY`：统计信息与最终时间窗
- `HISTORY_CONTEXT_BLOCK`：可直接放入当前对话上下文的命中消息片段

拿到脚本输出后，你应当：

1. 在回复中简要说明检索范围和命中情况。
2. 把 `HISTORY_CONTEXT_BLOCK` 中的内容原样（或轻度裁剪）贴到当前回复里，作为上下文依据。
3. 若无命中，明确说明已检索的时间窗、频道/会话和关键词，并询问是否放宽条件。
