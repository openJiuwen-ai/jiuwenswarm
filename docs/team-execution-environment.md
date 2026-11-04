# Code 集群会话的独立 Git 身份与执行环境

日期：2026-11-04

## 产品行为

Code 集群输入框「＋」菜单提供「独立 Git 身份」，默认关闭。它是 team 会话设置，
同时作用于 leader、预定义成员和动态成员，不提供成员级开关。
新会话草稿在首次 chat.send 携带；已有会话通过 session.execution_settings 读取和更新，
确认成功才改变 UI 状态，失败明确提示。切换影响之后开始的工具调用，不修改在途命令或后台进程。

## 数据与执行链路

请求结构：

```json
{
  "session_id": "example",
  "expected_version": 0,
  "settings": {
    "independent_git_identity": true,
    "environment": {"BUILD_PROFILE": "debug", "REMOVE_THIS": null}
  }
}
```

省略 settings 是读取。没有记录时返回 version=-1，不写入默认值，避免首次发送前的
读取覆盖草稿。更新必须携带 expected_version，冲突时重新读取。environment 是可扩展的
内部/API 配置，本次 UI 只显示 Git 身份开关。

共享团队数据库的 team_session_settings 是权威值，metadata.execution_settings 是恢复镜像。
首次执行前初始化设置，之后 chat.send 中的旧值不覆盖权威值。SwarmBuildContext 的
execution_settings_managed 随 seed 跨进程传递；成员每次工具执行从自身 backend 的数据库
读取当前设置，使用自己的 member_name 生成快照。读取失败时工具不执行。

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
冷恢复 seed、数据库 CAS、shell 普通/流式/后台路径和前端请求状态。
Git amend、cherry-pick 等继续遵循 Git 的作者保留语义，不自动 reset-author。
这不是防伪机制；外部独立 CLI、浏览器或 MCP 服务自行启动的命令不会自动被此作用域覆盖。
跨机器成员必须连接同一持久化团队数据库；纯内存存储不支持会话设置，开关返回明确错误。
本次不提供环境变量编辑器。

## English summary

The Code team composer exposes an opt-in **Independent Git identity** switch.
It is persisted per team session and affects new tool invocations for every native member.
Each member signs new Git commits with its member name and an `@jiuwenswarm.com` email.
Disabling the switch restores ordinary Git/environment behavior. Generic execution environment
overlays support values and removals independently of Git. Session settings are versioned in
the shared team database; concurrent stale updates fail, and in-flight commands keep their snapshot.
