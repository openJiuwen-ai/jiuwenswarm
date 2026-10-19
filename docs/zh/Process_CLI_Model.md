# 进程式 CLI：非交互命令的模型连接配置

[中文](Process_CLI_Model.md) · [English](../en/Process_CLI_Model.md)

在安装 JiuwenSwarm 的 Python 环境中，直接在 CMD 执行
`jiuwenswarm-process "任务文本"`。命令拥有一次本地 Runtime 生命周期，任务结束后退出；
无需先启动 Gateway、AgentServer、Web 或 TUI。入口随 JiuwenSwarm 的 wheel 包安装，
使用本文参数不需要另外安装 Python/TypeScript SDK。

## 1. 配置项

| 参数 | 填写什么 | 省略效果 / 作用 |
| --- | --- | --- |
| `-m MODEL` / `--model MODEL` | 已配置模型的名称、唯一别名或 `模型名称#全局索引` | 不指定模型连接参数时保留原有默认模型行为。单独使用时必须匹配已有配置。重复名称优先已有 `is_default` 条目，否则取首个；重复别名须改用索引。 |
| `--api-base URL` | HTTP(S) 模型服务基础地址，如 `https://your-service.example/v1` | 省略时沿用所选模型的地址；填写时必须同时指定 `--model`、`--api-key-env`。地址不能包含用户名、密码、查询参数、片段或空白。 |
| `--api-key-env ENV_NAME` | **保存密钥的环境变量名称**，如 `JIUWEN_MODEL_KEY` | 省略时沿用所选模型的凭据。填写时读取当前进程环境，变量必须已设置且非空；不会从 `.env` 自动补齐指定变量。 |
| `--model-provider OpenAI` / `Anthropic` | API 协议提供方名称，区分大小写 | 须同时指定 `--api-base`。省略时已配置模型沿用其提供方，新模型默认 `OpenAI`。OpenAI 兼容服务一般填写 `OpenAI`；实际接口须支持该提供方协议。 |

`#索引` 使用 Runtime 模型清单中的全局位置，从 0 开始，不是同名模型的出现次数。
终端模型参数仅用于带任务文本的非交互入口；REPL 继续使用 `/model`。
这些参数不能与 `--query-json`、`--run-json`、`--run-jsonl` 混用。

带任务文本的非交互命令仅支持单 Agent 普通模式，例如 `--mode agent.code.normal`
和 `--mode agent.work.normal`。通过 `--mode` 指定 Plan 或 Team 模式（包括历史别名）
会在启动 Runtime 前以退出码 `2` 拒绝；此限制也适用于 `--session` 续接命令及所有
`--output` 格式。交互式 CLI 的模式切换和 `/plan` 保持可用。
机器 JSON/JSONL 入口已有独立的模式与交互协议，不接受 `--mode`，不受本次限制影响。

工作环境统一通过完整的 `--mode` 选择：`agent.code.normal` 表示编程环境，
`agent.work.normal` 表示通用工作环境。终端不再接受独立的 `--work-mode` 参数。

## 2. 完整 CMD 示例

以下示例中的模型名称、服务地址和密钥应换成测试机的真实配置。
不需要 JSON 请求文件、`setup.cmd` 或宿主辅助脚本。

### 2.1 选择已有模型，复用 API 和密钥

```bat
jiuwenswarm-process "只回复 MODEL_OK" --model "你的模型名称或别名" --mode agent.code.normal --timeout 120
```

也可以用短参数或精确索引：

```bat
jiuwenswarm-process "只回复 MODEL_OK" -m "你的模型名称#0" --mode agent.work.normal --timeout 120
```

### 2.2 指定模型、API 地址和密钥来源

```bat
set "JIUWEN_MODEL_KEY=替换成你的真实密钥"
jiuwenswarm-process "只回复 CLI_OK" --model "服务接受的模型名称" --api-base "https://your-service.example/v1" --api-key-env JIUWEN_MODEL_KEY --model-provider OpenAI --mode agent.code.normal --timeout 120
```

上述完整组合可以使用未加入 `config.yaml` 的新模型。这里 `--model` 填服务实际接受的名称。
修改地址会重新建立连接配置，旧地址的密钥、自定义请求头和端点方言不会发送到新地址。
显式新连接启用 TLS 证书校验。

仅轮换某个已配置模型的密钥：

```bat
set "JIUWEN_MODEL_KEY=替换成你的真实密钥"
jiuwenswarm-process "只回复 KEY_OK" --model "已有模型别名" --api-key-env JIUWEN_MODEL_KEY --timeout 120
```

只更换密钥时保留原连接设置，但移除已有自定义请求头中的 `Authorization`、
`api-key`、`x-api-key`、`proxy-authorization`，避免其覆盖新密钥。

### 2.3 与工作目录、工具展示和输出格式组合

先在 `D:\cli-test\notes.txt` 写入可核对的测试内容，再执行：

```bat
jiuwenswarm-process "必须使用 read_file 读取 notes.txt，完整返回原文，不修改文件。" --model "已有模型别名" --cwd "D:\cli-test" --mode agent.code.normal --show-tools --timeout 120
jiuwenswarm-process "计算 17 加 25，只返回结果" --model "已有模型别名" --output json --timeout 120
```

检查答案和工具事件；`--output json` 返回现有普通命令的事件对象，不改变机器协议。

### 2.4 会话续接

```bat
jiuwenswarm-process "继续上一轮任务" --session "已有的 Process CLI 会话 ID" --model "服务接受的模型名称" --api-base "https://your-service.example/v1" --api-key-env JIUWEN_MODEL_KEY --timeout 120
```

连接覆盖不会写入会话或全局模型配置。每次启动新进程都应重新传入需要的连接参数，
并确保该 CMD 中仍有密钥环境变量；省略连接参数时继续按已有配置和会话行为执行。

### 2.5 帮助与退出码

```bat
jiuwenswarm-process -h
jiuwenswarm-process --help
jiuwenswarm-process -help
```

每条任务执行完后，**单独执行**下面一行查看退出码：

```bat
echo %ERRORLEVEL%
```

成功为 `0`；无效组合、未知模型、未设置密钥变量等配置错误为 `2`；
服务认证或模型运行失败为 `1`；超时沿用 `124`。不要仅凭退出码判断模型内容正确，
还需核对任务的实际答案或文件工具结果。

## 3. 配置优先级与作用范围

1. 未提供新参数：完全沿用已有模型配置、会话选择和默认行为。
2. 只提供 `--model`：使用 Runtime 的模型名称 / 别名 / 索引解析规则，复用该条目的 API 和凭据。
3. 提供 `--api-key-env`：显式环境变量密钥优先于所选模型凭据，并在加载 `.env` 前捕获，防止同名变量被覆盖。
4. 提供完整连接组合：该连接作为本次 Runtime 的默认模型，子 Agent 继承；原有默认模型组和 AgentOS 回退模型不会抢占这次连接。

覆盖采用内存中的异步上下文，不调用配置写入接口，不把 API 地址或密钥放进 Agent 请求。
其他进程中的 Web/TUI 和不在该上下文中的请求仍使用原配置。
模型密钥按 Codex 的 `env_key` 思路从环境变量读取；`--api-key-env` 的值是变量名称。
CMD 的 `set` 行包含用户自行输入的密钥，应在本机填写，不应放入共享测试资料。

## 4. 文件访问授权

启用 `permissions.enabled` 和 `permissions.file_guard.enabled` 时，进程式 CLI 对不能确定文件访问范围的
Shell 命令要求本次人工授权，包括变量命令链、重定向、解释器和未识别的程序。此类命令不会由智能审批
自动放行，也不会生成会话或永久授权。已识别的字面量路径继续使用现有路径权限规则。
普通单次命令无法接收授权输入时退出码为 `4`，工具尚未执行。机器协议继续使用既有授权响应机制。

`--cwd` 指定工作目录，`--trusted-dir` 增加信任目录；它们不是操作系统沙箱。
显式关闭权限或文件防护时沿用原有完全访问行为。此策略仅用于 `process_cli`，其他 channel 不变。

## 5. 对照 Codex

Codex 的 `exec` 支持 `-m/--model` 和 `-c/--config key=value`；
提供方配置使用 `model_providers.<id>.base_url`、`env_key` 等字段。
本实现复用“本次执行选择模型 + 指定服务地址 + 从环境读取密钥”的配置方式，
参数命名和模型调用协议仍由 JiuwenSwarm / agent-core 定义。

官方参考：[终端命令](https://learn.chatgpt.com/docs/developer-commands?surface=cli)、
[提供方配置](https://learn.chatgpt.com/docs/config-file/config-reference)、
[密钥环境变量](https://learn.chatgpt.com/docs/auth)。

---

[返回文档导航](../README.md) · [返回项目首页](../../README_CN.md)
