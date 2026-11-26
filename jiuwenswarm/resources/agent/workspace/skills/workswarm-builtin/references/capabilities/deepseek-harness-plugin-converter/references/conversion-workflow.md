# 转换与开发流程

## 1. 确认目标源码

按 [系统插件开发 skill](../../jiuwen-system-plugin-dev/SKILL.md#1-确认目标环境和目录) 的源码优先级、按域选择和现有运行环境执行。
用户指定目录优先；缺少源码时按需复用稀疏缓存。先从 [示例索引](../../jiuwen-system-plugin-dev/references/example.md) 找相近实现，再集中查本次尚未确认的接口。

修改已经转换好的系统插件时，按 [已有系统插件修改流程](../../jiuwen-system-plugin-dev/SKILL.md#1-确认目标环境和目录) 使用用户指定的源码或查询返回的安装副本，不搜索历史工作区、记忆或无关源码。效果确认尚未结束时保持当前版本；需要交付新版本时，先复制到本次工作目录，再在副本中递增版本并修改。只有需要核对原插件行为或目标接口时，才补查相应 DSH 源码和平台源码。

## 2. 建立转换映射

不要翻译 Cordis API 名称，要把原插件的行为映射到 JiuwenSwarm 公开扩展点：

| DSH 能力 | JiuwenSwarm 中需要寻找的对应能力 |
| --- | --- |
| `apply(ctx)` 注册和生命周期 | `create_extension()`、`register(registrar)`、系统插件加载与卸载 |
| Context 事件或服务 | 已登记事件、Matcher、Handler 链和 Ops |
| tool / command | 已有业务事件或公开 Ops；没有真实触发入口时不能伪造 |
| profile 或 session 状态 | 系统插件实例、会话隔离状态、宿主已有持久化能力 |
| Host RPC | 对应 domain 的事件、Ops 或 UI 上下行协议 |
| Client 页面、slot、widget | Gateway 已支持的 UI contribution、槽位和组件 |
| 定时任务、进程、网络 | 宿主已有调度、Shell、网络或审批能力 |
| MCP bundle | 目标环境已有 MCP 接入与工具注册能力 |

映射表至少记录：

| 原能力 | 目标事件或 Ops | domain | Handler 行为 | UI 槽位 | 验证入口 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |

结论只有三类：直接支持、可用现有能力等价实现、缺少必要能力。最后一类如果影响核心功能，停止开发并向用户说明；只影响非核心体验时采用最佳现有实现。

## 3. 选择单域或组合包

映射表只有一个 domain 时，沿用单域结构。核心行为同时需要两域时，按 [组合包开发](../../jiuwen-system-plugin-dev/references/bundle-development.md) 拆分成员，整体交付一个系统插件 ZIP。
不要把 DSH Host/Client 与 Gateway/AgentServer 机械对应，域由真实触发点和 Ops 的归属决定；两域协作使用已有业务链路，不跨进程传递 Ops。

## 4. 开发和验证

实现、源码版本、测试及交付按 [共享开发流程](../../jiuwen-system-plugin-dev/SKILL.md#3-编写系统插件说明和实现) 执行，脚本也使用该 skill 的原文件。
原插件带页面或嵌套网页时，按 [前端说明](../../jiuwen-system-plugin-dev/references/frontend-development.md) 区分组件权限、来源限制和外部网页策略，确认缺口后再停止转换。
保留原插件来源、许可和行为映射；其余共通约束不在本文件重复维护。
