---
name: baidu-netdisk-guide
description: "百度网盘官方 MCP Server 使用指引：文件浏览与检索、文件管理、分享链接、容量查询、内容保存的工具选择策略与写操作安全规则。当用户要求操作百度网盘（查容量、找文件、浏览整理文件、生成分享链接、保存文本或 URL 到网盘）时使用。"
description_zh: "百度网盘使用指引：浏览、检索、管理、分享、容量、内容保存的调用策略与写操作确认规则"
description_en: "Baidu Netdisk MCP guide: tool selection, path rules, search strategy, and write-operation confirmation rules"
version: 1.0.0
allowed-tools: Read,Write,Bash
---

# 百度网盘使用指引

本技能配合 `baidu-netdisk` MCP server（百度网盘官方 MCP Server）使用。调用网盘工具时必须遵守以下规则。

## 1. 工具选择策略

- 按类型浏览：文档用 `file_doc_list`、图片用 `file_image_list`、视频用 `file_video_list`；不限定类型用 `file_list`
- 查文件详情：`file_meta`（按 fsid 批量查询，一次最多 10 个；先用列表或搜索工具拿到 fsid）
- 查容量与账号：`get_quota`（总量/已用/剩余）、`user_info`（账号、会员类型）
- 首次连接验证：优先用 `get_quota` 或 `user_info` 等只读操作确认可用，不要一开始就执行写操作

## 2. 路径规则

- `dir` 参数必须是以 `/` 开头的绝对路径，默认 `/`
- 路径包含中文时需要 UrlEncode 编码，斜杠 `/` 也需要编码
- 上传与转存未指定目录时默认保存到 `/来自：mcp_server`

## 3. 检索策略

- 按文件名关键词搜索：`file_keyword_search`（`key` + `dir`，分页 `page`/`num`，默认每页 50）
- 自然语言描述搜索（内容、类型、大小、时间等特征）：`file_semantics_search`（`query` + `dir`）
- 语义检索无结果、或返回的 `content`/`abstract` 为空时，降级为关键词检索，并向用户说明
- 返回字段中的 `content`（文档分段内容/视频字幕/音频文稿）、`abstract`（内容摘要）、`thumbnail`（缩略图）可直接用于回答

## 4. 写操作必须「预览 → 确认」（强制）

以下操作执行前，必须先向用户展示操作预览并获得明确确认，未确认不得执行：

- `make_dir`：展示目标路径
- `file_copy` / `file_move`：逐项展示「原路径 → 目标目录/新文件名」及冲突策略 `ondup`
- `file_rename`：展示「原路径 → 新文件名」
- `file_del`：逐项列出将被删除的文件路径（删除不可恢复，务必确认）
- `file_sharelink_set`：展示文件清单、有效期、提取密码
- 文本保存 / URL 转存：展示目标目录与文件名

规则：

- **禁止静默使用 `overwrite` 覆盖已有文件**；冲突策略默认 `newcopy`（生成重命名副本）
- 批量操作（`filelist`）必须逐项列出后确认

## 5. 分享安全

- 创建分享前主动询问有效期与提取密码
- 用户未指定时，明示将使用默认值：有效期 7 天、密码 `1234`，并提醒可修改；不得在用户不知情时使用默认密码创建分享

## 6. 内容保存

- 文本直存：内容不超过 2 万字，默认保存为 docx；保存前与用户确认文件名与目录
- URL 转存：确认目标目录；未指定时默认 `/来自：mcp_server`

## 7. 错误处理

- 鉴权类错误（Access Token 无效或过期）：向用户说明 Token 已过期（有效期 30 天），引导更新——连接器详情页 →「解绑」→「连接」→ 打开官方授权链接重新授权 → 从地址栏复制新 access_token 粘贴到凭证弹窗
- 超频（错误码 20012）/ 无接口权限（错误码 20013）：如实转述官方错误码，建议稍后重试
- 检索无结果：建议用户更换关键词，或在使用关键词/语义两种检索方式间切换

## 8. 分页与效率

- 大目录遍历使用 `page` 分页（指定 page 时按修改时间倒序），避免一次性拉取全量
- 拿到列表后按需用 `file_meta` 补充详情，不要对每个文件重复调用列表工具
