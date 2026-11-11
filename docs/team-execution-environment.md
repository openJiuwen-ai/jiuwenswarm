# Code 集群会话的独立 Git 身份与执行环境

日期：2026-11-04

## 产品行为

Code 集群输入框「＋」菜单提供「独立 Git 身份」，默认关闭。它是 team 会话设置，
同时作用于 leader、预定义成员和动态成员，不提供成员级开关。
开关状态随每次 team 模式的 chat.send 一起下发（`execution_settings`），下一条消息起生效，
影响之后开始的工具调用，不修改在途命令或后台进程。设置不走独立 RPC：Web 请求经 agent-gateway
转发，而 gateway 只转发白名单内的方法，chat.send 本来就在其中，因此无需改动 gateway。

## 数据与执行链路

chat.send 参数：

```json
{
  "execution_settings": {
    "independent_git_identity": true,
    "environment": {"BUILD_PROFILE": "debug", "REMOVE_THIS": null}
  }
}
```

共享团队数据库的 team_session_settings 是权威值，metadata.execution_settings 是恢复镜像。
会话的首条消息用请求值初始化记录；之后的消息只把请求里出现的字段合并进已提交的记录，
以 version 做 CAS，并发冲突时基于最新值重试一次。不携带 execution_settings 则保持原值。
前端打开会话时从会话元数据的 execution_settings 读回开关状态。environment 是可扩展的
内部/API 配置，本次 UI 只显示 Git 身份开关。

SwarmBuildContext 的 execution_settings_managed 随 seed 跨进程传递；成员每次工具执行从自身
backend 的数据库读取当前设置，使用自己的 member_name 生成快照，因此运行中的团队在下一次工具
调用就能看到更新。读取失败时工具不执行。

开启时注入 GIT_AUTHOR_NAME / GIT_COMMITTER_NAME 为成员名，邮箱为
`<member_name>@jiuwenswarm.com`；不适合邮箱前缀的名称使用稳定哈希前缀，显示作者名保持原值。
关闭时不生成四个 Git 变量，保留用户原有 Git 配置与通用环境值。

优先级：执行端基础环境 < 通用 values < 单次显式参数 < Git 策略 overrides。
Core 的工具作用域提供隔离与清理，shell 合并环境；Swarm 的 mcp_exec_command 同样接入。
沙箱只传环境增量，不传宿主机完整环境。环境参数日志脱敏。

## 为什么不让模型传 env

成员身份与会话策略属于运行时。模型无需记忆参数，脚本中启动的 git 也会继承环境。
不改 git config 和 os.environ，避免污染人工操作或并发成员。
共享数据库读取代替开关广播，避免丢事件和离线成员恢复后读旧值。

## 验证与边界

定向测试覆盖真实 Git 提交的 author/committer、关闭恢复原身份、成员隔离、设置更新、
冷恢复 seed、chat.send 合并更新与 CAS、shell 普通/流式/后台路径和前端开关状态恢复。
Git amend、cherry-pick 等继续遵循 Git 的作者保留语义，不自动 reset-author。
这不是防伪机制；外部独立 CLI、浏览器或 MCP 服务自行启动的命令不会自动被此作用域覆盖。
跨机器成员必须连接同一持久化团队数据库；纯内存存储不支持会话设置，开关返回明确错误。
本次不提供环境变量编辑器。

## English summary

The Code team composer exposes an opt-in **Independent Git identity** switch.
It is persisted per team session and affects new tool invocations for every native member.
Each member signs new Git commits with its member name and an `@jiuwenswarm.com` email.
Disabling the switch restores ordinary Git/environment behavior. Generic execution environment
overlays support values and removals independently of Git. The setting travels with every team
`chat.send` (no separate RPC, so the Gateway needs no change): the first turn seeds the versioned
record in the shared team database, later turns merge the carried fields with compare-and-swap,
and clients restore the switch from the `execution_settings` session-metadata mirror.
In-flight commands keep their snapshot.
