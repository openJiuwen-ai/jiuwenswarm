# Team Organization 专家团与汇总配置

本文说明 JiuwenSwarm 如何把 AgentGroup 模板接入 Team Organization，以及共享 Summary Team 如何读取配置和交付结果。

---

## 一、AgentGroup 发现

JiuwenSwarm 从 local、built-in 和 resources 三类扩展根目录扫描 `agent_groups`。只有满足以下条件的目录才会进入专家目录：

- 目录名是安全的单个名称，不包含路径分隔符。
- 存在可解析的 `manifest.json`。
- `package_type` 为 `agent_group`。
- manifest 的 `name` 与目录名完全一致。
- 同名包只出现在一个来源中。
- AgentGroup 包可以被完整加载并通过模板校验。

目录扫描只返回模板描述，不创建 Team。`org_create_and_invite_expert_team` 被调用后，Launcher 才会装配并激活实例。

## 二、专家 Team 配置继承

专家 Team 使用 AgentGroup 中的 Leader、预置成员、技能、提示词和 capabilities，同时从当前 JiuwenSwarm Team 配置继承运行底座，包括：

- 默认模型与模型路由。
- storage 和共享 `TeamDatabase`。
- transport 与 spawn mode。
- workspace 和系统操作能力。
- 当前 session 与 Organization 上下文。

同一个 AgentGroup 可以创建多个实例。运行时 `team_id` 由 Launcher 唯一生成，不能在提示词中把 AgentGroup 包名当作目标 `team_id`；创建后应通过 `org_view_organization` 读取真实 ID。

## 三、Summary Team

JiuwenSwarm 的 Summary Team 是一个固定的持久 Team：

| 成员 | 职责 |
|------|------|
| `summary-leader` | 校验来源、协调汇总并提交最终结果 |
| `source-integrator` | 把只读来源整理为带来源归属的结构化提纲 |
| `delivery-drafter` | 根据来源和提纲生成面向用户的最终稿 |

它使用默认 Team 模型，但不会复制用户 Team 的成员、工具或模板提示词。内部 transport 固定为 `inprocess`，成员使用 `build_mode`；中间稿写入自身 Team workspace，最终交付写入 Organization workspace 的 `summary/`。

每个 Organization 最多有一个共享 Summary Team。第一次创建 `SUMMARY_TEAM` execution 时懒加载，后续根任务复用；完成一次汇总不会立即停止它。

## 四、配置建议

```yaml
modes:
  team:
    jiuwen_team:
      lifecycle: persistent
      spawn_mode: inprocess
      teammate_mode: build_mode
      agents:
        leader: $agent_leader
      workspace:
        enabled: true
      transport:
        type: inprocess
      storage:
        type: sqlite
```

生产环境建议：

- 使用稳定且所有 Team 可访问的数据库；多进程场景优先 PostgreSQL。
- 为 Organization Workspace 配置共享文件系统或对象存储映射。
- 确保默认 leader 和 teammate 模型均可解析。
- AgentGroup capabilities 使用稳定、小写、面向领域的标签，提示词中使用完全相同的值。
- 不要在配置中预启动每一个专家 Team；由 Owner Leader 按任务需要创建。

## 五、生命周期与故障处理

专家 Team 创建成功后会进入暂停待命状态，由 Organization 事件唤醒。若启动后入组失败，Launcher 会停止临时 Team。Summary Team 的创建状态与当前 Summary Execution 分开持久化；进程重启后会恢复共享实例并重新检查来源是否就绪。

排障时重点查看：AgentGroup 扫描日志、专家/汇总 Launcher 日志、Organization 进度事件、Team runtime 状态，以及各 Team 是否持有同一个数据库实例。

