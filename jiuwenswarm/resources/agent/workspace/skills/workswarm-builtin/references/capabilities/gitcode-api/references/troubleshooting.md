# Troubleshooting

## `gitcode-api: command not found`

安装或升级：

```bash
pip install -U gitcode-api
```

也可以临时运行：

```bash
uvx gitcode-api --version
```

## 版本低于 1.2.16

升级：

```bash
pip install -U gitcode-api
```

然后重新检查：

```bash
gitcode-api --version
```

## token 或权限错误

检查环境变量是否存在：

```bash
printf '%s\n' "${GITCODE_ACCESS_TOKEN:+GITCODE_ACCESS_TOKEN is set}"
```

不要打印 token 原文。若 token 已设置但仍失败，通常是权限范围、仓库可见性或 token 过期问题。
若没有GitCode token，告知用户可以使用此链接新建并按需配置读写权限：https://gitcode.com/setting/token-classic

## 缺少 owner / repo

仓库相关命令通常需要 `--owner` 和 `--repo`：

```bash
gitcode-api repos get --owner <owner> --repo <repo>
```

如果报配置错误，回到方法 help 确认该方法需要哪些仓库上下文参数。

## Unknown argument 或 Missing required parameter

先看方法级 help：

```bash
gitcode-api <resource> <method> --help
```

只传该版本实际支持的参数。Python SDK 的下划线参数名在 CLI 中通常是连字符形式，例如 `pull_number` 变为 `--pull-number`。

## JSON 参数解析失败

`--set-json` 必须是 JSON object，或 `@` 加 JSON 文件路径：

```bash
gitcode-api repos update --owner <owner> --repo <repo> --set-json @payload.json
```

shell 中 JSON 引号容易出错时，优先使用文件。

## 多行正文没有换行

使用 `-e` / `--escape`：

```bash
gitcode-api pulls update ... --body 'Line1\nLine2' -e '\n'
```

## 企业网络 SSL / 自定义 CA

可通过设置环境变量 `GITCODE_CA_BUNDLE` 或 `REQUESTS_CA_BUNDLE` 指定默认**CA 证书路径**。

## 文档与反馈

- [官方文档](https://gitcode-api.readthedocs.io)
- [Changelog](https://gitcode-api.readthedocs.io/en/latest/changelog.html)

若以上步骤仍无法解决问题，欢迎在仓库提交 Issue：

- [GitHub Issues](https://github.com/Trenza1ore/GitCode-API/issues)
- [GitCode Issues](https://gitcode.com/SushiNinja/GitCode-API/issues)

## 创建 Issue 反馈示例

> 请与用户确认，不要直接自行创建。推荐告知用户 GitHub / GitCode Issue 链接并由用户创建，可帮用户构思标题与正文。

### 自行创建 GitCode Issue

需已设置 `GITCODE_ACCESS_TOKEN`（见上文 token 小节）：

```bash
gitcode-api issues create \
  --owner "Trenza1ore" \
  --repo "GitCode-API" \
  --title "gitcode-api CLI feedback" \
  --body "What happened and what you expected..."
```

### 自行创建 GitHub Issue

技能目录下的 [scripts/open_github_issue.py](../scripts/open_github_issue.py) 依赖 [githubkit](https://github.com/yanyongyu/githubkit)。在技能根目录执行，需设置 `GITHUB_ACCESS_TOKEN`（或传入`--api-key`）：

```bash
uv run --with githubkit scripts/open_github_issue.py \
  "gitcode-api CLI feedback" \
  --body "What happened and what you expected..."
```

可选 `--label`（可重复），例如 `--label bug`。未传 `--owner` / `--repo` 时默认为 `Trenza1ore/GitCode-API`。
