# 常见任务示例

示例中的 owner 和 repo 使用占位值；实际执行前替换为用户目标仓库。若本机安装版本不同，先用 `gitcode-api <resource> <method> --help` 确认参数。

## 查看当前登录用户

```bash
gitcode-api users me
```

## 获取仓库概览

用 CLI 分三步获取仓库元数据、分支和最近提交。

```bash
gitcode-api repos get --owner openJiuwen --repo agent-core
gitcode-api branches list --owner openJiuwen --repo agent-core --per-page 5
gitcode-api commits list --owner openJiuwen --repo agent-core --per-page 5
```

只需要单独信息时，可分别调用：

```bash
gitcode-api repos get --owner openJiuwen --repo agent-core --compact
gitcode-api branches list --owner openJiuwen --repo agent-core --per-page 20
gitcode-api commits list --owner openJiuwen --repo agent-core --per-page 20
```

## 列出分支

CLI 只暴露同步命令形状，不区分 sync / async。

```bash
gitcode-api branches list --owner openJiuwen --repo agent-core --per-page 20
```

## 列出 Pull Requests

```bash
gitcode-api pulls list --owner openJiuwen --repo agent-core --state open --per-page 20
```

如果用户在环境中设置了 `GITCODE_PULL_STATE`，CLI 不会自动读取这个变量；需要显式传成 `--state "$GITCODE_PULL_STATE"`。

## 获取 Pull Request 模板

先列出 SDK 解析后的 active PR templates：

```bash
gitcode-api pulls list-templates --owner openJiuwen --repo agent-core
```

从返回结果中选择 `path`。如果模板来自继承解析后的其它仓库，使用返回里的 `template_owner` / `template_repo` 作为 `--owner` / `--repo`：

```bash
gitcode-api pulls get-template \
  --owner openJiuwen \
  --repo .gitcode \
  --path .gitcode/PULL_REQUEST_TEMPLATE.md
```

把模板正文保存到文件：

```bash
gitcode-api pulls get-template \
  --owner openJiuwen \
  --repo .gitcode \
  --path .gitcode/PULL_REQUEST_TEMPLATE.md \
  --output-file PULL_REQUEST_TEMPLATE.md
```

## 创建 Pull Request

先查看参数：

```bash
gitcode-api pulls create --help
```

再按签名传参，例如：

```bash
gitcode-api pulls create \
  --owner openJiuwen \
  --repo agent-core \
  --title "Add feature" \
  --head feature-branch \
  --base main \
  --body "Implements the new flow."
```

本技能只提供 CLI 调用方法；不要把它扩展成完整的开发、提交、解决 Issue、创建 PR 工作流。

## 合并 Pull Request

```bash
gitcode-api pulls merge --owner openJiuwen --repo agent-core --number 42
```

执行破坏性或不可逆操作前，先向用户确认目标仓库、编号和意图。

## 列出 Issues

```bash
gitcode-api issues list --owner openJiuwen --repo agent-core --state open
```

## 获取 Issue 模板

先列出 SDK 解析后的 active issue templates：

```bash
gitcode-api issues list-templates --owner openJiuwen --repo agent-core
```

从返回结果中选择 `path`。如果模板来自继承解析后的其它仓库，使用返回里的 `template_owner` / `template_repo` 作为 `--owner` / `--repo`：

```bash
gitcode-api issues get-template \
  --owner openJiuwen \
  --repo .gitcode \
  --path .gitcode/ISSUE_TEMPLATE.zh/001-bug-report.yml
```

把模板正文保存到文件：

```bash
gitcode-api issues get-template \
  --owner openJiuwen \
  --repo .gitcode \
  --path .gitcode/ISSUE_TEMPLATE.zh/001-bug-report.yml \
  --output-file ISSUE_TEMPLATE_BUG_REPORT.zh.yml
```

## 创建 Issue

```bash
gitcode-api issues create \
  --owner openJiuwen \
  --repo agent-core \
  --title "Bug report" \
  --body "Steps to reproduce..."
```

## 搜索仓库

```bash
gitcode-api search repositories --q sdk
```

带分页参数时，先用 help 确认该版本支持的参数名：

```bash
gitcode-api search repositories --help
```

## 获取文件内容

```bash
gitcode-api contents get --owner openJiuwen --repo agent-core --path <repo-file-path>
```

获取 raw 文件并保存：

```bash
gitcode-api contents get-raw \
  --owner openJiuwen \
  --repo agent-core \
  --path <repo-file-path> \
  --output-file <output-file>
```

## 列出 Releases

```bash
gitcode-api releases list \
  --owner openJiuwen \
  --repo agent-core \
  --per-page 50 \
  --page 1
```

如需翻页，递增 `--page`，直到返回数量小于 `--per-page`。

## 列出 Tags

读取 GitCode tags 并建立 tag 到 commit sha 映射时，可以分页列出 tags。

```bash
gitcode-api tags list \
  --owner openJiuwen \
  --repo agent-core \
  --per-page 50 \
  --page 1
```

## 创建 Release

```bash
gitcode-api releases create \
  --owner openJiuwen \
  --repo agent-core \
  --tag v1.2.3 \
  --name "1.2.3" \
  --body "Release notes..." \
  --target-commitish <commit-sha> \
  --release-status latest
```

预发布可使用：

```bash
gitcode-api releases create \
  --owner openJiuwen \
  --repo agent-core \
  --tag v1.2.3-rc.1 \
  --name "1.2.3 rc1" \
  --body "Release candidate notes..." \
  --target-commitish <commit-sha> \
  --release-status pre
```

创建 release 会改变远端仓库状态；执行前先向用户确认 tag、目标 commit、标题和正文。

## 上传 Release 附件

```bash
gitcode-api releases upload \
  --owner openJiuwen \
  --repo agent-core \
  --tag v1.2.3 \
  --file-name dist.zip \
  --content dist.zip \
  --upload-timeout 6000
```

`--content` 可以传文件路径，SDK 会读取该路径的 bytes 后上传。复杂同步或大量附件上传更适合写 Python 脚本调用 SDK。

## 使用额外查询参数

```bash
gitcode-api pulls list \
  --owner openJiuwen \
  --repo agent-core \
  --set only_count=true \
  --set reviewer=octocat
```

## 使用 JSON payload

```bash
gitcode-api repos update \
  --owner openJiuwen \
  --repo agent-core \
  --set-json '{"description": "Updated from CLI", "has_wiki": false}'
```

payload 较长时写入文件：

```bash
gitcode-api repos update --owner openJiuwen --repo agent-core --set-json @payload.json
```

## 构建 OAuth 授权 URL

```bash
gitcode-api oauth build-authorize-url \
  --client-id client-id \
  --redirect-uri https://example.com/callback \
  --scope user_info \
  --state opaque-state
```

## 查看资源组和方法

资源组列表：

```bash
gitcode-api --help
```

某个资源组的方法列表：

```bash
gitcode-api pulls --help
```

某个方法的签名和参数：

```bash
gitcode-api pulls list-issues --help
```

CLI help 的方法顺序与 SDK 的 `resource.methods` 顺序一致；方法级 help 开头会展示对应 SDK 签名。
