# CLI 使用规则

## 命令形状

```bash
gitcode-api <resource> <method> [options]
```

## 常见资源组

CLI 资源组来自同步客户端 `GitCode`：

- `repos`：仓库元数据、仓库设置、fork、贡献者、事件、模板等。
- `contents`：文件内容、raw 文件、tree、blob、创建/更新/删除文件。
- `branches`：分支查询与创建。
- `commits`：commit 查询、compare、commit comment。
- `issues`：Issue 创建、查询、更新、评论、标签等。
- `pulls`：Pull Request 创建、查询、更新、merge、评论、审查等。
- `labels`、`milestones`、`members`、`releases`、`tags`、`webhooks`：对应仓库管理资源。
- `users`、`orgs`：用户与组织相关接口。
- `search`：搜索 `repositories`、`issues`、`users`。
- `oauth`：OAuth token 与授权 URL 辅助方法。

## 通用参数

叶子命令通常支持：

- `--api-key`：GitCode access token；默认读取 `GITCODE_ACCESS_TOKEN`。
- `--base-url`：REST API base URL，通常不需要改。
- `--timeout`：请求超时时间，单位秒。
- `--output-file`：把响应写入文件。
- `--compact`：输出单行 JSON。
- `-e` / `--escape`：把参数中的 `\n`、`\t` 等转义序列还原。

## 额外参数

部分方法在 SDK 中接受 `**params` 或 `**payload`。CLI 对这类动态参数提供：

```bash
--set key=value
--set-json '{"key": "value"}'
--set-json @payload.json
```

`--set` 的 value 会先尝试按 JSON 解析；例如 `true` 会成为布尔值，`123` 会成为数字，解析失败才作为字符串。

## 输出

大多数命令输出 JSON：

```bash
gitcode-api search repositories --q sdk --compact
```

保存到文件：

```bash
gitcode-api repos get --owner openJiuwen --repo agent-core --output-file repo.json
```

raw bytes 接口也可以写文件，例如：

```bash
gitcode-api contents get-raw --owner openJiuwen --repo agent-core --path pyproject.toml --output-file jiuwenclaw_pyproject.toml
```
