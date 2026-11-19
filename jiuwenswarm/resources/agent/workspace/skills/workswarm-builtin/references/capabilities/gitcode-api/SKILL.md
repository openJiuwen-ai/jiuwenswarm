---
name: gitcode-api
description: 使用 gitcode-api 命令行工具访问 GitCode REST API。适用于需要查询仓库、Issue、Pull Request、文件内容、用户、组织、搜索结果，或需要获取 Issue 与 Pull Request 模版的场景。还可用于总结仓库最近 commit / PR / issue、责任人、模块、风险或对外接口影响。
version: 1.0.0
metadata:
  version: 1.0.0
---

# gitcode-api CLI

使用 `gitcode-api` 命令行调用 GitCode REST API。这个技能只覆盖 `gitcode-api` 1.2.14 及以上版本的 CLI。

## Quickstart

1. 先确认 CLI 是否可用：

```bash
gitcode-api --version
```

2. 如果未安装或版本低于 `1.2.14`，提示用户安装或升级：

```bash
pip install -U gitcode-api
```

也可以使用 `uv` 临时运行或安装：

```bash
uvx gitcode-api --version
uv pip install -U gitcode-api
```

3. 优先使用环境变量保存 token，避免把 token 写进命令历史：
- 用户需要在 `~/.jiuwenclaw/config/.env` 中配置 `GITCODE_ACCESS_TOKEN` 环境变量
- 若用户没有GitCode token，告知可以使用此链接新建并按需配置读写权限：https://gitcode.com/setting/token-classic

4. CLI 基本形状：

```bash
gitcode-api <resource> <method> [options]
```

例如：

```bash
gitcode-api repos get --owner openJiuwen --repo agent-core
gitcode-api pulls list --owner openJiuwen --repo agent-core --state open
gitcode-api search repositories --q sdk
```

5. 不确定命令或参数时，先看 help，不要猜参数：

```bash
gitcode-api --help
gitcode-api pulls --help
gitcode-api pulls create --help
```

## 使用原则

- 优先运行 `gitcode-api --version` 和分级 help 来确认本机实际 CLI 能力。
- 资源组和方法名使用 kebab-case，例如 SDK 的 `build_authorize_url` 在 CLI 中是 `build-authorize-url`。
- 大多数命令输出 JSON；需要机器处理时使用 `--compact`，需要保存结果时使用 `--output-file`。
- 遇到 `**params` 或 `**payload` 类型的额外参数，用 `--set key=value` 或 `--set-json '{...}'`。
- 如果用户要求的是完整开发流程、提交、创建 PR、解决 Issue，本技能只负责 GitCode CLI 查询或调用部分，不负责端到端工程流程。

## 何时阅读参考文件

- 安装、版本、token、`uv` 用法：读 [references/install-and-auth.md](references/install-and-auth.md)。
- CLI 语法、通用参数、输出保存、转义：读 [references/cli-usage.md](references/cli-usage.md)。
- 查找资源组、方法、参数签名：读 [references/command-discovery.md](references/command-discovery.md)。
- 常见任务示例：读 [references/common-recipes.md](references/common-recipes.md)。
- 报错、权限、参数、MCP server 问题：读 [references/troubleshooting.md](references/troubleshooting.md)。

## 任务文档

- 总结仓库最近 commit / PR / issue、责任人、模块、风险或对外接口影响：读 [tasks/summarize-repo-changes.md](tasks/summarize-repo-changes.md)。

## 脚本入口

如果当前环境更适合运行 Python 脚本（不建议），可调用：

```bash
python scripts/gitcode_api_cli.py --help
```

该脚本只是 `gitcode_api.cli.main` 的薄封装，行为应与 `gitcode-api` 控制台命令一致。

## 企业网络 SSL / 自定义 CA

可通过设置环境变量 `GITCODE_CA_BUNDLE` 或 `REQUESTS_CA_BUNDLE` 指定默认**CA 证书路径**。
