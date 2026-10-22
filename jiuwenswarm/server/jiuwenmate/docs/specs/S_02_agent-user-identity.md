# JiuwenMate Agent 级用户身份与个人数据访问规约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_02 / agent-user-identity |
| 关联模块 | `jiuwenswarm/server/jiuwenmate/lark_cli_runtime.py`、`bash_runtime.py`、`resource_assembly.py` |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | [F_24 飞书 Agent 级用户授权与个人日历读取](../features/F_24_personal-data-access.md) |
| 设计依据 | [JM-14 Lark CLI 运行时与 Skill 装配](../features/F_14_workspace-read.md) |

本规约定义 JiuwenMate Persistent Agent 如何使用其 Lark CLI profile 中的 Feishu 用户登录态读取个人资源。该身份属于 Agent 配置，不从当前消息提问者推导。

## 1. 范围、术语与适用条件

### 1.1 责任范围

- **本规约管：** 单个 Persistent Agent 的 Feishu CLI 用户登录态、身份选择和个人资源查询约束。
- **本规约不管：** 提问者本人身份隔离、Gateway 用户 OAuth Token、群内确认、JM-08 消息分发、JM-09 结果投递，以及 JM-11/JM-21 的群授权规则。

### 1.2 术语与适用条件

- **Agent CLI profile：** 由 `LARKSUITE_CLI_CONFIG_DIR` 指向的、按 `namespace_id + agent_id` 隔离的 Lark CLI 配置目录。
- **Agent 用户身份：** 曾在该 Agent CLI profile 中完成 `lark-cli auth login` 的飞书用户。它不是每次请求的提问者身份。
- **适用条件：** Feishu App 已绑定到该 Persistent Agent，CLI runtime 和相关 Skill 已由 JM-14 准备；个人日历查询还要求开发者平台允许相应用户 scope 且该 profile 已完成用户登录。

## 2. 数据与事实归属

| 对象 | 作用域 | 事实权威 |
| --- | --- | --- |
| Feishu App 与 Agent 绑定 | 一个 Agent 对应其配置的 Feishu App | JiuwenSwarm 渠道配置中的 `persistent_agent_id` 与 `app_id`；沿用 JM-08/JM-14 |
| Bot 配置 | 一个 Agent CLI profile | `runtime/<namespace_id>/<agent_id>/lark-cli/config/` 中的 CLI 配置；默认身份为 Bot |
| 用户登录态 | 一个 Agent CLI profile | 同一 CLI 配置 / 数据上下文，由 Lark CLI 管理；允许 CLI 在 app 配置中维护用户记录 |
| 群聊结果 | 原始群会话 | JM-09 投递结果及 JM-11 群消息记录；JM-21 按原有 Agent—群授权搜索 |

Agent CLI 配置中不设置 root 或 app 级 Bot-only `strictMode`。`apps[0].users` 初始可为空，登录后可由 CLI 写入用户条目；运行时不得因为该列表变为非空而判 profile 无效或清除登录资料。`defaultAs` 保持 `bot`。

## 3. 不变量

1. 每个 Agent 通过独立 `LARKSUITE_CLI_CONFIG_DIR` 使用自己的登录状态；不同 Agent 不共享本地 CLI profile。
2. 个人日历命令必须显式使用 `--as user` 并遵循已装配 Skill 的只读 scope；不以 Bot 身份尝试代查用户日历。
3. `--as user` 代表该 Agent CLI profile 中完成登录的用户，不代表当前消息发送者。私聊、群聊和不同群里的同一 Agent 复用此用户身份。
4. 群聊不要求提问者私聊确认。结果按当前执行会话回复；群聊结果对该群成员可见。不能将“群里询问”解释成“只对提问者可见”。
5. 用户授权缺失、scope 不足、API 失败或 CLI 状态未知时，说明具体限制；不得声称查询成功或把错误当成无数据。
6. Agent 提示不得要求模型复制、输出或展示 Token、Secret、用户凭据文件。提示词是行为指引，不是 Bash 系统级安全边界。
7. 新增日历查询不改变个人版、JM-08/JM-09 通用路由以及 JM-11/JM-21 现有的群访问授权。

## 4. 接口契约

### 4.1 Lark CLI 运行环境

`FeishuCliRuntime.shell_environment(namespace_id, agent_id)` 为 CLI 子进程提供：

- `LARKSUITE_CLI_CONFIG_DIR=<Agent 私有目录>/config`（未绑定时使用隔离的 `unbound` 目录）；
- `LARKSUITE_CLI_DATA_DIR=<Agent 私有目录>/data`；
- 当前 JiuwenSwarm 私有 CLI 安装目录加入子进程 `PATH`。

服务环境中继承的 Lark / Feishu 凭据变量会先被移除。该 profile 初始保留 `defaultAs: bot`，不生成 Bot-only strict mode；用户登录数据由 CLI 写入并持久保留。

### 4.2 登录和个人资源命令

- Agent 按当前已安装 `lark-shared` Skill 指引发起用户登录；非阻塞登录产生验证链接时，只将用户可打开的链接作为当前会话回复，避免在本轮阻塞等待授权。
- 用户完成授权后，Agent 在后续轮次按 Skill 指引检查登录状态。临时登录上下文失效时重新发起，不伪造登录成功。
- 日历查询由 `lark-calendar` Skill 指导现有 Bash 调用 Lark CLI；使用 `--as user`，只使用其 Skill 定义的必要只读授权范围。JM-24 不新增模型工具或 Gateway 请求/响应消息。
- 命令返回的错误、未授权和空结果须区分。未授权、限流、scope 不足和 API 故障均不得映射为 `empty`。

## 5. 生命周期与失败边界

- profile 由 JM-14 在 Agent 绑定后准备；重复准备应保留 CLI 用户登录态。
- Agent 删除、Feishu 绑定移除或 app Secret 轮换时，沿用 JM-14 profile 清理/重建规则；这可能要求该 Agent 重新登录，不影响其他 Agent profile。
- 正常登录和查询失败通过当前对话清楚反馈；不得向 Gateway 添加 OAuth callback、个人 Token vault 或私聊确认状态。
- 目前未提供仅对群提问者私密返回个人数据的能力。需要该语义时，应在新的特性中设计显式隔离或确认，而不能复用本规约作此保证。

## 6. 限制与兼容边界

- 一个 Agent 只能对应当前配置的一个 Feishu app profile；本规约不定义多用户、多租户或按会话身份映射。
- 群成员可触发使用 Agent 登录用户的日历读取，并能看到群内答案；不接受此风险的群不应启用该 Agent 的个人日历能力。
- CLI 登录和 Token 生命周期由 Lark CLI 管理；JiuwenSwarm 不读取或复制 Token，也不保证不同主机间 profile 迁移。
- 当前功能仅覆盖 Skill 可表达且飞书应用与用户均授权的个人日历只读操作；日历写操作及其他个人资料 API 不在本规约承诺范围内。

## 7. 关联规约与验证索引

- 设计和验证： [F_24 飞书 Agent 级用户授权与个人日历读取](../features/F_24_personal-data-access.md)。
- Agent / CLI 准备和 Feishu Skill 装配： [F_14 同事工作空间读写与 CLI](../features/F_14_workspace-read.md)。
- Feishu 入站绑定及群消息分发： [F_08](../features/F_08_feishu-group-fanout.md)；结果投递： [F_09](../features/F_09_web-and-feishu-result-delivery.md)。
- JM-24 的验收及真实飞书验证以开发计划对应条目和 F_24 §8 为准。
